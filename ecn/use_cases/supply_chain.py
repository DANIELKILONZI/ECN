"""
use_cases/supply_chain.py
--------------------------
Domain-Specific Use Case — GAP 3: Supply Chain Verification

Problem this solves
-------------------
Global supply chains are plagued by:
  - Counterfeit goods entering the chain undetected
  - Disputes between parties over product status / custody
  - No single trusted party (warehouse, shipper, customs all distrust each other)

How ECN fixes it
----------------
Multiple independent verifiers (warehouse, shipper, customs broker, insurer)
each execute the same state transitions locally and reach consensus on the
resulting state hash.  Any verifier that tries to forge a product status
(e.g. to pass a counterfeit item through customs) is immediately detected by
the consensus protocol — their state hash will differ from the majority.

State model
-----------
{
  "products": {
    "<product_id>": {
      "status":   "manufactured" | "in_transit" | "at_customs" | "quarantined" | "delivered",
      "location": "<location_name>",
      "owner":    "<party_name>",
      "checks":   ["<check_name>", ...]   # quality / inspection checks passed
    }
  }
}

Transaction types
-----------------
- ship:      move a product from one location to another (changes status to in_transit)
- receive:   accept delivery at a destination (changes status to at_customs or delivered)
- inspect:   record a passed quality / compliance check
- quarantine: flag a product as quarantined pending investigation
- release:   release a quarantined product back to normal flow
"""

import copy
from typing import Any, Dict

from ecn.execution_engine import register_handler, State, Transaction


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _get_product(state: State, product_id: str) -> Dict[str, Any]:
    products = state.get("products", {})
    if product_id not in products:
        raise ValueError(f"Product '{product_id}' not found in state")
    return products[product_id]


# ---------------------------------------------------------------------------
# Transaction handlers
# ---------------------------------------------------------------------------

def _handle_ship(state: State, tx: Transaction) -> State:
    """
    Ship a product from its current location to a new destination.

    Required tx fields: product_id, destination, shipper
    """
    product_id = tx["product_id"]
    destination = tx["destination"]
    shipper = tx["shipper"]

    new_state = copy.deepcopy(state)
    product = _get_product(new_state, product_id)

    if product["status"] in ("in_transit", "quarantined"):
        raise ValueError(
            f"Cannot ship product '{product_id}': current status is '{product['status']}'"
        )

    product["status"] = "in_transit"
    product["location"] = destination
    product["owner"] = shipper
    return new_state


def _handle_receive(state: State, tx: Transaction) -> State:
    """
    Record receipt of a product at a destination.

    Required tx fields: product_id, receiver, at_customs (bool)
    """
    product_id = tx["product_id"]
    receiver = tx["receiver"]
    at_customs = bool(tx.get("at_customs", False))

    new_state = copy.deepcopy(state)
    product = _get_product(new_state, product_id)

    if product["status"] != "in_transit":
        raise ValueError(
            f"Cannot receive product '{product_id}': current status is '{product['status']}'"
        )

    product["status"] = "at_customs" if at_customs else "delivered"
    product["owner"] = receiver
    return new_state


def _handle_inspect(state: State, tx: Transaction) -> State:
    """
    Record a passed inspection / quality check.

    Required tx fields: product_id, check_name, inspector
    """
    product_id = tx["product_id"]
    check_name = tx["check_name"]

    new_state = copy.deepcopy(state)
    product = _get_product(new_state, product_id)

    checks = product.setdefault("checks", [])
    if check_name not in checks:
        checks.append(check_name)
        checks.sort()  # deterministic ordering
    return new_state


def _handle_quarantine(state: State, tx: Transaction) -> State:
    """
    Flag a product as quarantined.

    Required tx fields: product_id, reason
    """
    product_id = tx["product_id"]
    reason = tx["reason"]

    new_state = copy.deepcopy(state)
    product = _get_product(new_state, product_id)

    if product["status"] == "quarantined":
        raise ValueError(f"Product '{product_id}' is already quarantined")

    product["status"] = "quarantined"
    product["quarantine_reason"] = reason
    return new_state


def _handle_release(state: State, tx: Transaction) -> State:
    """
    Release a quarantined product.

    Required tx fields: product_id, released_by
    """
    product_id = tx["product_id"]

    new_state = copy.deepcopy(state)
    product = _get_product(new_state, product_id)

    if product["status"] != "quarantined":
        raise ValueError(f"Product '{product_id}' is not quarantined")

    product["status"] = "at_customs"
    product.pop("quarantine_reason", None)
    return new_state


# ---------------------------------------------------------------------------
# Register handlers with the execution engine
# ---------------------------------------------------------------------------

def register_supply_chain_handlers() -> None:
    """
    Register all supply chain transaction handlers with the global execution
    engine registry.  Call once at startup before processing transactions.
    """
    register_handler("ship", _handle_ship)
    register_handler("receive", _handle_receive)
    register_handler("inspect", _handle_inspect)
    register_handler("quarantine", _handle_quarantine)
    register_handler("release", _handle_release)


# ---------------------------------------------------------------------------
# Initial state factory
# ---------------------------------------------------------------------------

def make_initial_state(product_ids: list) -> State:
    """
    Create an initial supply chain state with freshly manufactured products.

    Parameters
    ----------
    product_ids : list[str]
        List of product identifiers to include.

    Returns
    -------
    State
    """
    return {
        "products": {
            pid: {
                "status": "manufactured",
                "location": "factory",
                "owner": "manufacturer",
                "checks": [],
            }
            for pid in product_ids
        }
    }
