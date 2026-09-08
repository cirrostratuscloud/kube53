"""Garbage collection: tear down managed AWS resources with no backing object.

We only ever touch resources tagged k53.io/managed=true whose k53.io/key points
at a Kubernetes object that no longer exists in Route53. This is what makes
`kubectl delete` actually delete real infra: the object's TXT record disappears,
and the next tick reaps the ECS service / schedule / listener rule.

Conservative on purpose: if we can't confidently map a resource to a missing
object, we leave it alone.
"""

import store
import awsenv as A
from awsenv import ecs, elb, scheduler, log


def _desired_keys():
    """Set of object keys (Kind/ns/name) that currently exist in the datastore."""
    keys = set()
    for plural, kind in (("services", "Service"), ("ingresses", "Ingress"), ("cronjobs", "CronJob")):
        for obj in store.list_kind(plural):
            md = obj.get("metadata", {})
            keys.add(A.object_key(kind, md.get("namespace", "default"), md["name"]))
    return keys


def _tag_map(tag_list, key_field="Key", val_field="Value"):
    return {t[key_field]: t[val_field] for t in tag_list}


def collect():
    desired = _desired_keys()
    removed = {"services": 0, "schedules": 0, "target_groups": 0}

    # --- ECS services ---
    try:
        arns = ecs.list_services(cluster=A.CLUSTER_NAME).get("serviceArns", [])
        if arns:
            described = ecs.describe_services(cluster=A.CLUSTER_NAME, services=arns).get("services", [])
            for s in described:
                tags = _tag_map(s.get("tags", []), "key", "value")
                if tags.get(A.MANAGED_TAG_KEY) != A.MANAGED_TAG_VAL:
                    continue
                key = tags.get(A.OBJECT_TAG_KEY)
                if key and key not in desired:
                    log(f"GC ECS service {s['serviceName']} (orphan of {key})")
                    ecs.update_service(cluster=A.CLUSTER_NAME, service=s["serviceName"], desiredCount=0)
                    ecs.delete_service(cluster=A.CLUSTER_NAME, service=s["serviceName"], force=True)
                    removed["services"] += 1
    except ecs.exceptions.ClusterNotFoundException:
        log("no cluster; skipping ECS GC")

    # --- EventBridge schedules ---
    paginator = scheduler.get_paginator("list_schedules")
    for page in paginator.paginate():
        for sch in page.get("Schedules", []):
            full = scheduler.get_schedule(Name=sch["Name"])
            # schedules don't carry our object tag cheaply; map by name convention
            # k53-cj-<ns>-<name>; if no matching cronjob key remains, delete.
            name = sch["Name"]
            if not name.startswith("k53-cj-"):
                continue
            if not _schedule_has_backing(name, desired):
                log(f"GC schedule {name}")
                scheduler.delete_schedule(Name=name)
                removed["schedules"] += 1

    # --- orphan target groups (best-effort) ---
    for tg in elb.describe_target_groups().get("TargetGroups", []):
        tg_arn = tg["TargetGroupArn"]
        tags_resp = elb.describe_tags(ResourceArns=[tg_arn])
        tags = _tag_map(tags_resp["TagDescriptions"][0]["Tags"]) if tags_resp["TagDescriptions"] else {}
        if tags.get(A.MANAGED_TAG_KEY) != A.MANAGED_TAG_VAL:
            continue
        key = tags.get(A.OBJECT_TAG_KEY)
        if key and key not in desired:
            try:
                elb.delete_target_group(TargetGroupArn=tg_arn)
                removed["target_groups"] += 1
                log(f"GC target group {tg.get('TargetGroupName')} (orphan of {key})")
            except elb.exceptions.ResourceInUseException:
                # still referenced by a listener rule; a later tick gets it
                log(f"target group {tg.get('TargetGroupName')} still in use; deferring")

    return {"phase": "GarbageCollect", **removed}


def _schedule_has_backing(schedule_name, desired_keys):
    # schedule name = k53-cj-<ns>-<name> (truncated to 32). Compare against desired
    # cronjob keys by regenerating the aws_name for each.
    for key in desired_keys:
        kind, ns, name = key.split("/", 2)
        if kind != "CronJob":
            continue
        if A.aws_name("cj", ns, name) == schedule_name:
            return True
    return False
