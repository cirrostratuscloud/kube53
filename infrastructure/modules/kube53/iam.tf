# ---------------------------------------------------------------------------
# apiserver Lambda role: only touches Route53 (the datastore) + logs.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apiserver" {
  name               = "${var.name}-apiserver"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "apiserver" {
  statement {
    sid    = "Route53Datastore"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
      "route53:ListResourceRecordSets",
      "route53:GetChange",
    ]
    resources = [
      "arn:aws:route53:::hostedzone/${var.hosted_zone_id}",
      "arn:aws:route53:::change/*",
    ]
  }

  # Read-only ECS access so `kubectl get pods` can synthesize Pods from tasks.
  # These ECS Describe/List actions are not resource-scopeable in a useful way,
  # so allow broadly but note the apiserver only ever READS.
  statement {
    sid    = "ECSReadForPods"
    effect = "Allow"
    actions = [
      "ecs:ListServices",
      "ecs:DescribeServices",
      "ecs:ListTasks",
      "ecs:DescribeTasks",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:${local.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "apiserver" {
  name   = "apiserver"
  role   = aws_iam_role.apiserver.id
  policy = data.aws_iam_policy_document.apiserver.json
}

# ---------------------------------------------------------------------------
# reconciler Lambda role: the kubelet/controller. Needs to build real infra.
# Scoped to managed resources via tag conditions where the API supports it.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "reconciler" {
  name               = "${var.name}-reconciler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "reconciler" {
  # Read desired state from Route53 and publish service addresses back.
  statement {
    sid    = "Route53"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
      "route53:ListResourceRecordSets",
      "route53:GetChange",
    ]
    resources = [
      "arn:aws:route53:::hostedzone/${var.hosted_zone_id}",
      "arn:aws:route53:::change/*",
    ]
  }

  # ECS: cluster + services + task defs. Create/Describe are not resource-scoped
  # in a way that's practical here, so allow broadly but GC only touches tagged.
  statement {
    sid    = "ECS"
    effect = "Allow"
    actions = [
      "ecs:CreateCluster",
      "ecs:DeleteCluster",
      "ecs:DescribeClusters",
      "ecs:PutClusterCapacityProviders",
      "ecs:RegisterTaskDefinition",
      "ecs:DeregisterTaskDefinition",
      "ecs:DescribeTaskDefinition",
      "ecs:ListTaskDefinitions",
      "ecs:CreateService",
      "ecs:UpdateService",
      "ecs:DeleteService",
      "ecs:DescribeServices",
      "ecs:ListServices",
      "ecs:RunTask",
      "ecs:StopTask",
      "ecs:ListTasks",
      "ecs:DescribeTasks",
      "ecs:TagResource",
      "ecs:ListTagsForResource",
    ]
    resources = ["*"]
  }

  # ELBv2: ALBs, target groups, listeners for Service/Ingress.
  statement {
    sid    = "ELB"
    effect = "Allow"
    actions = [
      "elasticloadbalancing:CreateLoadBalancer",
      "elasticloadbalancing:DeleteLoadBalancer",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:CreateTargetGroup",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth",
      "elasticloadbalancing:CreateListener",
      "elasticloadbalancing:DeleteListener",
      "elasticloadbalancing:DescribeListeners",
      "elasticloadbalancing:ModifyListener",
      "elasticloadbalancing:CreateRule",
      "elasticloadbalancing:DeleteRule",
      "elasticloadbalancing:ModifyRule",
      "elasticloadbalancing:DescribeRules",
      "elasticloadbalancing:ModifyTargetGroupAttributes",
      "elasticloadbalancing:AddTags",
      "elasticloadbalancing:DescribeTags",
    ]
    resources = ["*"]
  }

  # The first ALB in an account needs the ELB service-linked role to exist.
  # CreateLoadBalancer creates it implicitly, which requires this action.
  # Scoped to the ELB service only via the AWSServiceName condition, so this is
  # not a broad IAM grant. No-op on every subsequent call once the role exists.
  statement {
    sid       = "ELBServiceLinkedRole"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::*:role/aws-service-role/elasticloadbalancing.amazonaws.com/AWSServiceRoleForElasticLoadBalancing"]

    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values   = ["elasticloadbalancing.amazonaws.com"]
    }
  }

  # EC2: security groups for the ALB + tasks.
  statement {
    sid    = "EC2SecurityGroups"
    effect = "Allow"
    actions = [
      "ec2:CreateSecurityGroup",
      "ec2:DeleteSecurityGroup",
      "ec2:DescribeSecurityGroups",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:DescribeSubnets",
      "ec2:DescribeVpcs",
      "ec2:CreateTags",
    ]
    resources = ["*"]
  }

  # EventBridge Scheduler for CronJobs.
  statement {
    sid    = "Scheduler"
    effect = "Allow"
    actions = [
      "scheduler:CreateSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
      "scheduler:GetSchedule",
      "scheduler:ListSchedules",
      "scheduler:TagResource",
    ]
    resources = ["*"]
  }

  # PassRole so ECS tasks / scheduler can assume the task + invocation roles.
  statement {
    sid       = "PassRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.task_execution.arn, aws_iam_role.task.arn, aws_iam_role.scheduler.arn]
  }

  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:${local.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "reconciler" {
  name   = "reconciler"
  role   = aws_iam_role.reconciler.id
  policy = data.aws_iam_policy_document.reconciler.json
}

# ---------------------------------------------------------------------------
# ECS task execution + task roles ("the node"). Minimal.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The task defs use the awslogs driver with awslogs-create-group=true, so the
# ECS agent creates the /kube53/... log group at task start using THIS role.
# The managed ECSTaskExecution policy grants CreateLogStream/PutLogEvents but not
# CreateLogGroup, so add it here, scoped to our log groups.
data "aws_iam_policy_document" "task_execution_logs" {
  statement {
    sid    = "CreateKube53LogGroups"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:*:${local.account_id}:log-group:/kube53/*",
      "arn:aws:logs:*:${local.account_id}:log-group:/kube53/*:*",
    ]
  }
}

resource "aws_iam_role_policy" "task_execution_logs" {
  name   = "kube53-task-logs"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_logs.json
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

# ---------------------------------------------------------------------------
# Scheduler invocation role (lets EventBridge Scheduler RunTask for CronJobs).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    effect    = "Allow"
    actions   = ["ecs:RunTask"]
    resources = ["*"]
  }
  statement {
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.task_execution.arn, aws_iam_role.task.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# ---------------------------------------------------------------------------
# Step Functions role: invoke the reconciler Lambda.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${var.name}-reconcile-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "sfn" {
  statement {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.reconciler.arn, "${aws_lambda_function.reconciler.arn}:*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "sfn"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

# ---------------------------------------------------------------------------
# EventBridge rule role: start the Step Function on each tick.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events" {
  name               = "${var.name}-reconcile-tick"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "events" {
  statement {
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.reconcile.arn]
  }
}

resource "aws_iam_role_policy" "events" {
  name   = "events"
  role   = aws_iam_role.events.id
  policy = data.aws_iam_policy_document.events.json
}
