# Changelog

## [0.3.1] — 2026-05-02

### Agent layer tuning via environment variables

All hardcoded constants in the agent layer are now configurable via env vars (defaults unchanged):

| Env var | Default | Controls |
|---|---|---|
| `AGENT_MODEL_DEFAULT` | `claude-sonnet-4-6` | Orchestrator model for standard runs |
| `AGENT_MODEL_THINKING` | `claude-opus-4-7` | Orchestrator model when `thinking=true` |
| `AGENT_MAX_STEPS` | `10` | Max tool-use iterations before forced summary |
| `AGENT_THINKING_BUDGET_TOKENS` | `8000` | Extended thinking token budget |
| `SUBAGENT_MODEL` | `claude-haiku-4-5-20251001` | Model for compliance/analyst subagents |
| `SUBAGENT_MAX_TOKENS` | `1024` | Max tokens per subagent response |
| `JUDGE_MODEL` | `claude-haiku-4-5-20251001` | LLM-as-judge model |
| `JUDGE_MAX_TOKENS` | `256` | Max tokens for judge scoring response |
| `AUDIT_LOG_TABLE` | `pci-llm-gateway-audit` | DynamoDB table queried by `query_audit_log` |

All 9 vars are declared in `infra/terraform/variables.tf` and wired into the Lambda `environment.variables` block in `lambda.tf` — override any of them in `terraform.tfvars` without a code change. Local dev defaults are documented in `.env.example`.

---

## [0.3.0] — 2026-05-02

### Agent layer (`services/agent/` + `services/api-gateway/routes/agent.py`)

