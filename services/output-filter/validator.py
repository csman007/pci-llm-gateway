MAX_RESPONSE_CHARS = 16_000
MIN_RESPONSE_CHARS = 1

# Strings that indicate the model refused or errored in a way we should surface.
REFUSAL_MARKERS = [
    "I cannot assist",
    "I'm unable to",
    "As an AI language model, I don't",
]


class OutputValidator:
    def is_valid(self, text: str) -> bool:
        if not (MIN_RESPONSE_CHARS <= len(text) <= MAX_RESPONSE_CHARS):
            return False
        if any(marker.lower() in text.lower() for marker in REFUSAL_MARKERS):
            return False
        return True
