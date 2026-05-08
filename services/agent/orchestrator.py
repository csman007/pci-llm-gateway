from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import anthropic
from agent_pipeline import AgentPipeline
from evaluator import LLMJudge
from fastapi import HTTPException
from prompts import ORCHESTRATOR_SYSTEM
from subagents import SubagentRunner
from tools import TOOL_DEFINITIONS, execute_tool

_MODEL_DEFAULT = os.environ.get("AGENT_MODEL_DEFAULT", "claude-sonnet-4-6")
_MODEL_THINKING = os.environ.get("AGENT_MODEL_THINKING", "claude-opus-4-7")
_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "10"))
_THINKING_BUDGET_TOKENS = int(os.environ.get("AGENT_THINKING_BUDGET_TOKENS", "8000"))


class AgentOrchestrator:
    """Runs a multi-step Claude tool-use loop for PCI compliance queries.

    Supports two execution modes:
    - sync (run): completes all tool calls and returns a full AgentRunResult.
    - streaming (stream): yields SSE-ready dicts as the agent works.

    All inputs and outputs pass through AgentPipeline for PII safety.
    Extended thinking is available when use_thinking=True (requires Opus model).
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        pipeline: AgentPipeline,
        subagent_runner: SubagentRunner,
        judge: LLMJudge,
    ) -> None:
        """
        Args:
            client: Shared AsyncAnthropic client.
            pipeline: PII scan/redact/validate pipeline.
            subagent_runner: Runner for specialist subagent calls.
            judge: LLM-as-judge evaluator.
        """
        self._client = client
        self._pipeline = pipeline
        self._subagent_runner = subagent_runner
        self._judge = judge

    async def run(
        self, question: str, use_thinking: bool = False, max_tokens: int = 4096
    ) -> dict:
        """Run the agent to completion and return a structured result.

        Args:
            question: The user's question or task.
            use_thinking: Enable extended thinking (uses Opus, slower but deeper).
            max_tokens: Maximum tokens for the orchestrator's responses.

        Returns:
            Dict with keys: response, tool_trace, thinking (or None), evaluation, steps_taken.

        Raises:
            HTTPException 400: if the question contains blocked PII.
            HTTPException 502: if the final response fails safety checks.
        """
        redacted_q, outer_token_map = self._pipeline.check_and_redact(question)
        model = _MODEL_THINKING if use_thinking else _MODEL_DEFAULT

        messages: list[dict] = [{"role": "user", "content": redacted_q}]
        tool_trace: list[dict] = []
        thinking_blocks: list[str] = []

        kwargs = self._build_kwargs(model, max_tokens, use_thinking)

        for step in range(_MAX_STEPS):
            response = await self._client.messages.create(
                messages=messages, **kwargs
            )

            self._collect_thinking(response.content, thinking_blocks)

            if response.stop_reason == "end_turn":
                final_text = self._extract_text(response.content)
                restored = self._pipeline.validate_and_restore(
                    final_text, outer_token_map
                )
                evaluation = await self._judge.score(question, restored)
                return {
                    "response": restored,
                    "tool_trace": tool_trace,
                    "thinking": thinking_blocks if use_thinking else None,
                    "evaluation": evaluation,
                    "steps_taken": step + 1,
                }

            if response.stop_reason == "tool_use":
                tool_results = await self._execute_tool_uses(
                    response.content, tool_trace
                )
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            break  # unexpected stop_reason — exit loop and synthesise

        # Graceful degradation: force a final text response after MAX_STEPS.
        messages.append(
            {
                "role": "user",
                "content": (
                    "You have used the maximum number of tool calls. "
                    "Please summarise your findings so far."
                ),
            }
        )
        final_kwargs = {**kwargs}
        final_kwargs.pop("tools", None)
        response = await self._client.messages.create(
            messages=messages, **final_kwargs
        )
        final_text = self._extract_text(response.content)
        restored = self._pipeline.validate_and_restore(final_text, outer_token_map)
        evaluation = await self._judge.score(question, restored)
        return {
            "response": restored,
            "tool_trace": tool_trace,
            "thinking": thinking_blocks if use_thinking else None,
            "evaluation": evaluation,
            "steps_taken": _MAX_STEPS,
        }

    async def stream(
        self, question: str, use_thinking: bool = False, max_tokens: int = 4096
    ) -> AsyncIterator[dict]:
        """Stream the agent's work as SSE-ready dicts.

        Yields events of types: thinking, tool_call, tool_result, text_delta,
        evaluation, done.

        Args:
            question: The user's question or task.
            use_thinking: Enable extended thinking blocks in the stream.
            max_tokens: Maximum tokens for the orchestrator's responses.

        Yields:
            Dicts suitable for serialisation as SSE data payloads.

        Raises:
            HTTPException 400: if the question contains blocked PII.
        """
        redacted_q, outer_token_map = self._pipeline.check_and_redact(question)
        model = _MODEL_THINKING if use_thinking else _MODEL_DEFAULT

        messages: list[dict] = [{"role": "user", "content": redacted_q}]
        tool_trace: list[dict] = []
        accumulated_response: list = []
        kwargs = self._build_kwargs(model, max_tokens, use_thinking)

        for _ in range(_MAX_STEPS):
            # Accumulate the full streamed turn before executing tools.
            current_tool_inputs: dict[str, dict] = {}  # tool_use_id → {name, input_str}
            content_blocks: list = []
            stop_reason = "end_turn"

            async with self._client.messages.stream(
                messages=messages, **kwargs
            ) as stream:
                async for event in stream:
                    event_type = type(event).__name__

                    if event_type == "RawContentBlockStartEvent":
                        block = event.content_block
                        if block.type == "thinking":
                            content_blocks.append({"type": "thinking", "thinking": ""})
                        elif block.type == "text":
                            content_blocks.append({"type": "text", "text": ""})
                        elif block.type == "tool_use":
                            content_blocks.append(
                                {"type": "tool_use", "id": block.id, "name": block.name, "input": {}}
                            )
                            current_tool_inputs[block.id] = {"name": block.name, "input_str": ""}

                    elif event_type == "RawContentBlockDeltaEvent":
                        delta = event.delta
                        idx = event.index
                        if delta.type == "thinking_delta":
                            content_blocks[idx]["thinking"] += delta.thinking
                            yield {"type": "thinking", "text": delta.thinking}
                        elif delta.type == "text_delta":
                            content_blocks[idx]["text"] += delta.text
                            yield {"type": "text_delta", "text": delta.text}
                        elif delta.type == "input_json_delta":
                            tid = content_blocks[idx]["id"]
                            current_tool_inputs[tid]["input_str"] += delta.partial_json

                    elif event_type == "RawMessageDeltaEvent":
                        stop_reason = event.delta.stop_reason or stop_reason

            # Finalise tool_use input JSON now that streaming is complete.
            for block in content_blocks:
                if block["type"] == "tool_use":
                    tid = block["id"]
                    try:
                        block["input"] = json.loads(
                            current_tool_inputs[tid]["input_str"] or "{}"
                        )
                    except json.JSONDecodeError:
                        block["input"] = {}

            accumulated_response = content_blocks

            if stop_reason == "end_turn":
                break

            if stop_reason == "tool_use":
                tool_results = []
                for block in content_blocks:
                    if block["type"] != "tool_use":
                        continue
                    yield {"type": "tool_call", "name": block["name"], "input": block["input"]}
                    result = await execute_tool(
                        block["name"],
                        block["input"],
                        subagent_runner=self._subagent_runner,
                        pipeline=self._pipeline,
                    )
                    tool_trace.append(
                        {
                            "tool_name": block["name"],
                            "tool_input": block["input"],
                            "tool_result": result,
                            "error": result.startswith("ERROR:"),
                        }
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": result,
                        }
                    )
                    yield {"type": "tool_result", "name": block["name"], "result": result}

                # Rebuild messages list with proper Anthropic content block format.
                messages.append({"role": "assistant", "content": content_blocks})
                messages.append({"role": "user", "content": tool_results})

        # Extract and restore the final text.
        final_text = "".join(
            b.get("text", "") for b in accumulated_response if b["type"] == "text"
        )
        try:
            restored = self._pipeline.validate_and_restore(final_text, outer_token_map)
        except HTTPException as exc:
            yield {"type": "error", "detail": exc.detail}
            yield {"type": "done"}
            return

        evaluation = await self._judge.score(question, restored)
        yield {"type": "evaluation", "score": evaluation["score"], "reasoning": evaluation["reasoning"]}
        yield {"type": "done"}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _build_kwargs(self, model: str, max_tokens: int, use_thinking: bool) -> dict:
        """Build the base kwargs dict for messages.create.

        Args:
            model: Claude model ID.
            max_tokens: Max tokens for the response.
            use_thinking: Whether to enable extended thinking.

        Returns:
            Dict of keyword arguments (excludes 'messages').
        """
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": ORCHESTRATOR_SYSTEM,
            "tools": TOOL_DEFINITIONS,
        }
        if use_thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": _THINKING_BUDGET_TOKENS}
        return kwargs

    def _collect_thinking(self, content: list, accumulator: list[str]) -> None:
        """Append any thinking block texts from *content* into *accumulator*.

        Args:
            content: List of content blocks from an Anthropic response.
            accumulator: List to append thinking text into (mutated in place).
        """
        for block in content:
            if hasattr(block, "type") and block.type == "thinking":
                accumulator.append(block.thinking)

    def _extract_text(self, content: list) -> str:
        """Return the concatenated text from all text blocks in *content*.

        Args:
            content: List of content blocks from an Anthropic response.

        Returns:
            Concatenated text string, empty string if no text blocks present.
        """
        return "".join(
            block.text for block in content if hasattr(block, "type") and block.type == "text"
        )

    async def _execute_tool_uses(self, content: list, trace: list) -> list[dict]:
        """Execute all tool_use blocks in *content* and return tool_result messages.

        Args:
            content: List of content blocks containing tool_use entries.
            trace: Tool trace list to append ToolCallRecord-shaped dicts to (mutated).

        Returns:
            List of tool_result content blocks ready for the next user message.
        """
        results = []
        for block in content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue
            result = await execute_tool(
                block.name,
                block.input,
                subagent_runner=self._subagent_runner,
                pipeline=self._pipeline,
            )
            trace.append(
                {
                    "tool_name": block.name,
                    "tool_input": block.input,
                    "tool_result": result,
                    "error": result.startswith("ERROR:"),
                }
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )
        return results
