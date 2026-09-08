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
import shared


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
    removed = {"services": 0, "schedules": 0, "target_groups": 0, "rules": 0}

    # --- ECS services ---
    try:
        arns = ecs.list_services(cluster=A.CLUSTER_NAME).get("serviceArns", [])
        if arns:
            # include=["TAGS"] is REQUIRED — describe_services omits tags by
            # default, which meant every service looked untagged and GC skipped
            # it, so `kubectl delete` never removed the ECS service.
            described = ecs.describe_services(
                cluster=A.CLUSTER_NAME, services=arns, include=["TAGS"]
            ).get("services", [])
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

    # --- orphan listener rules ---
    # Must run BEFORE target-group GC: a rule forwarding to an orphaned target
    # group blocks the target group's deletion. We delete any managed rule whose
    # host no longer corresponds to a desired Service or Ingress.
    removed["rules"] = _gc_listener_rules()

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

    # --- ALB lifecycle is tied to Ingress (Model B) ---
    # The ALB exists only while >=1 Ingress does. Also reap DNS alias records
    # that point at the ALB but have no backing Ingress host (this cleans up
    # stale Service `web-default.*` aliases and removed Ingress hosts).
    removed["dns"] = _gc_dns_and_alb()

    # --- cluster teardown (level-based) ---
    # Delete the ECS cluster once it's drained AND either the marker is gone
    # (tofu destroy) or there are no workloads left (scale-to-zero). The cluster
    # is rebuilt by EnsureCluster when a workload reappears (marker still present).
    cluster = _teardown_cluster_if_idle()
    removed["cluster"] = cluster

    return {"phase": "GarbageCollect", **removed}


def _desired_ingress_hosts():
    """Ingress hosts that should have a DNS record + ALB routing."""
    hosts = set()
    for ing in store.list_kind("ingresses"):
        for rule in ing.get("spec", {}).get("rules", []):
            if rule.get("host"):
                hosts.add(rule["host"].rstrip("."))
    return hosts


def _gc_dns_and_alb():
    """Reap ALB-targeted DNS records with no backing Ingress; tear down the ALB
    when no Ingress remains. Returns count of DNS records removed."""
    alb = shared.find_alb()
    removed = 0

    # If there's an ALB, delete alias records pointing at it whose host isn't a
    # desired Ingress host. If there's no ALB, any alias that still points at a
    # (now-gone) ALB DNS is stale — but we can only match by our current ALB, so
    # when the ALB is gone we skip record matching and rely on having deleted
    # them on the tick the ALB went away.
    desired_hosts = _desired_ingress_hosts()
    if alb:
        alb_dns = alb["DNSName"].rstrip(".")
        for rec in store.list_alias_a_records():
            # Only touch records that alias to OUR ALB (never api.* -> API GW).
            if rec["alias_dns"] != alb_dns:
                continue
            host = rec["name"].rstrip(".")
            if host not in desired_hosts:
                store.delete_alias_record(rec["raw"])
                removed += 1
                log(f"GC DNS alias {host} (no backing Ingress)")

    # Tear down the ALB itself when there are no Ingresses at all.
    if not store.list_kind("ingresses"):
        if alb:
            # Delete any remaining alias records to the ALB first, so nothing
            # dangles once the ALB DNS name disappears.
            alb_dns = alb["DNSName"].rstrip(".")
            for rec in store.list_alias_a_records():
                if rec["alias_dns"] == alb_dns:
                    store.delete_alias_record(rec["raw"])
                    removed += 1
                    log(f"GC DNS alias {rec['name'].rstrip('.')} (ALB teardown)")
        shared.teardown_alb()
        shared.teardown_alb_sg()

    return removed


def _has_workloads():
    """True if any workload object exists in the datastore (desired state).

    Deciding on DESIRED state (Route53), not live ECS state, avoids racing a
    freshly-applied Service whose ECS service hasn't converged yet.
    """
    return bool(store.list_kind("services") or store.list_kind("cronjobs"))


