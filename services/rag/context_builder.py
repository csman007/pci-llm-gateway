import os

_SEPARATOR = "\n\n" + "═" * 70 + "\n\n"
_MAX_CHUNK_CHARS = int(os.environ.get("CONTEXT_MAX_CHUNK_CHARS", "2000"))


class ContextBuilder:
    """Formats retrieved PCI DSS chunks into structured LLM context blocks.

    Each block labels the requirement prominently and includes a relevance score
    so the LLM can weight evidence when forming its answer.
    """

    def build(self, chunks: list[dict]) -> str:
        """Render *chunks* as a structured multi-section context string.

        Args:
            chunks: Output of RAGRetriever.retrieve() — dicts with at least
                    requirement_id, section_title, chunk_text, score.

        Returns:
            Multi-section string ready for injection into an LLM prompt.
            Returns a sentinel string when *chunks* is empty.
        """
        if not chunks:
            return "No relevant PCI DSS sections found."

        parts = []
        for chunk in chunks:
            req = chunk.get("requirement_id") or "General"
            title = chunk.get("section_title") or f"Requirement {req}"
            score = round(chunk.get("score", 0.0), 3)
            text = (chunk.get("chunk_text") or "").strip()[:_MAX_CHUNK_CHARS]

            parts.append(f"[PCI DSS v4.0.1 — Requirement {req}]\nSection:   {title}\nRelevance: {score}\n\n{text}")

        return _SEPARATOR.join(parts)
