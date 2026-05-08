from pydantic import BaseModel, Field


class RAGQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    model: str = Field(default="claude-haiku-4-5-20251001")
    max_tokens: int = Field(default=1024, ge=64, le=4096)
    top_k: int = Field(default=5, ge=1, le=20)


class RAGSource(BaseModel):
    requirement_id: str | None
    section_title: str | None
    score: float


class RAGCitationResult(BaseModel):
    """Output of the citation validation stage."""

    valid: bool                   # at least one citation exists and none are invalid
    cited: list[str]              # requirement IDs cited in the answer
    retrieved: list[str]          # requirement IDs present in retrieved chunks
    invalid_citations: list[str]  # cited IDs not backed by any retrieved chunk
    missing_all_citations: bool   # True when the answer contains no citations


class RAGUnsupportedClaim(BaseModel):
    """A claim sentence that could not be semantically matched to any chunk."""

    claim: str
    score: float  # best cosine similarity found (below threshold)


class RAGGrounding(BaseModel):
    """Layered grounding validation report from GroundingValidator."""

    is_grounded: bool                       # citations valid AND all claims supported
    score: float                            # fraction of semantically supported claims
    unsupported_claims: list[RAGUnsupportedClaim]
    citation_result: RAGCitationResult


class RAGQueryResponse(BaseModel):
    answer: str
    sources: list[RAGSource]
    chunks_retrieved: int
    requirement_hints: list[str] = []
    grounding: RAGGrounding | None = None
