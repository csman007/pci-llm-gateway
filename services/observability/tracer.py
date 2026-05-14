"""OpenTelemetry tracing setup for the PCI LLM Gateway.

Initialises a real TracerProvider when OTEL_ENABLED=true; otherwise installs a
no-op provider so every trace.get_tracer() call returns a zero-overhead tracer.
OTEL SDK packages are imported lazily so the app starts even when they are not
installed.
"""

import os

_OTEL_ENABLED: bool = os.environ.get("OTEL_ENABLED", "false").lower() == "true"
_SERVICE_NAME: str = os.environ.get("OTEL_SERVICE_NAME", "pci-llm-gateway")
_OTLP_ENDPOINT: str = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

# Lazily populated by setup_tracing()
_tracer_provider = None


def setup_tracing() -> None:
    """Initialise the global OpenTelemetry TracerProvider.

    When OTEL_ENABLED=true, creates an OTLPSpanExporter pointing at
    OTEL_EXPORTER_OTLP_ENDPOINT and registers a BatchSpanProcessor.
    When OTEL_ENABLED=false (the default), installs a NoOpTracerProvider so
    all downstream trace.get_tracer() calls are zero-overhead no-ops.

    Returns:
        None
    """
    global _tracer_provider

    if _OTEL_ENABLED:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create({"service.name": _SERVICE_NAME})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            _tracer_provider = provider
        except ImportError:
            # OTEL SDK not installed — fall back to no-op silently.
            _install_noop()
    else:
        _install_noop()


def _install_noop() -> None:
    """Install the no-op TracerProvider so trace.get_tracer() returns no-ops."""
    global _tracer_provider
    try:
        from opentelemetry import trace
        from opentelemetry.trace import NoOpTracerProvider

        provider = NoOpTracerProvider()
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
    except ImportError:
        _tracer_provider = None


def get_tracer(name: str):
    """Return a named tracer from the active TracerProvider.

    Args:
        name: Instrumentation scope name (e.g. "inference", "rag").

    Returns:
        An opentelemetry.trace.Tracer, or a _FallbackTracer when the SDK is
        not installed.
    """
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return _FallbackTracer()


def current_trace_id() -> str:
    """Return the hex trace ID of the currently active span, or "" if none.

    Returns:
        16-byte hex trace ID string, or empty string when there is no active
        span or when the OTEL SDK is not installed.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except ImportError:
        pass
    return ""


def current_span_id() -> str:
    """Return the hex span ID of the currently active span, or "" if none.

    Returns:
        8-byte hex span ID string, or empty string when there is no active
        span or when the OTEL SDK is not installed.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
    except ImportError:
        pass
    return ""


class _FallbackTracer:
    """No-op tracer used when the OTEL SDK is not installed."""

    def start_as_current_span(self, name: str, **kwargs):
        """Return a no-op context manager that yields None."""
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield None

        return _noop()
