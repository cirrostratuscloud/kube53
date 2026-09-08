"""kube53 apiserver: enough of the Kubernetes REST API that real kubectl works.

kubectl's flow for `apply -f svc.yaml`:
  1. discovery: GET /api, /apis, /apis/<group>/<version>, /api/v1  (RESTMapper)
  2. GET the object (to decide create vs patch)
  3. POST (create) or PATCH (apply) the object
`get`/`delete`/`create` follow the same discovery + verb pattern. We implement a
curated resource catalog and route verbs to the Route53-backed store.
"""

import json
import os
import re

import store
import pods as podview

API_TOKEN = os.environ["API_TOKEN"]

# The kinds kube53 pretends to support, keyed by (group, version, plural).
# group "" == core (/api/v1); others live under /apis/<group>/<version>.
RESOURCES = {
    ("", "v1", "namespaces"): {"kind": "Namespace", "namespaced": False, "singular": "namespace", "short": ["ns"]},
    ("", "v1", "pods"): {"kind": "Pod", "namespaced": True, "singular": "pod", "short": ["po"], "synthetic": True},
    ("", "v1", "services"): {"kind": "Service", "namespaced": True, "singular": "service", "short": ["svc"]},
    ("apps", "v1", "deployments"): {"kind": "Deployment", "namespaced": True, "singular": "deployment", "short": ["deploy"]},
    ("networking.k8s.io", "v1", "ingresses"): {"kind": "Ingress", "namespaced": True, "singular": "ingress", "short": ["ing"]},
    ("batch", "v1", "cronjobs"): {"kind": "CronJob", "namespaced": True, "singular": "cronjob", "short": ["cj"]},
}

GROUPS = {
    "apps": ["v1"],
    "networking.k8s.io": ["v1"],
    "batch": ["v1"],
}


def _resp(status, body, content_type="application/json"):
    if not isinstance(body, str):
        body = json.dumps(body)
    return {
        "statusCode": status,
        "headers": {"content-type": content_type},
        "body": body,
    }


def _status(code, message, reason, name=None):
    return _resp(code, {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "message": message,
        "reason": reason,
        "code": code,
        **({"details": {"name": name}} if name else {}),
    })


# --------------------------------------------------------------------------
# Discovery — kubectl builds its RESTMapper from these before any verb.
# --------------------------------------------------------------------------

def _api_versions():
    return {"kind": "APIVersions", "versions": ["v1"], "serverAddressByClientCIDRs": [
        {"clientCIDR": "0.0.0.0/0", "serverAddress": "api." + store.CLUSTER_DOMAIN}
    ]}


def _api_group_list():
    groups = []
    for g, versions in GROUPS.items():
        groups.append({
            "name": g,
            "versions": [{"groupVersion": f"{g}/{v}", "version": v} for v in versions],
            "preferredVersion": {"groupVersion": f"{g}/{versions[0]}", "version": versions[0]},
        })
    return {"kind": "APIGroupList", "apiVersion": "v1", "groups": groups}


