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
from typing import Optional

from ecn.node import Node
from ecn.crypto import generate_keypair, public_key_to_hex, sign_result
from ecn.execution_engine import State

logger = logging.getLogger(__name__)


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
        Bind address (default: ``"127.0.0.1"``).
    malicious : bool
        Run the node in malicious mode.
    """

    def __init__(
        self,
        node_id: str,
        initial_state: State,
        port: int,
        host: str = "127.0.0.1",
        malicious: bool = False,
    ) -> None:
        self.node_id = node_id
        self.host = host
        self.port = port
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

    async def stop(self) -> None:
        """Shut down the server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run an ECN node server")
    parser.add_argument("--node-id", required=True, help="Node identifier")
    parser.add_argument("--port", type=int, required=True, help="TCP port")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--malicious", action="store_true", help="Run in malicious mode")
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
    )
    print(f"Starting {args.node_id} on {args.host}:{args.port}")
    print(f"Public key: {server.public_key_hex}")
    asyncio.run(server.serve_forever())
