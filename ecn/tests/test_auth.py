"""
tests/test_auth.py
------------------
Tests for ecn.auth — ApiKeyRegistry and admin route integration.
"""

import os
import pytest

from ecn.auth import (
    ApiKeyRegistry,
    ROLE_ADMIN, ROLE_SUBMITTER, ROLE_AUDITOR, ROLE_READONLY,
    ALL_ROLES,
)


# ---------------------------------------------------------------------------
# ApiKeyRegistry unit tests
# ---------------------------------------------------------------------------

class TestApiKeyRegistry:
    def test_issue_returns_key(self):
        reg = ApiKeyRegistry()
        key = reg.issue(role=ROLE_ADMIN, description="test")
        assert key.role == ROLE_ADMIN
        assert key.key  # non-empty secret
        assert key.key_id

    def test_issue_all_roles(self):
        reg = ApiKeyRegistry()
        for role in ALL_ROLES:
            k = reg.issue(role=role)
            assert k.role == role

    def test_issue_invalid_role_raises(self):
        reg = ApiKeyRegistry()
        with pytest.raises(ValueError):
            reg.issue(role="superuser")

    def test_lookup_by_secret(self):
        reg = ApiKeyRegistry()
        key = reg.issue(role=ROLE_SUBMITTER)
        found = reg.lookup(key.key)
        assert found is not None
        assert found.key_id == key.key_id

    def test_lookup_unknown_returns_none(self):
        reg = ApiKeyRegistry()
        assert reg.lookup("not-a-real-key") is None

    def test_revoke_removes_key(self):
        reg = ApiKeyRegistry()
        key = reg.issue(role=ROLE_READONLY)
        assert reg.revoke(key.key_id) is True
        assert reg.lookup(key.key) is None
        assert len(reg) == 0

    def test_revoke_nonexistent_returns_false(self):
        reg = ApiKeyRegistry()
        assert reg.revoke("no-such-id") is False

    def test_list_keys_redacts_secrets(self):
        reg = ApiKeyRegistry()
        key = reg.issue(role=ROLE_AUDITOR, description="audit-key")
        listing = reg.list_keys()
        assert len(listing) == 1
        assert "key" not in listing[0]  # secret must not appear
        assert listing[0]["role"] == ROLE_AUDITOR
        assert listing[0]["description"] == "audit-key"

    def test_len(self):
        reg = ApiKeyRegistry()
        assert len(reg) == 0
        reg.issue(role=ROLE_ADMIN)
        assert len(reg) == 1
        reg.issue(role=ROLE_SUBMITTER)
        assert len(reg) == 2

    def test_explicit_secret(self):
        reg = ApiKeyRegistry()
        key = reg.issue(role=ROLE_ADMIN, key_id="fixed-id", secret="my-secret")
        assert key.key == "my-secret"
        assert key.key_id == "fixed-id"
        assert reg.lookup("my-secret") is not None

    def test_tenant_id_stored(self):
        reg = ApiKeyRegistry()
        key = reg.issue(role=ROLE_SUBMITTER, tenant_id="tenant-abc")
        assert key.tenant_id == "tenant-abc"
        listing = reg.list_keys()
        assert listing[0]["tenant_id"] == "tenant-abc"

    def test_unique_keys_per_issue(self):
        reg = ApiKeyRegistry()
        k1 = reg.issue(role=ROLE_AUDITOR)
        k2 = reg.issue(role=ROLE_AUDITOR)
        assert k1.key != k2.key
        assert k1.key_id != k2.key_id


