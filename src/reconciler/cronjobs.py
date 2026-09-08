"""Reconcile CronJobs into EventBridge Scheduler schedules that RunTask on ECS.

  spec:
    schedule: "*/5 * * * *"          # standard cron (5-field)
    jobTemplate:
      spec:
        template:
          metadata:
            annotations:
              k53.io/image: busybox:latest
          spec:
            containers:
              - name: job
                command: ["sh","-c","echo hello from route53"]

We translate the 5-field cron into an EventBridge Scheduler cron(...) expression
(EventBridge uses 6 fields incl. year) and target ECS RunTask with a task def
built from the jobTemplate.
"""

import json

import store
import awsenv as A
from awsenv import ecs, scheduler, log
import taskdefs

PLURAL = "cronjobs"
KIND = "CronJob"


def _to_eventbridge_cron(schedule: str) -> str:
    """k8s 5-field cron -> EventBridge 6-field cron(min hour dom mon dow year).

    EventBridge quirk: you cannot specify both day-of-month and day-of-week; one
    must be '?'. We map k8s '*' dow to '?' when dom is set, matching common usage.
    """
    parts = schedule.split()
    if len(parts) != 5:
        # allow rate() passthrough or already-6-field
        return schedule
    minute, hour, dom, month, dow = parts
    if dom != "*" and dow == "*":
        dow = "?"
    elif dow != "*":
        dom = "?"
    else:
        dow = "?"
    return f"cron({minute} {hour} {dom} {month} {dow} *)"


def _register_job_task_def(namespace, name, image, command):
    family = A.aws_name("cj", namespace, name)
    container = {
        "name": name,
        "image": image,
        "essential": True,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/kube53/jobs/{namespace}/{name}",
                "awslogs-region": A.REGION,
                "awslogs-stream-prefix": "k53",
                "awslogs-create-group": "true",
            },
        },
    }
    if command:
        container["command"] = command
    # Idempotent: reuses the current ACTIVE revision unless the spec changed.
    arn, _created = taskdefs.ensure(family, container, KIND, namespace, name)
    return arn


def _cluster_arn():
    clusters = ecs.describe_clusters(clusters=[A.CLUSTER_NAME]).get("clusters", [])
    return clusters[0]["clusterArn"] if clusters else None


def reconcile():
    jobs = store.list_kind(PLURAL)
    if not jobs:
        log("no cronjobs")
        return {"phase": "ReconcileCronJobs", "count": 0}

    cluster_arn = _cluster_arn()
    if not cluster_arn:
        log("cluster not ready; cronjobs will schedule next tick")
        return {"phase": "ReconcileCronJobs", "count": 0, "reason": "no cluster"}

    reconciled = 0
    for job in jobs:
        md = job.get("metadata", {})
        name = md["name"]
        namespace = md.get("namespace", "default")
        spec = job.get("spec", {})
        schedule = spec.get("schedule")
        if not schedule:
            continue

        pod_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {})
        pod_md = pod_spec.get("metadata", {}).get("annotations", {})
        containers = pod_spec.get("spec", {}).get("containers", [{}])
        image = pod_md.get("k53.io/image") or containers[0].get("image")
        command = containers[0].get("command")
        if not image:
            log(f"cronjob {namespace}/{name} has no image; skipping")
            continue

        task_def = _register_job_task_def(namespace, name, image, command)
        sched_name = A.aws_name("cj", namespace, name)
        eb_cron = _to_eventbridge_cron(schedule)

        target = {
            "Arn": cluster_arn,
            "RoleArn": A.SCHEDULER_ROLE,
            "EcsParameters": {
                "TaskDefinitionArn": task_def,
                "LaunchType": "FARGATE",
                "TaskCount": 1,
                "NetworkConfiguration": {
                    "awsvpcConfiguration": {
                        "Subnets": A.PRIVATE_SUBNETS,
                        "AssignPublicIp": "DISABLED",
                    }
                },
            },
        }

        params = dict(
            Name=sched_name,
            ScheduleExpression=eb_cron,
            FlexibleTimeWindow={"Mode": "OFF"},
            Target=target,
            State="ENABLED",
        )
        try:
            existing = scheduler.get_schedule(Name=sched_name)
            # Only update if the expression or the target task def changed, so we
            # don't rewrite the schedule on every tick.
            cur_expr = existing.get("ScheduleExpression")
            cur_td = existing.get("Target", {}).get("EcsParameters", {}).get("TaskDefinitionArn")
            if cur_expr == eb_cron and cur_td == task_def:
                log(f"schedule {sched_name} already converged")
            else:
                scheduler.update_schedule(**params)
                log(f"updated schedule {sched_name} ({eb_cron})")
        except scheduler.exceptions.ResourceNotFoundException:
            scheduler.create_schedule(**params)
            log(f"created schedule {sched_name} ({eb_cron})")
        reconciled += 1

    return {"phase": "ReconcileCronJobs", "count": reconciled}
