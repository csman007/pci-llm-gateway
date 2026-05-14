# ── Secrets Manager secrets ────────────────────────────────────────────────
# Sensitive values are stored here and fetched by the app at runtime.
# The ARNs (not the values) are passed as env vars to Lambda.

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name                    = "pci-llm-gateway/anthropic-api-key"
  recovery_window_in_days = 7
  kms_key_id              = aws_kms_key.pci.arn
}

resource "aws_secretsmanager_secret_version" "anthropic_api_key" {
  secret_id     = aws_secretsmanager_secret.anthropic_api_key.id
  secret_string = var.anthropic_api_key
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = "pci-llm-gateway/openai-api-key"
  recovery_window_in_days = 7
  kms_key_id              = aws_kms_key.pci.arn
}

resource "aws_secretsmanager_secret_version" "openai_api_key" {
  secret_id     = aws_secretsmanager_secret.openai_api_key.id
  secret_string = var.openai_api_key
}

resource "aws_secretsmanager_secret" "jwt_secret" {
  name                    = "pci-llm-gateway/jwt-secret"
  recovery_window_in_days = 7
  kms_key_id              = aws_kms_key.pci.arn
}

resource "aws_secretsmanager_secret_version" "jwt_secret" {
  secret_id     = aws_secretsmanager_secret.jwt_secret.id
  secret_string = var.jwt_secret
}

resource "aws_secretsmanager_secret" "api_key" {
  name                    = "pci-llm-gateway/api-key"
  recovery_window_in_days = 7
  kms_key_id              = aws_kms_key.pci.arn
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = var.api_key
}

# ── Lambda function ────────────────────────────────────────────────────────

resource "aws_lambda_function" "gateway" {
  function_name = "pci-llm-gateway"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.gateway.repository_url}:latest"
  timeout       = 30
  memory_size   = 1024
  kms_key_arn   = aws_kms_key.pci.arn

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.gateway.id]
  }

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      # Non-sensitive config — safe as plain env vars
      LOG_LEVEL            = "INFO"
      ENV                  = "prod"
      AWS_REGION_NAME      = var.aws_region
      COGNITO_USER_POOL_ID = aws_cognito_user_pool.main.id
      COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.main.id

      # Secrets Manager ARNs — the app fetches the actual values at runtime
      ANTHROPIC_API_KEY_SECRET_ARN = aws_secretsmanager_secret.anthropic_api_key.arn
      OPENAI_API_KEY_SECRET_ARN    = aws_secretsmanager_secret.openai_api_key.arn
      JWT_SECRET_ARN               = aws_secretsmanager_secret.jwt_secret.arn
      API_KEY_SECRET_ARN           = aws_secretsmanager_secret.api_key.arn

      # Agent layer tuning — non-sensitive, safe as plain env vars
      AGENT_MODEL_DEFAULT          = var.agent_model_default
      AGENT_MODEL_THINKING         = var.agent_model_thinking
      AGENT_MAX_STEPS              = tostring(var.agent_max_steps)
      AGENT_THINKING_BUDGET_TOKENS = tostring(var.agent_thinking_budget_tokens)
      SUBAGENT_MODEL               = var.subagent_model
      SUBAGENT_MAX_TOKENS          = tostring(var.subagent_max_tokens)
      JUDGE_MODEL                  = var.judge_model
      JUDGE_MAX_TOKENS             = tostring(var.judge_max_tokens)
      AUDIT_LOG_TABLE              = var.audit_log_table
      POSTGRES_DSN                 = "postgresql://gateway:${var.rds_password}@${aws_db_instance.pgvector.endpoint}/pci_gateway"
      EMBEDDING_MODEL              = var.embedding_model
      RAG_TOP_K                    = tostring(var.rag_top_k)
      RAG_HYBRID                   = var.rag_hybrid
      RAG_RRF_K                    = tostring(var.rag_rrf_k)
      QUERY_ANALYZER_MODEL         = var.query_analyzer_model
      QUERY_ANALYZER_MAX_TOKENS    = tostring(var.query_analyzer_max_tokens)
      GROUNDING_STRICT             = var.grounding_strict
      CONTEXT_MAX_CHUNK_CHARS      = tostring(var.context_max_chunk_chars)
      RAG_MIN_SCORE                = var.rag_min_score
      RAG_CONTEXT_BUDGET_CHARS     = tostring(var.rag_context_budget_chars)
      GROUNDING_VALIDATE           = var.grounding_validate
      GROUNDING_THRESHOLD          = var.grounding_threshold

      # Observability
      OTEL_ENABLED                 = tostring(var.otel_enabled)
      OTEL_EXPORTER_OTLP_ENDPOINT  = var.otel_exporter_otlp_endpoint
      OTEL_SERVICE_NAME            = var.otel_service_name
    }
  }
}

resource "aws_ecr_repository" "gateway" {
  name                 = "pci-llm-gateway"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.pci.arn
  }
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.gateway.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.llm_gateway.execution_arn}/*/*"
}
