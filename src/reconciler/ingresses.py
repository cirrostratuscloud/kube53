"""Reconcile Ingress objects into ALB host/path listener rules.

An Ingress routes host+path to a backend Service's target group (the Service
reconciler already created the target group). We add path-based rules on the
shared HTTPS listener and publish an alias record for each host.

  spec:
    rules:
      - host: site.kube53.example.com
        http:
          paths:
            - path: /
              pathType: Prefix
              backend:
                service:
                  name: web
                  port:
                    number: 80
"""

import store
import awsenv as A
from awsenv import elb, log
import shared

PLURAL = "ingresses"
KIND = "Ingress"


def _backend_tg_arn(namespace, service_name):
    tg_name = A.aws_name("svc", namespace, service_name)
    tg = shared.find_target_group(tg_name)
    return tg["TargetGroupArn"] if tg else None


def _ensure_rule(listener_arn, host, path, tg_arn, priority_seed):
    rules = elb.describe_rules(ListenerArn=listener_arn).get("Rules", [])
    for r in rules:
        conds = r.get("Conditions", [])
        hosts = [v for c in conds if c.get("Field") == "host-header" for v in c.get("Values", [])]
        paths = [v for c in conds if c.get("Field") == "path-pattern" for v in c.get("Values", [])]
        if host in hosts and (not path or path in paths or (path + "*") in paths):
            return  # already routed
    used = {int(r["Priority"]) for r in rules if r["Priority"].isdigit()}
    priority = priority_seed
    while priority in used:
        priority += 1
    conditions = [{"Field": "host-header", "HostHeaderConfig": {"Values": [host]}}]
    if path and path != "/":
        pat = path if path.endswith("*") else path.rstrip("/") + "/*"
        conditions.append({"Field": "path-pattern", "PathPatternConfig": {"Values": [pat]}})
    elb.create_rule(
        ListenerArn=listener_arn, Priority=priority,
        Conditions=conditions,
        Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        Tags=[{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL},
              {"Key": A.OBJECT_TAG_KEY, "Value": host}],
    )
    log(f"ingress rule host={host} path={path}")


def reconcile():
    ings = store.list_kind(PLURAL)
    if not ings:
        log("no ingresses")
        return {"phase": "ReconcileIngresses", "count": 0}

    infra = shared.ensure_alb()
    listener = infra["https_listener"]
    reconciled = 0
    seed = 200

    for ing in ings:
        md = ing.get("metadata", {})
        namespace = md.get("namespace", "default")
        for rule in ing.get("spec", {}).get("rules", []):
            host = rule.get("host")
            if not host:
                continue
            for p in rule.get("http", {}).get("paths", []):
                backend = p.get("backend", {}).get("service", {})
                svc_name = backend.get("name")
                if not svc_name:
                    continue
                tg_arn = _backend_tg_arn(namespace, svc_name)
                if not tg_arn:
                    log(f"ingress {namespace}/{md.get('name')} backend {svc_name} has no TG yet; will retry next tick")
                    continue
                _ensure_rule(listener, host, p.get("path", "/"), tg_arn, seed)
                seed += 1
            store.upsert_alias(host + ".", infra["dns_name"], infra["zone_id"])
        ing.setdefault("status", {})["loadBalancer"] = {"ingress": [{"hostname": infra["dns_name"]}]}
        store.put(ing, PLURAL)
        reconciled += 1

    return {"phase": "ReconcileIngresses", "count": reconciled}
