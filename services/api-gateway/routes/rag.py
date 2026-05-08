import os

from fastapi import APIRouter, HTTPException
from model_registry import get_client
from rag_pipeline import RAGPipeline
from schemas.rag_schemas import RAGQueryRequest, RAGQueryResponse, RAGSource

router = APIRouter()


def _postgres_configured() -> bool:
    return bool(os.environ.get("POSTGRES_DSN"))


@router.post("/rag/query", response_model=RAGQueryResponse)
async def rag_query(request: RAGQueryRequest):
    """Query PCI DSS v4.0.1 via RAG.

    Retrieves the most relevant requirement sections from pgvector, then
    generates a grounded answer that cites specific requirement numbers.
    Requires the PCI DSS corpus to have been ingested via scripts/ingest_pci_dss.py.
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
    try:
        result = await pipeline.query(
            question=request.question,
            model=request.model,
            max_tokens=request.max_tokens,
            top_k=request.top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RAG query failed: {exc}")

    grounding = result.get("grounding")
    return RAGQueryResponse(
        answer=result["answer"],
        sources=[RAGSource(**s) for s in result["sources"]],
        chunks_retrieved=len(result["sources"]),
        requirement_hints=result.get("requirement_hints", []),
        grounding=grounding,
    )
