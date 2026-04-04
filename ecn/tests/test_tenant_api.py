"""
tests/test_tenant_api.py
------------------------
Integration tests for multi-tenant isolation in ECN.

Each tenant gets its own isolated P2PNetwork + AuditLog.
These tests verify:
  - Tenant lifecycle (create, list, delete)
  - Tenant-scoped transactions do not affect other tenants
  - Per-tenant audit trail
  - Per-tenant network state
  - Per-tenant webhooks
"""

import pytest
from fastapi.testclient import TestClient

from ecn.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tenant lifecycle
# ---------------------------------------------------------------------------

class TestTenantLifecycle:
    def test_create_tenant_returns_201(self, client):
        resp = client.post("/tenants", json={"name": "Bank-A", "node_count": 2})
        assert resp.status_code == 201

    def test_create_tenant_response_fields(self, client):
        resp = client.post("/tenants", json={"name": "Bank-B", "node_count": 2})
        data = resp.json()
        assert "tenant_id" in data
        assert data["name"] == "Bank-B"
        assert data["node_count"] == 2

    def test_list_tenants(self, client):
        client.post("/tenants", json={"name": "List-Test", "node_count": 2})
        resp = client.get("/tenants")
        assert resp.status_code == 200
        tenants = resp.json()
        assert isinstance(tenants, list)
        names = [t["name"] for t in tenants]
        assert "List-Test" in names

    def test_delete_tenant(self, client):
        resp = client.post("/tenants", json={"name": "ToDelete", "node_count": 2})
        tid = resp.json()["tenant_id"]
        del_resp = client.delete(f"/tenants/{tid}")
        assert del_resp.status_code == 204
        # Verify gone from list
        tenants = client.get("/tenants").json()
        ids = [t["tenant_id"] for t in tenants]
        assert tid not in ids

    def test_delete_nonexistent_tenant_returns_404(self, client):
        resp = client.delete("/tenants/no-such-tenant")
        assert resp.status_code == 404

    def test_create_tenant_with_custom_products(self, client):
        resp = client.post("/tenants", json={
            "name": "CustomProducts",
            "node_count": 2,
            "products": ["WIDGET-001", "GADGET-002"],
        })
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Tenant-scoped transactions
# ---------------------------------------------------------------------------

