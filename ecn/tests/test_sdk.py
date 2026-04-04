"""
tests/test_sdk.py
-----------------
Unit tests for ecn.sdk.ECNClient.

Uses unittest.mock to stub the underlying requests.Session so tests run
without a live API server.  Every public SDK method is exercised.
"""

import json
from unittest.mock import MagicMock, patch, call

import pytest
import requests

from ecn.sdk import ECNClient, ECNError, ECNTransactionError, ECNNotFoundError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code: int = 200, body: dict = None, text: str = "") -> MagicMock:
    """Build a fake requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = (200 <= status_code < 300)
    if body is not None:
        resp.content = json.dumps(body).encode()
        resp.json.return_value = body
    else:
        resp.content = b""
        resp.json.side_effect = Exception("no body")
    resp.text = text
    return resp


ROUND_RESPONSE = {
    "round_id": 1,
    "timestamp": "2026-04-03T21:00:00Z",
    "transaction": {"type": "ship"},
    "consensus_reached": True,
    "agreed_hash": "abc123",
    "honest_nodes": ["A", "B"],
    "faulty_nodes": [],
    "invalid_sig_nodes": [],
    "votes": [],
}


# ---------------------------------------------------------------------------
# Context manager and lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_context_manager_closes_session(self):
        client = ECNClient("http://localhost:8000")
        with patch.object(client._session, "close") as mock_close:
            with client:
                pass
            mock_close.assert_called_once()

    def test_close_closes_session(self):
        client = ECNClient("http://localhost:8000")
        with patch.object(client._session, "close") as mock_close:
            client.close()
            mock_close.assert_called_once()

    def test_base_url_trailing_slash_stripped(self):
        client = ECNClient("http://localhost:8000/")
        assert client._base_url == "http://localhost:8000"
        client.close()

    def test_api_key_header_set(self):
        client = ECNClient("http://localhost:8000", api_key="secret-token")
        assert client._session.headers.get("X-API-Key") == "secret-token"
        client.close()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def _client(self):
        return ECNClient("http://localhost:8000")

    def test_404_raises_not_found(self):
        client = self._client()
        resp = _mock_response(404, {"detail": "Round 99 not found"})
        with patch.object(client._session, "get", return_value=resp):
            with pytest.raises(ECNNotFoundError) as exc_info:
                client.audit_event(99)
            assert exc_info.value.status_code == 404
        client.close()

    def test_422_raises_transaction_error(self):
        client = self._client()
        resp = _mock_response(422, {"detail": "Unknown transaction type"})
        with patch.object(client._session, "post", return_value=resp):
            with pytest.raises(ECNTransactionError) as exc_info:
                client.submit("explode")
            assert exc_info.value.status_code == 422
        client.close()

    def test_500_raises_ecn_error(self):
        client = self._client()
        resp = _mock_response(500, {"detail": "Internal error"})
        with patch.object(client._session, "get", return_value=resp):
            with pytest.raises(ECNError) as exc_info:
                client.network_state()
            assert exc_info.value.status_code == 500
        client.close()

    def test_204_returns_none(self):
        client = self._client()
        resp = _mock_response(204)
        with patch.object(client._session, "delete", return_value=resp):
            result = client.unsubscribe_webhook("some-id")
            assert result is None
        client.close()


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

class TestTransactionHelpers:
    def setup_method(self):
        self.client = ECNClient("http://localhost:8000")
        self.ok = _mock_response(200, ROUND_RESPONSE)

    def teardown_method(self):
        self.client.close()

    def test_submit_posts_to_transactions(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            result = self.client.submit("ship", product_id="P1", destination="port", shipper="DHL")
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert args[0] == "http://localhost:8000/transactions"
            assert kwargs["json"]["type"] == "ship"
            assert kwargs["json"]["product_id"] == "P1"
        assert result["consensus_reached"] is True

    def test_ship(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            self.client.ship("LAPTOP-001", destination="port", shipper="DHL")
            body = mock_post.call_args[1]["json"]
            assert body["type"] == "ship"
            assert body["product_id"] == "LAPTOP-001"
            assert body["destination"] == "port"
            assert body["shipper"] == "DHL"

    def test_receive(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            self.client.receive("LAPTOP-001", receiver="buyer", at_customs=True)
            body = mock_post.call_args[1]["json"]
            assert body["type"] == "receive"
            assert body["at_customs"] is True

    def test_receive_default_at_customs_false(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            self.client.receive("LAPTOP-001", receiver="buyer")
            body = mock_post.call_args[1]["json"]
            assert body["at_customs"] is False

    def test_inspect(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            self.client.inspect("LAPTOP-001", check_name="label", inspector="Lab")
            body = mock_post.call_args[1]["json"]
            assert body["type"] == "inspect"
            assert body["check_name"] == "label"

    def test_quarantine(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            self.client.quarantine("LAPTOP-001", reason="damaged")
            body = mock_post.call_args[1]["json"]
            assert body["type"] == "quarantine"
            assert body["reason"] == "damaged"

    def test_release(self):
        with patch.object(self.client._session, "post", return_value=self.ok) as mock_post:
            self.client.release("LAPTOP-001", released_by="Inspector")
            body = mock_post.call_args[1]["json"]
            assert body["type"] == "release"
            assert body["released_by"] == "Inspector"


# ---------------------------------------------------------------------------
# Network endpoints
# ---------------------------------------------------------------------------

class TestNetworkEndpoints:
    def setup_method(self):
        self.client = ECNClient("http://localhost:8000")

    def teardown_method(self):
        self.client.close()

    def test_nodes(self):
        nodes_data = [{"node_id": "A", "port": 9001, "public_key_hex": "abcd", "malicious": False}]
        resp = _mock_response(200, nodes_data)
        with patch.object(self.client._session, "get", return_value=resp):
            result = self.client.nodes()
            assert result == nodes_data

    def test_nodes_calls_correct_path(self):
        resp = _mock_response(200, [])
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            self.client.nodes()
            assert mock_get.call_args[0][0] == "http://localhost:8000/network/nodes"

    def test_network_state(self):
        state = {"node_count": 5, "fault_rate": 0.0}
        resp = _mock_response(200, state)
        with patch.object(self.client._session, "get", return_value=resp):
            result = self.client.network_state()
            assert result["node_count"] == 5

    def test_network_state_calls_correct_path(self):
        resp = _mock_response(200, {})
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            self.client.network_state()
            assert mock_get.call_args[0][0] == "http://localhost:8000/network/state"


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------

class TestAuditEndpoints:
    def setup_method(self):
        self.client = ECNClient("http://localhost:8000")

    def teardown_method(self):
        self.client.close()

    def test_audit_events(self):
        data = {"total": 3, "offset": 0, "events": []}
        resp = _mock_response(200, data)
        with patch.object(self.client._session, "get", return_value=resp):
            result = self.client.audit_events()
            assert result["total"] == 3

    def test_audit_events_pagination_params(self):
        resp = _mock_response(200, {"total": 0, "offset": 10, "events": []})
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            self.client.audit_events(limit=20, offset=10)
            params = mock_get.call_args[1]["params"]
            assert params["limit"] == 20
            assert params["offset"] == 10

    def test_fault_events(self):
        data = {"total": 1, "events": [ROUND_RESPONSE]}
        resp = _mock_response(200, data)
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            result = self.client.fault_events()
            assert "http://localhost:8000/audit/events/faults" in mock_get.call_args[0][0]
            assert result["total"] == 1

    def test_audit_event_by_id(self):
        resp = _mock_response(200, ROUND_RESPONSE)
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            result = self.client.audit_event(1)
            assert "/audit/events/1" in mock_get.call_args[0][0]
            assert result["round_id"] == 1

    def test_audit_event_not_found(self):
        resp = _mock_response(404, {"detail": "Round 99 not found"})
        with patch.object(self.client._session, "get", return_value=resp):
            with pytest.raises(ECNNotFoundError):
                self.client.audit_event(99)

    def test_audit_summary(self):
        summary = {"total_rounds": 5, "fault_rounds": 1, "fault_rate": 0.2,
                   "consensus_failure_rounds": 0, "top_faulty_nodes": [["Evil", 1]]}
        resp = _mock_response(200, summary)
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            result = self.client.audit_summary()
            assert "http://localhost:8000/audit/summary" in mock_get.call_args[0][0]
            assert result["fault_rate"] == 0.2

    def test_replay(self):
        data = {"total": 3, "replay": []}
        resp = _mock_response(200, data)
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            result = self.client.replay()
            assert "http://localhost:8000/audit/replay" in mock_get.call_args[0][0]
            assert result["total"] == 3


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------

class TestWebhookEndpoints:
    def setup_method(self):
        self.client = ECNClient("http://localhost:8000")

    def teardown_method(self):
        self.client.close()

    def test_subscribe_webhook(self):
        response_body = {
            "webhook_id": "uuid-123",
            "url": "https://example.com/hook",
            "events": ["fault"],
            "message": "Webhook registered.",
        }
        resp = _mock_response(201, response_body)
        with patch.object(self.client._session, "post", return_value=resp) as mock_post:
            result = self.client.subscribe_webhook("https://example.com/hook", events=["fault"])
            body = mock_post.call_args[1]["json"]
            assert body["url"] == "https://example.com/hook"
            assert body["events"] == ["fault"]
            assert "http://localhost:8000/webhooks" in mock_post.call_args[0][0]
        assert result["webhook_id"] == "uuid-123"

    def test_subscribe_webhook_no_events(self):
        response_body = {"webhook_id": "uuid-456", "url": "https://x.com", "events": [], "message": "ok"}
        resp = _mock_response(201, response_body)
        with patch.object(self.client._session, "post", return_value=resp) as mock_post:
            self.client.subscribe_webhook("https://x.com")
            body = mock_post.call_args[1]["json"]
            assert body["events"] == []

    def test_list_webhooks(self):
        subs = [{"webhook_id": "x", "url": "https://a.com", "events": []}]
        resp = _mock_response(200, subs)
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            result = self.client.list_webhooks()
            assert "http://localhost:8000/webhooks" in mock_get.call_args[0][0]
            assert result == subs

    def test_unsubscribe_webhook(self):
        resp = _mock_response(204)
        with patch.object(self.client._session, "delete", return_value=resp) as mock_del:
            result = self.client.unsubscribe_webhook("uuid-123")
            assert "http://localhost:8000/webhooks/uuid-123" in mock_del.call_args[0][0]
            assert result is None

    def test_unsubscribe_not_found(self):
        resp = _mock_response(404, {"detail": "Webhook not found"})
        with patch.object(self.client._session, "delete", return_value=resp):
            with pytest.raises(ECNNotFoundError):
                self.client.unsubscribe_webhook("bad-id")
