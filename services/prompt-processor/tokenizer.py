import tiktoken

_DEFAULT_ENCODING = "cl100k_base"


class PromptTokenizer:
    def __init__(self, encoding: str = _DEFAULT_ENCODING):
        self._enc = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))

    def truncate(self, text: str, max_tokens: int) -> str:
        tokens = self._enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return self._enc.decode(tokens[:max_tokens])

    def chunk(self, text: str, chunk_size: int) -> list[str]:
        tokens = self._enc.encode(text)
        return [self._enc.decode(tokens[i : i + chunk_size]) for i in range(0, len(tokens), chunk_size)]
