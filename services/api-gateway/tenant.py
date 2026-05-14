"""Tenant configuration loader with TTL cache and FastAPI dependency.

When TENANTS_TABLE is unset, multi-tenancy is disabled and every request
is assigned to the permissive "default" tenant — local dev is unrestricted.
"""

import os
import time
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError
from fastapi import Request

_TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")
_CACHE_TTL_SECS = int(os.environ.get("TENANT_CACHE_TTL_SECS", "60"))

_dynamodb = (
    boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION_NAME", "us-east-1")) if _TENANTS_TABLE else None
)

# Simple in-process TTL cache — one entry per tenant_id per Lambda instance.
_cache: dict[str, tuple["TenantConfig", float]] = {}


@dataclass
class TenantConfig:
    """Immutable configuration snapshot for a single tenant.

    Attributes:
        tenant_id:               Opaque identifier matching the JWT ``tenant_id`` claim.
        allowed_models:          Whitelist of model IDs the tenant may request.
                                 ``None`` means all models are allowed.
        blocked_entity_types:    Additional PII/PAN entity types blocked for this
                                 tenant beyond the global policy (e.g. ``["PHONE"]``).
        monthly_budget_usd:      Hard monthly spend cap in USD. ``None`` = unlimited.
        rate_limit_inference_rpm: Per-user inference cap; ``None`` uses the global default.
        rate_limit_rag_rpm:      Per-user RAG cap; ``None`` uses the global default.
        rate_limit_agent_rpm:    Per-user agent cap; ``None`` uses the global default.
    """

    tenant_id: str
    allowed_models: list[str] | None = None
    blocked_entity_types: list[str] = field(default_factory=list)
    monthly_budget_usd: float | None = None
    rate_limit_inference_rpm: int | None = None
    rate_limit_rag_rpm: int | None = None
    rate_limit_agent_rpm: int | None = None


def get_tenant_config(tenant_id: str) -> TenantConfig:
    """Return the TenantConfig for *tenant_id*, fetching from DynamoDB with TTL cache.

    Falls back to a permissive default config on any DynamoDB error (fail-open).

    Args:
        tenant_id: Tenant identifier from the JWT ``tenant_id`` claim.

    Returns:
        TenantConfig for the tenant, or a permissive default if multi-tenancy is
        disabled or the tenant is not found.
    """
    now = time.monotonic()

    cached, fetched_at = _cache.get(tenant_id, (None, 0.0))
    if cached is not None and now - fetched_at < _CACHE_TTL_SECS:
        return cached

    if not _TENANTS_TABLE or _dynamodb is None:
        config = TenantConfig(tenant_id=tenant_id)
        _cache[tenant_id] = (config, now)
        return config

    try:
        item = _dynamodb.Table(_TENANTS_TABLE).get_item(Key={"pk": f"tenant#{tenant_id}"}).get("Item", {})
        config = TenantConfig(
            tenant_id=tenant_id,
            allowed_models=list(item["allowed_models"]) if "allowed_models" in item else None,
            blocked_entity_types=list(item.get("blocked_entity_types", [])),
            monthly_budget_usd=float(item["monthly_budget_usd"]) if "monthly_budget_usd" in item else None,
            rate_limit_inference_rpm=int(item["rate_limit_inference_rpm"])
            if "rate_limit_inference_rpm" in item
            else None,
            rate_limit_rag_rpm=int(item["rate_limit_rag_rpm"]) if "rate_limit_rag_rpm" in item else None,
            rate_limit_agent_rpm=int(item["rate_limit_agent_rpm"]) if "rate_limit_agent_rpm" in item else None,
        )
    except ClientError:
        config = TenantConfig(tenant_id=tenant_id)

    _cache[tenant_id] = (config, now)
    return config


async def require_tenant(request: Request) -> TenantConfig:
    """FastAPI dependency — resolve TenantConfig from the authenticated request.

    Reads ``tenant_id`` from ``request.state.user`` (set by AuthMiddleware after
    JWT validation). Defaults to ``"default"`` when the claim is absent.

    Args:
        request: The incoming FastAPI request with ``state.user`` populated.

    Returns:
        TenantConfig for the caller's tenant.
    """
    user = getattr(request.state, "user", {})
    tenant_id = user.get("tenant_id", "default") if isinstance(user, dict) else "default"
    return get_tenant_config(tenant_id)
