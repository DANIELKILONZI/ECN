"""
billing.py
----------
SaaS usage metering for ECN — Priority 3 of the production roadmap.

Tracks consensus rounds per tenant per billing period, enabling per-round
or per-transaction pricing (Model A in the ECN business model):

    "ECN runs the nodes on behalf of customers.
     Customers pay per transaction or per consensus round."

Usage
-----
::

    from ecn.billing import BillingTracker

    tracker = BillingTracker()                  # in-memory (tests / dev)
    # or
    from ecn.persistence import SQLiteBillingStore
    tracker = BillingTracker(store=SQLiteBillingStore("/var/ecn/ecn.db"))

    tracker.record_round(tenant_id="acme")      # call after each broadcast

    usage = tracker.get_usage("acme")
    # → {"tenant_id": "acme", "current_period": "2026-04",
    #    "round_count": 42, "all_periods": [...]}

API routes added in api.py
--------------------------
    GET  /tenants/{tenant_id}/usage    — current period + history
    POST /billing/stripe-webhook       — Stripe payment event receiver

Billing periods
---------------
The default period granularity is ``"YYYY-MM"`` (calendar month).  Override
with ``ECN_BILLING_PERIOD=daily`` (``"YYYY-MM-DD"``) or
``ECN_BILLING_PERIOD=yearly`` (``"YYYY"``).
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ecn.persistence import BillingStore

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# BillingTracker
# ---------------------------------------------------------------------------

class BillingTracker:
    """
    Per-tenant round counter with optional durable persistence.

    Call ``record_round(tenant_id)`` after every successful consensus
    broadcast to keep an accurate usage tally.

    Parameters
    ----------
    store : BillingStore, optional
        When ``None`` (default) counts are kept in memory (useful for tests
        and single-pod deployments where persistence isn't required).
        Pass a ``SQLiteBillingStore`` to persist across restarts.
    """

    def __init__(self, store: "Optional[BillingStore]" = None) -> None:
        self._store = store
        self._mem = _InMemoryCounts() if store is None else None

    def record_round(self, tenant_id: str) -> None:
        """Increment the round counter for *tenant_id* in the current period."""
        period = _current_period()
        if self._store is not None:
            self._store.increment(tenant_id, period)
        else:
            self._mem.increment(tenant_id, period)
        logger.debug("billing: tenant=%s period=%s +1 round", tenant_id, period)

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


# ---------------------------------------------------------------------------
# Singleton (populated in api.py lifespan)
# ---------------------------------------------------------------------------

_tracker: Optional[BillingTracker] = None


def get_tracker() -> BillingTracker:
    """Return the global BillingTracker (raises if not initialised)."""
    if _tracker is None:
        raise RuntimeError("BillingTracker not initialised")
    return _tracker


def init_tracker(store: "Optional[BillingStore]" = None) -> BillingTracker:
    """Initialise and return the global BillingTracker."""
    global _tracker
    _tracker = BillingTracker(store=store)
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
