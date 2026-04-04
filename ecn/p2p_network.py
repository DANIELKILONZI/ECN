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

Network interface binding
-------------------------
Set ``ECN_NODE_HOST=0.0.0.0`` (or any routable interface) so node servers
are reachable across pods/machines.  The default remains ``127.0.0.1`` for
backward compatibility with single-host deployments and tests.

Quorum configuration
--------------------
``ECN_QUORUM_THRESHOLD`` (float, 0 < t ≤ 1.0, default 0.51) controls the
minimum fraction of responding nodes that must agree for consensus to be
marked as reached.  Use 0.67 for classical 1/3 BFT tolerance.

Partial-quorum acceptance
--------------------------
``ECN_QUORUM_MIN_RESPONSES`` (int, default 1) — if fewer than this many
nodes respond, the broadcast raises ``InsufficientQuorumError`` instead of
silently proceeding with a single result.  Set to ``ceil(n * quorum_threshold)``
for strict enforcement.

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
import math
import os
import time
from typing import Dict, List, Optional, Tuple

from ecn.node_server import NodeServer
from ecn.node import NodeResult
from ecn.consensus import ConsensusResult, run_consensus, DEFAULT_QUORUM_THRESHOLD
from ecn.crypto import public_key_from_hex, verify_result, SignedResult
from ecn.execution_engine import Transaction, State
from ecn.node_health import NodeHealthRegistry, NodeState, init_health_registry

logger = logging.getLogger(__name__)

# Short timeout for local TCP connections (seconds)
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0


class InsufficientQuorumError(Exception):
    """Raised when fewer nodes respond than the configured minimum quorum."""


