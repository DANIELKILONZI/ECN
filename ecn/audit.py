"""
audit.py
--------
Structured audit trail for the Executable Consensus Network.

Every broadcast round is recorded as an AuditEvent with:
  - ISO-8601 timestamp
  - The transaction that was submitted
  - Per-node results (node_id, state_hash, signature, fault classification)
  - Consensus outcome (agreed hash, vote counts, reached flag)
  - Detected faults (invalid sigs, hash divergence)

The AuditLog is the single source of truth for:
  - The REST API (/audit/events)
  - The CLI dashboard
  - The trust failure demo
  - Transaction replay

Usage:
    log = AuditLog()
    # pass to Network or P2PNetwork via the on_round callback
    network = Network(nodes, audit_log=log)
    ...
    events = log.get_events()            # all events
    faults = log.get_fault_events()      # only rounds with faults
    summary = log.summary()              # aggregate stats
"""

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class NodeVote:
    """A single node's vote in one consensus round."""
    node_id: str
    state_hash: str
    signature: Optional[str]
    status: str  # "honest" | "hash_fault" | "sig_fault"


@dataclass
class AuditEvent:
    """
    Immutable record of one consensus round.

    Attributes
    ----------
    round_id : int
        Monotonically increasing round counter (1-based).
    timestamp : str
        ISO-8601 UTC timestamp when the round completed.
    transaction : dict
        The transaction that was broadcast.
    votes : list[NodeVote]
        One entry per node.
    agreed_hash : str or None
        The winning hash (None if consensus failed).
    consensus_reached : bool
    honest_nodes : list[str]
    faulty_nodes : list[str]
        Nodes with hash divergence OR bad signatures.
    invalid_sig_nodes : list[str]
        Subset of faulty_nodes rejected specifically for bad signatures.
    vote_counts : dict
        ``{hash: vote_count}`` tally.
    """
    round_id: int
    timestamp: str
    transaction: Dict[str, Any]
    votes: List[NodeVote]
    agreed_hash: Optional[str]
    consensus_reached: bool
    honest_nodes: List[str]
    faulty_nodes: List[str]
    invalid_sig_nodes: List[str]
    vote_counts: Dict[str, int]

    def has_fault(self) -> bool:
        """Return True if any node was flagged faulty this round."""
        return len(self.faulty_nodes) > 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (JSON-safe)."""
        return {
            "round_id": self.round_id,
            "timestamp": self.timestamp,
            "transaction": self.transaction,
            "votes": [
                {
                    "node_id": v.node_id,
                    "state_hash": v.state_hash,
                    "signature": v.signature,
                    "status": v.status,
                }
                for v in self.votes
            ],
            "agreed_hash": self.agreed_hash,
            "consensus_reached": self.consensus_reached,
            "honest_nodes": self.honest_nodes,
            "faulty_nodes": self.faulty_nodes,
            "invalid_sig_nodes": self.invalid_sig_nodes,
            "vote_counts": self.vote_counts,
        }


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------

class AuditLog:
    """
    Thread-safe (single-threaded asyncio) append-only audit log.

    All ECN rounds are recorded here.  Consumers read via the query methods.
    """

    def __init__(self) -> None:
        self._events: List[AuditEvent] = []

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(
        self,
        transaction: Dict[str, Any],
        node_results,          # list[NodeResult]
        consensus_result,      # ConsensusResult
    ) -> AuditEvent:
        """
        Build and store an AuditEvent from raw ECN round output.

        Parameters
        ----------
        transaction : dict
        node_results : list[NodeResult]
        consensus_result : ConsensusResult

        Returns
        -------
        AuditEvent
            The newly created event (also stored internally).
        """
        round_id = len(self._events) + 1
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

        votes = []
        for r in node_results:
            if r.node_id in consensus_result.invalid_sig_nodes:
                status = "sig_fault"
            elif r.node_id in consensus_result.faulty_nodes:
                status = "hash_fault"
            else:
                status = "honest"
            votes.append(
                NodeVote(
                    node_id=r.node_id,
                    state_hash=r.state_hash,
                    signature=r.signature,
                    status=status,
                )
            )

        event = AuditEvent(
            round_id=round_id,
            timestamp=timestamp,
            transaction=dict(transaction),
            votes=votes,
            agreed_hash=consensus_result.agreed_hash,
            consensus_reached=consensus_result.consensus_reached,
            honest_nodes=list(consensus_result.honest_nodes),
            faulty_nodes=list(consensus_result.faulty_nodes),
            invalid_sig_nodes=list(consensus_result.invalid_sig_nodes),
            vote_counts=dict(consensus_result.vote_counts),
        )
        self._events.append(event)
        return event

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_events(
        self,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[AuditEvent]:
        """Return all events (or a paginated slice)."""
        sliced = self._events[offset:]
        if limit is not None:
            sliced = sliced[:limit]
        return list(sliced)

    def get_fault_events(self) -> List[AuditEvent]:
        """Return only rounds where at least one fault was detected."""
        return [e for e in self._events if e.has_fault()]

    def get_event(self, round_id: int) -> Optional[AuditEvent]:
        """Return the event for *round_id* (1-based), or None."""
        idx = round_id - 1
        if 0 <= idx < len(self._events):
            return self._events[idx]
        return None

    def summary(self) -> Dict[str, Any]:
        """Return aggregate statistics across all recorded rounds."""
        total = len(self._events)
        fault_rounds = sum(1 for e in self._events if e.has_fault())
        consensus_failures = sum(1 for e in self._events if not e.consensus_reached)
        all_faulty = []
        for e in self._events:
            all_faulty.extend(e.faulty_nodes)
        fault_counts: Dict[str, int] = {}
        for node_id in all_faulty:
            fault_counts[node_id] = fault_counts.get(node_id, 0) + 1

        return {
            "total_rounds": total,
            "fault_rounds": fault_rounds,
            "consensus_failure_rounds": consensus_failures,
            "fault_rate": round(fault_rounds / total, 4) if total else 0.0,
            "top_faulty_nodes": sorted(
                fault_counts.items(), key=lambda x: -x[1]
            ),
        }

    def __len__(self) -> int:
        return len(self._events)
