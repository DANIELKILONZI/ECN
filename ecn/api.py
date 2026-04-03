"""
api.py
------
Enterprise Integration Layer — GAP 3.

A FastAPI REST API that exposes the ECN supply-chain network as HTTP.
This is what enterprise customers integrate against: SDKs, dashboards,
and Kafka-style event consumers all talk to these endpoints.

Endpoints
---------
POST /transactions
    Submit a transaction to the ECN network.  All nodes execute it,
    consensus is run, the result is returned and logged.

GET  /network/nodes
    List all nodes in the network (id, malicious flag, public key).

GET  /network/state
    Summarise the current consensus health of the network.

GET  /audit/events
    Paginated list of all audit events (full round history).

GET  /audit/events/faults
    Only rounds where at least one fault was detected.

GET  /audit/events/{round_id}
    Single audit event by round number.

GET  /audit/summary
    Aggregate statistics (total rounds, fault rate, top faulty nodes).

GET  /audit/replay
    Replay the full transaction history as a JSON array (for dashboards /
    replay viewers).

Run with:
    python -m ecn.api               # starts on http://127.0.0.1:8000
    uvicorn ecn.api:app --reload    # dev mode
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from ecn.audit import AuditLog
from ecn.p2p_network import P2PNetwork
from ecn.use_cases.supply_chain import register_supply_chain_handlers, make_initial_state

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register domain handlers once at import time
# ---------------------------------------------------------------------------
register_supply_chain_handlers()

# ---------------------------------------------------------------------------
# Global singletons (initialised in lifespan)
# ---------------------------------------------------------------------------
_network: Optional[P2PNetwork] = None
_audit_log: Optional[AuditLog] = None

_DEFAULT_PRODUCT_IDS = ["LAPTOP-001", "PHONE-002", "TABLET-003"]
_NODE_CONFIGS = [
    {"node_id": "Warehouse"},
    {"node_id": "Shipper"},
    {"node_id": "Customs"},
    {"node_id": "Insurer"},
    {"node_id": "Retailer"},
]
_BASE_PORT = 18000


# ---------------------------------------------------------------------------
# Lifespan: start/stop the ECN network alongside the API server
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _network, _audit_log
    _audit_log = AuditLog()
    _network = await P2PNetwork.create(
        node_configs=_NODE_CONFIGS,
        initial_state=make_initial_state(_DEFAULT_PRODUCT_IDS),
        base_port=_BASE_PORT,
        verbose=False,
        audit_log=_audit_log,
    )
    logger.info("ECN network started (%d nodes)", _network.node_count() if _network else 0)
    yield
    if _network:
        await _network.shutdown()
    logger.info("ECN network stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ECN — Executable Consensus Network API",
    description=(
        "Enterprise REST API for the ECN Supply-Chain Verification Network.\n\n"
        "Every transaction is independently executed by multiple verifier nodes; "
        "cryptographic consensus ensures no single party can forge results."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class TransactionRequest(BaseModel):
    """Generic transaction payload."""
    type: str
    # Supply-chain fields (all optional; validation happens in execution engine)
    product_id: Optional[str] = None
    destination: Optional[str] = None
    shipper: Optional[str] = None
    receiver: Optional[str] = None
    at_customs: Optional[bool] = None
    check_name: Optional[str] = None
    inspector: Optional[str] = None
    reason: Optional[str] = None
    released_by: Optional[str] = None
    # Generic financial fields
    from_: Optional[str] = None
    to: Optional[str] = None
    amount: Optional[int] = None

    model_config = {"populate_by_name": True}

    def to_ecn_tx(self) -> Dict[str, Any]:
        """Convert to the flat dict format used by the execution engine."""
        tx: Dict[str, Any] = {"type": self.type}
        if self.product_id is not None:
            tx["product_id"] = self.product_id
        if self.destination is not None:
            tx["destination"] = self.destination
        if self.shipper is not None:
            tx["shipper"] = self.shipper
        if self.receiver is not None:
            tx["receiver"] = self.receiver
        if self.at_customs is not None:
            tx["at_customs"] = self.at_customs
        if self.check_name is not None:
            tx["check_name"] = self.check_name
        if self.inspector is not None:
            tx["inspector"] = self.inspector
        if self.reason is not None:
            tx["reason"] = self.reason
        if self.released_by is not None:
            tx["released_by"] = self.released_by
        if self.from_ is not None:
            tx["from"] = self.from_
        if self.to is not None:
            tx["to"] = self.to
        if self.amount is not None:
            tx["amount"] = self.amount
        return tx


class NodeVoteResponse(BaseModel):
    node_id: str
    state_hash: str
    signature: Optional[str]
    status: str  # "honest" | "hash_fault" | "sig_fault"


class TransactionResponse(BaseModel):
    round_id: int
    timestamp: str
    transaction: Dict[str, Any]
    consensus_reached: bool
    agreed_hash: Optional[str]
    honest_nodes: List[str]
    faulty_nodes: List[str]
    invalid_sig_nodes: List[str]
    votes: List[NodeVoteResponse]


class NodeInfo(BaseModel):
    node_id: str
    port: int
    public_key_hex: str
    malicious: bool


class NetworkStateResponse(BaseModel):
    node_count: int
    nodes: List[NodeInfo]
    total_rounds: int
    fault_rounds: int
    fault_rate: float
    consensus_failure_rounds: int


class AuditSummaryResponse(BaseModel):
    total_rounds: int
    fault_rounds: int
    consensus_failure_rounds: int
    fault_rate: float
    top_faulty_nodes: List[List]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _require_network() -> P2PNetwork:
    if _network is None:
        raise HTTPException(status_code=503, detail="ECN network not initialised")
    return _network


def _require_audit() -> AuditLog:
    if _audit_log is None:
        raise HTTPException(status_code=503, detail="Audit log not initialised")
    return _audit_log


def _event_to_response(event) -> Dict[str, Any]:
    return {
        "round_id": event.round_id,
        "timestamp": event.timestamp,
        "transaction": event.transaction,
        "consensus_reached": event.consensus_reached,
        "agreed_hash": event.agreed_hash,
        "honest_nodes": event.honest_nodes,
        "faulty_nodes": event.faulty_nodes,
        "invalid_sig_nodes": event.invalid_sig_nodes,
        "votes": [
            {
                "node_id": v.node_id,
                "state_hash": v.state_hash,
                "signature": v.signature,
                "status": v.status,
            }
            for v in event.votes
        ],
    }


# ---------------------------------------------------------------------------
# Routes — Transactions
# ---------------------------------------------------------------------------

@app.post(
    "/transactions",
    response_model=TransactionResponse,
    summary="Submit a transaction to the ECN network",
    tags=["Transactions"],
)
async def submit_transaction(body: TransactionRequest):
    """
    Broadcast a transaction to all ECN nodes.

    All nodes independently execute the transaction, sign their result,
    and the API returns the consensus outcome including any fault detections.
    """
    net = _require_network()
    audit = _require_audit()

    tx = body.to_ecn_tx()

    try:
        results, cr = await net.broadcast(tx)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # If all nodes rejected the transaction (empty results), return 422
    if not results:
        raise HTTPException(
            status_code=422,
            detail="All nodes rejected the transaction — check transaction type and fields",
        )

    # The audit log was updated inside broadcast(); fetch the latest event
    event = audit.get_event(len(audit))
    if event is None:
        raise HTTPException(status_code=500, detail="Audit event not recorded")

    return _event_to_response(event)


# ---------------------------------------------------------------------------
# Routes — Network
# ---------------------------------------------------------------------------

@app.get(
    "/network/nodes",
    response_model=List[NodeInfo],
    summary="List all nodes in the network",
    tags=["Network"],
)
async def list_nodes():
    """Return metadata for every node: ID, port, public key, malicious flag."""
    net = _require_network()
    return [
        NodeInfo(
            node_id=s.node_id,
            port=s.port,
            public_key_hex=s.public_key_hex,
            malicious=s.malicious,
        )
        for s in net._servers
    ]


@app.get(
    "/network/state",
    response_model=NetworkStateResponse,
    summary="Network consensus health summary",
    tags=["Network"],
)
async def network_state():
    """Overall consensus health: node count, round stats, fault rate."""
    net = _require_network()
    audit = _require_audit()
    summary = audit.summary()
    nodes = [
        NodeInfo(
            node_id=s.node_id,
            port=s.port,
            public_key_hex=s.public_key_hex,
            malicious=s.malicious,
        )
        for s in net._servers
    ]
    return NetworkStateResponse(
        node_count=net.node_count(),
        nodes=nodes,
        total_rounds=summary["total_rounds"],
        fault_rounds=summary["fault_rounds"],
        fault_rate=summary["fault_rate"],
        consensus_failure_rounds=summary["consensus_failure_rounds"],
    )


# ---------------------------------------------------------------------------
# Routes — Audit
# ---------------------------------------------------------------------------

@app.get(
    "/audit/events",
    summary="Full paginated audit trail",
    tags=["Audit"],
)
async def get_audit_events(
    offset: int = Query(0, ge=0, description="Skip this many events"),
    limit: int = Query(50, ge=1, le=500, description="Max events to return"),
):
    """Return all audit events, newest last, with optional pagination."""
    audit = _require_audit()
    events = audit.get_events(limit=limit, offset=offset)
    return {"total": len(audit), "offset": offset, "events": [_event_to_response(e) for e in events]}


@app.get(
    "/audit/events/faults",
    summary="Audit events where faults were detected",
    tags=["Audit"],
)
async def get_fault_events():
    """Return only rounds where at least one node was flagged faulty."""
    audit = _require_audit()
    events = audit.get_fault_events()
    return {"total": len(events), "events": [_event_to_response(e) for e in events]}


@app.get(
    "/audit/events/{round_id}",
    summary="Single audit event by round number",
    tags=["Audit"],
)
async def get_audit_event(round_id: int):
    """Return the complete audit record for the given round (1-based)."""
    audit = _require_audit()
    event = audit.get_event(round_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Round {round_id} not found")
    return _event_to_response(event)


@app.get(
    "/audit/summary",
    response_model=AuditSummaryResponse,
    summary="Aggregate audit statistics",
    tags=["Audit"],
)
async def get_audit_summary():
    """Return aggregate statistics: total rounds, fault rate, top faulty nodes."""
    audit = _require_audit()
    s = audit.summary()
    return AuditSummaryResponse(**s)


@app.get(
    "/audit/replay",
    summary="Full transaction replay log",
    tags=["Audit"],
)
async def get_replay():
    """
    Return the complete ordered list of transactions and their outcomes.
    Useful for replay viewers and dashboards.
    """
    audit = _require_audit()
    events = audit.get_events()
    return {
        "total": len(events),
        "replay": [
            {
                "round_id": e.round_id,
                "timestamp": e.timestamp,
                "transaction": e.transaction,
                "consensus_reached": e.consensus_reached,
                "agreed_hash": e.agreed_hash,
                "fault_detected": e.has_fault(),
                "faulty_nodes": e.faulty_nodes,
            }
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("ecn.api:app", host="127.0.0.1", port=8000, reload=False)
