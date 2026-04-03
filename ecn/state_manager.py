"""
state_manager.py
----------------
Manages global state for an ECN node.

Responsibilities:
- Maintain the current world state
- Apply state transitions via the execution engine
- Compute a deterministic SHA-256 hash of the state (for consensus)
- Optionally build a Merkle tree over individual account balances
"""

import hashlib
import json
import copy
from typing import Any, Dict, List, Optional, Tuple

from ecn.execution_engine import execute, State, Transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> bytes:
    """
    Produce a deterministic, canonical JSON byte string.

    Keys are sorted recursively so that dict order never affects the hash.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Merkle tree helpers (optional extension)
# ---------------------------------------------------------------------------

def build_merkle_tree(leaves: List[str]) -> List[List[str]]:
    """
    Build a binary Merkle tree from a list of hex-encoded leaf hashes.

    Returns a list of levels, where ``levels[0]`` contains the leaves and
    ``levels[-1]`` contains the single root hash.
    """
    if not leaves:
        return [[_sha256(b"")]]

    level: List[str] = list(leaves)
    levels: List[List[str]] = [level]

    while len(level) > 1:
        next_level: List[str] = []
        for i in range(0, len(level), 2):
            left = level[i]
            # If there is an odd number of nodes, duplicate the last one.
            right = level[i + 1] if i + 1 < len(level) else left
            combined = _sha256((left + right).encode("utf-8"))
            next_level.append(combined)
        level = next_level
        levels.append(level)

    return levels


def merkle_root(leaves: List[str]) -> str:
    """Return the Merkle root hash for the given list of leaf hashes."""
    tree = build_merkle_tree(leaves)
    return tree[-1][0]


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    """
    Encapsulates the world state and provides mutation / hashing APIs.

    Parameters
    ----------
    initial_state : State, optional
        Starting state.  Defaults to ``{"balances": {}}``.
    use_merkle : bool
        When *True*, ``compute_state_hash()`` returns the Merkle root over
        individual account hashes.  When *False* (default), a single
        SHA-256 hash of the full canonical JSON is returned.
    """

    def __init__(
        self,
        initial_state: Optional[State] = None,
        use_merkle: bool = False,
    ) -> None:
        self._state: State = (
            copy.deepcopy(initial_state)
            if initial_state is not None
            else {"balances": {}}
        )
        self._use_merkle = use_merkle
        # Execution trace: list of (tx, old_hash, new_hash, diff) tuples
        self._trace: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_state(self) -> State:
        """Return a deep copy of the current state (read-only view)."""
        return copy.deepcopy(self._state)

    def apply_transaction(self, tx: Transaction) -> State:
        """
        Execute *tx* against the current state and update internal state.

        Also records an entry in the execution trace.

        Returns
        -------
        State
            The new state after applying the transaction.
        """
        old_state = copy.deepcopy(self._state)
        old_hash = self.compute_state_hash()

        new_state = execute(self._state, tx)
        self._state = new_state

        new_hash = self.compute_state_hash()
        diff = self._compute_diff(old_state, new_state)

        self._trace.append(
            {
                "tx": tx,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "diff": diff,
            }
        )

        return copy.deepcopy(self._state)

    def compute_state_hash(self) -> str:
        """
        Return a deterministic hash of the current state.

        If ``use_merkle=True`` the hash is the Merkle root over per-account
        leaf hashes; otherwise it is the SHA-256 of the canonical JSON.
        """
        if self._use_merkle:
            return self._merkle_hash()
        return _sha256(_canonical_json(self._state))

    def get_trace(self) -> List[Dict[str, Any]]:
        """Return the full execution trace (read-only copy)."""
        return list(self._trace)

    def reset(self, new_state: Optional[State] = None) -> None:
        """Reset state (and trace) to *new_state* or to the empty default."""
        self._state = (
            copy.deepcopy(new_state)
            if new_state is not None
            else {"balances": {}}
        )
        self._trace = []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _merkle_hash(self) -> str:
        """Compute a Merkle root over sorted (account, balance) pairs."""
        balances = self._state.get("balances", {})
        # Sort by account name for determinism
        leaves = [
            _sha256(f"{account}:{balance}".encode("utf-8"))
            for account, balance in sorted(balances.items())
        ]
        return merkle_root(leaves)

    @staticmethod
    def _compute_diff(
        old_state: State, new_state: State
    ) -> Dict[str, Tuple[int, int]]:
        """
        Return a diff of account balances between old and new state.

        Returns
        -------
        dict
            ``{account: (old_balance, new_balance)}`` for every account
            whose balance changed.
        """
        old_balances = old_state.get("balances", {})
        new_balances = new_state.get("balances", {})
        all_accounts = set(old_balances) | set(new_balances)
        diff: Dict[str, Tuple[int, int]] = {}
        for acc in sorted(all_accounts):
            old_val = old_balances.get(acc, 0)
            new_val = new_balances.get(acc, 0)
            if old_val != new_val:
                diff[acc] = (old_val, new_val)
        return diff
