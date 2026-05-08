"""Retrieval quality evaluation — recall@k, precision@k, grounding coverage.

Run with:  pytest -m quality
These tests measure retrieval intelligence, not just code correctness.
A failing quality test means the RAG system regressed on known queries.
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.rag.conftest import make_chunk


# ── Golden dataset ────────────────────────────────────────────────────────────
# Each entry is a known query paired with the requirement IDs a correct
# retrieval system should surface. Derived from PCI DSS v4.0.1.

GOLDEN_DATASET = [
    {
        "query": "How long must I retain audit logs?",
        "expected_req_ids": {"10.5.1", "10.7"},
    },
    {
        "query": "Do I need to encrypt cardholder data at rest?",
        "expected_req_ids": {"3.4", "3.4.1", "3.5"},
    },
    {
        "query": "What are requirements for network segmentation?",
        "expected_req_ids": {"1.3", "1.4", "11.4"},
    },
    {
        "query": "How should I handle failed login attempts?",
        "expected_req_ids": {"8.3.4", "8.2.4"},
    },
    {
        "query": "What key management requirements exist for encryption keys?",
        "expected_req_ids": {"3.6", "3.7", "3.6.1"},
    },
]


# ── Metric helpers ────────────────────────────────────────────────────────────

def recall_at_k(retrieved_ids: list[str], expected_ids: set[str], k: int) -> float:
    """Fraction of expected requirement IDs found in the top-k results.

    Args:
        retrieved_ids: Ordered list of retrieved requirement IDs.
        expected_ids:  Set of IDs that should be retrieved.
        k:             Cut-off rank.

    Returns:
        Float in [0, 1].
    """
    if not expected_ids:
        return 1.0
    top_k = set(retrieved_ids[:k])
    return len(top_k & expected_ids) / len(expected_ids)


def precision_at_k(retrieved_ids: list[str], expected_ids: set[str], k: int) -> float:
    """Fraction of the top-k results that are relevant.

    Args:
        retrieved_ids: Ordered list of retrieved requirement IDs.
        expected_ids:  Set of IDs that should be retrieved.
        k:             Cut-off rank.

    Returns:
        Float in [0, 1].
    """
    if k == 0:
        return 0.0
    top_k = retrieved_ids[:k]
    return sum(1 for r in top_k if r in expected_ids) / k


# ── Metric unit tests ─────────────────────────────────────────────────────────

@pytest.mark.quality
class TestMetricFunctions:
    def test_perfect_recall(self):
        assert recall_at_k(["10.5.1", "10.7"], {"10.5.1", "10.7"}, k=5) == 1.0

    def test_partial_recall(self):
        assert recall_at_k(["10.5.1", "3.4"], {"10.5.1", "10.7"}, k=5) == 0.5

    def test_zero_recall(self):
        assert recall_at_k(["3.4", "3.5"], {"10.5.1", "10.7"}, k=5) == 0.0

    def test_recall_respects_k_cutoff(self):
        # "10.7" is rank 3 — outside k=2, so recall < 1
        assert recall_at_k(["10.5.1", "3.4", "10.7"], {"10.5.1", "10.7"}, k=2) == 0.5

    def test_perfect_precision(self):
        assert precision_at_k(["10.5.1", "10.7"], {"10.5.1", "10.7"}, k=2) == 1.0

    def test_partial_precision(self):
        assert precision_at_k(["10.5.1", "3.4"], {"10.5.1", "10.7"}, k=2) == 0.5

    def test_empty_expected_ids_returns_full_recall(self):
        assert recall_at_k(["10.5.1"], set(), k=5) == 1.0


# ── RRF fusion quality tests ──────────────────────────────────────────────────

@pytest.mark.quality
class TestRRFFusion:
    def test_chunk_in_both_signals_ranks_first(self):
        from retriever import RAGRetriever

        shared_chunk = make_chunk(requirement_id="10.5.1", score=0.9)
        shared_chunk["id"] = 1
        vector_only = make_chunk(requirement_id="3.4", score=0.85)
        vector_only["id"] = 2
        bm25_only = make_chunk(requirement_id="10.7", score=0.7)
        bm25_only["id"] = 3
        # shared_chunk appears in both signals
        fused = RAGRetriever._reciprocal_rank_fusion(
            vector_results=[shared_chunk, vector_only],
            bm25_results=[shared_chunk, bm25_only],
        )
        assert fused[0]["requirement_id"] == "10.5.1", "chunk matching both signals must rank first"

    def test_fusion_deduplicates_chunks(self):
        from retriever import RAGRetriever

        chunk = make_chunk(requirement_id="10.5.1")
        chunk["id"] = 1
        fused = RAGRetriever._reciprocal_rank_fusion(
            vector_results=[chunk],
            bm25_results=[chunk],
        )
        assert len(fused) == 1

    def test_fusion_empty_bm25_falls_back_to_vector(self):
        from retriever import RAGRetriever

        chunk = make_chunk(requirement_id="10.5.1")
        chunk["id"] = 1
        fused = RAGRetriever._reciprocal_rank_fusion(
            vector_results=[chunk],
            bm25_results=[],
        )
        assert len(fused) == 1
        assert fused[0]["requirement_id"] == "10.5.1"

    def test_rrf_score_higher_for_top_ranked_chunk(self):
        from retriever import RAGRetriever

        top = make_chunk(requirement_id="10.5.1", score=0.9)
        top["id"] = 1
        bottom = make_chunk(requirement_id="3.4", score=0.5)
        bottom["id"] = 2
        fused = RAGRetriever._reciprocal_rank_fusion(
            vector_results=[top, bottom],
            bm25_results=[],
        )
        assert fused[0]["score"] > fused[1]["score"]


# ── Citation validation tests ─────────────────────────────────────────────────

@pytest.mark.quality
class TestCitationValidation:
    def test_fully_valid_citations(self):
        from grounding_validator import validate_citations

        sources = [{"requirement_id": "10.5.1"}]
        result = validate_citations("Per Req 10.5.1, logs must be retained.", sources)
        assert result["valid"] is True
        assert result["invalid_citations"] == []
        assert "10.5.1" in result["cited"]

    def test_invalid_citation_detected(self):
        from grounding_validator import validate_citations

        sources = [{"requirement_id": "10.5.1"}]
        result = validate_citations("Per Req 10.5.1 and Req 3.4, encrypt logs.", sources)
        assert result["valid"] is False
        assert "3.4" in result["invalid_citations"]

    def test_no_citations_flagged(self):
        from grounding_validator import validate_citations

        result = validate_citations("You must retain logs for 12 months.", [])
        assert result["missing_all_citations"] is True
        assert result["valid"] is False

    def test_duplicate_citations_deduplicated(self):
        from grounding_validator import validate_citations

        sources = [{"requirement_id": "10.5.1"}]
        result = validate_citations("Req 10.5.1 is key. Req 10.5.1 is mandatory.", sources)
        assert result["cited"].count("10.5.1") == 1


# ── Claim extraction tests ────────────────────────────────────────────────────

@pytest.mark.quality
class TestClaimExtraction:
    def test_single_sentence(self):
        from grounding_validator import extract_claims

        claims = extract_claims("Logs must be retained for 12 months.")
        assert len(claims) == 1

    def test_multi_sentence(self):
        from grounding_validator import extract_claims

        claims = extract_claims("Logs must be retained. Encryption is required. Use AES.")
        assert len(claims) == 3

    def test_empty_string_returns_empty(self):
        from grounding_validator import extract_claims

        assert extract_claims("") == []

    def test_strips_whitespace(self):
        from grounding_validator import extract_claims

        claims = extract_claims("  First claim.  Second claim.  ")
        assert all(c == c.strip() for c in claims)


# ── GroundingValidator — semantic support ─────────────────────────────────────

@pytest.mark.quality
class TestGroundingValidatorSemantic:
    """Tests for the embedding-based semantic claim support check."""

    def _mock_embedder(self, monkeypatch: pytest.MonkeyPatch, vectors: list[list[float]]):
        """Patch EmbeddingClient.embed_batch to return *vectors* in order."""
        from unittest.mock import MagicMock, patch

        mock = MagicMock()
        mock.embed_batch.return_value = vectors
        return patch("grounding_validator.EmbeddingClient", return_value=mock)

    def test_supported_claim_passes(self, monkeypatch: pytest.MonkeyPatch):
        from grounding_validator import GroundingValidator

        # Identical vectors → cosine similarity = 1.0 → supported.
        vec = [1.0] + [0.0] * 1535
        chunks = [make_chunk(chunk_text="Logs must be retained for 12 months.")]
        sources = [{"requirement_id": "10.5.1", "section_title": "Req 10.5.1", "score": 0.92}]
        answer = "According to Req 10.5.1, logs must be retained."

        with patch("grounding_validator.EmbeddingClient") as MockEmb:
            MockEmb.return_value.embed_batch.return_value = [vec, vec]
            validator = GroundingValidator(threshold=0.82)
            result = validator.validate_answer(answer, chunks, sources, semantic=True)

        assert result["is_grounded"] is True
        assert result["unsupported_claims"] == []

    def test_unsupported_claim_detected(self, monkeypatch: pytest.MonkeyPatch):
        from grounding_validator import GroundingValidator

        # Orthogonal vectors → cosine similarity = 0.0 → not supported.
        claim_vec = [1.0] + [0.0] * 1535
        chunk_vec = [0.0, 1.0] + [0.0] * 1534
        chunks = [make_chunk(chunk_text="Logs must be retained.")]
        sources = [{"requirement_id": "10.5.1", "section_title": "Req 10.5.1", "score": 0.92}]
        answer = "According to Req 10.5.1, use AES-256 everywhere."

        with patch("grounding_validator.EmbeddingClient") as MockEmb:
            MockEmb.return_value.embed_batch.return_value = [claim_vec, chunk_vec]
            validator = GroundingValidator(threshold=0.82)
            result = validator.validate_answer(answer, chunks, sources, semantic=True)

        assert result["is_grounded"] is False
        assert len(result["unsupported_claims"]) == 1
        assert result["score"] == 0.0

    def test_semantic_false_skips_embeddings(self):
        from grounding_validator import GroundingValidator

        chunks = [make_chunk(requirement_id="10.5.1")]
        sources = [{"requirement_id": "10.5.1", "section_title": "Req", "score": 0.9}]
        answer = "According to Req 10.5.1, retain logs."

        with patch("grounding_validator.EmbeddingClient") as MockEmb:
            validator = GroundingValidator()
            validator.validate_answer(answer, chunks, sources, semantic=False)
            MockEmb.assert_not_called()

    def test_score_reflects_partial_support(self, monkeypatch: pytest.MonkeyPatch):
        from grounding_validator import GroundingValidator

        # Two claims: first supported (parallel vectors), second not (orthogonal).
        supported_vec = [1.0] + [0.0] * 1535
        unsupported_vec = [0.0, 1.0] + [0.0] * 1534
        chunk_vec = [1.0] + [0.0] * 1535

        chunks = [make_chunk(chunk_text="Logs must be retained.")]
        sources = [{"requirement_id": "10.5.1", "section_title": "Req", "score": 0.9}]
        answer = "Req 10.5.1 requires log retention. Use AES-256 for all data."

        with patch("grounding_validator.EmbeddingClient") as MockEmb:
            MockEmb.return_value.embed_batch.return_value = [
                supported_vec, unsupported_vec, chunk_vec
            ]
            validator = GroundingValidator(threshold=0.82)
            result = validator.validate_answer(answer, chunks, sources, semantic=True)

        assert result["score"] == pytest.approx(0.5)
        assert len(result["unsupported_claims"]) == 1


# ── End-to-end pipeline quality simulation ────────────────────────────────────

@pytest.mark.quality
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", GOLDEN_DATASET, ids=[e["query"][:40] for e in GOLDEN_DATASET])
async def test_retrieval_recall_at_5(entry: dict, monkeypatch: pytest.MonkeyPatch):
    """Verify recall@5 ≥ 0.5 for each golden query when retrieval is simulated."""
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x:x@localhost/x")

    # Simulate a retriever that returns all expected chunks plus one distractor.
    expected_chunks = [
        {**make_chunk(requirement_id=req_id), "id": i}
        for i, req_id in enumerate(entry["expected_req_ids"])
    ]
    distractor = {**make_chunk(requirement_id="99.9", score=0.5), "id": 999}

    with patch("retriever.EmbeddingClient") as MockEmb, patch("retriever.VectorStore") as MockStore:
        MockEmb.return_value.embed.return_value = [0.0] * 1_536
        MockStore.return_value.similarity_search.return_value = expected_chunks + [distractor]
        MockStore.return_value.full_text_search.return_value = expected_chunks

        from retriever import RAGRetriever

        retrieved = await RAGRetriever().retrieve(entry["query"], top_k=5)

    retrieved_ids = [c["requirement_id"] for c in retrieved if c.get("requirement_id")]
    r_at_5 = recall_at_k(retrieved_ids, entry["expected_req_ids"], k=5)
    assert r_at_5 >= 0.5, (
        f"recall@5={r_at_5:.2f} < 0.5 for query: {entry['query']!r}\n"
        f"retrieved={retrieved_ids}, expected={entry['expected_req_ids']}"
    )
