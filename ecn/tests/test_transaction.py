"""
test_transaction.py
-------------------
Tests for ecn.transaction — the ECN Core Transaction Lifecycle module.
"""
import pytest
from ecn.transaction import (
    TxState,
    TransactionRecord,
    InvalidTransitionError,
    new_record,
    transition,
)


class TestTxState:
    def test_all_states_defined(self):
        states = [s.value for s in TxState]
        assert "INITIATED" in states
        assert "EXECUTING" in states
        assert "PROPOSED" in states
        assert "CONSENSUS_PENDING" in states
        assert "FINALIZED" in states
        assert "BILLED" in states
        assert "REPLICATED" in states

    def test_state_is_string_enum(self):
        assert TxState.INITIATED == "INITIATED"


class TestNewRecord:
    def test_creates_initiated_record(self):
        rec = new_record(tenant_id="acme")
        assert rec.state == TxState.INITIATED

    def test_generates_uuid_tx_id(self):
        rec = new_record(tenant_id="acme")
        assert len(rec.tx_id) == 36  # UUID4 format

    def test_explicit_tx_id(self):
        rec = new_record(tenant_id="acme", tx_id="custom-id")
        assert rec.tx_id == "custom-id"

    def test_sets_tenant_id(self):
        rec = new_record(tenant_id="acme")
        assert rec.tenant_id == "acme"

    def test_sets_node_count(self):
        rec = new_record(tenant_id="acme", node_count=5)
        assert rec.node_count == 5

    def test_default_values(self):
        rec = new_record(tenant_id="acme")
        assert rec.consensus_reached is False
        assert rec.fault_count == 0
        assert rec.duration_ms == 0
        assert rec.billed_amount == 0.0
        assert rec.finalized_at is None

    def test_created_at_is_set(self):
        rec = new_record(tenant_id="acme")
        assert rec.created_at.endswith("Z")


class TestTransition:
    def test_valid_full_lifecycle(self):
        rec = new_record(tenant_id="acme")
        states = [
            TxState.EXECUTING,
            TxState.PROPOSED,
            TxState.CONSENSUS_PENDING,
            TxState.FINALIZED,
            TxState.BILLED,
            TxState.REPLICATED,
        ]
        for state in states:
            rec = transition(rec, state)
            assert rec.state == state

    def test_returns_new_record(self):
        rec = new_record(tenant_id="acme")
        rec2 = transition(rec, TxState.EXECUTING)
        assert rec2 is not rec
        assert rec.state == TxState.INITIATED  # original unchanged
        assert rec2.state == TxState.EXECUTING

    def test_sets_finalized_at_on_finalized(self):
        rec = new_record(tenant_id="acme")
        rec = transition(rec, TxState.EXECUTING)
        rec = transition(rec, TxState.PROPOSED)
        rec = transition(rec, TxState.CONSENSUS_PENDING)
        rec = transition(rec, TxState.FINALIZED)
        assert rec.finalized_at is not None
        assert rec.finalized_at.endswith("Z")

    def test_sets_duration_ms_on_finalized(self):
        rec = new_record(tenant_id="acme")
        rec = transition(rec, TxState.EXECUTING)
        rec = transition(rec, TxState.PROPOSED)
        rec = transition(rec, TxState.CONSENSUS_PENDING)
        rec = transition(rec, TxState.FINALIZED)
        assert rec.duration_ms >= 0

    def test_invalid_transition_raises(self):
        rec = new_record(tenant_id="acme")
        with pytest.raises(InvalidTransitionError):
            transition(rec, TxState.FINALIZED)  # skip steps

    def test_cannot_transition_from_replicated(self):
        rec = new_record(tenant_id="acme")
        for s in [TxState.EXECUTING, TxState.PROPOSED, TxState.CONSENSUS_PENDING,
                  TxState.FINALIZED, TxState.BILLED, TxState.REPLICATED]:
            rec = transition(rec, s)
        with pytest.raises(InvalidTransitionError):
            transition(rec, TxState.INITIATED)

    def test_cannot_skip_states(self):
        rec = new_record(tenant_id="acme")
        with pytest.raises(InvalidTransitionError):
            transition(rec, TxState.PROPOSED)

    def test_cannot_go_backwards(self):
        rec = new_record(tenant_id="acme")
        rec = transition(rec, TxState.EXECUTING)
        with pytest.raises(InvalidTransitionError):
            transition(rec, TxState.INITIATED)


class TestTransactionRecordToDict:
    def test_to_dict_has_required_keys(self):
        rec = new_record(tenant_id="acme", node_count=3)
        d = rec.to_dict()
        required = [
            "tx_id", "tenant_id", "state", "node_count",
            "consensus_reached", "fault_count", "duration_ms",
            "billed_amount", "created_at", "finalized_at",
        ]
        for key in required:
            assert key in d

    def test_state_serialized_as_string(self):
        rec = new_record(tenant_id="acme")
        d = rec.to_dict()
        assert d["state"] == "INITIATED"
        assert isinstance(d["state"], str)
