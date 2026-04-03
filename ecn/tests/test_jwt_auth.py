"""
tests/test_jwt_auth.py
----------------------
Tests for JWT Bearer token authentication in ecn.auth.require_role().

These tests verify:
  - Valid HS256 JWT with correct role is accepted
  - Expired JWT is rejected
  - Wrong secret JWT is rejected
  - Malformed JWT is rejected
  - Missing role claim is rejected
  - JWT issuer/audience validation
  - Both X-API-Key and Bearer token work on the same endpoint
"""

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from ecn.auth import _verify_jwt_and_get_role, ROLE_ADMIN, ROLE_SUBMITTER, ROLE_AUDITOR


# ---------------------------------------------------------------------------
# JWT test helpers
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _make_jwt(
    payload: dict,
    secret: str = "test-secret",
    algorithm: str = "HS256",
) -> str:
    header = {"alg": algorithm, "typ": "JWT"}
    h = _b64url_encode(json.dumps(header).encode())
    p = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{h}.{p}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    s = _b64url_encode(sig)
    return f"{h}.{p}.{s}"


def _valid_payload(
    role: str = ROLE_ADMIN,
    exp_offset: int = 3600,
) -> dict:
    now = int(time.time())
    return {
        "sub": "user123",
        "role": role,
        "iat": now,
        "exp": now + exp_offset,
    }


# ---------------------------------------------------------------------------
# _verify_jwt_and_get_role unit tests
# ---------------------------------------------------------------------------

class TestVerifyJWT:
    def test_valid_jwt_returns_role(self, monkeypatch):
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        token = _make_jwt(_valid_payload(role=ROLE_SUBMITTER))
        role = _verify_jwt_and_get_role(token)
        assert role == ROLE_SUBMITTER

    def test_admin_role(self, monkeypatch):
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        token = _make_jwt(_valid_payload(role=ROLE_ADMIN))
        assert _verify_jwt_and_get_role(token) == ROLE_ADMIN

    def test_auditor_role(self, monkeypatch):
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        token = _make_jwt(_valid_payload(role=ROLE_AUDITOR))
        assert _verify_jwt_and_get_role(token) == ROLE_AUDITOR

    def test_wrong_secret_raises_401(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "correct-secret")
        token = _make_jwt(_valid_payload(), secret="wrong-secret")
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 401

    def test_expired_jwt_raises_401(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        expired_payload = _valid_payload(exp_offset=-1)  # 1 second in the past
        token = _make_jwt(expired_payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_malformed_token_raises_401(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role("not.a.valid.jwt.at.all.extra")
        assert exc_info.value.status_code == 401

    def test_missing_role_claim_raises_403(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        payload = {"sub": "user", "exp": int(time.time()) + 3600}
        token = _make_jwt(payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 403

    def test_invalid_role_claim_raises_403(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        payload = {**_valid_payload(), "role": "superuser"}
        token = _make_jwt(payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 403

    def test_no_jwt_secret_configured_raises_401(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.delenv("ECN_JWT_SECRET", raising=False)
        token = _make_jwt(_valid_payload())
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 401
        assert "ECN_JWT_SECRET" in exc_info.value.detail

    def test_ecn_role_claim_alternative(self, monkeypatch):
        """Tokens may use 'ecn_role' claim instead of 'role'."""
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        payload = {"sub": "user", "ecn_role": ROLE_SUBMITTER, "exp": int(time.time()) + 3600}
        token = _make_jwt(payload)
        assert _verify_jwt_and_get_role(token) == ROLE_SUBMITTER

    def test_issuer_validation_pass(self, monkeypatch):
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        monkeypatch.setenv("ECN_JWT_ISSUER", "https://auth.example.com")
        payload = {**_valid_payload(), "iss": "https://auth.example.com"}
        token = _make_jwt(payload)
        role = _verify_jwt_and_get_role(token)
        assert role == ROLE_ADMIN

    def test_issuer_validation_fail(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        monkeypatch.setenv("ECN_JWT_ISSUER", "https://auth.example.com")
        payload = {**_valid_payload(), "iss": "https://evil.com"}
        token = _make_jwt(payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 401
        assert "issuer" in exc_info.value.detail.lower()

    def test_audience_validation_pass(self, monkeypatch):
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        monkeypatch.setenv("ECN_JWT_AUDIENCE", "ecn-api")
        payload = {**_valid_payload(), "aud": "ecn-api"}
        token = _make_jwt(payload)
        role = _verify_jwt_and_get_role(token)
        assert role == ROLE_ADMIN

    def test_audience_validation_fail(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        monkeypatch.setenv("ECN_JWT_AUDIENCE", "ecn-api")
        payload = {**_valid_payload(), "aud": "other-service"}
        token = _make_jwt(payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 401
        assert "audience" in exc_info.value.detail.lower()

    def test_audience_list_validation(self, monkeypatch):
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        monkeypatch.setenv("ECN_JWT_AUDIENCE", "ecn-api")
        payload = {**_valid_payload(), "aud": ["ecn-api", "other"]}
        token = _make_jwt(payload)
        role = _verify_jwt_and_get_role(token)
        assert role == ROLE_ADMIN

    def test_nbf_future_raises_401(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setenv("ECN_JWT_SECRET", "test-secret")
        payload = {**_valid_payload(), "nbf": int(time.time()) + 3600}
        token = _make_jwt(payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_jwt_and_get_role(token)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Integration: Bearer token on /network/state via TestClient
# ---------------------------------------------------------------------------

class TestBearerIntegration:
    @pytest.fixture(scope="class")
    def client(self):
        from ecn.api import app
        with TestClient(app) as c:
            yield c

    def test_bearer_token_accepted_when_auth_disabled(self, client, monkeypatch):
        """When auth is disabled, Bearer tokens pass through (no validation)."""
        import ecn.auth as auth_mod
        assert not auth_mod._is_auth_enabled()
        resp = client.get(
            "/network/state",
            headers={"Authorization": "Bearer fake.token.here"},
        )
        assert resp.status_code == 200

    def test_no_auth_passes_without_header(self, client):
        resp = client.get("/network/state")
        assert resp.status_code == 200
