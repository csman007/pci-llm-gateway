"""Per-tenant monthly spend tracking and budget enforcement backed by DynamoDB.

Spend items live in the same TENANTS_TABLE as tenant config, keyed by
``spend#{tenant_id}#{YYYY-MM}``. Atomic ADD keeps concurrent Lambda
instances from racing on the counter.
"""

import calendar
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

_TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")
_dynamodb = (
    boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION_NAME", "us-east-1")) if _TENANTS_TABLE else None
)


def _spend_pk(tenant_id: str) -> tuple[str, int]:
    """Return the DynamoDB PK and TTL for this month's spend item.

    Args:
        tenant_id: Tenant identifier.

    Returns:
        Tuple of (pk string, unix TTL timestamp kept for 30 days past month end).
    """
    t = time.gmtime()
    pk = f"spend#{tenant_id}#{t.tm_year:04d}-{t.tm_mon:02d}"
    _, days = calendar.monthrange(t.tm_year, t.tm_mon)
    month_end = int(time.mktime((t.tm_year, t.tm_mon, days, 23, 59, 59, 0, 0, -1)))
    return pk, month_end + 30 * 86_400


def check_budget(tenant_id: str, monthly_budget_usd: float) -> None:
    """Raise 429 if the tenant has exhausted their monthly spend budget.

    Reads the current month's spend counter from DynamoDB. Fails open on
    any DynamoDB error so that a table outage never blocks legitimate requests.

    Args:
        tenant_id:           Tenant identifier.
        monthly_budget_usd:  Hard monthly cap in USD.

    Raises:
        HTTPException(429): When ``current_spend >= monthly_budget_usd``.
    """
    if not _TENANTS_TABLE or _dynamodb is None:
        return

    pk, _ = _spend_pk(tenant_id)
    try:
        item = _dynamodb.Table(_TENANTS_TABLE).get_item(Key={"pk": pk}).get("Item", {})
        current = float(item.get("spend_usd", 0))
    except ClientError:
        return  # fail open

    if current >= monthly_budget_usd:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly budget of ${monthly_budget_usd:.2f} exhausted for tenant '{tenant_id}'",
            headers={
                "X-Tenant-Budget-Limit": f"{monthly_budget_usd:.2f}",
                "X-Tenant-Budget-Remaining": "0.00",
            },
        )


def record_spend(tenant_id: str, cost_usd: float) -> None:
    """Atomically add *cost_usd* to the tenant's current-month spend counter.

    Silently swallows DynamoDB errors — a failed write is preferable to
    blocking the response that has already been generated.

    Args:
        tenant_id: Tenant identifier.
        cost_usd:  Cost of this request in USD (from ``calculate_cost()``).
    """
    if not _TENANTS_TABLE or _dynamodb is None:
        return

    pk, ttl = _spend_pk(tenant_id)
    try:
        _dynamodb.Table(_TENANTS_TABLE).update_item(
            Key={"pk": pk},
            UpdateExpression=(
                "ADD spend_usd :cost, request_count :one "
                "SET #ttl = if_not_exists(#ttl, :ttl_val), "
                "tenant_id = if_not_exists(tenant_id, :tid)"
            ),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":cost": Decimal(str(round(cost_usd, 6))),
                ":one": 1,
                ":ttl_val": ttl,
                ":tid": tenant_id,
            },
        )
    except ClientError:
        pass
