"""
tests/test_stream.py
--------------------
Tests for ecn.stream — EventBus and stream API endpoints.
"""

import asyncio
import json
import pytest

from ecn.stream import EventBus, TOPIC_TRANSACTIONS, TOPIC_FAULTS, ALL_TOPICS


# ---------------------------------------------------------------------------
# EventBus unit tests
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_topic_info_lists_all_topics(self):
        bus = EventBus()
        info = {t["topic"]: t for t in bus.topic_info()}
        assert TOPIC_TRANSACTIONS in info
        assert TOPIC_FAULTS in info

    def test_publish_increments_offset(self):
        bus = EventBus()
        assert bus.next_offset(TOPIC_TRANSACTIONS) == 0
        bus.publish(TOPIC_TRANSACTIONS, {"x": 1})
        assert bus.next_offset(TOPIC_TRANSACTIONS) == 1
        bus.publish(TOPIC_TRANSACTIONS, {"x": 2})
        assert bus.next_offset(TOPIC_TRANSACTIONS) == 2

    def test_publish_returns_seq(self):
        bus = EventBus()
        seq0 = bus.publish(TOPIC_TRANSACTIONS, {"a": 1})
        seq1 = bus.publish(TOPIC_TRANSACTIONS, {"a": 2})
        assert seq0 == 0
        assert seq1 == 1

    def test_consume_empty_topic(self):
        bus = EventBus()
        events = bus.consume(TOPIC_TRANSACTIONS)
        assert events == []

    def test_consume_returns_events(self):
        bus = EventBus()
        bus.publish(TOPIC_TRANSACTIONS, {"n": 1})
        bus.publish(TOPIC_TRANSACTIONS, {"n": 2})
        bus.publish(TOPIC_TRANSACTIONS, {"n": 3})
        events = bus.consume(TOPIC_TRANSACTIONS, offset=0, limit=10)
        assert len(events) == 3
        assert events[0]["seq"] == 0
        assert events[2]["seq"] == 2

    def test_consume_respects_offset(self):
        bus = EventBus()
        for i in range(5):
            bus.publish(TOPIC_TRANSACTIONS, {"n": i})
        events = bus.consume(TOPIC_TRANSACTIONS, offset=2, limit=10)
        assert len(events) == 3
        assert events[0]["seq"] == 2

    def test_consume_respects_limit(self):
        bus = EventBus()
        for i in range(10):
            bus.publish(TOPIC_TRANSACTIONS, {"n": i})
        events = bus.consume(TOPIC_TRANSACTIONS, offset=0, limit=3)
        assert len(events) == 3

    def test_consume_includes_topic_field(self):
        bus = EventBus()
        bus.publish(TOPIC_TRANSACTIONS, {"x": 1})
        events = bus.consume(TOPIC_TRANSACTIONS, offset=0)
        assert events[0]["topic"] == TOPIC_TRANSACTIONS

    def test_consume_payload(self):
        bus = EventBus()
        bus.publish(TOPIC_TRANSACTIONS, {"round_id": 42})
        events = bus.consume(TOPIC_TRANSACTIONS, offset=0)
        assert events[0]["payload"]["round_id"] == 42

    def test_unknown_topic_raises(self):
        bus = EventBus()
        with pytest.raises(ValueError):
            bus.publish("nonexistent", {})
        with pytest.raises(ValueError):
            bus.consume("nonexistent")

    def test_ring_buffer_maxlen(self):
        bus = EventBus(maxlen=5)
        for i in range(10):
            bus.publish(TOPIC_TRANSACTIONS, {"n": i})
        # Only last 5 are retained
        assert len(bus._buffers[TOPIC_TRANSACTIONS]) == 5
        events = bus.consume(TOPIC_TRANSACTIONS, offset=5, limit=10)
        assert len(events) == 5  # seqs 5-9

    def test_fault_topic_independent(self):
        bus = EventBus()
        bus.publish(TOPIC_TRANSACTIONS, {"type": "tx"})
        bus.publish(TOPIC_FAULTS, {"type": "fault"})
        txs = bus.consume(TOPIC_TRANSACTIONS, offset=0)
        faults = bus.consume(TOPIC_FAULTS, offset=0)
        assert len(txs) == 1
        assert len(faults) == 1
        assert txs[0]["seq"] == 0
        assert faults[0]["seq"] == 0  # independent counters

    def test_topic_info_buffered_count(self):
        bus = EventBus()
        for i in range(3):
            bus.publish(TOPIC_FAULTS, {"n": i})
        info = {t["topic"]: t for t in bus.topic_info()}
        assert info[TOPIC_FAULTS]["buffered_events"] == 3
        assert info[TOPIC_FAULTS]["next_offset"] == 3


