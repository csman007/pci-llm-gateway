"""Integration tests — wire multiple RAG components together, mock only I/O boundaries."""

from unittest.mock import AsyncMock, patch

import pytest

from tests.rag.conftest import make_chunk


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_tool_returns_error_without_postgres(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    from tools import _search_pci_dss

    result = await _search_pci_dss("log retention")

    assert result.startswith("ERROR")
    assert "POSTGRES_DSN" in result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_tool_returns_formatted_context(postgres_env: None, monkeypatch: pytest.MonkeyPatch):
    fake_chunks = [make_chunk(requirement_id="10.5.1", chunk_text="Retain logs.")]
    formatted = "[PCI DSS Requirement 10.5.1]\nRetain logs."

    with patch("retriever.RAGRetriever") as MockRetriever:
        instance = MockRetriever.return_value
        instance.retrieve = AsyncMock(return_value=fake_chunks)
        MockRetriever.format_context = staticmethod(lambda _chunks: formatted)

        from tools import _search_pci_dss

        result = await _search_pci_dss("log retention", top_k=3)

    assert "10.5.1" in result
    assert "Retain logs" in result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retriever_passes_top_k_to_vector_store(postgres_env: None):
    """Verify that top_k×2 candidates are fetched from each signal in hybrid mode."""
    with patch("retriever.EmbeddingClient") as MockEmb, patch("retriever.VectorStore") as MockStore:
        MockEmb.return_value.embed.return_value = [0.0] * 1_536
        MockStore.return_value.similarity_search.return_value = []
        MockStore.return_value.full_text_search.return_value = []

        from retriever import RAGRetriever

        await RAGRetriever().retrieve("question", top_k=3)

    MockStore.return_value.similarity_search.assert_called_once()
    _args, _kwargs = MockStore.return_value.similarity_search.call_args
    # Hybrid mode fetches top_k * 2 candidates per signal before RRF re-ranking.
    assert 6 in _args, f"expected top_k*2=6 in similarity_search args, got {_args}"
