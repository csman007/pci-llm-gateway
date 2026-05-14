"""Unit and contract tests for tenant config loading, quota enforcement, and route isolation."""

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── TenantConfig + get_tenant_config ─────────────────────────────────────────


@pytest.mark.unit
def test_returns_permissive_default_when_table_not_configured():
    import tenant

    with patch.multiple(tenant, _TENANTS_TABLE="", _dynamodb=None, _cache={}):
        cfg = tenant.get_tenant_config("acme")

    assert cfg.tenant_id == "acme"
    assert cfg.allowed_models is None
    assert cfg.blocked_entity_types == []
    assert cfg.monthly_budget_usd is None


@pytest.mark.unit
def test_loads_config_from_dynamodb():
    import tenant

    item = {
        "pk": "tenant#acme",
        "allowed_models": ["claude-haiku-4-5-20251001"],
        "blocked_entity_types": ["PHONE"],
        "monthly_budget_usd": Decimal("50.00"),
        "rate_limit_inference_rpm": 30,
    }
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": item}
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb, _cache={}):
        cfg = tenant.get_tenant_config("acme")

    assert cfg.allowed_models == ["claude-haiku-4-5-20251001"]
    assert cfg.blocked_entity_types == ["PHONE"]
    assert cfg.monthly_budget_usd == 50.0
    assert cfg.rate_limit_inference_rpm == 30


@pytest.mark.unit
def test_ttl_cache_avoids_second_dynamodb_call():
    import tenant

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {}}
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb, _cache={}):
        tenant.get_tenant_config("acme")
        tenant.get_tenant_config("acme")

    assert mock_table.get_item.call_count == 1


@pytest.mark.unit
def test_cache_expired_triggers_refetch():
    import tenant

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {}}
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})
    stale_cache = {"acme": (tenant.TenantConfig(tenant_id="acme"), time.monotonic() - 999)}

    with patch.multiple(tenant, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb, _cache=stale_cache):
        tenant.get_tenant_config("acme")

    assert mock_table.get_item.call_count == 1


@pytest.mark.unit
def test_dynamodb_error_returns_permissive_default():
    from botocore.exceptions import ClientError

    import tenant

    mock_table = MagicMock()
    mock_table.get_item.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": ""}}, "GetItem")
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb, _cache={}):
        cfg = tenant.get_tenant_config("acme")

    assert cfg.monthly_budget_usd is None


# ── tenant_quota ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_check_budget_passes_when_under_limit():
    import tenant_quota

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"spend_usd": Decimal("40.00")}}
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant_quota, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb):
        tenant_quota.check_budget("acme", 50.0)  # should not raise


@pytest.mark.unit
def test_check_budget_raises_429_when_exhausted():
    from fastapi import HTTPException

    import tenant_quota

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"spend_usd": Decimal("50.00")}}
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant_quota, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb):
        with pytest.raises(HTTPException) as exc_info:
            tenant_quota.check_budget("acme", 50.0)

    assert exc_info.value.status_code == 429
    assert "X-Tenant-Budget-Remaining" in exc_info.value.headers


@pytest.mark.unit
def test_check_budget_fails_open_on_dynamodb_error():
    from botocore.exceptions import ClientError

    import tenant_quota

    mock_table = MagicMock()
    mock_table.get_item.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": ""}}, "GetItem")
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant_quota, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb):
        tenant_quota.check_budget("acme", 1.0)  # must not raise


@pytest.mark.unit
def test_record_spend_atomically_increments():
    import tenant_quota

    mock_table = MagicMock()
    mock_ddb = MagicMock(**{"Table.return_value": mock_table})

    with patch.multiple(tenant_quota, _TENANTS_TABLE="test-tenants", _dynamodb=mock_ddb):
        tenant_quota.record_spend("acme", 0.0028)

    call = mock_table.update_item.call_args
    assert call.kwargs["ExpressionAttributeValues"][":cost"] == Decimal("0.0028")
    assert ":one" in call.kwargs["ExpressionAttributeValues"]


@pytest.mark.unit
def test_record_spend_noop_when_no_table():
    import tenant_quota

    with patch.multiple(tenant_quota, _TENANTS_TABLE="", _dynamodb=None):
        tenant_quota.record_spend("acme", 99.99)  # must not raise


# ── contract: model allow-list and policy enforcement ─────────────────────────


def _make_client(tenant_override):
    """Build a TestClient with the given TenantConfig injected via dependency override."""
    import jwt as _jwt
    from fastapi.testclient import TestClient
    from main import app
    from tenant import require_tenant

    app.dependency_overrides[require_tenant] = lambda: tenant_override
    client = TestClient(app, raise_server_exceptions=False)
    token = _jwt.encode({"sub": "test-user", "tenant_id": tenant_override.tenant_id}, "test-secret-that-is-long-enough-for-hs256", algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return client, headers


@pytest.mark.contract
def test_inference_blocked_when_model_not_in_allowlist():
    from tenant import TenantConfig

    restricted = TenantConfig(tenant_id="acme", allowed_models=["claude-haiku-4-5-20251001"])
    client, headers = _make_client(restricted)

    resp = client.post("/v1/inference", headers=headers, json={"prompt": "Hello", "model": "claude-sonnet-4-6", "max_tokens": 64})

    assert resp.status_code == 422
    assert "not permitted" in resp.json()["detail"]


@pytest.mark.contract
def test_inference_allowed_when_model_in_allowlist():
    from unittest.mock import AsyncMock, MagicMock, patch

    from llm_response import LLMResponse
    from tenant import TenantConfig

    allowed = TenantConfig(tenant_id="acme", allowed_models=["claude-sonnet-4-6"])
    client, headers = _make_client(allowed)

    mock_resp = LLMResponse(text="Hi!", prompt_tokens=10, completion_tokens=5, model="claude-sonnet-4-6")
    with patch("routes.inference.get_client") as mock_get:
        mock_get.return_value.complete = AsyncMock(return_value=mock_resp)
        resp = client.post("/v1/inference", headers=headers, json={"prompt": "Hello", "model": "claude-sonnet-4-6", "max_tokens": 64})

    assert resp.status_code == 200


@pytest.mark.contract
def test_inference_blocked_by_tenant_entity_policy():
    from tenant import TenantConfig

    strict = TenantConfig(tenant_id="acme", blocked_entity_types=["PHONE"])
    client, headers = _make_client(strict)

    resp = client.post("/v1/inference", headers=headers, json={"prompt": "Call me on +1-800-555-0100", "model": "claude-sonnet-4-6", "max_tokens": 64})

    assert resp.status_code == 400
    assert "tenant policy" in resp.json()["detail"]
