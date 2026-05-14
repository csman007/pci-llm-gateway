import asyncio
import logging
import os

from context_builder import ContextBuilder
from grounding_validator import GroundingValidator
from injection_detector import InjectionDetector
from query_analyzer import QueryAnalyzer
from retriever import RAGRetriever
from token_counter import calculate_cost

_inject_detector = InjectionDetector()
log = logging.getLogger("pci-gateway.rag_pipeline")

# ── Tuning ─────────────────────────────────────────────────────────────────────
# Chunks with a retrieval score below this threshold are discarded before context
# building.  Set to 0.0 to disable score filtering.
_MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.0"))

# Hard budget on the total number of characters allowed in the context block.
# Prevents long tails of low-ranked chunks from blowing up the prompt.
_CONTEXT_BUDGET_CHARS = int(os.environ.get("RAG_CONTEXT_BUDGET_CHARS", "8000"))

# When true, run batched embedding-based semantic support validation on the
# generated answer (one extra API call).  When false, only the fast citation
# regex check is run.
_GROUNDING_VALIDATE = os.environ.get("GROUNDING_VALIDATE", "false").lower() == "true"

_GROUNDING_THRESHOLD = float(os.environ.get("GROUNDING_THRESHOLD", "0.82"))

_RAG_SYSTEM = (
    "You are a PCI DSS v4.0.1 compliance specialist. "
    "Answer the question using ONLY the PCI DSS sections provided below. "
    "Cite the requirement number for every claim (e.g. 'Req 10.5.1'). "
    "If the answer cannot be found in the provided sections, say so explicitly."
)

_CONTEXT_TEMPLATE = """\
The following sections from PCI DSS v4.0.1 are relevant to the question:

{context}

---

Question: {question}"""

_NO_CONTEXT_ANSWER = (
    "No relevant PCI DSS sections were found for this question. "
    "Please rephrase your query or contact your compliance team directly."
)


class RAGPipeline:
    """Retrieves PCI DSS context and generates a grounded, cited LLM answer.

    Pipeline stages:
      1. QueryAnalyzer     — classify query to requirement IDs (intent layer)
      2. RAGRetriever      — hybrid vector + BM25 retrieval with RRF fusion
      3. _filter_chunks    — score threshold + requirement_id deduplication
      4. ContextBuilder    — structured multi-block context with char budget
      5. LLM completion    — grounded answer (system prompt enforced)
      6. GroundingValidator — citation check + optional semantic support
    """

    def __init__(self, llm_client) -> None:
        """
        Args:
            llm_client: An LLM client exposing complete(prompt, model, max_tokens, system).
        """
        self._retriever = RAGRetriever()
        self._context_builder = ContextBuilder()
        self._query_analyzer = QueryAnalyzer(llm_client)
        self._llm = llm_client

    async def query(
        self,
        question: str,
        model: str,
        max_tokens: int = 1024,
        top_k: int | None = None,
    ) -> dict:
        """Retrieve PCI DSS context and generate a grounded answer.

        Args:
            question:   The user's PCI DSS question.
            model:      LLM model identifier for the final answer.
            max_tokens: Maximum tokens for the generated answer.
            top_k:      Number of chunks to retrieve (None → env default).

        Returns:
            Dict with keys:
                answer             — grounded LLM response (str)
                sources            — list of {requirement_id, section_title, score}
                requirement_hints  — requirement IDs inferred from query intent
                grounding          — citation + semantic support validation report
                llm_usage          — token counts and cost from calculate_cost()
        """
        # Stage 1 — query intent classification (best-effort; never aborts query).
        try:
            requirement_hints = await self._query_analyzer.classify(question)
        except Exception:  # noqa: BLE001
            requirement_hints = []

        # Stage 2 — hybrid retrieval.
        raw_chunks = await self._retriever.retrieve(question, top_k)

        # Stage 3 — score filter + requirement_id deduplication.
        chunks = _filter_chunks(raw_chunks, min_score=_MIN_SCORE)

        # Stage 3b — indirect injection guard: drop any chunk whose text matches
        # a BLOCK-severity injection pattern before it enters the context window.
        chunks = _filter_injection_chunks(chunks)

        # Short-circuit: no usable context → safe refusal, skip LLM call.
        if not chunks:
            return {
                "answer": _NO_CONTEXT_ANSWER,
                "sources": [],
                "requirement_hints": requirement_hints,
                "grounding": _empty_grounding(),
                "llm_usage": calculate_cost(model, 0, 0),
            }

        # Stage 4 — structured context with character budget.
        context = _build_context(chunks, self._context_builder, _CONTEXT_BUDGET_CHARS)
        prompt = _CONTEXT_TEMPLATE.format(context=context, question=question)

        # Stage 5 — grounded answer.  System prompt is passed explicitly so
        # the model receives the compliance instruction as a first-class message.
        llm_resp = await self._llm.complete(prompt=prompt, model=model, max_tokens=max_tokens, system=_RAG_SYSTEM)
        answer = llm_resp.text
        usage = calculate_cost(model, llm_resp.prompt_tokens, llm_resp.completion_tokens)

        # Stage 6 — grounding validation.
        sources = [
            {
                "requirement_id": c.get("requirement_id"),
                "section_title": c.get("section_title"),
                "score": round(c.get("score", 0.0), 3),
            }
            for c in chunks
        ]
        validator = GroundingValidator(threshold=_GROUNDING_THRESHOLD)
        grounding = await asyncio.to_thread(
            validator.validate_answer,
            answer,
            chunks,
            sources,
            semantic=_GROUNDING_VALIDATE,
        )

        if _GROUNDING_VALIDATE and not grounding["is_grounded"] and grounding["unsupported_claims"]:
            answer += (
                "\n\n⚠ Grounding note: one or more claims could not be verified "
                "against retrieved PCI DSS sections — review before use in compliance decisions."
            )

        return {
            "answer": answer,
            "sources": sources,
            "requirement_hints": requirement_hints,
            "grounding": grounding,
            "llm_usage": usage,
        }


