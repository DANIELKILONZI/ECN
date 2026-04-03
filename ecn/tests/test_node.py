"""
tests/test_node.py
------------------
Unit tests for ecn.node.
"""

import copy
import pytest

from ecn.node import Node, NodeResult


BASE_STATE = {
    "balances": {
        "A": 1000,
        "B": 500,
    }
}

TX = {"type": "transfer", "from": "A", "to": "B", "amount": 100}


class TestHonestNode:
    def test_execute_returns_node_result(self):
        node = Node("Node-1", copy.deepcopy(BASE_STATE))
        result = node.execute_transaction(TX)
        assert isinstance(result, NodeResult)

    def test_state_updated_after_execution(self):
        node = Node("Node-1", copy.deepcopy(BASE_STATE))
        node.execute_transaction(TX)
        assert node.get_state()["balances"]["A"] == 900

    def test_hash_is_deterministic(self):
        node1 = Node("Node-1", copy.deepcopy(BASE_STATE))
        node2 = Node("Node-2", copy.deepcopy(BASE_STATE))
        r1 = node1.execute_transaction(TX)
        r2 = node2.execute_transaction(TX)
        assert r1.state_hash == r2.state_hash

    def test_node_id_in_result(self):
        node = Node("MyNode", copy.deepcopy(BASE_STATE))
        result = node.execute_transaction(TX)
        assert result.node_id == "MyNode"

    def test_trace_recorded(self):
        node = Node("Node-1", copy.deepcopy(BASE_STATE))
        node.execute_transaction(TX)
        trace = node.get_trace()
        assert len(trace) == 1
        assert trace[0]["tx"] == TX


class TestMaliciousNode:
    def test_malicious_node_reports_different_hash(self):
        honest = Node("Honest", copy.deepcopy(BASE_STATE), malicious=False)
        evil = Node("Evil", copy.deepcopy(BASE_STATE), malicious=True)
        r_honest = honest.execute_transaction(TX)
        r_evil = evil.execute_transaction(TX)
        assert r_honest.state_hash != r_evil.state_hash

    def test_malicious_node_still_computes_correct_state(self):
        """Internal state of malicious node must still be correct."""
        evil = Node("Evil", copy.deepcopy(BASE_STATE), malicious=True)
        evil.execute_transaction(TX)
        # Internal state should be correctly updated
        assert evil.get_state()["balances"]["A"] == 900
        assert evil.get_state()["balances"]["B"] == 600

    def test_malicious_hash_ends_with_deadbeef(self):
        evil = Node("Evil", copy.deepcopy(BASE_STATE), malicious=True)
        result = evil.execute_transaction(TX)
        assert result.state_hash.endswith("DEADBEEF")


class TestNodeReset:
    def test_reset_restores_state(self):
        node = Node("Node-1", copy.deepcopy(BASE_STATE))
        node.execute_transaction(TX)
        node.reset(copy.deepcopy(BASE_STATE))
        assert node.get_state()["balances"]["A"] == 1000
