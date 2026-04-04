"""
execution_engine.py
-------------------
Deterministic execution engine for the Executable Consensus Network (ECN).

Key design constraints:
- Pure function: execute(state, tx) -> new_state
- No randomness, no system time, no floating-point operations
- All arithmetic uses integers only
- Raises ValueError for invalid / unsupported transactions
"""

import copy
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
State = Dict[str, Any]
Transaction = Dict[str, Any]


# ---------------------------------------------------------------------------
# Transaction handlers (pluggable)
# ---------------------------------------------------------------------------

def _handle_transfer(state: State, tx: Transaction) -> State:
    """Transfer `amount` from one account to another."""
    sender = tx["from"]
    receiver = tx["to"]
    amount = int(tx["amount"])          # strict integer cast

    if amount <= 0:
        raise ValueError(f"Transfer amount must be positive, got {amount}")

    balances = state["balances"]

    if sender not in balances:
        raise ValueError(f"Sender '{sender}' not found in state")
    if receiver not in balances:
        raise ValueError(f"Receiver '{receiver}' not found in state")
    if balances[sender] < amount:
        raise ValueError(
            f"Insufficient funds: '{sender}' has {balances[sender]}, needs {amount}"
        )

    new_state = copy.deepcopy(state)
    new_state["balances"][sender] -= amount
    new_state["balances"][receiver] += amount
    return new_state


def _handle_mint(state: State, tx: Transaction) -> State:
    """Create new tokens and credit them to `to`."""
    receiver = tx["to"]
    amount = int(tx["amount"])

    if amount <= 0:
        raise ValueError(f"Mint amount must be positive, got {amount}")

    new_state = copy.deepcopy(state)
    balances = new_state["balances"]
    balances[receiver] = balances.get(receiver, 0) + amount
    return new_state


def _handle_burn(state: State, tx: Transaction) -> State:
    """Destroy tokens from `from`'s balance."""
    sender = tx["from"]
    amount = int(tx["amount"])

    if amount <= 0:
        raise ValueError(f"Burn amount must be positive, got {amount}")

    balances = state["balances"]
    if sender not in balances:
        raise ValueError(f"Account '{sender}' not found in state")
    if balances[sender] < amount:
        raise ValueError(
            f"Insufficient funds to burn: '{sender}' has {balances[sender]}, needs {amount}"
        )

    new_state = copy.deepcopy(state)
    new_state["balances"][sender] -= amount
    return new_state


# ---------------------------------------------------------------------------
# Handler registry (pluggable transaction types)
# ---------------------------------------------------------------------------

_HANDLERS = {
    "transfer": _handle_transfer,
    "mint": _handle_mint,
    "burn": _handle_burn,
}


def register_handler(tx_type: str, handler) -> None:
    """Register a custom transaction handler at runtime."""
    _HANDLERS[tx_type] = handler


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute(state: State, tx: Transaction) -> State:
    """
    Apply *tx* to *state* and return the resulting new state.

    This is a pure function:
      - The input state is never mutated.
      - Given the same (state, tx) inputs, the output is always identical.
      - No side effects, no I/O, no randomness, no floating-point math.

    Parameters
    ----------
    state : State
        Current world state (dict with at least a ``"balances"`` key).
    tx : Transaction
        A dict with at least a ``"type"`` key identifying the operation.

    Returns
    -------
    State
        A new state dict reflecting the applied transaction.

    Raises
    ------
    ValueError
        If the transaction type is unknown or the transaction is invalid.
    """
    tx_type = tx.get("type")
    if tx_type not in _HANDLERS:
        raise ValueError(f"Unknown transaction type: '{tx_type}'")

    handler = _HANDLERS[tx_type]
    return handler(state, tx)
