"""
tests/test_p2p_network.py
-------------------------
Integration tests for the real TCP networking layer (ecn.p2p_network).

These tests spin up actual asyncio TCP servers and communicate over real
localhost sockets.
"""

import asyncio
import pytest

from ecn.p2p_network import P2PNetwork
from ecn.use_cases.supply_chain import register_supply_chain_handlers, make_initial_state

register_supply_chain_handlers()

INITIAL_STATE = {"balances": {"A": 1000, "B": 500, "C": 250}}
SUPPLY_STATE = make_initial_state(["ITEM-1", "ITEM-2"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run a coroutine in a new event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Basic connectivity tests
# ---------------------------------------------------------------------------

class TestP2PBasic:
    def test_single_honest_node(self):
        async def _test():
            net = await P2PNetwork.create(
                node_configs=[{"node_id": "Node-1"}],
                initial_state=INITIAL_STATE,
                base_port=19500,
                verbose=False,
            )
            try:
                tx = {"type": "transfer", "from": "A", "to": "B", "amount": 50}
                results, cr = await net.broadcast(tx)
                assert len(results) == 1
                assert results[0].node_id == "Node-1"
                assert cr.consensus_reached
                assert cr.faulty_nodes == []
            finally:
                await net.shutdown()
        run(_test())

    def test_all_honest_nodes_agree(self):
        async def _test():
            configs = [{"node_id": f"Node-{i+1}"} for i in range(4)]
            net = await P2PNetwork.create(
                node_configs=configs,
                initial_state=INITIAL_STATE,
                base_port=19510,
                verbose=False,
            )
            try:
                tx = {"type": "transfer", "from": "A", "to": "B", "amount": 100}
                results, cr = await net.broadcast(tx)
                assert cr.consensus_reached
                assert len(cr.faulty_nodes) == 0
                hashes = {r.state_hash for r in results}
                assert len(hashes) == 1
            finally:
                await net.shutdown()
        run(_test())

    def test_results_have_signatures(self):
        async def _test():
            net = await P2PNetwork.create(
                node_configs=[{"node_id": "Node-1"}, {"node_id": "Node-2"}],
                initial_state=INITIAL_STATE,
                base_port=19520,
                verbose=False,
            )
            try:
                tx = {"type": "transfer", "from": "A", "to": "B", "amount": 10}
                results, _ = await net.broadcast(tx)
                for r in results:
                    assert r.signature is not None
                    assert len(r.signature) == 128  # 64 bytes hex
            finally:
                await net.shutdown()
        run(_test())


# ---------------------------------------------------------------------------
# Fault detection
# ---------------------------------------------------------------------------

class TestP2PFaultDetection:
    def test_malicious_node_detected(self):
        async def _test():
            configs = [
                {"node_id": "Honest-1"},
                {"node_id": "Honest-2"},
                {"node_id": "Honest-3"},
                {"node_id": "Evil",    "malicious": True},
            ]
            net = await P2PNetwork.create(
                node_configs=configs,
                initial_state=INITIAL_STATE,
                base_port=19530,
                verbose=False,
            )
            try:
                tx = {"type": "transfer", "from": "A", "to": "B", "amount": 50}
                _, cr = await net.broadcast(tx)
                assert cr.consensus_reached
                assert "Evil" in cr.faulty_nodes
            finally:
                await net.shutdown()
        run(_test())

    def test_multiple_broadcasts(self):
        async def _test():
            configs = [{"node_id": f"Node-{i+1}"} for i in range(3)]
            net = await P2PNetwork.create(
                node_configs=configs,
                initial_state=INITIAL_STATE,
                base_port=19540,
                verbose=False,
            )
            try:
                for amount in [50, 100, 150]:
                    tx = {"type": "transfer", "from": "A", "to": "B", "amount": amount}
                    _, cr = await net.broadcast(tx)
                    assert cr.consensus_reached
                assert len(net._history) == 3
            finally:
                await net.shutdown()
        run(_test())


# ---------------------------------------------------------------------------
# Supply chain over P2P
# ---------------------------------------------------------------------------

class TestP2PSupplyChain:
    def test_ship_transaction_over_tcp(self):
        async def _test():
            configs = [{"node_id": f"Node-{i+1}"} for i in range(3)]
            net = await P2PNetwork.create(
                node_configs=configs,
                initial_state=make_initial_state(["ITEM-1"]),
                base_port=19550,
                verbose=False,
            )
            try:
                tx = {"type": "ship", "product_id": "ITEM-1", "destination": "port", "shipper": "DHL"}
                _, cr = await net.broadcast(tx)
                assert cr.consensus_reached
                assert len(cr.faulty_nodes) == 0
            finally:
                await net.shutdown()
        run(_test())
