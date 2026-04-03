"""
tenant.py
---------
Multi-tenant isolation for ECN.

Each tenant gets a fully independent execution environment:
  - Its own ``P2PNetwork`` (isolated node pool on OS-assigned ports)
  - Its own ``AuditLog``   (no cross-tenant audit leakage)
  - Its own ``WebhookRegistry`` (tenant-scoped notifications)

Tenants do not share state, ports, or audit history.  Transactions
submitted to tenant A have no effect on tenant B's world state.

FastAPI Router
--------------
A dedicated FastAPI router is included.  Mount it on the app::

    from ecn.tenant import router as tenant_router
    app.include_router(tenant_router)

Routes added:
    POST   /tenants                              — create tenant
    GET    /tenants                              — list tenants
    DELETE /tenants/{tenant_id}                  — delete tenant (shuts down network)
    POST   /tenants/{tenant_id}/transactions     — submit tx (same schema as global)
    GET    /tenants/{tenant_id}/network/state    — tenant network health
    GET    /tenants/{tenant_id}/audit/events     — tenant audit trail
    GET    /tenants/{tenant_id}/audit/summary    — tenant audit summary
    POST   /tenants/{tenant_id}/webhooks         — subscribe tenant webhook
    GET    /tenants/{tenant_id}/webhooks         — list tenant webhooks
    DELETE /tenants/{tenant_id}/webhooks/{wid}   — unsubscribe tenant webhook
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ecn.audit import AuditLog
from ecn.p2p_network import P2PNetwork
from ecn.use_cases.supply_chain import register_supply_chain_handlers, make_initial_state

logger = logging.getLogger(__name__)

# Ensure supply-chain handlers are registered (idempotent)
register_supply_chain_handlers()


# ---------------------------------------------------------------------------
# Default tenant node configuration
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_NODES = [
    {"node_id": "Validator-1"},
    {"node_id": "Validator-2"},
    {"node_id": "Validator-3"},
]

_DEFAULT_TENANT_PRODUCTS = ["ITEM-001", "ITEM-002"]


# ---------------------------------------------------------------------------
# Tenant data class
# ---------------------------------------------------------------------------

@dataclass
class Tenant:
    """Runtime representation of an active tenant."""
    tenant_id: str
    name: str
    network: P2PNetwork
    audit_log: AuditLog
    webhook_registry: Any  # WebhookRegistry (avoid circular import)


# ---------------------------------------------------------------------------
# TenantRegistry
# ---------------------------------------------------------------------------

class TenantRegistry:
    """
    In-memory registry of active tenants.

    Each tenant owns an independent P2PNetwork running on OS-assigned ports
    so there are no port conflicts between tenants or with the main network.
    """

    def __init__(self) -> None:
        self._tenants: Dict[str, Tenant] = {}

    async def create_tenant(
        self,
        name: str,
        node_configs: Optional[List[Dict]] = None,
        product_ids: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
    ) -> Tenant:
        """
        Create a new tenant and start its node network.

        Parameters
        ----------
        name : str
            Human-readable tenant name.
        node_configs : list[dict], optional
            List of ``{"node_id": str}`` dicts.  Defaults to 3 nodes.
        product_ids : list[str], optional
            Initial product IDs for the supply-chain state.
        tenant_id : str, optional
            Explicit ID (generated if omitted).

        Returns
        -------
        Tenant
        """
        from ecn.api import WebhookRegistry  # local import to avoid circular

        tid = tenant_id or str(uuid.uuid4())[:8]
        if tid in self._tenants:
            raise ValueError(f"Tenant {tid!r} already exists")

        configs = node_configs or _DEFAULT_TENANT_NODES
        products = product_ids or _DEFAULT_TENANT_PRODUCTS

        # Use port=0 so the OS assigns free ports — no collision with other tenants
        tenant_node_configs = [
            {"node_id": f"{tid}-{c['node_id']}", "port": 0}
            for c in configs
        ]

        audit_log = AuditLog()
        network = await P2PNetwork.create(
            node_configs=tenant_node_configs,
            initial_state=make_initial_state(products),
            base_port=0,      # signals P2PNetwork to use per-config ports
            verbose=False,
            audit_log=audit_log,
        )
        webhook_registry = WebhookRegistry()

        tenant = Tenant(
            tenant_id=tid,
            name=name,
            network=network,
            audit_log=audit_log,
            webhook_registry=webhook_registry,
        )
        self._tenants[tid] = tenant
        logger.info("Tenant created: id=%s name=%s nodes=%d", tid, name, network.node_count())
        return tenant

    async def delete_tenant(self, tenant_id: str) -> bool:
        """Shut down a tenant's network and remove it from the registry."""
        tenant = self._tenants.pop(tenant_id, None)
        if tenant is None:
            return False
        await tenant.network.shutdown()
        logger.info("Tenant deleted: id=%s", tenant_id)
        return True

    def get(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)

    def list_tenants(self) -> List[Dict[str, Any]]:
        return [
            {
                "tenant_id": t.tenant_id,
                "name": t.name,
                "node_count": t.network.node_count(),
            }
            for t in self._tenants.values()
        ]

    async def shutdown_all(self) -> None:
        """Shut down every tenant network (called on app shutdown)."""
        for tenant in list(self._tenants.values()):
            await tenant.network.shutdown()
        self._tenants.clear()

    def __len__(self) -> int:
        return len(self._tenants)


