# Changelog

## [0.9.0] — 2026-05-14

### Security: Prompt injection detection layer

Five-category regex-based injection detector applied at every untrusted input surface.

#### New module — `services/prompt-processor/injection_detector.py`

`InjectionDetector` scans text for instruction-hijacking, persona attacks, system-prompt extraction, template-delimiter injection, and indirect injection markers.  Pattern severity is either `"block"` (reject/drop) or `"warn"` (log and pass through).

Set `INJECTION_BLOCK_ACTION=log` to enter monitor-only mode — all findings are logged but nothing is blocked.  Useful when first deploying to measure false-positive rate before enforcing.

#### New module — `services/agent/tool_validator.py`

`validate_tool_name()` enforces a static allowlist (`query_audit_log`, `analyze_pii_risk`, `calculator`, `search_pci_dss`, `call_subagent`) before any tool is dispatched.  `scan_tool_result()` scans each tool result for injection patterns and replaces poisoned results with a safe sentinel string so they never reach the model's context window.

#### Integration points

| Surface | Action on BLOCK finding |
|---|---|
| User prompt (`/v1/inference`) | HTTP 400 |
| RAG question (`/v1/rag/query`) | HTTP 400 |
| Retrieved RAG chunks (indirect injection) | Chunk silently dropped |
| Agent tool results | Result replaced with error sentinel |

#### Tests

21 new tests in `tests/test_injection.py` covering all pattern categories, `is_blocked()` behaviour, monitor mode, tool allowlist enforcement, and all four integration surfaces.

---

## [0.8.0] — 2026-05-14

### Multi-tenant isolation

Four-layer tenant model that scopes every request to an isolated tenant context derived from the JWT `tenant_id` claim.

#### Tenant model (`services/api-gateway/tenant.py`)

`TenantConfig` is a dataclass loaded from DynamoDB on first request and cached in Lambda memory for `TENANT_CACHE_TTL_SECS` (default 60 s). Config is keyed by `tenant#{tenant_id}` in the tenants table.

| Field | Type | Meaning |
|---|---|---|
| `allowed_models` | `list[str] \| None` | Whitelist of model IDs; `None` = all allowed |
| `blocked_entity_types` | `list[str]` | Extra PII types blocked beyond global policy |
| `monthly_budget_usd` | `float \| None` | Hard monthly spend cap in USD; `None` = unlimited |
| `rate_limit_inference_rpm` | `int \| None` | Per-user inference cap override; `None` = global default |
| `rate_limit_rag_rpm` | `int \| None` | Per-user RAG cap override |
| `rate_limit_agent_rpm` | `int \| None` | Per-user agent cap override |

`get_tenant_config()` fails open on DynamoDB errors — a table outage returns a permissive default rather than blocking traffic.

#### Isolation boundaries

Every request is scoped at four layers:

1. **JWT claim** — `tenant_id` embedded in the token (dev: explicit field; Cognito prod: `custom:tenant_id` attribute). Defaults to `"default"` when absent for backwards compatibility.
2. **Rate limit counters** — key format changed from `{user_id}#{endpoint}#{bucket}` to `{tenant_id}#{user_id}#{endpoint}#{bucket}`. Tenants never share counters.
3. **Spend counters** — per-tenant monthly spend item: `spend#{tenant_id}#{YYYY-MM}` with atomic `ADD`. Tenants cannot consume each other's budget.
4. **Audit log** — `tenant_id` included in every `inference_complete` and `rag_complete` structured log entry.

#### Per-tenant policy engine

Applied in inference and RAG routes after global PII policy:

- **Model allow-list** — `422` if the requested model is not in `allowed_models`.
- **Additional blocked entity types** — `400` if any detected PII entity type is in `blocked_entity_types` (tenant PHONE block, for example).

The agent route checks the orchestrator's resolved model (`AGENT_MODEL_DEFAULT` or `AGENT_MODEL_THINKING`) against the allow-list before starting the tool-use loop.

#### Per-tenant quotas and billing (`services/api-gateway/tenant_quota.py`)

