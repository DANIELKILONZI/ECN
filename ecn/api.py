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

POST /webhooks
    Subscribe a URL to receive HTTP POST notifications after each round.
    Supports optional event filtering ("transaction", "fault").

GET  /webhooks
    List all active webhook subscriptions.

DELETE /webhooks/{webhook_id}
    Remove a webhook subscription.

GET  /stream/topics
    List available event stream topics and their current offsets.

GET  /stream/topics/{topic}?offset=0&limit=10
    Kafka-style offset-based event poll.

GET  /stream/events?topics=transactions,faults&limit=N
    Server-Sent Events (SSE) live stream.

POST /admin/api-keys
    Issue a new API key (requires admin role when auth is enabled).

GET  /admin/api-keys
    List all issued API keys (secrets redacted).

DELETE /admin/api-keys/{key_id}
    Revoke an API key.

POST /tenants
    Create a new isolated tenant namespace.

GET  /tenants
    List all tenant namespaces.

DELETE /tenants/{tenant_id}
    Delete a tenant namespace and shut down its nodes.

POST /tenants/{tenant_id}/transactions
    Submit a transaction within a tenant namespace.

GET  /tenants/{tenant_id}/network/state
    Tenant network health.

GET  /tenants/{tenant_id}/audit/events
    Tenant audit trail.

GET  /tenants/{tenant_id}/audit/summary
    Tenant audit statistics.

Run with:
    python -m ecn.api               # starts on http://127.0.0.1:8000
    uvicorn ecn.api:app --reload    # dev mode
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl

from ecn.audit import AuditLog
from ecn.auth import init_registry, require_admin, require_role, ROLE_ADMIN, ROLE_SUBMITTER
from ecn.billing import (
    BillingTracker,
    StripeWebhookPayload,
    StripeWebhookResponse,
    UsageResponse,
    handle_stripe_event,
    init_tracker,
)
from ecn.p2p_network import P2PNetwork
from ecn.persistence import open_from_env
from ecn.stream import EventBus, sse_generator, TOPIC_TRANSACTIONS, TOPIC_FAULTS
from ecn.tenant import init_tenant_registry, router as tenant_router
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

# Webhook registry — stored here so it survives for the app lifetime
_webhook_registry: "WebhookRegistry"

# Event streaming bus
_event_bus: Optional[EventBus] = None

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
    global _network, _audit_log, _webhook_registry, _event_bus
    _webhook_registry = WebhookRegistry()
    _event_bus = EventBus()
    # Open persistent stores (no-op when ECN_DB_PATH is not set)
    stores = open_from_env()
    init_registry(store=stores["key_store"])
    init_tracker(store=stores["billing_store"])
    init_tenant_registry()
    _audit_log = AuditLog(store=stores["audit_store"])
    _network = await P2PNetwork.create(
        node_configs=_NODE_CONFIGS,
        initial_state=make_initial_state(_DEFAULT_PRODUCT_IDS),
        base_port=_BASE_PORT,
        verbose=False,
        audit_log=_audit_log,
    )
    logger.info("ECN network started (%d nodes)", _network.node_count() if _network else 0)
    yield
    from ecn.tenant import get_tenant_registry
    try:
        await get_tenant_registry().shutdown_all()
    except RuntimeError:
        pass
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

app.include_router(tenant_router)


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

    # Fire webhooks in the background (non-blocking)
    asyncio.create_task(_webhook_registry.dispatch(event))

    # Publish to event streaming bus
    if _event_bus is not None:
        _event_bus.publish(TOPIC_TRANSACTIONS, event.to_dict())
        if event.has_fault():
            _event_bus.publish(TOPIC_FAULTS, event.to_dict())

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
# Webhook Event Streaming
# ---------------------------------------------------------------------------

# Allowed event filter values
_WEBHOOK_EVENTS = frozenset({"transaction", "fault"})


class WebhookSubscription:
    """Internal representation of a registered webhook subscriber."""

    def __init__(self, url: str, events: List[str]) -> None:
        self.webhook_id: str = str(uuid.uuid4())
        self.url: str = url
        self.events: List[str] = events  # empty = receive all events


