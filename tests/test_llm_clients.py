import pytest
import httpx
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
import anthropic
import openai


def _http_response(status_code: int, provider: str = "anthropic") -> httpx.Response:
    url = f"https://api.{provider}.com/v1/messages"
    return httpx.Response(status_code, request=httpx.Request("POST", url))


# ── Anthropic ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_success():
    from anthropic_client import AnthropicClient
    mock_msg = AsyncMock()
    mock_msg.content = [AsyncMock(text="hello")]
    with patch("anthropic_client._client") as mock:
        mock.messages.create = AsyncMock(return_value=mock_msg)
        result = await AnthropicClient().complete("prompt", "claude-sonnet-4-6", 10)
    assert result == "hello"


@pytest.mark.asyncio
async def test_anthropic_auth_error():
    from anthropic_client import AnthropicClient
    exc = anthropic.AuthenticationError("Invalid key", response=_http_response(401), body=None)
    with patch("anthropic_client._client") as mock:
        mock.messages.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await AnthropicClient().complete("prompt", "claude-sonnet-4-6", 10)
    assert info.value.status_code == 401


@pytest.mark.asyncio
async def test_anthropic_low_credits():
    from anthropic_client import AnthropicClient
    exc = anthropic.BadRequestError("Your credit balance is too low", response=_http_response(400), body=None)
    with patch("anthropic_client._client") as mock:
        mock.messages.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await AnthropicClient().complete("prompt", "claude-sonnet-4-6", 10)
    assert info.value.status_code == 402
    assert "console.anthropic.com" in info.value.detail


@pytest.mark.asyncio
async def test_anthropic_bad_request_other():
    from anthropic_client import AnthropicClient
    exc = anthropic.BadRequestError("Invalid model", response=_http_response(400), body=None)
    with patch("anthropic_client._client") as mock:
        mock.messages.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await AnthropicClient().complete("prompt", "claude-sonnet-4-6", 10)
    assert info.value.status_code == 400


@pytest.mark.asyncio
async def test_anthropic_rate_limit():
    from anthropic_client import AnthropicClient
    exc = anthropic.RateLimitError("Rate limit", response=_http_response(429), body=None)
    with patch("anthropic_client._client") as mock:
        mock.messages.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await AnthropicClient().complete("prompt", "claude-sonnet-4-6", 10)
    assert info.value.status_code == 429


@pytest.mark.asyncio
async def test_anthropic_generic_api_error():
    from anthropic_client import AnthropicClient
    exc = anthropic.InternalServerError("Server error", response=_http_response(500), body=None)
    with patch("anthropic_client._client") as mock:
        mock.messages.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await AnthropicClient().complete("prompt", "claude-sonnet-4-6", 10)
    assert info.value.status_code == 502


# ── OpenAI ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_success():
    from openai_client import OpenAIClient
    mock_choice = AsyncMock()
    mock_choice.message.content = "hello"
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]
    with patch("openai_client._client") as mock:
        mock.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await OpenAIClient().complete("prompt", "gpt-4o", 10)
    assert result == "hello"


@pytest.mark.asyncio
async def test_openai_auth_error():
    from openai_client import OpenAIClient
    exc = openai.AuthenticationError("Invalid key", response=_http_response(401, "openai"), body=None)
    with patch("openai_client._client") as mock:
        mock.chat.completions.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await OpenAIClient().complete("prompt", "gpt-4o", 10)
    assert info.value.status_code == 401


@pytest.mark.asyncio
async def test_openai_insufficient_quota():
    from openai_client import OpenAIClient
    exc = openai.RateLimitError("insufficient_quota", response=_http_response(429, "openai"), body=None)
    with patch("openai_client._client") as mock:
        mock.chat.completions.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await OpenAIClient().complete("prompt", "gpt-4o", 10)
    assert info.value.status_code == 402
    assert "platform.openai.com" in info.value.detail


@pytest.mark.asyncio
async def test_openai_rate_limit():
    from openai_client import OpenAIClient
    exc = openai.RateLimitError("Too many requests", response=_http_response(429, "openai"), body=None)
    with patch("openai_client._client") as mock:
        mock.chat.completions.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await OpenAIClient().complete("prompt", "gpt-4o", 10)
    assert info.value.status_code == 429


@pytest.mark.asyncio
async def test_openai_generic_api_error():
    from openai_client import OpenAIClient
    exc = openai.InternalServerError("Server error", response=_http_response(500, "openai"), body=None)
    with patch("openai_client._client") as mock:
        mock.chat.completions.create = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as info:
            await OpenAIClient().complete("prompt", "gpt-4o", 10)
    assert info.value.status_code == 502
