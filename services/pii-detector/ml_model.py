from __future__ import annotations
from patterns import Finding

# Lazy import so the gateway starts without heavy ML deps if the model is unused.
try:
    from transformers import pipeline
    _HF_AVAILABLE = True
except ImportError:
    _HF_AVAILABLE = False

_NER_MODEL = "Jean-Baptiste/roberta-large-ner-english"

PII_LABELS = {"PER", "ORG", "LOC", "MISC"}


class PIIClassifier:
    def __init__(self):
        self._pipe = None
        if _HF_AVAILABLE:
            self._pipe = pipeline("ner", model=_NER_MODEL, aggregation_strategy="simple")

    def predict(self, text: str) -> list[Finding]:
        if self._pipe is None:
            return []
        results = self._pipe(text)
        return [
            Finding(
                entity_type=r["entity_group"],
                start=r["start"],
                end=r["end"],
                text=r["word"],
                confidence=round(r["score"], 4),
            )
            for r in results
            if r["entity_group"] in PII_LABELS and r["score"] >= 0.80
        ]