Two new endpoints behind the existing auth middleware:

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/agent/run` | Runs the agent to completion; returns full result + tool trace + evaluation |
| `POST` | `/v1/agent/stream` | Streams agent work as Server-Sent Events |

**`AgentOrchestrator`** — multi-step Claude tool-use loop (up to 10 iterations). Selects `claude-sonnet-4-6` by default; promotes to `claude-opus-4-7` when `thinking: true` is set in the request. Extended thinking blocks are returned in the `thinking` field.

**Tools available to the agent:**

| Tool | What it does |
|---|---|
| `query_audit_log` | Scans DynamoDB audit table; filterable by `user_id` and `limit` |
| `analyze_pii_risk` | Runs the existing PIIDetector and returns a JSON risk report (HIGH / MEDIUM / NONE) |
| `calculator` | Evaluates arithmetic expressions via AST inspection — no `eval` on arbitrary input |
| `call_subagent` | Delegates to a specialist (`compliance` — PCI DSS expert, or `analyst` — audit log interpreter), each a scoped Haiku call |

**`LLMJudge`** — after every run, a Haiku call scores the final response (0.0–1.0) with one-sentence reasoning. Score returned in `evaluation` field.

**PII safety** — all agent inputs and tool I/O pass through the existing `PIIDetector → PolicyEngine → Redactor` pipeline. The final response passes through `OutputValidator + LeakageDetector` before being returned. PANs/CVVs/SSNs/EXPIRYs in the question return `400`.

**Streaming note** — `/v1/agent/stream` yields true token-by-token SSE events locally (uvicorn). On Lambda/Mangum the response is buffered; true streaming requires a persistent runtime (ECS or Lambda response streaming).

**New files:**
- `services/agent/orchestrator.py` — `AgentOrchestrator`
- `services/agent/agent_pipeline.py` — `AgentPipeline` (re-uses existing PII services, no circular import)
- `services/agent/tools.py` — tool definitions + implementations
- `services/agent/subagents.py` — `SubagentRunner`
- `services/agent/evaluator.py` — `LLMJudge`
- `services/agent/prompts.py` — system prompt constants
- `services/api-gateway/routes/agent.py` — FastAPI router
- `services/api-gateway/schemas/agent_schemas.py` — Pydantic models

**Tests** — 35 new tests in `tests/test_agent.py` covering tools, pipeline error paths, evaluator, subagents, orchestrator loop (including tool call + thinking), and HTTP endpoints. Overall coverage: 83%.

**Design decisions in this release:**

- **AST inspection for the calculator tool** — `eval()` on user-supplied expressions would let prompt injection execute arbitrary Python. The AST visitor whitelists numeric literals, arithmetic operators, and a small set of math functions, and rejects anything else before touching the interpreter. This makes the tool safe to expose through a tool-use loop where the model constructs expressions from user input.
- **Haiku for subagents, Sonnet for the orchestrator** — specialist subagent tasks (PCI DSS lookup, audit log interpretation) are well-scoped and predictable; Haiku is fast and cost-effective for these. The orchestrator handles open-ended multi-step reasoning and tool selection — Sonnet (or Opus with extended thinking) is reserved for that higher-stakes coordination. Mixing models this way roughly halves inference cost per agent run compared to running everything on Sonnet.
- **LLM-as-judge for evaluation** — a ground-truth dataset wasn't feasible for a domain-specific demo. A Haiku judge call gives a useful quality signal (citation accuracy, hallucination detection, relevance) with one inference call per run. The score surfaces obvious failures without requiring labelled data; the judge prompt explicitly instructs it to return a low score rather than guess when it cannot verify a claim.
- **PII pipeline wrapping all agent I/O** — tool inputs, tool results, and the final response all pass through the same `PIIDetector → PolicyEngine → Redactor` pipeline used by the inference endpoint. This prevents a prompt injection in the user's question from causing the agent to exfiltrate card data through a DynamoDB query result or subagent response.

---

## [0.2.0] — 2026-05-01

### Authentication (`services/api-gateway`)

- **`POST /auth/login`** — Cognito `USER_PASSWORD_AUTH` flow; returns access token or `NEW_PASSWORD_REQUIRED` challenge
- **`POST /auth/change-password`** — completes first-login `NEW_PASSWORD_REQUIRED` challenge and returns a valid token
- **`x-api-key` header** required on all requests except `/health` and `/dev/token`; validated via HMAC constant-time compare against a key stored in Secrets Manager (`pci-llm-gateway/api-key`); enforcement is opt-in in local dev (set `API_KEY=` in `.env`)
- `secrets.py` renamed to `secret_resolver.py` to avoid shadowing Python's stdlib `secrets` module

### Infrastructure (`infra/terraform`)

**KMS CMK (`alias/pci-llm-gateway`)** — new `kms.tf`

| Resource | Change |
|---|---|
| API Gateway CloudWatch log group | Encrypted with CMK |
| Lambda CloudWatch log group (pre-created) | Encrypted with CMK |
| DynamoDB audit table | SSE upgraded from AWS-managed key → CMK |
| Lambda function | `kms_key_arn` — environment variables encrypted with CMK |
| Lambda IAM role | `kms:GenerateDataKey` + `kms:Decrypt` on CMK |

Annual key rotation is enabled. Deletion window is 14 days.

**S3 encrypted log bucket** — new `s3.tf`

| Setting | Value |
|---|---|
| Encryption | SSE-KMS (CMK) with bucket key enabled |
| Versioning | Enabled |
| Public access | Fully blocked |
| Bucket policy | TLS-only (`aws:SecureTransport` = true required) |
| Lifecycle | 90 days → Glacier; 365 days → expire; 30 days for non-current versions |
| Access logging | Self-referencing with `s3-access-logs/` prefix |

**CloudWatch Logs → S3 pipeline** — new `firehose.tf`

- Kinesis Firehose delivery stream (`pci-llm-gateway-logs`): buffers 5 MB / 60 s, writes GZIP-compressed, date-partitioned objects to the log bucket
- CW Logs subscription filters on both log groups (API Gateway + Lambda) forward all events to Firehose
- Dedicated IAM roles with least-privilege policies for Firehose and the CW-to-Firehose trust relationship
- Firehose delivery errors logged to `/aws/kinesisfirehose/pci-llm-gateway` (CMK-encrypted)

**API key secret** — `pci-llm-gateway/api-key` added to Secrets Manager, ARN passed to Lambda as `API_KEY_SECRET_ARN`

**Design decisions in this release:**

- **HMAC constant-time compare for the API key** — a naive string comparison leaks timing information about how many characters match, enabling a character-by-character brute-force with enough samples. `hmac.compare_digest` runs in constant time regardless of where the first mismatch occurs.
- **`secret_resolver.py` naming** — originally named `secrets.py`; renamed to avoid shadowing Python's stdlib `secrets` module, which is imported internally for generating keys in test helpers and token issuance.
- **CMK over AWS-managed keys** — AWS-managed keys are per-service and rotated on AWS's schedule; a CMK gives explicit control over key policy, rotation schedule, and the ability to disable or delete the key under incident response procedures — required by PCI DSS for environments processing cardholder data.

---

## [0.1.0] — 2026-04-29

Initial release of the PCI LLM Gateway.

**Design decisions:**

- **Luhn validation on PANs** — pure regex would flag any 16-digit sequence (timestamps, product codes, phone numbers formatted without separators). Luhn reduces false positives to numbers that could plausibly be real card numbers, making the detector usable in practice without drowning operators in noise.
- **Keyword context for CVV and EXPIRY** — 3–4 digit numbers appear in essentially all text; `cvv: 123` matches but bare `123` does not. The tradeoff is a false negative if an attacker deliberately omits the keyword prefix — accepted because flagging all date-like or short numeric patterns produces an unusable false-positive rate.
- **Block vs. redact distinction** — PAN, CVV, SSN, and EXPIRY are rejected outright rather than redacted. Redaction is safe only when the original value is needed purely for context (email addresses, phone numbers). A PAN that survives to the LLM and is echoed back verbatim is still exposed to the provider — the only safe option is rejection at the gateway.
- **Container image over Lambda zip** — the HuggingFace NER model, boto3, FastAPI, and their transitive dependencies exceed Lambda's 250 MB zip limit. Container images have no equivalent size limit at invocation time.
- **`Finding` dataclass in `patterns.py` not `detector.py`** — both `detector.py` and `ml_model.py` return `Finding` objects; placing the dataclass in `detector.py` created a circular import when `ml_model.py` needed to import it. Moving it to the shared `patterns.py` module breaks the cycle without introducing a separate utility package.
- **Flat-namespace service directories (no `__init__.py`)** — each service directory is added directly to `sys.path`. This keeps imports simple (`from detector import PIIDetector`) across a multi-service layout where the services will eventually run as separate Lambda functions with no shared package hierarchy.

---

### API Gateway (`services/api-gateway`)

**Endpoints**

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Liveness check |
| `GET` | `/v1/models` | None | List supported models from `models.yaml` |
| `POST` | `/v1/inference` | Bearer JWT | Run a prompt through the full PII pipeline and return an LLM response |
| `POST` | `/dev/token` | None | Issue a HS256 JWT for local testing (`ENV=dev` only; returns `404` otherwise) |

**Inference pipeline** — every `POST /v1/inference` request passes through these steps in order:

1. `PIIDetector.scan()` — regex + optional ML scan of the prompt
2. `PolicyEngine.enforce()` — block or allow based on entity types found
3. `Redactor.redact()` — replace sensitive entities with placeholder tokens
4. `get_client(model).complete()` — forward redacted prompt to the LLM
5. `OutputValidator.is_valid()` — validate response length and content
6. `LeakageDetector.detected()` — scan LLM response for PII not in the original token map
7. `Redactor.restore()` — swap placeholder tokens back to originals

**Middleware**

- `AuthMiddleware` — validates JWT on every request except `/health`, `/dev/token`, `/v1/models`
  - `ENV=dev`: HS256 validation against `JWT_SECRET`
  - Production: RS256 validation via Cognito JWKS (fetched once, cached)
- `LoggingMiddleware` — structured request/response logging with `X-Request-ID` header

---

### PII Detection (`services/pii-detector`)

| Entity | Method | Detection logic |
|---|---|---|
| `PAN` | Regex + Luhn | 13–19 digit sequence that passes the Luhn checksum |
| `CVV` | Regex (context) | 3–4 digits preceded by `cvv`, `cvc`, or `security code` |
| `EXPIRY` | Regex (context) | `MM/YY` or `MM/YYYY` preceded by `expiry`, `exp date`, `valid thru/through/until`; month validated 01–12 |
| `SSN` | Regex | `123-45-6789` or `123456789` |
| `EMAIL` | Regex | Standard email format |
| `PHONE` | Regex | US phone with optional `+1` prefix |

CVV and EXPIRY require keyword context — bare digits are not flagged to avoid false positives.

The `Finding` dataclass is defined in `patterns.py` (not `detector.py`) to avoid a circular import with `ml_model.py`, which also returns `Finding` objects.

The HuggingFace NER model (`ml_model.py`) is loaded lazily. If `transformers` is not installed, the detector falls back to regex-only without error.

---

### Policy Engine (`services/prompt-processor/policy_engine.py`)

| Rule | Result |
|---|---|
| `PAN`, `CVV`, `SSN`, or `EXPIRY` detected | `400` — request blocked |
| More than 5 PII entities in one prompt | `400` — request blocked |
| `EMAIL` or `PHONE` detected | Redacted before LLM, restored in response |
| LLM response contains PII not in the token map | `502` — response blocked |

---

### LLM Clients (`services/llm-client`)

Supported providers and routing are driven by `models.yaml` — no code changes required to add a model. To add a new provider, create a client file and add an entry to `model_registry.py`.

**Error handling**

| Situation | Status | Detail |
|---|---|---|
| Insufficient credits / quota | `402` | Provider-specific message with billing URL |
| Invalid API key | `401` | Provider-specific message |
| Rate limited | `429` | Retry message |
| Other provider error | `502` | Provider name + status code |

---

### Model Registry (`models.yaml` + `services/api-gateway/model_registry.py`)

Models are configured in `models.yaml` at the project root. The registry is the single source of truth for:
- Which models are accepted (request validation)
- Which provider client to use (routing)
- What `/v1/models` returns

**Adding a model** — edit `models.yaml`:
```yaml
- name: new-model-name
  provider: existing-provider-prefix
