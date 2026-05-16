"""Fixture-based replay system for deterministic offline evaluation.

Record mode (``--record`` flag on the CLI): real API calls are made and
responses are persisted to ``evals/fixtures/<suite>.json``.

Replay mode (default): pre-recorded responses are served from the fixture
file; no network calls are made and no API costs are incurred.

Fixture keys are case IDs (strings from the golden YAML), which makes the
fixture files human-readable and allows intentional regeneration of individual
cases without resetting the entire suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Fixture store ──────────────────────────────────────────────────────────────


class FixtureStore:
    """Loads and persists evaluation fixtures for one suite (rag or agent)."""

    def __init__(self, suite: str) -> None:
        """
        Args:
            suite: Suite name — determines the fixture file path
                   (e.g. ``'rag'`` → ``evals/fixtures/rag.json``).
        """
        self._path = _FIXTURES_DIR / f"{suite}.json"
        self._data: dict[str, Any] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def get(self, case_id: str) -> dict | None:
        """Return the fixture for *case_id*, or ``None`` if not yet recorded.

        Args:
            case_id: Golden-dataset case identifier (e.g. ``'rag_001'``).

        Returns:
            Fixture dict, or ``None`` when the case has no recorded entry.
        """
        return self._data.get(case_id)

    def put(self, case_id: str, fixture: dict) -> None:
        """Store *fixture* under *case_id* and flush to disk.

        Args:
            case_id: Golden-dataset case identifier.
            fixture: Dict to store (must be JSON-serialisable).
        """
        self._data[case_id] = fixture
        _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))


# ── RAG replay objects ─────────────────────────────────────────────────────────


class ReplayLLMClient:
    """LLM client that returns a pre-recorded LLMResponse, ignoring all arguments."""

    def __init__(self, fixture: dict) -> None:
        """
        Args:
            fixture: Fixture dict with a ``'llm_response'`` key whose value
                     has ``text``, ``prompt_tokens``, ``completion_tokens``,
                     and ``model`` fields.
        """
        from llm_response import LLMResponse

        r = fixture["llm_response"]
        self._response = LLMResponse(
            text=r["text"],
            prompt_tokens=r["prompt_tokens"],
            completion_tokens=r["completion_tokens"],
            model=r["model"],
        )

    async def complete(self, prompt: str, model: str, max_tokens: int, system: str = "") -> Any:
        """Return the pre-recorded response regardless of inputs.

        Args:
            prompt:     Ignored.
            model:      Ignored.
            max_tokens: Ignored.
            system:     Ignored.

        Returns:
            Pre-recorded LLMResponse.
        """
        return self._response


class ReplayRetriever:
    """RAG retriever that returns pre-recorded chunks, ignoring query and top_k."""

    def __init__(self, fixture: dict) -> None:
        """
        Args:
            fixture: Fixture dict with a ``'chunks'`` key containing the chunk list.
        """
        self._chunks: list[dict] = fixture["chunks"]

    async def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """Return pre-recorded chunks regardless of inputs.

        Args:
            query: Ignored.
            top_k: Ignored.

        Returns:
            Pre-recorded chunk list.
        """
        return self._chunks


# ── Agent replay objects ───────────────────────────────────────────────────────


@dataclass
class _FakeBlock:
    """Minimal content block duck-typing the Anthropic SDK content block types.

    Attributes:
        type:     ``'text'``, ``'tool_use'``, or ``'thinking'``.
        text:     Text content (text blocks).
        id:       Tool-use ID (tool_use blocks).
        name:     Tool name (tool_use blocks).
        input:    Tool input dict (tool_use blocks).
        thinking: Thinking text (thinking blocks).
    """

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    thinking: str = ""


@dataclass
class _FakeMessage:
    """Minimal Anthropic Message duck-typing for the AgentOrchestrator."""

    content: list[_FakeBlock]
    stop_reason: str


class ReplayAnthropicClient:
    """Anthropic client that returns fixture turns in sequence.

    Each call to ``messages.create()`` pops the next pre-recorded turn.
    When all turns are exhausted, a synthetic ``end_turn`` is returned so
    the orchestrator exits cleanly rather than raising an exception.
    """

    def __init__(self, fixture: dict) -> None:
        """
        Args:
            fixture: Fixture dict with a ``'turns'`` key — a list of dicts,
                     each with ``stop_reason`` (str) and ``content_blocks``
                     (list of _FakeBlock-compatible dicts).
        """
        self._turns: list[dict] = list(fixture["turns"])
        self._pos: int = 0
        self.messages = _MessagesProxy(self)

    def _next_message(self) -> _FakeMessage:
        """Return the next turn, or a synthetic end_turn if exhausted."""
        if self._pos >= len(self._turns):
            return _FakeMessage(
                content=[_FakeBlock(type="text", text="All fixture turns consumed.")],
                stop_reason="end_turn",
            )
        turn = self._turns[self._pos]
        self._pos += 1
        return _FakeMessage(
            content=[_FakeBlock(**b) for b in turn["content_blocks"]],
            stop_reason=turn["stop_reason"],
        )


class _MessagesProxy:
    """Proxy that exposes ``create()`` on ``ReplayAnthropicClient.messages``."""

    def __init__(self, client: ReplayAnthropicClient) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> _FakeMessage:
        """Return the next pre-recorded turn.

        Args:
            **kwargs: All orchestrator kwargs are ignored in replay mode.

        Returns:
            _FakeMessage with content blocks and stop_reason from the fixture.
        """
        return self._client._next_message()
