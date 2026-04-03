"""
network.py
----------
Simulates the distributed ECN network.

Responsibilities:
- Hold a collection of nodes
- Broadcast a transaction to all nodes simultaneously (simulated)
- Collect execution results
- Run a consensus round
- Print per-node and consensus output
"""

from typing import List, Optional, Tuple

from ecn.node import Node, NodeResult
from ecn.consensus import ConsensusResult, run_consensus
from ecn.execution_engine import Transaction, State


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class Network:
    """
    Simulated ECN network of nodes.

    Parameters
    ----------
    nodes : list[Node]
        Pre-constructed node instances to include in the network.
    verbose : bool
        When *True*, ``broadcast()`` prints results to stdout.
    """

    def __init__(self, nodes: List[Node], verbose: bool = True) -> None:
        self._nodes = list(nodes)
        self.verbose = verbose
        # History of (transaction, [NodeResult], ConsensusResult) tuples
        self._history: List[Tuple[Transaction, List[NodeResult], ConsensusResult]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_node(self, node: Node) -> None:
        """Add a node to the network."""
        self._nodes.append(node)

    def broadcast(
        self, tx: Transaction
    ) -> Tuple[List[NodeResult], ConsensusResult]:
        """
        Broadcast *tx* to every node, collect results, and run consensus.

        Returns
        -------
        (results, consensus_result)
        """
        results: List[NodeResult] = []
        for node in self._nodes:
            result = node.execute_transaction(tx)
            results.append(result)

        consensus_result = run_consensus(results)

        self._history.append((tx, results, consensus_result))

        if self.verbose:
            self._print_round(tx, results, consensus_result)

        return results, consensus_result

    def get_history(self):
        """Return the full broadcast history."""
        return list(self._history)

    def node_count(self) -> int:
        return len(self._nodes)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _print_round(
        tx: Transaction,
        results: List[NodeResult],
        cr: ConsensusResult,
    ) -> None:
        """Pretty-print the round output to stdout."""
        print("\n" + "=" * 60)
        print(f"Transaction: {tx}")
        print("-" * 60)
        for r in results:
            flag = "  ← FAULT DETECTED" if r.node_id in cr.faulty_nodes else ""
            # Print execution trace diff if available
            diff = r.trace_entry.get("diff", {})
            diff_str = ""
            if diff:
                parts = []
                for acc, (old_v, new_v) in diff.items():
                    parts.append(f"{acc}: {old_v}→{new_v}")
                diff_str = "  diff=[" + ", ".join(parts) + "]"
            print(
                f"  {r.node_id:<12} → hash: {r.state_hash}{diff_str}{flag}"
            )
        print("-" * 60)
        if cr.consensus_reached:
            print(f"Consensus    → {cr.agreed_hash}")
        else:
            print(f"Consensus    → FAILED (no strict majority)")
        if cr.faulty_nodes:
            print(f"Faulty nodes → {cr.faulty_nodes}")
        else:
            print("Faulty nodes → none")
        print("=" * 60)
