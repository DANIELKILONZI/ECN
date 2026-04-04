"""
billing.py
----------
SaaS usage metering for ECN — Priority 3 of the production roadmap.

Tracks consensus rounds per tenant per billing period, enabling RMAE-based
pricing (Resolved Multi-Party Agreement Event):

    "ECN runs the nodes on behalf of customers.
     Customers pay per RMAE — not per compute round."

Pricing tiers (set ECN_PRICING_TIER=basic|standard|critical):
    Tier      SLA    Quorum   Price/RMAE   Use case
    basic     95%    0.51     $0.001       Internal workflows
    standard  99%    0.67     $0.005       Logistics
    critical  99.99% 0.80     $0.020       Financial settlement

Usage
-----
::

    from ecn.billing import BillingTracker

    tracker = BillingTracker()                        # in-memory (tests / dev)
    # or with persistence:
    from ecn.persistence import SQLiteBillingStore, SQLiteBillingLedger
    tracker = BillingTracker(
        store=SQLiteBillingStore("/var/ecn/ecn.db"),
        ledger=SQLiteBillingLedger("/var/ecn/ecn.db"),
    )

    billed = tracker.record_round(
        tenant_id="acme",
        tx_id="abc-123",
        node_count=5,
        consensus_reached=True,
    )
    # billed → amount charged in USD (float)

    usage = tracker.get_usage("acme")
    # → {"tenant_id": "acme", "current_period": "2026-04",
    #    "round_count": 42, "all_periods": [...]}

    sla = tracker.get_sla("acme")
    # → {"tier": "standard", "sla_threshold": 0.99, "fault_rate": 0.01,
    #    "sla_met": True, "credits_earned": 0.0}

API routes added in api.py
--------------------------
    GET  /tenants/{tenant_id}/usage            — current period + history
    GET  /tenants/{tenant_id}/billing-ledger   — immutable billing truth
    GET  /tenants/{tenant_id}/sla              — SLA compliance + pricing tier
    POST /billing/stripe-webhook               — Stripe payment event receiver
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ecn.persistence import BillingStore, BillingLedger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RMAE Pricing tier configuration
# ---------------------------------------------------------------------------

_PRICING_TIERS: Dict[str, Dict[str, Any]] = {
    "basic": {
        "sla_threshold": 0.95,
        "quorum_threshold": 0.51,
        "price_per_rmae": 0.001,
        "description": "Internal workflows — 95% consensus SLA",
    },
    "standard": {
        "sla_threshold": 0.99,
        "quorum_threshold": 0.67,
        "price_per_rmae": 0.005,
        "description": "Logistics — 99% consensus SLA",
    },
    "critical": {
        "sla_threshold": 0.9999,
        "quorum_threshold": 0.80,
        "price_per_rmae": 0.020,
        "description": "Financial settlement — 99.99% consensus SLA",
    },
}
_DEFAULT_TIER = "standard"


def get_pricing_tier() -> str:
    """Return the active pricing tier from ECN_PRICING_TIER env var."""
    tier = os.environ.get("ECN_PRICING_TIER", _DEFAULT_TIER).lower().strip()
    if tier not in _PRICING_TIERS:
        logger.warning("ECN_PRICING_TIER=%r unknown — using 'standard'", tier)
        return _DEFAULT_TIER
    return tier


def get_tier_config(tier: Optional[str] = None) -> Dict[str, Any]:
    """Return the pricing configuration for *tier* (defaults to active tier)."""
    return _PRICING_TIERS[tier or get_pricing_tier()]


def compute_billed_amount(node_count: int, tier: Optional[str] = None) -> float:
    """
    Compute the billed amount for one RMAE.

    Billing unit = node_count × price_per_rmae[tier].
    This is the "Resolved Multi-Party Agreement Event" price.
    """
    config = get_tier_config(tier)
    return round(node_count * config["price_per_rmae"], 6)


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def _current_period() -> str:
    """Return a string key for the current billing period."""
    now = datetime.datetime.now(datetime.timezone.utc)
    granularity = os.environ.get("ECN_BILLING_PERIOD", "monthly").lower()
    if granularity == "daily":
        return now.strftime("%Y-%m-%d")
    if granularity in ("yearly", "annual"):
        return now.strftime("%Y")
    return now.strftime("%Y-%m")  # monthly (default)


# ---------------------------------------------------------------------------
# In-memory fallback counters
# ---------------------------------------------------------------------------

class _InMemoryCounts:
    """Simple dict-based counter used when no durable store is configured."""

    def __init__(self) -> None:
        self._counts: Dict[tuple, int] = {}

    def increment(self, tenant_id: str, period: str) -> None:
        key = (tenant_id, period)
        self._counts[key] = self._counts.get(key, 0) + 1

    def get_count(self, tenant_id: str, period: str) -> int:
        return self._counts.get((tenant_id, period), 0)

    def get_all_periods(self, tenant_id: str) -> List[Dict[str, Any]]:
        result = []
        for (tid, period), count in sorted(self._counts.items()):
            if tid == tenant_id:
                result.append({"period": period, "round_count": count})
        return result


class _InMemoryLedger:
    """Simple list-based billing ledger for in-memory mode."""

    def __init__(self) -> None:
        self._entries: List[Dict[str, Any]] = []
        self._seq = 0

    def append_entry(
        self,
        tx_id: str,
        tenant_id: str,
        period: str,
        node_round_count: int,
        billed_amount: float,
        consensus_reached: bool,
    ) -> None:
        import datetime as _dt
        self._seq += 1
        self._entries.append({
            "ledger_id": self._seq,
            "tx_id": tx_id,
            "tenant_id": tenant_id,
            "period": period,
            "node_round_count": node_round_count,
            "billed_amount": billed_amount,
            "consensus_reached": consensus_reached,
            "recorded_at": (
                _dt.datetime.now(_dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        })

    def load_entries(self, tenant_id: str) -> List[Dict[str, Any]]:
        return [e for e in self._entries if e["tenant_id"] == tenant_id]


class _InMemorySLATracker:
    """In-memory SLA stats per tenant (total rounds, fault rounds)."""

    def __init__(self) -> None:
        self._total: Dict[str, int] = {}
        self._faults: Dict[str, int] = {}

    def record(self, tenant_id: str, consensus_reached: bool) -> None:
        self._total[tenant_id] = self._total.get(tenant_id, 0) + 1
        if not consensus_reached:
            self._faults[tenant_id] = self._faults.get(tenant_id, 0) + 1

    def get_stats(self, tenant_id: str) -> Dict[str, Any]:
        total = self._total.get(tenant_id, 0)
        faults = self._faults.get(tenant_id, 0)
        return {"total": total, "fault_rounds": faults}


# ---------------------------------------------------------------------------
# BillingTracker
# ---------------------------------------------------------------------------

class BillingTracker:
    """
    Per-tenant RMAE billing tracker with optional durable persistence.

    Call ``record_round(tenant_id, tx_id, node_count, consensus_reached)``
    after every successful consensus broadcast to keep accurate usage tallies
    and append an immutable billing ledger entry.

    Parameters
    ----------
    store : BillingStore, optional
        Counter store.  When ``None`` counts are kept in memory.
    ledger : BillingLedger, optional
        Immutable billing ledger.  When ``None`` entries are kept in memory.
    """

    def __init__(
        self,
        store: "Optional[BillingStore]" = None,
        ledger: "Optional[BillingLedger]" = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._mem = _InMemoryCounts() if store is None else None
        self._mem_ledger = _InMemoryLedger() if ledger is None else None
        self._sla = _InMemorySLATracker()

    def record_round(
        self,
        tenant_id: str,
        tx_id: str = "",
        node_count: int = 1,
        consensus_reached: bool = True,
    ) -> float:
        """
        Record one completed RMAE for *tenant_id*.

        Writes to:
        1. The counter store (for GET /usage).
        2. The immutable billing ledger (for dispute resolution).

        Returns
        -------
        float
            The amount billed for this RMAE in USD.
        """
        period = _current_period()
        billed_amount = compute_billed_amount(node_count)
        node_round_count = node_count  # 1 round × node_count

        # 1. Increment usage counter
        if self._store is not None:
            self._store.increment(tenant_id, period)
        else:
            self._mem.increment(tenant_id, period)

        # 2. Append to immutable billing ledger
        ledger = self._ledger if self._ledger is not None else self._mem_ledger
        ledger.append_entry(
            tx_id=tx_id,
            tenant_id=tenant_id,
            period=period,
            node_round_count=node_round_count,
            billed_amount=billed_amount,
            consensus_reached=consensus_reached,
        )

        # 3. Track SLA stats
        self._sla.record(tenant_id, consensus_reached)

        logger.debug(
            "billing: tenant=%s period=%s tx_id=%s node_count=%d billed=$%.6f",
            tenant_id, period, tx_id, node_count, billed_amount,
        )
        return billed_amount

    def get_usage(self, tenant_id: str) -> Dict[str, Any]:
        """
        Return usage data for *tenant_id*.

        Returns
        -------
        dict
            ``current_period``, ``round_count`` (this period),
            ``all_periods`` (full history), ``tenant_id``.
        """
        period = _current_period()
        if self._store is not None:
            count = self._store.get_count(tenant_id, period)
            history = self._store.get_all_periods(tenant_id)
        else:
            count = self._mem.get_count(tenant_id, period)
            history = self._mem.get_all_periods(tenant_id)

        return {
            "tenant_id": tenant_id,
            "current_period": period,
            "round_count": count,
            "all_periods": history,
        }

    def get_billing_ledger(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Return the immutable billing ledger entries for *tenant_id*."""
        ledger = self._ledger if self._ledger is not None else self._mem_ledger
        return ledger.load_entries(tenant_id)

    def get_sla(self, tenant_id: str) -> Dict[str, Any]:
        """
        Return SLA compliance data for *tenant_id*.

        Returns
        -------
        dict
            ``tier``, ``sla_threshold``, ``fault_rate_this_period``,
            ``sla_met``, ``credits_earned``.
        """
        tier = get_pricing_tier()
        config = get_tier_config(tier)
        sla_threshold = config["sla_threshold"]

        stats = self._sla.get_stats(tenant_id)
        total = stats["total"]
        fault_rounds = stats["fault_rounds"]
        consensus_rate = ((total - fault_rounds) / total) if total > 0 else 1.0
        fault_rate = fault_rounds / total if total > 0 else 0.0
        sla_met = consensus_rate >= sla_threshold

        return {
            "tenant_id": tenant_id,
            "tier": tier,
            "sla_threshold": sla_threshold,
            "quorum_threshold": config["quorum_threshold"],
            "price_per_rmae": config["price_per_rmae"],
            "description": config["description"],
            "total_rounds": total,
            "fault_rounds": fault_rounds,
            "fault_rate": round(fault_rate, 4),
            "consensus_rate": round(consensus_rate, 4),
            "sla_met": sla_met,
            "credits_earned": 0.0,  # credit logic to be wired per contract
        }


