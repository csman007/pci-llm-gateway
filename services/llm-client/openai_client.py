"""OpenAI Chat Completions client with circuit breaker and unified error mapping."""

import openai
from circuit_breaker import CircuitOpenError, get_breaker
from fastapi import HTTPException
from llm_response import LLMResponse
from openai import AsyncOpenAI
from secret_resolver import resolve_env_secret

_client = AsyncOpenAI(api_key=resolve_env_secret("OPENAI_API_KEY_SECRET_ARN", "OPENAI_API_KEY"))


class _ProviderError(Exception):
    """Transient OpenAI failure — counts toward the circuit-breaker threshold."""

    def __init__(self, http_status: int, detail: str) -> None:
        self.http_status = http_status
        self.detail = detail


class OpenAIClient:
    """Async wrapper around the OpenAI Chat Completions API with circuit breaker and unified error mapping."""

    async def complete(self, prompt: str, model: str, max_tokens: int, system: str | None = None) -> LLMResponse:
        """Send *prompt* to the OpenAI API and return a structured response.

        Provider-level failures (429, 5xx) advance the circuit breaker toward OPEN.
        Client errors (401, 402) are returned immediately without affecting circuit state.

        Args:
            prompt:     User message text.
            model:      OpenAI model identifier.
            max_tokens: Maximum tokens to generate.
            system:     Optional system prompt inserted as a system message.

        Returns:
            LLMResponse with text, token counts, and model identifier.

        Raises:
            HTTPException(503): When the circuit is OPEN (provider repeatedly failing).
            HTTPException(429): OpenAI rate limit (circuit not yet open).
            HTTPException(401): Bad API key.
            HTTPException(402): Insufficient quota.
            HTTPException(502): Other OpenAI API error.
        """
        breaker = get_breaker("openai")
        try:
            return await breaker.call(self._call(prompt, model, max_tokens, system), trip_on=(_ProviderError,))
        except CircuitOpenError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"OpenAI provider unavailable: {exc}",
                headers={"Retry-After": str(int(exc.retry_after))},
            )
        except _ProviderError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.detail)

    async def _call(self, prompt: str, model: str, max_tokens: int, system: str | None) -> LLMResponse:
        """Raw OpenAI API call — raises _ProviderError for transient failures."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await _client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
            )
            return LLMResponse(
                text=response.choices[0].message.content,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                model=model,
            )
        except openai.AuthenticationError:
            raise HTTPException(status_code=401, detail="OpenAI: invalid API key")
        except openai.RateLimitError as exc:
            if "insufficient_quota" in str(exc):
                raise HTTPException(
                    status_code=402, detail="OpenAI: insufficient quota — add credits at platform.openai.com"
                )
            raise _ProviderError(429, "OpenAI: rate limit exceeded, retry shortly")
        except openai.APIStatusError as exc:
            raise _ProviderError(502, f"OpenAI error {exc.status_code}: {exc.message}")
