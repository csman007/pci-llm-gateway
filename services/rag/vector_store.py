import json
import os

import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values

_POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "postgresql://gateway:gateway@localhost:5432/pci_gateway")

_INIT_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS pci_dss_chunks (
    id            SERIAL PRIMARY KEY,
    requirement_id TEXT,
    section_title  TEXT,
    chunk_text     TEXT        NOT NULL,
    embedding      vector(1536) NOT NULL,
    metadata       JSONB       DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS pci_dss_chunks_embedding_idx
    ON pci_dss_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- GIN index for hybrid BM25-style full-text search over chunk text + requirement IDs.
CREATE INDEX IF NOT EXISTS pci_dss_chunks_fts_idx
    ON pci_dss_chunks
    USING GIN (
        to_tsvector(
            'english',
            coalesce(chunk_text, '') || ' ' || coalesce(requirement_id, '')
        )
    );
"""

# Shared expression used in both the index and query — must match exactly.
_FTS_EXPR = "to_tsvector('english', coalesce(chunk_text, '') || ' ' || coalesce(requirement_id, ''))"


class VectorStore:
    """PostgreSQL + pgvector store for PCI DSS document chunks."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or _POSTGRES_DSN

    def _connect(self):
        conn = psycopg2.connect(self._dsn)
        register_vector(conn)
        return conn

    def initialise(self) -> None:
        """Create the pgvector extension, table, IVFFlat index, and FTS GIN index if absent."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_INIT_SQL)

    def upsert_chunks(self, chunks: list[dict]) -> int:
        """Insert chunk dicts into the store. Existing rows are skipped.

        Args:
            chunks: Each dict must have keys: requirement_id, section_title,
                    chunk_text, embedding. Optional: metadata (dict).

        Returns:
            Number of rows inserted.
        """
        rows = [
            (
                c.get("requirement_id"),
                c.get("section_title"),
                c["chunk_text"],
                c["embedding"],
                json.dumps(c.get("metadata", {})),
            )
            for c in chunks
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO pci_dss_chunks
                        (requirement_id, section_title, chunk_text, embedding, metadata)
                    VALUES %s
                    """,
                    rows,
                )
                return cur.rowcount

    def similarity_search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """Return the top_k chunks nearest to *query_embedding* by cosine similarity.

        Args:
            query_embedding: Query vector of length 1536.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys: id, requirement_id, section_title,
            chunk_text, metadata, score (cosine similarity 0–1).
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, requirement_id, section_title, chunk_text, metadata,
                           1 - (embedding <=> %s::vector) AS score
                    FROM   pci_dss_chunks
                    ORDER  BY embedding <=> %s::vector
                    LIMIT  %s
                    """,
                    (query_embedding, query_embedding, top_k),
                )
                rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "requirement_id": r[1],
                "section_title": r[2],
                "chunk_text": r[3],
                "metadata": r[4] if isinstance(r[4], dict) else {},
                "score": float(r[5]),
            }
            for r in rows
        ]

    def full_text_search(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25-style keyword search using PostgreSQL tsvector / ts_rank_cd.

        Searches both chunk_text and requirement_id so queries like "10.5.1"
        match by requirement number even when the text doesn't repeat it.

        Args:
            query: Natural-language query string.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with the same shape as similarity_search(), ordered
            by descending BM25 rank score. Empty list when no FTS matches.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, requirement_id, section_title, chunk_text, metadata,
                           ts_rank_cd({_FTS_EXPR}, plainto_tsquery('english', %s)) AS score
                    FROM   pci_dss_chunks
                    WHERE  {_FTS_EXPR} @@ plainto_tsquery('english', %s)
                    ORDER  BY score DESC
                    LIMIT  %s
                    """,
                    (query, query, top_k),
                )
                rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "requirement_id": r[1],
                "section_title": r[2],
                "chunk_text": r[3],
                "metadata": r[4] if isinstance(r[4], dict) else {},
                "score": float(r[5]),
            }
            for r in rows
        ]

    def count(self) -> int:
        """Return the total number of chunks in the store."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM pci_dss_chunks")
                return cur.fetchone()[0]

    def clear(self) -> None:
        """Delete all chunks — used before a full re-ingestion."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE pci_dss_chunks RESTART IDENTITY")
