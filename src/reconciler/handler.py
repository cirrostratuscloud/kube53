"""kube53 reconciler: the controller manager.

Invoked once per phase by the reconcile Step Function on every tick. Each phase
reads desired state from Route53 (via store) and converges real AWS resources.
Everything is level-based and idempotent: we compute desired-vs-actual each run.

Phases:
  EnsureCluster      - create the ECS cluster if the _cluster marker exists
  ReconcileServices  - ECS service + ALB target group + listener rule per Service
  ReconcileIngresses - ALB listener rules per Ingress host/path
  ReconcileCronJobs  - EventBridge Scheduler schedule -> ECS RunTask per CronJob
  GarbageCollect     - tear down managed AWS resources with no backing object

The shared ALB is created lazily the first time a Service/Ingress needs it.
"""

import store
import awsenv as A
from awsenv import ecs, elb, ec2, scheduler, log

import services
import ingresses
import cronjobs
import garbage as gcphase


def handler(event, context):
    phase = event.get("phase", "ReconcileServices")
    log(f"phase={phase}")
    try:
        if phase == "EnsureCluster":
            return ensure_cluster()
        if phase == "ReconcileServices":
            return services.reconcile()
        if phase == "ReconcileIngresses":
            return ingresses.reconcile()
        if phase == "ReconcileCronJobs":
            return cronjobs.reconcile()
        if phase == "GarbageCollect":
            return gcphase.collect()
        return {"phase": phase, "status": "noop", "reason": "unknown phase"}
    except Exception as e:  # never let one phase wedge the loop
        log(f"phase={phase} ERROR {type(e).__name__}: {e}")
        raise


def ensure_cluster():
    """Create the ECS cluster (Fargate) if the cluster marker exists.

    The marker (`_cluster.k53.<domain>` TXT) is created declaratively by OpenTofu
    (aws_route53_record.cluster_marker). No marker -> we don't stand anything up.

    We also require at least one workload (Service or CronJob) before creating
    the cluster. Otherwise, when idle, this phase would recreate the cluster
    every tick just for GarbageCollect to tear it down again (thrash). The
    cluster comes up on the first tick after a workload is applied.
    """
    if not store.exists_marker(store.cluster_marker_name()):
        log("no _cluster marker; skipping cluster creation")
        return {"phase": "EnsureCluster", "cluster": None}

    has_workloads = bool(store.list_kind("services") or store.list_kind("cronjobs"))
    if not has_workloads:
        log("no workloads (Service/CronJob); skipping cluster creation")
        return {"phase": "EnsureCluster", "cluster": None}

    existing = ecs.describe_clusters(clusters=[A.CLUSTER_NAME]).get("clusters", [])
    active = [c for c in existing if c["status"] == "ACTIVE"]
    if not active:
        log(f"creating ECS cluster {A.CLUSTER_NAME}")
        ecs.create_cluster(
            clusterName=A.CLUSTER_NAME,
            capacityProviders=["FARGATE", "FARGATE_SPOT"],
            defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
            tags=[{"key": A.MANAGED_TAG_KEY, "value": A.MANAGED_TAG_VAL}],
        )
    else:
        log(f"ECS cluster {A.CLUSTER_NAME} already ACTIVE")
    return {"phase": "EnsureCluster", "cluster": A.CLUSTER_NAME}
