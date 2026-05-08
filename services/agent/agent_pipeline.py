from detector import PIIDetector
from fastapi import HTTPException
from leakage_detector import LeakageDetector
from policy_engine import PolicyEngine
from redactor import Redactor
from validator import OutputValidator


class AgentPipeline:
    """Applies the PII scan → policy → redact pipeline to agent inputs and validates outputs.

    Instantiates all pipeline collaborators directly from their flat-namespace modules,
    avoiding any import dependency on the api-gateway route layer.
    """

    def __init__(self) -> None:
        self._detector = PIIDetector()
        self._redactor = Redactor()
        self._policy = PolicyEngine()
        self._validator = OutputValidator()
        self._leakage = LeakageDetector()

    def check_and_redact(self, text: str) -> tuple[str, dict[str, str]]:
        """Scan *text* for PII, enforce policy, and return the redacted version.

        Args:
            text: Raw input text (user question or tool input).

        Returns:
            Tuple of (redacted_text, token_map) where token_map maps placeholder
            tokens back to their original values.

        Raises:
            HTTPException 400: if the text contains blocked PII types (PAN, CVV, SSN, EXPIRY)
                or exceeds the PII entity threshold.
        """
        findings = self._detector.scan(text)
        self._policy.enforce(findings)
        redacted, token_map = self._redactor.redact(text, findings)
        return redacted, token_map

    def validate_and_restore(self, response_text: str, token_map: dict[str, str]) -> str:
        """Validate an LLM response for safety and restore redacted tokens.

        Args:
            response_text: Raw text returned by the LLM (may contain placeholder tokens).
            token_map: Token → original value map produced by check_and_redact.

        Returns:
            Restored response text with placeholder tokens swapped back to originals.

        Raises:
            HTTPException 502: if the response fails validation or contains PII leakage.
        """
        if not self._validator.is_valid(response_text):
            raise HTTPException(status_code=502, detail="Agent response failed validation")
        if self._leakage.detected(response_text, token_map):
            raise HTTPException(status_code=502, detail="PII leakage detected in agent response")
        return self._redactor.restore(response_text, token_map)
