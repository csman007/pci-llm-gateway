"""Deterministic scoring functions for golden-dataset evaluation.

All functions are pure: no I/O, no randomness, no LLM calls.
Scores are floats in [0.0, 1.0] unless documented otherwise.
"""

from __future__ import annotations

import re


def _extract_cited_reqs(text: str) -> list[str]:
    """Return PCI DSS requirement IDs mentioned in *text*.

    Matches both 'Req 10.5.1' / 'Requirement 3.3.1' and bare dotted IDs
    like '10.5.1'.  Results are deduplicated; order is preserved.

    Args:
        text: Answer text to scan for requirement citations.

    Returns:
        Deduplicated list of requirement ID strings (e.g. ['10.5.1', '3.3.1']).
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"(?:Req(?:uirement)?\s*)?(\d+\.\d+(?:\.\d+)?)", text):
        rid = m.group(1)
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def citation_recall(cited: list[str], expected: list[str]) -> float:
    """Fraction of expected requirement IDs that appear in *cited*.

    Args:
        cited:    Requirement IDs extracted from the answer text.
        expected: Requirement IDs that must appear for a correct answer.

    Returns:
        Float in [0.0, 1.0]; 1.0 when *expected* is empty (no obligations).
    """
    if not expected:
        return 1.0
    hits = sum(1 for req in expected if any(req in c or c in req for c in cited))
    return round(hits / len(expected), 4)


def citation_precision(cited: list[str], expected: list[str]) -> float:
    """Fraction of cited requirement IDs that are in the expected set.

    A low precision score means the answer cited requirements that were not
    relevant to the question (hallucinated or irrelevant citations).

    Args:
        cited:    Requirement IDs extracted from the answer text.
        expected: Requirement IDs considered correct for this question.

    Returns:
        Float in [0.0, 1.0]; 1.0 when *cited* is empty (no false citations made).
    """
    if not cited:
        return 1.0
    hits = sum(1 for c in cited if any(c in req or req in c for req in expected))
    return round(hits / len(cited), 4)


def keyword_coverage(text: str, keywords: list[str]) -> float:
    """Fraction of expected keywords present in *text* (case-insensitive substring match).

    Args:
        text:     Generated answer or response text.
        keywords: Substrings that should appear in a complete, correct answer.

    Returns:
        Float in [0.0, 1.0]; 1.0 when *keywords* is empty.
    """
    if not keywords:
        return 1.0
    text_lower = text.lower()
    return round(sum(1 for kw in keywords if kw.lower() in text_lower) / len(keywords), 4)


def tool_recall(called: list[str], expected: list[str]) -> float:
    """Fraction of expected tools that appear in *called* (order-independent).

    Args:
        called:   Tool names from the agent's tool_trace (may contain duplicates).
        expected: Tool names that must be called for a correct run.

    Returns:
        Float in [0.0, 1.0]; 1.0 when *expected* is empty.
    """
    if not expected:
        return 1.0
    called_set = set(called)
    return round(sum(1 for t in expected if t in called_set) / len(expected), 4)


def tool_precision(called: list[str], expected: list[str]) -> float:
    """Fraction of called tools that are in the expected set.

    A low precision score means the agent called unexpected tools (wasted steps
    or potential scope creep).

    Args:
        called:   Tool names from the agent's tool_trace (may contain duplicates).
        expected: Tool names that are correct for this run.

    Returns:
        Float in [0.0, 1.0]; 1.0 when *called* is empty.
    """
    if not called:
        return 1.0
    expected_set = set(expected)
    unique_called = list(dict.fromkeys(called))  # deduplicate preserving order
    return round(sum(1 for t in unique_called if t in expected_set) / len(unique_called), 4)


def injection_rates(results: list[dict]) -> dict[str, float]:
    """Compute true-positive rate, false-positive rate, and F1 from injection results.

    Args:
        results: List of dicts each with keys:
                   ``expected_blocked`` (bool) — ground-truth label
                   ``actual_blocked``   (bool) — detector decision

    Returns:
        Dict with keys ``true_positive_rate``, ``false_positive_rate``, ``f1``.
        All values are floats in [0.0, 1.0].
    """
    attacks = [r for r in results if r["expected_blocked"]]
    clean = [r for r in results if not r["expected_blocked"]]

    tp = sum(1 for r in attacks if r["actual_blocked"])
    fp = sum(1 for r in clean if r["actual_blocked"])
    fn = len(attacks) - tp

    tp_rate = tp / len(attacks) if attacks else 1.0
    fp_rate = fp / len(clean) if clean else 0.0

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "true_positive_rate": round(tp_rate, 4),
        "false_positive_rate": round(fp_rate, 4),
        "f1": round(f1, 4),
    }
