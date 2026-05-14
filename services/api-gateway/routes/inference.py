"""Inference route — PII-safe LLM completion pipeline with tenant isolation and observability."""

import logging

import rate_limiter
import tenant_quota
from detector import PIIDetector
from fastapi import APIRouter, Depends, HTTPException
from leakage_detector import LeakageDetector
from model_registry import MODEL_NAMES, get_client
from policy_engine import PolicyEngine
from redactor import Redactor
from schemas.request import InferenceRequest, InferenceResponse
from structured_logger import stage_span
from tenant import TenantConfig, require_tenant
from token_counter import calculate_cost
from tracer import get_tracer
from validator import OutputValidator

router = APIRouter()
log = logging.getLogger("pci-gateway.inference")
_tracer = get_tracer("inference")

detector = PIIDetector()
redactor = Redactor()
policy = PolicyEngine()
validator = OutputValidator()
leakage = LeakageDetector()


@router.get("/models")
def list_models() -> dict:
    """Return the list of supported model identifiers."""
    return {"models": MODEL_NAMES}


@router.post("/inference", response_model=InferenceResponse, dependencies=[Depends(rate_limiter.limit("inference"))])
async def inference(body: InferenceRequest, tenant: TenantConfig = Depends(require_tenant)) -> InferenceResponse:
    """Run a prompt through the full PII-safe inference pipeline with tenant isolation.

    Pipeline stages (in order):
      tenant_check → pii_detect → policy_enforce → tenant_policy →
      redact → budget_check → llm_complete → output_validate →
      leakage_detect → restore → record_spend

    Args:
        body:   Validated InferenceRequest with prompt, model, and max_tokens.
        tenant: Resolved TenantConfig from the JWT ``tenant_id`` claim.

    Returns:
        InferenceResponse with the restored LLM response and PII finding count.
    """
    request_id: str = getattr(body, "request_id", "")
    stage_latencies: dict[str, float] = {}
    pii_count = 0

    # Tenant: model allow-list check (fast reject before any scanning)
    if tenant.allowed_models is not None and body.model not in tenant.allowed_models:
        raise HTTPException(
            status_code=422,
            detail=f"Model '{body.model}' is not permitted for tenant '{tenant.tenant_id}'",
        )

    with _tracer.start_as_current_span("inference"):
        with stage_span(log, "pii_detect", request_id=request_id) as meta:
            findings = detector.scan(body.prompt)
            pii_count = len(findings)
            meta["pii_count"] = pii_count
        stage_latencies["pii_detect"] = meta.get("latency_ms", 0.0)

        with stage_span(log, "policy_enforce", request_id=request_id) as meta:
            policy.enforce(findings)
        stage_latencies["policy_enforce"] = meta.get("latency_ms", 0.0)

        # Tenant: additional blocked entity types beyond global policy
        if tenant.blocked_entity_types:
            tenant_blocked = {f.entity_type for f in findings if f.entity_type in tenant.blocked_entity_types}
            if tenant_blocked:
                raise HTTPException(
                    status_code=400,
                    detail=f"Request blocked by tenant policy: {sorted(tenant_blocked)}",
                )

        with stage_span(log, "redact", request_id=request_id) as meta:
            redacted_prompt, token_map = redactor.redact(body.prompt, findings)
            meta["token_count"] = len(token_map)
        stage_latencies["redact"] = meta.get("latency_ms", 0.0)

        # Tenant: budget gate — check before incurring LLM cost
        if tenant.monthly_budget_usd is not None:
            tenant_quota.check_budget(tenant.tenant_id, tenant.monthly_budget_usd)

        with stage_span(log, "llm_complete", request_id=request_id, model=body.model) as meta:
            result = await get_client(body.model).complete(redacted_prompt, body.model, body.max_tokens)
            usage = calculate_cost(result.model, result.prompt_tokens, result.completion_tokens)
            meta["prompt_tokens"] = result.prompt_tokens
            meta["completion_tokens"] = result.completion_tokens
            meta["total_cost_usd"] = usage["total_cost_usd"]
        stage_latencies["llm_complete"] = meta.get("latency_ms", 0.0)

        # Tenant: record spend after successful LLM call
        tenant_quota.record_spend(tenant.tenant_id, usage["total_cost_usd"])

        response_text = result.text

        with stage_span(log, "output_validate", request_id=request_id) as meta:
            valid = validator.is_valid(response_text)
            meta["valid"] = valid
        stage_latencies["output_validate"] = meta.get("latency_ms", 0.0)

        if not valid:
            raise HTTPException(status_code=502, detail="LLM response failed validation")

        with stage_span(log, "leakage_detect", request_id=request_id) as meta:
            leaked = leakage.detected(response_text, token_map)
            meta["leaked"] = leaked
        stage_latencies["leakage_detect"] = meta.get("latency_ms", 0.0)

        if leaked:
            raise HTTPException(status_code=502, detail="PII leakage detected in response")

        with stage_span(log, "restore", request_id=request_id) as meta:
            restored = redactor.restore(response_text, token_map)
        stage_latencies["restore"] = meta.get("latency_ms", 0.0)

    log.info(
        "inference_complete",
        extra={
            "tenant_id": tenant.tenant_id,
            "request_id": request_id,
            "model": body.model,
            "pii_count": pii_count,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "input_cost_usd": usage["input_cost_usd"],
            "output_cost_usd": usage["output_cost_usd"],
            "total_cost_usd": usage["total_cost_usd"],
            "stage_latencies_ms": stage_latencies,
        },
    )

    return InferenceResponse(response=restored, pii_findings=pii_count)
