"""RAG route — PCI DSS v4.0.1 question-answering with tenant isolation and structured logging."""

import logging
import os

import rate_limiter
import tenant_quota
from fastapi import APIRouter, Depends, HTTPException
from model_registry import get_client
from rag_pipeline import RAGPipeline
from schemas.rag_schemas import RAGQueryRequest, RAGQueryResponse, RAGSource
from structured_logger import stage_span
from tenant import TenantConfig, require_tenant

router = APIRouter()
log = logging.getLogger("pci-gateway.rag")


def _postgres_configured() -> bool:
    """Return True when POSTGRES_DSN is set in the environment."""
    return bool(os.environ.get("POSTGRES_DSN"))


@router.post("/rag/query", response_model=RAGQueryResponse, dependencies=[Depends(rate_limiter.limit("rag"))])
async def rag_query(body: RAGQueryRequest, tenant: TenantConfig = Depends(require_tenant)) -> RAGQueryResponse:
    """Query PCI DSS v4.0.1 via RAG with tenant isolation.

    Args:
        body:   Validated RAGQueryRequest with question, model, and options.
        tenant: Resolved TenantConfig from the JWT ``tenant_id`` claim.

    Returns:
        RAGQueryResponse with the grounded answer, sources, and grounding metadata.
    """
    if not _postgres_configured():
        raise HTTPException(status_code=503, detail="RAG is not available — POSTGRES_DSN is not configured.")

    if tenant.allowed_models is not None and body.model not in tenant.allowed_models:
        raise HTTPException(
            status_code=422,
            detail=f"Model '{body.model}' is not permitted for tenant '{tenant.tenant_id}'",
        )

    if tenant.monthly_budget_usd is not None:
        tenant_quota.check_budget(tenant.tenant_id, tenant.monthly_budget_usd)

    try:
        llm_client = get_client(body.model)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unsupported model: {body.model}")

    pipeline = RAGPipeline(llm_client)
    with stage_span(log, "rag_query", model=body.model) as meta:
        try:
            result = await pipeline.query(
                question=body.question,
                model=body.model,
                max_tokens=body.max_tokens,
                top_k=body.top_k,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"RAG query failed: {exc}")

        llm_usage = result.get("llm_usage", {})
        meta["prompt_tokens"] = llm_usage.get("prompt_tokens", 0)
        meta["completion_tokens"] = llm_usage.get("completion_tokens", 0)
        meta["total_cost_usd"] = llm_usage.get("total_cost_usd", 0.0)
        meta["chunks_retrieved"] = len(result.get("sources", []))

    tenant_quota.record_spend(tenant.tenant_id, llm_usage.get("total_cost_usd", 0.0))

    log.info(
        "rag_complete",
        extra={
            "tenant_id": tenant.tenant_id,
            "model": body.model,
            "chunks_retrieved": len(result.get("sources", [])),
            "prompt_tokens": llm_usage.get("prompt_tokens", 0),
            "completion_tokens": llm_usage.get("completion_tokens", 0),
            "total_tokens": llm_usage.get("total_tokens", 0),
            "input_cost_usd": llm_usage.get("input_cost_usd", 0.0),
            "output_cost_usd": llm_usage.get("output_cost_usd", 0.0),
            "total_cost_usd": llm_usage.get("total_cost_usd", 0.0),
        },
    )

    grounding = result.get("grounding")
    return RAGQueryResponse(
        answer=result["answer"],
        sources=[RAGSource(**s) for s in result["sources"]],
        chunks_retrieved=len(result["sources"]),
        requirement_hints=result.get("requirement_hints", []),
        grounding=grounding,
    )
