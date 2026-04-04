"""
tests/test_persistence.py
-------------------------
Tests for ecn.persistence — SQLite audit store, key store, billing store,
and AuditLog / ApiKeyRegistry integration with a persistent backend.
"""

import os
import tempfile

import pytest

from ecn.audit import AuditLog, AuditEvent, NodeVote
from ecn.auth import ApiKeyRegistry, ROLE_ADMIN, ROLE_SUBMITTER, ROLE_AUDITOR
from ecn.persistence import (
    AuditStore,
    KeyStore,
    BillingStore,
    SQLiteAuditStore,
    SQLiteKeyStore,
    SQLiteBillingStore,
    open_from_env,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dummy_event(round_id: int = 1) -> AuditEvent:
    return AuditEvent(
        round_id=round_id,
        timestamp="2026-04-01T00:00:00Z",
        transaction={"type": "ship", "product_id": "X-001"},
        votes=[
            NodeVote(node_id="N1", state_hash="abc", signature="sig1", status="honest"),
        ],
        agreed_hash="abc",
        consensus_reached=True,
        honest_nodes=["N1"],
        faulty_nodes=[],
        invalid_sig_nodes=[],
        vote_counts={"abc": 1},
    )


# ---------------------------------------------------------------------------
# Abstract base class defaults
# ---------------------------------------------------------------------------

class TestAbstractStores:
    def test_audit_store_base_append_noop(self):
        store = AuditStore()
        store.append({"round_id": 1}, tenant_id="")  # should not raise

    def test_audit_store_base_load_empty(self):
        store = AuditStore()
        assert store.load_all() == []

    def test_key_store_base_noop(self):
        store = KeyStore()
        store.save_key("k1", {"key_id": "k1", "key": "s", "role": ROLE_ADMIN, "description": ""})
        store.delete_key("k1")
        assert store.load_all() == []

    def test_billing_store_base_noop(self):
        store = BillingStore()
        store.increment("t1", "2026-04")
        assert store.get_count("t1", "2026-04") == 0
        assert store.get_all_periods("t1") == []


# ---------------------------------------------------------------------------
# SQLiteAuditStore
# ---------------------------------------------------------------------------

class TestSQLiteAuditStore:
    @pytest.fixture
    def store(self, tmp_path):
        return SQLiteAuditStore(str(tmp_path / "ecn.db"))

    def test_append_and_load(self, store):
        event = _make_dummy_event(round_id=1)
        store.append(event.to_dict(), tenant_id="")
        rows = store.load_all(tenant_id="")
        assert len(rows) == 1
        assert rows[0]["round_id"] == 1
        assert rows[0]["transaction"]["type"] == "ship"

    def test_tenant_scope_isolation(self, store):
        event_a = _make_dummy_event(round_id=1)
        event_b = _make_dummy_event(round_id=2)
        store.append(event_a.to_dict(), tenant_id="tenant-a")
        store.append(event_b.to_dict(), tenant_id="tenant-b")
        rows_a = store.load_all(tenant_id="tenant-a")
        rows_b = store.load_all(tenant_id="tenant-b")
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0]["round_id"] == 1
        assert rows_b[0]["round_id"] == 2

    def test_multiple_events_ordered(self, store):
        for i in range(1, 6):
            store.append(_make_dummy_event(round_id=i).to_dict(), tenant_id="")
        rows = store.load_all(tenant_id="")
        assert [r["round_id"] for r in rows] == [1, 2, 3, 4, 5]

    def test_empty_tenant_returns_empty(self, store):
        store.append(_make_dummy_event().to_dict(), tenant_id="tenant-a")
        assert store.load_all(tenant_id="tenant-b") == []

    def test_reopen_persists(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        s1 = SQLiteAuditStore(db_path)
        s1.append(_make_dummy_event(1).to_dict(), tenant_id="")
        # Reopen from same path
        s2 = SQLiteAuditStore(db_path)
        rows = s2.load_all(tenant_id="")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# SQLiteKeyStore
# ---------------------------------------------------------------------------

class TestSQLiteKeyStore:
    @pytest.fixture
    def store(self, tmp_path):
        return SQLiteKeyStore(str(tmp_path / "ecn.db"))

    def test_save_and_load(self, store):
        store.save_key("k1", {"key_id": "k1", "key": "secret", "role": ROLE_ADMIN, "description": "test"})
        rows = store.load_all()
        assert len(rows) == 1
        assert rows[0]["key_id"] == "k1"
        assert rows[0]["key"] == "secret"

    def test_delete_key(self, store):
        store.save_key("k1", {"key_id": "k1", "key": "s", "role": ROLE_ADMIN, "description": ""})
        store.delete_key("k1")
        assert store.load_all() == []

    def test_delete_nonexistent_noop(self, store):
        store.delete_key("no-such-key")  # should not raise

    def test_upsert_replaces(self, store):
        store.save_key("k1", {"key_id": "k1", "key": "old", "role": ROLE_ADMIN, "description": ""})
        store.save_key("k1", {"key_id": "k1", "key": "new", "role": ROLE_SUBMITTER, "description": ""})
        rows = store.load_all()
        assert len(rows) == 1
        assert rows[0]["key"] == "new"
        assert rows[0]["role"] == ROLE_SUBMITTER

    def test_reopen_persists(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        s1 = SQLiteKeyStore(db_path)
        s1.save_key("k1", {"key_id": "k1", "key": "s", "role": ROLE_ADMIN, "description": ""})
        s2 = SQLiteKeyStore(db_path)
        assert len(s2.load_all()) == 1


# ---------------------------------------------------------------------------
# SQLiteBillingStore
# ---------------------------------------------------------------------------

class TestSQLiteBillingStore:
    @pytest.fixture
    def store(self, tmp_path):
        return SQLiteBillingStore(str(tmp_path / "ecn.db"))

    def test_increment_and_get(self, store):
        store.increment("tenant-a", "2026-04")
        assert store.get_count("tenant-a", "2026-04") == 1

    def test_multiple_increments(self, store):
        for _ in range(5):
            store.increment("t1", "2026-04")
        assert store.get_count("t1", "2026-04") == 5

    def test_tenant_isolation(self, store):
        store.increment("t1", "2026-04")
        store.increment("t2", "2026-04")
        assert store.get_count("t1", "2026-04") == 1
        assert store.get_count("t2", "2026-04") == 1

    def test_period_isolation(self, store):
        store.increment("t1", "2026-03")
        store.increment("t1", "2026-04")
        assert store.get_count("t1", "2026-03") == 1
        assert store.get_count("t1", "2026-04") == 1

    def test_get_all_periods(self, store):
        store.increment("t1", "2026-03")
        store.increment("t1", "2026-03")
        store.increment("t1", "2026-04")
        periods = store.get_all_periods("t1")
        assert len(periods) == 2
        assert periods[0]["period"] == "2026-03"
        assert periods[0]["round_count"] == 2
        assert periods[1]["period"] == "2026-04"
        assert periods[1]["round_count"] == 1

    def test_get_count_missing_returns_zero(self, store):
        assert store.get_count("nobody", "2026-04") == 0

    def test_reopen_persists(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        s1 = SQLiteBillingStore(db_path)
        s1.increment("t1", "2026-04")
        s2 = SQLiteBillingStore(db_path)
        assert s2.get_count("t1", "2026-04") == 1


# ---------------------------------------------------------------------------
# AuditLog + persistent store integration
# ---------------------------------------------------------------------------

class TestAuditLogPersistence:
    def test_in_memory_by_default(self):
        log = AuditLog()
        assert log._store is None

    def test_events_round_trip_to_sqlite(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        store = SQLiteAuditStore(db_path)
        log = AuditLog(store=store)
        # Record an event via a fake round
        from ecn.node import NodeResult
        from ecn.consensus import ConsensusResult
        from ecn.consensus import run_consensus
        results = [NodeResult(
            node_id="N1", state_hash="aaa", result_state={}, trace_entry={}, signature="sig"
        )]
        cr = run_consensus(results)
        tx = {"type": "ship", "product_id": "P-001"}
        log.record(tx, results, cr)
        assert len(log) == 1

        # Reopen — events should be reloaded
        log2 = AuditLog(store=SQLiteAuditStore(db_path))
        assert len(log2) == 1
        assert log2.get_event(1).transaction["type"] == "ship"

    def test_tenant_scoped_audit_isolation(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        from ecn.node import NodeResult
        from ecn.consensus import run_consensus

        results = [NodeResult("N1", "hash1", {}, {}, "sig")]
        cr = run_consensus(results)

        log_a = AuditLog(store=SQLiteAuditStore(db_path), tenant_id="tenant-a")
        log_b = AuditLog(store=SQLiteAuditStore(db_path), tenant_id="tenant-b")

        log_a.record({"type": "ship"}, results, cr)
        assert len(log_a) == 1
        assert len(log_b) == 0  # tenant-b should not see tenant-a's events


# ---------------------------------------------------------------------------
# ApiKeyRegistry + persistent store integration
# ---------------------------------------------------------------------------

class TestApiKeyRegistryPersistence:
    def test_in_memory_by_default(self):
        reg = ApiKeyRegistry()
        assert reg._store is None

    def test_keys_round_trip_to_sqlite(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        store = SQLiteKeyStore(db_path)
        reg = ApiKeyRegistry(store=store)
        key = reg.issue(role=ROLE_SUBMITTER, description="test key")

        # Reopen
        reg2 = ApiKeyRegistry(store=SQLiteKeyStore(db_path))
        found = reg2.lookup(key.key)
        assert found is not None
        assert found.role == ROLE_SUBMITTER
        assert found.key_id == key.key_id

    def test_revoked_key_not_present_after_reopen(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        store = SQLiteKeyStore(db_path)
        reg = ApiKeyRegistry(store=store)
        key = reg.issue(role=ROLE_AUDITOR)
        reg.revoke(key.key_id)

        reg2 = ApiKeyRegistry(store=SQLiteKeyStore(db_path))
        assert reg2.lookup(key.key) is None

    def test_len_counts_loaded_keys(self, tmp_path):
        db_path = str(tmp_path / "ecn.db")
        store = SQLiteKeyStore(db_path)
        reg = ApiKeyRegistry(store=store)
        reg.issue(role=ROLE_ADMIN)
        reg.issue(role=ROLE_SUBMITTER)

        reg2 = ApiKeyRegistry(store=SQLiteKeyStore(db_path))
        assert len(reg2) == 2


# ---------------------------------------------------------------------------
# open_from_env factory
# ---------------------------------------------------------------------------

class TestOpenFromEnv:
    def test_no_env_var_returns_none_stores(self, monkeypatch):
        monkeypatch.delenv("ECN_DB_PATH", raising=False)
        stores = open_from_env()
        assert stores["audit_store"] is None
        assert stores["key_store"] is None
        assert stores["billing_store"] is None

    def test_with_db_path_returns_sqlite_stores(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "ecn.db")
        monkeypatch.setenv("ECN_DB_PATH", db_path)
        stores = open_from_env()
        assert isinstance(stores["audit_store"], SQLiteAuditStore)
        assert isinstance(stores["key_store"], SQLiteKeyStore)
        assert isinstance(stores["billing_store"], SQLiteBillingStore)

    def test_memory_db_works(self, monkeypatch):
        monkeypatch.setenv("ECN_DB_PATH", ":memory:")
        stores = open_from_env()
        # Stores should be usable (in-memory SQLite)
        stores["audit_store"].append({"round_id": 1, "votes": []}, tenant_id="")
        rows = stores["audit_store"].load_all(tenant_id="")
        assert len(rows) == 1
