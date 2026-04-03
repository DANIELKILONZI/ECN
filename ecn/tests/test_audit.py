"""
tests/test_audit.py
-------------------
Unit tests for ecn.audit.
"""

import pytest
from ecn.audit import AuditLog, AuditEvent, NodeVote
from ecn.node import Node, NodeResult
from ecn.consensus import ConsensusResult
from ecn.network import Network


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INITIAL_STATE = {"balances": {"A": 1000, "B": 500}}


def _make_node_result(node_id: str, state_hash: str, signature=None) -> NodeResult:
    return NodeResult(
        node_id=node_id,
        state_hash=state_hash,
        result_state={},
        trace_entry={},
        signature=signature,
    )


def _make_consensus_result(
    agreed_hash: str,
    honest: list,
    faulty: list,
    invalid_sig: list = None,
) -> ConsensusResult:
    from collections import Counter
    vote_counts = Counter(agreed_hash for _ in honest)
    return ConsensusResult(
        agreed_hash=agreed_hash,
        vote_counts=dict(vote_counts),
        honest_nodes=honest,
        faulty_nodes=faulty,
        invalid_sig_nodes=invalid_sig or [],
    )


# ---------------------------------------------------------------------------
# AuditLog.record
# ---------------------------------------------------------------------------

class TestAuditLogRecord:
    def test_record_creates_event(self):
        log = AuditLog()
        results = [
            _make_node_result("N1", "hash_abc"),
            _make_node_result("N2", "hash_abc"),
        ]
        cr = _make_consensus_result("hash_abc", ["N1", "N2"], [])
        event = log.record({"type": "transfer"}, results, cr)
        assert isinstance(event, AuditEvent)
        assert event.round_id == 1
        assert event.transaction == {"type": "transfer"}
        assert event.agreed_hash == "hash_abc"
        assert event.consensus_reached

    def test_round_ids_increment(self):
        log = AuditLog()
        results = [_make_node_result("N1", "h")]
        cr = _make_consensus_result("h", ["N1"], [])
        for i in range(5):
            e = log.record({"type": "x"}, results, cr)
            assert e.round_id == i + 1

    def test_fault_classified_correctly(self):
        log = AuditLog()
        results = [
            _make_node_result("Honest", "good_hash"),
            _make_node_result("Liar",   "bad_hash"),
        ]
        cr = _make_consensus_result("good_hash", ["Honest"], ["Liar"])
        event = log.record({"type": "t"}, results, cr)

        statuses = {v.node_id: v.status for v in event.votes}
        assert statuses["Honest"] == "honest"
        assert statuses["Liar"] == "hash_fault"

    def test_invalid_sig_classified(self):
        log = AuditLog()
        results = [
            _make_node_result("N1", "good_hash"),
            _make_node_result("BadSig", "good_hash", signature=None),
        ]
        cr = _make_consensus_result("good_hash", ["N1"], ["BadSig"], invalid_sig=["BadSig"])
        event = log.record({"type": "t"}, results, cr)
        statuses = {v.node_id: v.status for v in event.votes}
        assert statuses["BadSig"] == "sig_fault"

    def test_timestamp_is_iso8601(self):
        log = AuditLog()
        results = [_make_node_result("N1", "h")]
        cr = _make_consensus_result("h", ["N1"], [])
        event = log.record({"type": "t"}, results, cr)
        assert event.timestamp.endswith("Z")
        # Should parse as ISO format
        import datetime
        datetime.datetime.fromisoformat(event.timestamp.rstrip("Z"))


# ---------------------------------------------------------------------------
# AuditLog.get_events / get_fault_events
# ---------------------------------------------------------------------------

