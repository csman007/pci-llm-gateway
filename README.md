# PCI LLM Gateway

[![Tests](https://github.com/csman007/pci-llm-gateway/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/csman007/pci-llm-gateway/actions/workflows/tests.yml) [![Coverage](https://codecov.io/gh/csman007/pci-llm-gateway/branch/master/graph/badge.svg)](https://codecov.io/gh/csman007/pci-llm-gateway) [![Security](https://github.com/csman007/pci-llm-gateway/actions/workflows/security.yml/badge.svg?branch=master)](https://github.com/csman007/pci-llm-gateway/actions/workflows/security.yml) ![Python](https://img.shields.io/badge/python-3.12-blue)

A secure API gateway for routing LLM inference requests with PII detection, redaction, and output filtering to meet PCI DSS compliance requirements. Includes an agentic layer with Claude tool use, multi-agent orchestration, extended thinking, SSE streaming, and LLM-as-judge evaluation.

## Architecture

```
Client → API Gateway → PII Detector → Prompt Processor → LLM Client → Output Filter → Client

Agent layer (POST /v1/agent/run, /v1/agent/stream):
  Question → AgentPipeline (PII check) → AgentOrchestrator
    → tool use loop: query_audit_log | analyze_pii_risk | calculator | call_subagent
      → SubagentRunner (compliance / analyst specialist via Haiku)
    → AgentPipeline (validate + restore)
    → LLMJudge (evaluation score)
    → Client
```

![AWS Architecture](architecture/aws-architecture.png)

## Services

| Service | Description |
|---|---|
| `api-gateway` | FastAPI entry point, auth middleware, request routing |
| `pii-detector` | Regex + ML-based PII/PAN detection |
| `prompt-processor` | Redaction, tokenization, policy enforcement |
| `llm-client` | Anthropic and OpenAI client wrappers |
| `output-filter` | Response validation and leakage detection |
| `agent` | Orchestrator, tools, subagents, evaluator — agentic layer |

## Agent endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/agent/run` | Run the agent to completion; returns result, tool trace, and evaluation score |
| `POST` | `/v1/agent/stream` | Stream agent work as Server-Sent Events |

**Request body:**

```json
{
  "question": "What PCI DSS requirements apply to audit log retention?",
  "thinking": false,
  "max_tokens": 2048
}
```

Set `thinking: true` to enable extended reasoning (automatically selects `claude-opus-4-7`).

**Response (`/v1/agent/run`):**

```json
{
  "response": "PCI DSS Req 10.5.1 requires audit logs to be retained for at least 12 months...",
  "tool_trace": [
    {"tool_name": "call_subagent", "tool_input": {"agent": "compliance", "question": "..."}, "tool_result": "...", "error": false}
  ],
  "thinking": null,
  "evaluation": {"score": 0.92, "reasoning": "Accurate citation with requirement number."},
  "steps_taken": 2
}
```

**SSE events (`/v1/agent/stream`):**

```
data: {"type": "tool_call", "name": "calculator", "input": {"expression": "365 * 24"}}
data: {"type": "tool_result", "name": "calculator", "result": "8760"}
data: {"type": "text_delta", "text": "There are 8760 hours in a year..."}
data: {"type": "evaluation", "score": 0.95, "reasoning": "Correct and concise."}
data: {"type": "done"}
```

**Tools:**

| Tool | Description |
|---|---|
| `query_audit_log` | Query the DynamoDB audit table (filterable by `user_id`) |
| `analyze_pii_risk` | PII risk assessment — returns entity types and HIGH/MEDIUM/NONE risk level |
| `calculator` | Safe arithmetic via AST inspection (no arbitrary eval) |
| `call_subagent` | Delegate to `compliance` (PCI DSS expert) or `analyst` (audit data interpreter) |

**Tuning (env vars / Terraform variables):** all agent constants have sensible defaults and can be overridden without a code change — set them in `.env` locally or in `terraform.tfvars` for Lambda. See `.env.example` for the full list (`AGENT_MODEL_DEFAULT`, `AGENT_MAX_STEPS`, `JUDGE_MODEL`, etc.).

## Running the app

**With Docker (recommended):**

```bash
cp .env.example .env        # fill in API keys, set JWT_SECRET and ENV=dev
docker compose up --build   # first run — builds the image
docker compose up           # subsequent runs
```

The API is available at `http://localhost:8000`. Changes to `services/` and `models.yaml` hot-reload automatically.

**Without Docker:**

```bash
pip install -r requirements.txt
PYTHONPATH=services/api-gateway:services/pii-detector:services/prompt-processor:services/llm-client:services/output-filter \
  uvicorn main:app --reload --app-dir services/api-gateway
```

**Generate a dev JWT** (requires `ENV=dev` in `.env`):

```bash
curl -s -X POST http://localhost:8000/dev/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "dev-user"}'
```

Or import `postman_collection.json` — the **Generate Token** request saves the token to all other requests automatically.

## Running tests

Install dependencies first:

```bash
pip install -r requirements.txt
```

**Run all tests:**

```bash
pytest
```

**Run with coverage report:**

```bash
pytest --cov=services --cov-report=term-missing
```

**Run with HTML coverage** (open `htmlcov/index.html`):

```bash
pytest --cov=services --cov-report=term-missing --cov-report=html
```

**Run a single test:**

```bash
pytest tests/test_redaction.py::test_pan_detected
```

## Deployment

![Cost breakdown](architecture/aws-cost-breakdown.png)

The app runs as a **container image on AWS Lambda** — not a zip file. Lambda's 250 MB zip limit is too small for this dependency set, so the image is stored in ECR and Lambda pulls it on invocation.

```
docker build → push to ECR → Lambda pulls image → API Gateway invokes Lambda
```

`Mangum` (`main.handler`) bridges Lambda's event/context format and FastAPI's ASGI interface. The same codebase runs locally via uvicorn and in production via Lambda — no code changes needed between environments.

**1. Bootstrap remote state** (one-time, before `terraform init`):

```bash
aws s3api create-bucket --bucket pci-llm-gateway-tfstate --region us-east-1
aws s3api put-bucket-encryption --bucket pci-llm-gateway-tfstate \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-versioning --bucket pci-llm-gateway-tfstate \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name pci-llm-gateway-tfstate-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

**2. Configure secrets and provision infrastructure:**

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in your real values
terraform init
terraform apply
```

Terraform creates the Secrets Manager secrets, populates the values, and wires the ARNs into Lambda in one step. `terraform.tfvars` is gitignored — never commit it.

For CI/CD, pass secrets as environment variables instead of a file (Terraform picks up `TF_VAR_*` automatically):

```bash
TF_VAR_anthropic_api_key="sk-ant-..." \
TF_VAR_openai_api_key="sk-..." \
TF_VAR_jwt_secret="..." \
TF_VAR_api_key="..." \
terraform apply
```

**3. Build and push the image:**

```bash
docker build -f infra/docker/Dockerfile -t pci-llm-gateway .

aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <account_id>.dkr.ecr.us-east-1.amazonaws.com

docker tag pci-llm-gateway <ecr_repo_url>:latest
docker push <ecr_repo_url>:latest
```

In a CI/CD pipeline steps 2 and 3 run automatically on every merge to main.

## Secrets management

Sensitive values are never stored as plain Lambda environment variables. Instead:

| Value | Storage | How the app reads it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secrets Manager | ARN passed as `ANTHROPIC_API_KEY_SECRET_ARN` env var, fetched at cold start |
| `OPENAI_API_KEY` | Secrets Manager | ARN passed as `OPENAI_API_KEY_SECRET_ARN` env var, fetched at cold start |
| `JWT_SECRET` | Secrets Manager (prod) / `.env` (dev) | ARN passed as `JWT_SECRET_ARN` in prod; plain `JWT_SECRET` in dev |
| `API_KEY` | Secrets Manager (prod) / `.env` (dev, optional) | ARN passed as `API_KEY_SECRET_ARN` in prod; plain `API_KEY` in dev (unset = check disabled) |
| `LOG_LEVEL`, `ENV`, Cognito IDs | Lambda env vars | Not sensitive — stored as plain config |

`secret_resolver.py` fetches values via `boto3` on first use and caches them with `@lru_cache` so only one Secrets Manager call is made per cold start. Local dev bypasses Secrets Manager entirely — plain env vars from `.env` are used when no `*_SECRET_ARN` var is present.

## Authentication

| Environment | How tokens are issued | How tokens are validated |
|---|---|---|
| `ENV=dev` (local) | `POST /dev/token` — issues HS256 token signed with `JWT_SECRET` | `AuthMiddleware` validates with `JWT_SECRET` |
| Production | `POST /auth/login` — Cognito `USER_PASSWORD_AUTH`; first login triggers `NEW_PASSWORD_REQUIRED`, complete with `POST /auth/change-password` | API Gateway Cognito JWT Authorizer at the edge; `AuthMiddleware` validates via Cognito JWKS |

Every request (except `/health` and `/dev/token`) also requires a valid `x-api-key` header. In production the key is fetched from Secrets Manager; in local dev, leave `API_KEY` unset to disable the check.

In Postman, set the `api_key` collection variable. Use **Generate Token (dev only)** locally or **Login via Cognito (prod)** — both auto-save the token to `jwt_token`.

## Environment Variables

**Local dev** (`.env` file):

| Variable | Description |
|---|---|
| `ENV` | Set to `dev` — enables `/dev/token` and HS256 auth |
| `JWT_SECRET` | HS256 signing secret — `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `LOG_LEVEL` | Logging verbosity (default: `INFO`) |
| `API_KEY` | Optional — set to enforce `x-api-key` validation locally; unset to disable |

**Production** (Lambda env vars — set by Terraform, no secrets in plain text):

| Variable | Description |
|---|---|
| `ENV` | `prod` |
| `LOG_LEVEL` | Logging verbosity |
| `AWS_REGION_NAME` | AWS region (e.g. `us-east-1`) |
| `COGNITO_USER_POOL_ID` | Cognito User Pool ID |
| `COGNITO_CLIENT_ID` | Cognito App Client ID |
| `ANTHROPIC_API_KEY_SECRET_ARN` | Secrets Manager ARN for the Anthropic key |
| `OPENAI_API_KEY_SECRET_ARN` | Secrets Manager ARN for the OpenAI key |
| `JWT_SECRET_ARN` | Secrets Manager ARN for the JWT secret |
| `API_KEY_SECRET_ARN` | Secrets Manager ARN for the API key (`x-api-key` header) |

## PII Detection & Policy

Every prompt is scanned before it reaches the LLM. Detected entities are either **blocked** (request rejected with `400`) or **redacted** (replaced with a placeholder token, restored in the response).

### Detected entity types

| Entity | Method | How it works |
|---|---|---|
| `PAN` | Regex + Luhn | Matches 13–19 digit sequences (with optional spaces/dashes); only flagged if the number passes the Luhn checksum |
| `CVV` | Regex (context) | Matches 3–4 digits preceded by keywords: `cvv`, `cvc`, `security code` |
| `EXPIRY` | Regex (context) | Matches `MM/YY` or `MM/YYYY` preceded by keywords: `expiry`, `exp date`, `valid thru/through/until`; month validated as 01–12 |
| `SSN` | Regex | Matches `123-45-6789` or `123456789` |
| `EMAIL` | Regex | Standard email format |
| `PHONE` | Regex | US phone numbers with optional `+1` prefix |

CVV and EXPIRY require keyword context (e.g. `expiry: 09/26`) to avoid false positives on bare numbers that appear naturally in text.

### Policy rules

| Rule | Behaviour |
|---|---|
| `PAN`, `CVV`, `SSN`, `EXPIRY` detected | Request blocked — `400` |
| `EMAIL`, `PHONE` detected | Redacted before sending to LLM, restored in response |
| More than 5 PII entities in one request | Request blocked — `400` |
| LLM response contains PII not in the original prompt | Response blocked — `502` |

### LLM error handling

| Situation | Status |
|---|---|
| Insufficient credits / quota | `402` with provider-specific message |
| Invalid API key | `401` |
| Rate limited | `429` |
| Other provider error | `502` |

## Example: prompt with PII

### Case 1 — payment card number (blocked)

```bash
curl -s -X POST http://localhost:8000/v1/inference \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "prompt": "Can you summarize this transaction? Card: 4532015112830366, amount: $42.00, merchant: ACME Corp."
  }'
```

```
HTTP/1.1 400 Bad Request

{"detail": "Request blocked: contains restricted PII type(s): PAN"}
```

`4532015112830366` passes the Luhn checksum, so it is classified as a PAN. The request is rejected before it reaches the LLM provider.

---

### Case 2 — email address (redacted and restored)

```bash
curl -s -X POST http://localhost:8000/v1/inference \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "prompt": "Draft a fraud alert for alice@example.com about suspicious activity on her account."
  }'
