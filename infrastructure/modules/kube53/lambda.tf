# ---------------------------------------------------------------------------
# Package the two Lambdas from ../../../src. Pure-Python, no deps beyond boto3
# (provided by the Lambda runtime), so a zip of the source is enough.
# ---------------------------------------------------------------------------
data "archive_file" "apiserver" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/apiserver"
  output_path = "${path.module}/.build/apiserver.zip"
}

data "archive_file" "reconciler" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/reconciler"
  output_path = "${path.module}/.build/reconciler.zip"
}

resource "aws_cloudwatch_log_group" "apiserver" {
  name              = "/aws/lambda/${var.name}-apiserver"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "reconciler" {
  name              = "/aws/lambda/${var.name}-reconciler"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_lambda_function" "apiserver" {
  function_name    = "${var.name}-apiserver"
  role             = aws_iam_role.apiserver.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.apiserver.output_path
  source_code_hash = data.archive_file.apiserver.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = merge(local.common_env, {
      API_TOKEN = var.api_token
    })
  }

  depends_on = [aws_cloudwatch_log_group.apiserver]
  tags       = local.tags
}

resource "aws_lambda_function" "reconciler" {
  function_name    = "${var.name}-reconciler"
  role             = aws_iam_role.reconciler.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.reconciler.output_path
  source_code_hash = data.archive_file.reconciler.output_base64sha256
  timeout          = 120
  memory_size      = 512

  environment {
    variables = merge(local.common_env, {
      VPC_ID              = var.vpc_id
      PUBLIC_SUBNET_IDS   = join(",", var.public_subnet_ids)
      PRIVATE_SUBNET_IDS  = join(",", var.private_subnet_ids)
      ACM_CERTIFICATE_ARN = var.acm_certificate
      TASK_EXECUTION_ROLE = aws_iam_role.task_execution.arn
      TASK_ROLE           = aws_iam_role.task.arn
      SCHEDULER_ROLE      = aws_iam_role.scheduler.arn
    })
  }

  depends_on = [aws_cloudwatch_log_group.reconciler]
  tags       = local.tags
}
