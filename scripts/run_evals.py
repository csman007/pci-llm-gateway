#!/usr/bin/env python3
"""Offline evaluation CLI — runs golden-dataset suites and prints a scored report.

Usage:
    python scripts/run_evals.py                    # replay mode, all suites
    python scripts/run_evals.py --suite rag        # single suite
    python scripts/run_evals.py --suite agent
    python scripts/run_evals.py --suite injection
    python scripts/run_evals.py --output report.json
    python scripts/run_evals.py --record           # call real APIs and save fixtures

Exit codes:
    0 — all suites pass their score floors
    1 — one or more suites are below a floor threshold
    2 — unexpected error
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Bootstrap sys.path so service modules and evals package are importable from the
# project root without requiring an editable install.
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

from evals.harness import EvalReport, run_all  # noqa: E402 (after sys.path setup)

_RESULTS_DIR = _root / "evals" / "results"

# ANSI colours (disabled on non-TTY)
_GREEN = "\033[32m" if sys.stdout.isatty() else ""
_RED = "\033[31m" if sys.stdout.isatty() else ""
_YELLOW = "\033[33m" if sys.stdout.isatty() else ""
_BOLD = "\033[1m" if sys.stdout.isatty() else ""
_RESET = "\033[0m" if sys.stdout.isatty() else ""


def _fmt(value: float, floor: float | None) -> str:
    """Format a metric value with pass/fail colouring against its floor."""
    pct = f"{value * 100:.1f}%"
    if floor is None:
        return pct
    if value >= floor:
        return f"{_GREEN}{pct}{_RESET}"
    return f"{_RED}{pct} (floor {floor * 100:.0f}%){_RESET}"


def _print_rag(report: EvalReport) -> None:
    if report.rag is None:
        return
    floors = report.floors.get("rag", {})
    r = report.rag
    print(f"\n{_BOLD}RAG suite{_RESET}  (n={r['n']})")
    print(f"  citation recall    {_fmt(r['citation_recall'], floors.get('citation_recall'))}")
    print(f"  citation precision {_fmt(r['citation_precision'], floors.get('citation_precision'))}")
    print(f"  keyword coverage   {_fmt(r['keyword_coverage'], floors.get('keyword_coverage'))}")
    failures = [res for res in r["results"] if res["citation_recall"] < floors.get("citation_recall", 0.0)]
    if failures:
        print(f"  {_YELLOW}Low-recall cases:{_RESET}")
        for f in failures:
            print(
                f"    {f['case_id']}: recall={f['citation_recall']:.2f}  cited={f['cited']}  expected={f['expected_requirement_ids']}"
            )


def _print_agent(report: EvalReport) -> None:
    if report.agent is None:
        return
    floors = report.floors.get("agent", {})
    a = report.agent
    print(f"\n{_BOLD}Agent suite{_RESET}  (n={a['n']})")
    print(f"  tool recall        {_fmt(a['tool_recall'], floors.get('tool_recall'))}")
    print(f"  tool precision     {_fmt(a['tool_precision'], floors.get('tool_precision'))}")
    print(f"  keyword coverage   {_fmt(a['keyword_coverage'], floors.get('keyword_coverage'))}")
    misses = [res for res in a["results"] if res["tool_recall"] < floors.get("tool_recall", 0.0)]
    if misses:
        print(f"  {_YELLOW}Tool-recall misses:{_RESET}")
        for m in misses:
            print(f"    {m['case_id']}: called={m['called_tools']}  expected={m['expected_tools']}")


def _print_injection(report: EvalReport) -> None:
    if report.injection is None:
        return
    floors = report.floors.get("injection", {})
    inj = report.injection
    print(f"\n{_BOLD}Injection suite{_RESET}  (n={inj['n']}, correct={inj['n_correct']})")
    print(f"  true positive rate  {_fmt(inj['true_positive_rate'], floors.get('true_positive_rate'))}")
    print(
        f"  false positive rate {_fmt(1.0 - inj['false_positive_rate'], None)} clean  "
        f"(FP floor ≤{floors.get('false_positive_rate', 0.05) * 100:.0f}%)"
    )
    print(f"  F1                  {inj['f1']:.4f}")
    wrong = [r for r in inj["results"] if not r["correct"]]
    if wrong:
        print(f"  {_YELLOW}Misclassified:{_RESET}")
        for w in wrong:
            print(
                f"    {w['case_id']} [{w['category']}]: expected_blocked={w['expected_blocked']}  actual={w['actual_blocked']}"
            )
            print(f"      text: {w['text']!r}")


def _print_summary(report: EvalReport) -> None:
    status = f"{_GREEN}PASS{_RESET}" if report.passed else f"{_RED}FAIL{_RESET}"
    print(f"\n{_BOLD}Overall: {status}{_RESET}")
    for f in report.failures():
        print(f"  {_RED}✗ {f}{_RESET}")


def _to_dict(report: EvalReport) -> dict:
    """Serialise EvalReport to a JSON-compatible dict."""
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": report.passed,
        "failures": report.failures(),
        "rag": report.rag,
        "agent": report.agent,
        "injection": report.injection,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run PCI LLM Gateway offline evaluation suites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--suite",
        choices=["rag", "agent", "injection", "all"],
        default="all",
        help="Which suite to run (default: all)",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Record mode: call real APIs and save responses to fixture files",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write the JSON report to FILE (optional)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save the report to evals/results/",
    )
    args = parser.parse_args()

    suite_arg = None if args.suite == "all" else args.suite
    try:
        report = asyncio.run(run_all(suite=suite_arg, record=args.record))
    except RuntimeError as exc:
        print(f"{_RED}Error:{_RESET} {exc}", file=sys.stderr)
        sys.exit(2)

    _print_rag(report)
    _print_agent(report)
    _print_injection(report)
    _print_summary(report)

    report_dict = _to_dict(report)

    # Always save a timestamped copy unless --no-save
    if not args.no_save:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = _RESULTS_DIR / f"{ts}.json"
        out_path.write_text(json.dumps(report_dict, indent=2))
        print(f"\nReport saved to {out_path}")

    if args.output:
        Path(args.output).write_text(json.dumps(report_dict, indent=2))

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
