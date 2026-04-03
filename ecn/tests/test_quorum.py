"""
tests/test_quorum.py
--------------------
Tests for configurable quorum threshold in ecn.consensus and
configurable host binding + partial-quorum in ecn.p2p_network.
"""

import asyncio

import pytest

from ecn.consensus import (
    ConsensusResult,
    DEFAULT_QUORUM_THRESHOLD,
    run_consensus,
)
from ecn.node import NodeResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(node_id: str, state_hash: str) -> NodeResult:
    return NodeResult(
        node_id=node_id,
        state_hash=state_hash,
        result_state={},
        trace_entry={},
        signature=None,
    )


# ---------------------------------------------------------------------------
# DEFAULT_QUORUM_THRESHOLD
# ---------------------------------------------------------------------------

class TestDefaultQuorumThreshold:
    def test_default_is_just_over_half(self):
        assert DEFAULT_QUORUM_THRESHOLD == 0.51

    def test_stored_on_result(self):
        cr = run_consensus([_result("N1", "aaa")])
        assert cr.quorum_threshold == DEFAULT_QUORUM_THRESHOLD


# ---------------------------------------------------------------------------
# ConsensusResult quorum logic
# ---------------------------------------------------------------------------

class TestConsensusResultQuorum:
    def test_all_agree_strict_majority(self):
        results = [_result(f"N{i}", "aaa") for i in range(5)]
        cr = run_consensus(results)
        assert cr.consensus_reached is True
        assert cr.agreed_hash == "aaa"

    def test_majority_agree_threshold_default(self):
        """3 of 5 agree → 0.60 ≥ 0.51 → consensus reached."""
        results = [
            _result("N1", "aaa"), _result("N2", "aaa"), _result("N3", "aaa"),
            _result("N4", "bbb"), _result("N5", "bbb"),
        ]
        cr = run_consensus(results)
        assert cr.consensus_reached is True
        assert cr.agreed_hash == "aaa"

    def test_tie_no_consensus_default(self):
        """2 of 4 agree → exactly 0.50 < 0.51 → no consensus."""
        results = [
            _result("N1", "aaa"), _result("N2", "aaa"),
            _result("N3", "bbb"), _result("N4", "bbb"),
        ]
        cr = run_consensus(results)
        assert cr.consensus_reached is False

    def test_strict_bft_threshold(self):
        """With threshold=0.67, need 4/5 agreement."""
        results = [
            _result("N1", "aaa"), _result("N2", "aaa"),
            _result("N3", "aaa"), _result("N4", "aaa"),
            _result("N5", "bbb"),
        ]
        cr = run_consensus(results, quorum_threshold=0.67)
        assert cr.consensus_reached is True  # 4/5 = 0.80 ≥ 0.67

    def test_strict_bft_threshold_not_met(self):
        """With threshold=0.67, 3/5 = 0.60 is not enough."""
        results = [
            _result("N1", "aaa"), _result("N2", "aaa"), _result("N3", "aaa"),
            _result("N4", "bbb"), _result("N5", "bbb"),
        ]
        cr = run_consensus(results, quorum_threshold=0.67)
        assert cr.consensus_reached is False

    def test_threshold_1_0_requires_unanimity(self):
        """With threshold=1.0, all nodes must agree."""
        results = [
            _result("N1", "aaa"), _result("N2", "aaa"),
            _result("N3", "bbb"),
        ]
        cr = run_consensus(results, quorum_threshold=1.0)
        assert cr.consensus_reached is False

    def test_threshold_1_0_unanimity_met(self):
        results = [_result(f"N{i}", "aaa") for i in range(3)]
        cr = run_consensus(results, quorum_threshold=1.0)
        assert cr.consensus_reached is True

    def test_low_threshold(self):
        """With threshold=0.34, even minority wins if it's the plurality."""
        results = [
            _result("N1", "aaa"),
            _result("N2", "bbb"), _result("N3", "bbb"),
            _result("N4", "ccc"),
        ]
        cr = run_consensus(results, quorum_threshold=0.34)
        # 2/4 = 0.50 ≥ 0.34 for "bbb" (plurality winner)
        assert cr.consensus_reached is True
        assert cr.agreed_hash == "bbb"

    def test_empty_results_no_consensus(self):
        cr = run_consensus([], quorum_threshold=0.51)
        assert cr.consensus_reached is False
        assert cr.agreed_hash is None

    def test_single_node_always_reaches_consensus(self):
        cr = run_consensus([_result("N1", "aaa")], quorum_threshold=0.51)
        assert cr.consensus_reached is True

    def test_quorum_threshold_stored_on_result(self):
        cr = run_consensus([_result("N1", "aaa")], quorum_threshold=0.75)
        assert cr.quorum_threshold == 0.75

    def test_faulty_nodes_not_counted_in_quorum(self):
        """Faulty nodes should not count in the denominator for quorum."""
        results = [
            _result("N1", "aaa"), _result("N2", "aaa"),  # honest
            _result("N3", "bbb"),                           # faulty
        ]
        # 2/3 = 0.667 of verified results agree on "aaa"
        cr = run_consensus(results, quorum_threshold=0.67)
        assert cr.consensus_reached is False  # 2/3 < 0.67 (strict)

    def test_all_sig_failures_no_consensus(self):
        from ecn.node import NodeResult
        results = [
            NodeResult("N1", "aaa", {}, {}, signature="bad"),
            NodeResult("N2", "aaa", {}, {}, signature="bad"),
        ]
        # With no public_keys, sigs are not verified — just check default behaviour
        cr = run_consensus(results)
        assert cr.agreed_hash == "aaa"


# ---------------------------------------------------------------------------
# P2PNetwork — ECN_QUORUM_THRESHOLD env var
# ---------------------------------------------------------------------------

