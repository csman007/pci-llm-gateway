"""Factories and shared fixtures for all RAG test layers."""

import os
import time
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient


# ── Factories ─────────────────────────────────────────────────────────────────
# Plain functions — call them directly in tests or wrap them in fixtures.
# Every field has a meaningful default so tests only specify what they care about.

_chunk_id_counter = 0


def make_chunk(
    requirement_id: str = "10.5.1",
    section_title: str = "Requirement 10.5.1",
    chunk_text: str = "Logs must be retained for 12 months.",
    score: float = 0.92,
    chunk_id: int | None = None,
) -> dict:
    global _chunk_id_counter
    _chunk_id_counter += 1
    return {
        "id": chunk_id if chunk_id is not None else _chunk_id_counter,
        "requirement_id": requirement_id,
        "section_title": section_title,
        "chunk_text": chunk_text,
        "score": score,
    }


def make_citation_result(
    cited: list[str] | None = None,
    retrieved: list[str] | None = None,
    invalid: list[str] | None = None,
    missing_all: bool = False,
) -> dict:
    cited = cited or ["10.5.1"]
    retrieved = retrieved or ["10.5.1"]
    invalid = invalid or []
    return {
        "valid": bool(cited) and not invalid,
        "cited": cited,
        "retrieved": retrieved,
        "invalid_citations": invalid,
        "missing_all_citations": missing_all,
    }


def make_grounding(
    is_grounded: bool = True,
    score: float = 1.0,
    unsupported_claims: list[dict] | None = None,
    citation_result: dict | None = None,
) -> dict:
    return {
        "is_grounded": is_grounded,
        "score": score,
        "unsupported_claims": unsupported_claims or [],
        "citation_result": citation_result or make_citation_result(),
    }


def make_rag_result(
    answer: str = "PCI DSS Req 10.5.1 requires audit logs to be retained for 12 months.",
    sources: list[dict] | None = None,
    requirement_hints: list[str] | None = None,
    grounding: dict | None = None,
) -> dict:
    return {
        "answer": answer,
        "sources": sources if sources is not None else [make_chunk()],
        "requirement_hints": requirement_hints if requirement_hints is not None else ["10.5.1"],
        "grounding": grounding if grounding is not None else make_grounding(),
    }


def make_token(sub: str = "test-user", exp_offset: int = 3600) -> str:
    """Build a signed HS256 JWT. Pass exp_offset < 0 for an already-expired token."""
    secret = os.environ.get("JWT_SECRET", "test-secret")
    return jwt.encode(
        {"sub": sub, "exp": int(time.time()) + exp_offset},
        secret,
        algorithm="HS256",
    )


# ── Factory fixtures ──────────────────────────────────────────────────────────
# Expose the factory callables as fixtures so tests can import them via DI.

@pytest.fixture()
def chunk_factory():
    """Returns the make_chunk factory so tests can build custom chunks inline."""
    return make_chunk


@pytest.fixture()
def rag_result_factory():
    """Returns the make_rag_result factory for inline result construction."""
    return make_rag_result


# ── Data fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def fake_chunks() -> list[dict]:
    return [make_chunk()]


@pytest.fixture()
def fake_rag_result(fake_chunks: list[dict]) -> dict:
    return make_rag_result(sources=fake_chunks)


# ── Infrastructure fixtures ───────────────────────────────────────────────────

@pytest.fixture()
def app_client() -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token()}"}


@pytest.fixture()
def postgres_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activates a dummy POSTGRES_DSN so the route guard doesn't fire 503."""
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x:x@localhost/x")


@pytest.fixture()
def mock_rag_pipeline(fake_rag_result: dict) -> AsyncMock:
    """Fully-mocked RAGPipeline whose query() returns fake_rag_result."""
    pipeline = AsyncMock()
    pipeline.query.return_value = fake_rag_result
    return pipeline


@pytest.fixture()
def mock_llm_client() -> MagicMock:
    return MagicMock()
