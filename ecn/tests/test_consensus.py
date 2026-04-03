"""
tests/test_consensus.py
-----------------------
Unit tests for ecn.consensus.
"""

import pytest

from ecn.node import NodeResult
from ecn.consensus import run_consensus, ConsensusResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_result(node_id: str, state_hash: str) -> NodeResult:
    return NodeResult(
        node_id=node_id,
        state_hash=state_hash,
        result_state={"balances": {}},
        trace_entry={},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRunConsensus:
    def test_all_agree(self):
        results = [make_result(f"Node-{i}", "hash_abc") for i in range(5)]
        cr = run_consensus(results)
        assert cr.agreed_hash == "hash_abc"
        assert cr.consensus_reached is True
        assert cr.faulty_nodes == []
        assert set(cr.honest_nodes) == {f"Node-{i}" for i in range(5)}

    def test_one_faulty(self):
        results = [make_result(f"Node-{i}", "hash_abc") for i in range(4)]
        results.append(make_result("Node-4", "hash_EVIL"))
        cr = run_consensus(results)
        assert cr.agreed_hash == "hash_abc"
        assert cr.consensus_reached is True
        assert "Node-4" in cr.faulty_nodes
        assert len(cr.faulty_nodes) == 1

    def test_two_faulty_still_consensus(self):
        results = [make_result(f"Node-{i}", "hash_abc") for i in range(3)]
        results.append(make_result("Node-3", "hash_EVIL"))
        results.append(make_result("Node-4", "hash_EVIL"))
        cr = run_consensus(results)
        assert cr.agreed_hash == "hash_abc"
        assert cr.consensus_reached is True
        assert len(cr.faulty_nodes) == 2

    def test_split_vote_no_majority(self):
        # 2 vs 2 — no strict majority
        results = [
            make_result("Node-1", "hash_A"),
            make_result("Node-2", "hash_A"),
            make_result("Node-3", "hash_B"),
            make_result("Node-4", "hash_B"),
        ]
        cr = run_consensus(results)
        assert cr.consensus_reached is False

    def test_empty_results(self):
        cr = run_consensus([])
        assert cr.agreed_hash is None
        assert cr.consensus_reached is False
        assert cr.faulty_nodes == []
        assert cr.honest_nodes == []

    def test_single_node(self):
        results = [make_result("Node-1", "hash_solo")]
        cr = run_consensus(results)
        assert cr.agreed_hash == "hash_solo"
        assert cr.consensus_reached is True

    def test_total_nodes_count(self):
        results = [make_result(f"Node-{i}", "hash_abc") for i in range(5)]
        cr = run_consensus(results)
        assert cr.total_nodes == 5

    def test_vote_counts_populated(self):
        results = [make_result("N1", "h1"), make_result("N2", "h1"), make_result("N3", "h2")]
        cr = run_consensus(results)
        assert cr.vote_counts["h1"] == 2
        assert cr.vote_counts["h2"] == 1

    def test_tie_breaking_is_deterministic(self):
        """Ties must always resolve to the same hash regardless of input order."""
        results_a = [make_result("N1", "zzz"), make_result("N2", "aaa")]
        results_b = [make_result("N1", "aaa"), make_result("N2", "zzz")]
        cr_a = run_consensus(results_a)
        cr_b = run_consensus(results_b)
        # Both should pick the lexicographically smallest hash
        assert cr_a.agreed_hash == cr_b.agreed_hash == "aaa"
