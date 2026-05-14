"""Prompt injection detector — scans text for instruction-hijacking, persona attacks,
system-prompt extraction attempts, and template-delimiter injection.

Used at three surfaces:
  1. User prompt  (inference route)      — BLOCK severity → HTTP 400
  2. User question (RAG route)           — BLOCK severity → HTTP 400
  3. Retrieved RAG chunks                — BLOCK severity → chunk dropped (indirect injection)
  4. Agent tool results                  — BLOCK severity → result replaced with error string

Set INJECTION_BLOCK_ACTION=log to enter monitor-only mode (all findings logged, none blocked).
Useful when first deploying to measure false-positive rate before enforcing.
"""

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("pci-gateway.injection")

# "block" (default) — BLOCK-severity findings reject the request.
# "log"             — all findings are logged; nothing is blocked (monitor mode).
_BLOCK_ACTION = os.environ.get("INJECTION_BLOCK_ACTION", "block").lower()

# ── pattern registry ──────────────────────────────────────────────────────────
#
# Each entry: (pattern_name, regex, severity)
# severity "block" → reject or drop; "warn" → log and pass through.

_RAW_PATTERNS: list[tuple[str, str, str]] = [
    # ── Instruction override ──────────────────────────────────────────────────
    (
        "instruction_override",
        r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|constraints?|directives?)",
        "block",
    ),
    (
        "instruction_override",
        r"forget\s+(?:everything|all|previous|prior|your)\s+(?:instructions?|training|rules?|constraints?)",
        "block",
    ),
    (
        "instruction_override",
        r"disregard\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?|directives?)",
        "block",
    ),
    (
        "instruction_override",
        r"override\s+(?:your\s+)?(?:instructions?|programming|system\s+prompt|directives?)",
        "block",
    ),
    (
        "instruction_override",
        r"(?:new|updated)\s+(?:primary\s+)?(?:instructions?|directive|objective)\s*:",
        "block",
    ),
    (
        "instruction_override",
        r"from\s+now\s+on[,\s]+you\s+(?:are|will|must|should)\b",
        "block",
    ),
    (
        "instruction_override",
        r"your\s+(?:new\s+)?(?:instructions?|task|role|purpose)\s+(?:is|are)\s+(?:to|now)\b",
        "block",
    ),
    # ── Persona / jailbreak hijacking ─────────────────────────────────────────
    (
        "persona_hijack",
        r"you\s+are\s+now\s+(?:a|an|the)\s+\w+(?:\s+without\s+restrictions?|\s+that\s+can)?",
        "block",
    ),
    (
        "persona_hijack",
        r"pretend\s+(?:you\s+are|to\s+be)\s+(?:an?\s+)?(?:unrestricted|uncensored|unfiltered|jailbroken|evil|DAN|\w+\s+without\s+restrictions?)",
        "block",
    ),
    (
        "persona_hijack",
        r"act\s+as\s+(?:an?\s+)?(?:unrestricted|uncensored|unfiltered|jailbroken|DAN|evil|malicious|harmful|rouge|rogue)",
        "block",
    ),
    (
        "persona_hijack",
        r"roleplay\s+as\s+(?:an?\s+)?(?:unrestricted|uncensored|unfiltered|evil|DAN)",
        "block",
    ),
    (
        "persona_hijack",
        r"(?:developer|jailbreak|god|admin|unrestricted|DAN|sudo)\s+mode\s+(?:enabled|activated|on|engaged)",
        "block",
    ),
    (
        "persona_hijack",
        r"\bDAN\b.{0,40}(?:mode|prompt|jailbreak|activated)",
        "block",
    ),
    (
        "persona_hijack",
        r"(?:enable|activate|switch\s+(?:to|on))\s+(?:developer|unrestricted|jailbreak|DAN)\s+mode",
        "block",
    ),
    # ── System prompt extraction ───────────────────────────────────────────────
    (
        "system_prompt_extraction",
        r"(?:print|repeat|output|show|tell\s+me|reveal|display|dump|leak)\s+(?:your\s+)?(?:system\s+prompt|initial\s+instructions?|original\s+instructions?|base\s+prompt|full\s+prompt)",
        "block",
    ),
    (
        "system_prompt_extraction",
        r"what\s+(?:are|is|were)\s+(?:your\s+)?(?:system\s+prompt|initial\s+instructions?|original\s+instructions?|hidden\s+instructions?|confidential\s+instructions?)",
        "block",
    ),
    (
        "system_prompt_extraction",
        r"(?:ignore|bypass|circumvent)\s+(?:your\s+)?system\s+prompt",
        "block",
    ),
    (
        "system_prompt_extraction",
        r"pretend\s+(?:you\s+don.t\s+have|there\s+is\s+no|you\s+have\s+no)\s+system\s+prompt",
        "block",
    ),
    (
        "system_prompt_extraction",
        r"repeat\s+(?:everything|the\s+text)\s+(?:above|before|prior\s+to\s+this)",
        "block",
    ),
    # ── Template-delimiter injection (critical for RAG indirect injection) ─────
    (
        "delimiter_injection",
        r"</?(?:system|user|assistant|human|inst)\s*>",
        "block",
    ),
    (
        "delimiter_injection",
        r"\[/?(?:INST|SYS|SYS2|END|SYSTEM)\]",
        "block",
    ),
    (
        "delimiter_injection",
        r"###\s*(?:System|User|Assistant|Human|Instruction)\s*:",
        "block",
    ),
    (
        "delimiter_injection",
        r"<\|(?:im_start|im_end|endoftext|system|user|assistant)\|>",
        "block",
    ),
    # ── Indirect injection markers (warn — common in adversarial documents) ───
    (
        "indirect_injection",
        r"\bATTENTION\b.{0,20}(?:AI|LLM|Model|Claude|language\s+model)",
        "warn",
    ),
    (
        "indirect_injection",
        r"NOTE\s+TO\s+(?:AI|LLM|LANGUAGE\s+MODEL|CLAUDE|the\s+AI)\s*:",
        "warn",
    ),
    (
        "indirect_injection",
        r"\[(?:SYSTEM|HIDDEN|SECRET|OVERRIDE|INJECTED?)\]\s*:",
        "warn",
    ),
    (
        "indirect_injection",
        r"(?:AI|LLM)?\s*(?:OVERRIDE|INJECTION|HIJACK)\s*:",
        "warn",
    ),
    # ── Data exfiltration ─────────────────────────────────────────────────────
    (
        "exfiltration",
        r"(?:base64|hex)\s+encode\s+(?:and\s+)?(?:send|output|print|transmit|return)\b",
        "warn",
    ),
    (
        "exfiltration",
        r"(?:send|transmit|forward|email|post)\s+(?:this|the)\s+(?:conversation|chat\s+history|context|system\s+prompt)\b",
        "warn",
    ),
]