# ---------------------------------------------------------------------------
# Singleton (populated in api.py lifespan)
# ---------------------------------------------------------------------------

_tracker: Optional[BillingTracker] = None


def get_tracker() -> BillingTracker:
    """Return the global BillingTracker (raises if not initialised)."""
    if _tracker is None:
        raise RuntimeError("BillingTracker not initialised")
    return _tracker


def init_tracker(
    store: "Optional[BillingStore]" = None,
    ledger: "Optional[BillingLedger]" = None,
) -> BillingTracker:
    """Initialise and return the global BillingTracker."""
    global _tracker
    _tracker = BillingTracker(store=store, ledger=ledger)
    return _tracker


# ---------------------------------------------------------------------------
# Pydantic models for API layer
# ---------------------------------------------------------------------------

class UsageResponse(BaseModel):
    """Response schema for GET /tenants/{id}/usage."""
    tenant_id: str
    current_period: str
    round_count: int
    all_periods: List[Dict[str, Any]]


class BillingLedgerEntry(BaseModel):
    """A single immutable billing ledger entry."""
    ledger_id: int
    tx_id: str
    tenant_id: str
    period: str
    node_round_count: int
    billed_amount: float
    consensus_reached: bool
    recorded_at: str


class BillingLedgerResponse(BaseModel):
    """Response schema for GET /tenants/{id}/billing-ledger."""
    tenant_id: str
    entries: List[BillingLedgerEntry]
    total_entries: int
    total_billed: float


