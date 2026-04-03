"""
tests/test_billing.py
---------------------
Tests for ecn.billing — BillingTracker, usage metering, Stripe webhook handling,
and the API endpoint GET /tenants/{id}/usage.
"""

import pytest
from fastapi.testclient import TestClient

from ecn.billing import (
    BillingTracker,
    StripeWebhookPayload,
    _current_period,
    _InMemoryCounts,
    handle_stripe_event,
    init_tracker,
)


# ---------------------------------------------------------------------------
# _current_period
# ---------------------------------------------------------------------------

class TestCurrentPeriod:
    def test_monthly_format(self, monkeypatch):
        monkeypatch.delenv("ECN_BILLING_PERIOD", raising=False)
        period = _current_period()
        import re
        assert re.match(r"^\d{4}-\d{2}$", period), f"Expected YYYY-MM, got {period!r}"

    def test_daily_format(self, monkeypatch):
        monkeypatch.setenv("ECN_BILLING_PERIOD", "daily")
        period = _current_period()
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", period), f"Expected YYYY-MM-DD, got {period!r}"

    def test_yearly_format(self, monkeypatch):
        monkeypatch.setenv("ECN_BILLING_PERIOD", "yearly")
        period = _current_period()
        import re
        assert re.match(r"^\d{4}$", period), f"Expected YYYY, got {period!r}"


# ---------------------------------------------------------------------------
# _InMemoryCounts
# ---------------------------------------------------------------------------

class TestInMemoryCounts:
    def test_increment_and_get(self):
        counts = _InMemoryCounts()
        counts.increment("t1", "2026-04")
        assert counts.get_count("t1", "2026-04") == 1

    def test_multiple_increments(self):
        counts = _InMemoryCounts()
        for _ in range(10):
            counts.increment("t1", "2026-04")
        assert counts.get_count("t1", "2026-04") == 10

    def test_zero_for_unknown(self):
        counts = _InMemoryCounts()
        assert counts.get_count("nobody", "2026-04") == 0

    def test_tenant_isolation(self):
        counts = _InMemoryCounts()
        counts.increment("t1", "2026-04")
        counts.increment("t2", "2026-04")
        assert counts.get_count("t1", "2026-04") == 1
        assert counts.get_count("t2", "2026-04") == 1

    def test_period_isolation(self):
        counts = _InMemoryCounts()
        counts.increment("t1", "2026-03")
        counts.increment("t1", "2026-04")
        assert counts.get_count("t1", "2026-03") == 1
        assert counts.get_count("t1", "2026-04") == 1

    def test_get_all_periods_sorted(self):
        counts = _InMemoryCounts()
        counts.increment("t1", "2026-04")
        counts.increment("t1", "2026-04")
        counts.increment("t1", "2026-03")
        periods = counts.get_all_periods("t1")
        assert [p["period"] for p in periods] == ["2026-03", "2026-04"]

    def test_get_all_periods_filters_tenant(self):
        counts = _InMemoryCounts()
        counts.increment("t1", "2026-04")
        counts.increment("t2", "2026-04")
        periods = counts.get_all_periods("t1")
        assert len(periods) == 1
        assert periods[0]["period"] == "2026-04"


# ---------------------------------------------------------------------------
# BillingTracker (in-memory)
# ---------------------------------------------------------------------------

class TestBillingTrackerInMemory:
    def test_record_round_increments(self):
        tracker = BillingTracker()
        tracker.record_round("acme")
        usage = tracker.get_usage("acme")
        assert usage["round_count"] == 1

    def test_multiple_rounds(self):
        tracker = BillingTracker()
        for _ in range(5):
            tracker.record_round("acme")
        assert tracker.get_usage("acme")["round_count"] == 5

    def test_tenant_isolation(self):
        tracker = BillingTracker()
        tracker.record_round("acme")
        tracker.record_round("beta")
        assert tracker.get_usage("acme")["round_count"] == 1
        assert tracker.get_usage("beta")["round_count"] == 1

    def test_zero_usage_for_unknown_tenant(self):
        tracker = BillingTracker()
        usage = tracker.get_usage("ghost")
        assert usage["round_count"] == 0
        assert usage["tenant_id"] == "ghost"

    def test_usage_response_structure(self):
        tracker = BillingTracker()
        tracker.record_round("acme")
        usage = tracker.get_usage("acme")
        assert "tenant_id" in usage
        assert "current_period" in usage
        assert "round_count" in usage
        assert "all_periods" in usage

    def test_all_periods_includes_history(self):
        tracker = BillingTracker()
        tracker.record_round("acme")
        usage = tracker.get_usage("acme")
        assert len(usage["all_periods"]) == 1
        assert usage["all_periods"][0]["round_count"] == 1

    def test_init_tracker_creates_singleton(self):
        from ecn.billing import get_tracker
        tracker = init_tracker()
        assert get_tracker() is tracker

    def test_get_tracker_raises_before_init(self, monkeypatch):
        import ecn.billing as billing_mod
        old_tracker = billing_mod._tracker
        try:
            billing_mod._tracker = None
            from ecn.billing import get_tracker
            with pytest.raises(RuntimeError, match="not initialised"):
                get_tracker()
        finally:
            billing_mod._tracker = old_tracker


