# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Rules

These apply to every change, no exceptions:

1. **API change → update tests.** Any change to an endpoint (schema, status codes, new route) must include updated or new tests.
2. **Test change → run it.** After modifying any test file, run it before finishing.
3. **Coverage floor: 80%.** Run `pytest --cov=services --cov-report=term-missing` and confirm overall coverage stays ≥ 80%.
4. **Every change updates three documents:** `CHANGELOG.md`, `README.md` (if user-facing behaviour changed), and `CLAUDE.md` (if architecture/commands/conventions changed).
5. **Terraform change → run tflint + tfsec, fix all findings.** Run both tools after creating or modifying any `.tf` file.
   ```bash
   tflint --chdir=infra/terraform
   tfsec infra/terraform
   ```
   Ignore annotations must be **inline on the exact flagged line** — preceding comment blocks are not picked up:
   ```
   cidr_blocks = ["0.0.0.0/0"] #tfsec:ignore:aws-ec2-no-public-egress-sgr -- reason here
   ```
   **Established intentional ignores** (do not re-open these):
   - `aws-ec2-no-public-egress-sgr` — SG egress to LLM providers; IPs not stable, port 443 only.
   - `aws-iam-no-policy-wildcards` — flow logs IAM path prefix `vpc-flow-logs/*`; not a permission wildcard.
   - `aws-s3-enable-logging` — the log bucket IS the destination; a second bucket is not required by PCI DSS.
7. **No hardcoded tuneable values — use env vars, wired through Terraform.** Model names, token limits, step counts, timeouts, and table names must be read from `os.environ.get("VAR", default)`. Because the app runs on Lambda, every env var the code reads must also be declared in `variables.tf` and set in the Lambda `environment.variables` block in `lambda.tf`. `.env.example` documents defaults for local dev only.
   ```python
   # Python
   _JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5-20251001")
   _JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "256"))
   ```
   ```hcl
   # lambda.tf
   JUDGE_MODEL      = var.judge_model
   JUDGE_MAX_TOKENS = tostring(var.judge_max_tokens)
   ```
   Cast numeric values in Python (`int()`/`float()`); use `tostring()` in Terraform. Non-sensitive tuning vars go as plain Lambda env vars, not Secrets Manager.

6. **Service code → inline documentation on every class and public method.** Add to any Python code written or modified:
   - One-line docstring on every class (its responsibility).
   - Docstring on every public method with `Args:` and `Returns:` blocks when names alone aren't self-explanatory.
   - Type annotations on all parameters and return types.
   ```python
   class PIIDetector:
       """Scans text for PII entities using regex patterns and an optional ML model."""

       def scan(self, text: str) -> list[Finding]:
           """Return all PII findings in *text*.

           Args:
               text: Raw prompt or response to scan.
           Returns:
               List of Finding objects, empty if no PII detected.
           """
   ```

## Commands

**Install dependencies**
```bash
pip install -r requirements.txt
```

**Run the API server** (from project root, no Docker)
```bash
PYTHONPATH=services/api-gateway:services/pii-detector:services/prompt-processor:services/llm-client:services/output-filter:services/agent \
  uvicorn main:app --reload --app-dir services/api-gateway
```

**Run with Docker (recommended)**
```bash
docker compose up --build   # first run
docker compose up           # subsequent runs
```

**Run all tests**
```bash
pytest
```

**Run with coverage**
```bash
pytest --cov=services --cov-report=term-missing
```

**Run a single test**
```bash
pytest tests/test_redaction.py::test_pan_detected
```

**Load test**
```bash
locust -f scripts/load_test.py --host http://localhost:8000
```

## Agent layer

`services/agent/` is a flat-namespace service (no `__init__.py`, added to `sys.path` like all other services). It provides two endpoints via `services/api-gateway/routes/agent.py`:

- `POST /v1/agent/run` — runs the tool-use loop to completion, returns `AgentRunResponse`
- `POST /v1/agent/stream` — same loop, yields SSE events

**Key classes:**

| Class | File | Responsibility |
|---|---|---|
| `AgentOrchestrator` | `orchestrator.py` | Multi-step tool-use loop (max 10 steps); selects Sonnet by default, Opus when `thinking=True` |
| `AgentPipeline` | `agent_pipeline.py` | Re-instantiates all PII pipeline collaborators — no import from the route layer |
| `SubagentRunner` | `subagents.py` | Runs `compliance` / `analyst` specialist calls via Haiku; inputs/outputs pass through `AgentPipeline` |
| `LLMJudge` | `evaluator.py` | Post-run evaluation; single Haiku call returning `{"score": float, "reasoning": str}` |

**Tools** (`tools.py`): `query_audit_log` (DynamoDB scan), `analyze_pii_risk` (PIIDetector report), `calculator` (AST-safe eval), `call_subagent` (delegates to SubagentRunner).

**Import rule**: `services/agent/` imports from other service directories (`detector`, `redactor`, etc.) but never from `services/api-gateway/`. The route file (`routes/agent.py`) is the only place that imports from `services/agent/`.

