import re
from dataclasses import dataclass


@dataclass
class Finding:
    entity_type: str
    start: int
    end: int
    text: str
    confidence: float


# PAN: 13-19 digit sequences passing Luhn check
PAN_PATTERN = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# SSN: 123-45-6789 or 123456789
SSN_PATTERN = re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")

# Email
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# US phone
PHONE_PATTERN = re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")

# CVV (3-4 digits near card-related keywords)
CVV_CONTEXT_PATTERN = re.compile(r"(?:cvv|cvc|security\s+code)[:\s]*(\d{3,4})", re.IGNORECASE)

# Expiry: keyword context + MM/YY or MM/YYYY, month must be 01-12
EXPIRY_PATTERN = re.compile(
    r"(?:expir(?:y|es?|ation)|exp(?:\s*date)?|valid\s+(?:thru|through|until))[:\s]*"
    r"(0[1-9]|1[0-2])[/\-](2[4-9]|[3-9]\d|\d{4})",
    re.IGNORECASE,
)

ALL_PATTERNS = {
    "PAN": PAN_PATTERN,
    "SSN": SSN_PATTERN,
    "EMAIL": EMAIL_PATTERN,
    "PHONE": PHONE_PATTERN,
    "CVV": CVV_CONTEXT_PATTERN,
    "EXPIRY": EXPIRY_PATTERN,
}


def luhn_check(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    digits.reverse()
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0