class TestAuditLogQuery:
    def _log_with_events(self, n_honest: int, n_fault: int) -> AuditLog:
        log = AuditLog()
        r_honest = [_make_node_result("N1", "h")]
        cr_honest = _make_consensus_result("h", ["N1"], [])
        r_fault = [_make_node_result("N1", "h"), _make_node_result("Bad", "x")]
        cr_fault = _make_consensus_result("h", ["N1"], ["Bad"])
        for _ in range(n_honest):
            log.record({"type": "t"}, r_honest, cr_honest)
        for _ in range(n_fault):
            log.record({"type": "t"}, r_fault, cr_fault)
        return log

    def test_get_events_returns_all(self):
        log = self._log_with_events(3, 2)
        assert len(log.get_events()) == 5

    def test_get_events_pagination(self):
        log = self._log_with_events(5, 0)
        page = log.get_events(limit=2, offset=1)
        assert len(page) == 2
        assert page[0].round_id == 2

    def test_get_fault_events(self):
        log = self._log_with_events(3, 2)
        faults = log.get_fault_events()
        assert len(faults) == 2
        assert all(e.has_fault() for e in faults)

    def test_get_event_by_round_id(self):
        log = self._log_with_events(3, 0)
        event = log.get_event(2)
        assert event is not None
        assert event.round_id == 2

    def test_get_event_out_of_range_returns_none(self):
        log = self._log_with_events(1, 0)
        assert log.get_event(99) is None
        assert log.get_event(0) is None

    def test_len(self):
        log = self._log_with_events(4, 1)
        assert len(log) == 5


# ---------------------------------------------------------------------------
# AuditLog.summary
# ---------------------------------------------------------------------------

class TestAuditLogSummary:
    def test_empty_summary(self):
        log = AuditLog()
        s = log.summary()
        assert s["total_rounds"] == 0
        assert s["fault_rate"] == 0.0

    def test_fault_rate_calculation(self):
        log = AuditLog()
        r1 = [_make_node_result("N1", "h")]
        cr1 = _make_consensus_result("h", ["N1"], [])
        r2 = [_make_node_result("N1", "h"), _make_node_result("Bad", "x")]
        cr2 = _make_consensus_result("h", ["N1"], ["Bad"])
        log.record({"type": "t"}, r1, cr1)
        log.record({"type": "t"}, r1, cr1)
        log.record({"type": "t"}, r2, cr2)
        log.record({"type": "t"}, r2, cr2)
        s = log.summary()
        assert s["total_rounds"] == 4
        assert s["fault_rounds"] == 2
        assert abs(s["fault_rate"] - 0.5) < 0.001

    def test_top_faulty_nodes_sorted(self):
        log = AuditLog()
        for node_id in ["A", "A", "B"]:
            results = [_make_node_result(node_id, "bad"), _make_node_result("H", "good")]
            cr = _make_consensus_result("good", ["H"], [node_id])
            log.record({"type": "t"}, results, cr)
        s = log.summary()
        top = s["top_faulty_nodes"]
        assert top[0][0] == "A"
        assert top[0][1] == 2


# ---------------------------------------------------------------------------
# AuditEvent.to_dict
# ---------------------------------------------------------------------------

class TestAuditEventToDict:
    def test_to_dict_is_json_serialisable(self):
        import json
        log = AuditLog()
        results = [_make_node_result("N1", "h", signature="abc")]
        cr = _make_consensus_result("h", ["N1"], [])
        event = log.record({"type": "t"}, results, cr)
        d = event.to_dict()
        # Should not raise
        json.dumps(d)
        assert d["round_id"] == 1
        assert d["votes"][0]["signature"] == "abc"

    def test_to_dict_required_keys(self):
        log = AuditLog()
        results = [_make_node_result("N1", "h")]
        cr = _make_consensus_result("h", ["N1"], [])
        event = log.record({"type": "t"}, results, cr)
        d = event.to_dict()
        required = {"round_id", "timestamp", "transaction", "votes",
                    "agreed_hash", "consensus_reached", "honest_nodes",
                    "faulty_nodes", "invalid_sig_nodes", "vote_counts"}
        assert required.issubset(d.keys())


# ---------------------------------------------------------------------------
# Integration: Network + AuditLog
# ---------------------------------------------------------------------------

class TestNetworkWithAuditLog:
    def test_network_records_rounds(self):
        from ecn.node import Node
        log = AuditLog()
        nodes = [Node(f"N{i}", INITIAL_STATE) for i in range(3)]
        net = Network(nodes, verbose=False, audit_log=log)
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 100}
        net.broadcast(tx)
        net.broadcast(tx)
        assert len(log) == 2

    def test_fault_recorded_in_audit(self):
        from ecn.node import Node
        log = AuditLog()
        nodes = [
            Node("H1", INITIAL_STATE),
            Node("H2", INITIAL_STATE),
            Node("Evil", INITIAL_STATE, malicious=True),
        ]
        net = Network(nodes, verbose=False, audit_log=log)
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 50}
        net.broadcast(tx)
        fault_events = log.get_fault_events()
        assert len(fault_events) == 1
        assert "Evil" in fault_events[0].faulty_nodes
