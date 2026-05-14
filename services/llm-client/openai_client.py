import openai
from fastapi import HTTPException
from llm_response import LLMResponse
from openai import AsyncOpenAI
from secret_resolver import resolve_env_secret

_client = AsyncOpenAI(api_key=resolve_env_secret("OPENAI_API_KEY_SECRET_ARN", "OPENAI_API_KEY"))


class OpenAIClient:
    """Async wrapper around the OpenAI Chat Completions API with unified error mapping."""

    async def complete(self, prompt: str, model: str, max_tokens: int, system: str | None = None) -> LLMResponse:
        """Send *prompt* to the OpenAI API and return a structured response.

        Args:
            prompt:     User message text.
            model:      OpenAI model identifier.
            max_tokens: Maximum tokens to generate.
            system:     Optional system prompt inserted as a system message.

        Returns:
            LLMResponse with text, token counts, and model identifier.
        """
        messages = []
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
        except openai.RateLimitError as e:
            if "insufficient_quota" in str(e):
                raise HTTPException(
                    status_code=402, detail="OpenAI: insufficient quota — add credits at platform.openai.com"
                )
            raise HTTPException(status_code=429, detail="OpenAI: rate limit exceeded, retry shortly")
        except openai.APIStatusError as e:
            raise HTTPException(status_code=502, detail=f"OpenAI error {e.status_code}: {e.message}")
