import os
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch
import jwt

from llm_response import LLMResponse


def _make_llm_response(text: str, model: str = "claude-sonnet-4-6") -> LLMResponse:
    """Build a mock LLMResponse with zero token counts."""
    return LLMResponse(text=text, prompt_tokens=10, completion_tokens=5, model=model)


def _make_token(sub: str = "user-1") -> str:
    return jwt.encode({"sub": sub}, os.environ["JWT_SECRET"], algorithm="HS256")


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


@pytest.mark.asyncio
async def test_health_check():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_models():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert "models" in resp.json()
    assert len(resp.json()["models"]) > 0


@pytest.mark.asyncio
async def test_dev_token_endpoint():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/dev/token", json={"sub": "test-user"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_pan_blocked(auth_headers):
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/inference",
            json={"prompt": "Charge card 4111111111111111", "model": "claude-sonnet-4-6"},
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert "PAN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_expiry_blocked(auth_headers):
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/inference",
            json={"prompt": "My card expiry: 09/26", "model": "claude-sonnet-4-6"},
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert "EXPIRY" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unsupported_model_returns_422(auth_headers):
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/inference",
            json={"prompt": "Hello", "model": "unknown-model-x"},
            headers=auth_headers,
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_safe_prompt_reaches_llm(auth_headers):
    from main import app
    mock_text = "A neural network is a machine learning model."

    with patch("routes.inference.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value=_make_llm_response(mock_text))
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/inference",
                json={"prompt": "What is a neural network?", "model": "claude-sonnet-4-6"},
                headers=auth_headers,
            )
    assert resp.status_code == 200
    assert resp.json()["response"] == mock_text


@pytest.mark.asyncio
async def test_invalid_llm_response_returns_502(auth_headers):
    from main import app
    with patch("routes.inference.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value=_make_llm_response("I cannot assist with that request."))
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/inference",
                json={"prompt": "Hello", "model": "claude-sonnet-4-6"},
                headers=auth_headers,
            )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_pii_leakage_in_response_returns_502(auth_headers):
    from main import app
    with patch("routes.inference.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(
            return_value=_make_llm_response("Contact leaker@evil.com for more info.")
        )
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/inference",
                json={"prompt": "Hello", "model": "claude-sonnet-4-6"},
                headers=auth_headers,
            )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_invalid_token_returns_401():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/inference",
            json={"prompt": "Hello"},
            headers={"Authorization": "Bearer not.a.valid.token"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_token_returns_401():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/inference", json={"prompt": "Hello"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_key_enforced_when_configured(auth_headers, monkeypatch):
    import middleware.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_API_KEY", "correct-key")

    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Wrong key → 401
        resp = await client.post(
            "/v1/inference",
            json={"prompt": "Hello"},
            headers={**auth_headers, "x-api-key": "wrong-key"},
        )
    assert resp.status_code == 401

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing key → 401
        resp = await client.post(
            "/v1/inference",
            json={"prompt": "Hello"},
            headers=auth_headers,
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_key_accepted_when_correct(auth_headers, monkeypatch):
    import middleware.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_API_KEY", "correct-key")

    from main import app
    with patch("routes.inference.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value=_make_llm_response("Hello world"))
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/inference",
                json={"prompt": "Hello", "model": "claude-sonnet-4-6"},
                headers={**auth_headers, "x-api-key": "correct-key"},
            )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_exempt_from_api_key(monkeypatch):
    import middleware.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_API_KEY", "some-key")

    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
