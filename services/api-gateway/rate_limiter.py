"""DynamoDB-backed fixed-window rate limiter for per-user, per-endpoint throttling.

When RATE_LIMIT_TABLE is not set the limiter is a no-op — local dev is unrestricted.

Window: 60-second fixed bucket keyed by user + endpoint + UTC minute.
Each bucket is atomically incremented; requests beyond the limit receive 429.
"""

import math
import os
import time
from collections.abc import Callable

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, Request

_TABLE_NAME = os.environ.get("RATE_LIMIT_TABLE", "")
_WINDOW_SECS = 60

# Default per-endpoint request-per-minute caps — overridable via env vars.
_LIMITS: dict[str, int] = {
    "inference": int(os.environ.get("RATE_LIMIT_INFERENCE_RPM", "60")),
    "rag": int(os.environ.get("RATE_LIMIT_RAG_RPM", "20")),
    "agent": int(os.environ.get("RATE_LIMIT_AGENT_RPM", "30")),
}

_dynamodb = (
    boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION_NAME", "us-east-1")) if _TABLE_NAME else None
)


async def check_rate_limit(user_id: str, endpoint: str, rpm: int, tenant_id: str = "default") -> None:
    """Atomically increment the request counter and raise 429 if the limit is exceeded.

    Uses a DynamoDB fixed-window counter: the bucket key resets every 60 seconds.
    Counter keys are scoped to ``tenant_id`` so tenants never share counters.
    A TTL attribute ensures stale counters are cleaned up automatically.

    Args:
        user_id:   JWT ``sub`` claim identifying the caller.
        endpoint:  Logical endpoint name (``"inference"``, ``"rag"``, ``"agent"``).
        rpm:       Maximum requests allowed in the current 60-second window.
        tenant_id: Tenant identifier — isolates counters between tenants.

    Raises:
        HTTPException(429): When the counter for this window exceeds *rpm*.
    """
    if not _TABLE_NAME or _dynamodb is None:
        return  # rate limiting disabled in local dev

    now = time.time()
    bucket = math.floor(now / _WINDOW_SECS)
    window_end = (bucket + 1) * _WINDOW_SECS
    retry_after = int(window_end - now) + 1
    pk = f"{tenant_id}#{user_id}#{endpoint}#{bucket}"

    table = _dynamodb.Table(_TABLE_NAME)
    try:
        response = table.update_item(
            Key={"pk": pk},
            UpdateExpression="ADD #c :inc SET #ttl = if_not_exists(#ttl, :ttl_val)",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={":inc": 1, ":ttl_val": int(window_end) + _WINDOW_SECS},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError:
        return  # fail open — don't block on DynamoDB errors

    new_count = int(response["Attributes"]["count"])
    if new_count > rpm:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded for endpoint '{endpoint}': {rpm} req/min",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(rpm),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(window_end)),
            },
        )


def limit(endpoint: str) -> Callable:
    """Return a FastAPI dependency that enforces the rate limit for *endpoint*.

    Usage::

        @router.post("/v1/inference", dependencies=[Depends(rate_limiter.limit("inference"))])
        async def inference(...): ...

    Args:
        endpoint: Logical name matching a key in ``_LIMITS``.

    Returns:
        An async dependency callable compatible with ``fastapi.Depends``.
    """
    rpm = _LIMITS.get(endpoint, 60)

    async def _dependency(request: Request) -> None:
        """FastAPI dependency — extracts user_id and tenant_id from request state and checks the counter."""
        user = getattr(request.state, "user", None)
        user_id = user.get("sub", "anonymous") if isinstance(user, dict) else "anonymous"
        tenant_id = user.get("tenant_id", "default") if isinstance(user, dict) else "default"
        await check_rate_limit(user_id, endpoint, rpm, tenant_id)

    return _dependency
