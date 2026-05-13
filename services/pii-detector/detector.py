from ml_model import PIIClassifier
from patterns import ALL_PATTERNS, Finding, luhn_check


class PIIDetector:
    def __init__(self):
        self.classifier = PIIClassifier()

    def scan(self, text: str) -> list[Finding]:
        findings: list[Finding] = []
        findings.extend(self._regex_scan(text))
        findings.extend(self.classifier.predict(text))
        return self._deduplicate(findings)

    def _regex_scan(self, text: str) -> list[Finding]:
        results = []
        for entity_type, pattern in ALL_PATTERNS.items():
            for match in pattern.finditer(text):
                raw = match.group()
                if entity_type == "PAN" and not luhn_check(raw):
                    continue
                results.append(
                    Finding(
                        entity_type=entity_type,
                        start=match.start(),
                        end=match.end(),
                        text=raw,
                        confidence=0.95,
                    )
                )
        return results

    def _deduplicate(self, findings: list[Finding]) -> list[Finding]:
        findings.sort(key=lambda f: f.start)
        deduped = []
        last_end = -1
        for f in findings:
            if f.start >= last_end:
                deduped.append(f)
                last_end = f.end
        return deduped
