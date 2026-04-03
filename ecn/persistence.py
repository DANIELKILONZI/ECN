"""
persistence.py
--------------
Pluggable persistence layer for ECN.

By default ECN keeps all data in memory — existing behaviour is unchanged.
Set ``ECN_DB_PATH`` to enable durable SQLite storage that survives pod
restarts:

    ECN_DB_PATH=/var/ecn/ecn.db

For Postgres in production use a ``postgresql://…`` URL as ECN_DB_PATH
and configure SQLAlchemy externally; the SQLite driver is the zero-dep
default that works out of the box.

Architecture
------------
Two thin store abstractions (``AuditStore``, ``KeyStore``) are injected
into ``AuditLog`` and ``ApiKeyRegistry`` respectively.  When ``None`` is
passed the objects fall back to their existing in-memory-only behaviour, so
no existing test or caller needs to change.

Usage
-----
::

    from ecn.persistence import open_from_env

    stores = open_from_env()
    audit_log = AuditLog(store=stores["audit_store"])
    registry  = ApiKeyRegistry(store=stores["key_store"])

Environment variables
---------------------
``ECN_DB_PATH``
    File path (or ``:memory:`` for a transient in-memory SQLite DB).
    Leave unset to use the old in-memory-only behaviour.

``ECN_DB_TENANT_ID``
    Optional tenant scope used when this process is a dedicated tenant
    sidecar.  Normally the tenant_id is passed per-call.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract store contracts
# ---------------------------------------------------------------------------

class AuditStore:
    """Persistence contract for AuditLog events."""

    def append(self, event_dict: Dict[str, Any], tenant_id: str = "") -> None:
        """Persist a single audit event (idempotent on replay)."""

    def load_all(self, tenant_id: str = "") -> List[Dict[str, Any]]:
        """Return all stored events for *tenant_id* in insertion order."""
        return []


class KeyStore:
    """Persistence contract for ApiKeyRegistry entries."""

    def save_key(self, key_id: str, key_dict: Dict[str, Any]) -> None:
        """Upsert a key record."""

    def delete_key(self, key_id: str) -> None:
        """Remove a key record (no-op if missing)."""

    def load_all(self) -> List[Dict[str, Any]]:
        """Return all stored key records."""
        return []


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

class SQLiteAuditStore(AuditStore):
    """
    SQLite-backed audit store.

    Thread-safety: uses ``check_same_thread=False`` with connection-per-call
    semantics to avoid cross-thread errors in test environments.  Each call
    opens and closes a connection so no long-lived state is held, except for
    in-memory databases (``":memory:"``) which share a single connection to
    preserve data across calls.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # For :memory: databases keep a single persistent connection
        # so that tables created in _init_db are visible in later calls.
        self._mem_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    data      TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_tenant"
                " ON audit_events (tenant_id)"
            )

    def append(self, event_dict: Dict[str, Any], tenant_id: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_events (tenant_id, data) VALUES (?, ?)",
                (tenant_id, json.dumps(event_dict, default=str)),
            )

    def load_all(self, tenant_id: str = "") -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM audit_events WHERE tenant_id = ? ORDER BY id",
                (tenant_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


class SQLiteKeyStore(KeyStore):
    """SQLite-backed API key store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._mem_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id TEXT PRIMARY KEY,
                    data   TEXT NOT NULL
                )
                """
            )

    def save_key(self, key_id: str, key_dict: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO api_keys (key_id, data) VALUES (?, ?)",
                (key_id, json.dumps(key_dict)),
            )

    def delete_key(self, key_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))

    def load_all(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM api_keys").fetchall()
        return [json.loads(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# BillingStore — rounds per tenant per period
# ---------------------------------------------------------------------------

class BillingStore:
    """Persistence contract for billing counters."""

    def increment(self, tenant_id: str, period: str) -> None:
        """Increment the round counter for *tenant_id* in *period*."""

    def get_count(self, tenant_id: str, period: str) -> int:
        """Return current round count for *tenant_id* in *period*."""
        return 0

    def get_all_periods(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Return all recorded billing periods for *tenant_id*."""
        return []


class SQLiteBillingStore(BillingStore):
    """SQLite-backed billing counter store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._mem_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS billing_usage (
                    tenant_id   TEXT NOT NULL,
                    period      TEXT NOT NULL,
                    round_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, period)
                )
                """
            )

    def increment(self, tenant_id: str, period: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO billing_usage (tenant_id, period, round_count)
                VALUES (?, ?, 1)
                ON CONFLICT(tenant_id, period)
                DO UPDATE SET round_count = round_count + 1
                """,
                (tenant_id, period),
            )

    def get_count(self, tenant_id: str, period: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT round_count FROM billing_usage"
                " WHERE tenant_id = ? AND period = ?",
                (tenant_id, period),
            ).fetchone()
        return row[0] if row else 0

    def get_all_periods(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT period, round_count FROM billing_usage"
                " WHERE tenant_id = ? ORDER BY period",
                (tenant_id,),
            ).fetchall()
        return [{"period": r[0], "round_count": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Factory: open stores from environment
# ---------------------------------------------------------------------------

def open_from_env() -> Dict[str, Optional[Any]]:
    """
    Return store instances driven by ``ECN_DB_PATH``.

    Returns a dict::

        {
            "audit_store":   SQLiteAuditStore | None,
            "key_store":     SQLiteKeyStore   | None,
            "billing_store": SQLiteBillingStore | None,
        }

    When ``ECN_DB_PATH`` is not set all values are ``None`` and the calling
    modules fall back to their existing in-memory behaviour.
    """
    db_path = os.environ.get("ECN_DB_PATH", "").strip()
    if not db_path:
        return {"audit_store": None, "key_store": None, "billing_store": None}

    logger.info("ECN persistence enabled: db_path=%s", db_path)
    return {
        "audit_store": SQLiteAuditStore(db_path),
        "key_store": SQLiteKeyStore(db_path),
        "billing_store": SQLiteBillingStore(db_path),
    }
