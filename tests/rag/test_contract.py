"""Contract tests — validate HTTP semantics and enforce strict response schema."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tests.rag.conftest import make_chunk, make_token


# ── Response schema contracts ─────────────────────────────────────────────────
# These models mirror the API's declared response types and add business-logic
# invariants. Parsing any API response through them is the assertion.

class RAGSourceContract(BaseModel):
    """Expected shape of a single source citation in the RAG response."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str | None
    section_title: str | None
    score: float


class RAGCitationResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    cited: list[str]
    retrieved: list[str]
    invalid_citations: list[str]
    missing_all_citations: bool


class RAGUnsupportedClaimContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    score: float


class RAGGroundingContract(BaseModel):
    """Expected shape of the grounding report."""

    model_config = ConfigDict(extra="forbid")

    is_grounded: bool
    score: float
    unsupported_claims: list[RAGUnsupportedClaimContract]
    citation_result: RAGCitationResultContract


class RAGResponseContract(BaseModel):
    """Full expected shape of POST /v1/rag/query 200 response."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    sources: list[RAGSourceContract]
    chunks_retrieved: int
    requirement_hints: list[str] = []
    grounding: RAGGroundingContract | None = None

    @field_validator("answer")
    @classmethod
    def answer_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("answer must not be blank")
        return v

    @model_validator(mode="after")
    def chunks_retrieved_matches_sources(self) -> "RAGResponseContract":
        if self.chunks_retrieved != len(self.sources):
            raise ValueError(
                f"chunks_retrieved={self.chunks_retrieved} != len(sources)={len(self.sources)}"
            )
        return self


def _assert_contract(response_body: dict) -> RAGResponseContract:
    """Parse *response_body* through the strict contract; raises on any violation."""
    return RAGResponseContract.model_validate(response_body)


# ── Auth guard ────────────────────────────────────────────────────────────────

@pytest.mark.contract
def test_no_token_is_rejected(app_client: TestClient):
    response = app_client.post("/v1/rag/query", json={"question": "test"})
    assert response.status_code in (401, 403)


@pytest.mark.contract
def test_malformed_token_is_rejected(app_client: TestClient):
    response = app_client.post(
        "/v1/rag/query",
        json={"question": "test"},
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert response.status_code in (401, 403)


@pytest.mark.contract
def test_expired_token_is_rejected(app_client: TestClient):
    response = app_client.post(
        "/v1/rag/query",
        json={"question": "test"},
        headers={"Authorization": f"Bearer {make_token(exp_offset=-60)}"},
    )
    assert response.status_code in (401, 403)


# ── Service availability guard ────────────────────────────────────────────────

@pytest.mark.contract
def test_missing_postgres_returns_503(
    app_client: TestClient,
    auth_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    response = app_client.post(
        "/v1/rag/query",
        json={"question": "What is Requirement 10.5.1?"},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert "POSTGRES_DSN" in response.json()["detail"]


# ── Success path — full contract validation ───────────────────────────────────

@pytest.mark.contract
def test_success_response_satisfies_contract(
    app_client: TestClient,
    auth_headers: dict,
    postgres_env: None,
    mock_rag_pipeline: AsyncMock,
    mock_llm_client: MagicMock,
):
    with (
        patch("routes.rag.RAGPipeline", return_value=mock_rag_pipeline),
        patch("routes.rag.get_client", return_value=mock_llm_client),
    ):
        response = app_client.post(
            "/v1/rag/query",
            json={"question": "What is Requirement 10.5.1?"},
            headers=auth_headers,
        )

    assert response.status_code == 200
    contract = _assert_contract(response.json())
    # Spot-check meaningful values beyond schema shape.
    assert "10.5.1" in contract.answer
    assert contract.sources[0].requirement_id == "10.5.1"
    assert 0.0 < contract.sources[0].score <= 1.0


@pytest.mark.contract
def test_success_response_with_multiple_sources(
    app_client: TestClient,
    auth_headers: dict,
    postgres_env: None,
    rag_result_factory,
    mock_llm_client: MagicMock,
):
    result = rag_result_factory(
        sources=[
            make_chunk(requirement_id="10.5.1"),
            make_chunk(requirement_id="10.5.2", section_title="Requirement 10.5.2", score=0.88),
        ]
    )
    pipeline = AsyncMock()
    pipeline.query.return_value = result

    with (
        patch("routes.rag.RAGPipeline", return_value=pipeline),
        patch("routes.rag.get_client", return_value=mock_llm_client),
    ):
        response = app_client.post(
            "/v1/rag/query",
            json={"question": "log requirements"},
            headers=auth_headers,
        )

    assert response.status_code == 200
    contract = _assert_contract(response.json())
    assert contract.chunks_retrieved == 2
    req_ids = [s.requirement_id for s in contract.sources]
    assert "10.5.1" in req_ids
    assert "10.5.2" in req_ids


# ── Failure paths ─────────────────────────────────────────────────────────────

@pytest.mark.contract
def test_pipeline_failure_returns_502(
    app_client: TestClient,
    auth_headers: dict,
    postgres_env: None,
    mock_llm_client: MagicMock,
):
    failing_pipeline = AsyncMock()
    failing_pipeline.query.side_effect = Exception("upstream failure")

    with (
        patch("routes.rag.RAGPipeline", return_value=failing_pipeline),
        patch("routes.rag.get_client", return_value=mock_llm_client),
    ):
        response = app_client.post(
            "/v1/rag/query",
            json={"question": "What is Requirement 10.5.1?"},
            headers=auth_headers,
        )

    assert response.status_code == 502
    assert "upstream failure" in response.json()["detail"]


@pytest.mark.contract
def test_unknown_model_returns_422(
    app_client: TestClient,
    auth_headers: dict,
    postgres_env: None,
):
    with patch("routes.rag.get_client", side_effect=ValueError("unknown model")):
        response = app_client.post(
            "/v1/rag/query",
            json={"question": "test", "model": "not-a-real-model"},
            headers=auth_headers,
        )
    assert response.status_code == 422


# ── Request validation ────────────────────────────────────────────────────────

@pytest.mark.contract
@pytest.mark.parametrize(
    "payload,expected_status",
    [
        ({}, 422),                                          # missing question
        ({"question": ""}, 422),                           # empty question
        ({"question": "x", "top_k": 0}, 422),             # top_k below minimum
        ({"question": "x", "top_k": 21}, 422),            # top_k above maximum
        ({"question": "x", "max_tokens": 63}, 422),       # max_tokens below minimum
        ({"question": "x", "max_tokens": 4097}, 422),     # max_tokens above maximum
    ],
)
def test_invalid_request_payload_returns_422(
    app_client: TestClient,
    auth_headers: dict,
    payload: dict,
    expected_status: int,
):
    response = app_client.post("/v1/rag/query", json=payload, headers=auth_headers)
    assert response.status_code == expected_status
