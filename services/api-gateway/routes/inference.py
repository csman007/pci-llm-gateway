"""Inference route — PII-safe LLM completion pipeline with observability."""

import logging

import rate_limiter
from detector import PIIDetector
from fastapi import APIRouter, Depends, HTTPException
from leakage_detector import LeakageDetector
from model_registry import MODEL_NAMES, get_client
from policy_engine import PolicyEngine
from redactor import Redactor
from schemas.request import InferenceRequest, InferenceResponse
from structured_logger import stage_span
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
async def inference(request: InferenceRequest) -> InferenceResponse:
    """Run a prompt through the full PII-safe inference pipeline.

    Pipeline stages (in order):
      pii_detect → policy_enforce → redact → llm_complete →
      output_validate → leakage_detect → restore

    Args:
        request: Validated InferenceRequest with prompt, model, and max_tokens.

    Returns:
        InferenceResponse with the restored LLM response and PII finding count.
    """
    request_id: str = getattr(request, "request_id", "")
    stage_latencies: dict[str, float] = {}
    pii_count = 0

    with _tracer.start_as_current_span("inference"):
        # Stage: pii_detect
        with stage_span(log, "pii_detect", request_id=request_id) as meta:
            findings = detector.scan(request.prompt)
            pii_count = len(findings)
            meta["pii_count"] = pii_count
        stage_latencies["pii_detect"] = meta.get("latency_ms", 0.0)

        # Stage: policy_enforce
        with stage_span(log, "policy_enforce", request_id=request_id) as meta:
            policy.enforce(findings)
        stage_latencies["policy_enforce"] = meta.get("latency_ms", 0.0)

        # Stage: redact
        with stage_span(log, "redact", request_id=request_id) as meta:
            redacted_prompt, token_map = redactor.redact(request.prompt, findings)
            meta["token_count"] = len(token_map)
        stage_latencies["redact"] = meta.get("latency_ms", 0.0)

        # Stage: llm_complete
        with stage_span(log, "llm_complete", request_id=request_id, model=request.model) as meta:
            result = await get_client(request.model).complete(redacted_prompt, request.model, request.max_tokens)
            usage = calculate_cost(result.model, result.prompt_tokens, result.completion_tokens)
            meta["prompt_tokens"] = result.prompt_tokens
            meta["completion_tokens"] = result.completion_tokens
            meta["total_cost_usd"] = usage["total_cost_usd"]
        stage_latencies["llm_complete"] = meta.get("latency_ms", 0.0)

        response_text = result.text

        # Stage: output_validate
        with stage_span(log, "output_validate", request_id=request_id) as meta:
            valid = validator.is_valid(response_text)
            meta["valid"] = valid
        stage_latencies["output_validate"] = meta.get("latency_ms", 0.0)

        if not valid:
            raise HTTPException(status_code=502, detail="LLM response failed validation")

        # Stage: leakage_detect
        with stage_span(log, "leakage_detect", request_id=request_id) as meta:
            leaked = leakage.detected(response_text, token_map)
            meta["leaked"] = leaked
        stage_latencies["leakage_detect"] = meta.get("latency_ms", 0.0)

        if leaked:
            raise HTTPException(status_code=502, detail="PII leakage detected in response")

        # Stage: restore
        with stage_span(log, "restore", request_id=request_id) as meta:
            restored = redactor.restore(response_text, token_map)
        stage_latencies["restore"] = meta.get("latency_ms", 0.0)

    # Summary log with all stage latencies, token counts, cost, and pii_count.
    log.info(
        "inference_complete",
        extra={
            "request_id": request_id,
            "model": request.model,
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
