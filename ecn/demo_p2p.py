"""
demo_p2p.py
-----------
Full ECN demonstration combining all three production upgrades:

  GAP 1 — Real Networking   : nodes communicate over real asyncio TCP sockets
  GAP 2 — Cryptographic Trust: every result is signed with Ed25519; bad sigs
                               are rejected before consensus
  GAP 3 — Domain Use Case   : supply chain verification (ship / receive /
                               inspect / quarantine / release)

Run with:
    python -m ecn.demo_p2p
"""

import asyncio

from ecn.use_cases.supply_chain import register_supply_chain_handlers, make_initial_state
from ecn.p2p_network import P2PNetwork


# ---------------------------------------------------------------------------
# Register domain handlers (supply chain extends the generic engine)
# ---------------------------------------------------------------------------
register_supply_chain_handlers()


# ---------------------------------------------------------------------------
# Initial supply chain state
# ---------------------------------------------------------------------------
PRODUCTS = ["LAPTOP-001", "PHONE-002"]


# ---------------------------------------------------------------------------
# Scenario A — Normal supply chain flow (all honest nodes)
# ---------------------------------------------------------------------------

async def scenario_a():
    print("\n" + "#" * 65)
    print("# SCENARIO A — Supply Chain: Normal Flow (5 honest nodes)")
    print("#   GAP1: real TCP | GAP2: Ed25519 signatures | GAP3: supply chain")
    print("#" * 65)

    node_configs = [
        {"node_id": "Warehouse"},
        {"node_id": "Shipper"},
        {"node_id": "Customs"},
        {"node_id": "Insurer"},
        {"node_id": "Retailer"},
    ]

    network = await P2PNetwork.create(
        node_configs=node_configs,
        initial_state=make_initial_state(PRODUCTS),
        base_port=19100,
        verbose=True,
    )

    try:
        transactions = [
            # Factory ships LAPTOP-001 to the port warehouse
            {"type": "ship",    "product_id": "LAPTOP-001", "destination": "port_warehouse",  "shipper": "DHL"},
            # Quality inspection at warehouse
            {"type": "inspect", "product_id": "LAPTOP-001", "check_name": "physical_integrity", "inspector": "QA-Lab"},
            # Received at customs
            {"type": "receive", "product_id": "LAPTOP-001", "receiver": "customs_agent",        "at_customs": True},
            # Customs clears and ships to retailer
            {"type": "ship",    "product_id": "LAPTOP-001", "destination": "retailer_depot",    "shipper": "FedEx"},
            {"type": "receive", "product_id": "LAPTOP-001", "receiver": "retailer",              "at_customs": False},
        ]

        for tx in transactions:
            results, cr = await network.broadcast(tx)
            assert cr.consensus_reached, f"Consensus failed for: {tx}"
            assert len(cr.faulty_nodes) == 0, f"Unexpected fault for: {tx}"

        print("\n✅  Scenario A passed: full supply chain tracked, all nodes agreed.")

    finally:
        await network.shutdown()


# ---------------------------------------------------------------------------
# Scenario B — Fault injection: one node tampers with a result
# ---------------------------------------------------------------------------

async def scenario_b():
    print("\n" + "#" * 65)
    print("# SCENARIO B — Supply Chain: Fault Injection (1 malicious node)")
    print("#   Malicious node reports a different hash → detected by consensus")
    print("#" * 65)

    node_configs = [
        {"node_id": "Warehouse"},
        {"node_id": "Shipper"},
        {"node_id": "Customs"},
        {"node_id": "Insurer"},
        {"node_id": "Auditor",   "malicious": True},   # ← tampers with hash
    ]

    network = await P2PNetwork.create(
        node_configs=node_configs,
        initial_state=make_initial_state(PRODUCTS),
        base_port=19200,
        verbose=True,
    )

    try:
        tx = {"type": "ship", "product_id": "PHONE-002", "destination": "airport", "shipper": "AirFreight"}
        results, cr = await network.broadcast(tx)

        assert cr.consensus_reached, "Consensus should be reached (4 honest vs 1 malicious)"
        assert "Auditor" in cr.faulty_nodes, "Malicious Auditor should be flagged"
        print("\n✅  Scenario B passed: malicious node detected and flagged.")

    finally:
        await network.shutdown()


# ---------------------------------------------------------------------------
# Scenario C — Signature forgery: a node result with no signature is rejected
# ---------------------------------------------------------------------------

async def scenario_c():
    print("\n" + "#" * 65)
    print("# SCENARIO C — Signature Forgery: unsigned node result rejected")
    print("#   One node has no signing key → result has no signature")
    print("#   Consensus with signature verification rejects it")
    print("#" * 65)

    from ecn.node import Node
    from ecn.network import Network
    from ecn.crypto import generate_keypair, public_key_to_hex

    initial = make_initial_state(PRODUCTS)

    # Build nodes: 2 signed honest, 1 unsigned (no signing key)
    priv1, pub1 = generate_keypair()
    priv2, pub2 = generate_keypair()
    # Node-3 deliberately has no signing key
    node1 = Node("Node-1", initial, signing_key=priv1)
    node2 = Node("Node-2", initial, signing_key=priv2)
    node3 = Node("Node-3", initial, signing_key=None)   # ← no key

    public_keys = {"Node-1": pub1, "Node-2": pub2}
    # Node-3 is not in the known-keys registry either

    network = Network([node1, node2, node3], verbose=True, public_keys=public_keys)

    tx = {"type": "inspect", "product_id": "LAPTOP-001", "check_name": "label_check", "inspector": "Compliance"}
    results, cr = network.broadcast(tx)

    assert "Node-3" in cr.invalid_sig_nodes, "Node-3 (unsigned) should be in invalid_sig_nodes"
    print(f"\n  Invalid-sig nodes: {cr.invalid_sig_nodes}")
    print("\n✅  Scenario C passed: unsigned result correctly rejected.")


# ---------------------------------------------------------------------------
# Scenario D — Quarantine flow
# ---------------------------------------------------------------------------

async def scenario_d():
    print("\n" + "#" * 65)
    print("# SCENARIO D — Supply Chain: Quarantine & Release Flow")
    print("#" * 65)

    node_configs = [{"node_id": f"Node-{i+1}"} for i in range(3)]
    network = await P2PNetwork.create(
        node_configs=node_configs,
        initial_state=make_initial_state(["GOODS-X"]),
        base_port=19400,
        verbose=True,
    )

    try:
        steps = [
            {"type": "ship",       "product_id": "GOODS-X", "destination": "port",     "shipper": "Ocean"},
            {"type": "quarantine", "product_id": "GOODS-X", "reason": "damaged_packaging"},
            {"type": "release",    "product_id": "GOODS-X", "released_by": "inspector"},
        ]
        for tx in steps:
            results, cr = await network.broadcast(tx)
            assert cr.consensus_reached, f"Consensus failed for: {tx}"

        print("\n✅  Scenario D passed: quarantine and release flow completed.")

    finally:
        await network.shutdown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    await scenario_a()
    await scenario_b()
    await scenario_c()
    await scenario_d()
    print("\n🎉  All P2P ECN scenarios completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
