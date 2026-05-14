"""Unified return value from any LLM client complete() call."""

from dataclasses import dataclass


@dataclass
class LLMResponse:
    """Unified return value from any LLM client complete() call."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
