"""
sdk.py
------
Python SDK for the ECN — Executable Consensus Network API.

Provides a clean, typed interface for all ECN REST API endpoints so
enterprise integrators never need to hand-craft HTTP requests.

Quick start
-----------
>>> from ecn.sdk import ECNClient
>>> client = ECNClient("http://localhost:8000")
>>>
>>> # Ship a product through the supply chain
>>> result = client.ship("LAPTOP-001", destination="port", shipper="DHL")
>>> print(result["consensus_reached"])   # True
>>> print(result["faulty_nodes"])        # []
>>>
>>> # Inspect health
>>> summary = client.audit_summary()
>>> print(summary["fault_rate"])         # 0.0
>>>
>>> # Subscribe to fault alerts via webhook
>>> sub = client.subscribe_webhook("https://my-system/ecn-faults", events=["fault"])
>>> print(sub["webhook_id"])

Context manager (auto-closes session):
>>> with ECNClient("http://localhost:8000") as client:
...     result = client.inspect("LAPTOP-001", check_name="label", inspector="QA")
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ECNError(Exception):
    """Raised when the ECN API returns an error response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"ECN API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ECNTransactionError(ECNError):
    """Raised when a transaction is rejected (422)."""


class ECNNotFoundError(ECNError):
    """Raised when a resource is not found (404)."""


# ---------------------------------------------------------------------------
# ECNClient
# ---------------------------------------------------------------------------

