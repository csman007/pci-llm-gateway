import uuid


class Redactor:
    def redact(self, text: str, findings) -> tuple[str, dict[str, str]]:
        """Replace each finding with a placeholder token. Returns redacted text and token→original map."""
        token_map: dict[str, str] = {}
        # Process in reverse order so offsets stay valid after replacement.
        for finding in sorted(findings, key=lambda f: f.start, reverse=True):
            token = f"[{finding.entity_type}_{uuid.uuid4().hex[:8].upper()}]"
            token_map[token] = finding.text
            text = text[: finding.start] + token + text[finding.end :]
        return text, token_map

    def restore(self, text: str, token_map: dict[str, str]) -> str:
        """Swap placeholder tokens back to originals in LLM output."""
        for token, original in token_map.items():
            text = text.replace(token, original)
        return text