```

**Adding a provider** — create a client in `services/llm-client/`, add it to `_PROVIDER_CLIENTS` in `model_registry.py`, then add models to `models.yaml`.

---

### Authentication

| Environment | Token source | Validation |
|---|---|---|
| `ENV=dev` | `POST /dev/token` (HS256, signed with `JWT_SECRET`) | `AuthMiddleware` validates with `JWT_SECRET` |
| Production | AWS Cognito `USER_PASSWORD_AUTH` flow | API Gateway Cognito JWT Authorizer at the edge + `AuthMiddleware` via JWKS |

Cognito infrastructure is provisioned in `infra/terraform/cognito.tf`. The API Gateway JWT Authorizer (`infra/terraform/api_gateway.tf`) rejects unauthenticated requests before Lambda is invoked.

---

### Infrastructure (`infra/`)

| File | Provisions |
|---|---|
| `terraform/vpc.tf` | VPC, private subnets, security group |
| `terraform/api_gateway.tf` | HTTP API Gateway, Cognito JWT Authorizer, CloudWatch log group |
| `terraform/cognito.tf` | User Pool (MFA optional, strong password policy), App Client |
| `terraform/lambda.tf` | Lambda function (container image), ECR repository |
| `terraform/dynamodb.tf` | Audit log table (PITR enabled, SSE enabled, TTL) |
| `terraform/iam.tf` | Lambda execution role, DynamoDB + Secrets Manager permissions |
| `docker/Dockerfile` | Production image (AWS Lambda Python 3.12 base) |
| `docker/Dockerfile.dev` | Local dev image (standard Python 3.12, uvicorn with hot-reload) |

**Deployment model — container image on Lambda**

Lambda runs the app as a container image (not a zip) because the dependency footprint exceeds Lambda's 250 MB zip limit. The flow is:

```
docker build → push to ECR → Lambda pulls image → API Gateway invokes Lambda
```

`Mangum` (`main.handler`) acts as the bridge between Lambda's event/context format and FastAPI's ASGI interface. The same codebase runs locally via uvicorn and in production via Lambda with no code changes.

**Secrets management**

Sensitive values (API keys, JWT secret) are stored in Secrets Manager — never as plain Lambda environment variables. Terraform provisions the secret shells; values are populated separately via the AWS CLI. Lambda env vars carry only the Secrets Manager ARNs. `services/api-gateway/secrets.py` fetches the actual values via `boto3` on cold start and caches them with `@lru_cache`. Local dev bypasses Secrets Manager entirely — plain env vars from `.env` are used when no `*_SECRET_ARN` var is present.

| Value | Storage | Lambda env var |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secrets Manager | `ANTHROPIC_API_KEY_SECRET_ARN` |
| `OPENAI_API_KEY` | Secrets Manager | `OPENAI_API_KEY_SECRET_ARN` |
| `JWT_SECRET` | Secrets Manager (prod) / `.env` (dev) | `JWT_SECRET_ARN` |
| `LOG_LEVEL`, `ENV`, Cognito IDs | Lambda env vars (plain) | direct |

**VPC network controls**

- Private subnets across a minimum of 2 AZs
- Security group: no public ingress (Lambda is invoked internally by API Gateway), HTTPS-only egress (port 443) for LLM provider calls
- DynamoDB: Gateway VPC endpoint — traffic stays on AWS backbone, free
- Secrets Manager: PrivateLink (Interface endpoint) — private IP addresses across all AZs

**Remote state** (`infra/terraform/backend.tf`)

Terraform state is stored in an encrypted, versioned S3 bucket with a DynamoDB lock table — required because the state file contains sensitive variable values. The bucket and table are bootstrapped once manually before `terraform init`. State is never stored locally or committed to git.

**tfvars-based secret injection**

Secrets are passed to Terraform via `terraform.tfvars` (gitignored) or `TF_VAR_*` environment variables in CI/CD. Terraform creates the Secrets Manager secrets and populates the values in a single `terraform apply` — no manual CLI steps needed after initial setup. Variables are declared `sensitive = true` so values are redacted from plan/apply output.

Deploy steps:

```bash
# 1. Bootstrap remote state (one-time)
aws s3api create-bucket --bucket pci-llm-gateway-tfstate --region us-east-1
aws s3api put-bucket-encryption --bucket pci-llm-gateway-tfstate \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-versioning --bucket pci-llm-gateway-tfstate \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name pci-llm-gateway-tfstate-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