- `check_budget(tenant_id, monthly_budget_usd)` — reads current month's spend counter and raises `429` with `X-Tenant-Budget-Limit` / `X-Tenant-Budget-Remaining` headers when `spend >= budget`. Called before the LLM call.
- `record_spend(tenant_id, cost_usd)` — atomically increments the spend counter after a successful LLM call. Uses DynamoDB `ADD` to handle concurrent Lambda instances. Silently swallows errors (fail open).

Spend items share the tenants table under the `spend#` prefix with a 60-day TTL.

#### Auth middleware and dev token

`_decode_dev()` and `_decode_cognito()` now return `{"sub": ..., "tenant_id": ...}` dicts instead of bare strings, fixing the long-standing bug where `request.state.user` was a string and rate limiting always fell back to `"anonymous"`.

`POST /dev/token` accepts an optional `tenant_id` field (default `"default"`).

#### Infrastructure

New `aws_dynamodb_table.tenants` (KMS, PITR, TTL). Lambda IAM: `dynamodb:GetItem` + `dynamodb:UpdateItem` on tenants table. Two new Terraform variables: `tenants_table`, `tenant_cache_ttl_secs`.

### Tests

`tests/test_tenant.py` — 13 tests: config loading, TTL cache, DynamoDB fail-open, budget check (pass/fail/error), spend recording, contract tests for model allow-list and entity-type policy enforcement.

---

## [0.7.0] — 2026-05-14

### Concurrency & scaling

#### Per-user rate limiting (`services/api-gateway/rate_limiter.py`)

DynamoDB-backed fixed-window rate limiter applied to every authenticated endpoint as a FastAPI dependency. Each `user_id` (JWT `sub`) gets its own counter per endpoint per 60-second window. On limit breach the caller receives:

```
HTTP 429 Too Many Requests
Retry-After: <seconds>
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: <unix timestamp>
```

| Endpoint | Default limit | Env var |
|---|---|---|
| `POST /v1/inference` | 60 req/min | `RATE_LIMIT_INFERENCE_RPM` |
| `POST /v1/rag/query` | 20 req/min | `RATE_LIMIT_RAG_RPM` |
| `POST /v1/agent/run` + `/stream` | 30 req/min | `RATE_LIMIT_AGENT_RPM` |

Disabled locally when `RATE_LIMIT_TABLE` is unset. DynamoDB errors fail-open — a table outage never blocks requests.

**DynamoDB counter schema**: `pk = {user_id}#{endpoint}#{minute_bucket}`. The `ttl` attribute auto-expires old buckets. Atomic `UpdateItem` with `ADD count 1` prevents race conditions across Lambda instances.

#### Per-provider circuit breakers (`services/llm-client/circuit_breaker.py`)

In-process circuit breaker per LLM provider (Anthropic, OpenAI). Protects against cascading failures when a provider is degraded:

| State | Trigger | Behaviour |
|---|---|---|
| `CLOSED` | default | All calls go through |
| `OPEN` | ≥ N provider failures in window | Calls fail fast with `503` + `Retry-After` |
| `HALF_OPEN` | After recovery timeout | One probe call; success → CLOSED, failure → OPEN |

Default thresholds (all overridable via env vars):

| Env var | Default | Meaning |
|---|---|---|
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures needed to open the circuit |
| `CIRCUIT_BREAKER_FAILURE_WINDOW_SECS` | `60` | Sliding window over which failures are counted |
| `CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECS` | `30` | Seconds in OPEN before transitioning to HALF_OPEN |

Only **provider-level** failures (`429`, `5xx` from the LLM API) trip the circuit. Client errors (`401` bad key, `400` bad request) pass through without affecting circuit state.

#### Lambda reserved concurrency (`infra/terraform/lambda.tf`)

`reserved_concurrent_executions` is now wired from `var.lambda_reserved_concurrency` (default: `50`). This sets a hard cap on parallel Lambda invocations and acts as the primary **backpressure** mechanism:

- At the cap, AWS Lambda throttles new invocations with `429 TooManyRequestsException`.
- API Gateway translates this to an HTTP `429` to callers.
- **Throughput formula**: `reserved_concurrency / avg_latency_secs`. With 50 concurrency at 2.5 s avg: ~20 req/s peak.

#### New DynamoDB table

