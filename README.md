# ECN — Executable Consensus Network

A Python prototype of an **Executable Consensus Network**: a distributed system
where nodes independently execute transactions and reach consensus on the
**result** of computation — not on transaction ordering.

---

## What is ECN?

Traditional blockchains (e.g. Ethereum) reach consensus on which transactions
to include and in what order, then each node re-executes them locally.

**ECN flips this**: every node executes the same transaction independently and
deterministically, then the network agrees on the *output hash*.  This makes
execution the first-class citizen and means a faulty or malicious node is
detected by the divergence of its output — not by detecting a bad signature or
block proposal.

| Property | Traditional Blockchain | ECN |
|---|---|---|
| Consensus target | Transaction ordering | Execution result |
| Fault detection | Invalid signature / fork | Hash mismatch + sig verification |
| State proof | Merkle Patricia trie | SHA-256 / Merkle root |
| Execution timing | After ordering | Before consensus |
| Trust model | Chain of blocks | Cryptographic result signatures |

---

## Production Upgrades

Three gaps between a prototype and production-grade infrastructure are addressed:

### GAP 1 — Real Networking

Nodes run as independent **asyncio TCP servers** (`node_server.py`).  The
broadcaster (`p2p_network.py`) connects to each server over a real TCP socket,
sends a transaction, and receives a signed result — no shared memory, no
in-process shortcuts.

### GAP 2 — Cryptographic Trust Layer

Every node generates an **Ed25519 key pair** on startup.  After executing a
transaction the node signs its `(node_id, state_hash)` pair with its private
key.  The consensus layer verifies every signature before counting votes:

```json
{
  "node_id":    "Warehouse",
  "state_hash": "910b2fbb...",
  "signature":  "4696afdf..."
}
```

Results with missing or invalid signatures are flagged as faulty
*independently* of the hash comparison, preventing an attacker from simply
copying a majority hash without being detected.

### GAP 3 — Domain-Specific Use Case: Supply Chain Verification

`ecn/use_cases/supply_chain.py` implements a **supply chain verification**
domain — a concrete pain point where multiple mutually distrusting parties
(warehouse, shipper, customs, insurer, retailer) must agree on the status of
physical goods without a central authority.

Transaction types: `ship`, `receive`, `inspect`, `quarantine`, `release`.

---

## Project Structure

```
ecn/
├── __init__.py
├── execution_engine.py         # Pure deterministic execution (state, tx) → new_state
├── state_manager.py            # State storage, SHA-256 / Merkle hashing, trace
├── node.py                     # Node: honest or malicious, optional Ed25519 signing
├── consensus.py                # Plurality-vote consensus + signature verification
├── network.py                  # Simulated broadcast network (in-process)
├── crypto.py                   # Ed25519 key-gen, sign, verify; SignedResult type
├── node_server.py              # Real asyncio TCP server wrapping a Node
├── p2p_network.py              # Real TCP broadcast client + consensus
├── main.py                     # Demo: 3 simulated scenarios
├── demo_p2p.py                 # Demo: 4 real-TCP + signed + supply-chain scenarios
└── use_cases/
│   ├── __init__.py
│   └── supply_chain.py         # Ship/receive/inspect/quarantine/release handlers
└── tests/
    ├── test_execution_engine.py
    ├── test_state_manager.py
    ├── test_consensus.py
    ├── test_node.py
    ├── test_network.py
    ├── test_crypto.py          # Ed25519 key-gen, sign/verify, SignedResult
    ├── test_p2p_network.py     # Real TCP integration tests
    └── test_supply_chain.py    # Supply chain transaction tests
```

---

## How Consensus Works

1. A transaction is **broadcast** to all nodes.
2. Every node **independently executes** the transaction against its local state
   copy using the same deterministic execution engine.
3. Each node **signs** its result with its Ed25519 private key and reports:
   `{node_id, state_hash, signature}`.
4. The consensus module **verifies every signature** first — results with bad
   or missing signatures are immediately flagged as faulty.
5. Verified results are **vote-counted** by hash; the plurality winner wins.
6. Any node whose hash diverges is **flagged as faulty**.

```
Warehouse  → hash: 910b2fbb...  sig=4696af...
Shipper    → hash: 910b2fbb...  sig=5d633c...
Customs    → hash: 910b2fbb...  sig=1bd112...
Auditor    → hash: 910b2fbb...DEADBEEF  sig=ee2cd4...  ← FAULT DETECTED

Consensus → 910b2fbb...
Faulty    → [Auditor]
```

---

## Determinism Guarantees

* **No randomness** — all operations are pure functions of (state, tx).
* **No floating point** — only integer arithmetic is used.
* **No system time** — nothing reads the clock.
* **Canonical JSON** — dict keys are always sorted before hashing.
* **Deep copies** — input state is never mutated.

---

## Supported Transaction Types

### Generic (financial)

| Type | Required fields | Description |
|------|----------------|-------------|
| `transfer` | `from`, `to`, `amount` | Move tokens between accounts |
| `mint` | `to`, `amount` | Create new tokens for an account |
| `burn` | `from`, `amount` | Destroy tokens from an account |

### Supply chain

| Type | Required fields | Description |
|------|----------------|-------------|
| `ship` | `product_id`, `destination`, `shipper` | Dispatch product |
| `receive` | `product_id`, `receiver`, `at_customs` | Accept delivery |
| `inspect` | `product_id`, `check_name`, `inspector` | Record a passed check |
| `quarantine` | `product_id`, `reason` | Flag for investigation |
| `release` | `product_id`, `released_by` | Clear quarantine |

Custom types can be added via `register_handler("my_type", handler_fn)`.

---

## Running the Demos

### Simulated network (no real TCP)
```bash
python -m ecn.main
```

### Real TCP + Ed25519 signatures + supply chain
```bash
python -m ecn.demo_p2p
```

---

## Running the Tests

```bash
pip install pytest
python -m pytest ecn/tests/ -v
```

101 tests, all passing.

---

## Extensions Implemented

- ✅ Real TCP networking (asyncio, `node_server.py` + `p2p_network.py`)
- ✅ Ed25519 digital signatures per node result (`crypto.py`)
- ✅ Signature verification in consensus (bad/missing sigs → faulty)
- ✅ Supply chain domain use case (`use_cases/supply_chain.py`)
- ✅ Execution trace logging (per-node, per-transaction)
- ✅ State diff output (shows which fields changed and by how much)
- ✅ Merkle tree state hashing (opt-in via `use_merkle=True`)
- ✅ Pluggable transaction types (`register_handler`)

