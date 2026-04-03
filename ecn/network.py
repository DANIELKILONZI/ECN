"""
network.py
----------
Simulates the distributed ECN network.

Responsibilities:
- Hold a collection of nodes
- Broadcast a transaction to all nodes simultaneously (simulated)
- Collect execution results (optionally with cryptographic signatures)
- Run a consensus round (optionally with signature verification)
- Print per-node and consensus output
"""

from typing import Dict, List, Optional, Tuple

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
    public_keys : dict[str, Ed25519PublicKey], optional
        Mapping of ``node_id -> public_key``.  When provided, consensus
        verifies each result's signature before counting votes.
    """

    def __init__(
        self,
        nodes: List[Node],
        verbose: bool = True,
        public_keys: Optional[Dict[str, object]] = None,
    ) -> None:
        self._nodes = list(nodes)
        self.verbose = verbose
        self._public_keys = public_keys or {}
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

        consensus_result = run_consensus(
            results,
            public_keys=self._public_keys if self._public_keys else None,
        )

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
            flags = []
            if r.node_id in cr.invalid_sig_nodes:
                flags.append("INVALID SIG")
            elif r.node_id in cr.faulty_nodes:
                flags.append("FAULT DETECTED")
            flag_str = "  ← " + ", ".join(flags) if flags else ""

            # Signature status indicator
            sig_str = ""
            if r.signature is not None:
                sig_str = f"  sig={r.signature[:12]}..."

            # Print execution trace diff if available
            diff = r.trace_entry.get("diff", {})
            diff_str = ""
            if diff:
                parts = []
                for acc, (old_v, new_v) in diff.items():
                    parts.append(f"{acc}: {old_v}→{new_v}")
                diff_str = "  diff=[" + ", ".join(parts) + "]"
            print(
                f"  {r.node_id:<12} → hash: {r.state_hash}{sig_str}{diff_str}{flag_str}"
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
