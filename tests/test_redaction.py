import pytest
from detector import PIIDetector
from redactor import Redactor


@pytest.fixture
def detector():
    return PIIDetector()


@pytest.fixture
def redactor():
    return Redactor()


def test_pan_detected(detector):
    findings = detector.scan("Please charge card 4111111111111111 for the order.")
    assert any(f.entity_type == "PAN" for f in findings)


def test_invalid_luhn_not_detected(detector):
    findings = detector.scan("The number 1234567890123456 is not a valid card.")
    assert not any(f.entity_type == "PAN" for f in findings)


def test_ssn_detected(detector):
    findings = detector.scan("SSN: 123-45-6789")
    assert any(f.entity_type == "SSN" for f in findings)


def test_email_detected(detector):
    findings = detector.scan("Contact john.doe@example.com for support.")
    assert any(f.entity_type == "EMAIL" for f in findings)


def test_cvv_detected(detector):
    findings = detector.scan("The cvv: 123 is wrong.")
    assert any(f.entity_type == "CVV" for f in findings)


def test_cvv_without_keyword_not_detected(detector):
    findings = detector.scan("The code is 123.")
    assert not any(f.entity_type == "CVV" for f in findings)


def test_expiry_detected(detector):
    findings = detector.scan("My card expiry: 09/26 is coming up.")
    assert any(f.entity_type == "EXPIRY" for f in findings)


def test_expiry_invalid_month_not_detected(detector):
    findings = detector.scan("expiry: 13/26")
    assert not any(f.entity_type == "EXPIRY" for f in findings)


def test_expiry_without_keyword_not_detected(detector):
    findings = detector.scan("The date is 09/26.")
    assert not any(f.entity_type == "EXPIRY" for f in findings)


def test_redact_and_restore(detector, redactor):
    original = "Email me at test@example.com tomorrow."
    findings = detector.scan(original)
    redacted, token_map = redactor.redact(original, findings)

    assert "test@example.com" not in redacted
    assert len(token_map) == 1

    restored = redactor.restore(redacted, token_map)
    assert restored == original


def test_redact_multiple_entities(detector, redactor):
    text = "Card 4111111111111111 belongs to jane@corp.com."
    findings = detector.scan(text)
    redacted, token_map = redactor.redact(text, findings)
    assert "4111111111111111" not in redacted
    assert "jane@corp.com" not in redacted
    assert len(token_map) == 2
