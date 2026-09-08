"""Lazily-created shared infra: one ALB + security groups for the whole cluster.

MVP choice: a single cluster-wide ALB fronts all Services and Ingresses, using
host/path listener rules to route. That keeps costs sane (one ALB, not one per
Service) and is enough to demo real traffic. Everything is discovered by tag so
reconciliation is idempotent across ticks.
"""

import awsenv as A
from awsenv import elb, ec2, log

ALB_NAME = "k53-shared"
ALB_SG_NAME = "k53-alb-sg"
TASK_SG_NAME = "k53-task-sg"


def _find_sg(name):
    resp = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [name]},
        {"Name": "vpc-id", "Values": [A.VPC_ID]},
    ])
    groups = resp.get("SecurityGroups", [])
    return groups[0]["GroupId"] if groups else None


def _vpc_cidr():
    v = ec2.describe_vpcs(VpcIds=[A.VPC_ID])["Vpcs"][0]
    return v["CidrBlock"]


def ensure_task_sg():
    """Task SG, allowing ingress from within the VPC. ALB-independent.

    Tasks accept traffic from anything in the VPC CIDR (which includes the ALB),
    so this SG exists on its own — a Service can run without any ALB/Ingress.
    """
    task_sg = _find_sg(TASK_SG_NAME)
    if task_sg:
        return task_sg
    task_sg = ec2.create_security_group(
        GroupName=TASK_SG_NAME, Description="kube53 Fargate tasks", VpcId=A.VPC_ID,
        TagSpecifications=[{"ResourceType": "security-group",
                            "Tags": [{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL}]}],
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=task_sg,
        IpPermissions=[{"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535,
                        "IpRanges": [{"CidrIp": _vpc_cidr()}]}],
    )
    log(f"created task SG {task_sg}")
    return task_sg


def ensure_alb_sg():
    """ALB SG, allowing 80/443 from the world. Only needed when an ALB exists."""
    alb_sg = _find_sg(ALB_SG_NAME)
    if alb_sg:
        return alb_sg
    alb_sg = ec2.create_security_group(
        GroupName=ALB_SG_NAME, Description="kube53 shared ALB", VpcId=A.VPC_ID,
        TagSpecifications=[{"ResourceType": "security-group",
                            "Tags": [{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL}]}],
    )["GroupId"]
    for port in (80, 443):
        ec2.authorize_security_group_ingress(
            GroupId=alb_sg,
            IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        )
    log(f"created ALB SG {alb_sg}")
    return alb_sg


def _find_alb():
    try:
        resp = elb.describe_load_balancers(Names=[ALB_NAME])
        return resp["LoadBalancers"][0]
    except elb.exceptions.LoadBalancerNotFoundException:
        return None


def ensure_alb():
    """Return (alb_arn, dns_name, canonical_zone_id, https_listener_arn, task_sg).

    Provisions the shared ALB. Called by the Ingress reconciler ONLY — a Service
    on its own never creates an ALB.
    """
    alb_sg = ensure_alb_sg()
    task_sg = ensure_task_sg()
    alb = _find_alb()
    if not alb:
        log("creating shared ALB")
        alb = elb.create_load_balancer(
            Name=ALB_NAME,
            Subnets=A.PUBLIC_SUBNETS,
            SecurityGroups=[alb_sg],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
            Tags=[{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL}],
        )["LoadBalancers"][0]
        # wait a beat for it to exist; describe again next tick if not ready
    alb_arn = alb["LoadBalancerArn"]

    listener = _ensure_listeners(alb_arn)
    return {
        "alb_arn": alb_arn,
        "dns_name": alb["DNSName"],
        "zone_id": alb["CanonicalHostedZoneId"],
        "https_listener": listener,
        "task_sg": task_sg,
        "alb_sg": alb_sg,
    }