class WebhookRegistry:
    """
    In-memory registry of webhook subscriptions.

    After each consensus round the ``dispatch`` method fires an async HTTP
    POST to every matching subscriber.  Delivery failures are logged but
    never raise (fire-and-forget semantics).
    """

    def __init__(self) -> None:
        self._subscriptions: Dict[str, WebhookSubscription] = {}

    def subscribe(self, url: str, events: List[str]) -> WebhookSubscription:
        sub = WebhookSubscription(url=url, events=events)
        self._subscriptions[sub.webhook_id] = sub
        logger.info("Webhook subscribed: id=%s url=%s events=%s", sub.webhook_id, url, events)
        return sub

    def unsubscribe(self, webhook_id: str) -> bool:
        if webhook_id in self._subscriptions:
            del self._subscriptions[webhook_id]
            logger.info("Webhook unsubscribed: id=%s", webhook_id)
            return True
        return False

    def list_subscriptions(self) -> List[WebhookSubscription]:
        return list(self._subscriptions.values())

    async def dispatch(self, event) -> None:
        """
        Fire HTTP POST to every subscriber whose event filter matches.
        Runs after a consensus round completes (background task).
        """
        if not self._subscriptions:
            return

        payload = event.to_dict()
        # Annotate which event types this round triggered
        triggered: List[str] = ["transaction"]
        if event.has_fault():
            triggered.append("fault")

        async with httpx.AsyncClient(timeout=10.0) as client:
            for sub in list(self._subscriptions.values()):
                # Filter: if subscriber specified events, only send matching ones
                if sub.events and not any(t in sub.events for t in triggered):
                    continue
                try:
                    resp = await client.post(
                        sub.url,
                        json={
                            "webhook_id": sub.webhook_id,
                            "event_types": triggered,
                            "payload": payload,
                        },
                        headers={"Content-Type": "application/json", "X-ECN-Webhook": "1"},
                    )
                    logger.debug(
                        "Webhook delivered: id=%s status=%d", sub.webhook_id, resp.status_code
                    )
                except Exception as exc:
                    logger.warning("Webhook delivery failed: id=%s url=%s error=%s",
                                   sub.webhook_id, sub.url, exc)


# ---------------------------------------------------------------------------
# Webhook Request/Response models
# ---------------------------------------------------------------------------

class WebhookSubscribeRequest(BaseModel):
    url: str
    events: List[str] = []  # empty = all events; options: "transaction", "fault"

    def validate_events(self) -> None:
        invalid = set(self.events) - _WEBHOOK_EVENTS
        if invalid:
            raise ValueError(f"Unknown event types: {invalid}. Valid: {_WEBHOOK_EVENTS}")


class WebhookSubscribeResponse(BaseModel):
    webhook_id: str
    url: str
    events: List[str]
    message: str


class WebhookInfo(BaseModel):
    webhook_id: str
    url: str
    events: List[str]


# ---------------------------------------------------------------------------
# Webhook helper
# ---------------------------------------------------------------------------

def _require_webhooks() -> WebhookRegistry:
    try:
        return _webhook_registry
    except NameError:
        raise HTTPException(status_code=503, detail="Webhook registry not initialised")


# ---------------------------------------------------------------------------
# Routes — Webhooks
# ---------------------------------------------------------------------------

@app.post(
    "/webhooks",
    response_model=WebhookSubscribeResponse,
    status_code=201,
    summary="Subscribe a URL to receive webhook notifications",
    tags=["Webhooks"],
)
async def subscribe_webhook(body: WebhookSubscribeRequest):
    """
    Register a URL to receive HTTP POST notifications after each consensus round.

    **Event types** (optional filter — omit to receive all):
    - ``"transaction"`` — fired after every round
    - ``"fault"`` — fired only when at least one faulty node was detected

    The webhook payload is the full `AuditEvent` JSON plus metadata:
    ```json
    {
      "webhook_id": "...",
      "event_types": ["transaction"],
      "payload": { <AuditEvent> }
    }
    ```
    """
    try:
        body.validate_events()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    registry = _require_webhooks()
    sub = registry.subscribe(url=body.url, events=body.events)
    return WebhookSubscribeResponse(
        webhook_id=sub.webhook_id,
        url=sub.url,
        events=sub.events,
        message="Webhook registered. You will receive POST requests after each consensus round.",
    )


