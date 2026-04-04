"""
test_billing_ledger.py
----------------------
Tests for the billing ledger separation (Priority 3) and RMAE pricing (Priority 4).
"""
import os
import pytest
from ecn.billing import (
    BillingTracker,
    compute_billed_amount,
    get_pricing_tier,
    get_tier_config,
    _PRICING_TIERS,
)
from ecn.persistence import SQLiteBillingLedger


class TestRMAEPricing:
    def test_all_tiers_defined(self):
        for tier in ("basic", "standard", "critical"):
            assert tier in _PRICING_TIERS
            cfg = _PRICING_TIERS[tier]
            assert "sla_threshold" in cfg
            assert "quorum_threshold" in cfg
            assert "price_per_rmae" in cfg

    def test_tier_prices_ordered(self):
        assert _PRICING_TIERS["basic"]["price_per_rmae"] < _PRICING_TIERS["standard"]["price_per_rmae"]
        assert _PRICING_TIERS["standard"]["price_per_rmae"] < _PRICING_TIERS["critical"]["price_per_rmae"]

    def test_sla_thresholds_ordered(self):
        assert _PRICING_TIERS["basic"]["sla_threshold"] < _PRICING_TIERS["standard"]["sla_threshold"]
        assert _PRICING_TIERS["standard"]["sla_threshold"] < _PRICING_TIERS["critical"]["sla_threshold"]

    def test_compute_billed_amount_basic(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "basic")
        amount = compute_billed_amount(node_count=5)
        assert amount == pytest.approx(5 * 0.001)

    def test_compute_billed_amount_standard(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "standard")
        amount = compute_billed_amount(node_count=3)
        assert amount == pytest.approx(3 * 0.005)

    def test_compute_billed_amount_critical(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "critical")
        amount = compute_billed_amount(node_count=7)
        assert amount == pytest.approx(7 * 0.020)

    def test_get_pricing_tier_default(self, monkeypatch):
        monkeypatch.delenv("ECN_PRICING_TIER", raising=False)
        tier = get_pricing_tier()
        assert tier == "standard"

    def test_get_pricing_tier_from_env(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "basic")
        assert get_pricing_tier() == "basic"

    def test_invalid_tier_falls_back_to_standard(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "ultra")
        assert get_pricing_tier() == "standard"


class TestBillingTrackerWithLedger:
    def test_record_round_returns_billed_amount(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "standard")
        tracker = BillingTracker()
        amount = tracker.record_round(
            tenant_id="acme",
            tx_id="tx-1",
            node_count=3,
            consensus_reached=True,
        )
        assert amount == pytest.approx(3 * 0.005)

    def test_billing_ledger_appended(self):
        tracker = BillingTracker()
        tracker.record_round(
            tenant_id="acme",
            tx_id="tx-1",
            node_count=3,
            consensus_reached=True,
        )
        entries = tracker.get_billing_ledger("acme")
        assert len(entries) == 1
        assert entries[0]["tx_id"] == "tx-1"
        assert entries[0]["tenant_id"] == "acme"
        assert entries[0]["node_round_count"] == 3

    def test_billing_ledger_is_append_only(self):
        tracker = BillingTracker()
        for i in range(3):
            tracker.record_round(
                tenant_id="acme",
                tx_id=f"tx-{i}",
                node_count=2,
                consensus_reached=True,
            )
        entries = tracker.get_billing_ledger("acme")
        assert len(entries) == 3

    def test_billing_ledger_tenant_scoped(self):
        tracker = BillingTracker()
        tracker.record_round(tenant_id="acme", tx_id="tx-1", node_count=2, consensus_reached=True)
        tracker.record_round(tenant_id="beta", tx_id="tx-2", node_count=2, consensus_reached=True)
        acme_entries = tracker.get_billing_ledger("acme")
        beta_entries = tracker.get_billing_ledger("beta")
        assert len(acme_entries) == 1
        assert len(beta_entries) == 1
        assert acme_entries[0]["tx_id"] == "tx-1"
        assert beta_entries[0]["tx_id"] == "tx-2"

    def test_usage_counter_also_incremented(self):
        tracker = BillingTracker()
        tracker.record_round(tenant_id="acme", tx_id="tx-1", node_count=2, consensus_reached=True)
        usage = tracker.get_usage("acme")
        assert usage["round_count"] == 1

    def test_ledger_entry_has_consensus_flag(self):
        tracker = BillingTracker()
        tracker.record_round(tenant_id="acme", tx_id="tx-1", node_count=2, consensus_reached=False)
        entries = tracker.get_billing_ledger("acme")
        assert entries[0]["consensus_reached"] is False

    def test_ledger_entry_has_recorded_at(self):
        tracker = BillingTracker()
        tracker.record_round(tenant_id="acme", tx_id="tx-1", node_count=2, consensus_reached=True)
        entries = tracker.get_billing_ledger("acme")
        assert entries[0]["recorded_at"].endswith("Z")


