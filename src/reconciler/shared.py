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


def ensure_security_groups():
    """ALB SG (allow 80/443 from world) and task SG (allow from ALB SG)."""
    alb_sg = _find_sg(ALB_SG_NAME)
    if not alb_sg:
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

    task_sg = _find_sg(TASK_SG_NAME)
    if not task_sg:
        task_sg = ec2.create_security_group(
            GroupName=TASK_SG_NAME, Description="kube53 Fargate tasks", VpcId=A.VPC_ID,
            TagSpecifications=[{"ResourceType": "security-group",
                                "Tags": [{"Key": A.MANAGED_TAG_KEY, "Value": A.MANAGED_TAG_VAL}]}],
        )["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=task_sg,
            IpPermissions=[{"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535,
                            "UserIdGroupPairs": [{"GroupId": alb_sg}]}],
        )
        log(f"created task SG {task_sg}")

    return alb_sg, task_sg


def _find_alb():
    try:
        resp = elb.describe_load_balancers(Names=[ALB_NAME])
        return resp["LoadBalancers"][0]
    except elb.exceptions.LoadBalancerNotFoundException:
        return None


def ensure_alb():
    """Return (alb_arn, dns_name, canonical_zone_id, https_listener_arn, task_sg)."""
    alb_sg, task_sg = ensure_security_groups()
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
    """One HTTPS:443 listener with a default 404 fixed-response; rules added per Service/Ingress.

    (We keep an HTTP:80 -> HTTPS redirect too, so plain http works in demos.)
    """
    listeners = elb.describe_listeners(LoadBalancerArn=alb_arn).get("Listeners", [])
    by_port = {l["Port"]: l for l in listeners}

    https = by_port.get(443)
    if not https:
        https = elb.create_listener(
            LoadBalancerArn=alb_arn, Protocol="HTTPS", Port=443,
            Certificates=[{"CertificateArn": A.ACM_CERT}],
            SslPolicy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            DefaultActions=[{"Type": "fixed-response", "FixedResponseConfig": {
                "StatusCode": "404", "ContentType": "text/plain",
                "MessageBody": "kube53: no route",
            }}],
        )["Listeners"][0]

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