# ---------------------------------------------------------------------------
# Admin route integration tests (auth disabled — backward compat)
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from ecn.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestAdminRoutes:
    """When ECN_AUTH_ENABLED is not set, admin routes work without a key."""

    def test_issue_key_returns_201(self, client):
        resp = client.post("/admin/api-keys", json={"role": "submitter", "description": "ci-bot"})
        assert resp.status_code == 201

    def test_issue_key_response_fields(self, client):
        resp = client.post("/admin/api-keys", json={"role": "auditor"})
        data = resp.json()
        assert "key_id" in data
        assert "key" in data
        assert data["role"] == "auditor"
        assert "message" in data

    def test_issue_invalid_role_returns_422(self, client):
        resp = client.post("/admin/api-keys", json={"role": "god"})
        assert resp.status_code == 422

    def test_list_keys_returns_200(self, client):
        resp = client.get("/admin/api-keys")
        assert resp.status_code == 200
        assert "keys" in resp.json()

    def test_revoke_key(self, client):
        # Issue a key
        resp = client.post("/admin/api-keys", json={"role": "readonly", "description": "temp"})
        key_id = resp.json()["key_id"]
        # Revoke it
        del_resp = client.delete(f"/admin/api-keys/{key_id}")
        assert del_resp.status_code == 204

    def test_revoke_nonexistent_returns_404(self, client):
        resp = client.delete("/admin/api-keys/no-such-key-id")
        assert resp.status_code == 404

    def test_list_keys_does_not_expose_secrets(self, client):
        client.post("/admin/api-keys", json={"role": "auditor"})
        keys = client.get("/admin/api-keys").json()["keys"]
        for k in keys:
            assert "key" not in k  # secret value must not be exposed


# ---------------------------------------------------------------------------
# Auth enforcement tests (ECN_AUTH_ENABLED=1)
# These test the auth dependency directly without booting a new network.
# ---------------------------------------------------------------------------

class TestAuthEnforcement:
    """Tests that verify auth is enforced when ECN_AUTH_ENABLED=1."""

    def test_missing_key_returns_401(self):
        import asyncio
        os.environ["ECN_AUTH_ENABLED"] = "1"
        os.environ["ECN_ADMIN_KEY"] = "test-admin-secret"
        try:
            from ecn.auth import init_registry, require_admin
            init_registry()
            check = require_admin()

            async def _run():
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as exc_info:
                    await check(x_api_key=None)
                assert exc_info.value.status_code == 401

            asyncio.run(_run())
        finally:
            os.environ.pop("ECN_AUTH_ENABLED", None)
            os.environ.pop("ECN_ADMIN_KEY", None)
            from ecn.auth import init_registry
            init_registry()

    def test_valid_admin_key_accepted(self):
        import asyncio
        os.environ["ECN_AUTH_ENABLED"] = "1"
        os.environ["ECN_ADMIN_KEY"] = "test-admin-secret"
        try:
            from ecn.auth import init_registry, require_admin
            init_registry()
            check = require_admin()

            async def _run():
                # Should not raise
                await check(x_api_key="test-admin-secret")

            asyncio.run(_run())
        finally:
            os.environ.pop("ECN_AUTH_ENABLED", None)
            os.environ.pop("ECN_ADMIN_KEY", None)
            from ecn.auth import init_registry
            init_registry()

    def test_wrong_key_returns_403(self):
        import asyncio
        os.environ["ECN_AUTH_ENABLED"] = "1"
        os.environ["ECN_ADMIN_KEY"] = "test-admin-secret"
        try:
            from ecn.auth import init_registry, require_admin
            init_registry()
            check = require_admin()

            async def _run():
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as exc_info:
                    await check(x_api_key="wrong-key")
                assert exc_info.value.status_code == 403

            asyncio.run(_run())
        finally:
            os.environ.pop("ECN_AUTH_ENABLED", None)
            os.environ.pop("ECN_ADMIN_KEY", None)
            from ecn.auth import init_registry
            init_registry()

    def test_non_admin_role_returns_403(self):
        import asyncio
        os.environ["ECN_AUTH_ENABLED"] = "1"
        os.environ["ECN_ADMIN_KEY"] = "test-admin-secret"
        try:
            from ecn.auth import init_registry, require_role, ROLE_ADMIN, get_registry
            init_registry()
            # Issue a submitter key
            sub_key = get_registry().issue(role="submitter")
            check = require_role(ROLE_ADMIN)

            async def _run():
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as exc_info:
                    await check(x_api_key=sub_key.key)
                assert exc_info.value.status_code == 403

            asyncio.run(_run())
        finally:
            os.environ.pop("ECN_AUTH_ENABLED", None)
            os.environ.pop("ECN_ADMIN_KEY", None)
            from ecn.auth import init_registry
            init_registry()

    def test_auth_disabled_passes_without_key(self):
        import asyncio
        # Make sure ECN_AUTH_ENABLED is not set
        os.environ.pop("ECN_AUTH_ENABLED", None)
        from ecn.auth import init_registry, require_admin
        init_registry()
        check = require_admin()

        async def _run():
            # Should not raise even with no key
            await check(x_api_key=None)

        asyncio.run(_run())