```

**What the gateway forwards to the LLM:**
```
Draft a fraud alert for [EMAIL_3F9A2C1B] about suspicious activity on her account.
```

**Raw LLM response (before restoration):**
```
Subject: Fraud Alert — Action Required

Dear [EMAIL_3F9A2C1B],

We detected suspicious activity on the account associated with [EMAIL_3F9A2C1B]. ...
```

**What the client receives (`200 OK`):**
```json
{
  "response": "Subject: Fraud Alert — Action Required\n\nDear alice@example.com,\n\nWe detected suspicious activity on the account associated with alice@example.com. ..."
}
```

The placeholder token `[EMAIL_3F9A2C1B]` is generated fresh per request (UUID-keyed). The LLM never sees the real address; the token map is used to restore it in the response before returning to the caller.

---

### Case 3 — PII in the LLM response (output leak blocked)

If the LLM response contains a PAN or SSN that was **not** in the original token map — for example, a hallucinated or cached value — the `LeakageDetector` catches it and the gateway returns `502` rather than exposing the data to the client.

---

## Threat model

### What we mitigated

| Threat | Control |
|---|---|
| Card data (PAN/CVV/SSN/EXPIRY) sent to an LLM provider | Policy engine blocks the request at `400` before the prompt is forwarded |
| PII leakage in LLM responses | `LeakageDetector` re-scans the raw response; any PII not in the original token map triggers a `502` |
| API key theft | Keys live only in Secrets Manager; Lambda env vars hold ARNs, not values; fetched once per cold start with `@lru_cache` |
| Timing-oracle attack on the API key check | `hmac.compare_digest` runs in constant time regardless of where comparison fails |
| Network interception of LLM calls | VPC private subnets; HTTPS-only egress (port 443); Lambda has no public ingress — only API Gateway can invoke it |
| Unauthorized access | Dual-layer auth: `x-api-key` header validated before JWT; Cognito RS256 in production |
| Arbitrary code execution via the calculator tool | AST inspection whitelists numeric literals and arithmetic operators before evaluation — `eval()` is never called on raw user input |
| PII exfiltration through agent tool I/O | All agent inputs, tool results, and final responses pass through the same `PIIDetector → PolicyEngine → Redactor` pipeline as the inference endpoint |
| Sensitive data in Terraform state | State stored in an encrypted, versioned S3 bucket with DynamoDB locking; `terraform.tfvars` is gitignored and never committed |

### Residual / accepted risk

| Risk | Why accepted |
|---|---|
| False negatives on obfuscated PANs | Luhn validation catches syntactically valid PANs; non-standard separators (e.g. Unicode spaces, unusual delimiters) or intentional digit transposition can bypass regex. A WAF or tokenisation layer upstream is the right control for adversarial inputs at that level. |
| CVV/EXPIRY detection requires keyword context | Bare 3-digit numbers appear in essentially all text; flagging without context would produce an unacceptable false-positive rate. The accepted residual is that a prompt that deliberately omits the keyword prefix (`cvv: 123` → `123`) will not be detected. |
| LLM provider data handling | Redaction limits what the provider sees, but their data retention and training policies are outside our control. The token map means real PANs never leave the gateway, but redacted prompts may still be logged by the provider. |
| Multi-turn conversation context | The token map is in-memory per request. A multi-turn setup where the client replays prior assistant messages containing restored PII would re-expose originals to the LLM on the next turn. The gateway is designed for stateless single-turn inference; callers are responsible for not echoing sensitive values back. |
| Lambda cold-start latency spike | Secrets Manager fetches and (optionally) the HuggingFace NER model load on cold start, adding several hundred milliseconds to the first request after inactivity. Provisioned Concurrency eliminates this but was not provisioned by default to keep baseline cost low. |

### Known limitations

- **ML detection latency** — when the HuggingFace NER model is installed and loaded, each scan adds roughly 150–300 ms. The system falls back to regex-only if `transformers` is not installed; the latency tradeoff is the caller's choice.
- **Regex-only for non-US formats** — the PHONE pattern matches US numbers only (`+1` prefix optional). International numbers are not detected. SSN matches the US 9-digit format; other national ID formats are out of scope.
- **Agent streaming on Lambda** — `/v1/agent/stream` yields true SSE locally (uvicorn). Mangum buffers the full response on Lambda, so the client receives all events at once rather than incrementally. True streaming requires a persistent runtime (ECS, Lambda response streaming with a custom runtime).
- **No prompt-injection detection** — the pipeline detects structured PII patterns; it does not detect adversarial instructions embedded in user input (e.g. "ignore previous instructions"). A separate input-classification step would be required to address this.

---

## Compliance

This gateway is designed to satisfy PCI DSS requirements for environments that process payment card data alongside LLM workloads. See `architecture/diagram.png` for the full data flow.

### Encryption at rest

| Resource | Key |
|---|---|
| CloudWatch Logs (API Gateway + Lambda + Firehose) | CMK (`alias/pci-llm-gateway`), annual rotation |
| DynamoDB audit table | CMK (upgraded from AWS-managed) |
| Lambda environment variables | CMK |
| S3 log bucket | SSE-KMS with CMK, bucket key enabled |

### Log retention pipeline

All CloudWatch Logs are continuously forwarded to an encrypted S3 bucket via Kinesis Firehose:

```
CloudWatch Logs (API GW + Lambda)
  → Kinesis Firehose (GZIP, date-partitioned)
  → S3 (SSE-KMS, versioned)
    → Glacier after 90 days
    → Expired after 365 days
```

### Access control layers

| Layer | Mechanism |
|---|---|
| Network | VPC private subnets; HTTPS-only egress (port 443) |
| API consumer | `x-api-key` header validated before JWT; constant-time compare |
| User identity | Cognito JWT (RS256 in prod, HS256 in dev) |
| Payload | PAN / CVV / SSN / EXPIRY blocked; EMAIL / PHONE redacted |
| Output | LLM response scanned for PII leakage before returning |
