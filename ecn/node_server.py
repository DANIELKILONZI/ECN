"""
node_server.py
--------------
Real network layer — GAP 1.

Each ECN node runs as an independent asyncio TCP server process.
The server:
  1. Accepts a JSON-encoded transaction from any client.
  2. Executes it locally using its Node instance.
  3. Returns a JSON-encoded SignedResult back to the client.

Wire protocol (line-delimited JSON over TCP):
  → client sends:  <json transaction>\\n
  ← server sends:  <json signed-result>\\n

Network interface binding
--------------------------
Set ``ECN_NODE_HOST=0.0.0.0`` to bind to all interfaces so nodes are
reachable across pods/machines.  The default is ``127.0.0.1`` for
backward-compatible single-host deployments.

Health check
------------
``NodeServer`` exposes a minimal HTTP health endpoint on ``health_port``
when that parameter is provided.  Call:

    GET http://<host>:<health_port>/health
    → {"status": "ok", "node_id": "...", "port": ...}

Usage (run a node server in a separate process):
    python -m ecn.node_server --node-id Node-1 --port 9001

Or programmatically (e.g. in tests):
    server = NodeServer(node_id="Node-1", initial_state=..., port=9001)
    await server.start()
    ...
    await server.stop()
"""

import asyncio
import json
import logging
import os
from typing import Optional

from ecn.node import Node
from ecn.crypto import generate_keypair, public_key_to_hex, sign_result
from ecn.execution_engine import State

logger = logging.getLogger(__name__)


def _default_node_host() -> str:
    """Return the default bind host from ``ECN_NODE_HOST`` env var."""
    return os.environ.get("ECN_NODE_HOST", "127.0.0.1").strip()


class NodeServer:
    """
    Asyncio TCP server wrapping a single ECN Node.

    Each request is a JSON transaction; each response is a JSON SignedResult.

    Parameters
    ----------
    node_id : str
    initial_state : State
    port : int
        TCP port to listen on.
    host : str
        Bind address.  Defaults to ``ECN_NODE_HOST`` env var, falling back
        to ``"127.0.0.1"``.  Set ``ECN_NODE_HOST=0.0.0.0`` for cross-pod
        accessibility in Kubernetes.
    malicious : bool
        Run the node in malicious mode.
    health_port : int or None
        Port for the HTTP health endpoint.  When ``None`` (default) no
        health server is started.  In production pass an explicit port.
    """

    def __init__(
        self,
        node_id: str,
        initial_state: State,
        port: int,
        host: str = "",
        malicious: bool = False,
        health_port: Optional[int] = None,
    ) -> None:
        self.node_id = node_id
        self.host = host or _default_node_host()
        self.port = port
        self._health_port = health_port
        # Generate a fresh Ed25519 key pair for this node
        self._private_key, self.public_key = generate_keypair()
        self.public_key_hex = public_key_to_hex(self.public_key)
        self._node = Node(
            node_id=node_id,
            initial_state=initial_state,
            malicious=malicious,
            signing_key=self._private_key,
        )
        self._server: Optional[asyncio.AbstractServer] = None
        self._health_server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def malicious(self) -> bool:
        """Return True if this node is running in malicious mode."""
        return self._node.malicious

    async def start(self) -> None:
        """Start listening for incoming connections.

        When ``port`` is 0 the OS assigns a free port; ``self.port`` is
        updated to reflect the actual bound port after this method returns.
        """
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=self.port,
        )
        # If port=0 was requested, read back the OS-assigned port
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]
        logger.info(
            "NodeServer %s listening on %s:%d (pubkey=%s...)",
            self.node_id,
            self.host,
            self.port,
            self.public_key_hex[:16],
        )
        # Start health HTTP server if configured
        if self._health_port is not None:
            self._health_server = await asyncio.start_server(
                self._handle_health,
                host=self.host,
                port=self._health_port,
            )
            logger.info(
                "NodeServer %s health endpoint on %s:%d",
                self.node_id,
                self.host,
                self._health_port,
            )

    async def stop(self) -> None:
        """Shut down the server and health endpoint."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._health_server:
            self._health_server.close()
            await self._health_server.wait_closed()
            self._health_server = None

    async def serve_forever(self) -> None:
        """Run until cancelled."""
        await self.start()
        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            line = await reader.readline()
            if not line:
                return
            tx = json.loads(line.decode("utf-8").strip())
            result = self._node.execute_transaction(tx)
            response = {
                "node_id": result.node_id,
                "state_hash": result.state_hash,
                "signature": result.signature,
            }
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
            logger.debug(
                "%s processed tx=%s → hash=%s...",
                self.node_id,
                tx.get("type"),
                result.state_hash[:16],
            )
        except Exception as exc:
            error_response = {"error": str(exc)}
            try:
                writer.write((json.dumps(error_response) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
            logger.warning("%s error handling request from %s: %s", self.node_id, peer, exc)
        finally:
            writer.close()

    async def _handle_health(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Minimal HTTP/1.1 health check handler (no framework dependency)."""
        try:
            # Read and discard the HTTP request headers
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
            body = json.dumps({
                "status": "ok",
                "node_id": self.node_id,
                "host": self.host,
                "port": self.port,
                "malicious": self.malicious,
                "public_key_hex": self.public_key_hex[:16] + "...",
            })
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
                + body
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception as exc:
            logger.debug("Health handler error: %s", exc)
        finally:
            writer.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run an ECN node server")
    parser.add_argument("--node-id", required=True, help="Node identifier")
    parser.add_argument("--port", type=int, required=True, help="TCP port")
    parser.add_argument(
        "--host",
        default=_default_node_host(),
        help="Bind address (default: ECN_NODE_HOST env var or 127.0.0.1)",
    )
    parser.add_argument("--malicious", action="store_true", help="Run in malicious mode")
    parser.add_argument("--health-port", type=int, default=None, help="HTTP health check port")
    args = parser.parse_args()

    # Default initial state for CLI demo
    initial_state = {"balances": {"A": 1000, "B": 500, "C": 250}}

    logging.basicConfig(level=logging.INFO)
    server = NodeServer(
        node_id=args.node_id,
        initial_state=initial_state,
        port=args.port,
        host=args.host,
        malicious=args.malicious,
        health_port=args.health_port,
    )
    print(f"Starting {args.node_id} on {args.host}:{args.port}")
    print(f"Public key: {server.public_key_hex}")
    asyncio.run(server.serve_forever())
