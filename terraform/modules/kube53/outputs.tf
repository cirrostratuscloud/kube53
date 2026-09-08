output "api_endpoint" {
  description = "kube53 apiserver endpoint (custom domain) kubectl talks to."
  value       = "https://api.${var.cluster_domain}"
}

output "api_gateway_endpoint" {
  description = "Raw API Gateway endpoint (before custom domain / DNS propagation)."
  value       = aws_apigatewayv2_api.apiserver.api_endpoint
}

output "reconcile_state_machine_arn" {
  description = "The reconcile Step Function ARN. Start it manually to force a sync."
  value       = aws_sfn_state_machine.reconcile.arn
}

output "apiserver_function" {
  value = aws_lambda_function.apiserver.function_name
}

output "reconciler_function" {
  value = aws_lambda_function.reconciler.function_name
}