@app.get(
    "/webhooks",
    response_model=List[WebhookInfo],
    summary="List active webhook subscriptions",
    tags=["Webhooks"],
)
async def list_webhooks():
    """Return all currently active webhook subscriptions."""
    registry = _require_webhooks()
    return [
        WebhookInfo(webhook_id=s.webhook_id, url=s.url, events=s.events)
        for s in registry.list_subscriptions()
    ]


@app.delete(
    "/webhooks/{webhook_id}",
    status_code=204,
    summary="Remove a webhook subscription",
    tags=["Webhooks"],
)
async def unsubscribe_webhook(webhook_id: str):
    """Unsubscribe a previously registered webhook."""
    registry = _require_webhooks()
    if not registry.unsubscribe(webhook_id):
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")


# ---------------------------------------------------------------------------
# Routes — Event Streaming (Kafka-style)
# ---------------------------------------------------------------------------

def _require_event_bus() -> EventBus:
    if _event_bus is None:
        raise HTTPException(status_code=503, detail="Event bus not initialised")
    return _event_bus


@app.get(
    "/stream/topics",
    summary="List event stream topics",
    tags=["Streaming"],
)
async def list_stream_topics():
    """
    List available event topics and their current state (buffered events,
    next offset).  Use these offsets for Kafka-style polling.
    """
    bus = _require_event_bus()
    return {"topics": bus.topic_info()}


@app.get(
    "/stream/topics/{topic}",
    summary="Kafka-style offset-based event poll",
    tags=["Streaming"],
)
async def poll_topic(
    topic: str,
    offset: int = Query(0, ge=0, description="Start from this absolute offset"),
    limit: int = Query(10, ge=1, le=500, description="Max events to return"),
):
    """
    Return buffered events from *topic* starting at *offset*.

    Use the returned ``next_offset`` in your next poll request to avoid
    receiving duplicates — this is the same pattern as Kafka's consumer API.

    **Topics:** ``transactions``, ``faults``
    """
    bus = _require_event_bus()
    try:
        events = bus.consume(topic, offset=offset, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "topic": topic,
        "offset": offset,
        "count": len(events),
        "next_offset": (events[-1]["seq"] + 1) if events else offset,
        "events": events,
    }


