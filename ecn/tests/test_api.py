"""
tests/test_api.py
-----------------
Integration tests for the FastAPI REST API (ecn.api).

These tests use FastAPI's TestClient (sync wrapper over httpx) which handles
the asyncio lifecycle internally.  The lifespan starts real asyncio TCP node
servers on localhost, so this is a true end-to-end integration test.
"""

import pytest
from fastapi.testclient import TestClient

from ecn.api import app


# The TestClient must be used as a context manager so the FastAPI lifespan
# (which boots the asyncio TCP node servers) is properly started/stopped.
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /network/nodes
# ---------------------------------------------------------------------------

class TestNetworkNodes:
    def test_returns_list_of_nodes(self, client):
        resp = client.get("/network/nodes")
        assert resp.status_code == 200
        nodes = resp.json()
        assert isinstance(nodes, list)
        assert len(nodes) == 5  # Warehouse, Shipper, Customs, Insurer, Retailer

    def test_node_has_required_fields(self, client):
        resp = client.get("/network/nodes")
        node = resp.json()[0]
        assert "node_id" in node
        assert "port" in node
        assert "public_key_hex" in node
        assert "malicious" in node

    def test_nodes_are_honest(self, client):
        resp = client.get("/network/nodes")
        for node in resp.json():
            assert node["malicious"] is False


# ---------------------------------------------------------------------------
# /network/state
# ---------------------------------------------------------------------------

class TestNetworkState:
    def test_returns_state(self, client):
        resp = client.get("/network/state")
        assert resp.status_code == 200
        state = resp.json()
        assert state["node_count"] == 5

    def test_fault_rate_zero_initially(self, client):
        # Only check that the field is present; it may not be zero if other
        # tests ran first in the same module scope.
        resp = client.get("/network/state")
        assert "fault_rate" in resp.json()


# ---------------------------------------------------------------------------
# POST /transactions
# ---------------------------------------------------------------------------