# ---------------------------------------------------------------------------
# Module-level registry singleton
# ---------------------------------------------------------------------------

_tenant_registry: Optional[TenantRegistry] = None


def get_tenant_registry() -> TenantRegistry:
    if _tenant_registry is None:
        raise RuntimeError("TenantRegistry not initialised")
    return _tenant_registry


def init_tenant_registry() -> TenantRegistry:
    global _tenant_registry
    _tenant_registry = TenantRegistry()
    return _tenant_registry


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateTenantRequest(BaseModel):
    name: str
    node_count: int = 3  # number of validator nodes for this tenant
    products: List[str] = []  # optional initial product IDs


class TenantInfo(BaseModel):
    tenant_id: str
    name: str
    node_count: int


class TenantTransactionRequest(BaseModel):
    type: str
    product_id: Optional[str] = None
    destination: Optional[str] = None
    shipper: Optional[str] = None
    receiver: Optional[str] = None
    at_customs: Optional[bool] = None
    check_name: Optional[str] = None
    inspector: Optional[str] = None
    reason: Optional[str] = None
    released_by: Optional[str] = None

    def to_ecn_tx(self) -> Dict[str, Any]:
        tx: Dict[str, Any] = {"type": self.type}
        for field_name in (
            "product_id", "destination", "shipper", "receiver",
            "at_customs", "check_name", "inspector", "reason", "released_by",
        ):
            val = getattr(self, field_name)
            if val is not None:
                tx[field_name] = val
        return tx


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/tenants", tags=["Tenants"])


def _get_tenant(tenant_id: str) -> Tenant:
    registry = get_tenant_registry()
    tenant = registry.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id!r} not found")
    return tenant


