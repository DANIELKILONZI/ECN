"""
tests/test_supply_chain.py
--------------------------
Unit tests for ecn.use_cases.supply_chain.
"""

import copy
import pytest

from ecn.use_cases.supply_chain import (
    register_supply_chain_handlers,
    make_initial_state,
)
from ecn.execution_engine import execute

# Register handlers once for the test module
register_supply_chain_handlers()

PRODUCTS = ["LAPTOP-001", "PHONE-002"]


def fresh_state():
    return make_initial_state(PRODUCTS)


class TestMakeInitialState:
    def test_products_present(self):
        state = make_initial_state(["A", "B"])
        assert "A" in state["products"]
        assert "B" in state["products"]

    def test_initial_status_manufactured(self):
        state = fresh_state()
        for pid in PRODUCTS:
            assert state["products"][pid]["status"] == "manufactured"
            assert state["products"][pid]["location"] == "factory"
            assert state["products"][pid]["owner"] == "manufacturer"
            assert state["products"][pid]["checks"] == []


class TestShip:
    def test_basic_ship(self):
        tx = {"type": "ship", "product_id": "LAPTOP-001", "destination": "warehouse", "shipper": "DHL"}
        new = execute(fresh_state(), tx)
        p = new["products"]["LAPTOP-001"]
        assert p["status"] == "in_transit"
        assert p["location"] == "warehouse"
        assert p["owner"] == "DHL"

    def test_ship_in_transit_raises(self):
        state = fresh_state()
        tx = {"type": "ship", "product_id": "LAPTOP-001", "destination": "port", "shipper": "DHL"}
        state = execute(state, tx)
        with pytest.raises(ValueError, match="in_transit"):
            execute(state, tx)

    def test_ship_unknown_product_raises(self):
        tx = {"type": "ship", "product_id": "UNKNOWN", "destination": "port", "shipper": "DHL"}
        with pytest.raises(ValueError, match="not found"):
            execute(fresh_state(), tx)

    def test_ship_does_not_mutate_input(self):
        state = fresh_state()
        tx = {"type": "ship", "product_id": "LAPTOP-001", "destination": "port", "shipper": "DHL"}
        execute(state, tx)
        assert state["products"]["LAPTOP-001"]["status"] == "manufactured"


class TestReceive:
    def _shipped_state(self):
        state = fresh_state()
        tx = {"type": "ship", "product_id": "LAPTOP-001", "destination": "port", "shipper": "DHL"}
        return execute(state, tx)

    def test_receive_as_delivered(self):
        state = self._shipped_state()
        tx = {"type": "receive", "product_id": "LAPTOP-001", "receiver": "buyer", "at_customs": False}
        new = execute(state, tx)
        assert new["products"]["LAPTOP-001"]["status"] == "delivered"
        assert new["products"]["LAPTOP-001"]["owner"] == "buyer"

    def test_receive_at_customs(self):
        state = self._shipped_state()
        tx = {"type": "receive", "product_id": "LAPTOP-001", "receiver": "customs", "at_customs": True}
        new = execute(state, tx)
        assert new["products"]["LAPTOP-001"]["status"] == "at_customs"

    def test_receive_non_in_transit_raises(self):
        tx = {"type": "receive", "product_id": "LAPTOP-001", "receiver": "buyer", "at_customs": False}
        with pytest.raises(ValueError, match="Cannot receive"):
            execute(fresh_state(), tx)


class TestInspect:
    def test_inspect_adds_check(self):
        tx = {"type": "inspect", "product_id": "LAPTOP-001", "check_name": "electrical_safety", "inspector": "Lab"}
        new = execute(fresh_state(), tx)
        assert "electrical_safety" in new["products"]["LAPTOP-001"]["checks"]

    def test_inspect_duplicate_check_not_duplicated(self):
        state = fresh_state()
        tx = {"type": "inspect", "product_id": "LAPTOP-001", "check_name": "label", "inspector": "Lab"}
        state = execute(state, tx)
        state = execute(state, tx)
        assert state["products"]["LAPTOP-001"]["checks"].count("label") == 1

    def test_inspect_checks_are_sorted(self):
        state = fresh_state()
        state = execute(state, {"type": "inspect", "product_id": "LAPTOP-001", "check_name": "zzz", "inspector": "L"})
        state = execute(state, {"type": "inspect", "product_id": "LAPTOP-001", "check_name": "aaa", "inspector": "L"})
        checks = state["products"]["LAPTOP-001"]["checks"]
        assert checks == sorted(checks)


class TestQuarantine:
    def test_quarantine_sets_status(self):
        tx = {"type": "quarantine", "product_id": "LAPTOP-001", "reason": "damaged"}
        new = execute(fresh_state(), tx)
        p = new["products"]["LAPTOP-001"]
        assert p["status"] == "quarantined"
        assert p["quarantine_reason"] == "damaged"

    def test_quarantine_already_quarantined_raises(self):
        state = execute(fresh_state(), {"type": "quarantine", "product_id": "LAPTOP-001", "reason": "test"})
        with pytest.raises(ValueError, match="already quarantined"):
            execute(state, {"type": "quarantine", "product_id": "LAPTOP-001", "reason": "again"})

    def test_ship_quarantined_raises(self):
        state = execute(fresh_state(), {"type": "quarantine", "product_id": "LAPTOP-001", "reason": "test"})
        with pytest.raises(ValueError, match="quarantined"):
            execute(state, {"type": "ship", "product_id": "LAPTOP-001", "destination": "port", "shipper": "DHL"})


class TestRelease:
    def _quarantined_state(self):
        return execute(
            fresh_state(),
            {"type": "quarantine", "product_id": "LAPTOP-001", "reason": "test"},
        )

    def test_release_sets_at_customs(self):
        state = self._quarantined_state()
        new = execute(state, {"type": "release", "product_id": "LAPTOP-001", "released_by": "inspector"})
        assert new["products"]["LAPTOP-001"]["status"] == "at_customs"
        assert "quarantine_reason" not in new["products"]["LAPTOP-001"]

    def test_release_non_quarantined_raises(self):
        with pytest.raises(ValueError, match="not quarantined"):
            execute(fresh_state(), {"type": "release", "product_id": "LAPTOP-001", "released_by": "inspector"})


class TestDeterminism:
    def test_same_tx_same_result(self):
        tx = {"type": "ship", "product_id": "PHONE-002", "destination": "port", "shipper": "DHL"}
        r1 = execute(fresh_state(), tx)
        r2 = execute(fresh_state(), tx)
        assert r1 == r2
