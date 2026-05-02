import pytest
from fastapi import HTTPException
from validator import OutputValidator
from policy_engine import PolicyEngine
from patterns import Finding


@pytest.fixture
def validator():
    return OutputValidator()


@pytest.fixture
def policy():
    return PolicyEngine()


def _finding(entity_type: str) -> Finding:
    return Finding(entity_type=entity_type, start=0, end=5, text="dummy", confidence=0.99)


# ── OutputValidator ────────────────────────────────────────────────────────

def test_valid_response(validator):
    assert validator.is_valid("The answer is 42.")


def test_empty_response_invalid(validator):
    assert not validator.is_valid("")


def test_refusal_marker_invalid(validator):
    assert not validator.is_valid("I cannot assist with that request.")


def test_another_refusal_marker_invalid(validator):
    assert not validator.is_valid("I'm unable to help with that.")


def test_response_too_long_invalid(validator):
    assert not validator.is_valid("x" * 16_001)


# ── PolicyEngine ───────────────────────────────────────────────────────────

def test_no_findings_passes(policy):
    policy.enforce([])  # should not raise


def test_pan_blocked(policy):
    with pytest.raises(HTTPException) as info:
        policy.enforce([_finding("PAN")])
    assert info.value.status_code == 400
    assert "PAN" in info.value.detail


def test_cvv_blocked(policy):
    with pytest.raises(HTTPException) as info:
        policy.enforce([_finding("CVV")])
    assert info.value.status_code == 400


def test_ssn_blocked(policy):
    with pytest.raises(HTTPException) as info:
        policy.enforce([_finding("SSN")])
    assert info.value.status_code == 400


def test_expiry_blocked(policy):
    with pytest.raises(HTTPException) as info:
        policy.enforce([_finding("EXPIRY")])
    assert info.value.status_code == 400


def test_email_not_blocked(policy):
    policy.enforce([_finding("EMAIL")])  # should not raise


def test_exceeds_threshold_blocked(policy):
    findings = [_finding("EMAIL") for _ in range(6)]
    with pytest.raises(HTTPException) as info:
        policy.enforce(findings)
    assert info.value.status_code == 400
    assert "limit" in info.value.detail
