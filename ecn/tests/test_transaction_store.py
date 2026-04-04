"""
test_transaction_store.py
-------------------------
Tests for SQLiteTransactionStore and SQLiteSchemaVersion (migration tracking).
"""
import pytest
from ecn.persistence import (
    SQLiteTransactionStore,
    SQLiteSchemaVersion,
    open_from_env,
)
from ecn.transaction import TxState, new_record


class TestSQLiteTransactionStore:
    def _make_record_dict(self, tx_id="tx-1", tenant_id="acme", state="INITIATED"):
        return {
            "tx_id": tx_id,
            "tenant_id": tenant_id,
            "state": state,
            "consensus_reached": True,
            "node_count": 5,
            "fault_count": 0,
            "duration_ms": 123,
            "billed_amount": 0.025,
            "created_at": "2026-04-04T00:00:00Z",
            "finalized_at": "2026-04-04T00:00:01Z",
        }

    def test_save_and_load(self, tmp_path):
        store = SQLiteTransactionStore(str(tmp_path / "test.db"))
        d = self._make_record_dict()
        store.save(d)
        result = store.load("tx-1")
        assert result is not None
        assert result["tx_id"] == "tx-1"
        assert result["state"] == "INITIATED"
        assert result["node_count"] == 5
        assert result["billed_amount"] == pytest.approx(0.025)

    def test_load_nonexistent_returns_none(self, tmp_path):
        store = SQLiteTransactionStore(str(tmp_path / "test.db"))
        assert store.load("nonexistent") is None

    def test_save_updates_existing_record(self, tmp_path):
        store = SQLiteTransactionStore(str(tmp_path / "test.db"))
        d = self._make_record_dict()
        store.save(d)
        d["state"] = "FINALIZED"
        d["duration_ms"] = 500
        store.save(d)
        result = store.load("tx-1")
        assert result["state"] == "FINALIZED"
        assert result["duration_ms"] == 500

    def test_load_all_by_tenant(self, tmp_path):
        store = SQLiteTransactionStore(str(tmp_path / "test.db"))
        store.save(self._make_record_dict("tx-1", "acme"))
        store.save(self._make_record_dict("tx-2", "acme"))
        store.save(self._make_record_dict("tx-3", "beta"))
        acme_records = store.load_all("acme")
        assert len(acme_records) == 2
        assert all(r["tenant_id"] == "acme" for r in acme_records)

    def test_consensus_reached_stored_as_bool(self, tmp_path):
        store = SQLiteTransactionStore(str(tmp_path / "test.db"))
        d = self._make_record_dict()
        d["consensus_reached"] = False
        store.save(d)
        result = store.load("tx-1")
        assert result["consensus_reached"] is False

    def test_finalized_at_nullable(self, tmp_path):
        store = SQLiteTransactionStore(str(tmp_path / "test.db"))
        d = self._make_record_dict()
        d["finalized_at"] = None
        store.save(d)
        result = store.load("tx-1")
        assert result["finalized_at"] is None

    def test_persists_across_instances(self, tmp_path):
        db = str(tmp_path / "test.db")
        store1 = SQLiteTransactionStore(db)
        store1.save(self._make_record_dict())
        store2 = SQLiteTransactionStore(db)
        result = store2.load("tx-1")
        assert result is not None

    def test_in_memory_mode(self):
        store = SQLiteTransactionStore(":memory:")
        store.save(self._make_record_dict())
        result = store.load("tx-1")
        assert result["tx_id"] == "tx-1"


class TestSQLiteSchemaVersion:
    def test_initial_version_is_zero(self, tmp_path):
        sv = SQLiteSchemaVersion(str(tmp_path / "test.db"))
        assert sv.get_version() == 0

    def test_set_and_get_version(self, tmp_path):
        sv = SQLiteSchemaVersion(str(tmp_path / "test.db"))
        sv._bootstrap()
        sv.set_version(5)
        assert sv.get_version() == 5

    def test_ensure_migrations_applies_all(self, tmp_path):
        sv = SQLiteSchemaVersion(str(tmp_path / "test.db"))
        sv.ensure_migrations()
        assert sv.get_version() == SQLiteSchemaVersion.CURRENT_VERSION

    def test_ensure_migrations_idempotent(self, tmp_path):
        db = str(tmp_path / "test.db")
        sv1 = SQLiteSchemaVersion(db)
        sv1.ensure_migrations()
        sv2 = SQLiteSchemaVersion(db)
        sv2.ensure_migrations()  # should not raise
        assert sv2.get_version() == SQLiteSchemaVersion.CURRENT_VERSION


class TestOpenFromEnvNewKeys:
    def test_returns_none_stores_when_no_env(self, monkeypatch):
        monkeypatch.delenv("ECN_DB_PATH", raising=False)
        stores = open_from_env()
        assert stores["tx_store"] is None
        assert stores["billing_ledger"] is None

    def test_returns_stores_when_db_path_set(self, tmp_path, monkeypatch):
        db = str(tmp_path / "test.db")
        monkeypatch.setenv("ECN_DB_PATH", db)
        stores = open_from_env()
        assert stores["tx_store"] is not None
        assert stores["billing_ledger"] is not None