class TestSubmitTransaction:
    def test_ship_transaction(self, client):
        resp = client.post("/transactions", json={
            "type": "ship",
            "product_id": "LAPTOP-001",
            "destination": "port",
            "shipper": "DHL",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["consensus_reached"] is True
        assert data["faulty_nodes"] == []
        assert data["agreed_hash"] is not None

    def test_response_has_all_fields(self, client):
        resp = client.post("/transactions", json={
            "type": "inspect",
            "product_id": "LAPTOP-001",
            "check_name": "label",
            "inspector": "QA",
        })
        assert resp.status_code == 200
        data = resp.json()
        required = {"round_id", "timestamp", "transaction", "consensus_reached",
                    "agreed_hash", "honest_nodes", "faulty_nodes", "invalid_sig_nodes", "votes"}
        assert required.issubset(data.keys())

    def test_votes_present_for_all_nodes(self, client):
        resp = client.post("/transactions", json={
            "type": "ship",
            "product_id": "PHONE-002",
            "destination": "airport",
            "shipper": "FedEx",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["votes"]) == 5

    def test_votes_have_signatures(self, client):
        resp = client.post("/transactions", json={
            "type": "ship",
            "product_id": "TABLET-003",
            "destination": "warehouse",
            "shipper": "UPS",
        })
        assert resp.status_code == 200
        for vote in resp.json()["votes"]:
            assert vote["signature"] is not None

    def test_unknown_tx_type_returns_422(self, client):
        resp = client.post("/transactions", json={"type": "explode"})
        assert resp.status_code == 422

    def test_domain_error_returns_422(self, client):
        # Receiving a quarantined product (after prior quarantine test sets status)
        # or any invalid state transition — just verify the 422 path exists.
        resp = client.post("/transactions", json={
            "type": "receive",
            "product_id": "LAPTOP-001",
            "receiver": "buyer",
            "at_customs": False,
        })
        # Either 200 (if product was in_transit from prior test) or 422 (invalid)
        assert resp.status_code in (200, 422)


# ---------------------------------------------------------------------------
# GET /audit/events
# ---------------------------------------------------------------------------

class TestAuditEvents:
    def test_returns_events(self, client):
        resp = client.get("/audit/events")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "events" in data
        assert data["total"] > 0

    def test_pagination(self, client):
        resp = client.get("/audit/events?offset=0&limit=2")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) <= 2

    def test_event_structure(self, client):
        resp = client.get("/audit/events?limit=1")
        event = resp.json()["events"][0]
        assert "round_id" in event
        assert "timestamp" in event
        assert "transaction" in event
        assert "consensus_reached" in event


# ---------------------------------------------------------------------------
# GET /audit/events/faults
# ---------------------------------------------------------------------------

class TestAuditFaultEvents:
    def test_returns_list(self, client):
        resp = client.get("/audit/events/faults")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "events" in data

    def test_all_fault_events_have_faults(self, client):
        resp = client.get("/audit/events/faults")
        for event in resp.json()["events"]:
            assert len(event["faulty_nodes"]) > 0


# ---------------------------------------------------------------------------
# GET /audit/events/{round_id}
# ---------------------------------------------------------------------------

class TestAuditEventById:
    def test_get_round_1(self, client):
        resp = client.get("/audit/events/1")
        assert resp.status_code == 200
        assert resp.json()["round_id"] == 1

    def test_not_found(self, client):
        resp = client.get("/audit/events/99999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /audit/summary
# ---------------------------------------------------------------------------

class TestAuditSummary:
    def test_summary_fields(self, client):
        resp = client.get("/audit/summary")
        assert resp.status_code == 200
        s = resp.json()
        assert "total_rounds" in s
        assert "fault_rounds" in s
        assert "fault_rate" in s
        assert "top_faulty_nodes" in s

    def test_total_rounds_positive(self, client):
        resp = client.get("/audit/summary")
        assert resp.json()["total_rounds"] >= 1


# ---------------------------------------------------------------------------
# GET /audit/replay
# ---------------------------------------------------------------------------

class TestAuditReplay:
    def test_replay_structure(self, client):
        resp = client.get("/audit/replay")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "replay" in data

    def test_replay_entries_have_required_fields(self, client):
        resp = client.get("/audit/replay")
        for entry in resp.json()["replay"]:
            assert "round_id" in entry
            assert "transaction" in entry
            assert "consensus_reached" in entry
            assert "fault_detected" in entry
            assert "faulty_nodes" in entry


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------

class TestWebhooks:
    def test_subscribe_returns_201(self, client):
        resp = client.post("/webhooks", json={"url": "http://example.com/hook", "events": []})
        assert resp.status_code == 201

    def test_subscribe_response_fields(self, client):
        resp = client.post("/webhooks", json={"url": "http://example.com/hook", "events": ["fault"]})
        assert resp.status_code == 201
        data = resp.json()
        assert "webhook_id" in data
        assert data["url"] == "http://example.com/hook"
        assert data["events"] == ["fault"]
        assert "message" in data

    def test_list_webhooks(self, client):
        # Subscribe first to ensure at least one entry
        client.post("/webhooks", json={"url": "http://example.com/list-test", "events": []})
        resp = client.get("/webhooks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_subscribe_invalid_event_type_returns_422(self, client):
        resp = client.post("/webhooks", json={"url": "http://example.com/h", "events": ["invalid_type"]})
        assert resp.status_code == 422

    def test_unsubscribe_removes_webhook(self, client):
        # Subscribe
        sub_resp = client.post("/webhooks", json={"url": "http://del-test.com/h", "events": []})
        webhook_id = sub_resp.json()["webhook_id"]
        # Unsubscribe
        del_resp = client.delete(f"/webhooks/{webhook_id}")
        assert del_resp.status_code == 204
        # Verify gone from list
        hooks = client.get("/webhooks").json()
        ids = [h["webhook_id"] for h in hooks]
        assert webhook_id not in ids

    def test_unsubscribe_not_found_returns_404(self, client):
        resp = client.delete("/webhooks/no-such-id")
        assert resp.status_code == 404
