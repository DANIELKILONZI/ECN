"""
node_health.py
--------------
Per-node health tracker for adaptive quorum participation.

Node lifecycle
--------------
HEALTHY → SUSPECT → EXCLUDED → REHABILITATING → HEALTHY

Rules
-----
- 3 consecutive failures → SUSPECT
- 5 consecutive failures → EXCLUDED
- After EXCLUDED: next success within rehabilitation window → REHABILITATING
- After REHABILITATING: next success → HEALTHY
- Any success while HEALTHY or SUSPECT resets consecutive_failures to 0

This tracker is consumed by P2PNetwork to:
  - Skip EXCLUDED nodes in broadcast gather (Byzantine timing defense)
  - Count EXCLUDED nodes in the quorum denominator so InsufficientQuorumError
    fires correctly when too many nodes are lost

Byzantine timing defense
------------------------
"Silent killer" nodes respond just under READ_TIMEOUT — they slow every round
without being obviously wrong.  Latency-aware exclusion (p95 tracking) surfaces
these nodes before they destabilise quorum.

Usage
-----
::

    from ecn.node_health import NodeHealthRegistry

    registry = NodeHealthRegistry()
    registry.record_success("Node-1", latency_ms=12)
    registry.record_failure("Node-2")
    health = registry.get("Node-2")
    print(health.state)  # NodeState.HEALTHY (1 failure so far)
"""

from __future__ import annotations

import collections
import datetime
import enum
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class NodeState(str, enum.Enum):
    """All valid health states for an ECN node."""
    HEALTHY        = "HEALTHY"
    SUSPECT        = "SUSPECT"
    EXCLUDED       = "EXCLUDED"
    REHABILITATING = "REHABILITATING"


# Thresholds
_SUSPECT_THRESHOLD   = 3   # consecutive failures before SUSPECT
_EXCLUDED_THRESHOLD  = 5   # consecutive failures before EXCLUDED
_REHAB_WINDOW_SECS   = 60  # seconds excluded before first success → REHABILITATING
_LATENCY_WINDOW      = 20  # number of recent latency samples for p95


# ---------------------------------------------------------------------------
# NodeHealth dataclass
# ---------------------------------------------------------------------------

@dataclass
class NodeHealth:
    """Current health snapshot for a single node."""
    node_id: str
    state: NodeState = NodeState.HEALTHY
    consecutive_failures: int = 0
    last_success_at: Optional[str] = None
    excluded_since: Optional[str] = None
    # Rolling window of recent latency samples (milliseconds)
    _latency_samples: Deque[float] = field(
        default_factory=lambda: collections.deque(maxlen=_LATENCY_WINDOW),
        repr=False,
    )

    @property
    def p95_latency_ms(self) -> Optional[float]:
        """95th-percentile latency over the rolling window, or None if no data."""
        samples = list(self._latency_samples)
        if not samples:
            return None
        sorted_samples = sorted(samples)
        idx = int(len(sorted_samples) * 0.95)
        idx = min(idx, len(sorted_samples) - 1)
        return sorted_samples[idx]

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at,
            "excluded_since": self.excluded_since,
            "p95_latency_ms": self.p95_latency_ms,
        }


# ---------------------------------------------------------------------------
# NodeHealthRegistry
# ---------------------------------------------------------------------------

class NodeHealthRegistry:
    """
    Thread-safe (single-threaded asyncio) per-node health tracker.

    Maintains a ``NodeHealth`` record for every known node and advances
    nodes through the health lifecycle based on observed successes and
    failures.
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, NodeHealth] = {}

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record_success(self, node_id: str, latency_ms: float = 0.0) -> NodeHealth:
        """
        Record a successful response from *node_id*.

        - Resets ``consecutive_failures`` to 0.
        - Updates p95 latency window.
        - Advances EXCLUDED → REHABILITATING → HEALTHY.
        """
        health = self._get_or_create(node_id)
        now_str = _utcnow()

        # Add latency sample
        health._latency_samples.append(latency_ms)
        health.consecutive_failures = 0
        health.last_success_at = now_str

        if health.state == NodeState.EXCLUDED:
            # Must wait _REHAB_WINDOW_SECS before being rehabilitated
            if health.excluded_since and _seconds_since(health.excluded_since) >= _REHAB_WINDOW_SECS:
                health.state = NodeState.REHABILITATING
                health.excluded_since = None
            # else: still excluded — success noted but state unchanged

        elif health.state == NodeState.REHABILITATING:
            health.state = NodeState.HEALTHY

        elif health.state in (NodeState.SUSPECT, NodeState.HEALTHY):
            health.state = NodeState.HEALTHY

        return health

    def record_failure(self, node_id: str) -> NodeHealth:
        """
        Record a failed response from *node_id*.

        - Increments ``consecutive_failures``.
        - Advances HEALTHY → SUSPECT → EXCLUDED based on thresholds.
        """
        health = self._get_or_create(node_id)
        health.consecutive_failures += 1
        now_str = _utcnow()

        if health.state == NodeState.EXCLUDED:
            # Already excluded — nothing more to do
            pass
        elif health.consecutive_failures >= _EXCLUDED_THRESHOLD:
            if health.state != NodeState.EXCLUDED:
                health.state = NodeState.EXCLUDED
                health.excluded_since = now_str
        elif health.consecutive_failures >= _SUSPECT_THRESHOLD:
            if health.state not in (NodeState.SUSPECT, NodeState.EXCLUDED):
                health.state = NodeState.SUSPECT

        # Rehabilitating node that fails again → back to SUSPECT
        elif health.state == NodeState.REHABILITATING:
            health.state = NodeState.SUSPECT

        return health

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, node_id: str) -> NodeHealth:
        """Return the health record for *node_id* (creates HEALTHY if new)."""
        return self._get_or_create(node_id)

    def all_nodes(self) -> Dict[str, NodeHealth]:
        """Return a copy of the full registry."""
        return dict(self._nodes)

    def active_nodes(self) -> list[str]:
        """Return node IDs that are NOT EXCLUDED (eligible for broadcast)."""
        return [
            nid for nid, h in self._nodes.items()
            if h.state != NodeState.EXCLUDED
        ]

    def excluded_nodes(self) -> list[str]:
        """Return node IDs currently EXCLUDED from broadcast."""
        return [
            nid for nid, h in self._nodes.items()
            if h.state == NodeState.EXCLUDED
        ]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _get_or_create(self, node_id: str) -> NodeHealth:
        if node_id not in self._nodes:
            self._nodes[node_id] = NodeHealth(node_id=node_id)
        return self._nodes[node_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _seconds_since(iso_str: str) -> float:
    try:
        then = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - then).total_seconds()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Global singleton (initialised alongside P2PNetwork)
# ---------------------------------------------------------------------------

_registry: Optional[NodeHealthRegistry] = None


def get_health_registry() -> NodeHealthRegistry:
    """Return the global NodeHealthRegistry (raises if not initialised)."""
    if _registry is None:
        raise RuntimeError("NodeHealthRegistry not initialised")
    return _registry


def init_health_registry() -> NodeHealthRegistry:
    """Initialise and return the global NodeHealthRegistry."""
    global _registry
    _registry = NodeHealthRegistry()
    return _registry
