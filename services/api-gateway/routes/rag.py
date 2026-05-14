"""RAG route — PCI DSS v4.0.1 question-answering with structured logging."""

import logging
import os

from fastapi import APIRouter, HTTPException
from model_registry import get_client
from rag_pipeline import RAGPipeline
from schemas.rag_schemas import RAGQueryRequest, RAGQueryResponse, RAGSource
from structured_logger import stage_span

router = APIRouter()
log = logging.getLogger("pci-gateway.rag")


def _postgres_configured() -> bool:
    """Return True when POSTGRES_DSN is set in the environment."""
    return bool(os.environ.get("POSTGRES_DSN"))


@router.post("/rag/query", response_model=RAGQueryResponse)
async def rag_query(request: RAGQueryRequest) -> RAGQueryResponse:
    """Query PCI DSS v4.0.1 via RAG.

    Retrieves the most relevant requirement sections from pgvector, then
    generates a grounded answer that cites specific requirement numbers.
    Requires the PCI DSS corpus to have been ingested via scripts/ingest_pci_dss.py.

    Args:
        request: Validated RAGQueryRequest with question, model, and options.

    Returns:
        RAGQueryResponse with the grounded answer, sources, and grounding metadata.
    """
    if not _postgres_configured():
        raise HTTPException(
            status_code=503,
            detail="RAG is not available — POSTGRES_DSN is not configured.",
        )

    try:
        llm_client = get_client(request.model)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unsupported model: {request.model}")

    pipeline = RAGPipeline(llm_client)
    with stage_span(log, "rag_query", model=request.model) as meta:
        try:
            result = await pipeline.query(
                question=request.question,
                model=request.model,
                max_tokens=request.max_tokens,
                top_k=request.top_k,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"RAG query failed: {exc}")

        llm_usage = result.get("llm_usage", {})
        meta["prompt_tokens"] = llm_usage.get("prompt_tokens", 0)
        meta["completion_tokens"] = llm_usage.get("completion_tokens", 0)
        meta["total_cost_usd"] = llm_usage.get("total_cost_usd", 0.0)
        meta["chunks_retrieved"] = len(result.get("sources", []))

    log.info(
        "rag_complete",
        extra={
            "model": request.model,
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
