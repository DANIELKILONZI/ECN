"""
transaction.py
--------------
ECN Core Transaction Lifecycle — the constitutional layer.

Defines what a "transaction" is, what stages it passes through, what must
be true before moving forward, what triggers billing, and what triggers
audit finalization.

State machine
-------------
INITIATED → EXECUTING → PROPOSED → CONSENSUS_PENDING → FINALIZED → BILLED → REPLICATED

Rules
-----
- ``transition()`` enforces valid transitions; raises ``InvalidTransitionError``
  on any illegal move.
- Billing is triggered (externally) when a record reaches ``BILLED``.
- Audit finalization is triggered (externally) when a record reaches ``FINALIZED``.

Usage
-----
::

    from ecn.transaction import TransactionRecord, TxState, transition, new_record

    rec = new_record(tx_id="abc-123", tenant_id="acme", node_count=5)
    rec = transition(rec, TxState.EXECUTING)
    rec = transition(rec, TxState.PROPOSED)
    ...
    rec = transition(rec, TxState.FINALIZED)   # triggers audit
    rec = transition(rec, TxState.BILLED)      # triggers billing
    rec = transition(rec, TxState.REPLICATED)
"""

from __future__ import annotations

import datetime
import enum
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class TxState(str, enum.Enum):
    """All valid lifecycle states for an ECN transaction."""
    INITIATED         = "INITIATED"
    EXECUTING         = "EXECUTING"
    PROPOSED          = "PROPOSED"
    CONSENSUS_PENDING = "CONSENSUS_PENDING"
    FINALIZED         = "FINALIZED"
    BILLED            = "BILLED"
    REPLICATED        = "REPLICATED"


# Valid forward transitions — any other move raises InvalidTransitionError
_VALID_TRANSITIONS: dict[TxState, frozenset[TxState]] = {
    TxState.INITIATED:         frozenset({TxState.EXECUTING}),
    TxState.EXECUTING:         frozenset({TxState.PROPOSED}),
    TxState.PROPOSED:          frozenset({TxState.CONSENSUS_PENDING}),
    TxState.CONSENSUS_PENDING: frozenset({TxState.FINALIZED}),
    TxState.FINALIZED:         frozenset({TxState.BILLED}),
    TxState.BILLED:            frozenset({TxState.REPLICATED}),
    TxState.REPLICATED:        frozenset(),  # terminal state
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InvalidTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""


# ---------------------------------------------------------------------------
# TransactionRecord
# ---------------------------------------------------------------------------

@dataclass
class TransactionRecord:
    """
    Immutable-by-convention record that tracks a single ECN transaction
    through its full lifecycle.

    Fields
    ------
    tx_id : str
        Unique identifier for this transaction (UUID4).
    tenant_id : str
        Owning tenant.
    state : TxState
        Current lifecycle state.
    node_count : int
        Number of nodes that participated in consensus.
    consensus_reached : bool
        Whether consensus was achieved.
    fault_count : int
        Number of faulty nodes detected.
    duration_ms : int
        Wall-clock time from INITIATED to FINALIZED (milliseconds).
        Zero until FINALIZED.
    billed_amount : float
        Amount billed in USD for this transaction (computed at BILLED).
        Zero until BILLED.
    created_at : str
        ISO-8601 UTC timestamp of record creation.
    finalized_at : str or None
        ISO-8601 UTC timestamp of FINALIZED transition.
    """
    tx_id: str
    tenant_id: str
    state: TxState
    node_count: int = 0
    consensus_reached: bool = False
    fault_count: int = 0
    duration_ms: int = 0
    billed_amount: float = 0.0
    created_at: str = field(default_factory=lambda: _utcnow())
    finalized_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id,
            "tenant_id": self.tenant_id,
            "state": self.state.value,
            "node_count": self.node_count,
            "consensus_reached": self.consensus_reached,
            "fault_count": self.fault_count,
            "duration_ms": self.duration_ms,
            "billed_amount": self.billed_amount,
            "created_at": self.created_at,
            "finalized_at": self.finalized_at,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def new_record(
    tenant_id: str,
    tx_id: Optional[str] = None,
    node_count: int = 0,
) -> TransactionRecord:
    """Create a new TransactionRecord in the INITIATED state."""
    return TransactionRecord(
        tx_id=tx_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        state=TxState.INITIATED,
        node_count=node_count,
    )


def transition(record: TransactionRecord, new_state: TxState) -> TransactionRecord:
    """
    Advance *record* to *new_state*, enforcing valid transitions.

    Returns a **new** TransactionRecord with the updated state (records are
    treated as immutable outside of persistence writes).

    Raises
    ------
    InvalidTransitionError
        When the requested transition is not permitted from the current state.
    """
    allowed = _VALID_TRANSITIONS.get(record.state, frozenset())
    if new_state not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition {record.state.value} → {new_state.value}. "
            f"Allowed: {[s.value for s in allowed]}"
        )

    # Build a copy with the new state (dataclasses are mutable but we treat
    # them as value objects for clarity)
    from dataclasses import replace
    updates: dict = {"state": new_state}

    if new_state == TxState.FINALIZED:
        updates["finalized_at"] = _utcnow()
        # Compute duration_ms from created_at
        try:
            created = datetime.datetime.fromisoformat(
                record.created_at.replace("Z", "+00:00")
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            updates["duration_ms"] = max(0, int((now - created).total_seconds() * 1000))
        except Exception:
            pass

    return replace(record, **updates)
