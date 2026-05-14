"""Tool-call validation — name allowlist enforcement and output injection scanning.

Applied by AgentOrchestrator at two points:
  1. Before dispatch — reject tool names not in the static allowlist.
  2. After execution — scan the result string; replace blocked content with a
     safe error sentinel so it never reaches the model's context window.

The allowlist is the single source of truth for which tools the orchestrator
may call.  Any name the model hallucinates outside this set is rejected before
``execute_tool`` is ever invoked.
"""

import logging

from injection_detector import InjectionDetector

log = logging.getLogger("pci-gateway.tool_validator")

_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"query_audit_log", "analyze_pii_risk", "calculator", "search_pci_dss", "call_subagent"}
)

_detector = InjectionDetector()

_INJECTION_SENTINEL = "ERROR: Tool result blocked — prompt injection pattern detected in response"


def validate_tool_name(name: str) -> str | None:
    """Return an error string when *name* is not in the static tool allowlist.

    Args:
        name: Tool name from the model's ``tool_use`` content block.

    Returns:
        ``None`` if the name is allowed, otherwise an ``ERROR:``-prefixed string
        explaining the rejection.
    """
    if name not in _ALLOWED_TOOLS:
        log.warning("tool_name_rejected", extra={"tool_name": name})
        return f"ERROR: tool '{name}' is not in the allowed tool set"
    return None


def scan_tool_result(result: str, tool_name: str) -> str:
    """Scan a tool result for injection patterns; replace if blocked.

    This is the primary guard against indirect prompt injection via tool
    outputs — e.g. a poisoned DynamoDB record or a retrieved PCI DSS chunk
    that contains ``ignore previous instructions``.

    Args:
        result:    The raw string returned by the tool implementation.
        tool_name: Name of the tool that produced the result (for log context).

    Returns:
        The original *result* when clean, or :data:`_INJECTION_SENTINEL` when
        a BLOCK-severity pattern is detected.
    """
    findings = _detector.scan(result)
    _detector.log_findings(findings, source=f"tool_result:{tool_name}")
    if _detector.is_blocked(findings):
        log.warning("tool_result_blocked", extra={"tool_name": tool_name})
        return _INJECTION_SENTINEL
    return result
