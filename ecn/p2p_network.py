"""
p2p_network.py
--------------
Real asyncio TCP network client — GAP 1.

The P2PNetwork class:
  1. Spawns multiple NodeServer instances as in-process asyncio servers
     (each on its own localhost TCP port).
  2. Connects to every server to broadcast a transaction.
  3. Collects JSON SignedResult responses.
  4. Verifies Ed25519 signatures.
  5. Runs consensus over verified results.

This demonstrates real node-to-node communication over TCP while keeping the
demo self-contained (no external processes required).  In production each
NodeServer would run in its own process or container.

Usage:
    network = await P2PNetwork.create(
        node_configs=[
            {"node_id": "Node-1", "port": 9001},
            {"node_id": "Node-2", "port": 9002},
        ],
        initial_state={"balances": {"A": 1000, "B": 500}},
    )
    results, cr = await network.broadcast(tx)
    await network.shutdown()
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Tuple

from ecn.node_server import NodeServer
from ecn.node import NodeResult
from ecn.consensus import ConsensusResult, run_consensus
from ecn.crypto import public_key_from_hex, verify_result, SignedResult
from ecn.execution_engine import Transaction, State

logger = logging.getLogger(__name__)

# Short timeout for local TCP connections (seconds)
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0


class P2PNetwork:
    """
    Real TCP network of ECN nodes.

    Each node runs as an independent asyncio TCP server.  The broadcaster
    connects to every server, sends the transaction, and collects signed
    results over real TCP sockets.

    Parameters
    ----------
    servers : list[NodeServer]
        Pre-started NodeServer instances.
    verbose : bool
        When *True*, print results to stdout after each broadcast.
    """

    def __init__(self, servers: List[NodeServer], verbose: bool = True) -> None:
        self._servers = servers
        self.verbose = verbose
        # Build node_id -> public_key mapping for signature verification
        self._public_keys: Dict[str, object] = {
            s.node_id: s.public_key for s in servers
        }
        self._history: List[Tuple[Transaction, List[NodeResult], ConsensusResult]] = []

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        node_configs: List[Dict],
        initial_state: State,
        base_port: int = 19000,
        verbose: bool = True,
    ) -> "P2PNetwork":
        """
        Create and start all node servers, then return a connected P2PNetwork.

        Parameters
        ----------
        node_configs : list[dict]
            Each dict may contain:
              - ``node_id`` (str, required)
              - ``port`` (int, optional — auto-assigned if omitted)
              - ``malicious`` (bool, optional)
        initial_state : State
        base_port : int
            Starting port for auto-assignment.
        verbose : bool
        """
        servers: List[NodeServer] = []
        for i, cfg in enumerate(node_configs):
            node_id = cfg["node_id"]
            port = cfg.get("port", base_port + i)
            malicious = cfg.get("malicious", False)
            server = NodeServer(
                node_id=node_id,
                initial_state=initial_state,
                port=port,
                malicious=malicious,
            )
            await server.start()
            servers.append(server)

        # Brief pause so all servers are ready to accept connections
        await asyncio.sleep(0.05)
        return cls(servers=servers, verbose=verbose)

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(
        self, tx: Transaction
    ) -> Tuple[List[NodeResult], ConsensusResult]:
        """
        Send *tx* to every node server over real TCP, collect signed results,
        verify signatures, and run consensus.

        Returns
        -------
        (results, consensus_result)
        """
        tasks = [self._send_to_server(s, tx) for s in self._servers]
        raw_responses = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[NodeResult] = []
        for server, resp in zip(self._servers, raw_responses):
            if isinstance(resp, Exception):
                logger.warning("Server %s failed: %s", server.node_id, resp)
                continue
            results.append(resp)

        consensus_result = run_consensus(results, public_keys=self._public_keys)
        self._history.append((tx, results, consensus_result))

        if self.verbose:
            self._print_round(tx, results, consensus_result)

        return results, consensus_result

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop all node servers."""
        for server in self._servers:
            await server.stop()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _send_to_server(
        self, server: NodeServer, tx: Transaction
    ) -> NodeResult:
        """Open a TCP connection, send tx, read the signed result."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(server.host, server.port),
            timeout=_CONNECT_TIMEOUT,
        )
        try:
            writer.write((json.dumps(tx) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
            data = json.loads(line.decode("utf-8").strip())

            if "error" in data:
                raise RuntimeError(f"Node {server.node_id} returned error: {data['error']}")

            return NodeResult(
                node_id=data["node_id"],
                state_hash=data["state_hash"],
                result_state={},        # not transmitted over wire (saves bandwidth)
                trace_entry={},
                signature=data.get("signature"),
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _print_round(
        tx: Transaction,
        results: List[NodeResult],
        cr: ConsensusResult,
    ) -> None:
        """Pretty-print the round output to stdout."""
        print("\n" + "=" * 60)
        print(f"[P2P] Transaction: {tx}")
        print("-" * 60)
        for r in results:
            flags = []
            if r.node_id in cr.invalid_sig_nodes:
                flags.append("INVALID SIG")
            elif r.node_id in cr.faulty_nodes:
                flags.append("FAULT DETECTED")
            flag_str = "  ← " + ", ".join(flags) if flags else ""
            sig_str = f"  sig={r.signature[:12]}..." if r.signature else "  (unsigned)"
            print(f"  {r.node_id:<12} → hash: {r.state_hash}{sig_str}{flag_str}")
        print("-" * 60)
        if cr.consensus_reached:
            print(f"Consensus    → {cr.agreed_hash}")
        else:
            print(f"Consensus    → FAILED (no strict majority)")
        if cr.faulty_nodes:
            print(f"Faulty nodes → {cr.faulty_nodes}")
        else:
            print("Faulty nodes → none")
        print("=" * 60)
