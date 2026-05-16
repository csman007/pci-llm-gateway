"""Offline evaluation harness — runs golden-dataset suites against fixture-replayed pipelines.

Three suites:
  rag       — RAGPipeline scored on citation recall/precision and keyword coverage
  agent     — AgentOrchestrator scored on tool recall/precision and keyword coverage
  injection — InjectionDetector scored on true-positive / false-positive rates

Entry point: run_all() returns an EvalReport dataclass.  The CLI (scripts/run_evals.py)
calls run_all() and exits non-zero when any suite falls below its score floor.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import yaml

# ── sys.path bootstrap ─────────────────────────────────────────────────────────
# Ensures service modules are importable whether the harness is invoked from
# pytest (which uses pyproject.toml pythonpath) or from the CLI script.
_root = Path(__file__).parent.parent
for _svc in ["pii-detector", "prompt-processor", "llm-client", "output-filter",
             "api-gateway", "agent", "rag", "observability"]:
    _p = str(_root / "services" / _svc)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evals.metrics import (  # noqa: E402 (after sys.path setup)
    _extract_cited_reqs,
    citation_precision,
    citation_recall,
    injection_rates,
    keyword_coverage,
    tool_precision,
    tool_recall,
)
from evals.replay import FixtureStore, ReplayAnthropicClient, ReplayLLMClient, ReplayRetriever  # noqa: E402

_GOLDEN_DIR = Path(__file__).parent / "golden"
_FLOORS_PATH = Path(__file__).parent / "score_floors.yaml"


# ── Score floor loader ─────────────────────────────────────────────────────────


def load_score_floors() -> dict[str, dict[str, float]]:
    """Load ``evals/score_floors.yaml`` and return the thresholds dict.

    Returns:
        Dict keyed by suite name (``'rag'``, ``'agent'``, ``'injection'``),
        each value being a dict of metric → minimum acceptable score.
    """
    return yaml.safe_load(_FLOORS_PATH.read_text())


# ── Result container ───────────────────────────────────────────────────────────


@dataclass
class EvalReport:
    """Aggregated results for one eval run across all requested suites.

    Attributes:
        rag:       RAG suite aggregate metrics, or None if not run.
        agent:     Agent suite aggregate metrics, or None if not run.
        injection: Injection suite metrics, or None if not run.
        floors:    Score floors loaded from score_floors.yaml.
        passed:    True when every run suite meets all its floor thresholds.
    """

    rag: dict | None
    agent: dict | None
    injection: dict | None
    floors: dict
    passed: bool = field(init=False)

    def __post_init__(self) -> None:
        self.passed = self._check_floors()

    # Metrics where lower is better — these are upper bounds, not lower bounds.
    _UPPER_BOUND_METRICS: frozenset[str] = frozenset({"false_positive_rate"})

    def _metric_passes(self, metric: str, actual: float, floor: float) -> bool:
        """Return True when *actual* satisfies the floor for *metric*.

        Args:
            metric: Metric name (e.g. ``'citation_recall'``, ``'false_positive_rate'``).
            actual: Measured value.
            floor:  Threshold from score_floors.yaml.

        Returns:
            True when the metric is within acceptable bounds.
        """
        if metric in self._UPPER_BOUND_METRICS:
            return actual <= floor  # lower is better — floor is a ceiling
        return actual >= floor      # higher is better — floor is a minimum

    def _check_floors(self) -> bool:
        """Return True when every run suite meets all its floor thresholds."""
        checks = [
            (self.rag, self.floors.get("rag", {})),
            (self.agent, self.floors.get("agent", {})),
            (self.injection, self.floors.get("injection", {})),
        ]
        for results, suite_floors in checks:
            if results is None:
                continue
            for metric, floor in suite_floors.items():
                if not self._metric_passes(metric, results.get(metric, 0.0), floor):
                    return False
        return True

    def failures(self) -> list[str]:
        """Return a list of human-readable failure descriptions for metrics that miss their floor.

        Returns:
            List of strings like ``'rag.citation_recall 0.72 < floor 0.80'``.
            Empty when passed is True.
        """
        out: list[str] = []
        pairs = [("rag", self.rag), ("agent", self.agent), ("injection", self.injection)]
        for suite_name, results in pairs:
            if results is None:
                continue
            for metric, floor in self.floors.get(suite_name, {}).items():
                actual = results.get(metric, 0.0)
                if not self._metric_passes(metric, actual, floor):
                    op = ">" if metric in self._UPPER_BOUND_METRICS else "<"
                    out.append(f"{suite_name}.{metric} {actual:.4f} {op} floor {floor:.2f}")
        return out


# ── Stub collaborators used in both RAG and agent suites ──────────────────────


class _NoopQueryAnalyzer:
    """Query analyzer stub that returns an empty hint list without an LLM call."""

    async def classify(self, question: str) -> list[str]:
        """Return empty requirement hints (no-op in eval mode).

        Args:
            question: Ignored.

        Returns:
            Empty list.
        """
        return []


class _PassthroughPipeline:
    """AgentPipeline stub that passes text through without PII scanning or redaction.

    Used in the agent harness so the orchestrator logic is exercised without
    requiring a real PIIDetector / Redactor stack.
    """

    def check_and_redact(self, text: str) -> tuple[str, dict]:
        """Return text unchanged with an empty token map.

        Args:
            text: Input text.

        Returns:
            (text, {}) — passthrough.
        """
        return text, {}

    def validate_and_restore(self, text: str, token_map: dict) -> str:
        """Return text unchanged.

        Args:
            text:      Response text.
            token_map: Ignored.

        Returns:
            *text* unchanged.
        """
        return text


class _NoopSubagentRunner:
    """SubagentRunner stub that echoes the question without a real LLM call."""

    async def run(self, agent: str, question: str, pipeline: Any) -> str:
        """Return a stub result without calling a real subagent.

        Args:
            agent:    Subagent type (e.g. ``'compliance'``).
            question: Question forwarded to the subagent.
            pipeline: Ignored.

        Returns:
            Stub result string.
        """
        return f"[stub subagent {agent}]: {question}"


class _FixedJudge:
    """LLMJudge stub that returns a fixed score without an LLM call."""

    async def score(self, question: str, answer: str) -> dict:
        """Return a fixed 0.9 score.

        Args:
            question: Ignored.
            answer:   Ignored.

        Returns:
            Dict with ``score: 0.9`` and a note that this is a fixture judge.
        """
        return {"score": 0.9, "reasoning": "fixture-judge — deterministic replay"}


# ── RAG suite ─────────────────────────────────────────────────────────────────


async def run_rag_suite(record: bool = False) -> dict[str, Any]:
    """Run all RAG golden cases and return aggregate metrics.

    Args:
        record: When True, missing fixtures trigger real API calls and the
                responses are saved to evals/fixtures/rag.json.  When False
                (default), missing fixtures raise RuntimeError.

    Returns:
        Dict with per-case results and aggregate metrics:
        ``citation_recall``, ``citation_precision``, ``keyword_coverage``, ``n``.
    """
    from rag_pipeline import RAGPipeline

    cases: list[dict] = yaml.safe_load((_GOLDEN_DIR / "rag_cases.yaml").read_text())
    store = FixtureStore("rag")
    results: list[dict] = []

    for case in cases:
        fixture = store.get(case["id"])
        if fixture is None:
            if not record:
                raise RuntimeError(
                    f"No fixture for {case['id']}. Run with --record to populate."
                )
            continue  # record mode: skip for now (real recording not implemented here)

        llm_client = ReplayLLMClient(fixture)
        # Patch RAGRetriever at class level so __init__ doesn't attempt to
        # create an OpenAI embedding client before we can swap in the mock.
        with patch("rag_pipeline.RAGRetriever"):
            pipeline = RAGPipeline(llm_client)
        pipeline._retriever = ReplayRetriever(fixture)  # type: ignore[assignment]
        pipeline._query_analyzer = _NoopQueryAnalyzer()  # type: ignore[assignment]

        result = await pipeline.query(
            question=case["question"],
            model=case.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=case.get("max_tokens", 512),
        )

        answer = result["answer"]
        cited = _extract_cited_reqs(answer)
        expected_reqs = case.get("expected_requirement_ids", [])
        expected_kws = case.get("expected_keywords", [])

        results.append({
            "case_id": case["id"],
            "question": case["question"],
            "answer": answer,
            "cited": cited,
            "expected_requirement_ids": expected_reqs,
            "citation_recall": citation_recall(cited, expected_reqs),
            "citation_precision": citation_precision(cited, expected_reqs),
            "keyword_coverage": keyword_coverage(answer, expected_kws),
        })

    if not results:
        return {"results": [], "citation_recall": 0.0, "citation_precision": 0.0,
                "keyword_coverage": 0.0, "n": 0}

    def _avg(key: str) -> float:
        return round(sum(r[key] for r in results) / len(results), 4)

    return {
        "results": results,
        "citation_recall": _avg("citation_recall"),
        "citation_precision": _avg("citation_precision"),
        "keyword_coverage": _avg("keyword_coverage"),
        "n": len(results),
    }


# ── Agent suite ───────────────────────────────────────────────────────────────


async def run_agent_suite(record: bool = False) -> dict[str, Any]:
    """Run all agent golden cases and return aggregate metrics.

    Each case runs ``AgentOrchestrator.run()`` with:
    - A ``ReplayAnthropicClient`` that serves fixture turns in sequence.
    - ``execute_tool`` patched to return fixture tool results.
    - Stub pipeline, subagent runner, and judge.

    Args:
        record: Ignored in the current implementation (recording not yet automated).

    Returns:
        Dict with per-case results and aggregate metrics:
        ``tool_recall``, ``tool_precision``, ``keyword_coverage``, ``n``.
    """
    from orchestrator import AgentOrchestrator

    cases: list[dict] = yaml.safe_load((_GOLDEN_DIR / "agent_cases.yaml").read_text())
    store = FixtureStore("agent")
    results: list[dict] = []

    for case in cases:
        fixture = store.get(case["id"])
        if fixture is None:
            if not record:
                raise RuntimeError(
                    f"No fixture for {case['id']}. Run with --record to populate."
                )
            continue

        anthropic_client = ReplayAnthropicClient(fixture)
        orchestrator = AgentOrchestrator(
            client=anthropic_client,
            pipeline=_PassthroughPipeline(),  # type: ignore[arg-type]
            subagent_runner=_NoopSubagentRunner(),  # type: ignore[arg-type]
            judge=_FixedJudge(),  # type: ignore[arg-type]
        )

        fixture_tool_results: dict[str, str] = fixture.get("tool_results", {})

        async def _replay_tool(name: str, tool_input: dict, **kwargs: Any) -> str:
            return fixture_tool_results.get(
                name,
                f'{{"error": "no fixture result for tool {name}"}}',
            )

        with patch("orchestrator.execute_tool", _replay_tool):
            run_result = await orchestrator.run(
                question=case["question"],
                use_thinking=False,
                max_tokens=case.get("max_tokens", 2048),
            )

        called = [t["tool_name"] for t in run_result["tool_trace"]]
        expected = case.get("expected_tools", [])
        expected_kws = case.get("expected_keywords", [])

        results.append({
            "case_id": case["id"],
            "question": case["question"],
            "steps_taken": run_result["steps_taken"],
            "called_tools": called,
            "expected_tools": expected,
            "tool_recall": tool_recall(called, expected),
            "tool_precision": tool_precision(called, expected),
            "keyword_coverage": keyword_coverage(run_result["response"], expected_kws),
            "response": run_result["response"],
        })

    if not results:
        return {"results": [], "tool_recall": 0.0, "tool_precision": 0.0,
                "keyword_coverage": 0.0, "n": 0}

    def _avg(key: str) -> float:
        return round(sum(r[key] for r in results) / len(results), 4)

    return {
        "results": results,
        "tool_recall": _avg("tool_recall"),
        "tool_precision": _avg("tool_precision"),
        "keyword_coverage": _avg("keyword_coverage"),
        "n": len(results),
    }


# ── Injection suite ────────────────────────────────────────────────────────────


def run_injection_suite() -> dict[str, Any]:
    """Run all injection golden cases and return detection rate metrics.

    No mocking required — InjectionDetector is purely regex-based.

    Returns:
        Dict with per-case results and aggregate metrics:
        ``true_positive_rate``, ``false_positive_rate``, ``f1``, ``n``.
    """
    from injection_detector import InjectionDetector

    cases: list[dict] = yaml.safe_load((_GOLDEN_DIR / "injection_cases.yaml").read_text())
    detector = InjectionDetector()
    raw_results: list[dict] = []

    for case in cases:
        findings = detector.scan(case["text"])
        actual_blocked = detector.is_blocked(findings)
        raw_results.append({
            "case_id": case["id"],
            "text": case["text"][:80],
            "category": case.get("category", "unknown"),
            "expected_blocked": case["expected_blocked"],
            "actual_blocked": actual_blocked,
            "correct": case["expected_blocked"] == actual_blocked,
        })

    rates = injection_rates(raw_results)
    return {
        "results": raw_results,
        **rates,
        "n": len(raw_results),
        "n_correct": sum(1 for r in raw_results if r["correct"]),
    }


# ── Top-level runner ───────────────────────────────────────────────────────────


async def run_all(suite: str | None = None, record: bool = False) -> EvalReport:
    """Run all (or a single named) evaluation suite and return an EvalReport.

    Args:
        suite:  One of ``'rag'``, ``'agent'``, ``'injection'``, or ``None``
                to run all three.
        record: Pass through to individual suite runners (record mode).

    Returns:
        EvalReport with aggregate metrics and a ``passed`` flag.
    """
    floors = load_score_floors()
    rag_result = agent_result = inj_result = None

    if suite in (None, "rag"):
        rag_result = await run_rag_suite(record=record)
    if suite in (None, "agent"):
        agent_result = await run_agent_suite(record=record)
    if suite in (None, "injection"):
        inj_result = run_injection_suite()

    return EvalReport(rag=rag_result, agent=agent_result, injection=inj_result, floors=floors)
