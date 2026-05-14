"""Structured JSON logging and pipeline stage timing utilities.

Provides:
- _JsonFormatter: emits each log record as a single-line JSON object.
- configure_logging: replaces the root logger's handlers with a StreamHandler
  using _JsonFormatter.
- stage_span: context manager that times a named pipeline stage and emits a
  structured log entry on exit.
"""

import json
import logging
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Emitted keys: timestamp, level, logger, message, trace_id (if non-empty),
    span_id (if non-empty), exception (if exc_info present), plus any extra
    key=value pairs passed via extra={}.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise *record* to a single-line JSON string.

        Args:
            record: The LogRecord to format.

        Returns:
            A JSON string terminated by no newline (logging's StreamHandler
            appends the terminator).
        """
        # Lazy import to avoid circular dependencies at module load time.
        try:
            from tracer import current_span_id, current_trace_id
        except ImportError:
            current_trace_id = lambda: ""  # noqa: E731
            current_span_id = lambda: ""  # noqa: E731

        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        payload: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        trace_id = current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id

        span_id = current_span_id()
        if span_id:
            payload["span_id"] = span_id

        # Merge any extra key-value pairs (skip private logging internals).
        _SKIP = frozenset(
            {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "taskName",
                "message",
            }
        )
        for key, val in record.__dict__.items():
            if key not in _SKIP and not key.startswith("_"):
                payload[key] = val

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Replace the root logger's handlers with a single JSON StreamHandler.

    Reads LOG_LEVEL env var first; falls back to the *level* parameter.

    Args:
        level: Default log level when LOG_LEVEL env var is not set.

    Returns:
        None
    """
    effective_level = os.environ.get("LOG_LEVEL", level).upper()
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(effective_level)


@contextmanager
def stage_span(
    logger: logging.Logger,
    stage: str,
    **attrs: Any,
) -> Generator[dict, None, None]:
    """Time a named pipeline stage and emit a structured log entry on exit.

    Usage::

        with stage_span(log, "pii_detect", request_id=rid) as meta:
            findings = detector.scan(prompt)
            meta["pii_count"] = len(findings)

    The yielded *meta* dict is mutable; callers attach stage-specific results
    before the context exits. On exit the manager emits::

        logger.info("stage.<stage>", extra={stage, latency_ms, **attrs, **meta})

    Args:
        logger:  Logger instance to emit the structured entry on.
        stage:   Short name for this pipeline stage (e.g. "pii_detect").
        **attrs: Additional key-value pairs to include in the log record.

    Yields:
        Mutable dict that callers can populate with stage result metadata.
    """
    meta: dict[str, Any] = {}
    start = time.monotonic()
    try:
        yield meta
    finally:
        latency_ms = round((time.monotonic() - start) * 1000, 2)
        extra: dict[str, Any] = {"stage": stage, "latency_ms": latency_ms, **attrs, **meta}
        logger.info("stage.%s", stage, extra=extra)