@app.get(
    "/stream/events",
    summary="Server-Sent Events (SSE) live stream",
    tags=["Streaming"],
)
async def stream_events(
    topics: str = Query(
        "transactions",
        description="Comma-separated list of topics (e.g. 'transactions,faults')",
    ),
    limit: Optional[int] = Query(
        None,
        ge=1,
        description="Auto-close after this many events (omit for indefinite stream)",
    ),
    backfill: int = Query(
        0,
        ge=0,
        le=500,
        description="Replay this many recent buffered events before starting live stream",
    ),
):
    """
    Live Server-Sent Events stream.

    Each event is delivered as an SSE ``data:`` line containing JSON::

        data: {"seq": 3, "payload": {<AuditEvent>}}

    Use ``backfill=N`` to replay up to N recent buffered events per topic
    before switching to live streaming — provides Kafka-style reconnect
    semantics.

    A ``: heartbeat`` comment is sent every 15 seconds when idle to keep the
    connection alive through proxies.

    Connect with::

        curl -N http://localhost:8000/stream/events
        curl -N "http://localhost:8000/stream/events?topics=faults&limit=5"
        curl -N "http://localhost:8000/stream/events?backfill=10&limit=10"
    """
    bus = _require_event_bus()
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    return StreamingResponse(
        sse_generator(bus, topic_list, limit=limit, backfill=backfill),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Routes — Admin / RBAC
# ---------------------------------------------------------------------------

class IssueApiKeyRequest(BaseModel):
    role: str
    description: str = ""
    tenant_id: Optional[str] = None


class ApiKeyResponse(BaseModel):
    key_id: str
    key: str
    role: str
    description: str
    tenant_id: Optional[str]
    message: str


@app.post(
    "/admin/api-keys",
    response_model=ApiKeyResponse,
    status_code=201,
    summary="Issue a new API key",
    tags=["Admin"],
)
async def issue_api_key(
    body: IssueApiKeyRequest,
    _auth=Depends(require_admin()),
):
    """
    Issue a new API key with the given role.

    **Roles:** ``admin``, ``submitter``, ``auditor``, ``readonly``

    The key secret is returned **once** — store it securely.  It cannot be
    retrieved again.

    When ``ECN_AUTH_ENABLED`` is not set, this endpoint requires no key.
    """
    from ecn.auth import get_registry
    try:
        registry = get_registry()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        api_key = registry.issue(
            role=body.role,
            description=body.description,
            tenant_id=body.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return ApiKeyResponse(
        key_id=api_key.key_id,
        key=api_key.key,
        role=api_key.role,
        description=api_key.description,
        tenant_id=api_key.tenant_id,
        message="Key issued. Store the 'key' value securely — it is shown only once.",
    )


@app.get(
    "/admin/api-keys",
    summary="List all issued API keys (secrets redacted)",
    tags=["Admin"],
)
async def list_api_keys(_auth=Depends(require_admin())):
    """Return all API keys with secret values redacted."""
    from ecn.auth import get_registry
    try:
        registry = get_registry()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"keys": registry.list_keys()}


@app.delete(
    "/admin/api-keys/{key_id}",
    status_code=204,
    summary="Revoke an API key",
    tags=["Admin"],
)
async def revoke_api_key(key_id: str, _auth=Depends(require_admin())):
    """Permanently revoke an API key."""
    from ecn.auth import get_registry
    try:
        registry = get_registry()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not registry.revoke(key_id):
        raise HTTPException(status_code=404, detail=f"Key {key_id!r} not found")


# ---------------------------------------------------------------------------
# Routes — Billing / Usage metering
# ---------------------------------------------------------------------------

@app.get(
    "/tenants/{tenant_id}/usage",
    response_model=UsageResponse,
    summary="SaaS usage metering for this tenant",
    tags=["Billing"],
)
async def tenant_usage(tenant_id: str):
    """
    Return the number of consensus rounds executed by this tenant in the
    current billing period and all previous periods.

    This endpoint powers the SaaS billing model:
    - ``current_period`` — the active billing period key (e.g. ``"2026-04"``)
    - ``round_count``    — rounds executed in the current period
    - ``all_periods``    — full billing history (period → count)

    Wire ``round_count`` into your Stripe metered billing subscription to
    charge per consensus round.

    Set ``ECN_BILLING_PERIOD=daily`` for per-day buckets (default: monthly).
    """
    # Verify the tenant exists
    from ecn.tenant import get_tenant_registry
    registry = get_tenant_registry()
    if registry.get(tenant_id) is None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id!r} not found")

    from ecn.billing import get_tracker
    try:
        tracker = get_tracker()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    usage = tracker.get_usage(tenant_id)
    return UsageResponse(**usage)


@app.post(
    "/billing/stripe-webhook",
    response_model=StripeWebhookResponse,
    status_code=200,
    summary="Stripe payment event receiver",
    tags=["Billing"],
)
async def stripe_webhook(payload: StripeWebhookPayload):
    """
    Receive and process Stripe payment events.

    Stripe sends ``POST`` notifications to this URL for subscription events.
    Configure your Stripe webhook in the Stripe Dashboard to point at:

        https://your-ecn-host/billing/stripe-webhook

    **Supported event types:**
    - ``invoice.payment_succeeded`` — payment confirmed
    - ``invoice.payment_failed``    — payment failed, action required
    - ``customer.subscription.deleted`` — subscription cancelled

    To link Stripe customers to ECN tenants, set
    ``metadata.ecn_tenant_id`` on your Stripe customer or subscription object.

    **Stripe signature validation** is intentionally omitted here for
    portability (no Stripe SDK dependency).  In production, verify the
    ``Stripe-Signature`` header using your Stripe webhook secret before
    calling this route.
    """
    return handle_stripe_event(payload)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("ecn.api:app", host="127.0.0.1", port=8000, reload=False)
