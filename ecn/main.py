"""
main.py
-------
ECN demo entry point.

Scenario 1 — Normal Execution
    5 honest nodes process a valid transfer transaction.
    All nodes should agree on the same state hash.

Scenario 2 — Fault Injection
    4 honest nodes + 1 malicious node process the same transaction.
    The consensus algorithm detects and flags the faulty node.

Scenario 3 — Multiple Transaction Types
    Demonstrates mint, transfer, and burn transactions in sequence.
"""

from ecn.node import Node
from ecn.network import Network


# ---------------------------------------------------------------------------
# Shared initial state
# ---------------------------------------------------------------------------

INITIAL_STATE = {
    "balances": {
        "A": 1000,
        "B": 500,
        "C": 250,
    }
}

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_nodes(count: int, malicious_ids=None, use_merkle: bool = False):
    """
    Create *count* nodes, optionally marking some as malicious.

    Parameters
    ----------
    count : int
    malicious_ids : set[int], optional
        Zero-based indices of nodes to mark as malicious.
    use_merkle : bool
        Enable Merkle-tree hashing on all nodes.
    """
    if malicious_ids is None:
        malicious_ids = set()
    nodes = []
    for i in range(count):
        node_id = f"Node-{i + 1}"
        is_malicious = i in malicious_ids
        node = Node(
            node_id=node_id,
            initial_state=INITIAL_STATE,
            malicious=is_malicious,
            use_merkle=use_merkle,
        )
        nodes.append(node)
    return nodes


# ---------------------------------------------------------------------------
# Scenario 1 — Normal Execution (5 honest nodes)
# ---------------------------------------------------------------------------

def scenario_1():
    print("\n" + "#" * 60)
    print("# SCENARIO 1 — Normal Execution (5 honest nodes)")
    print("#" * 60)

    nodes = make_nodes(5)
    network = Network(nodes, verbose=True)

    tx = {"type": "transfer", "from": "A", "to": "B", "amount": 100}
    results, cr = network.broadcast(tx)

    assert cr.consensus_reached, "Consensus should be reached in Scenario 1"
    assert len(cr.faulty_nodes) == 0, "No faulty nodes expected in Scenario 1"
    print("\n✅  Scenario 1 passed: all nodes agreed, no faults detected.")


# ---------------------------------------------------------------------------
# Scenario 2 — Fault Injection (1 malicious node)
# ---------------------------------------------------------------------------

def scenario_2():
    print("\n" + "#" * 60)
    print("# SCENARIO 2 — Fault Injection (1 malicious node out of 5)")
    print("#" * 60)

    # Node-5 (index 4) is malicious
    nodes = make_nodes(5, malicious_ids={4})
    network = Network(nodes, verbose=True)

    tx = {"type": "transfer", "from": "A", "to": "B", "amount": 200}
    results, cr = network.broadcast(tx)

    assert cr.consensus_reached, "Consensus should still be reached (4 vs 1)"
    assert "Node-5" in cr.faulty_nodes, "Node-5 should be flagged as faulty"
    print("\n✅  Scenario 2 passed: consensus reached, Node-5 flagged as faulty.")


# ---------------------------------------------------------------------------
# Scenario 3 — Multiple Transaction Types
# ---------------------------------------------------------------------------

def scenario_3():
    print("\n" + "#" * 60)
    print("# SCENARIO 3 — Multiple Transaction Types (mint/transfer/burn)")
    print("#" * 60)

    nodes = make_nodes(3, use_merkle=True)
    network = Network(nodes, verbose=True)

    transactions = [
        {"type": "mint",     "to": "A",   "amount": 500},
        {"type": "transfer", "from": "A", "to": "C", "amount": 300},
        {"type": "burn",     "from": "B", "amount": 100},
    ]

    for tx in transactions:
        results, cr = network.broadcast(tx)
        assert cr.consensus_reached, f"Consensus failed for tx: {tx}"
        assert len(cr.faulty_nodes) == 0, f"Unexpected fault for tx: {tx}"

    # Print execution trace for Node-1
    node_1 = nodes[0]
    print("\n--- Execution trace for Node-1 ---")
    for entry in node_1.get_trace():
        diff_parts = [
            f"{acc}: {old}→{new}"
            for acc, (old, new) in entry["diff"].items()
        ]
        print(
            f"  tx={entry['tx']}  "
            f"hash_before={entry['old_hash'][:16]}...  "
            f"hash_after={entry['new_hash'][:16]}...  "
            f"diff=[{', '.join(diff_parts)}]"
        )

    print("\n✅  Scenario 3 passed: all transaction types processed correctly.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    scenario_1()
    scenario_2()
    scenario_3()
    print("\n🎉  All ECN scenarios completed successfully.")
