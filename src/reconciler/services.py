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
from awsenv import ecs, elb, log
import shared
import taskdefs

PLURAL = "services"
KIND = "Service"


def _host_for(namespace, name):
    return f"{name}-{namespace}.{A.CLUSTER_DOMAIN}"


def _labels_match(selector: dict, labels: dict) -> bool:
    """True if every key/value in the Service selector is present in pod labels."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _find_workload(svc, deployments):
    """Resolve (image, replicas, container_port, deployment_name) for a Service.

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
        return image, replicas, container_port, dmd.get("name")

    # Fallback: inline annotation shortcut.
    ann = svc.get("metadata", {}).get("annotations", {})
    if ann.get("k53.io/image"):
        return (
            ann["k53.io/image"],
            int(ann.get("k53.io/replicas", "1")),
            _svc_target_port(svc),
            None,
        )
    return None


def _svc_target_port(svc):
    ports = svc.get("spec", {}).get("ports", [])
    if not ports:
        return 80
    p = ports[0]
    return int(p.get("targetPort", p.get("port", 80)))


def _register_task_def(namespace, name, image, port):
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
    # Idempotent: reuses the current ACTIVE revision unless the spec changed.
    arn, _created = taskdefs.ensure(family, container, KIND, namespace, name)
    return arn


def _ensure_listener_rule(listener_arn, host, tg_arn, priority_seed):
    """Host-header rule forwarding to the target group. Idempotent by host match."""
    rules = elb.describe_rules(ListenerArn=listener_arn).get("Rules", [])
    for r in rules:
        for cond in r.get("Conditions", []):
            if cond.get("Field") == "host-header" and host in cond.get("Values", []):
                return r["RuleArn"]  # already routed
    used = {int(r["Priority"]) for r in rules if r["Priority"].isdigit()}
    priority = priority_seed
    while priority in used:
        priority += 1
    elb.create_rule(
        ListenerArn=listener_arn, Priority=priority,
        Conditions=[{"Field": "host-header", "HostHeaderConfig": {"Values": [host]}}],
        Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        Tags=[{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL}],
    )
    return None


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
    svcs = store.list_kind(PLURAL)
    if not svcs:
        log("no services")
        return {"phase": "ReconcileServices", "count": 0}

    deployments = store.list_kind("deployments")

    infra = shared.ensure_alb()
    listener = infra["https_listener"]
    task_sg = infra["task_sg"]

    reconciled = 0
    for idx, svc in enumerate(svcs):
        md = svc.get("metadata", {})
        name = md["name"]
        namespace = md.get("namespace", "default")

        workload = _find_workload(svc, deployments)
        if not workload:
            log(f"service {namespace}/{name}: no matching Deployment (or k53.io/image); skipping")
            continue
        image, replicas, container_port, dep_name = workload
        source = f"deployment/{dep_name}" if dep_name else "annotation"
        log(f"service {namespace}/{name}: image={image} replicas={replicas} port={container_port} via {source}")

        tg_name = A.aws_name("svc", namespace, name)
        tg_arn = shared.ensure_target_group(tg_name, container_port, A.object_key(KIND, namespace, name))
        task_def = _register_task_def(namespace, name, image, container_port)
        _ensure_ecs_service(namespace, name, task_def, tg_arn, container_port, replicas, task_sg)

        host = _host_for(namespace, name)
        _ensure_listener_rule(listener, host, tg_arn, 100 + idx)

        store.upsert_alias(host + ".", infra["dns_name"], infra["zone_id"])
        # Only write the object back if the status actually changed, otherwise we
        # rewrite the Route53 TXT record (and bump resourceVersion) every tick.
        desired_status = {"loadBalancer": {"ingress": [{"hostname": host}]}}
        if svc.get("status") != desired_status:
            svc["status"] = desired_status
            store.put(svc, PLURAL)
        reconciled += 1

    return {"phase": "ReconcileServices", "count": reconciled}
