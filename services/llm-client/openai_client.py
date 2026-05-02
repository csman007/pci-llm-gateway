import os
import openai
from fastapi import HTTPException
from openai import AsyncOpenAI
from secret_resolver import resolve_env_secret

_client = AsyncOpenAI(
    api_key=resolve_env_secret("OPENAI_API_KEY_SECRET_ARN", "OPENAI_API_KEY")
)


class OpenAIClient:
    async def complete(self, prompt: str, model: str, max_tokens: int, system: str | None = None) -> str:
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
            return response.choices[0].message.content
        except openai.AuthenticationError:
            raise HTTPException(status_code=401, detail="OpenAI: invalid API key")
        except openai.RateLimitError as e:
            if "insufficient_quota" in str(e):
                raise HTTPException(status_code=402, detail="OpenAI: insufficient quota — add credits at platform.openai.com")
            raise HTTPException(status_code=429, detail="OpenAI: rate limit exceeded, retry shortly")
        except openai.APIStatusError as e:
            raise HTTPException(status_code=502, detail=f"OpenAI error {e.status_code}: {e.message}")
