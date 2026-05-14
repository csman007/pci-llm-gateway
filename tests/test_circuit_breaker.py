"""Unit tests for the per-provider circuit breaker."""

import asyncio
import time
from unittest.mock import patch

import pytest
from circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState, get_breaker


def _breaker(*, threshold: int = 3, window: float = 60.0, recovery: float = 1.0) -> CircuitBreaker:
    return CircuitBreaker(
        provider="test",
        failure_threshold=threshold,
        failure_window_secs=window,
        recovery_timeout_secs=recovery,
    )


async def _ok():
    return "ok"


async def _fail():
    raise _ProviderError("boom")


class _ProviderError(Exception):
    pass


# ── state transitions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_starts_closed():
    assert _breaker().state == CircuitState.CLOSED


@pytest.mark.asyncio
@pytest.mark.unit
async def test_success_keeps_closed():
    b = _breaker()
    result = await b.call(_ok())
    assert result == "ok"
    assert b.state == CircuitState.CLOSED


@pytest.mark.asyncio
@pytest.mark.unit
async def test_opens_after_threshold_failures():
    b = _breaker(threshold=2)
    for _ in range(2):
        with pytest.raises(_ProviderError):
            await b.call(_fail(), trip_on=(_ProviderError,))
    assert b.state == CircuitState.OPEN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_open_circuit_raises_circuit_open_error():
    b = _breaker(threshold=1)
    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))
    assert b.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError) as exc_info:
        await b.call(_ok())
    assert exc_info.value.provider == "test"
    assert exc_info.value.retry_after > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_non_trip_exception_does_not_open_circuit():
    """HTTPException (client error) must not advance the circuit toward OPEN."""
    b = _breaker(threshold=2)

    class _ClientError(Exception):
        pass

    async def _client_fail():
        raise _ClientError("bad request")

    for _ in range(5):
        with pytest.raises(_ClientError):
            await b.call(_client_fail(), trip_on=(_ProviderError,))

    assert b.state == CircuitState.CLOSED


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transitions_to_half_open_after_recovery_timeout():
    b = _breaker(threshold=1, recovery=0.05)
    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))
    assert b.state == CircuitState.OPEN

    await asyncio.sleep(0.1)
    assert b.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_success_in_half_open_closes_circuit():
    b = _breaker(threshold=1, recovery=0.05)
    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))
    await asyncio.sleep(0.1)
    assert b.state == CircuitState.HALF_OPEN

    result = await b.call(_ok())
    assert result == "ok"
    assert b.state == CircuitState.CLOSED


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failure_in_half_open_reopens_circuit():
    b = _breaker(threshold=1, recovery=0.05)
    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))
    await asyncio.sleep(0.1)
    assert b.state == CircuitState.HALF_OPEN

    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))
    assert b.state == CircuitState.OPEN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failures_outside_window_do_not_count():
    b = _breaker(threshold=2, window=0.05)
    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))

    await asyncio.sleep(0.1)  # let first failure expire

    with pytest.raises(_ProviderError):
        await b.call(_fail(), trip_on=(_ProviderError,))

    # Only 1 failure within the window — circuit should still be closed.
    assert b.state == CircuitState.CLOSED


# ── registry ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_get_breaker_returns_singleton():
    b1 = get_breaker("anthropic-test-singleton")
    b2 = get_breaker("anthropic-test-singleton")
    assert b1 is b2


@pytest.mark.unit
def test_get_breaker_different_providers_are_isolated():
    b1 = get_breaker("provider-a-isolation")
    b2 = get_breaker("provider-b-isolation")
    assert b1 is not b2