**Streaming on Lambda**: `/v1/agent/stream` works locally (uvicorn). Mangum buffers the response on Lambda — true streaming requires a persistent runtime.

## Inference architecture

Every inference request passes through a fixed pipeline in `services/api-gateway/routes/inference.py`:

```
Client → Auth → PIIDetector.scan() → PolicyEngine.enforce() → Redactor.redact()
       → get_client(model).complete() → OutputValidator.is_valid()
       → LeakageDetector.detected() → Redactor.restore() → Client
```

**Key design decisions:**

- `PolicyEngine` **blocks** requests containing PAN, CVV, SSN, or EXPIRY (returns 400). EMAIL and PHONE are redacted and allowed through.
- `Redactor` replaces each finding with a UUID-keyed placeholder token (e.g. `[EMAIL_A1B2C3D4]`). The token→original map lives in memory for the request lifetime and is used to restore the LLM response before returning it.
- `LeakageDetector` re-scans the raw LLM response before restoration and blocks it if PII is found that isn't in the token map.
- `PIIDetector` combines regex patterns (with Luhn validation for PANs) and an optional HuggingFace NER model. Falls back to regex-only if `transformers` is not installed.
- CVV and EXPIRY patterns require keyword context (`cvv: 123`, `expiry: 09/26`) — bare digits are not flagged.

## Path / import model

Each service directory (`services/pii-detector`, `services/prompt-processor`, etc.) is a **flat namespace** added directly to `sys.path` — no `__init__.py` files. Imports are bare module names (`from detector import PIIDetector`, not `from services.pii_detector.detector import ...`).

`tests/conftest.py` adds all service directories to `sys.path` and sets default env vars at test time. `pyproject.toml` declares the same paths under `[tool.pytest.ini_options] pythonpath`. In PyCharm, mark each service directory as a **Sources Root** for IDE resolution.

The shared `Finding` dataclass lives in `services/pii-detector/patterns.py` to avoid a circular import between `detector.py` and `ml_model.py`.

## Model registry

Supported models are configured in `models.yaml` at the project root — the single source of truth for request validation, routing, and the `/v1/models` endpoint. To add a model, edit `models.yaml`. To add a provider, create a client in `services/llm-client/` and add it to `_PROVIDER_CLIENTS` in `services/api-gateway/model_registry.py`.

## Secrets

`services/api-gateway/secret_resolver.py` resolves sensitive values (named to avoid shadowing Python's stdlib `secrets` module):
- **Local dev** — reads plain env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `JWT_SECRET`) from `.env`
- **Production** — reads `*_SECRET_ARN` env vars and fetches actual values from Secrets Manager via boto3, cached with `@lru_cache` per cold start

Never add secrets directly to Lambda environment variables.

## Authentication

Every request passes two checks in `AuthMiddleware` before JWT validation:

1. **API key** — `x-api-key` header validated via `hmac.compare_digest` against `API_KEY` (dev) or Secrets Manager `pci-llm-gateway/api-key` (prod). Disabled when neither `API_KEY_SECRET_ARN` nor `API_KEY` env var is set (local dev default). Exempt paths: `/health`, `/dev/token`.
2. **JWT** — `ENV=dev`: HS256 signed with `JWT_SECRET`, issued by `/dev/token`. Production: RS256 validated via Cognito JWKS. Exempt paths: `/health`, `/dev/token`, `/v1/models`, `/auth/login`, `/auth/change-password`.

Production login flow: `POST /auth/login` (Cognito `USER_PASSWORD_AUTH`) → if `NEW_PASSWORD_REQUIRED` challenge returned, complete with `POST /auth/change-password`.

## Infrastructure

- Terraform state is stored remotely in S3 (encrypted, versioned) with DynamoDB locking — see `infra/terraform/backend.tf`.
- Secrets are injected via `terraform.tfvars` (gitignored) or `TF_VAR_*` env vars in CI/CD.
- The app runs as a **container image on Lambda** (not zip). `Mangum` (`main.handler`) bridges Lambda's event/context and FastAPI's ASGI interface.
- VPC: private subnets across minimum 2 AZs, no public ingress, HTTPS-only egress. DynamoDB uses a Gateway VPC endpoint; Secrets Manager uses PrivateLink (Interface endpoint).
- KMS CMK (`alias/pci-llm-gateway`, `kms.tf`): encrypts CloudWatch Logs, DynamoDB, Lambda env vars. Annual rotation enabled.
- S3 log bucket (`s3.tf`): SSE-KMS, versioned, TLS-only. Lifecycle: 90d → Glacier, 365d → expire.
- Log pipeline (`firehose.tf`): CloudWatch Logs (API GW + Lambda) → Kinesis Firehose → S3 (GZIP, date-partitioned). All three CMK-encrypted.

## LLM error handling

LLM client errors are caught and mapped to HTTP status codes: `402` for insufficient credits/quota, `401` for bad API key, `429` for rate limit, `502` for other provider errors.
