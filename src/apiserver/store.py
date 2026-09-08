"""Route53 as etcd.

Every Kubernetes object is a single TXT record:

    <name>.<namespace>.<plural>.k53.<domain>   TXT   "<b64 chunk>" "<b64 chunk>" ...

The value is base64(json(object)) split into <=255-char strings (the TXT limit).
Readers concatenate, decode, and json.loads. This module is the ONLY thing that
knows the wire format; both the apiserver and the reconciler import it.
"""

import base64
import json
import os
import time
import uuid

import boto3

r53 = boto3.client("route53")

HOSTED_ZONE_ID = os.environ["HOSTED_ZONE_ID"]
CLUSTER_DOMAIN = os.environ["CLUSTER_DOMAIN"].rstrip(".")
DATA_SUFFIX = os.environ.get("DATA_SUFFIX", f"k53.{CLUSTER_DOMAIN}").rstrip(".")

# A stable namespace UUID for deterministic object UIDs.
_UID_NS = uuid.UUID("53000000-0000-5300-0000-000000005353")

TXT_CHUNK = 255


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class TooLarge(Exception):
    pass


def record_name(kind_plural: str, namespace: str, name: str) -> str:
    """FQDN of the TXT record backing an object (trailing dot, Route53 style)."""
    return f"{name}.{namespace}.{kind_plural}.{DATA_SUFFIX}."


def cluster_marker_name() -> str:
    return f"_cluster.{DATA_SUFFIX}."


def make_uid(kind: str, namespace: str, name: str) -> str:
    return str(uuid.uuid5(_UID_NS, f"{kind}/{namespace}/{name}"))


def _encode(obj: dict) -> list[str]:
    raw = base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()
    chunks = [raw[i : i + TXT_CHUNK] for i in range(0, len(raw), TXT_CHUNK)]
    # Route53 TXT record set has a hard 4000-char total ceiling across chunks.
    total = sum(len(c) + 3 for c in chunks)  # +3 for quotes + space overhead
    if total > 4000:
        raise TooLarge("object does not fit in a single Route53 TXT record set")
    return chunks


def _decode(chunks: list[str]) -> dict:
    raw = "".join(chunks)
    return json.loads(base64.b64decode(raw).decode())


def _txt_value(chunks: list[str]) -> str:
    # Route53 wants each string quoted and space-separated in one Value.
    return " ".join(f'"{c}"' for c in chunks)


def _parse_txt_value(value: str) -> list[str]:
    # Wire form is: "chunk0" "chunk1" ... Strip the outer quotes of the whole
    # value, then split on the '" "' separator between chunks. (Stripping quotes
    # per-part is wrong: middle parts have no surrounding quotes.)
    if not value:
        return []
    v = value.strip()
    if v.startswith('"'):
        v = v[1:]
    if v.endswith('"'):
        v = v[:-1]
    return v.split('" "')


def _change(action: str, name: str, chunks: list[str], ttl: int = 5):
    return {
        "Action": action,
        "ResourceRecordSet": {
            "Name": name,
            "Type": "TXT",
            "TTL": ttl,
            "ResourceRecords": [{"Value": _txt_value(chunks)}],
        },
    }


def put(obj: dict, kind_plural: str) -> dict:
    """Create or replace an object. Returns the stored object (with metadata)."""
    md = obj.setdefault("metadata", {})
    name = md["name"]
    namespace = md.get("namespace", "default")
    md["namespace"] = namespace

    existing = None
    try:
        existing = get(kind_plural, namespace, name)
    except NotFound:
        pass

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    md["uid"] = make_uid(obj.get("kind", kind_plural), namespace, name)
    if existing:
        md["creationTimestamp"] = existing["metadata"].get("creationTimestamp", now)
        prev_rv = int(existing["metadata"].get("annotations", {}).get("k53.io/rv", "0"))
    else:
        md["creationTimestamp"] = now
        prev_rv = 0
    md.setdefault("annotations", {})["k53.io/rv"] = str(prev_rv + 1)
    md["resourceVersion"] = str(prev_rv + 1)

    chunks = _encode(obj)
    r53.change_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        ChangeBatch={"Changes": [_change("UPSERT", record_name(kind_plural, namespace, name), chunks)]},
    )
    return obj


def get(kind_plural: str, namespace: str, name: str) -> dict:
    fqdn = record_name(kind_plural, namespace, name)
    resp = r53.list_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        StartRecordName=fqdn,
        StartRecordType="TXT",
        MaxItems="1",
    )
    for rr in resp.get("ResourceRecordSets", []):
        if rr["Name"] == fqdn and rr["Type"] == "TXT":
            chunks = []
            for r in rr["ResourceRecords"]:
                chunks.extend(_parse_txt_value(r["Value"]))
            return _decode(chunks)
    raise NotFound(f"{kind_plural} {namespace}/{name} not found")


def delete(kind_plural: str, namespace: str, name: str) -> None:
    fqdn = record_name(kind_plural, namespace, name)
    # Need the exact current record to delete it.
    resp = r53.list_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID, StartRecordName=fqdn, StartRecordType="TXT", MaxItems="1"
    )
    for rr in resp.get("ResourceRecordSets", []):
        if rr["Name"] == fqdn and rr["Type"] == "TXT":
            r53.change_resource_record_sets(
                HostedZoneId=HOSTED_ZONE_ID,
                ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rr}]},
            )
            return
    raise NotFound(f"{kind_plural} {namespace}/{name} not found")


def list_kind(kind_plural: str, namespace: str | None = None) -> list[dict]:
    """List every object of a kind by scanning TXT records under its label."""
    suffix = f".{kind_plural}.{DATA_SUFFIX}."
    out = []
    kwargs = {"HostedZoneId": HOSTED_ZONE_ID}
    while True:
        resp = r53.list_resource_record_sets(**kwargs)
        for rr in resp.get("ResourceRecordSets", []):
            if rr["Type"] != "TXT" or not rr["Name"].endswith(suffix):
                continue
            chunks = []
            for r in rr["ResourceRecords"]:
                chunks.extend(_parse_txt_value(r["Value"]))
            try:
                obj = _decode(chunks)
            except Exception:
                continue
            if namespace and obj.get("metadata", {}).get("namespace", "default") != namespace:
                continue
            out.append(obj)
        if resp.get("IsTruncated"):
            kwargs.update(
                StartRecordName=resp["NextRecordName"],
                StartRecordType=resp["NextRecordType"],
            )
        else:
            break
    return out