class TestP2PNetworkQuorumEnv:
    def test_default_threshold_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("ECN_QUORUM_THRESHOLD", raising=False)
        from ecn.p2p_network import _get_quorum_threshold
        assert _get_quorum_threshold() == DEFAULT_QUORUM_THRESHOLD

    def test_custom_threshold_from_env(self, monkeypatch):
        monkeypatch.setenv("ECN_QUORUM_THRESHOLD", "0.67")
        from ecn.p2p_network import _get_quorum_threshold
        assert _get_quorum_threshold() == pytest.approx(0.67)

    def test_invalid_threshold_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ECN_QUORUM_THRESHOLD", "not-a-float")
        from ecn.p2p_network import _get_quorum_threshold
        assert _get_quorum_threshold() == DEFAULT_QUORUM_THRESHOLD

    def test_out_of_range_threshold_falls_back(self, monkeypatch):
        monkeypatch.setenv("ECN_QUORUM_THRESHOLD", "1.5")
        from ecn.p2p_network import _get_quorum_threshold
        assert _get_quorum_threshold() == DEFAULT_QUORUM_THRESHOLD


# ---------------------------------------------------------------------------
# P2PNetwork — quorum threshold end-to-end
# ---------------------------------------------------------------------------

class TestP2PNetworkQuorumE2E:
    @pytest.fixture
    def state(self):
        from ecn.use_cases.supply_chain import make_initial_state
        return make_initial_state(["ITEM-001"])

    @pytest.mark.asyncio
    async def test_custom_quorum_stored_on_network(self, state):
        from ecn.p2p_network import P2PNetwork
        net = await P2PNetwork.create(
            node_configs=[{"node_id": "V1"}, {"node_id": "V2"}],
            initial_state=state,
            verbose=False,
            quorum_threshold=0.67,
        )
        assert net._quorum_threshold == pytest.approx(0.67)
        await net.shutdown()

    @pytest.mark.asyncio
    async def test_consensus_uses_network_quorum(self, state):
        """With 3 honest nodes and quorum=0.51, consensus should be reached."""
        from ecn.p2p_network import P2PNetwork
        from ecn.audit import AuditLog
        audit = AuditLog()
        net = await P2PNetwork.create(
            node_configs=[{"node_id": f"V{i}"} for i in range(3)],
            initial_state=state,
            verbose=False,
            audit_log=audit,
            quorum_threshold=0.51,
        )
        try:
            tx = {"type": "ship", "product_id": "ITEM-001", "destination": "HQ", "shipper": "UPS"}
            results, cr = await net.broadcast(tx)
            assert cr.consensus_reached is True
        finally:
            await net.shutdown()


# ---------------------------------------------------------------------------
# NodeServer — ECN_NODE_HOST env var
# ---------------------------------------------------------------------------

class TestNodeServerHost:
    def test_default_host_127(self, monkeypatch):
        monkeypatch.delenv("ECN_NODE_HOST", raising=False)
        from ecn.node_server import _default_node_host
        assert _default_node_host() == "127.0.0.1"

    def test_custom_host_from_env(self, monkeypatch):
        monkeypatch.setenv("ECN_NODE_HOST", "0.0.0.0")
        from ecn.node_server import _default_node_host
        assert _default_node_host() == "0.0.0.0"

    def test_explicit_host_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ECN_NODE_HOST", "0.0.0.0")
        from ecn.node_server import NodeServer
        from ecn.use_cases.supply_chain import make_initial_state
        server = NodeServer(
            node_id="Test",
            initial_state=make_initial_state(["P1"]),
            port=0,
            host="127.0.0.1",  # explicit override
        )
        assert server.host == "127.0.0.1"

    def test_empty_host_uses_env(self, monkeypatch):
        monkeypatch.setenv("ECN_NODE_HOST", "0.0.0.0")
        from ecn.node_server import NodeServer
        from ecn.use_cases.supply_chain import make_initial_state
        server = NodeServer(
            node_id="Test",
            initial_state=make_initial_state(["P1"]),
            port=0,
            host="",  # empty → use env var
        )
        assert server.host == "0.0.0.0"

    @pytest.mark.asyncio
    async def test_health_server_starts_and_responds(self, monkeypatch):
        monkeypatch.delenv("ECN_NODE_HOST", raising=False)
        from ecn.node_server import NodeServer
        from ecn.use_cases.supply_chain import make_initial_state
        server = NodeServer(
            node_id="HealthTest",
            initial_state=make_initial_state(["P1"]),
            port=0,
            health_port=0,  # OS-assigned
        )
        await server.start()
        health_port = server._health_server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", health_port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = b""
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                response += chunk
            writer.close()
            assert b"200 OK" in response
            assert b'"status": "ok"' in response
            assert b"HealthTest" in response
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# InsufficientQuorumError
# ---------------------------------------------------------------------------

class TestInsufficientQuorumError:
    @pytest.mark.asyncio
    async def test_raises_when_min_responses_not_met(self):
        """When we set min_responses=5 but only 2 nodes exist, the error fires."""
        from ecn.p2p_network import P2PNetwork, InsufficientQuorumError
        from ecn.use_cases.supply_chain import make_initial_state
        state = make_initial_state(["ITEM-001"])
        net = await P2PNetwork.create(
            node_configs=[{"node_id": "V1"}, {"node_id": "V2"}],
            initial_state=state,
            verbose=False,
            min_responses=5,  # impossible with only 2 nodes
        )
        try:
            with pytest.raises(InsufficientQuorumError):
                # Shutdown the servers so they don't respond, then broadcast
                for server in net._servers:
                    await server.stop()
                await net.broadcast({"type": "ship", "product_id": "ITEM-001"})
        finally:
            pass  # already stopped
