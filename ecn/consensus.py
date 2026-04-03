"""
consensus.py
------------
Result-based consensus for the Executable Consensus Network.

Algorithm:
- Collect state hashes reported by every node after executing a transaction.
- The hash with the most votes wins (majority / plurality).
- Any node whose hash differs from the winning hash is flagged as faulty.

This is intentionally simple (plurality voting) so the prototype stays clear.
For production use, a threshold such as ⌊(n-1)/3⌋ Byzantine fault tolerance
could be layered on top.
"""

from collections import Counter
from typing import Dict, List, Optional, Tuple

from ecn.node import NodeResult


# ---------------------------------------------------------------------------
# ConsensusResult
# ---------------------------------------------------------------------------

class ConsensusResult:
    """
    Output of a single consensus round.

    Attributes
    ----------
    agreed_hash : str or None
        The hash that achieved plurality.  ``None`` if no results were
        provided.
    vote_counts : dict
        ``{hash: vote_count}`` mapping for all observed hashes.
    honest_nodes : list[str]
        Node IDs whose reported hash matches ``agreed_hash``.
    faulty_nodes : list[str]
        Node IDs whose reported hash diverges from ``agreed_hash``.
    total_nodes : int
    consensus_reached : bool
        ``True`` when ``agreed_hash`` received a strict majority (> 50 %).
    """

    def __init__(
        self,
        agreed_hash: Optional[str],
        vote_counts: Dict[str, int],
        honest_nodes: List[str],
        faulty_nodes: List[str],
    ) -> None:
        self.agreed_hash = agreed_hash
        self.vote_counts = vote_counts
        self.honest_nodes = honest_nodes
        self.faulty_nodes = faulty_nodes
        self.total_nodes = len(honest_nodes) + len(faulty_nodes)
        # Strict majority: more than half of all nodes agree
        if agreed_hash is not None and self.total_nodes > 0:
            self.consensus_reached = (
                vote_counts.get(agreed_hash, 0) > self.total_nodes // 2
            )
        else:
            self.consensus_reached = False

    def __repr__(self) -> str:
        return (
            f"ConsensusResult("
            f"hash={self.agreed_hash[:16] if self.agreed_hash else None}..., "
            f"faulty={self.faulty_nodes}, "
            f"reached={self.consensus_reached})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_consensus(results: List[NodeResult]) -> ConsensusResult:
    """
    Perform a consensus round over a list of node results.

    Parameters
    ----------
    results : list[NodeResult]
        One result per node, produced after executing the same transaction.

    Returns
    -------
    ConsensusResult
    """
    if not results:
        return ConsensusResult(
            agreed_hash=None,
            vote_counts={},
            honest_nodes=[],
            faulty_nodes=[],
        )

    # Count votes per hash
    vote_counts: Dict[str, int] = Counter(r.state_hash for r in results)

    # Winning hash = most votes (ties broken deterministically by hash value)
    agreed_hash = _select_winner(vote_counts)

    honest_nodes = [r.node_id for r in results if r.state_hash == agreed_hash]
    faulty_nodes = [r.node_id for r in results if r.state_hash != agreed_hash]

    return ConsensusResult(
        agreed_hash=agreed_hash,
        vote_counts=vote_counts,
        honest_nodes=honest_nodes,
        faulty_nodes=faulty_nodes,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _select_winner(vote_counts: Dict[str, int]) -> str:
    """
    Select the hash with the most votes.

    Ties are broken by choosing the lexicographically smallest hash so that
    the selection is fully deterministic regardless of dict insertion order.
    """
    max_votes = max(vote_counts.values())
    candidates = [h for h, v in vote_counts.items() if v == max_votes]
    return min(candidates)  # deterministic tie-breaking
