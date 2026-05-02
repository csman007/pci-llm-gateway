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
