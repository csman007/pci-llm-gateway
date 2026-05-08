import logging
import os

import anthropic
from fastapi import HTTPException
from prompts import ANALYST_SUBAGENT_SYSTEM, COMPLIANCE_SUBAGENT_SYSTEM

log = logging.getLogger(__name__)

_SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "claude-haiku-4-5-20251001")
_SUBAGENT_MAX_TOKENS = int(os.environ.get("SUBAGENT_MAX_TOKENS", "1024"))

_CONFIGS: dict[str, dict] = {
    "compliance": {"system": COMPLIANCE_SUBAGENT_SYSTEM, "max_tokens": _SUBAGENT_MAX_TOKENS},
    "analyst": {"system": ANALYST_SUBAGENT_SYSTEM, "max_tokens": _SUBAGENT_MAX_TOKENS},
}


class SubagentRunner:
    """Runs specialist subagent calls using Haiku with domain-specific system prompts.

    Each subagent is a fresh Claude session with a focused persona — no tool access,
    bounded token budget. Inputs and outputs pass through the shared AgentPipeline
    so PII guarantees are preserved end-to-end.
    """

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        """
        Args:
            client: Shared AsyncAnthropic client instance.
        """
        self._client = client

    async def run(self, agent_name: str, question: str, pipeline) -> str:
        """Invoke a named specialist subagent with *question* and return its answer.

        Args:
            agent_name: Either 'compliance' or 'analyst'.
            question: The specific question or task to delegate.
            pipeline: AgentPipeline — used to redact the question and validate the response.

        Returns:
            The subagent's text response with any placeholder tokens restored,
            or an ERROR: prefixed string if the subagent call fails.
        """
        config = _CONFIGS.get(agent_name)
        if config is None:
            return f"ERROR: unknown subagent '{agent_name}'"

        try:
            redacted_q, token_map = pipeline.check_and_redact(question)
        except HTTPException as exc:
            return f"ERROR: subagent input blocked by PII policy — {exc.detail}"

        # Augment compliance questions with retrieved PCI DSS v4.0.1 context.
        user_content = redacted_q
        if agent_name == "compliance" and os.environ.get("POSTGRES_DSN"):
            try:
                from retriever import RAGRetriever

                retriever = RAGRetriever()
                chunks = await retriever.retrieve(redacted_q)
                if chunks:
                    context = RAGRetriever.format_context(chunks)
                    user_content = f"Relevant PCI DSS v4.0.1 sections:\n\n{context}\n\n---\n\nQuestion: {redacted_q}"
            except Exception as exc:
                log.warning("RAG retrieval failed, falling back to base model: %s", exc)

        try:
            response = await self._client.messages.create(
                model=_SUBAGENT_MODEL,
                max_tokens=config["max_tokens"],
                system=config["system"],
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = response.content[0].text
        except anthropic.APIError as exc:
            return f"ERROR: subagent API failure — {exc}"

        try:
            return pipeline.validate_and_restore(raw_text, token_map)
        except HTTPException as exc:
            return f"ERROR: subagent response failed safety check — {exc.detail}"