`aws_dynamodb_table.rate_limit` (name from `var.rate_limit_table`, default `pci-llm-gateway-rate-limit`): PAY_PER_REQUEST billing, KMS encryption, TTL, PITR enabled. IAM policy grants `dynamodb:UpdateItem` to Lambda.

### Tests

- `tests/test_circuit_breaker.py` — 11 tests covering all state transitions: CLOSED→OPEN→HALF_OPEN→CLOSED, failure window expiry, non-trip exceptions, registry isolation.
- `tests/test_rate_limiter.py` — 8 tests: under/at/over limit, per-user and per-endpoint isolation, DynamoDB fail-open, no-table noop, bucket key format.

---

## [0.6.0] — 2026-05-14

### Observability — OpenTelemetry tracing, structured logging, and cost tracking

New `services/observability/` layer providing production-grade observability as a cross-cutting concern that wraps each pipeline without changing business logic.

**OpenTelemetry tracing (`services/observability/tracer.py`)**

- `setup_tracing()` installs an `OTLPSpanExporter` (HTTP/protobuf) when `OTEL_ENABLED=true`; falls back to a `NoOpTracerProvider` by default — zero overhead when disabled.
- All OTEL imports are guarded with `try/except ImportError` so the app starts without the packages installed.
- `get_tracer(name)` — returns a named tracer scoped to the service name.
- `current_trace_id()` / `current_span_id()` — returns the hex trace/span ID of the active span, or `""` when no span is active.

**Structured JSON logging (`services/observability/structured_logger.py`)**

- `_JsonFormatter` replaces the default `logging.Formatter` and emits one JSON line per record:
  ```json
  {"timestamp":"2026-05-14T12:00:00.000Z","level":"INFO","logger":"inference","message":"inference_complete","trace_id":"abc123","span_id":"def456","latency_ms":142,"total_cost_usd":0.0028}
  ```
- `configure_logging(level)` replaces the root logger's handlers — called once at app startup in `main.py`.
- `@contextmanager stage_span(logger, stage, **attrs)` — times a named pipeline stage and emits a structured log entry on exit with `latency_ms`.
- `_TraceFilter` (in `middleware/logging.py`) injects `trace_id`/`span_id` from the active OTEL span into every log record.

**Token and cost tracking (`services/observability/token_counter.py`)**

- Pricing table ($/M tokens) for 7 models (Opus 4.7, Sonnet 4.6, Haiku 4.5, all Sonnet/Haiku variants). All prices overridable via env vars.
- `calculate_cost(model, prompt_tokens, completion_tokens)` returns a dict with `prompt_tokens`, `completion_tokens`, `total_tokens`, `input_cost_usd`, `output_cost_usd`, `total_cost_usd`.

**`LLMResponse` dataclass (`services/llm-client/llm_response.py`)**

Unified return type from `complete()` in both LLM clients:
```python
@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
```

`AnthropicClient.complete()` and `OpenAIClient.complete()` now return `LLMResponse` instead of `str`.

**Inference pipeline instrumentation (`services/api-gateway/routes/inference.py`)**

All seven pipeline stages wrapped in `stage_span`:
`pii_detect → policy_enforce → redact → llm_complete → output_validate → leakage_detect → restore`

A summary `inference_complete` log is emitted at the end of each request with latency, token counts, and cost. The entire request is enclosed in an OTEL span via `get_tracer("inference")`.

**RAG pipeline instrumentation (`services/api-gateway/routes/rag.py`)**

RAG pipeline call wrapped in `stage_span`; `llm_usage` (tokens + cost) logged per query.

### New environment variables

| Env var | Default | Controls |
|---|---|---|
| `OTEL_ENABLED` | `false` | Enable OTEL span export to the configured endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | OTLP HTTP endpoint (e.g. `http://localhost:4318`) |
| `OTEL_SERVICE_NAME` | `pci-llm-gateway` | Service name attached to all spans |

All three are declared in `variables.tf` and wired into Lambda `environment.variables` in `lambda.tf`.

---

## [0.5.0] — 2026-05-07

### Linting & CI

