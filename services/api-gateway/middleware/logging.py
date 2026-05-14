"""Request logging middleware with OpenTelemetry trace context injection."""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from tracer import current_span_id, current_trace_id

logger = logging.getLogger("pci-gateway")


class _TraceFilter(logging.Filter):
    """Injects trace_id and span_id from the active OTEL span into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach trace_id and span_id to *record*.

        Args:
            record: The LogRecord being processed.

        Returns:
            Always True — this filter never drops records.
        """
        record.trace_id = current_trace_id()
        record.span_id = current_span_id()
        return True


# Attach the trace filter to the gateway logger at module load time.
logger.addFilter(_TraceFilter())


class LoggingMiddleware(BaseHTTPMiddleware):
    """Logs request_start and request_end events for every HTTP request."""

    async def dispatch(self, request: Request, call_next):
        """Log request lifecycle events and attach a unique request_id.

        Args:
            request:   The incoming Starlette request.
            call_next: The next middleware or route handler.

        Returns:
            The HTTP response, with an X-Request-ID header added.
        """
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.monotonic()

        logger.info(
            "request_start",
            extra={"request_id": request_id, "method": request.method, "path": request.url.path},
        )

        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)

        trace_id = getattr(request.state, "trace_id", current_trace_id())
        logger.info(
            "request_end",
            extra={
                "request_id": request_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "trace_id": trace_id,
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response