class TestSLA:
    def test_sla_met_on_zero_rounds(self):
        tracker = BillingTracker()
        sla = tracker.get_sla("acme")
        assert sla["sla_met"] is True
        assert sla["total_rounds"] == 0

    def test_sla_met_when_all_consensus(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "standard")
        tracker = BillingTracker()
        for _ in range(10):
            tracker.record_round(tenant_id="acme", tx_id="t", node_count=3, consensus_reached=True)
        sla = tracker.get_sla("acme")
        assert sla["sla_met"] is True
        assert sla["fault_rate"] == 0.0

    def test_sla_not_met_when_too_many_faults(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "standard")
        tracker = BillingTracker()
        # 99% SLA means need 99% consensus rate — 10 failures out of 100 = fail
        for _ in range(90):
            tracker.record_round(tenant_id="acme", tx_id="t", node_count=3, consensus_reached=True)
        for _ in range(10):
            tracker.record_round(tenant_id="acme", tx_id="t", node_count=3, consensus_reached=False)
        sla = tracker.get_sla("acme")
        assert sla["sla_met"] is False

    def test_sla_response_has_required_fields(self, monkeypatch):
        monkeypatch.setenv("ECN_PRICING_TIER", "standard")
        tracker = BillingTracker()
        sla = tracker.get_sla("acme")
        for field in [
            "tenant_id", "tier", "sla_threshold", "quorum_threshold",
            "price_per_rmae", "description", "total_rounds", "fault_rounds",
            "fault_rate", "consensus_rate", "sla_met", "credits_earned",
        ]:
            assert field in sla

    def test_sla_tenant_scoped(self):
        tracker = BillingTracker()
        tracker.record_round(tenant_id="acme", tx_id="t", node_count=3, consensus_reached=True)
        tracker.record_round(tenant_id="beta", tx_id="t", node_count=3, consensus_reached=False)
        sla_acme = tracker.get_sla("acme")
        sla_beta = tracker.get_sla("beta")
        assert sla_acme["total_rounds"] == 1
        assert sla_beta["total_rounds"] == 1
        assert sla_acme["fault_rounds"] == 0
        assert sla_beta["fault_rounds"] == 1


class TestSQLiteBillingLedger:
    def test_append_and_load(self, tmp_path):
        db = str(tmp_path / "test.db")
        ledger = SQLiteBillingLedger(db)
        ledger.append_entry(
            tx_id="tx-1",
            tenant_id="acme",
            period="2026-04",
            node_round_count=5,
            billed_amount=0.025,
            consensus_reached=True,
        )
        entries = ledger.load_entries("acme")
        assert len(entries) == 1
        assert entries[0]["tx_id"] == "tx-1"
        assert entries[0]["billed_amount"] == pytest.approx(0.025)
        assert entries[0]["node_round_count"] == 5

    def test_append_only_no_overwrite(self, tmp_path):
        db = str(tmp_path / "test.db")
        ledger = SQLiteBillingLedger(db)
        for i in range(5):
            ledger.append_entry(
                tx_id=f"tx-{i}",
                tenant_id="acme",
                period="2026-04",
                node_round_count=3,
                billed_amount=0.015,
                consensus_reached=True,
            )
        entries = ledger.load_entries("acme")
        assert len(entries) == 5

    def test_ledger_ordered_by_insertion(self, tmp_path):
        db = str(tmp_path / "test.db")
        ledger = SQLiteBillingLedger(db)
        for i in range(3):
            ledger.append_entry(
                tx_id=f"tx-{i}",
                tenant_id="acme",
                period="2026-04",
                node_round_count=1,
                billed_amount=0.001,
                consensus_reached=True,
            )
        entries = ledger.load_entries("acme")
        tx_ids = [e["tx_id"] for e in entries]
        assert tx_ids == ["tx-0", "tx-1", "tx-2"]

    def test_ledger_tenant_isolation(self, tmp_path):
        db = str(tmp_path / "test.db")
        ledger = SQLiteBillingLedger(db)
        ledger.append_entry("tx-1", "acme", "2026-04", 3, 0.015, True)
        ledger.append_entry("tx-2", "beta", "2026-04", 3, 0.015, True)
        assert len(ledger.load_entries("acme")) == 1
        assert len(ledger.load_entries("beta")) == 1

    def test_ledger_persists_across_instances(self, tmp_path):
        db = str(tmp_path / "test.db")
        ledger1 = SQLiteBillingLedger(db)
        ledger1.append_entry("tx-1", "acme", "2026-04", 3, 0.015, True)
        ledger2 = SQLiteBillingLedger(db)
        entries = ledger2.load_entries("acme")
        assert len(entries) == 1
