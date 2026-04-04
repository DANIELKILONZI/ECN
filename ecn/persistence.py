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
# TransactionStore — lifecycle records per transaction
# ---------------------------------------------------------------------------

class TransactionStore:
    """Persistence contract for TransactionRecord lifecycle entries."""

    def save(self, record_dict: Dict[str, Any]) -> None:
        """Upsert a transaction record (keyed on tx_id)."""

    def load(self, tx_id: str) -> Optional[Dict[str, Any]]:
        """Return the record for *tx_id*, or None if not found."""
        return None

    def load_all(self, tenant_id: str = "") -> List[Dict[str, Any]]:
        """Return all records for *tenant_id* in insertion order."""
        return []


class SQLiteTransactionStore(TransactionStore):
    """SQLite-backed transaction lifecycle store with explicit columns."""

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
                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id             TEXT PRIMARY KEY,
                    tenant_id         TEXT NOT NULL DEFAULT '',
                    state             TEXT NOT NULL,
                    consensus_reached INTEGER NOT NULL DEFAULT 0,
                    node_count        INTEGER NOT NULL DEFAULT 0,
                    fault_count       INTEGER NOT NULL DEFAULT 0,
                    duration_ms       INTEGER NOT NULL DEFAULT 0,
                    billed_amount     REAL    NOT NULL DEFAULT 0.0,
                    created_at        TEXT    NOT NULL,
                    finalized_at      TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tx_tenant"
                " ON transactions (tenant_id)"
            )

    def save(self, record_dict: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO transactions
                    (tx_id, tenant_id, state, consensus_reached, node_count,
                     fault_count, duration_ms, billed_amount, created_at, finalized_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tx_id) DO UPDATE SET
                    state             = excluded.state,
                    consensus_reached = excluded.consensus_reached,
                    node_count        = excluded.node_count,
                    fault_count       = excluded.fault_count,
                    duration_ms       = excluded.duration_ms,
                    billed_amount     = excluded.billed_amount,
                    finalized_at      = excluded.finalized_at
                """,
                (
                    record_dict["tx_id"],
                    record_dict.get("tenant_id", ""),
                    record_dict["state"],
                    1 if record_dict.get("consensus_reached") else 0,
                    record_dict.get("node_count", 0),
                    record_dict.get("fault_count", 0),
                    record_dict.get("duration_ms", 0),
                    record_dict.get("billed_amount", 0.0),
                    record_dict.get("created_at", ""),
                    record_dict.get("finalized_at"),
                ),
            )

    def load(self, tx_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tx_id, tenant_id, state, consensus_reached, node_count,"
                " fault_count, duration_ms, billed_amount, created_at, finalized_at"
                " FROM transactions WHERE tx_id = ?",
                (tx_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "tx_id": row[0],
            "tenant_id": row[1],
            "state": row[2],
            "consensus_reached": bool(row[3]),
            "node_count": row[4],
            "fault_count": row[5],
            "duration_ms": row[6],
            "billed_amount": row[7],
            "created_at": row[8],
            "finalized_at": row[9],
        }

    def load_all(self, tenant_id: str = "") -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tx_id, tenant_id, state, consensus_reached, node_count,"
                " fault_count, duration_ms, billed_amount, created_at, finalized_at"
                " FROM transactions WHERE tenant_id = ? ORDER BY rowid",
                (tenant_id,),
            ).fetchall()
        return [
            {
                "tx_id": r[0], "tenant_id": r[1], "state": r[2],
                "consensus_reached": bool(r[3]), "node_count": r[4],
                "fault_count": r[5], "duration_ms": r[6], "billed_amount": r[7],
                "created_at": r[8], "finalized_at": r[9],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# BillingLedger — immutable, append-only financial truth
# ---------------------------------------------------------------------------

class BillingLedger:
    """
    Persistence contract for the immutable billing ledger.

    This is SEPARATE from BillingStore (usage counters).  Records here are
    NEVER updated or deleted — they are the financial source of truth for
    dispute resolution.
    """

    def append_entry(
        self,
        tx_id: str,
        tenant_id: str,
        period: str,
        node_round_count: int,
        billed_amount: float,
        consensus_reached: bool,
    ) -> None:
        """Append an immutable billing entry (never updates existing rows)."""

    def load_entries(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Return all ledger entries for *tenant_id* in insertion order."""
        return []


class SQLiteBillingLedger(BillingLedger):
    """
    SQLite-backed append-only billing ledger.

    Schema uses an autoincrement primary key so rows are strictly ordered by
    insertion time.  No UPDATE or DELETE operations are ever issued.
    """

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
                CREATE TABLE IF NOT EXISTS billing_ledger (
                    ledger_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    tx_id            TEXT    NOT NULL,
                    tenant_id        TEXT    NOT NULL,
                    period           TEXT    NOT NULL,
                    node_round_count INTEGER NOT NULL DEFAULT 0,
                    billed_amount    REAL    NOT NULL DEFAULT 0.0,
                    consensus_reached INTEGER NOT NULL DEFAULT 0,
                    recorded_at      TEXT    NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_tenant"
                " ON billing_ledger (tenant_id)"
            )

    def append_entry(
        self,
        tx_id: str,
        tenant_id: str,
        period: str,
        node_round_count: int,
        billed_amount: float,
        consensus_reached: bool,
    ) -> None:
        import datetime as _dt
        recorded_at = (
            _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO billing_ledger
                    (tx_id, tenant_id, period, node_round_count,
                     billed_amount, consensus_reached, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id, tenant_id, period, node_round_count,
                    billed_amount, 1 if consensus_reached else 0, recorded_at,
                ),
            )

    def load_entries(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ledger_id, tx_id, tenant_id, period, node_round_count,"
                " billed_amount, consensus_reached, recorded_at"
                " FROM billing_ledger WHERE tenant_id = ? ORDER BY ledger_id",
                (tenant_id,),
            ).fetchall()
        return [
            {
                "ledger_id": r[0], "tx_id": r[1], "tenant_id": r[2],
                "period": r[3], "node_round_count": r[4],
                "billed_amount": r[5], "consensus_reached": bool(r[6]),
                "recorded_at": r[7],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# SchemaVersion — upgrade-safe migration table
# ---------------------------------------------------------------------------

class SQLiteSchemaVersion:
    """
    Tracks schema migrations in a SQLite database.

    On startup, ``ensure_migrations()`` applies any pending migrations in
    sequence.  Each migration is a callable that receives a
    ``sqlite3.Connection``.
    """

    CURRENT_VERSION = 1

    def __init__(self, db_path: str, mem_conn: Optional[sqlite3.Connection] = None) -> None:
        self._db_path = db_path
        self._mem_conn = mem_conn

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _bootstrap(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    id      INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 0)"
            )

    def get_version(self) -> int:
        self._bootstrap()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
        return row[0] if row else 0

    def set_version(self, version: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schema_version SET version = ? WHERE id = 1", (version,)
            )

    def ensure_migrations(self) -> None:
        """Apply all pending migrations in version order."""
        current = self.get_version()
        for version, migration_fn in sorted(_MIGRATIONS.items()):
            if current < version:
                logger.info("Applying schema migration v%d", version)
                with self._connect() as conn:
                    migration_fn(conn)
                self.set_version(version)
                current = version


def _migration_v1(conn: sqlite3.Connection) -> None:
    """v1: baseline schema (tables created by individual stores already)."""
    # This migration is a no-op placeholder — the individual store _init_db()
    # methods handle table creation.  Future migrations add ALTER TABLE etc.
    pass


_MIGRATIONS: Dict[int, Any] = {
    1: _migration_v1,
}


# ---------------------------------------------------------------------------
# Factory: open stores from environment
# ---------------------------------------------------------------------------

def open_from_env() -> Dict[str, Optional[Any]]:
    """
    Return store instances driven by ``ECN_DB_PATH``.

    Returns a dict::

        {
            "audit_store":      SQLiteAuditStore      | None,
            "key_store":        SQLiteKeyStore         | None,
            "billing_store":    SQLiteBillingStore     | None,
            "tx_store":         SQLiteTransactionStore | None,
            "billing_ledger":   SQLiteBillingLedger    | None,
        }

    When ``ECN_DB_PATH`` is not set all values are ``None`` and the calling
    modules fall back to their existing in-memory behaviour.
    """
    db_path = os.environ.get("ECN_DB_PATH", "").strip()
    if not db_path:
        return {
            "audit_store": None,
            "key_store": None,
            "billing_store": None,
            "tx_store": None,
            "billing_ledger": None,
        }

    logger.info("ECN persistence enabled: db_path=%s", db_path)
    # Run schema migrations before opening stores
    mem_conn = None
    if db_path == ":memory:":
        import sqlite3 as _sq
        mem_conn = _sq.connect(":memory:", check_same_thread=False)
    SQLiteSchemaVersion(db_path, mem_conn=mem_conn).ensure_migrations()
    return {
        "audit_store": SQLiteAuditStore(db_path),
        "key_store": SQLiteKeyStore(db_path),
        "billing_store": SQLiteBillingStore(db_path),
        "tx_store": SQLiteTransactionStore(db_path),
        "billing_ledger": SQLiteBillingLedger(db_path),
    }
