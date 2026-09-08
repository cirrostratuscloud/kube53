"""Reconcile Kubernetes Services (+ their Deployments) into ECS services.

The idiomatic Kubernetes shape works here: a Deployment defines the workload
(image, replicas, container port) and a Service selects its Pods by label.

  apiVersion: apps/v1
  kind: Deployment
  metadata: { name: web }
  spec:
    replicas: 2
    selector: { matchLabels: { app: web } }
    template:
      metadata: { labels: { app: web } }
      spec:
        containers:
          - name: web
            image: nginx:latest
            ports: [{ containerPort: 80 }]
  ---
  apiVersion: v1
  kind: Service
  metadata: { name: web }
  spec:
    selector: { app: web }
    ports: [{ port: 80, targetPort: 80 }]

For each Service we find the Deployment whose pod-template labels satisfy the
Service selector, then build: an ECS task definition (from the Deployment's
container), an ECS service (awsvpc/Fargate) in the private subnets, a target
group, and a host-based rule on the shared ALB at <name>-<ns>.<cluster-domain>.
The address is written back to status.loadBalancer.ingress and published as an
alias A record so the Service is reachable.

Back-compat: if no Deployment matches but the Service carries a `k53.io/image`
annotation, we use that (the old inline shortcut still works).
"""

import store
import awsenv as A
from collections import namedtuple

from awsenv import ecs, log
import shared
import taskdefs

PLURAL = "services"
KIND = "Service"

# Resolved workload for a Service (from its Deployment, or inline annotation).
_Workload = namedtuple("_Workload", "image replicas container_port deployment_name command")


