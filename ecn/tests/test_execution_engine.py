"""
tests/test_execution_engine.py
------------------------------
Unit tests for ecn.execution_engine.
"""

import copy
import pytest

from ecn.execution_engine import execute, register_handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_STATE = {
    "balances": {
        "A": 1000,
        "B": 500,
        "C": 250,
    }
}


def fresh_state():
    return copy.deepcopy(BASE_STATE)


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

class TestTransfer:
    def test_basic_transfer(self):
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 100}
        new = execute(fresh_state(), tx)
        assert new["balances"]["A"] == 900
        assert new["balances"]["B"] == 600
        assert new["balances"]["C"] == 250

    def test_transfer_does_not_mutate_input(self):
        state = fresh_state()
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 50}
        execute(state, tx)
        assert state["balances"]["A"] == 1000  # original unchanged

    def test_transfer_is_deterministic(self):
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 200}
        result1 = execute(fresh_state(), tx)
        result2 = execute(fresh_state(), tx)
        assert result1 == result2

    def test_transfer_insufficient_funds_raises(self):
        tx = {"type": "transfer", "from": "B", "to": "A", "amount": 9999}
        with pytest.raises(ValueError, match="Insufficient funds"):
            execute(fresh_state(), tx)

    def test_transfer_zero_amount_raises(self):
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": 0}
        with pytest.raises(ValueError, match="positive"):
            execute(fresh_state(), tx)

    def test_transfer_negative_amount_raises(self):
        tx = {"type": "transfer", "from": "A", "to": "B", "amount": -10}
        with pytest.raises(ValueError, match="positive"):
            execute(fresh_state(), tx)

    def test_transfer_unknown_sender_raises(self):
        tx = {"type": "transfer", "from": "Z", "to": "B", "amount": 10}
        with pytest.raises(ValueError, match="not found"):
            execute(fresh_state(), tx)

    def test_transfer_unknown_receiver_raises(self):
        tx = {"type": "transfer", "from": "A", "to": "Z", "amount": 10}
        with pytest.raises(ValueError, match="not found"):
            execute(fresh_state(), tx)

    def test_transfer_exact_balance(self):
        tx = {"type": "transfer", "from": "B", "to": "A", "amount": 500}
        new = execute(fresh_state(), tx)
        assert new["balances"]["B"] == 0
        assert new["balances"]["A"] == 1500


# ---------------------------------------------------------------------------
# mint
# ---------------------------------------------------------------------------

class TestMint:
    def test_basic_mint(self):
        tx = {"type": "mint", "to": "A", "amount": 500}
        new = execute(fresh_state(), tx)
        assert new["balances"]["A"] == 1500

    def test_mint_new_account(self):
        tx = {"type": "mint", "to": "NEW", "amount": 100}
        new = execute(fresh_state(), tx)
        assert new["balances"]["NEW"] == 100

    def test_mint_is_deterministic(self):
        tx = {"type": "mint", "to": "B", "amount": 200}
        assert execute(fresh_state(), tx) == execute(fresh_state(), tx)

    def test_mint_zero_raises(self):
        tx = {"type": "mint", "to": "A", "amount": 0}
        with pytest.raises(ValueError, match="positive"):
            execute(fresh_state(), tx)


# ---------------------------------------------------------------------------
# burn
# ---------------------------------------------------------------------------

class TestBurn:
    def test_basic_burn(self):
        tx = {"type": "burn", "from": "A", "amount": 200}
        new = execute(fresh_state(), tx)
        assert new["balances"]["A"] == 800

    def test_burn_entire_balance(self):
        tx = {"type": "burn", "from": "C", "amount": 250}
        new = execute(fresh_state(), tx)
        assert new["balances"]["C"] == 0

    def test_burn_insufficient_funds_raises(self):
        tx = {"type": "burn", "from": "C", "amount": 9999}
        with pytest.raises(ValueError, match="Insufficient funds to burn"):
            execute(fresh_state(), tx)

    def test_burn_zero_raises(self):
        tx = {"type": "burn", "from": "A", "amount": 0}
        with pytest.raises(ValueError, match="positive"):
            execute(fresh_state(), tx)


# ---------------------------------------------------------------------------
# Unknown transaction type
# ---------------------------------------------------------------------------

class TestUnknownType:
    def test_unknown_type_raises(self):
        tx = {"type": "unknown_op", "from": "A", "to": "B", "amount": 10}
        with pytest.raises(ValueError, match="Unknown transaction type"):
            execute(fresh_state(), tx)

    def test_missing_type_raises(self):
        tx = {"from": "A", "to": "B", "amount": 10}
        with pytest.raises(ValueError, match="Unknown transaction type"):
            execute(fresh_state(), tx)


# ---------------------------------------------------------------------------
# Pluggable handler
# ---------------------------------------------------------------------------

class TestPluggableHandler:
    def test_register_custom_handler(self):
        def _swap(state, tx):
            import copy
            s = copy.deepcopy(state)
            a, b = tx["a"], tx["b"]
            s["balances"][a], s["balances"][b] = (
                s["balances"][b],
                s["balances"][a],
            )
            return s

        register_handler("swap", _swap)
        tx = {"type": "swap", "a": "A", "b": "B"}
        new = execute(fresh_state(), tx)
        assert new["balances"]["A"] == 500
        assert new["balances"]["B"] == 1000
