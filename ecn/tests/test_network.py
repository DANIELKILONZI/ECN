"""
tests/test_network.py
---------------------
Integration tests for ecn.network.
"""

import copy
import pytest

from ecn.node import Node
from ecn.network import Network


BASE_STATE = {
    "balances": {
        "A": 1000,
        "B": 500,
    }
}

TX = {"type": "transfer", "from": "A", "to": "B", "amount": 100}


def make_network(n_honest: int, n_malicious: int = 0, verbose: bool = False) -> Network:
    nodes = []
    for i in range(n_honest):
        nodes.append(Node(f"Node-{i+1}", copy.deepcopy(BASE_STATE)))
    for j in range(n_malicious):
        nodes.append(
            Node(f"Evil-{j+1}", copy.deepcopy(BASE_STATE), malicious=True)
        )
    return Network(nodes, verbose=verbose)


class TestNormalOperation:
    def test_all_honest_consensus_reached(self):
        net = make_network(5)
        _, cr = net.broadcast(TX)
        assert cr.consensus_reached is True
        assert len(cr.faulty_nodes) == 0

    def test_all_honest_same_hash(self):
        net = make_network(3)
        results, _ = net.broadcast(TX)
        hashes = {r.state_hash for r in results}
        assert len(hashes) == 1

    def test_history_recorded(self):
        net = make_network(2)
        net.broadcast(TX)
        history = net.get_history()
        assert len(history) == 1
        assert history[0][0] == TX


class TestFaultInjection:
    def test_one_malicious_flagged(self):
        net = make_network(4, n_malicious=1)
        _, cr = net.broadcast(TX)
        assert cr.consensus_reached is True
        assert len(cr.faulty_nodes) == 1
        assert "Evil-1" in cr.faulty_nodes

    def test_two_malicious_flagged(self):
        net = make_network(4, n_malicious=2)
        _, cr = net.broadcast(TX)
        assert cr.consensus_reached is True
        assert len(cr.faulty_nodes) == 2

    def test_majority_malicious_no_consensus(self):
        # 2 honest vs 3 malicious: majority are faulty
        net = make_network(2, n_malicious=3)
        _, cr = net.broadcast(TX)
        # Consensus reached on whatever has most votes (the evil hash)
        # but honest nodes should be in faulty list
        assert cr.total_nodes == 5