def _labels_match(selector: dict, labels: dict) -> bool:
    """True if every key/value in the Service selector is present in pod labels."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _find_workload(svc, deployments):
    """Resolve a _Workload (image, replicas, port, deployment, command) for a Service.

    Prefers a Deployment whose pod-template labels match the Service selector.
    Falls back to the Service's k53.io/image annotation for back-compat.
    """
    spec = svc.get("spec", {})
    selector = spec.get("selector", {}) or {}
    ns = svc.get("metadata", {}).get("namespace", "default")

    for dep in deployments:
        dmd = dep.get("metadata", {})
        if dmd.get("namespace", "default") != ns:
            continue
        template = dep.get("spec", {}).get("template", {})
        pod_labels = template.get("metadata", {}).get("labels", {}) or {}
        if not _labels_match(selector, pod_labels):
            continue
        containers = template.get("spec", {}).get("containers", [])
        if not containers:
            continue
        c0 = containers[0]
        image = c0.get("image")
        if not image:
            continue
        replicas = int(dep.get("spec", {}).get("replicas", 1))
        cports = c0.get("ports", [])
        container_port = int(cports[0].get("containerPort", 80)) if cports else _svc_target_port(svc)
        # k8s command -> ECS entrypoint, k8s args -> ECS command args. ECS has a
        # single `command` list, so concatenate (the standard translation).
        command = (c0.get("command") or []) + (c0.get("args") or []) or None
        return _Workload(image, replicas, container_port, dmd.get("name"), command)

    # Fallback: inline annotation shortcut.
    ann = svc.get("metadata", {}).get("annotations", {})
    if ann.get("k53.io/image"):
        return _Workload(
            ann["k53.io/image"],
            int(ann.get("k53.io/replicas", "1")),
            _svc_target_port(svc),
            None,
            None,
        )
    return None


def _svc_target_port(svc):
    ports = svc.get("spec", {}).get("ports", [])
    if not ports:
        return 80
    p = ports[0]
    return int(p.get("targetPort", p.get("port", 80)))


def _register_task_def(namespace, name, image, port, command=None):
    family = A.aws_name("svc", namespace, name)
    container = {
        "name": name,
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": port, "protocol": "tcp"}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/kube53/tasks/{namespace}/{name}",
                "awslogs-region": A.REGION,
                "awslogs-stream-prefix": "k53",
                "awslogs-create-group": "true",
            },
        },
    }
    if command:
        container["command"] = command
    # Idempotent: reuses the current ACTIVE revision unless the spec changed.
    arn, _created = taskdefs.ensure(family, container, KIND, namespace, name)
    return arn


def _ensure_ecs_service(namespace, name, task_def_arn, tg_arn, container_port, replicas, task_sg):
    svc_name = A.aws_name("svc", namespace, name)
    existing = ecs.describe_services(cluster=A.CLUSTER_NAME, services=[svc_name]).get("services", [])
    active = [s for s in existing if s["status"] == "ACTIVE"]

    network = {
        "awsvpcConfiguration": {
            "subnets": A.PRIVATE_SUBNETS,
            "securityGroups": [task_sg],
            "assignPublicIp": "DISABLED",
        }
    }
    load_balancers = [{
        "targetGroupArn": tg_arn,
        "containerName": name,
        "containerPort": container_port,
    }]

    if active:
        svc = active[0]
        # IDEMPOTENCY: only update if the task def revision or replica count
        # actually changed. Otherwise every tick would trigger a rolling
        # deployment for no reason.
        cur_td = svc.get("taskDefinition", "")
        cur_count = svc.get("desiredCount")
        # taskDefinition may be an ARN or family:rev; compare by suffix so an ARN
        # vs family:rev mismatch doesn't cause a false diff.
        same_td = cur_td == task_def_arn or cur_td.endswith(task_def_arn.split("/")[-1])
        if same_td and cur_count == replicas:
            log(f"ECS service {svc_name} already converged (td unchanged, count={replicas})")
            return
        ecs.update_service(
            cluster=A.CLUSTER_NAME, service=svc_name,
            taskDefinition=task_def_arn, desiredCount=replicas,
        )
        log(f"updated ECS service {svc_name} (td or count changed)")
    else:
        ecs.create_service(
            cluster=A.CLUSTER_NAME, serviceName=svc_name,
            taskDefinition=task_def_arn, desiredCount=replicas,
            launchType="FARGATE",
            networkConfiguration=network,
            loadBalancers=load_balancers,
            tags=[{"key": A.MANAGED_TAG_KEY, "value": A.MANAGED_TAG_VAL},
                  {"key": A.OBJECT_TAG_KEY, "value": A.object_key(KIND, namespace, name)}],
        )
        log(f"created ECS service {svc_name}")


def reconcile():
    """Reconcile Services into ECS services + target groups only.

    A Service on its own is INTERNAL — no ALB, no listener rule, no DNS record.
    External exposure is the Ingress reconciler's job (it creates the ALB and
    forwards to the target group we register here). This mirrors real Kubernetes:
    a plain Service isn't reachable from outside the cluster; an Ingress exposes it.
    """
    svcs = store.list_kind(PLURAL)
    if not svcs:
        log("no services")
        return {"phase": "ReconcileServices", "count": 0}

    deployments = store.list_kind("deployments")

    # Only the task SG is needed to run tasks — NOT the ALB.
    task_sg = shared.ensure_task_sg()

    reconciled = 0
    for svc in svcs:
        md = svc.get("metadata", {})
        name = md["name"]
        namespace = md.get("namespace", "default")

        workload = _find_workload(svc, deployments)
        if not workload:
            log(f"service {namespace}/{name}: no matching Deployment (or k53.io/image); skipping")
            continue
        w = workload
        source = f"deployment/{w.deployment_name}" if w.deployment_name else "annotation"
        log(f"service {namespace}/{name}: image={w.image} replicas={w.replicas} "
            f"port={w.container_port} via {source}")

        tg_name = A.aws_name("svc", namespace, name)
        tg_arn = shared.ensure_target_group(tg_name, w.container_port, A.object_key(KIND, namespace, name))
        task_def = _register_task_def(namespace, name, w.image, w.container_port, w.command)
        _ensure_ecs_service(namespace, name, task_def, tg_arn, w.container_port, w.replicas, task_sg)

        # ClusterIP-style status: reachable in-cluster via its target group. No
        # external hostname unless an Ingress exposes it.
        desired_status = {"loadBalancer": {}}
        if svc.get("status") != desired_status:
            svc["status"] = desired_status
            store.put(svc, PLURAL)
        reconciled += 1

    return {"phase": "ReconcileServices", "count": reconciled}