def _resource_list(group, version):
    gv = version if group == "" else f"{group}/{version}"
    resources = []
    for (g, v, plural), meta in RESOURCES.items():
        if g == group and v == version:
            resources.append({
                "name": plural,
                "singularName": meta["singular"],
                "namespaced": meta["namespaced"],
                "kind": meta["kind"],
                "shortNames": meta.get("short", []),
                "verbs": ["create", "delete", "get", "list", "patch", "update"],
            })
    return {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": gv, "resources": resources}


def _find_resource(group, version, plural):
    return RESOURCES.get((group, version, plural))


# --------------------------------------------------------------------------
# Path parsing
# --------------------------------------------------------------------------

def parse_path(path):
    """Return a dict describing the request target.

    Handles:
      /api                                  -> {"disc": "apiversions"}
      /apis                                 -> {"disc": "apigroups"}
      /api/v1                               -> {"disc": "corelist"}
      /apis/<group>/<version>               -> {"disc": "grouplist", ...}
      /api/v1/namespaces/<ns>/<plural>[/<name>]
      /api/v1/<plural>                       (cluster-scoped or all-ns list)
      /apis/<group>/<version>/namespaces/<ns>/<plural>[/<name>]
    """
    p = path.strip("/")
    parts = p.split("/") if p else []

    if parts == ["api"]:
        return {"disc": "apiversions"}
    if parts == ["apis"]:
        return {"disc": "apigroups"}
    if parts == ["version"]:
        return {"disc": "version"}
    if parts and parts[0] == "openapi":
        # /openapi/v2                     -> swagger 2.0 JSON doc
        # /openapi/v3                     -> v3 discovery (JSON list of group paths)
        # /openapi/v3/apis/<g>/<v>        -> per-group schema (kubectl wants protobuf)
        if parts[:2] == ["openapi", "v3"] and len(parts) > 2:
            return {"disc": "openapi_v3_group"}
        if parts[:2] == ["openapi", "v3"]:
            return {"disc": "openapi_v3_root"}
        return {"disc": "openapi_v2"}

    if parts[:1] == ["api"] and len(parts) >= 2:
        group, version, rest = "", parts[1], parts[2:]
    elif parts[:1] == ["apis"] and len(parts) >= 3:
        group, version, rest = parts[1], parts[2], parts[3:]
    else:
        return {"disc": "unknown"}

    if not rest:
        # /api/v1 or /apis/<group>/<version>
        return {"disc": "corelist" if group == "" else "grouplist", "group": group, "version": version}

    # namespaced?
    namespace = None
    if rest[0] == "namespaces" and len(rest) >= 2:
        # Could be a namespace object (/api/v1/namespaces/<name>) or scoped list.
        if len(rest) == 2:
            # /api/v1/namespaces/<name> -> operate on the Namespace object
            return {"group": group, "version": version, "plural": "namespaces", "name": rest[1], "namespace": "default"}
        namespace = rest[1]
        rest = rest[2:]

    plural = rest[0]
    name = rest[1] if len(rest) >= 2 else None
    return {"group": group, "version": version, "plural": plural, "name": name, "namespace": namespace}


# --------------------------------------------------------------------------
# Verb handlers
# --------------------------------------------------------------------------

def _list(meta, group, version, plural, namespace):
    items = store.list_kind(plural, namespace)
    gv = version if group == "" else f"{group}/{version}"
    return _resp(200, {
        "kind": meta["kind"] + "List",
        "apiVersion": gv,
        "metadata": {"resourceVersion": "0"},
        "items": items,
    })


def _get(meta, group, version, plural, namespace, name):
    ns = namespace or "default"
    try:
        obj = store.get(plural, ns, name)
    except store.NotFound:
        return _status(404, f"{meta['kind'].lower()} \"{name}\" not found", "NotFound", name)
    return _resp(200, obj)


def _create(meta, group, version, plural, namespace, body):
    obj = json.loads(body) if body else {}
    gv = version if group == "" else f"{group}/{version}"
    obj.setdefault("apiVersion", gv)
    obj.setdefault("kind", meta["kind"])
    md = obj.setdefault("metadata", {})
    if meta["namespaced"]:
        md["namespace"] = namespace or md.get("namespace", "default")
    else:
        md["namespace"] = "default"  # store all in a synthetic ns
    if "name" not in md:
        return _status(422, "metadata.name is required", "Invalid")
    try:
        # kubectl create should conflict on existing.
        store.get(plural, md["namespace"], md["name"])
        return _status(409, f"{meta['kind'].lower()} \"{md['name']}\" already exists", "AlreadyExists", md["name"])
    except store.NotFound:
        pass
    try:
        saved = store.put(obj, plural)
    except store.TooLarge as e:
        return _status(413, str(e), "RequestEntityTooLarge")
    return _resp(201, saved)


def _replace(meta, group, version, plural, namespace, name, body):
    obj = json.loads(body) if body else {}
    md = obj.setdefault("metadata", {})
    md["name"] = name
    md["namespace"] = (namespace or "default") if meta["namespaced"] else "default"
    obj.setdefault("kind", meta["kind"])
    try:
        saved = store.put(obj, plural)
    except store.TooLarge as e:
        return _status(413, str(e), "RequestEntityTooLarge")
    return _resp(200, saved)


def _deep_merge(base, patch):
    for k, v in patch.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _patch(meta, group, version, plural, namespace, name, body, content_type):
    ns = (namespace or "default") if meta["namespaced"] else "default"
    patch = json.loads(body) if body else {}
    try:
        current = store.get(plural, ns, name)
    except store.NotFound:
        # apply-patch on a missing object == create it.
        if "apply" in (content_type or ""):
            return _create(meta, group, version, plural, ns, body)
        return _status(404, f"{meta['kind'].lower()} \"{name}\" not found", "NotFound", name)
    merged = _deep_merge(current, patch)
    try:
        saved = store.put(merged, plural)
    except store.TooLarge as e:
        return _status(413, str(e), "RequestEntityTooLarge")
    return _resp(200, saved)


def _delete(meta, group, version, plural, namespace, name):
    ns = (namespace or "default") if meta["namespaced"] else "default"
    try:
        obj = store.get(plural, ns, name)
        store.delete(plural, ns, name)
    except store.NotFound:
        return _status(404, f"{meta['kind'].lower()} \"{name}\" not found", "NotFound", name)
    return _resp(200, {
        "kind": "Status", "apiVersion": "v1", "status": "Success",
        "details": {"name": name, "kind": plural},
    })


def _get_synthetic(meta, plural, namespace, name):
    """Serve read-only synthetic resources (pods) built from live AWS state."""
    try:
        if name:
            pod = podview.get_pod(namespace or "default", name)
            if not pod:
                return _status(404, f"pods \"{name}\" not found", "NotFound", name)
            return _resp(200, pod)
        items = podview.list_pods(namespace)
        return _resp(200, {
            "kind": meta["kind"] + "List",
            "apiVersion": "v1",
            "metadata": {"resourceVersion": "0"},
            "items": items,
        })
    except Exception as e:
        # No cluster yet, or a transient ECS error: behave like an empty list
        # rather than 500 so `kubectl get pods` degrades to "No resources found".
        msg = type(e).__name__
        if name:
            return _status(404, f"pods \"{name}\" not found ({msg})", "NotFound", name)
        return _resp(200, {
            "kind": meta["kind"] + "List", "apiVersion": "v1",
            "metadata": {"resourceVersion": "0"}, "items": [],
        })


# --------------------------------------------------------------------------
# Entry point (API Gateway HTTP API payload v2)
# --------------------------------------------------------------------------

def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = http.get("path", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    content_type = headers.get("content-type", "")

    # Auth: static bearer token.
    auth = headers.get("authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        return _status(401, "Unauthorized", "Unauthorized")

    target = parse_path(path)
    disc = target.get("disc")

    if disc == "apiversions":
        return _resp(200, _api_versions())
    if disc == "apigroups":
        return _resp(200, _api_group_list())
    if disc == "corelist":
        return _resp(200, _resource_list("", "v1"))
    if disc == "grouplist":
        return _resp(200, _resource_list(target["group"], target["version"]))
    if disc == "version":
        return _resp(200, {"major": "1", "minor": "29", "gitVersion": "v1.29.0-kube53",
                           "platform": "route53/txt"})
    if disc == "openapi_v2":
        # Swagger 2.0 JSON. kubectl's legacy validator accepts an empty doc.
        return _resp(200, {"swagger": "2.0", "info": {"title": "kube53", "version": "v1.29.0"}, "paths": {}})
    if disc == "openapi_v3_root":
        # v3 discovery is JSON. We advertise NO group schemas, so kubectl never
        # requests a per-group protobuf document — which is what previously caused
        # "proto: cannot parse invalid wire-format data" (we were returning JSON
        # where kubectl expected protobuf). Empty paths => it applies without
        # schema validation for our custom types, no --validate=false needed.
        return _resp(200, {"paths": {}})
    if disc == "openapi_v3_group":
        # No schema published for any group. 404 makes kubectl skip validation
        # cleanly instead of trying to parse a body as protobuf.
        return _status(404, "the server could not find the requested resource", "NotFound")
    if disc in ("unknown", None) and "plural" not in target:
        return _status(404, "the server could not find the requested resource", "NotFound")

    group, version, plural = target["group"], target["version"], target["plural"]
    meta = _find_resource(group, version, plural)
    if not meta:
        return _status(404, f"the server doesn't have a resource type \"{plural}\"", "NotFound")

    namespace = target.get("namespace")
    name = target.get("name")

    # Synthetic, read-only resources (e.g. pods derived from live ECS tasks).
    if meta.get("synthetic"):
        if method != "GET":
            return _status(405, f"{plural} are read-only in kube53 (managed via ECS)", "MethodNotAllowed")
        return _get_synthetic(meta, plural, namespace, name)

    if method == "GET":
        if name:
            return _get(meta, group, version, plural, namespace, name)
        return _list(meta, group, version, plural, namespace)
    if method == "POST":
        return _create(meta, group, version, plural, namespace, body)
    if method == "PUT":
        return _replace(meta, group, version, plural, namespace, name, body)
    if method == "PATCH":
        return _patch(meta, group, version, plural, namespace, name, body, content_type)
    if method == "DELETE":
        return _delete(meta, group, version, plural, namespace, name)

    return _status(405, f"method {method} not allowed", "MethodNotAllowed")