def _ensure_listeners(alb_arn):
    """One HTTPS:443 listener with a default 503 (dead route); rules added per Ingress.

    There is no default backend, so anything that doesn't match an Ingress host/
    path rule falls through to this fixed 503 response. (We also keep an HTTP:80
    -> HTTPS redirect so plain http works in demos.)
    """
    listeners = elb.describe_listeners(LoadBalancerArn=alb_arn).get("Listeners", [])
    by_port = {l["Port"]: l for l in listeners}

    default_action = {"Type": "fixed-response", "FixedResponseConfig": {
        "StatusCode": "503", "ContentType": "text/plain",
        "MessageBody": "kube53: no route",
    }}

    https = by_port.get(443)
    if not https:
        https = elb.create_listener(
            LoadBalancerArn=alb_arn, Protocol="HTTPS", Port=443,
            Certificates=[{"CertificateArn": A.ACM_CERT}],
            SslPolicy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            DefaultActions=[default_action],
        )["Listeners"][0]
    else:
        # Converge an existing listener's default action to the 503 (self-heals
        # an ALB created before we switched the default from 404 -> 503).
        cur = https.get("DefaultActions", [{}])
        cur_code = cur[0].get("FixedResponseConfig", {}).get("StatusCode") if cur else None
        if cur_code != "503":
            elb.modify_listener(ListenerArn=https["ListenerArn"], DefaultActions=[default_action])
            log("updated HTTPS listener default action -> 503")

    if 80 not in by_port:
        elb.create_listener(
            LoadBalancerArn=alb_arn, Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "redirect", "RedirectConfig": {
                "Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301",
            }}],
        )

    return https["ListenerArn"]


def find_target_group(name):
    try:
        return elb.describe_target_groups(Names=[name])["TargetGroups"][0]
    except elb.exceptions.TargetGroupNotFoundException:
        return None


def ensure_target_group(name, port, key_tag):
    tg = find_target_group(name)
    if tg:
        return tg["TargetGroupArn"]
    tg = elb.create_target_group(
        Name=name, Protocol="HTTP", Port=port, VpcId=A.VPC_ID,
        TargetType="ip",  # Fargate awsvpc
        HealthCheckProtocol="HTTP", HealthCheckPath="/", HealthCheckIntervalSeconds=30,
        Matcher={"HttpCode": "200-399"},
        Tags=[{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL},
              {"Key": A.OBJECT_TAG_KEY, "Value": key_tag}],
    )["TargetGroups"][0]
    log(f"created target group {name}")
    return tg["TargetGroupArn"]


# ---------------------------------------------------------------------------
# Teardown. Called by GC when the cluster marker is gone (full teardown). The
# ALB and its security groups aren't backed by any Kubernetes object, so they
# are torn down as part of dismantling the cluster rather than per-object GC.
# ---------------------------------------------------------------------------

def teardown_alb():
    """Delete the shared ALB and its listeners. Returns True if gone/absent."""
    alb = _find_alb()
    if not alb:
        return True
    arn = alb["LoadBalancerArn"]
    # Listeners must go before the LB (deleting the LB also drops them, but be
    # explicit so rules referencing target groups don't block TG cleanup).
    for l in elb.describe_listeners(LoadBalancerArn=arn).get("Listeners", []):
        try:
            elb.delete_listener(ListenerArn=l["ListenerArn"])
        except Exception as e:
            log(f"listener delete deferred: {type(e).__name__}")
    elb.delete_load_balancer(LoadBalancerArn=arn)
    log("deleted shared ALB")
    return False  # deletion is async; report not-yet-gone so GC rechecks next tick


def _delete_sg(name):
    sg = _find_sg(name)
    if not sg:
        return 0
    try:
        ec2.delete_security_group(GroupId=sg)
        log(f"deleted security group {name}")
        return 1
    except Exception as e:
        # Still attached to ENIs while the ALB finishes deleting; retry next tick.
        log(f"security group {name} delete deferred: {type(e).__name__}")
        return 0


def teardown_alb_sg():
    """Delete the ALB security group (after the ALB is gone)."""
    return _delete_sg(ALB_SG_NAME)


def teardown_task_sg():
    """Delete the task security group (only on full cluster teardown)."""
    return _delete_sg(TASK_SG_NAME)


def find_alb():
    """Public read-only ALB accessor (dict or None). Never creates."""
    return _find_alb()


def find_https_listener():
    """Return the HTTPS:443 listener ARN if the ALB exists, else None.

    Read-only — used by GC, which must never create infra.
    """
    alb = _find_alb()
    if not alb:
        return None
    for l in elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"]).get("Listeners", []):
        if l.get("Port") == 443:
            return l["ListenerArn"]
    return None
