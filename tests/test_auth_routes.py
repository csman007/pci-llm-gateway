"""Tests for /auth/login and /auth/change-password routes."""
import os
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


@pytest.fixture()
def app():
    os.environ["COGNITO_CLIENT_ID"] = "test-client-id"
    from main import app as _app
    return _app


@pytest.fixture()
def client(app):
    return TestClient(app)


# ── /auth/login ────────────────────────────────────────────────────────────────

def test_login_no_cognito_configured(client):
    saved = os.environ.pop("COGNITO_CLIENT_ID", None)
    try:
        resp = client.post("/auth/login", json={"username": "u", "password": "p"})
        assert resp.status_code == 404
    finally:
        if saved:
            os.environ["COGNITO_CLIENT_ID"] = saved


def test_login_success(client):
    mock_result = {
        "AuthenticationResult": {"AccessToken": "access-token-xyz"}
    }
    with patch("routes.auth._cognito") as mk:
        mk.return_value.initiate_auth.return_value = mock_result
        resp = client.post("/auth/login", json={"username": "user@example.com", "password": "Pass123!"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "access-token-xyz"
    assert data["token_type"] == "bearer"


def test_login_new_password_required(client):
    mock_result = {
        "ChallengeName": "NEW_PASSWORD_REQUIRED",
        "Session": "session-token-abc",
        "ChallengeParameters": {},
    }
    with patch("routes.auth._cognito") as mk:
        mk.return_value.initiate_auth.return_value = mock_result
        resp = client.post("/auth/login", json={"username": "user@example.com", "password": "Temp123!"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["challenge"] == "NEW_PASSWORD_REQUIRED"
    assert data["session"] == "session-token-abc"


def test_login_wrong_credentials(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.initiate_auth.side_effect = _client_error("NotAuthorizedException")
        resp = client.post("/auth/login", json={"username": "u", "password": "wrong"})

    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


def test_login_user_not_found(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.initiate_auth.side_effect = _client_error("UserNotFoundException")
        resp = client.post("/auth/login", json={"username": "ghost@example.com", "password": "p"})

    assert resp.status_code == 401


def test_login_password_reset_required(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.initiate_auth.side_effect = _client_error("PasswordResetRequiredException")
        resp = client.post("/auth/login", json={"username": "u", "password": "p"})

    assert resp.status_code == 403


def test_login_cognito_error(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.initiate_auth.side_effect = _client_error("InternalErrorException")
        resp = client.post("/auth/login", json={"username": "u", "password": "p"})

    assert resp.status_code == 502


# ── /auth/change-password ──────────────────────────────────────────────────────

def test_change_password_success(client):
    mock_result = {
        "AuthenticationResult": {"AccessToken": "new-access-token"}
    }
    with patch("routes.auth._cognito") as mk:
        mk.return_value.respond_to_auth_challenge.return_value = mock_result
        resp = client.post(
            "/auth/change-password",
            json={"username": "u", "new_password": "NewPass123!", "session": "sess"},
        )

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "new-access-token"


def test_change_password_invalid_password(client):
    err = ClientError(
        {"Error": {"Code": "InvalidPasswordException", "Message": "Password does not meet requirements"}},
        "op",
    )
    with patch("routes.auth._cognito") as mk:
        mk.return_value.respond_to_auth_challenge.side_effect = err
        resp = client.post(
            "/auth/change-password",
            json={"username": "u", "new_password": "weak", "session": "sess"},
        )

    assert resp.status_code == 422
    assert "requirements" in resp.json()["detail"]


def test_change_password_expired_session(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.respond_to_auth_challenge.side_effect = _client_error("NotAuthorizedException")
        resp = client.post(
            "/auth/change-password",
            json={"username": "u", "new_password": "NewPass123!", "session": "expired"},
        )

    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_change_password_expired_code(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.respond_to_auth_challenge.side_effect = _client_error("ExpiredCodeException")
        resp = client.post(
            "/auth/change-password",
            json={"username": "u", "new_password": "NewPass123!", "session": "expired"},
        )

    assert resp.status_code == 401


def test_change_password_cognito_error(client):
    with patch("routes.auth._cognito") as mk:
        mk.return_value.respond_to_auth_challenge.side_effect = _client_error("InternalErrorException")
        resp = client.post(
            "/auth/change-password",
            json={"username": "u", "new_password": "NewPass123!", "session": "sess"},
        )

    assert resp.status_code == 502
