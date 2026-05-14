import anthropic
from fastapi import HTTPException
from llm_response import LLMResponse
from secret_resolver import resolve_env_secret

_client = anthropic.AsyncAnthropic(api_key=resolve_env_secret("ANTHROPIC_API_KEY_SECRET_ARN", "ANTHROPIC_API_KEY"))


class AnthropicClient:
    """Async wrapper around the Anthropic Messages API with unified error mapping."""

    async def complete(self, prompt: str, model: str, max_tokens: int, system: str | None = None) -> LLMResponse:
        """Send *prompt* to the Anthropic API and return a structured response.

        Args:
            prompt:     User message text.
            model:      Anthropic model identifier.
            max_tokens: Maximum tokens to generate.
            system:     Optional system prompt.

        Returns:
            LLMResponse with text, token counts, and model identifier.
        """
        kwargs = {
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
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e):
                raise HTTPException(
                    status_code=402, detail="Anthropic: insufficient credits — add funds at console.anthropic.com"
                )
            raise HTTPException(status_code=400, detail=f"Anthropic: {e.message}")
        except anthropic.RateLimitError:
            raise HTTPException(status_code=429, detail="Anthropic: rate limit exceeded, retry shortly")
        except anthropic.APIStatusError as e:
            raise HTTPException(status_code=502, detail=f"Anthropic error {e.status_code}: {e.message}")