# ---------------------------------------------------------------------------
# BillingTracker (SQLite-backed)
# ---------------------------------------------------------------------------

class TestBillingTrackerSQLite:
    def test_record_and_retrieve(self, tmp_path):
        from ecn.persistence import SQLiteBillingStore
        store = SQLiteBillingStore(str(tmp_path / "ecn.db"))
        tracker = BillingTracker(store=store)
        tracker.record_round("acme")
        usage = tracker.get_usage("acme")
        assert usage["round_count"] == 1

    def test_persists_across_restart(self, tmp_path):
        from ecn.persistence import SQLiteBillingStore
        db_path = str(tmp_path / "ecn.db")
        t1 = BillingTracker(store=SQLiteBillingStore(db_path))
        t1.record_round("acme")
        t1.record_round("acme")

        t2 = BillingTracker(store=SQLiteBillingStore(db_path))
        usage = t2.get_usage("acme")
        assert usage["round_count"] == 2


# ---------------------------------------------------------------------------
# Stripe webhook handler
# ---------------------------------------------------------------------------

class TestStripeWebhookHandler:
    def _payload(self, event_type: str, tenant_id: str = "acme") -> StripeWebhookPayload:
        return StripeWebhookPayload(
            id="evt_test_001",
            type=event_type,
            data={
                "object": {
                    "metadata": {"ecn_tenant_id": tenant_id}
                }
            },
        )

    def test_payment_succeeded(self):
        resp = handle_stripe_event(self._payload("invoice.payment_succeeded"))
        assert resp.status == "ok"
        assert resp.event_type == "invoice.payment_succeeded"
        assert "acme" in resp.message

    def test_payment_failed(self):
        resp = handle_stripe_event(self._payload("invoice.payment_failed"))
        assert resp.status == "ok"
        assert "acme" in resp.message

    def test_subscription_deleted(self):
        resp = handle_stripe_event(self._payload("customer.subscription.deleted"))
        assert resp.status == "ok"

    def test_unknown_event_acknowledged(self):
        resp = handle_stripe_event(self._payload("customer.updated"))
        assert resp.status == "ok"
        assert "acknowledged" in resp.message

    def test_missing_tenant_metadata(self):
        payload = StripeWebhookPayload(
            id="evt_002",
            type="invoice.payment_succeeded",
            data={"object": {}},
        )
        resp = handle_stripe_event(payload)
        assert resp.status == "ok"
        assert "unknown" in resp.message

    def test_event_id_preserved(self):
        payload = StripeWebhookPayload(id="evt_xyz", type="invoice.payment_succeeded", data={})
        resp = handle_stripe_event(payload)
        assert resp.event_id == "evt_xyz"


# ---------------------------------------------------------------------------
# API endpoint — GET /tenants/{id}/usage
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from ecn.api import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def tenant_id(client):
    resp = client.post("/tenants", json={"name": "BillingTestCo", "node_count": 2})
    assert resp.status_code == 201
    return resp.json()["tenant_id"]


class TestUsageAPI:
    def test_usage_returns_200(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/usage")
        assert resp.status_code == 200

    def test_usage_fields(self, client, tenant_id):
        data = client.get(f"/tenants/{tenant_id}/usage").json()
        assert data["tenant_id"] == tenant_id
        assert "current_period" in data
        assert "round_count" in data
        assert "all_periods" in data

    def test_usage_zero_before_transactions(self, client, tenant_id):
        data = client.get(f"/tenants/{tenant_id}/usage").json()
        assert isinstance(data["round_count"], int)
        assert data["round_count"] >= 0

    def test_usage_increments_after_transaction(self, client, tenant_id):
        before = client.get(f"/tenants/{tenant_id}/usage").json()["round_count"]
        client.post(
            f"/tenants/{tenant_id}/transactions",
            json={"type": "ship", "product_id": "ITEM-001", "destination": "HQ", "shipper": "UPS"},
        )
        after = client.get(f"/tenants/{tenant_id}/usage").json()["round_count"]
        assert after == before + 1

    def test_usage_unknown_tenant_404(self, client):
        resp = client.get("/tenants/no-such-tenant/usage")
        assert resp.status_code == 404

    def test_stripe_webhook_endpoint(self, client):
        resp = client.post(
            "/billing/stripe-webhook",
            json={
                "id": "evt_test",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"metadata": {"ecn_tenant_id": "acme"}}},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_stripe_webhook_unknown_event(self, client):
        resp = client.post(
            "/billing/stripe-webhook",
            json={"id": "evt_unk", "type": "some.future.event", "data": {}},
        )
        assert resp.status_code == 200