class SLAResponse(BaseModel):
    """Response schema for GET /tenants/{id}/sla."""
    tenant_id: str
    tier: str
    sla_threshold: float
    quorum_threshold: float
    price_per_rmae: float
    description: str
    total_rounds: int
    fault_rounds: int
    fault_rate: float
    consensus_rate: float
    sla_met: bool
    credits_earned: float


class StripeWebhookPayload(BaseModel):
    """Minimal Stripe event envelope (only fields ECN acts on)."""
    id: str
    type: str
    data: Dict[str, Any] = {}


class StripeWebhookResponse(BaseModel):
    status: str
    event_id: str
    event_type: str
    message: str


# ---------------------------------------------------------------------------
# Stripe webhook handler (business logic only — no Stripe SDK dependency)
# ---------------------------------------------------------------------------

def handle_stripe_event(payload: StripeWebhookPayload) -> StripeWebhookResponse:
    """
    Process a Stripe event and return a response.

    Currently handles:
      - ``invoice.payment_succeeded`` — log successful payment, extend access
      - ``invoice.payment_failed``    — log failed payment, flag tenant
      - ``customer.subscription.deleted`` — log cancellation

    This is intentionally a stub that logs the event.  Wire in your CRM /
    provisioning system here.
    """
    event_type = payload.type
    event_id = payload.id

    if event_type == "invoice.payment_succeeded":
        tenant_id = _extract_tenant_from_stripe(payload.data)
        logger.info(
            "Stripe payment succeeded: event_id=%s tenant=%s",
            event_id,
            tenant_id,
        )
        message = f"Payment recorded for tenant {tenant_id!r}"

    elif event_type == "invoice.payment_failed":
        tenant_id = _extract_tenant_from_stripe(payload.data)
        logger.warning(
            "Stripe payment FAILED: event_id=%s tenant=%s — access suspension pending",
            event_id,
            tenant_id,
        )
        message = f"Payment failed for tenant {tenant_id!r} — action required"

    elif event_type == "customer.subscription.deleted":
        tenant_id = _extract_tenant_from_stripe(payload.data)
        logger.info(
            "Stripe subscription cancelled: event_id=%s tenant=%s",
            event_id,
            tenant_id,
        )
        message = f"Subscription cancelled for tenant {tenant_id!r}"

    else:
        logger.debug("Stripe event ignored: type=%s id=%s", event_type, event_id)
        message = f"Event type {event_type!r} acknowledged (no action)"

    return StripeWebhookResponse(
        status="ok",
        event_id=event_id,
        event_type=event_type,
        message=message,
    )


def _extract_tenant_from_stripe(data: Dict[str, Any]) -> str:
    """Extract ECN tenant_id from Stripe event data.

    Stripe events store custom metadata in ``data.object.metadata``.
    Set ``metadata.ecn_tenant_id`` on your Stripe customer/subscription to
    link payments to ECN tenants.
    """
    obj = data.get("object", {})
    metadata = obj.get("metadata", {})
    return metadata.get("ecn_tenant_id", "unknown")
