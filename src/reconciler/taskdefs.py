"""Idempotent ECS task-definition registration.

register_task_definition ALWAYS mints a new revision — so calling it every
reconcile tick silently piles up revisions (we once hit revision 951 after a
single deploy) and rolls the service each time. These helpers register a new
revision only when the desired container spec actually differs from the family's
current ACTIVE task def, making reconciliation genuinely level-based/idempotent.
"""

import awsenv as A
from awsenv import ecs, log


def describe_active(family):
    """Latest ACTIVE task def for a family, or None if the family doesn't exist."""
    try:
        return ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
    except ecs.exceptions.ClientException:
        return None


def _container_matches(current_container, desired_container):
    c, d = current_container, desired_container
    if c.get("name") != d.get("name"):
        return False
    if c.get("image") != d.get("image"):
        return False
    if (c.get("command") or None) != (d.get("command") or None):
        return False
    cur_ports = [(p.get("containerPort"), p.get("protocol")) for p in c.get("portMappings", [])]
    des_ports = [(p.get("containerPort"), p.get("protocol")) for p in d.get("portMappings", [])]
    if cur_ports != des_ports:
        return False
    cur_log = c.get("logConfiguration", {}).get("options", {})
    des_log = d.get("logConfiguration", {}).get("options", {})
    if cur_log != des_log:
        return False
    return True


def matches(current_td, desired_container):
    """True if current task def's roles + single container match desired."""
    if current_td.get("executionRoleArn") != A.TASK_EXECUTION_ROLE:
        return False
    if current_td.get("taskRoleArn") != A.TASK_ROLE:
        return False
    containers = current_td.get("containerDefinitions", [])
    if len(containers) != 1:
        return False
    return _container_matches(containers[0], desired_container)


def ensure(family, container, kind, namespace, name):
    """Return a task def ARN, registering a new revision only if spec changed."""
    current = describe_active(family)
    if current and matches(current, container):
        return current["taskDefinitionArn"], False
    resp = ecs.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        executionRoleArn=A.TASK_EXECUTION_ROLE,
        taskRoleArn=A.TASK_ROLE,
        containerDefinitions=[container],
        tags=[{"key": A.MANAGED_TAG_KEY, "value": A.MANAGED_TAG_VAL},
              {"key": A.OBJECT_TAG_KEY, "value": A.object_key(kind, namespace, name)}],
    )
    arn = resp["taskDefinition"]["taskDefinitionArn"]
    log(f"registered new task def {arn}")
    return arn, True
