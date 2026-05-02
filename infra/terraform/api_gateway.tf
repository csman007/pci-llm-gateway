resource "aws_apigatewayv2_api" "llm_gateway" {
  name          = "pci-llm-gateway"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.llm_gateway.id
  name        = "prod"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gw.arn
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.llm_gateway.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.gateway.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.llm_gateway.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.main.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

resource "aws_apigatewayv2_route" "inference" {
  api_id             = aws_apigatewayv2_api.llm_gateway.id
  route_key          = "POST /v1/inference"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_cloudwatch_log_group" "api_gw" {
  name              = "/aws/apigateway/pci-llm-gateway"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.pci.arn
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/pci-llm-gateway"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.pci.arn
}