class ECNClient:
    """
    Synchronous Python client for the ECN REST API.

    Parameters
    ----------
    base_url : str
        Base URL of the ECN API server (e.g. ``"http://localhost:8000"``).
    timeout : float
        Request timeout in seconds (default: 30).
    api_key : str, optional
        Optional ``X-API-Key`` header for authenticated deployments.

    Examples
    --------
    >>> client = ECNClient("http://localhost:8000")
    >>> result = client.ship("LAPTOP-001", destination="port", shipper="DHL")
    >>> client.close()

    Or use as a context manager::

        with ECNClient("http://localhost:8000") as client:
            result = client.ship("LAPTOP-001", destination="port", shipper="DHL")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: float = 30.0,
        api_key: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        if api_key:
            self._session.headers["X-API-Key"] = api_key

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "ECNClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        url = f"{self._base_url}{path}"
        resp = self._session.get(url, params=params, timeout=self._timeout)
        return self._handle(resp)

    def _post(self, path: str, body: Dict) -> Any:
        url = f"{self._base_url}{path}"
        resp = self._session.post(url, json=body, timeout=self._timeout)
        return self._handle(resp)

    def _delete(self, path: str) -> None:
        url = f"{self._base_url}{path}"
        resp = self._session.delete(url, timeout=self._timeout)
        self._handle(resp)

    @staticmethod
    def _handle(resp: requests.Response) -> Any:
        if resp.status_code == 204:
            return None
        if resp.status_code == 404:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ECNNotFoundError(resp.status_code, detail)
        if resp.status_code == 422:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ECNTransactionError(resp.status_code, detail)
        if not resp.ok:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ECNError(resp.status_code, detail)
        if resp.content:
            return resp.json()
        return None

    # ------------------------------------------------------------------
    # Generic transaction submission
    # ------------------------------------------------------------------

    def submit(self, tx_type: str, **fields: Any) -> Dict[str, Any]:
        """
        Submit a generic transaction to the ECN network.

        Parameters
        ----------
        tx_type : str
            Transaction type (e.g. ``"ship"``, ``"inspect"``).
        **fields
            Additional transaction fields (product_id, destination, etc.).

        Returns
        -------
        dict
            Consensus round result containing: round_id, timestamp,
            consensus_reached, agreed_hash, honest_nodes, faulty_nodes, votes.

        Raises
        ------
        ECNTransactionError
            If the transaction is rejected by all nodes (422).
        ECNError
            For other API errors.
        """
        body = {"type": tx_type, **fields}
        return self._post("/transactions", body)

    # ------------------------------------------------------------------
    # Supply-chain transaction helpers
    # ------------------------------------------------------------------

    def ship(
        self,
        product_id: str,
        destination: str,
        shipper: str,
    ) -> Dict[str, Any]:
        """Ship a product to a destination.

        Parameters
        ----------
        product_id : str
        destination : str
        shipper : str

        Returns
        -------
        dict — consensus round result
        """
        return self.submit(
            "ship",
            product_id=product_id,
            destination=destination,
            shipper=shipper,
        )

    def receive(
        self,
        product_id: str,
        receiver: str,
        at_customs: bool = False,
    ) -> Dict[str, Any]:
        """Accept delivery of a product.

        Parameters
        ----------
        product_id : str
        receiver : str
        at_customs : bool
            True if the product is being received at customs.

        Returns
        -------
        dict — consensus round result
        """
        return self.submit(
            "receive",
            product_id=product_id,
            receiver=receiver,
            at_customs=at_customs,
        )

    def inspect(
        self,
        product_id: str,
        check_name: str,
        inspector: str,
    ) -> Dict[str, Any]:
        """Record a passed quality or compliance check.

        Parameters
        ----------
        product_id : str
        check_name : str
        inspector : str

        Returns
        -------
        dict — consensus round result
        """
        return self.submit(
            "inspect",
            product_id=product_id,
            check_name=check_name,
            inspector=inspector,
        )

    def quarantine(
        self,
        product_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Flag a product as quarantined pending investigation.

        Parameters
        ----------
        product_id : str
        reason : str

        Returns
        -------
        dict — consensus round result
        """
        return self.submit("quarantine", product_id=product_id, reason=reason)

    def release(
        self,
        product_id: str,
        released_by: str,
    ) -> Dict[str, Any]:
        """Release a quarantined product back to normal flow.

        Parameters
        ----------
        product_id : str
        released_by : str

        Returns
        -------
        dict — consensus round result
        """
        return self.submit("release", product_id=product_id, released_by=released_by)

    # ------------------------------------------------------------------
    # Network information
    # ------------------------------------------------------------------

    def nodes(self) -> List[Dict[str, Any]]:
        """Return the list of verifier nodes in the network.

        Returns
        -------
        list[dict]
            Each dict has: node_id, port, public_key_hex, malicious.
        """
        return self._get("/network/nodes")

    def network_state(self) -> Dict[str, Any]:
        """Return the current consensus health of the network.

        Returns
        -------
        dict
            Contains: node_count, nodes, total_rounds, fault_rounds,
            fault_rate, consensus_failure_rounds.
        """
        return self._get("/network/state")

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def audit_events(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return a paginated list of all audit events.

        Parameters
        ----------
        limit : int
            Maximum number of events to return (1–500).
        offset : int
            Number of events to skip (for pagination).

        Returns
        -------
        dict
            Contains: total, offset, events (list).
        """
        return self._get("/audit/events", params={"limit": limit, "offset": offset})

    def fault_events(self) -> Dict[str, Any]:
        """Return only rounds where at least one fault was detected.

        Returns
        -------
        dict
            Contains: total, events (list).
        """
        return self._get("/audit/events/faults")

    def audit_event(self, round_id: int) -> Dict[str, Any]:
        """Return the complete audit record for a specific round.

        Parameters
        ----------
        round_id : int
            1-based round number.

        Returns
        -------
        dict — full AuditEvent

        Raises
        ------
        ECNNotFoundError
            If the round does not exist.
        """
        return self._get(f"/audit/events/{round_id}")

    def audit_summary(self) -> Dict[str, Any]:
        """Return aggregate audit statistics.

        Returns
        -------
        dict
            Contains: total_rounds, fault_rounds, consensus_failure_rounds,
            fault_rate, top_faulty_nodes.
        """
        return self._get("/audit/summary")

    def replay(self) -> Dict[str, Any]:
        """Return the full ordered transaction replay log.

        Returns
        -------
        dict
            Contains: total, replay (list of simplified round records).
        """
        return self._get("/audit/replay")

    # ------------------------------------------------------------------
    # Webhook event streaming
    # ------------------------------------------------------------------

    def subscribe_webhook(
        self,
        url: str,
        events: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Register a URL to receive webhook notifications after each round.

        Parameters
        ----------
        url : str
            HTTPS/HTTP endpoint that will receive POST requests.
        events : list[str], optional
            Event filter.  Options: ``"transaction"``, ``"fault"``.
            Omit (or pass empty list) to receive all events.

        Returns
        -------
        dict
            Contains: webhook_id, url, events, message.

        Examples
        --------
        >>> sub = client.subscribe_webhook("https://my-siem/ecn", events=["fault"])
        >>> print(sub["webhook_id"])
        """
        body: Dict[str, Any] = {"url": url, "events": events or []}
        return self._post("/webhooks", body)

    def list_webhooks(self) -> List[Dict[str, Any]]:
        """List all active webhook subscriptions.

        Returns
        -------
        list[dict]
            Each dict has: webhook_id, url, events.
        """
        return self._get("/webhooks")

    def unsubscribe_webhook(self, webhook_id: str) -> None:
        """Remove a webhook subscription.

        Parameters
        ----------
        webhook_id : str
            The ID returned by ``subscribe_webhook``.

        Raises
        ------
        ECNNotFoundError
            If the webhook_id does not exist.
        """
        self._delete(f"/webhooks/{webhook_id}")
