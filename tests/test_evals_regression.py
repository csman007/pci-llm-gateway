"""Regression test suite — asserts that all eval suites meet their score floors.

Marked with ``@pytest.mark.eval`` so they run separately from unit/contract tests:

    pytest -m eval                   # eval suite only
    pytest -m "not eval"             # everything except eval
    pytest                           # all tests including eval

These tests use fixture-replay mode (no real API calls, no API cost).
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Bootstrap: ensure service modules and evals package are importable.
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))
for _svc in [
    "pii-detector",
    "prompt-processor",
    "llm-client",
    "output-filter",
    "api-gateway",
    "agent",
    "rag",
    "observability",
]:
    _p = str(_root / "services" / _svc)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evals.harness import load_score_floors, run_agent_suite, run_injection_suite, run_rag_suite


# ── RAG suite ─────────────────────────────────────────────────────────────────


@pytest.mark.eval
def test_rag_citation_recall():
    """RAG answers must cite ≥80% of expected PCI DSS requirement IDs."""
    result = asyncio.run(run_rag_suite())
    floor = load_score_floors()["rag"]["citation_recall"]
    assert result["citation_recall"] >= floor, (
        f"RAG citation_recall {result['citation_recall']:.4f} < floor {floor}. "
        f"Low-recall cases: {[r['case_id'] for r in result['results'] if r['citation_recall'] < floor]}"
    )


@pytest.mark.eval
def test_rag_citation_precision():
    """RAG answers must not hallucinate citations — ≥70% of cited IDs must be relevant."""
    result = asyncio.run(run_rag_suite())
    floor = load_score_floors()["rag"]["citation_precision"]
    assert result["citation_precision"] >= floor, (
        f"RAG citation_precision {result['citation_precision']:.4f} < floor {floor}"
    )


@pytest.mark.eval
def test_rag_keyword_coverage():
    """RAG answers must contain ≥75% of expected answer keywords."""
    result = asyncio.run(run_rag_suite())
    floor = load_score_floors()["rag"]["keyword_coverage"]
    assert result["keyword_coverage"] >= floor, (
        f"RAG keyword_coverage {result['keyword_coverage']:.4f} < floor {floor}"
    )


# ── Agent suite ───────────────────────────────────────────────────────────────


@pytest.mark.eval
def test_agent_tool_recall():
    """Agent must call ≥80% of expected tools per golden case."""
    result = asyncio.run(run_agent_suite())
    floor = load_score_floors()["agent"]["tool_recall"]
    assert result["tool_recall"] >= floor, (
        f"Agent tool_recall {result['tool_recall']:.4f} < floor {floor}. "
        f"Misses: {[r['case_id'] for r in result['results'] if r['tool_recall'] < floor]}"
    )


@pytest.mark.eval
def test_agent_tool_precision():
    """Agent must not call unexpected tools — ≥75% of called tools must be expected."""
    result = asyncio.run(run_agent_suite())
    floor = load_score_floors()["agent"]["tool_precision"]
    assert result["tool_precision"] >= floor, (
        f"Agent tool_precision {result['tool_precision']:.4f} < floor {floor}"
    )


@pytest.mark.eval
def test_agent_keyword_coverage():
    """Agent responses must cover ≥70% of expected answer keywords."""
    result = asyncio.run(run_agent_suite())
    floor = load_score_floors()["agent"]["keyword_coverage"]
    assert result["keyword_coverage"] >= floor, (
        f"Agent keyword_coverage {result['keyword_coverage']:.4f} < floor {floor}"
    )


# ── Injection suite ────────────────────────────────────────────────────────────


@pytest.mark.eval
def test_injection_true_positive_rate():
    """InjectionDetector must block ≥95% of known attack strings."""
    result = run_injection_suite()
    floor = load_score_floors()["injection"]["true_positive_rate"]
    assert result["true_positive_rate"] >= floor, (
        f"Injection TP rate {result['true_positive_rate']:.4f} < floor {floor}. "
        f"Missed attacks: {[r['case_id'] for r in result['results'] if r['expected_blocked'] and not r['actual_blocked']]}"
    )


@pytest.mark.eval
def test_injection_false_positive_rate():
    """InjectionDetector must not block clean PCI DSS queries — FP rate ≤5%."""
    result = run_injection_suite()
    floor = load_score_floors()["injection"]["false_positive_rate"]
    assert result["false_positive_rate"] <= floor, (  # FP rate: lower is better
        f"Injection FP rate {result['false_positive_rate']:.4f} > floor {floor}. "
        f"False positives: {[r['case_id'] for r in result['results'] if not r['expected_blocked'] and r['actual_blocked']]}"
    )