- Added `ruff` as the project linter (covers pyflakes F401, isort I001, pycodestyle E/W, pyupgrade UP; E501 ignored).
- Removed all unused imports across the codebase (`ruff --fix`).
- New GitHub Actions workflow `.github/workflows/lint.yml` runs `ruff check .` on every push and pull request.
- `ruff` added to `requirements.txt`; configuration in `pyproject.toml` under `[tool.ruff.lint]`.

### Test architecture — four-layer pytest structure

The monolithic `tests/test_rag.py` was replaced with a proper layered test suite under `tests/rag/`:

| Layer | File | Mark | Tests |
|---|---|---|---|
| Unit | `test_unit.py` | `@pytest.mark.unit` | Individual components — chunking, embedder, retriever helpers, pipeline helpers |
| Integration | `test_integration.py` | `@pytest.mark.integration` | Multi-component workflows with selective mocking |
| Contract | `test_contract.py` | `@pytest.mark.contract` | HTTP semantics, strict Pydantic response schema, auth guards |
| Quality | `test_quality.py` | `@pytest.mark.quality` | Retrieval metrics (recall@k, precision@k), golden dataset, grounding validators |

**Factories and fixtures (`tests/rag/conftest.py`):**

- `make_chunk()`, `make_rag_result()`, `make_citation_result()`, `make_grounding()` — plain factory functions with meaningful defaults; exposed as fixtures via `chunk_factory`, `rag_result_factory`.
- `make_token(exp_offset)` — generates signed HS256 JWTs; negative offset creates expired tokens for auth failure tests.
- Shared fixtures: `app_client`, `auth_headers`, `postgres_env`, `mock_rag_pipeline`, `mock_llm_client`.

**Strict response schema validation (`test_contract.py`):**

Pydantic contract models (`model_config = ConfigDict(extra="forbid")`) mirror the API's declared response types and add business-logic invariants:
- `chunks_retrieved` must equal `len(sources)` (cross-field model validator).
- `answer` must not be blank.
- All field names are exhaustive — extra fields in the response raise `ValidationError`.

**Golden dataset quality tests (`test_quality.py`):**

Five known PCI DSS queries with expected requirement IDs, tested for `recall@5 ≥ 0.5`. Metric helpers `recall_at_k` and `precision_at_k` are unit-tested independently.

**New markers declared in `pyproject.toml`:** `unit`, `integration`, `contract`, `quality`. `asyncio_mode = "strict"` enforced.

### RAG — hybrid retrieval with Reciprocal Rank Fusion

`RAGRetriever` now combines two retrieval signals before returning results:

| Signal | Implementation |
|---|---|
| Vector (semantic) | pgvector cosine similarity — unchanged |
| BM25 (keyword) | PostgreSQL `ts_rank_cd + plainto_tsquery` full-text search on a GIN index |

The two ranked lists are fused with **Reciprocal Rank Fusion** (k=60): `score[id] += 1/(k + rank + 1)` from each signal. A chunk appearing in both signals gets a double boost; vector-only or BM25-only chunks are still included. Each signal fetches `top_k × 2` candidates before fusion to give RRF enough candidates.

Toggle: `RAG_HYBRID=true` (default) / `false` (pure vector).

`VectorStore` additions: GIN FTS index in `_INIT_SQL`, new `full_text_search(query, top_k)` method, `id` field propagated through `similarity_search` results (required by RRF).

### RAG — QueryAnalyzer (pre-retrieval intent classification)

New class `QueryAnalyzer` (`services/rag/query_analyzer.py`): a single Haiku call before retrieval classifies the query into a list of PCI DSS requirement IDs (e.g., `["10.5.1", "3.4"]`). These hints are returned in the response as `requirement_hints` and optionally used to bias retrieval. Failures are silently suppressed — a failed classification never aborts a query.

### RAG — ContextBuilder (structured LLM context formatting)

New class `ContextBuilder` (`services/rag/context_builder.py`): replaces the flat context string with structured blocks:

```
[PCI DSS v4.0.1 — Requirement 10.5.1]
Section: Requirement 10.5.1
Relevance: 0.92

<chunk text>

══════════════════════════
```

