"""
tests/test_state_manager.py
---------------------------
Unit tests for ecn.state_manager.
"""

import copy
import pytest

from ecn.state_manager import StateManager, build_merkle_tree, merkle_root


BASE_STATE = {
    "balances": {
        "A": 1000,
        "B": 500,
    }
}


# ---------------------------------------------------------------------------
# StateManager basics
# ---------------------------------------------------------------------------

class TestStateManager:
    def test_get_state_returns_copy(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        s = sm.get_state()
        s["balances"]["A"] = 0
        assert sm.get_state()["balances"]["A"] == 1000  # not mutated

    def test_apply_transaction_updates_state(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 100}
        sm.apply_transaction(tx)
        assert sm.get_state()["balances"]["A"] == 900
        assert sm.get_state()["balances"]["B"] == 600

    def test_apply_transaction_returns_new_state(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 50}
        result = sm.apply_transaction(tx)
        assert result["balances"]["A"] == 950

    def test_state_hash_is_deterministic(self):
        sm1 = StateManager(copy.deepcopy(BASE_STATE))
        sm2 = StateManager(copy.deepcopy(BASE_STATE))
        assert sm1.compute_state_hash() == sm2.compute_state_hash()

    def test_state_hash_changes_after_transaction(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        h_before = sm.compute_state_hash()
        sm.apply_transaction({"type": "transfer", "from": "A", "to": "B", "amount": 1})
        h_after = sm.compute_state_hash()
        assert h_before != h_after

    def test_same_state_same_hash(self):
        sm1 = StateManager({"balances": {"A": 900, "B": 600}})
        sm2 = StateManager({"balances": {"A": 900, "B": 600}})
        assert sm1.compute_state_hash() == sm2.compute_state_hash()

    def test_hash_is_hex_string(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        h = sm.compute_state_hash()
        assert isinstance(h, str)
        # SHA-256 hex = 64 chars
        assert len(h) == 64
        int(h, 16)  # should not raise

    def test_trace_is_recorded(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 10}
        sm.apply_transaction(tx)
        trace = sm.get_trace()
        assert len(trace) == 1
        assert trace[0]["tx"] == tx
        assert "old_hash" in trace[0]
        assert "new_hash" in trace[0]
        assert "diff" in trace[0]

    def test_diff_recorded_correctly(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        sm.apply_transaction({"type": "transfer", "from": "A", "to": "B", "amount": 200})
        diff = sm.get_trace()[0]["diff"]
        assert diff["A"] == (1000, 800)
        assert diff["B"] == (500, 700)

    def test_reset_clears_state_and_trace(self):
        sm = StateManager(copy.deepcopy(BASE_STATE))
        sm.apply_transaction({"type": "transfer", "from": "A", "to": "B", "amount": 10})
        sm.reset()
        assert sm.get_state() == {"balances": {}}
        assert sm.get_trace() == []


# ---------------------------------------------------------------------------
# Merkle hashing
# ---------------------------------------------------------------------------

class TestMerkleHashing:
    def test_merkle_hash_deterministic(self):
        sm1 = StateManager(copy.deepcopy(BASE_STATE), use_merkle=True)
        sm2 = StateManager(copy.deepcopy(BASE_STATE), use_merkle=True)
        assert sm1.compute_state_hash() == sm2.compute_state_hash()

    def test_merkle_and_sha256_differ(self):
        sm_sha = StateManager(copy.deepcopy(BASE_STATE), use_merkle=False)
        sm_mkl = StateManager(copy.deepcopy(BASE_STATE), use_merkle=True)
        # Different algorithms → different hashes (almost certainly)
        assert sm_sha.compute_state_hash() != sm_mkl.compute_state_hash()

    def test_merkle_root_changes_with_state(self):
        sm = StateManager(copy.deepcopy(BASE_STATE), use_merkle=True)
        h1 = sm.compute_state_hash()
        sm.apply_transaction({"type": "mint", "to": "A", "amount": 1})
        h2 = sm.compute_state_hash()
        assert h1 != h2

    def test_build_merkle_tree_single_leaf(self):
        import hashlib
        leaf = hashlib.sha256(b"a:100").hexdigest()
        tree = build_merkle_tree([leaf])
        assert len(tree) == 1
        assert tree[0][0] == leaf

    def test_build_merkle_tree_two_leaves(self):
        leaves = ["aaa", "bbb"]
        tree = build_merkle_tree(leaves)
        # Level 0: leaves, Level 1: root
        assert len(tree) == 2
        assert len(tree[1]) == 1

    def test_merkle_root_empty(self):
        root = merkle_root([])
        assert isinstance(root, str)
        assert len(root) == 64
