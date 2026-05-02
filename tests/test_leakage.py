import pytest
from leakage_detector import LeakageDetector


@pytest.fixture
def leakage():
    return LeakageDetector()


def test_no_leakage_in_clean_response(leakage):
    assert not leakage.detected("The transaction was approved successfully.", {})


def test_pan_leakage_detected(leakage):
    # LLM echoes a card number that was NOT in the token map
    assert leakage.detected("Your card ending in 4111111111111111 is on file.", {})


def test_allowed_value_not_flagged(leakage):
    # The PAN was already known (it was redacted and its original is in token_map)
    token_map = {"[PAN_ABC12345]": "4111111111111111"}
    # Pre-restore response still contains the placeholder, not the raw PAN
    assert not leakage.detected("Your card [PAN_ABC12345] has been charged.", token_map)


def test_email_leakage_detected(leakage):
    assert leakage.detected("Sending receipt to attacker@evil.com", {})