Chunk text is truncated to `CONTEXT_MAX_CHUNK_CHARS` (default 2000) before insertion. Multiple chunks are separated by `═` dividers so the model can distinguish source boundaries.

### RAG — GroundingValidator (3-layer deterministic grounding check)

New class `GroundingValidator` (`services/rag/grounding_validator.py`) validates LLM answers without using an LLM:

| Layer | Method | Cost |
|---|---|---|
| 1. Citation check | `validate_citations()` — regex extracts cited req IDs; verifies each appears in retrieved sources | Free |
| 2. Claim extraction | `extract_claims()` — splits answer into sentences | Free |
| 3. Semantic support | `GroundingValidator.validate_answer(semantic=True)` — one batched `embed_batch` call for all claims + chunks; cosine similarity per claim vs. all chunks | 1 API call |

`validate_answer()` returns `is_grounded`, `score` (fraction of supported claims), `unsupported_claims`, and `citation_result`. The embedding call is skipped when `semantic=False`.

Toggle: `GROUNDING_VALIDATE=true` / `false` (default). Runs via `asyncio.to_thread` to avoid blocking.

### RAG — pipeline hardening

`RAGPipeline` (`services/rag/rag_pipeline.py`) now runs six sequential stages:

1. `QueryAnalyzer.classify()` — intent hints (non-fatal)
2. `RAGRetriever.retrieve()` — hybrid RRF retrieval
3. `_filter_chunks()` — score threshold (`RAG_MIN_SCORE`, default 0.0) + requirement_id deduplication (keep highest-score chunk per requirement)
4. `_build_context()` — structured context with `RAG_CONTEXT_BUDGET_CHARS` (default 8000) character budget; always includes the top-ranked chunk even if it exceeds the budget
5. `llm.complete()` — answer generation with explicit system prompt (`_RAG_SYSTEM`)
6. `GroundingValidator.validate_answer()` — via `asyncio.to_thread` (when `GROUNDING_VALIDATE=true`)

Empty-retrieval short-circuit: if `_filter_chunks` returns nothing, returns a canned "no relevant PCI DSS information found" answer without calling the LLM.

**Response schema additions** (`schemas/rag_schemas.py`):

| New field | Type | Description |
|---|---|---|
| `requirement_hints` | `list[str]` | Requirement IDs extracted by QueryAnalyzer |
| `grounding` | `RAGGrounding \| None` | Full grounding report when `GROUNDING_VALIDATE=true` |
| `grounding.is_grounded` | `bool` | Citations valid AND all claims semantically supported |
| `grounding.score` | `float` | Fraction of claims with cosine similarity ≥ threshold |
| `grounding.unsupported_claims` | `list[{claim, score}]` | Claims that fell below the similarity threshold |
| `grounding.citation_result` | object | `valid`, `cited`, `retrieved`, `invalid_citations`, `missing_all_citations` |

### Bug fix — `_sliding_chunks` infinite loop

`scripts/ingest_pci_dss.py`: When the final segment of text was shorter than `_OVERLAP` (200 chars), `start = end - _OVERLAP` never advanced, causing an infinite loop. Fixed with `if end >= len(text): break` after appending each chunk.

### New environment variables

All new tuneable values are declared in `variables.tf` and wired into Lambda `environment.variables` in `lambda.tf`.

| Env var | Default | Controls |
|---|---|---|
| `RAG_HYBRID` | `true` | Enable hybrid BM25 + vector retrieval |
| `RAG_MIN_SCORE` | `0.0` | Minimum similarity score to keep a chunk |
| `RAG_CONTEXT_BUDGET_CHARS` | `8000` | Max total characters of context passed to the LLM |
| `CONTEXT_MAX_CHUNK_CHARS` | `2000` | Max characters from a single chunk in the context block |
| `GROUNDING_VALIDATE` | `false` | Enable GroundingValidator after LLM answer generation |
| `GROUNDING_THRESHOLD` | `0.82` | Cosine similarity floor for semantic claim support |
| `QUERY_ANALYZER_MODEL` | `claude-haiku-4-5-20251001` | Model for pre-retrieval query classification |
| `QUERY_ANALYZER_MAX_TOKENS` | `128` | Max tokens for QueryAnalyzer response |
| `RAG_SYSTEM_PROMPT` | *(see code)* | System prompt prepended to every RAG LLM call |
| `RRF_K` | `60` | RRF constant k (higher = smoother rank weighting) |

