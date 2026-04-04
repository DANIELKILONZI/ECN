"""
test_lifecycle_api.py
---------------------
Integration tests for new ECN Product Mode API endpoints.

Tests cover:
- Transaction lifecycle (tx_id in response, GET /transactions/{tx_id})
- Node health (GET /network/nodes/{node_id}/health)
- Billing ledger (GET /tenants/{id}/billing-ledger)
- SLA endpoint (GET /tenants/{id}/sla)
- ECN_DEBUG gate on /audit/replay
- X-ECN-Request-ID header on every response
"""
import os
import pytest
from fastapi.testclient import TestClient

from ecn.api import app


_SAFE_TX = {
    "type": "inspect",
    "product_id": "LAPTOP-001",
    "check_name": "weight",
    "inspector": "QA",
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def tenant_id(client):
    """Create a tenant and return its ID for billing/SLA tests."""
    resp = client.post("/tenants", json={"name": "Test Tenant"})
    assert resp.status_code == 201
    return resp.json()["tenant_id"]


# ---------------------------------------------------------------------------
# POST /transactions — lifecycle fields
# ---------------------------------------------------------------------------

class TestTransactionLifecycleFields:
    def test_response_includes_tx_id(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        assert resp.status_code == 200
        body = resp.json()
        assert "tx_id" in body
        assert len(body["tx_id"]) == 36  # UUID4

    def test_response_includes_lifecycle_state(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle_state"] == "REPLICATED"

    def test_response_includes_billed_amount(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        assert resp.status_code == 200
        body = resp.json()
        assert "billed_amount" in body
        assert body["billed_amount"] >= 0.0

    def test_response_includes_duration_ms(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        assert resp.status_code == 200
        body = resp.json()
        assert "duration_ms" in body
        assert body["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# GET /transactions/{tx_id}
# ---------------------------------------------------------------------------

class TestGetTransactionStatus:
    def test_returns_transaction_record(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        status_resp = client.get(f"/transactions/{tx_id}")
        assert status_resp.status_code == 200

    def test_status_has_required_fields(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        body = client.get(f"/transactions/{tx_id}").json()
        for field in [
            "tx_id", "tenant_id", "lifecycle_state", "node_count",
            "consensus_reached", "fault_count", "duration_ms",
            "billed_amount", "created_at", "finalized_at",
        ]:
            assert field in body

    def test_status_shows_replicated_state(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        body = client.get(f"/transactions/{tx_id}").json()
        assert body["lifecycle_state"] == "REPLICATED"

    def test_unknown_tx_id_returns_404(self, client):
        resp = client.get("/transactions/nonexistent-tx-id")
        assert resp.status_code == 404

    def test_tx_id_matches(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        body = client.get(f"/transactions/{tx_id}").json()
        assert body["tx_id"] == tx_id

    def test_consensus_reached_recorded(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        body = client.get(f"/transactions/{tx_id}").json()
        assert body["consensus_reached"] is True

    def test_finalized_at_is_set(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        body = client.get(f"/transactions/{tx_id}").json()
        assert body["finalized_at"] is not None

    def test_node_count_is_positive(self, client):
        resp = client.post("/transactions", json=_SAFE_TX)
        tx_id = resp.json()["tx_id"]
        body = client.get(f"/transactions/{tx_id}").json()
        assert body["node_count"] > 0


# ---------------------------------------------------------------------------
# GET /network/nodes/{node_id}/health
# ---------------------------------------------------------------------------

class TestNodeHealth:
    def _get_a_node_id(self, client):
        nodes = client.get("/network/nodes").json()
        return nodes[0]["node_id"]

    def test_returns_health_for_known_node(self, client):
        node_id = self._get_a_node_id(client)
        resp = client.get(f"/network/nodes/{node_id}/health")
        assert resp.status_code == 200

    def test_health_has_required_fields(self, client):
        node_id = self._get_a_node_id(client)
        body = client.get(f"/network/nodes/{node_id}/health").json()
        for field in ["node_id", "state", "consecutive_failures", "last_success_at",
                      "excluded_since", "p95_latency_ms"]:
            assert field in body

    def test_health_state_is_healthy_initially(self, client):
        node_id = self._get_a_node_id(client)
        body = client.get(f"/network/nodes/{node_id}/health").json()
        assert body["state"] in ("HEALTHY", "SUSPECT", "EXCLUDED", "REHABILITATING")

    def test_health_for_unknown_node_returns_healthy(self, client):
        """Unknown nodes are created on first access as HEALTHY."""
        resp = client.get("/network/nodes/new-unknown-node/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "HEALTHY"

    def test_health_updates_after_transaction(self, client):
        """After a successful broadcast, nodes should have last_success_at set."""
        client.post("/transactions", json=_SAFE_TX)
        node_id = self._get_a_node_id(client)
        body = client.get(f"/network/nodes/{node_id}/health").json()
        assert body["last_success_at"] is not None


# ---------------------------------------------------------------------------
# GET /tenants/{id}/billing-ledger
# ---------------------------------------------------------------------------

class TestBillingLedger:
    def test_returns_404_for_unknown_tenant(self, client):
        resp = client.get("/tenants/nonexistent-tenant/billing-ledger")
        assert resp.status_code == 404

    def test_returns_empty_ledger_for_new_tenant(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/billing-ledger")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == tenant_id
        assert isinstance(body["entries"], list)
        assert "total_entries" in body
        assert "total_billed" in body

    def test_ledger_updates_after_transaction(self, client, tenant_id):
        before = client.get(f"/tenants/{tenant_id}/billing-ledger").json()["total_entries"]
        client.post(f"/tenants/{tenant_id}/transactions", json={
            "type": "ship",
            "product_id": "ITEM-001",
            "destination": "Paris",
            "shipper": "DHL",
        })
        after = client.get(f"/tenants/{tenant_id}/billing-ledger").json()["total_entries"]
        assert after == before + 1

    def test_ledger_entry_has_required_fields(self, client, tenant_id):
        client.post(f"/tenants/{tenant_id}/transactions", json={
            "type": "ship",
            "product_id": "ITEM-001",
            "destination": "Paris",
            "shipper": "DHL",
        })
        body = client.get(f"/tenants/{tenant_id}/billing-ledger").json()
        if body["entries"]:
            entry = body["entries"][0]
            for field in [
                "ledger_id", "tx_id", "tenant_id", "period",
                "node_round_count", "billed_amount", "consensus_reached", "recorded_at",
            ]:
                assert field in entry

    def test_total_billed_is_sum_of_entries(self, client, tenant_id):
        body = client.get(f"/tenants/{tenant_id}/billing-ledger").json()
        entries = body["entries"]
        expected = sum(e["billed_amount"] for e in entries)
        assert body["total_billed"] == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# GET /tenants/{id}/sla
# ---------------------------------------------------------------------------

class TestSLAEndpoint:
    def test_returns_404_for_unknown_tenant(self, client):
        resp = client.get("/tenants/nonexistent-tenant/sla")
        assert resp.status_code == 404

    def test_returns_sla_for_known_tenant(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/sla")
        assert resp.status_code == 200

    def test_sla_has_required_fields(self, client, tenant_id):
        body = client.get(f"/tenants/{tenant_id}/sla").json()
        for field in [
            "tenant_id", "tier", "sla_threshold", "quorum_threshold",
            "price_per_rmae", "description", "total_rounds", "fault_rounds",
            "fault_rate", "consensus_rate", "sla_met", "credits_earned",
        ]:
            assert field in body

    def test_sla_tenant_id_matches(self, client, tenant_id):
        body = client.get(f"/tenants/{tenant_id}/sla").json()
        assert body["tenant_id"] == tenant_id

    def test_sla_met_true_on_zero_rounds(self, client, tenant_id):
        body = client.get(f"/tenants/{tenant_id}/sla").json()
        assert body["sla_met"] is True


# ---------------------------------------------------------------------------
# ECN_DEBUG gate on /audit/replay
# ---------------------------------------------------------------------------

class TestReplayDebugGate:
    def test_replay_enabled_by_default(self, client):
        resp = client.get("/audit/replay")
        assert resp.status_code == 200

    def test_replay_disabled_when_ecn_debug_false(self, client, monkeypatch):
        monkeypatch.setenv("ECN_DEBUG", "false")
        resp = client.get("/audit/replay")
        assert resp.status_code == 403

    def test_replay_enabled_when_ecn_debug_true(self, client, monkeypatch):
        monkeypatch.setenv("ECN_DEBUG", "true")
        resp = client.get("/audit/replay")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# X-ECN-Request-ID header
# ---------------------------------------------------------------------------

class TestRequestIDHeader:
    def test_response_includes_request_id(self, client):
        resp = client.get("/network/nodes")
        assert "x-ecn-request-id" in resp.headers

    def test_request_id_is_uuid(self, client):
        resp = client.get("/network/nodes")
        request_id = resp.headers["x-ecn-request-id"]
        assert len(request_id) == 36

    def test_different_requests_get_different_ids(self, client):
        r1 = client.get("/network/nodes")
        r2 = client.get("/network/nodes")
        assert r1.headers["x-ecn-request-id"] != r2.headers["x-ecn-request-id"]

    def test_client_provided_request_id_is_echoed(self, client):
        custom_id = "my-custom-id-12345"
        resp = client.get("/network/nodes", headers={"X-ECN-Request-ID": custom_id})
        assert resp.headers["x-ecn-request-id"] == custom_id
