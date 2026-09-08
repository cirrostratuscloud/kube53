"""Shared AWS clients, config, naming, and tag helpers for the reconciler."""

import os

import boto3

REGION = os.environ.get("REGION") or os.environ["AWS_REGION"]

ecs = boto3.client("ecs", region_name=REGION)
elb = boto3.client("elbv2", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
scheduler = boto3.client("scheduler", region_name=REGION)

CLUSTER_NAME = os.environ["CLUSTER_NAME"]
CLUSTER_DOMAIN = os.environ["CLUSTER_DOMAIN"].rstrip(".")
VPC_ID = os.environ["VPC_ID"]
PUBLIC_SUBNETS = [s for s in os.environ["PUBLIC_SUBNET_IDS"].split(",") if s]
PRIVATE_SUBNETS = [s for s in os.environ["PRIVATE_SUBNET_IDS"].split(",") if s]
ACM_CERT = os.environ["ACM_CERTIFICATE_ARN"]
TASK_EXECUTION_ROLE = os.environ["TASK_EXECUTION_ROLE"]
TASK_ROLE = os.environ["TASK_ROLE"]
SCHEDULER_ROLE = os.environ["SCHEDULER_ROLE"]

MANAGED_TAG_KEY = "k53.io/managed"
MANAGED_TAG_VAL = "true"
OBJECT_TAG_KEY = "k53.io/key"


def object_key(kind: str, namespace: str, name: str) -> str:
    return f"{kind}/{namespace}/{name}"


def managed_tags(kind: str, namespace: str, name: str) -> list[dict]:
    return [
        {"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VAL},
        {"Key": OBJECT_TAG_KEY, "Value": object_key(kind, namespace, name)},
    ]


def aws_name(kind: str, namespace: str, name: str) -> str:
    """A DNS/AWS-safe resource name. ALB names cap at 32 chars, so hash-ish it."""
    base = f"k53-{kind[:3]}-{namespace}-{name}".lower()
    base = "".join(c if c.isalnum() or c == "-" else "-" for c in base)
    return base[:32].rstrip("-")


def log(*args):
    print("[reconciler]", *args, flush=True)