def _event_to_dict(event) -> Dict[str, Any]:
    """Convert an AuditEvent to a serialisable dict."""
    return {
        "round_id": event.round_id,
        "timestamp": event.timestamp,
        "transaction": event.transaction,
        "consensus_reached": event.consensus_reached,
        "agreed_hash": event.agreed_hash,
        "honest_nodes": event.honest_nodes,
        "faulty_nodes": event.faulty_nodes,
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


@router.post(
    "",
    response_model=TenantInfo,
    status_code=201,
    summary="Create a new isolated tenant namespace",
)
async def create_tenant(body: CreateTenantRequest):
    """
    Create a new tenant with its own isolated ECN node network.

    Each tenant runs independent validator nodes on OS-assigned ports so
    there are no port conflicts.  Transactions in one tenant never affect
    another tenant's state or audit trail.
    """
    registry = get_tenant_registry()

    node_count = max(1, min(body.node_count, 10))
    node_configs = [{"node_id": f"Node-{i + 1}"} for i in range(node_count)]
    products = body.products or _DEFAULT_TENANT_PRODUCTS

    try:
        tenant = await registry.create_tenant(
            name=body.name,
            node_configs=node_configs,
            product_ids=products,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create tenant: {exc}")

    return TenantInfo(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        node_count=tenant.network.node_count(),
    )


@router.get(
    "",
    response_model=List[TenantInfo],
    summary="List all tenant namespaces",
)
async def list_tenants():
    """Return all active tenant namespaces."""
    registry = get_tenant_registry()
    return [
        TenantInfo(tenant_id=t["tenant_id"], name=t["name"], node_count=t["node_count"])
        for t in registry.list_tenants()
    ]


@router.delete(
    "/{tenant_id}",
    status_code=204,
    summary="Delete a tenant namespace and shut down its nodes",
)
async def delete_tenant(tenant_id: str):
    """Permanently remove a tenant and free its resources."""
    registry = get_tenant_registry()
    if not await registry.delete_tenant(tenant_id):
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id!r} not found")


@router.post(
    "/{tenant_id}/transactions",
    summary="Submit a transaction within a tenant namespace",
)
async def tenant_submit_transaction(tenant_id: str, body: TenantTransactionRequest):
    """
    Broadcast a transaction to the tenant's private node network.

    Fully isolated from all other tenants.
    """
    tenant = _get_tenant(tenant_id)
    tx = body.to_ecn_tx()

    try:
        results, cr = await tenant.network.broadcast(tx)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not results:
        raise HTTPException(
            status_code=422,
            detail="All nodes rejected the transaction — check type and fields",
        )

    event = tenant.audit_log.get_event(len(tenant.audit_log))
    if event is None:
        raise HTTPException(status_code=500, detail="Audit event not recorded")

    # Dispatch tenant webhooks in background
    asyncio.create_task(tenant.webhook_registry.dispatch(event))

    return _event_to_dict(event)


@router.get(
    "/{tenant_id}/network/state",
    summary="Tenant network health summary",
)
async def tenant_network_state(tenant_id: str):
    """Return consensus health and node info for the tenant's network."""
    tenant = _get_tenant(tenant_id)
    summary = tenant.audit_log.summary()
    return {
        "tenant_id": tenant_id,
        "node_count": tenant.network.node_count(),
        "nodes": [
            {
                "node_id": s.node_id,
                "port": s.port,
                "public_key_hex": s.public_key_hex,
                "malicious": s.malicious,
            }
            for s in tenant.network._servers
        ],
        "total_rounds": summary["total_rounds"],
        "fault_rounds": summary["fault_rounds"],
        "fault_rate": summary["fault_rate"],
    }


@router.get(
    "/{tenant_id}/audit/events",
    summary="Tenant audit trail",
)
async def tenant_audit_events(
    tenant_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """Return paginated audit events for this tenant."""
    tenant = _get_tenant(tenant_id)
    events = tenant.audit_log.get_events(limit=limit, offset=offset)
    return {
        "tenant_id": tenant_id,
        "total": len(tenant.audit_log),
        "offset": offset,
        "events": [_event_to_dict(e) for e in events],
    }


@router.get(
    "/{tenant_id}/audit/summary",
    summary="Tenant audit statistics",
)
async def tenant_audit_summary(tenant_id: str):
    """Return aggregate audit statistics for this tenant."""
    tenant = _get_tenant(tenant_id)
    s = tenant.audit_log.summary()
    return {"tenant_id": tenant_id, **s}


# ---------------------------------------------------------------------------
# Tenant webhooks
# ---------------------------------------------------------------------------

class TenantWebhookRequest(BaseModel):
    url: str
    events: List[str] = []


@router.post(
    "/{tenant_id}/webhooks",
    status_code=201,
    summary="Subscribe a URL to tenant webhook notifications",
)
async def tenant_subscribe_webhook(tenant_id: str, body: TenantWebhookRequest):
    tenant = _get_tenant(tenant_id)
    from ecn.api import _WEBHOOK_EVENTS
    invalid = set(body.events) - _WEBHOOK_EVENTS
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unknown event types: {invalid}")
    sub = tenant.webhook_registry.subscribe(url=body.url, events=body.events)
    return {"webhook_id": sub.webhook_id, "url": sub.url, "events": sub.events}


@router.get(
    "/{tenant_id}/webhooks",
    summary="List tenant webhook subscriptions",
)
async def tenant_list_webhooks(tenant_id: str):
    tenant = _get_tenant(tenant_id)
    return [
        {"webhook_id": s.webhook_id, "url": s.url, "events": s.events}
        for s in tenant.webhook_registry.list_subscriptions()
    ]


@router.delete(
    "/{tenant_id}/webhooks/{webhook_id}",
    status_code=204,
    summary="Remove a tenant webhook subscription",
)
async def tenant_unsubscribe_webhook(tenant_id: str, webhook_id: str):
    tenant = _get_tenant(tenant_id)
    if not tenant.webhook_registry.unsubscribe(webhook_id):
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
