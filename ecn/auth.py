"""
auth.py
-------
Enterprise Identity and RBAC for ECN.

Role-based access control layered over the FastAPI API.  The implementation
is deliberately self-contained: it uses an in-memory API key registry and
requires no external identity provider.

Activation
----------
RBAC is **opt-in**.  Set the environment variable::

    ECN_AUTH_ENABLED=1

to enable enforcement.  When not set (the default), all requests are treated
as having full ``admin`` permissions — preserving backward compatibility with
existing clients and tests.

Bootstrap admin key
-------------------
When auth is enabled a bootstrap admin key must be provided::

    ECN_ADMIN_KEY=<secret-key>

If ``ECN_ADMIN_KEY`` is not set ECN generates a random key at startup and
logs it at WARNING level so it can be captured from the container logs.

Roles
-----
+------------+---------------------------------------------------------------+
| Role       | Permitted operations                                          |
+============+===============================================================+
| admin      | All operations + manage API keys                              |
+------------+---------------------------------------------------------------+
| submitter  | POST /transactions + all GET endpoints                        |
+------------+---------------------------------------------------------------+
| auditor    | GET /audit/* + GET /network/* + GET /stream/*                 |
+------------+---------------------------------------------------------------+
| readonly   | GET /network/state + GET /audit/summary only                  |
+------------+---------------------------------------------------------------+

Usage in routes::

    from ecn.auth import require_role
    from fastapi import Depends

    @app.post("/transactions")
    async def submit(body: ..., _=Depends(require_role("submitter", "admin"))):
        ...

Admin API key management routes are added to ``api.py``:
    POST   /admin/api-keys       — create a new key
    GET    /admin/api-keys       — list all keys (values redacted)
    DELETE /admin/api-keys/{id}  — revoke a key
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_ADMIN = "admin"
ROLE_SUBMITTER = "submitter"
ROLE_AUDITOR = "auditor"
ROLE_READONLY = "readonly"

ALL_ROLES = {ROLE_ADMIN, ROLE_SUBMITTER, ROLE_AUDITOR, ROLE_READONLY}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ApiKey:
    """An issued API key bound to a role."""
    key_id: str
    key: str          # the secret value shown once at creation
    role: str
    description: str
    tenant_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ApiKeyRegistry:
    """
    In-memory API key registry.

    Thread-safety: single-threaded asyncio — no locking needed.
    """

    def __init__(self) -> None:
        self._keys: Dict[str, ApiKey] = {}  # key_id → ApiKey
        self._lookup: Dict[str, ApiKey] = {}  # secret_value → ApiKey

    def issue(
        self,
        role: str,
        description: str = "",
        tenant_id: Optional[str] = None,
        key_id: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> ApiKey:
        """
        Issue a new API key.

        Parameters
        ----------
        role : str
        description : str
        tenant_id : str, optional
        key_id : str, optional
            Explicit ID (for bootstrapping from env vars).
        secret : str, optional
            Explicit secret (for bootstrapping from env vars).

        Returns
        -------
        ApiKey
            The new key (``key`` field contains the secret value).
        """
        if role not in ALL_ROLES:
            raise ValueError(f"Unknown role: {role!r}. Valid roles: {ALL_ROLES}")
        api_key = ApiKey(
            key_id=key_id or str(uuid.uuid4()),
            key=secret or secrets.token_urlsafe(32),
            role=role,
            description=description,
            tenant_id=tenant_id,
        )
        self._keys[api_key.key_id] = api_key
        self._lookup[api_key.key] = api_key
        return api_key

    def revoke(self, key_id: str) -> bool:
        """Revoke a key by its ID.  Returns True if found, False otherwise."""
        api_key = self._keys.pop(key_id, None)
        if api_key is None:
            return False
        self._lookup.pop(api_key.key, None)
        return True

    def lookup(self, secret: str) -> Optional[ApiKey]:
        """Return the ApiKey for *secret*, or None if not found."""
        return self._lookup.get(secret)

    def list_keys(self) -> List[Dict]:
        """Return all keys with the secret value redacted."""
        return [
            {
                "key_id": k.key_id,
                "role": k.role,
                "description": k.description,
                "tenant_id": k.tenant_id,
            }
            for k in self._keys.values()
        ]

    def __len__(self) -> int:
        return len(self._keys)


# ---------------------------------------------------------------------------
# Module-level registry singleton
# (populated during API lifespan startup)
# ---------------------------------------------------------------------------

_registry: Optional[ApiKeyRegistry] = None


def get_registry() -> ApiKeyRegistry:
    """Return the global ApiKeyRegistry (raises if not initialised)."""
    if _registry is None:
        raise RuntimeError("ApiKeyRegistry not initialised")
    return _registry


def init_registry() -> ApiKeyRegistry:
    """
    Initialise the global ApiKeyRegistry.

    If ``ECN_AUTH_ENABLED`` is set, bootstraps an admin key from
    ``ECN_ADMIN_KEY`` (or generates and logs a random one).

    Returns the registry regardless of whether auth is enabled.
    """
    global _registry
    _registry = ApiKeyRegistry()

    if _is_auth_enabled():
        admin_secret = os.environ.get("ECN_ADMIN_KEY", "").strip()
        if not admin_secret:
            admin_secret = secrets.token_urlsafe(32)
            # Write to stdout — not to the log — so it does not end up in
            # log-aggregation systems (SIEM, Splunk, CloudWatch, etc.).
            # In production always set ECN_ADMIN_KEY explicitly.
            import sys
            print(
                f"[ECN] ECN_AUTH_ENABLED is set but ECN_ADMIN_KEY is not. "
                f"Generated bootstrap admin key (set ECN_ADMIN_KEY to avoid regeneration): "
                f"{admin_secret}",
                file=sys.stderr,
            )
            logger.warning(
                "ECN_AUTH_ENABLED is set but ECN_ADMIN_KEY is not — "
                "generated a temporary bootstrap key (see stderr for value). "
                "Set ECN_ADMIN_KEY to use a fixed key."
            )
        admin_key = _registry.issue(
            role=ROLE_ADMIN,
            description="bootstrap admin",
            key_id="admin-bootstrap",
            secret=admin_secret,
        )
        logger.info("Auth enabled. Admin key_id=%s", admin_key.key_id)

    return _registry


def _is_auth_enabled() -> bool:
    """Return True if ECN_AUTH_ENABLED is set to a truthy value."""
    val = os.environ.get("ECN_AUTH_ENABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# FastAPI dependency factories
# ---------------------------------------------------------------------------

def require_role(*allowed_roles: str):
    """
    FastAPI dependency factory.

    Returns a dependency function that:
    - Passes immediately when auth is disabled (backward compatible).
    - Validates ``X-API-Key`` header and checks the role when auth is enabled.

    Usage::

        @app.post("/transactions")
        async def submit(_, _auth=Depends(require_role("submitter", "admin"))):
            ...
    """
    async def _check(x_api_key: Optional[str] = Header(default=None)):
        if not _is_auth_enabled():
            return  # auth disabled — all requests pass
        if _registry is None:
            raise HTTPException(status_code=503, detail="Auth registry not initialised")
        if not x_api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing X-API-Key header",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        api_key = _registry.lookup(x_api_key)
        if api_key is None:
            raise HTTPException(status_code=403, detail="Invalid or revoked API key")
        if api_key.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Role {api_key.role!r} is not authorised for this operation. "
                       f"Required: {list(allowed_roles)}",
            )

    return _check


def require_admin():
    """Convenience: require admin role."""
    return require_role(ROLE_ADMIN)
