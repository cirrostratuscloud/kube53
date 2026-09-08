# ---------------------------------------------------------------------------
# The controller manager: EventBridge tick -> Step Function -> reconciler Lambda.
# Each phase is a Lambda invoke with a distinct `phase` input. The state machine
# is orchestration + retry/catch; convergence logic lives in the Lambda.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.name}-reconcile"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

locals {
  # Helper to build a Task state that invokes the reconciler with a phase.
  reconcile_phases = [
    "EnsureCluster",
    "ReconcileServices",
    "ReconcileIngresses",
    "ReconcileCronJobs",
    "GarbageCollect",
  ]
}

resource "aws_sfn_state_machine" "reconcile" {
  name     = "${var.name}-reconcile"
  role_arn = aws_iam_role.sfn.arn

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  definition = jsonencode({
    Comment = "kube53 level-based reconcile loop. Reads desired state from Route53, converges AWS."
    StartAt = local.reconcile_phases[0]
    States = merge({
      for idx, phase in local.reconcile_phases : phase => merge(
        {
          Type     = "Task"
          Resource = aws_lambda_function.reconciler.arn
          Parameters = {
            phase = phase
          }
          Retry = [{
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 2
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }]
          # One failing phase must not wedge the whole loop; log and move on.
          Catch = [{
            ErrorEquals = ["States.ALL"]
            Next        = idx == length(local.reconcile_phases) - 1 ? "Done" : local.reconcile_phases[idx + 1]
            ResultPath  = "$.error"
          }]
        },
        idx == length(local.reconcile_phases) - 1
        ? { Next = "Done" }
        : { Next = local.reconcile_phases[idx + 1] }
      )
      },
      {
        Done = {
          Type = "Succeed"
        }
      }
    )
  })

  tags = local.tags
}

# The tick. This is our informer resync interval.
resource "aws_cloudwatch_event_rule" "tick" {
  name                = "${var.name}-reconcile-tick"
  description         = "kube53 control loop tick"
  schedule_expression = "rate(${var.reconcile_interval_minutes} minute${var.reconcile_interval_minutes == 1 ? "" : "s"})"
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "tick" {
  rule     = aws_cloudwatch_event_rule.tick.name
  arn      = aws_sfn_state_machine.reconcile.arn
  role_arn = aws_iam_role.events.arn
  input    = jsonencode({ trigger = "tick" })
}
