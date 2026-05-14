"""Anthropic Messages API client with circuit breaker and unified error mapping."""

import anthropic
from circuit_breaker import CircuitOpenError, get_breaker
from fastapi import HTTPException
from llm_response import LLMResponse
from secret_resolver import resolve_env_secret

_client = anthropic.AsyncAnthropic(api_key=resolve_env_secret("ANTHROPIC_API_KEY_SECRET_ARN", "ANTHROPIC_API_KEY"))


class _ProviderError(Exception):
    """Transient Anthropic failure — counts toward the circuit-breaker threshold."""

    def __init__(self, http_status: int, detail: str) -> None:
        self.http_status = http_status
        self.detail = detail


class AnthropicClient:
    """Async wrapper around the Anthropic Messages API with circuit breaker and unified error mapping."""

    async def complete(self, prompt: str, model: str, max_tokens: int, system: str | None = None) -> LLMResponse:
        """Send *prompt* to the Anthropic API and return a structured response.

        Provider-level failures (429, 5xx) advance the circuit breaker toward OPEN.
        Client errors (401, 400) are returned immediately without affecting circuit state.

        Args:
            prompt:     User message text.
            model:      Anthropic model identifier.
            max_tokens: Maximum tokens to generate.
            system:     Optional system prompt.

        Returns:
            LLMResponse with text, token counts, and model identifier.

        Raises:
            HTTPException(503): When the circuit is OPEN (provider repeatedly failing).
            HTTPException(429): Anthropic rate limit (circuit not yet open).
            HTTPException(401): Bad API key.
            HTTPException(402): Insufficient credits.
            HTTPException(502): Other Anthropic API error.
        """
        breaker = get_breaker("anthropic")
        try:
            return await breaker.call(self._call(prompt, model, max_tokens, system), trip_on=(_ProviderError,))
        except CircuitOpenError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Anthropic provider unavailable: {exc}",
                headers={"Retry-After": str(int(exc.retry_after))},
            )
        except _ProviderError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.detail)

    async def _call(self, prompt: str, model: str, max_tokens: int, system: str | None) -> LLMResponse:
        """Raw Anthropic API call — raises _ProviderError for transient failures."""
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        try:
            message = await _client.messages.create(**kwargs)
            return LLMResponse(
                text=message.content[0].text,
                prompt_tokens=message.usage.input_tokens,
                completion_tokens=message.usage.output_tokens,
                model=model,
            )
        except anthropic.AuthenticationError:
            raise HTTPException(status_code=401, detail="Anthropic: invalid API key")
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc):
                raise HTTPException(
                    status_code=402, detail="Anthropic: insufficient credits — add funds at console.anthropic.com"
                )
            raise HTTPException(status_code=400, detail=f"Anthropic: {exc.message}")
        except anthropic.RateLimitError:
            raise _ProviderError(429, "Anthropic: rate limit exceeded, retry shortly")
        except anthropic.APIStatusError as exc:
            raise _ProviderError(502, f"Anthropic error {exc.status_code}: {exc.message}")
