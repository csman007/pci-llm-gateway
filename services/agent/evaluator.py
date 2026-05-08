import json
import os

import anthropic
from prompts import JUDGE_SYSTEM

_JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5-20251001")
_JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "256"))


class LLMJudge:
    """Scores an agent response using a lightweight Haiku call (LLM-as-judge).

    The judge sees the original question and the final restored answer.
    It outputs a JSON object with a 0.0–1.0 score and one-sentence reasoning.
    """

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        """
        Args:
            client: Shared AsyncAnthropic client instance.
        """
        self._client = client

    async def score(self, question: str, answer: str) -> dict:
        """Evaluate *answer* against *question* and return a score dict.

        Args:
            question: The original user question (pre-redaction).
            answer: The final agent response (post-restoration).

        Returns:
            Dict with keys 'score' (float 0.0–1.0) and 'reasoning' (str).
            Falls back to {"score": 0.5, "reasoning": "judge parse error"} on failure.
        """
        prompt = f"Question: {question}\n\nAnswer: {answer}"
        try:
            response = await self._client.messages.create(
                model=_JUDGE_MODEL,
                max_tokens=_JUDGE_MAX_TOKENS,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text_block = next(
                (b for b in response.content if getattr(b, "type", None) == "text"),
                None,
            )
            if text_block is None:
                return {"score": 0.5, "reasoning": "judge returned no text block"}
            parsed = json.loads(text_block.text.strip())
            return {
                "score": float(parsed["score"]),
                "reasoning": str(parsed["reasoning"]),
            }
        except Exception:
            return {"score": 0.5, "reasoning": "judge parse error"}
