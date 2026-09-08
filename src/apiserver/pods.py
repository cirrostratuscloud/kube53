"""Synthesize read-only Pod objects from live ECS tasks.

kube53 has no real Pods — workloads run as ECS/Fargate tasks. But `kubectl get
pods` is the first thing anyone types, so we make it truthful: each running ECS
task in the cluster becomes a Pod, with phase/readiness derived from the task's
real ECS status. This is READ-ONLY; Pods are created/destroyed by the reconciler
via ECS, never directly.

Pod naming: <service>-<task-suffix>, where <service> is the kube53 object name
recovered from the ECS service's k53.io/key tag, mirroring how Kubernetes names
Pods <deploy>-<replicaset>-<pod>.
"""

import os

import boto3

REGION = os.environ.get("REGION") or os.environ.get("AWS_REGION")
CLUSTER_NAME = os.environ["CLUSTER_NAME"]

MANAGED_TAG_KEY = "k53.io/managed"
OBJECT_TAG_KEY = "k53.io/key"

ecs = boto3.client("ecs", region_name=REGION)

# Map ECS task lastStatus -> Kubernetes Pod phase.
_PHASE = {
    "PROVISIONING": "Pending",
    "PENDING": "Pending",
    "ACTIVATING": "Pending",
    "RUNNING": "Running",
    "DEACTIVATING": "Running",
    "STOPPING": "Running",
    "DEPROVISIONING": "Running",
    "STOPPED": "Failed",
}


def _svc_key_by_arn():
    """Return {serviceArn: (namespace, name)} for kube53-managed ECS services."""
    out = {}
    arns = []
    paginator = ecs.get_paginator("list_services")
    for page in paginator.paginate(cluster=CLUSTER_NAME):
        arns.extend(page.get("serviceArns", []))
    # describe in chunks of 10 (ECS limit) with tags
    for i in range(0, len(arns), 10):
        chunk = arns[i : i + 10]
        if not chunk:
            continue
        desc = ecs.describe_services(cluster=CLUSTER_NAME, services=chunk, include=["TAGS"])
        for s in desc.get("services", []):
            tags = {t["key"]: t["value"] for t in s.get("tags", [])}
            if tags.get(MANAGED_TAG_KEY) != "true":
                continue
            key = tags.get(OBJECT_TAG_KEY, "")  # e.g. "Service/default/web"
            parts = key.split("/")
            if len(parts) == 3 and parts[0] == "Service":
                out[s["serviceArn"]] = (parts[1], parts[2])
                out[s["serviceName"]] = (parts[1], parts[2])
    return out


def _task_suffix(task_arn):
    # arn:aws:ecs:...:task/<cluster>/<id> -> last 10 chars of id
    tid = task_arn.rsplit("/", 1)[-1]
    return tid[:10]


def _pod_from_task(task, namespace, svc_name):
    task_arn = task["taskArn"]
    name = f"{svc_name}-{_task_suffix(task_arn)}"
    last = task.get("lastStatus", "PENDING")
    phase = _PHASE.get(last, "Unknown")
    health = task.get("healthStatus", "UNKNOWN")
    ready = last == "RUNNING" and health in ("HEALTHY", "UNKNOWN")

    containers = []
    ready_count = 0
    for c in task.get("containers", []):
        c_running = c.get("lastStatus") == "RUNNING"
        if c_running:
            ready_count += 1
        state = {"running" if c_running else "waiting": {}}
        containers.append({
            "name": c.get("name", "app"),
            "image": c.get("image", ""),
            "ready": c_running,
            "restartCount": 0,
            "state": state,
        })
    total = len(containers) or 1

    started = task.get("startedAt") or task.get("createdAt")
    started_iso = started.strftime("%Y-%m-%dT%H:%M:%SZ") if started else None

    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"k53.io/service": svc_name},
            "annotations": {
                "k53.io/ecs-task-arn": task_arn,
                "k53.io/launch-type": task.get("launchType", ""),
            },
            **({"creationTimestamp": started_iso} if started_iso else {}),
        },
        "spec": {
            "containers": [{"name": c["name"], "image": c["image"]} for c in containers],
            "nodeName": f"fargate/{task.get('availabilityZone', '')}",
        },
        "status": {
            "phase": phase,
            "podIP": _first_ip(task),
            "hostIP": _first_ip(task),
            "containerStatuses": containers,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
        # extra field kubectl ignores but humans like in -o json
        "k53_ready": f"{ready_count}/{total}",
    }
    return pod


def _first_ip(task):
    for a in task.get("attachments", []):
        for d in a.get("details", []):
            if d.get("name") == "privateIPv4Address":
                return d.get("value")
    return None


def list_pods(namespace=None):
    key_map = _svc_key_by_arn()
    if not key_map:
        return []
    pods = []
    task_arns = []
    paginator = ecs.get_paginator("list_tasks")
    for page in paginator.paginate(cluster=CLUSTER_NAME):
        task_arns.extend(page.get("taskArns", []))
    for i in range(0, len(task_arns), 100):
        chunk = task_arns[i : i + 100]
        if not chunk:
            continue
        desc = ecs.describe_tasks(cluster=CLUSTER_NAME, tasks=chunk)
        for t in desc.get("tasks", []):
            grp = t.get("group", "")  # "service:<svcName>"
            svc_name = grp.split(":", 1)[1] if grp.startswith("service:") else None
            mapped = key_map.get(svc_name) or key_map.get(t.get("startedBy", ""))
            if not mapped:
                continue
            ns, kname = mapped
            if namespace and ns != namespace:
                continue
            pods.append(_pod_from_task(t, ns, kname))
    return pods


def get_pod(namespace, name):
    for p in list_pods(namespace):
        if p["metadata"]["name"] == name:
            return p
    return None
