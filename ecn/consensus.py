"""
consensus.py
------------
Result-based consensus for the Executable Consensus Network.

Algorithm:
- Collect state hashes reported by every node after executing a transaction.
- Optionally verify each result's Ed25519 signature before counting votes.
- The hash with the most votes wins (majority / plurality).
- Any node whose hash differs from the winning hash is flagged as faulty.
- Any node whose signature fails verification is flagged as faulty immediately.

Quorum threshold
----------------
The ``quorum_threshold`` parameter (0 < threshold ≤ 1.0, default 0.51) sets
the minimum fraction of *verified* responses that must agree on the winning
hash for ``ConsensusResult.consensus_reached`` to be ``True``.

BFT guarantees with configurable quorum
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
With *n* total nodes and threshold *t*:
- The network tolerates up to ``floor(n * (1 - t))`` faulty or non-responding
  nodes while still reaching consensus.
- Classical BFT (⌊(n-1)/3⌋ Byzantine tolerance) corresponds to t ≈ 0.67.
- The default (t = 0.51) tolerates up to ⌊n/2⌋ - 1 failures.

Examples:
  5 nodes, t=0.51 → need 3/5 agreement, tolerates 2 failures
  5 nodes, t=0.67 → need 4/5 agreement, tolerates 1 failure
  7 nodes, t=0.67 → need 5/7 agreement, tolerates 2 failures

This is intentionally simple (plurality voting) so the prototype stays clear.
"""

from collections import Counter
from typing import Dict, List, Optional

from ecn.node import NodeResult


# Default quorum threshold — strict majority (just over 50%)
DEFAULT_QUORUM_THRESHOLD = 0.51


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
        Node IDs whose reported hash diverges from ``agreed_hash`` OR
        whose signature failed verification.
    total_nodes : int
    consensus_reached : bool
        ``True`` when ``agreed_hash`` received votes meeting or exceeding
        ``quorum_threshold`` (default: strict majority > 50%).
    invalid_sig_nodes : list[str]
        Subset of faulty_nodes that were rejected due to bad signatures.
    quorum_threshold : float
        The threshold used for this round (fraction of verified responders).
    """

    def __init__(
        self,
        agreed_hash: Optional[str],
        vote_counts: Dict[str, int],
        honest_nodes: List[str],
        faulty_nodes: List[str],
        invalid_sig_nodes: Optional[List[str]] = None,
        quorum_threshold: float = DEFAULT_QUORUM_THRESHOLD,
    ) -> None:
        self.agreed_hash = agreed_hash
        self.vote_counts = vote_counts
        self.honest_nodes = honest_nodes
        self.faulty_nodes = faulty_nodes
        self.invalid_sig_nodes = invalid_sig_nodes or []
        self.total_nodes = len(honest_nodes) + len(faulty_nodes)
        self.quorum_threshold = quorum_threshold
        # Quorum check: agreed_hash votes / total verified responders >= threshold
        total_verified = sum(vote_counts.values()) if vote_counts else 0
        if agreed_hash is not None and total_verified > 0:
            agree_fraction = vote_counts.get(agreed_hash, 0) / total_verified
            self.consensus_reached = agree_fraction >= quorum_threshold
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

def run_consensus(
    results: List[NodeResult],
    public_keys: Optional[Dict[str, object]] = None,
    quorum_threshold: float = DEFAULT_QUORUM_THRESHOLD,
) -> ConsensusResult:
    """
    Perform a consensus round over a list of node results.

    Parameters
    ----------
    results : list[NodeResult]
        One result per node, produced after executing the same transaction.
    public_keys : dict[str, Ed25519PublicKey], optional
        Mapping of ``node_id -> Ed25519PublicKey``.  When provided, each
        result's signature is verified before it is counted.  Results with
        missing or invalid signatures are immediately flagged as faulty and
        excluded from the vote tally.
    quorum_threshold : float
        Minimum fraction of verified responders that must agree on the
        winning hash for consensus to be reached.  Defaults to 0.51 (strict
        majority).  Use 0.67 for classical 1/3 BFT tolerance.

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
            quorum_threshold=quorum_threshold,
        )

    # Step 1: signature verification (when keys are supplied)
    invalid_sig_nodes: List[str] = []
    verified_results: List[NodeResult] = []

    if public_keys:
        from ecn.crypto import verify_result
        for r in results:
            pk = public_keys.get(r.node_id)
            if pk is None:
                # No known key for this node — treat as invalid
                invalid_sig_nodes.append(r.node_id)
                continue
            if r.signature is None or not verify_result(
                r.node_id, r.state_hash, r.signature, pk
            ):
                invalid_sig_nodes.append(r.node_id)
                continue
            verified_results.append(r)
    else:
        verified_results = list(results)

    if not verified_results:
        # All nodes failed signature verification
        return ConsensusResult(
            agreed_hash=None,
            vote_counts={},
            honest_nodes=[],
            faulty_nodes=[r.node_id for r in results],
            invalid_sig_nodes=invalid_sig_nodes,
            quorum_threshold=quorum_threshold,
        )

    # Step 2: count votes per hash (only verified results)
    vote_counts: Dict[str, int] = Counter(r.state_hash for r in verified_results)

    # Winning hash = most votes (ties broken deterministically by hash value)
    agreed_hash = _select_winner(vote_counts)

    honest_nodes = [r.node_id for r in verified_results if r.state_hash == agreed_hash]
    hash_faulty = [r.node_id for r in verified_results if r.state_hash != agreed_hash]
    all_faulty = hash_faulty + invalid_sig_nodes

    return ConsensusResult(
        agreed_hash=agreed_hash,
        vote_counts=vote_counts,
        honest_nodes=honest_nodes,
        faulty_nodes=all_faulty,
        invalid_sig_nodes=invalid_sig_nodes,
        quorum_threshold=quorum_threshold,
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
