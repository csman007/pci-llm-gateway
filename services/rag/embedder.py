import os

from openai import OpenAI

_EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
_EMBEDDING_DIMS = 1536


class EmbeddingClient:
    """Generates text embeddings via OpenAI's embedding API."""

    def __init__(self) -> None:
        self._client = OpenAI()
        self.model = _EMBEDDING_MODEL
        self.dimensions = _EMBEDDING_DIMS

    def embed(self, text: str) -> list[float]:
        """Return an embedding vector for *text*.

        Args:
            text: Raw text to embed (newlines are collapsed before sending).

        Returns:
            List of floats of length self.dimensions.
        """
        response = self._client.embeddings.create(
            model=self.model,
            input=text.replace("\n", " "),
        )
        return response.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for a list of texts in a single API call.

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors, one per input text, in the same order.
        """
        cleaned = [t.replace("\n", " ") for t in texts]
        response = self._client.embeddings.create(model=self.model, input=cleaned)
        return [item.embedding for item in response.data]