def _teardown_cluster_if_idle():
    """Drain and delete the ECS cluster when it's no longer needed.

    The cluster is torn down when EITHER:
      - the `_cluster` marker is absent (explicit teardown, e.g. tofu destroy), OR
      - there are no workload objects (no Services, no CronJobs) — scale-to-zero.
    In the scale-to-zero case the marker stays, so EnsureCluster rebuilds the
    cluster on the next tick once a workload is applied again.

    Returns: "kept" | "draining" | "deleted" | "absent".
    """
    marker_present = store.exists_marker(store.cluster_marker_name())
    if marker_present and _has_workloads():
        return "kept"

    # The ALB (+ its SG) is torn down by _gc_dns_and_alb when no Ingress remains.
    # Here we only need to drop the task SG and delete the drained cluster.
    shared.teardown_task_sg()

    try:
        clusters = ecs.describe_clusters(clusters=[A.CLUSTER_NAME]).get("clusters", [])
    except ecs.exceptions.ClusterNotFoundException:
        return "absent"
    active = [c for c in clusters if c.get("status") == "ACTIVE"]
    if not active:
        return "absent"

    c = active[0]
    # ECS refuses DeleteCluster while services or tasks remain. The per-object
    # GC scales/deletes services, but draining is async, so we may need a few
    # ticks. Don't force it here — just wait until it's empty.
    if c.get("activeServicesCount", 0) > 0 or c.get("runningTasksCount", 0) > 0 or c.get("pendingTasksCount", 0) > 0:
        log(f"cluster {A.CLUSTER_NAME} still draining "
            f"(services={c.get('activeServicesCount')}, "
            f"tasks={c.get('runningTasksCount')}/{c.get('pendingTasksCount')}); deferring delete")
        return "draining"

    try:
        ecs.delete_cluster(cluster=A.CLUSTER_NAME)
        log(f"deleted ECS cluster {A.CLUSTER_NAME}")
        return "deleted"
    except (ecs.exceptions.ClusterContainsServicesException,
            ecs.exceptions.ClusterContainsTasksException) as e:
        log(f"cluster {A.CLUSTER_NAME} not empty yet ({type(e).__name__}); deferring")
        return "draining"


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


def _rule_host_values(rule):
    vals = []
    for cond in rule.get("Conditions", []):
        if cond.get("Field") == "host-header":
            vals.extend(cond.get("HostHeaderConfig", {}).get("Values") or cond.get("Values", []))
    return vals


def _gc_listener_rules():
    """Delete managed listener rules whose host has no backing Ingress.

    Only Ingress creates listener rules now, so rules are matched against Ingress
    hosts. This reaps orphaned rules from deleted Ingresses, leftover Service
    rules from the old design, AND the pile of duplicate rules from the earlier
    dedup bug (keeping one rule per still-desired host).
    """
    listener = shared.find_https_listener()
    if not listener:
        return 0

    desired_hosts = _desired_ingress_hosts()
    removed = 0
    seen_desired_hosts = set()

    # describe_rules doesn't return tags; fetch them per-rule in a batch.
    rules = elb.describe_rules(ListenerArn=listener).get("Rules", [])
    arns = [r["RuleArn"] for r in rules if not r.get("IsDefault")]
    tags_by_arn = {}
    for i in range(0, len(arns), 20):
        chunk = arns[i : i + 20]
        for td in elb.describe_tags(ResourceArns=chunk).get("TagDescriptions", []):
            tags_by_arn[td["ResourceArn"]] = {t["Key"]: t["Value"] for t in td.get("Tags", [])}

    for r in rules:
        if r.get("IsDefault"):
            continue
        arn = r["RuleArn"]
        tags = tags_by_arn.get(arn, {})
        if tags.get(A.MANAGED_TAG_KEY) != A.MANAGED_TAG_VAL:
            continue  # never touch rules we didn't create
        hosts = _rule_host_values(r)
        keep = False
        for h in hosts:
            if h in desired_hosts and h not in seen_desired_hosts:
                # keep exactly one rule per desired host; dedupe the rest
                seen_desired_hosts.add(h)
                keep = True
        if keep:
            continue
        try:
            elb.delete_rule(RuleArn=arn)
            removed += 1
            log(f"GC listener rule {arn.rsplit('/', 1)[-1]} (hosts={hosts or 'none'})")
        except Exception as e:
            log(f"listener rule delete deferred: {type(e).__name__}")
    return removed
