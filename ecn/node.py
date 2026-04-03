"""
node.py
-------
Represents a single participant node in the Executable Consensus Network.

Each node:
- Holds its own independent copy of the world state.
- Executes transactions independently and deterministically.
- Produces a state hash after each execution for consensus comparison.
- Optionally operates in "malicious mode" to simulate Byzantine faults.
"""

import copy
from typing import Any, Dict, Optional

from ecn.state_manager import StateManager
from ecn.execution_engine import Transaction, State


# ---------------------------------------------------------------------------
# NodeResult
# ---------------------------------------------------------------------------

class NodeResult:
    """
    Immutable record of a node's execution output for a single transaction.

    Attributes
    ----------
    node_id : str
    state_hash : str
        SHA-256 (or Merkle root) of the resulting state.
    result_state : State
        Full resulting state (deep copy).
    trace_entry : dict
        Execution trace entry recorded by StateManager.
    """

    __slots__ = ("node_id", "state_hash", "result_state", "trace_entry")

    def __init__(
        self,
        node_id: str,
        state_hash: str,
        result_state: State,
        trace_entry: Dict[str, Any],
    ) -> None:
        self.node_id = node_id
        self.state_hash = state_hash
        self.result_state = result_state
        self.trace_entry = trace_entry

    def __repr__(self) -> str:
        return (
            f"NodeResult(node_id={self.node_id!r}, "
            f"state_hash={self.state_hash[:16]}...)"
        )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class Node:
    """
    A single ECN network node.

    Parameters
    ----------
    node_id : str
        Human-readable identifier (e.g. ``"Node-1"``).
    initial_state : State
        The starting world state (will be deep-copied).
    malicious : bool
        When *True* the node tampers with its state hash to simulate a
        Byzantine fault (the underlying state is still computed correctly,
        but the reported hash is artificially corrupted).
    use_merkle : bool
        Forwarded to the underlying ``StateManager``.
    """

    def __init__(
        self,
        node_id: str,
        initial_state: State,
        malicious: bool = False,
        use_merkle: bool = False,
    ) -> None:
        self.node_id = node_id
        self.malicious = malicious
        self._manager = StateManager(
            initial_state=initial_state,
            use_merkle=use_merkle,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_transaction(self, tx: Transaction) -> NodeResult:
        """
        Apply *tx* to this node's state.

        Returns
        -------
        NodeResult
            Contains the resulting state hash and full state.  If the node
            is malicious the reported hash is deliberately corrupted.
        """
        new_state = self._manager.apply_transaction(tx)
        true_hash = self._manager.compute_state_hash()

        # Malicious nodes report a corrupted hash
        reported_hash = self._corrupt_hash(true_hash) if self.malicious else true_hash

        trace = self._manager.get_trace()[-1]  # last trace entry

        return NodeResult(
            node_id=self.node_id,
            state_hash=reported_hash,
            result_state=new_state,
            trace_entry=trace,
        )

    def get_state(self) -> State:
        """Return the current state (read-only deep copy)."""
        return self._manager.get_state()

    def get_state_hash(self) -> str:
        """Return the current state hash."""
        return self._manager.compute_state_hash()

    def get_trace(self):
        """Return the full execution trace."""
        return self._manager.get_trace()

    def reset(self, new_state: Optional[State] = None) -> None:
        """Reset this node's state."""
        self._manager.reset(new_state)

    def __repr__(self) -> str:
        mode = "MALICIOUS" if self.malicious else "honest"
        return f"Node(id={self.node_id!r}, mode={mode})"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _corrupt_hash(true_hash: str) -> str:
        """
        Return a deterministically corrupted hash.

        We flip the last 8 hex characters so the corruption is reproducible
        (important for deterministic testing) but clearly diverges from the
        honest result.
        """
        return true_hash[:-8] + "DEADBEEF"
