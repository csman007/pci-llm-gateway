import anthropic
from fastapi import HTTPException
from secret_resolver import resolve_env_secret

_client = anthropic.AsyncAnthropic(
    api_key=resolve_env_secret("ANTHROPIC_API_KEY_SECRET_ARN", "ANTHROPIC_API_KEY")
)


class AnthropicClient:
    async def complete(self, prompt: str, model: str, max_tokens: int, system: str | None = None) -> str:
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        try:
            message = await _client.messages.create(**kwargs)
            return message.content[0].text
        except anthropic.AuthenticationError:
            raise HTTPException(status_code=401, detail="Anthropic: invalid API key")
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e):
                raise HTTPException(status_code=402, detail="Anthropic: insufficient credits — add funds at console.anthropic.com")
            raise HTTPException(status_code=400, detail=f"Anthropic: {e.message}")
        except anthropic.RateLimitError:
            raise HTTPException(status_code=429, detail="Anthropic: rate limit exceeded, retry shortly")
        except anthropic.APIStatusError as e:
            raise HTTPException(status_code=502, detail=f"Anthropic error {e.status_code}: {e.message}")