_COMPILED: list[tuple[str, re.Pattern, str]] = [
    (name, re.compile(pattern, re.IGNORECASE | re.DOTALL), severity) for name, pattern, severity in _RAW_PATTERNS
]


# ── public API ────────────────────────────────────────────────────────────────


@dataclass
class InjectionFinding:
    """A single pattern match within the scanned text.

    Attributes:
        pattern_name: Logical category (e.g. ``"instruction_override"``).
        severity:     ``"block"`` — should be rejected; ``"warn"`` — suspicious but allowed.
        matched_text: The substring that triggered the pattern.
    """

    pattern_name: str
    severity: str
    matched_text: str


class InjectionDetector:
    """Scans text for prompt injection patterns using compiled regex rules.

    Covers five attack categories:
      - Instruction override: attempts to replace server-side instructions.
      - Persona hijacking: jailbreak / DAN-style role substitution.
      - System prompt extraction: requests to reveal server-side context.
      - Delimiter injection: LLM template tokens that would hijack message structure.
      - Indirect injection / exfiltration: markers placed in retrieved documents or
        data sources to influence the model when retrieved content is in context.
    """

    def scan(self, text: str) -> list[InjectionFinding]:
        """Return all injection findings in *text*.

        Args:
            text: Raw text to scan (prompt, chunk, tool result, etc.).

        Returns:
            List of InjectionFinding objects, empty if no patterns matched.
        """
        findings: list[InjectionFinding] = []
        for name, pattern, severity in _COMPILED:
            for match in pattern.finditer(text):
                findings.append(
                    InjectionFinding(
                        pattern_name=name,
                        severity=severity,
                        matched_text=match.group(0)[:120],  # cap for log safety
                    )
                )
        return findings

    def is_blocked(self, findings: list[InjectionFinding]) -> bool:
        """Return True when *findings* contain a BLOCK-severity match AND the
        block action is not overridden to ``"log"`` by ``INJECTION_BLOCK_ACTION``.

        Args:
            findings: Output of :meth:`scan`.

        Returns:
            True if the content should be rejected.
        """
        if _BLOCK_ACTION == "log":
            return False
        return any(f.severity == "block" for f in findings)

    def log_findings(self, findings: list[InjectionFinding], source: str) -> None:
        """Emit a structured log entry for each finding.

        Args:
            findings: Output of :meth:`scan`.
            source:   Label for where the text came from (e.g. ``"user_prompt"``,
                      ``"rag_chunk"``, ``"tool_result"``).
        """
        for f in findings:
            log.warning(
                "injection_detected",
                extra={
                    "source": source,
                    "pattern": f.pattern_name,
                    "severity": f.severity,
                    "matched_text": f.matched_text,
                },
            )