---

## [0.4.0] — 2026-05-06

### RAG — PCI DSS v4.0.1 knowledge base (`services/rag/` + `scripts/ingest_pci_dss.py`)

A retrieval-augmented generation layer grounded in the actual PCI DSS v4.0.1 standard text.

**New endpoint — `POST /v1/rag/query`**

| Field | Description |
|---|---|
| `question` | Natural language question about PCI DSS |
| `model` | LLM to generate the answer (default: `claude-haiku-4-5-20251001`) |
| `top_k` | Chunks to retrieve (1–20, default 5) |
| `max_tokens` | Max tokens in the generated answer (default 1024) |

Response includes `answer`, `sources` (requirement ID + section title + similarity score), and `chunks_retrieved`.

**Ingestion script — `scripts/ingest_pci_dss.py`**

Downloads or reads a local copy of the PCI DSS v4.0.1 PDF, chunks by requirement section (falls back to sliding window with 200-character overlap), generates OpenAI `text-embedding-3-small` embeddings in batches of 20, and inserts into pgvector. Run once before using the RAG endpoint:

```bash
python scripts/ingest_pci_dss.py \
  --url https://www.middlebury.edu/sites/default/files/2025-01/PCI-DSS-v4_0_1.pdf
```

**New service — `services/rag/`**

| Class | File | Responsibility |
|---|---|---|
| `EmbeddingClient` | `embedder.py` | OpenAI `text-embedding-3-small` embed / embed_batch |
| `VectorStore` | `vector_store.py` | psycopg2 + pgvector — initialise, upsert, similarity search, clear |
| `RAGRetriever` | `retriever.py` | Embed query → similarity search → format context block |
| `RAGPipeline` | `rag_pipeline.py` | Retrieve context → build augmented prompt → call LLM |

**Agent integration — compliance subagent and new `search_pci_dss` tool**

- The `compliance` subagent now automatically retrieves relevant PCI DSS chunks before its Haiku call, grounding answers in the actual standard text. Falls back gracefully if `POSTGRES_DSN` is not set.
- New orchestrator tool `search_pci_dss(query, top_k)` — lets the orchestrator retrieve raw PCI DSS sections directly without delegating to the subagent.
- Orchestrator system prompt updated to instruct use of `search_pci_dss` before making compliance claims.

**Infrastructure**

- `docker-compose.yml`: `pgvector/pgvector:pg16` sidecar with a named volume; `gateway` service depends on it via `healthcheck`.
- `infra/terraform/rds.tf`: RDS PostgreSQL 16 in the existing VPC private subnets — encrypted with CMK, `multi_az = true`, deletion protection, 7-day backups.
- New env vars: `POSTGRES_DSN`, `EMBEDDING_MODEL` (default `text-embedding-3-small`), `RAG_TOP_K` (default 5). All declared in `variables.tf` and wired into Lambda `environment.variables`.

**Design decisions:**

- **OpenAI embeddings, not Anthropic** — Anthropic does not offer an embeddings API. `text-embedding-3-small` (1536 dimensions, $0.02/1M tokens) was chosen over `text-embedding-3-large` because the quality difference is marginal for domain-specific retrieval on a structured regulatory document.
- **Chunking by requirement section, not fixed token count** — PCI DSS is a numbered requirements document. Keeping each requirement's text, testing procedures, and guidance together preserves the semantic unit that maps to a real question ("what does Req 10.5.1 require?"). Fixed-size chunks would routinely split across requirement boundaries.
- **Graceful degradation when pgvector is unavailable** — `POSTGRES_DSN` being unset disables the RAG endpoint with a `503` and silently falls back to base-model responses in the compliance subagent. This means the existing inference and agent endpoints continue to work without any database dependency.
- **IVFFlat index (lists=100)** — chosen over HNSW because the corpus fits in memory and IVFFlat has lower build time, which matters for a document that may need re-ingestion when a new PCI DSS version is released. HNSW is faster at query time and is the better choice above ~500K chunks.

---

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
