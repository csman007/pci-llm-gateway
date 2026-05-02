from detector import PIIDetector

_detector = PIIDetector()


class LeakageDetector:
    def detected(self, response_text: str, token_map: dict[str, str]) -> bool:
        """Return True if the response contains PII not introduced via token restoration."""
        # Scan the raw (pre-restore) response for any new PII findings.
        findings = _detector.scan(response_text)
        if not findings:
            return False

        # Allow findings whose text matches a known placeholder value —
        # those will be restored legitimately.
        allowed_values = set(token_map.values())
        for finding in findings:
            if finding.text not in allowed_values:
                return True
        return False