# 2. Provision (local)
cp terraform.tfvars.example terraform.tfvars   # fill in values
terraform init && terraform apply

# 2. Provision (CI/CD)
TF_VAR_anthropic_api_key="..." TF_VAR_openai_api_key="..." TF_VAR_jwt_secret="..." terraform apply

# 3. Build and push image
docker build -f infra/docker/Dockerfile -t pci-llm-gateway .
docker tag pci-llm-gateway <ecr_repo_url>:latest && docker push <ecr_repo_url>:latest
```

---

### Tests (`tests/`)

| File | Covers |
|---|---|
| `test_redaction.py` | PAN (valid + invalid Luhn), SSN, EMAIL, CVV (with/without keyword), EXPIRY (with/without keyword, invalid month), redact/restore round-trip, multiple entities |
| `test_leakage.py` | Clean response, PAN leakage, allowed token map values, email leakage |
| `test_end_to_end.py` | Health check, model list, PAN blocked, EXPIRY blocked, unsupported model `422`, safe prompt reaches LLM (mocked), missing token `401` |

`tests/conftest.py` adds all service directories to `sys.path` and sets default env vars (`ENV=dev`, `JWT_SECRET`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) so individual test files stay clean.

Run all tests:
```bash
pytest
```

Run a single test:
```bash
pytest tests/test_redaction.py::test_expiry_detected
```

---

### Local Development

```bash
cp .env.example .env      # fill in API keys
docker compose up --build  # first run
docker compose up          # subsequent runs (models.yaml changes hot-reload)
```

Generate a dev JWT:
```bash
curl -s -X POST http://localhost:8000/dev/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "dev-user"}'
```

Or use `postman_collection.json` — **Generate Token (dev only)** auto-saves the token to all requests.