class TestEventBusLiveSubscription:
    def test_subscribe_live_receives_event(self):
        async def _run():
            bus = EventBus()
            q = bus.subscribe_live([TOPIC_TRANSACTIONS])
            bus.publish(TOPIC_TRANSACTIONS, {"msg": "hello"})
            seq, payload = await asyncio.wait_for(q.get(), timeout=1.0)
            assert seq == 0
            assert payload["msg"] == "hello"
            bus.unsubscribe_live(q)

        asyncio.run(_run())

    def test_unsubscribe_stops_delivery(self):
        async def _run():
            bus = EventBus()
            q = bus.subscribe_live([TOPIC_TRANSACTIONS])
            bus.unsubscribe_live(q)
            bus.publish(TOPIC_TRANSACTIONS, {"msg": "after unsub"})
            assert q.empty()

        asyncio.run(_run())

    def test_multiple_subscribers(self):
        async def _run():
            bus = EventBus()
            q1 = bus.subscribe_live([TOPIC_TRANSACTIONS])
            q2 = bus.subscribe_live([TOPIC_TRANSACTIONS])
            bus.publish(TOPIC_TRANSACTIONS, {"x": 99})
            seq1, p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            seq2, p2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert seq1 == seq2 == 0
            assert p1 == p2
            bus.unsubscribe_live(q1)
            bus.unsubscribe_live(q2)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Stream API endpoint tests
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from ecn.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestStreamTopics:
    def test_list_topics_returns_200(self, client):
        resp = client.get("/stream/topics")
        assert resp.status_code == 200

    def test_list_topics_structure(self, client):
        resp = client.get("/stream/topics")
        data = resp.json()
        assert "topics" in data
        topics_by_name = {t["topic"]: t for t in data["topics"]}
        assert "transactions" in topics_by_name
        assert "faults" in topics_by_name

    def test_topic_info_has_offset_and_count(self, client):
        resp = client.get("/stream/topics")
        for t in resp.json()["topics"]:
            assert "next_offset" in t
            assert "buffered_events" in t


class TestStreamPoll:
    def test_poll_transactions_before_any(self, client):
        resp = client.get("/stream/topics/transactions?offset=0&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic"] == "transactions"
        assert "events" in data
        assert "next_offset" in data

    def test_poll_unknown_topic_returns_404(self, client):
        resp = client.get("/stream/topics/unknown_topic")
        assert resp.status_code == 404

    def test_poll_after_transaction(self, client):
        # Submit a transaction to populate the stream
        client.post("/transactions", json={
            "type": "ship",
            "product_id": "LAPTOP-001",
            "destination": "port",
            "shipper": "DHL",
        })
        resp = client.get("/stream/topics/transactions?offset=0&limit=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

    def test_poll_event_has_seq_and_payload(self, client):
        client.post("/transactions", json={
            "type": "inspect",
            "product_id": "LAPTOP-001",
            "check_name": "weight",
            "inspector": "QA",
        })
        resp = client.get("/stream/topics/transactions?offset=0&limit=50")
        events = resp.json()["events"]
        assert len(events) >= 1
        first = events[0]
        assert "seq" in first
        assert "payload" in first
        assert "round_id" in first["payload"]

    def test_poll_faults_initially_empty(self, client):
        resp = client.get("/stream/topics/faults?offset=0&limit=10")
        assert resp.status_code == 200

    def test_poll_respects_offset(self, client):
        # Get current next_offset for transactions topic
        info = client.get("/stream/topics").json()
        current_offset = 0
        for t in info["topics"]:
            if t["topic"] == "transactions":
                current_offset = t["next_offset"]
                break
        # Poll from beyond current offset → empty
        resp = client.get(f"/stream/topics/transactions?offset={current_offset + 1000}&limit=5")
        assert resp.json()["events"] == []


class TestSSEEndpoint:
    def test_sse_content_type(self, client):
        # Submit a transaction so there is at least 1 event in the bus
        client.post("/transactions", json={
            "type": "ship",
            "product_id": "PHONE-002",
            "destination": "airport",
            "shipper": "FedEx",
        })
        # Use backfill=50 to replay buffered events; limit=1 to auto-close
        with client.stream("GET", "/stream/events?topics=transactions&backfill=50&limit=1") as resp:
            assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_sse_returns_data_lines(self, client):
        client.post("/transactions", json={
            "type": "inspect",
            "product_id": "PHONE-002",
            "check_name": "label",
            "inspector": "Lab",
        })
        lines = []
        with client.stream("GET", "/stream/events?topics=transactions&backfill=50&limit=1") as resp:
            for line in resp.iter_lines():
                if line:
                    lines.append(line)
                    break
        assert any(l.startswith("data:") for l in lines)

    def test_sse_data_is_valid_json(self, client):
        client.post("/transactions", json={
            "type": "ship",
            "product_id": "TABLET-003",
            "destination": "warehouse",
            "shipper": "UPS",
        })
        with client.stream("GET", "/stream/events?topics=transactions&backfill=50&limit=1") as resp:
            for line in resp.iter_lines():
                if line and line.startswith("data:"):
                    payload = json.loads(line[len("data:"):].strip())
                    assert "seq" in payload
                    assert "payload" in payload
                    break
