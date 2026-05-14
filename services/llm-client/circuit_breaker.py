"""Per-provider circuit breaker for LLM clients.

States:
  CLOSED    — normal operation; all calls go through.
  OPEN      — provider is unhealthy; calls fail fast with CircuitOpenError.
  HALF_OPEN — recovery probe; one success closes the circuit, one failure re-opens it.
"""

import os
import threading
import time
from enum import Enum


class CircuitState(Enum):
    """Possible states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN.

    Attributes:
        provider:    Name of the unhealthy LLM provider.
        retry_after: Seconds until the circuit transitions to HALF_OPEN.
    """

    def __init__(self, provider: str, retry_after: float) -> None:
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"Circuit open for '{provider}'; retry in {retry_after:.1f}s")


class CircuitBreaker:
    """State machine that stops cascading failures to an LLM provider.

    Opens after `failure_threshold` qualifying failures within `failure_window_secs`.
    Transitions to HALF_OPEN after `recovery_timeout_secs`.
    Closes on the first successful call from HALF_OPEN.
    """

    def __init__(
        self,
        provider: str,
        failure_threshold: int,
        failure_window_secs: float,
        recovery_timeout_secs: float,
    ) -> None:
        self.provider = provider
        self._threshold = failure_threshold
        self._window = failure_window_secs
        self._recovery = recovery_timeout_secs

        self._state = CircuitState.CLOSED
        self._failure_timestamps: list[float] = []
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    # ── public ────────────────────────────────────────────────────────────────

    async def call(self, coro, *, trip_on: tuple[type[Exception], ...] = (Exception,)):
        """Execute *coro*, applying circuit-breaker logic.

        Args:
            coro:    Awaitable to execute (e.g. an LLM client call).
            trip_on: Exception types that count as provider failures and advance
                     the circuit toward OPEN. Other exceptions are re-raised
                     without changing circuit state.

        Returns:
            The return value of *coro*.

        Raises:
            CircuitOpenError: If the circuit is currently OPEN.
            Exception:        Any exception raised by *coro*.
        """
        with self._lock:
            state = self._current_state()
            if state == CircuitState.OPEN:
                coro.close()  # prevent "coroutine never awaited" ResourceWarning
                raise CircuitOpenError(self.provider, self._seconds_until_retry())

        try:
            result = await coro
            self._on_success()
            return result
        except Exception as exc:
            if isinstance(exc, trip_on):
                self._on_failure()
            raise

    @property
    def state(self) -> CircuitState:
        """Current circuit state (thread-safe)."""
        with self._lock:
            return self._current_state()

    # ── internals ─────────────────────────────────────────────────────────────

    def _current_state(self) -> CircuitState:
        """Return state, transitioning OPEN → HALF_OPEN when the recovery window has passed.

        Must be called while holding self._lock.
        """
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._recovery:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def _on_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_timestamps.clear()
            self._opened_at = None

    def _on_failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._failure_timestamps = [t for t in self._failure_timestamps if now - t < self._window]
            self._failure_timestamps.append(now)
            if len(self._failure_timestamps) >= self._threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now

    def _seconds_until_retry(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._recovery - (time.monotonic() - self._opened_at))


# ── registry ──────────────────────────────────────────────────────────────────

_FAILURE_THRESHOLD = int(os.environ.get("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5"))
_FAILURE_WINDOW_SECS = float(os.environ.get("CIRCUIT_BREAKER_FAILURE_WINDOW_SECS", "60"))
_RECOVERY_TIMEOUT_SECS = float(os.environ.get("CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECS", "30"))

_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(provider: str) -> CircuitBreaker:
    """Return the singleton CircuitBreaker for *provider*, creating it on first call.

    Args:
        provider: Logical provider name, e.g. ``"anthropic"`` or ``"openai"``.

    Returns:
        The shared CircuitBreaker for that provider (one per Lambda instance).
    """
    with _registry_lock:
        if provider not in _registry:
            _registry[provider] = CircuitBreaker(
                provider=provider,
                failure_threshold=_FAILURE_THRESHOLD,
                failure_window_secs=_FAILURE_WINDOW_SECS,
                recovery_timeout_secs=_RECOVERY_TIMEOUT_SECS,
            )
        return _registry[provider]
