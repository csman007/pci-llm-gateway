"""Unit tests for the DynamoDB-backed rate limiter."""

import math
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_table(new_count: int) -> MagicMock:
    """Return a mock DynamoDB Table whose update_item returns *new_count*."""
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"count": new_count}}
    return table


def _patch_table(table: MagicMock):
    """Context manager that injects *table* and enables the rate limiter."""
    import rate_limiter

    return patch.multiple(
        rate_limiter,
        _TABLE_NAME="test-table",
        _dynamodb=MagicMock(**{"Table.return_value": table}),
    )


# ── check_rate_limit ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_under_limit_passes():
    import rate_limiter

    table = _make_table(new_count=10)
    with _patch_table(table):
        await rate_limiter.check_rate_limit("user1", "inference", rpm=60)

    table.update_item.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_exactly_at_limit_passes():
    import rate_limiter

    table = _make_table(new_count=60)
    with _patch_table(table):
        await rate_limiter.check_rate_limit("user1", "inference", rpm=60)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_over_limit_raises_429():
    import rate_limiter

    table = _make_table(new_count=61)
    with _patch_table(table):
        with pytest.raises(HTTPException) as exc_info:
            await rate_limiter.check_rate_limit("user1", "inference", rpm=60)

    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers
    assert exc_info.value.headers["X-RateLimit-Limit"] == "60"
    assert exc_info.value.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_different_users_have_separate_counters():
    import rate_limiter

    table = _make_table(new_count=1)
    with _patch_table(table):
        await rate_limiter.check_rate_limit("user-a", "inference", rpm=60)
        await rate_limiter.check_rate_limit("user-b", "inference", rpm=60)

    assert table.update_item.call_count == 2
    keys = [call.kwargs["Key"]["pk"] for call in table.update_item.call_args_list]
    assert keys[0] != keys[1]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_different_endpoints_have_separate_counters():
    import rate_limiter

    table = _make_table(new_count=1)
    with _patch_table(table):
        await rate_limiter.check_rate_limit("user1", "inference", rpm=60)
        await rate_limiter.check_rate_limit("user1", "rag", rpm=20)

    keys = [call.kwargs["Key"]["pk"] for call in table.update_item.call_args_list]
    assert "inference" in keys[0]
    assert "rag" in keys[1]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dynamodb_error_fails_open():
    """A DynamoDB ClientError must not block the request — fail open."""
    from botocore.exceptions import ClientError

    import rate_limiter

    table = MagicMock()
    table.update_item.side_effect = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": ""}}, "UpdateItem")

    with _patch_table(table):
        # Should not raise
        await rate_limiter.check_rate_limit("user1", "inference", rpm=60)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_table_configured_is_noop():
    """When RATE_LIMIT_TABLE is unset, check_rate_limit returns immediately."""
    import rate_limiter

    with patch.multiple(rate_limiter, _TABLE_NAME="", _dynamodb=None):
        # Must not raise regardless of count
        await rate_limiter.check_rate_limit("user1", "inference", rpm=1)


# ── bucket key ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bucket_key_contains_user_endpoint_and_minute():
    import rate_limiter

    now = time.time()
    bucket = math.floor(now / 60)
    table = _make_table(new_count=1)
    with _patch_table(table):
        await rate_limiter.check_rate_limit("alice", "rag", rpm=100)

    pk = table.update_item.call_args.kwargs["Key"]["pk"]
    assert pk == f"default#alice#rag#{bucket}"
