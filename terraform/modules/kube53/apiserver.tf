# ---------------------------------------------------------------------------
# HTTP API in front of the apiserver Lambda, on a custom domain
# api.<cluster-domain> with the ACM cert, so kubectl gets valid TLS.
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "apiserver" {
  name          = "${var.name}-apiserver"
  protocol_type = "HTTP"
  tags          = local.tags
}

resource "aws_apigatewayv2_integration" "apiserver" {
  api_id                 = aws_apigatewayv2_api.apiserver.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.apiserver.invoke_arn
  payload_format_version = "2.0"
}

# Catch-all: the Lambda does its own kube-style routing.
resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.apiserver.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.apiserver.id}"
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/aws/apigw/${var.name}-apiserver"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.apiserver.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId = "$context.requestId"
      method    = "$context.httpMethod"
      path      = "$context.path"
      status    = "$context.status"
      error     = "$context.error.message"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.apiserver.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.apiserver.execution_arn}/*/*"
}

# Custom domain: api.<cluster-domain>
resource "aws_apigatewayv2_domain_name" "api" {
  domain_name = "api.${var.cluster_domain}"

  domain_name_configuration {
    certificate_arn = var.acm_certificate
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = local.tags
}

resource "aws_apigatewayv2_api_mapping" "api" {
  api_id      = aws_apigatewayv2_api.apiserver.id
  domain_name = aws_apigatewayv2_domain_name.api.id
  stage       = aws_apigatewayv2_stage.default.id
}

resource "aws_route53_record" "api" {
  zone_id = var.hosted_zone_id
  name    = "api.${var.cluster_domain}"
  type    = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.api.domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.api.domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}
