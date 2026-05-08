import asyncio
import os

from embedder import EmbeddingClient
from vector_store import VectorStore

_RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
# Set RAG_HYBRID=false to fall back to vector-only search.
_RAG_HYBRID = os.environ.get("RAG_HYBRID", "true").lower() == "true"
# Constant k in the RRF formula — higher values reduce the impact of top ranks.
_RRF_K = int(os.environ.get("RAG_RRF_K", "60"))


class RAGRetriever:
    """Embeds a query and retrieves the most relevant PCI DSS chunks from pgvector.

    In hybrid mode (default) both cosine-similarity vector search and
    PostgreSQL full-text (BM25) search are run in parallel, then fused with
    Reciprocal Rank Fusion so that chunks matching both signals rank highest.
    """

    def __init__(self) -> None:
        self._embedder = EmbeddingClient()
        self._store = VectorStore()

    async def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """Retrieve the top_k most relevant PCI DSS chunks for *query*.

        Uses hybrid retrieval (vector + BM25 via RRF) when RAG_HYBRID=true,
        otherwise falls back to pure vector search.

        Args:
            query: Natural language question or search string.
            top_k: Number of chunks to return; defaults to RAG_TOP_K env var.

        Returns:
            List of chunk dicts ordered by descending relevance score.
        """
        k = top_k or _RAG_TOP_K

        if not _RAG_HYBRID:
            embedding = await asyncio.to_thread(self._embedder.embed, query)
            return await asyncio.to_thread(self._store.similarity_search, embedding, k)

        # Fetch 2× candidates from each signal so RRF has enough to re-rank.
        embedding = await asyncio.to_thread(self._embedder.embed, query)
        vector_results, bm25_results = await asyncio.gather(
            asyncio.to_thread(self._store.similarity_search, embedding, k * 2),
            asyncio.to_thread(self._store.full_text_search, query, k * 2),
        )
        return self._reciprocal_rank_fusion(vector_results, bm25_results, _RRF_K)[:k]

    @staticmethod
    def _reciprocal_rank_fusion(
        vector_results: list[dict],
        bm25_results: list[dict],
        k: int = 60,
    ) -> list[dict]:
        """Fuse two ranked lists via Reciprocal Rank Fusion.

        Score_RRF(d) = Σ 1 / (k + rank(d, list_i))

        Args:
            vector_results: Cosine-similarity ranked chunks (must include 'id').
            bm25_results:   BM25 ranked chunks (must include 'id').
            k: RRF constant; 60 is the standard value.

        Returns:
            Merged list sorted by descending RRF score, with 'score' replaced
            by the fused RRF score.
        """
        scores: dict[int, float] = {}
        meta: dict[int, dict] = {}

        for rank, chunk in enumerate(vector_results):
            cid = chunk["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            meta[cid] = chunk

        for rank, chunk in enumerate(bm25_results):
            cid = chunk["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in meta:
                meta[cid] = chunk

        sorted_ids = sorted(scores, key=lambda i: scores[i], reverse=True)
        return [{**meta[i], "score": round(scores[i], 4)} for i in sorted_ids]

    @staticmethod
    def format_context(chunks: list[dict]) -> str:
        """Format retrieved chunks as a simple context block (legacy helper).

        Args:
            chunks: Output of retrieve().

        Returns:
            Multi-section string with each chunk labelled by its requirement ID.
        """
        if not chunks:
            return "No relevant PCI DSS sections found."
        parts = []
        for chunk in chunks:
            req = chunk.get("requirement_id") or "General"
            label = f"[PCI DSS Requirement {req}]" if req != "General" else "[PCI DSS]"
            parts.append(f"{label}\n{chunk['chunk_text'].strip()}")
        return "\n\n---\n\n".join(parts)