def _get_quorum_threshold() -> float:
    """Read ECN_QUORUM_THRESHOLD from env (falls back to DEFAULT_QUORUM_THRESHOLD)."""
    raw = os.environ.get("ECN_QUORUM_THRESHOLD", "").strip()
    if raw:
        try:
            val = float(raw)
            if 0 < val <= 1.0:
                return val
            logger.warning("ECN_QUORUM_THRESHOLD=%r out of range (0,1] — using default", raw)
        except ValueError:
            logger.warning("ECN_QUORUM_THRESHOLD=%r is not a float — using default", raw)
    return DEFAULT_QUORUM_THRESHOLD


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
    quorum_threshold : float
        Minimum fraction of responding nodes that must agree for consensus
        to be reached.  Reads from ``ECN_QUORUM_THRESHOLD`` env var when
        not explicitly provided.
    min_responses : int
        Minimum number of nodes that must respond.  Defaults to 1.
        Set to ``math.ceil(n * quorum_threshold)`` for strict enforcement.
    """

    def __init__(
        self,
        servers: List[NodeServer],
        verbose: bool = True,
        audit_log=None,
        quorum_threshold: Optional[float] = None,
        min_responses: Optional[int] = None,
        health_registry: Optional[NodeHealthRegistry] = None,
    ) -> None:
        self._servers = servers
        self.verbose = verbose
        self._audit_log = audit_log
        self._quorum_threshold = quorum_threshold if quorum_threshold is not None else _get_quorum_threshold()
        self._min_responses = min_responses if min_responses is not None else 1
        # Build node_id -> public_key mapping for signature verification
        self._public_keys: Dict[str, object] = {
            s.node_id: s.public_key for s in servers
        }
        self._history: List[Tuple[Transaction, List[NodeResult], ConsensusResult]] = []
        self._health: NodeHealthRegistry = health_registry or NodeHealthRegistry()
        # Pre-register all servers with the health registry
        for s in servers:
            self._health.get(s.node_id)

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
        audit_log=None,
        quorum_threshold: Optional[float] = None,
        min_responses: Optional[int] = None,
        health_registry: Optional[NodeHealthRegistry] = None,
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
              - ``host`` (str, optional — defaults to ECN_NODE_HOST env var or 127.0.0.1)
        initial_state : State
        base_port : int
            Starting port for auto-assignment.
        verbose : bool
        audit_log : AuditLog, optional
            When provided, every round is recorded as an AuditEvent.
        quorum_threshold : float, optional
            Override the consensus quorum threshold for this network.
        min_responses : int, optional
            Override the minimum response count for this network.
        """
        node_host = os.environ.get("ECN_NODE_HOST", "127.0.0.1").strip()
        servers: List[NodeServer] = []
        for i, cfg in enumerate(node_configs):
            node_id = cfg["node_id"]
            port = cfg.get("port", base_port + i)
            malicious = cfg.get("malicious", False)
            host = cfg.get("host", node_host)
            server = NodeServer(
                node_id=node_id,
                initial_state=initial_state,
                port=port,
                host=host,
                malicious=malicious,
            )
            await server.start()
            servers.append(server)

        # Brief pause so all servers are ready to accept connections
        await asyncio.sleep(0.05)
        return cls(
            servers=servers,
            verbose=verbose,
            audit_log=audit_log,
            quorum_threshold=quorum_threshold,
            min_responses=min_responses,
            health_registry=health_registry,
        )

    # ------------------------------------------------------------------
    # Health registry access
    # ------------------------------------------------------------------

    @property
    def health_registry(self) -> NodeHealthRegistry:
        """Return the NodeHealthRegistry for this network."""
        return self._health

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(
        self, tx: Transaction
    ) -> Tuple[List[NodeResult], ConsensusResult]:
        """
        Send *tx* to every node server over real TCP, collect signed results,
        verify signatures, and run consensus.

        Raises
        ------
        InsufficientQuorumError
            When fewer than ``min_responses`` nodes respond.

        Returns
        -------
        (results, consensus_result)
        """
        tasks = []
        participating_servers = []
        excluded_servers = []
        for s in self._servers:
            if self._health.get(s.node_id).state == NodeState.EXCLUDED:
                excluded_servers.append(s)
            else:
                participating_servers.append(s)
                tasks.append(self._send_to_server(s, tx))

        raw_responses = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[NodeResult] = []
        failed_nodes: List[str] = []
        for server, resp in zip(participating_servers, raw_responses):
            if isinstance(resp, Exception):
                logger.warning("Server %s failed: %s", server.node_id, resp)
                failed_nodes.append(server.node_id)
                self._health.record_failure(server.node_id)
                continue
            node_result, latency_ms = resp
            self._health.record_success(server.node_id, latency_ms=latency_ms)
            results.append(node_result)

        if excluded_servers:
            logger.info(
                "Adaptive quorum: skipping %d EXCLUDED nodes: %s",
                len(excluded_servers),
                [s.node_id for s in excluded_servers],
            )

        if failed_nodes:
            logger.info(
                "Partial-quorum round: %d/%d nodes responded (non-responding: %s)",
                len(results),
                len(self._servers),
                failed_nodes,
            )

        # Total denominator includes excluded nodes — quorum is checked against
        # the full network size so InsufficientQuorumError fires when too many
        # nodes are lost.
        if len(results) < self._min_responses:
            raise InsufficientQuorumError(
                f"Only {len(results)}/{len(self._servers)} nodes responded "
                f"(min_responses={self._min_responses})"
            )

        consensus_result = run_consensus(
            results,
            public_keys=self._public_keys,
            quorum_threshold=self._quorum_threshold,
        )
        self._history.append((tx, results, consensus_result))

        if self._audit_log is not None:
            self._audit_log.record(tx, results, consensus_result)

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

    def node_count(self) -> int:
        """Return the number of nodes in the network."""
        return len(self._servers)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _send_to_server(
        self, server: NodeServer, tx: Transaction
    ) -> NodeResult:
        """Open a TCP connection, send tx, read the signed result."""
        t0 = time.monotonic()
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

            latency_ms = (time.monotonic() - t0) * 1000.0
            result = NodeResult(
                node_id=data["node_id"],
                state_hash=data["state_hash"],
                result_state={},        # not transmitted over wire (saves bandwidth)
                trace_entry={},
                signature=data.get("signature"),
            )
            return result, latency_ms
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