class TestTenantTransactions:
    @pytest.fixture(scope="class")
    def tenant_id(self, client):
        resp = client.post("/tenants", json={"name": "TxTest-Tenant", "node_count": 2})
        return resp.json()["tenant_id"]

    def test_submit_transaction(self, client, tenant_id):
        resp = client.post(f"/tenants/{tenant_id}/transactions", json={
            "type": "ship",
            "product_id": "ITEM-001",
            "destination": "port",
            "shipper": "DHL",
        })
        assert resp.status_code == 200

    def test_transaction_response_has_consensus_fields(self, client, tenant_id):
        resp = client.post(f"/tenants/{tenant_id}/transactions", json={
            "type": "inspect",
            "product_id": "ITEM-001",
            "check_name": "label",
            "inspector": "QA",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "consensus_reached" in data
        assert "agreed_hash" in data
        assert "honest_nodes" in data
        assert data["consensus_reached"] is True

    def test_unknown_tx_type_returns_422(self, client, tenant_id):
        resp = client.post(f"/tenants/{tenant_id}/transactions", json={"type": "explode"})
        assert resp.status_code == 422

    def test_transaction_on_nonexistent_tenant_returns_404(self, client):
        resp = client.post("/tenants/no-such-id/transactions", json={
            "type": "ship",
            "product_id": "ITEM-001",
            "destination": "port",
            "shipper": "DHL",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tenant audit trail
# ---------------------------------------------------------------------------

class TestTenantAudit:
    @pytest.fixture(scope="class")
    def tenant_id(self, client):
        resp = client.post("/tenants", json={"name": "AuditTest-Tenant", "node_count": 2})
        tid = resp.json()["tenant_id"]
        # Submit some transactions
        client.post(f"/tenants/{tid}/transactions", json={
            "type": "ship",
            "product_id": "ITEM-001",
            "destination": "port",
            "shipper": "DHL",
        })
        return tid

    def test_audit_events_returns_200(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/audit/events")
        assert resp.status_code == 200

    def test_audit_events_has_entries(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/audit/events")
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["events"]) >= 1

    def test_audit_events_structure(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/audit/events")
        event = resp.json()["events"][0]
        assert "round_id" in event
        assert "timestamp" in event
        assert "transaction" in event
        assert "consensus_reached" in event

    def test_audit_summary(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/audit/summary")
        assert resp.status_code == 200
        s = resp.json()
        assert "total_rounds" in s
        assert "fault_rate" in s
        assert s["total_rounds"] >= 1

    def test_audit_on_nonexistent_tenant_returns_404(self, client):
        resp = client.get("/tenants/no-such-id/audit/events")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tenant isolation (state does not cross between tenants)
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    def test_tenant_audit_trails_are_independent(self, client):
        resp_a = client.post("/tenants", json={"name": "IsolationA", "node_count": 2})
        resp_b = client.post("/tenants", json={"name": "IsolationB", "node_count": 2})
        tid_a = resp_a.json()["tenant_id"]
        tid_b = resp_b.json()["tenant_id"]

        # Submit to A only
        client.post(f"/tenants/{tid_a}/transactions", json={
            "type": "ship",
            "product_id": "ITEM-001",
            "destination": "port",
            "shipper": "DHL",
        })

        summary_a = client.get(f"/tenants/{tid_a}/audit/summary").json()
        summary_b = client.get(f"/tenants/{tid_b}/audit/summary").json()

        assert summary_a["total_rounds"] >= 1
        assert summary_b["total_rounds"] == 0  # B has no transactions

    def test_tenant_node_count_matches_request(self, client):
        resp = client.post("/tenants", json={"name": "NodeCount-Test", "node_count": 2})
        tid = resp.json()["tenant_id"]
        state_resp = client.get(f"/tenants/{tid}/network/state")
        assert state_resp.status_code == 200
        assert state_resp.json()["node_count"] == 2


# ---------------------------------------------------------------------------
# Tenant network state
# ---------------------------------------------------------------------------

class TestTenantNetworkState:
    @pytest.fixture(scope="class")
    def tenant_id(self, client):
        resp = client.post("/tenants", json={"name": "NetworkState-Tenant", "node_count": 2})
        return resp.json()["tenant_id"]

    def test_network_state_returns_200(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/network/state")
        assert resp.status_code == 200

    def test_network_state_fields(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/network/state")
        state = resp.json()
        assert "tenant_id" in state
        assert "node_count" in state
        assert "nodes" in state
        assert "fault_rate" in state

    def test_nodes_have_required_fields(self, client, tenant_id):
        resp = client.get(f"/tenants/{tenant_id}/network/state")
        for node in resp.json()["nodes"]:
            assert "node_id" in node
            assert "port" in node
            assert "public_key_hex" in node


# ---------------------------------------------------------------------------
# Tenant webhooks
# ---------------------------------------------------------------------------

class TestTenantWebhooks:
    @pytest.fixture(scope="class")
    def tenant_id(self, client):
        resp = client.post("/tenants", json={"name": "Webhook-Tenant", "node_count": 2})
        return resp.json()["tenant_id"]

    def test_subscribe_webhook(self, client, tenant_id):
        resp = client.post(f"/tenants/{tenant_id}/webhooks", json={
            "url": "http://example.com/tenant-hook",
            "events": [],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "webhook_id" in data

    def test_list_webhooks(self, client, tenant_id):
        client.post(f"/tenants/{tenant_id}/webhooks", json={
            "url": "http://example.com/hook2", "events": []})
        resp = client.get(f"/tenants/{tenant_id}/webhooks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_unsubscribe_webhook(self, client, tenant_id):
        resp = client.post(f"/tenants/{tenant_id}/webhooks", json={
            "url": "http://del.example.com/h", "events": []})
        wid = resp.json()["webhook_id"]
        del_resp = client.delete(f"/tenants/{tenant_id}/webhooks/{wid}")
        assert del_resp.status_code == 204

    def test_invalid_event_type_returns_422(self, client, tenant_id):
        resp = client.post(f"/tenants/{tenant_id}/webhooks", json={
            "url": "http://x.com/h", "events": ["invalid"]})
        assert resp.status_code == 422
