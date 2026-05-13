"""Deterministic grounding validation for RAG-generated answers.

Validation is layered from cheapest to most expensive:
  1. Citation check   — regex, free
  2. Claim extraction — regex, free
  3. Semantic support — batched embeddings, one API call per validate_answer()

Never uses an LLM to validate an LLM.  Stochastic validators introduce a
second error surface; deterministic checks catch the most dangerous failures.
"""

import os
import re

import numpy as np
from embedder import EmbeddingClient

_GROUNDING_THRESHOLD = float(os.environ.get("GROUNDING_THRESHOLD", "0.82"))

# Matches "Req 10.5.1", "Requirement 3.4", "Req 10.5" — case-insensitive.
_CITATION_RE = re.compile(r"Req(?:uirement)?\s+(\d+\.\d+(?:\.\d+)*)", re.IGNORECASE)
# Split on sentence-ending punctuation followed by whitespace.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


# ── Pure helpers ───────────────────────────────────────────────────────────────


def validate_citations(answer: str, sources: list[dict]) -> dict:
    """Check that every requirement ID cited in *answer* appears in *sources*.

    Args:
        answer:  Raw LLM-generated answer text.
        sources: List of {requirement_id, ...} dicts from the pipeline.

    Returns:
        Dict with keys:
            valid               — True when at least one citation exists AND none are invalid
            cited               — deduplicated requirement IDs cited in the answer
            retrieved           — requirement IDs present in the source list
            invalid_citations   — cited IDs not found in any retrieved source
            missing_all_citations — True when the answer contains no citations at all
    """
    cited = list(dict.fromkeys(_CITATION_RE.findall(answer)))
    retrieved = list(dict.fromkeys(s["requirement_id"] for s in sources if s.get("requirement_id")))
    retrieved_set = set(retrieved)
    invalid = [r for r in cited if r not in retrieved_set]
    return {
        "valid": bool(cited) and not invalid,
        "cited": cited,
        "retrieved": retrieved,
        "invalid_citations": invalid,
        "missing_all_citations": len(cited) == 0,
    }


def extract_claims(answer: str) -> list[str]:
    """Split *answer* into atomic claim sentences.

    Args:
        answer: LLM-generated answer text.

    Returns:
        List of non-empty stripped sentences.
    """
    return [p.strip() for p in _SENTENCE_RE.split(answer) if p.strip()]


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── Validator class ────────────────────────────────────────────────────────────


class GroundingValidator:
    """Validates LLM answer grounding against retrieved PCI DSS chunks.

    EmbeddingClient is instantiated lazily so importing this module does not
    require an OpenAI key at startup (only when semantic=True is used).
    """

    def __init__(self, threshold: float = _GROUNDING_THRESHOLD) -> None:
        """
        Args:
            threshold: Minimum cosine similarity for a claim to be considered
                       supported by a retrieved chunk (default: GROUNDING_THRESHOLD).
        """
        self._threshold = threshold
        self._embedder = None

    def _get_embedder(self) -> EmbeddingClient:
        if self._embedder is None:
            self._embedder = EmbeddingClient()
        return self._embedder

    def validate_answer(
        self,
        answer: str,
        chunks: list[dict],
        sources: list[dict],
        *,
        semantic: bool = True,
    ) -> dict:
        """Run the full grounding validation pipeline.

        Stage 1 is always executed (citation check, free).
        Stage 2 requires an embedding API call and only runs when semantic=True.

        Args:
            answer:   Raw LLM-generated answer text.
            chunks:   Retrieved chunks — used for semantic support matching.
            sources:  Source metadata dicts — used for citation validation.
            semantic: When True, run embedding-based claim support validation.

        Returns:
            Dict with keys:
                is_grounded        — citations valid AND every claim is supported
                score              — fraction of semantically supported claims (0–1)
                unsupported_claims — list of {claim, score} for unsupported sentences
                citation_result    — output of validate_citations()
        """
        citation_result = validate_citations(answer, sources)
        claims = extract_claims(answer)
        unsupported: list[dict] = []

        if semantic and claims and chunks:
            chunk_texts = [ch["chunk_text"] for ch in chunks]
            all_embeddings = self._get_embedder().embed_batch(claims + chunk_texts)
            claim_embs = [np.array(e) for e in all_embeddings[: len(claims)]]
            chunk_embs = [np.array(e) for e in all_embeddings[len(claims) :]]

            for i, claim in enumerate(claims):
                best = max(
                    (_cosine_similarity(claim_embs[i], ce) for ce in chunk_embs),
                    default=0.0,
                )
                if best < self._threshold:
                    unsupported.append({"claim": claim, "score": round(best, 3)})

        support_score = 1.0 - (len(unsupported) / max(len(claims), 1))
        is_grounded = citation_result["valid"] and len(unsupported) == 0

        return {
            "is_grounded": is_grounded,
            "score": round(support_score, 3),
            "unsupported_claims": unsupported,
            "citation_result": citation_result,
        }
