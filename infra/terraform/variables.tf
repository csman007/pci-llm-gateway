variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "anthropic_api_key" {
  description = "Anthropic API key"
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
}

variable "jwt_secret" {
  description = "JWT signing secret for dev token endpoint"
  type        = string
  sensitive   = true
}

variable "api_key" {
  description = "API key required on every request via x-api-key header"
  type        = string
  sensitive   = true
}

# ── Agent layer tuning ─────────────────────────────────────────────────────

variable "agent_model_default" {
  description = "Claude model used by the orchestrator for standard (non-thinking) runs"
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "agent_model_thinking" {
  description = "Claude model used by the orchestrator when thinking=true"
  type        = string
  default     = "claude-opus-4-7"
}

variable "agent_max_steps" {
  description = "Maximum tool-use iterations per agent run before forcing a final response"
  type        = number
  default     = 10
}

variable "agent_thinking_budget_tokens" {
  description = "Token budget for extended thinking blocks"
  type        = number
  default     = 8000
}

variable "subagent_model" {
  description = "Claude model used for compliance and analyst subagent calls"
  type        = string
  default     = "claude-haiku-4-5-20251001"
}

variable "subagent_max_tokens" {
  description = "Maximum tokens per subagent response"
  type        = number
  default     = 1024
}

variable "judge_model" {
  description = "Claude model used for LLM-as-judge evaluation"
  type        = string
  default     = "claude-haiku-4-5-20251001"
}

variable "judge_max_tokens" {
  description = "Maximum tokens for the judge's scoring response"
  type        = number
  default     = 256
}

variable "audit_log_table" {
  description = "DynamoDB table name for the audit log (queried by the agent's query_audit_log tool)"
  type        = string
  default     = "pci-llm-gateway-audit"
}

# ── RAG / pgvector ─────────────────────────────────────────────────────────

variable "rds_instance_class" {
  description = "RDS instance class for the pgvector database"
  type        = string
  default     = "db.t3.medium"
}

variable "rds_allocated_storage" {
  description = "RDS allocated storage in GB"
  type        = number
  default     = 20
}

variable "rds_password" {
  description = "Master password for the pgvector RDS instance"
  type        = string
  sensitive   = true
}

variable "embedding_model" {
  description = "OpenAI embedding model used for RAG"
  type        = string
  default     = "text-embedding-3-small"
}

variable "rag_top_k" {
  description = "Number of PCI DSS chunks retrieved per query"
  type        = number
  default     = 5
}

variable "rag_hybrid" {
  description = "Enable hybrid vector + BM25 retrieval with RRF fusion"
  type        = string
  default     = "true"
}

variable "rag_rrf_k" {
  description = "RRF constant k — higher values reduce the weight of top ranks"
  type        = number
  default     = 60
}

variable "query_analyzer_model" {
  description = "Model used for query intent classification"
  type        = string
  default     = "claude-haiku-4-5-20251001"
}

variable "query_analyzer_max_tokens" {
  description = "Max tokens for the query intent classification call"
  type        = number
  default     = 256
}

variable "grounding_strict" {
  description = "Append a warning to answers that cite unverified requirement IDs"
  type        = string
  default     = "false"
}

variable "context_max_chunk_chars" {
  description = "Maximum characters per chunk in structured context output"
  type        = number
  default     = 2000
}

variable "rag_min_score" {
  description = "Minimum retrieval score; chunks below this are discarded before context building"
  type        = string
  default     = "0.0"
}

variable "rag_context_budget_chars" {
  description = "Maximum total characters across all chunks included in the context prompt"
  type        = number
  default     = 8000
}

variable "grounding_validate" {
  description = "Enable batched embedding-based semantic claim support validation"
  type        = string
  default     = "false"
}

variable "grounding_threshold" {
  description = "Minimum cosine similarity for a claim to be considered grounded"
  type        = string
  default     = "0.82"
}

# ── Concurrency & rate limiting ────────────────────────────────────────────

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions for the Lambda function (-1 = unreserved)"
  type        = number
  default     = 50
}

variable "rate_limit_table" {
  description = "DynamoDB table name for per-user rate limit counters"
  type        = string
  default     = "pci-llm-gateway-rate-limit"
}

variable "rate_limit_inference_rpm" {
  description = "Maximum inference requests per user per minute"
  type        = number
  default     = 60
}

variable "rate_limit_rag_rpm" {
  description = "Maximum RAG query requests per user per minute"
  type        = number
  default     = 20
}

variable "rate_limit_agent_rpm" {
  description = "Maximum agent requests per user per minute"
  type        = number
  default     = 30
}

variable "circuit_breaker_failure_threshold" {
  description = "Number of provider failures within the window before the circuit opens"
  type        = number
  default     = 5
}

variable "circuit_breaker_failure_window_secs" {
  description = "Sliding window (seconds) over which failures are counted"
  type        = number
  default     = 60
}

variable "circuit_breaker_recovery_timeout_secs" {
  description = "Seconds the circuit stays OPEN before transitioning to HALF_OPEN"
  type        = number
  default     = 30
}

# ── Observability ──────────────────────────────────────────────────────────

variable "otel_enabled" {
  description = "Enable OpenTelemetry tracing; when false a no-op provider is used"
  type        = bool
  default     = false
}

variable "otel_exporter_otlp_endpoint" {
  description = "OTLP HTTP endpoint for span export (e.g. http://collector:4318)"
  type        = string
  default     = ""
}

variable "otel_service_name" {
  description = "Service name reported in OTEL resource attributes"
  type        = string
  default     = "pci-llm-gateway"
}