# ── Pipeline helpers ───────────────────────────────────────────────────────────


def _filter_injection_chunks(chunks: list[dict]) -> list[dict]:
    """Drop chunks whose text contains BLOCK-severity injection patterns.

    Protects against indirect prompt injection where adversarial content
    embedded in a retrieved document attempts to hijack the model when
    included in the context window.

    Args:
        chunks: Chunks after score-filtering and deduplication.

    Returns:
        Subset of *chunks* with any injection-poisoned entries removed.
    """
    safe = []
    for chunk in chunks:
        text = chunk.get("chunk_text", "")
        findings = _inject_detector.scan(text)
        _inject_detector.log_findings(findings, source="rag_chunk")
        if not _inject_detector.is_blocked(findings):
            safe.append(chunk)
        else:
            log.warning("rag_chunk_dropped_injection", extra={"requirement_id": chunk.get("requirement_id")})
    return safe


def _filter_chunks(chunks: list[dict], min_score: float) -> list[dict]:
    """Apply score threshold and deduplicate by requirement_id.

    Deduplication keeps the highest-scoring chunk per requirement ID so the
    context does not repeat the same requirement in multiple blocks.

    Args:
        chunks:    Retrieved chunks ordered by descending relevance score.
        min_score: Chunks with score < min_score are discarded.

    Returns:
        Filtered and deduplicated list, preserving original score order.
    """
    filtered = [c for c in chunks if c.get("score", 0.0) >= min_score]
    seen: set[str | None] = set()
    deduped = []
    for chunk in filtered:
        req_id = chunk.get("requirement_id")
        if req_id not in seen:
            seen.add(req_id)
            deduped.append(chunk)
    return deduped


def _build_context(chunks: list[dict], builder: ContextBuilder, budget: int) -> str:
    """Build structured context, respecting a total character budget.

    Chunks are added in order until the budget would be exceeded; the first
    chunk that fits is always included regardless of size.

    Args:
        chunks:  Filtered chunks, highest relevance first.
        builder: ContextBuilder instance for formatting.
        budget:  Maximum total characters across all included chunks.

    Returns:
        Formatted context string.
    """
    included: list[dict] = []
    total = 0
    for chunk in chunks:
        chunk_len = len(chunk.get("chunk_text", ""))
        if included and total + chunk_len > budget:
            break
        included.append(chunk)
        total += chunk_len
    return builder.build(included)


def _empty_grounding() -> dict:
    """Grounding report for queries that returned no retrievable chunks."""
    return {
        "is_grounded": True,  # no claims made → no ungrounded claims
        "score": 1.0,
        "unsupported_claims": [],
        "citation_result": {
            "valid": False,
            "cited": [],
            "retrieved": [],
            "invalid_citations": [],
            "missing_all_citations": True,
        },
    }
