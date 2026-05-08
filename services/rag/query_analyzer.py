import json
import os
import re

_ANALYZER_MODEL = os.environ.get("QUERY_ANALYZER_MODEL", "claude-haiku-4-5-20251001")
_ANALYZER_MAX_TOKENS = int(os.environ.get("QUERY_ANALYZER_MAX_TOKENS", "256"))

_REQ_ID_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*$")

_SYSTEM = (
    "You are a PCI DSS v4.0.1 expert. "
    "Given a user question, identify which PCI DSS requirement numbers it most likely relates to. "
    "Respond with a JSON array of requirement ID strings only, e.g. [\"10.5.1\", \"3.4.1\"]. "
    "If no specific requirements apply, return []. "
    "Output ONLY the JSON array — no explanation, no markdown."
)


class QueryAnalyzer:
    """Classifies a natural-language PCI DSS query into relevant requirement IDs.

    Uses a fast Haiku call to map intent → requirement numbers before retrieval
    so that ambiguous queries can be expanded or filtered appropriately.
    """

    def __init__(self, llm_client) -> None:
        """
        Args:
            llm_client: An LLM client exposing complete(prompt, model, max_tokens, system).
        """
        self._llm = llm_client

    async def classify(self, query: str) -> list[str]:
        """Return PCI DSS requirement IDs most relevant to *query*.

        Args:
            query: The user's natural-language question.

        Returns:
            List of requirement ID strings (e.g. ["10.5.1", "3.4.1"]).
            Returns an empty list on LLM errors or unparseable responses.
        """
        try:
            raw = await self._llm.complete(
                prompt=f"Question: {query}",
                model=_ANALYZER_MODEL,
                max_tokens=_ANALYZER_MAX_TOKENS,
                system=_SYSTEM,
            )
            ids = json.loads(raw.strip())
            return [r for r in ids if isinstance(r, str) and _REQ_ID_RE.match(r)]
        except Exception:
            return []
