"""Unit tests — each test exercises one component with everything else mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.rag.conftest import make_chunk


# ── Ingestion: chunking ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_chunk_document_splits_on_requirement_ids():
    from ingest_pci_dss import chunk_document

    reqs = "\n\n".join(
        f"Requirement 1.{i}.1: Section {i} description.\nGuidance: text."
        for i in range(1, 12)
    )
    text = reqs + "\n\nRequirement 10.5.1: Retain audit logs for 12 months.\n"
    chunks = chunk_document(text)

    req_ids = {c["requirement_id"] for c in chunks}
    assert "10.5.1" in req_ids
    for chunk in chunks:
        assert chunk["requirement_id"], "every chunk must carry a requirement_id"
        assert chunk["chunk_text"].strip(), "chunk_text must not be blank"


@pytest.mark.unit
def test_chunk_document_falls_back_to_sliding_window():
    from ingest_pci_dss import chunk_document

    text = "A" * 5_000  # no requirement headers → sliding window
    chunks = chunk_document(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk["chunk_text"]) > 0


@pytest.mark.unit
def test_sliding_chunks_size_and_overlap():
    from ingest_pci_dss import _OVERLAP, _sliding_chunks

    text = "x" * 4_000
    chunks = _sliding_chunks(text)

    assert len(chunks) >= 2
    for c in chunks:
        assert 0 < len(c) <= 1_500, f"chunk length {len(c)} out of bounds"
    # Consecutive chunks must share exactly _OVERLAP characters at the seam.
    assert chunks[0][-_OVERLAP:] == chunks[1][:_OVERLAP]


# ── Embedder ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_embedding_client_single_embed():
    fake_embedding = [0.1] * 1_536
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=fake_embedding)]

    with patch("embedder.OpenAI") as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.embeddings.create.return_value = mock_response

        from embedder import EmbeddingClient

        client = EmbeddingClient()
        result = client.embed("test text")

    assert result == fake_embedding
    instance.embeddings.create.assert_called_once()


@pytest.mark.unit
def test_embedding_client_batch_embed():
    fake_embeddings = [[float(i)] * 1_536 for i in range(3)]
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=e) for e in fake_embeddings]

    with patch("embedder.OpenAI") as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.embeddings.create.return_value = mock_response

        from embedder import EmbeddingClient

        client = EmbeddingClient()
        result = client.embed_batch(["a", "b", "c"])

    assert result == fake_embeddings


# ── RAGRetriever ──────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_retriever_returns_ranked_chunks(fake_chunks: list[dict]):
    with patch("retriever.EmbeddingClient") as MockEmb, patch("retriever.VectorStore") as MockStore:
        MockEmb.return_value.embed.return_value = [0.0] * 1_536
        MockStore.return_value.similarity_search.return_value = fake_chunks
        MockStore.return_value.full_text_search.return_value = []

        from retriever import RAGRetriever

        result = await RAGRetriever().retrieve("log retention requirements")

    assert len(result) == 1
    assert result[0]["requirement_id"] == "10.5.1"
    assert "retained for 12 months" in result[0]["chunk_text"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retriever_propagates_embedding_failure():
    with patch("retriever.EmbeddingClient") as MockEmb, patch("retriever.VectorStore"):
        MockEmb.return_value.embed.side_effect = Exception("OpenAI API error")

        from retriever import RAGRetriever

        with pytest.raises(Exception, match="OpenAI API error"):
            await RAGRetriever().retrieve("query")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retriever_propagates_vector_store_failure():
    with patch("retriever.EmbeddingClient") as MockEmb, patch("retriever.VectorStore") as MockStore:
        MockEmb.return_value.embed.return_value = [0.0] * 1_536
        MockStore.return_value.similarity_search.side_effect = Exception("DB connection refused")

        from retriever import RAGRetriever

        with pytest.raises(Exception, match="DB connection refused"):
            await RAGRetriever().retrieve("query")


@pytest.mark.unit
def test_format_context_renders_all_chunks():
    from retriever import RAGRetriever

    chunks = [
        make_chunk(requirement_id="10.5.1", chunk_text="Retain logs for 12 months."),
        make_chunk(requirement_id="10.5.2", chunk_text="Protect logs from modification.", score=0.85),
    ]
    context = RAGRetriever.format_context(chunks)

    assert "10.5.1" in context
    assert "Retain logs for 12 months" in context
    assert "10.5.2" in context
    assert "---" in context


@pytest.mark.unit
def test_format_context_empty_returns_sentinel():
    from retriever import RAGRetriever

    result = RAGRetriever.format_context([])
    assert "No relevant" in result


# ── Pipeline helpers ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_filter_chunks_removes_low_score():
    from rag_pipeline import _filter_chunks

    chunks = [
        make_chunk(requirement_id="10.5.1", score=0.9),
        make_chunk(requirement_id="3.4", score=0.5),
        make_chunk(requirement_id="1.2", score=0.8),
    ]
    result = _filter_chunks(chunks, min_score=0.75)
    req_ids = [c["requirement_id"] for c in result]

    assert "10.5.1" in req_ids
    assert "1.2" in req_ids
    assert "3.4" not in req_ids


@pytest.mark.unit
def test_filter_chunks_deduplicates_requirement_id():
    from rag_pipeline import _filter_chunks

    # Two chunks with same requirement_id — only the first (higher score) survives.
    chunks = [
        make_chunk(requirement_id="10.5.1", score=0.9),
        make_chunk(requirement_id="10.5.1", score=0.7),
        make_chunk(requirement_id="3.4", score=0.8),
    ]
    result = _filter_chunks(chunks, min_score=0.0)

    assert len(result) == 2
    assert result[0]["requirement_id"] == "10.5.1"
    assert result[0]["score"] == 0.9  # higher-scored chunk kept


@pytest.mark.unit
def test_filter_chunks_zero_threshold_keeps_all():
    from rag_pipeline import _filter_chunks

    chunks = [make_chunk(score=0.1), make_chunk(requirement_id="3.4", score=0.0)]
    assert len(_filter_chunks(chunks, min_score=0.0)) == 2


@pytest.mark.unit
def test_build_context_respects_budget():
    from context_builder import ContextBuilder
    from rag_pipeline import _build_context

    long_text = "A" * 5000
    chunks = [
        make_chunk(requirement_id="10.5.1", chunk_text=long_text),
        make_chunk(requirement_id="3.4",    chunk_text=long_text),
    ]
    context = _build_context(chunks, ContextBuilder(), budget=6000)

    # Only the first chunk should fit within a 6000-char budget.
    assert "10.5.1" in context
    assert "3.4" not in context


@pytest.mark.unit
def test_build_context_always_includes_first_chunk():
    from context_builder import ContextBuilder
    from rag_pipeline import _build_context

    # Even if a single chunk exceeds the budget, it must be included.
    chunks = [make_chunk(requirement_id="10.5.1", chunk_text="A" * 10_000)]
    context = _build_context(chunks, ContextBuilder(), budget=100)
    assert "10.5.1" in context
