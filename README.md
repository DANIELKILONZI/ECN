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
| Fault detection | Invalid signature / fork | Hash mismatch |
| State proof | Merkle Patricia trie | SHA-256 / Merkle root |
| Execution timing | After ordering | Before consensus |

---

## Project Structure

```
ecn/
├── __init__.py
├── execution_engine.py   # Pure deterministic execution (state, tx) → new_state
├── state_manager.py      # State storage, SHA-256 / Merkle hashing, trace
├── node.py               # Individual network node (honest or malicious)
├── consensus.py          # Plurality-vote consensus over state hashes
├── network.py            # Simulated broadcast network
├── main.py               # Demo: 3 scenarios
└── tests/
    ├── test_execution_engine.py
    ├── test_state_manager.py
    ├── test_consensus.py
    ├── test_node.py
    └── test_network.py
```

---

## How Consensus Works

1. A transaction is **broadcast** to all nodes.
2. Every node **independently executes** the transaction against its local state
   copy using the same deterministic execution engine.
3. Each node reports its resulting **state hash** (SHA-256 of canonical JSON, or
   optionally a Merkle root over per-account hashes).
4. The consensus module collects all hashes and **counts votes**.
5. The hash with the most votes wins (**plurality rule**).  For a strict
   majority (> 50 %) the round is considered *finalized*.
6. Any node whose hash differs from the winning hash is **flagged as faulty**.

```
Node-1  → hash: abc123
Node-2  → hash: abc123
Node-3  → hash: abc123
Node-4  → hash: abc123
Node-5  → hash: xyz999  ← FAULT DETECTED

Consensus → abc123
Faulty    → [Node-5]
```

---

## Determinism Guarantees

* **No randomness** — all operations are pure functions of (state, tx).
* **No floating point** — only integer arithmetic is used.
* **No system time** — nothing reads the clock.
* **Canonical JSON** — dict keys are always sorted before hashing.
* **Deep copies** — input state is never mutated; every execution returns a
  fresh state object.

---

## Supported Transaction Types

| Type | Required fields | Description |
|------|----------------|-------------|
| `transfer` | `from`, `to`, `amount` | Move tokens between accounts |
| `mint` | `to`, `amount` | Create new tokens for an account |
| `burn` | `from`, `amount` | Destroy tokens from an account |

Custom transaction types can be registered at runtime:

```python
from ecn.execution_engine import register_handler

def my_handler(state, tx):
    import copy
    new_state = copy.deepcopy(state)
    # ... apply custom logic ...
    return new_state

register_handler("my_type", my_handler)
```

---

## Running the Demo

```bash
# From the repository root:
python -m ecn.main
```

Three scenarios are executed:

1. **Normal Execution** — 5 honest nodes, one transfer; all agree.
2. **Fault Injection** — 4 honest + 1 malicious node; malicious node detected.
3. **Multiple Transaction Types** — mint, transfer, burn using Merkle hashing
   with execution trace output.

---

## Running the Tests

```bash
pip install pytest
python -m pytest ecn/tests/ -v
```

All 60 tests should pass.

---

## Extensions Implemented

- ✅ Execution trace logging (per-node, per-transaction)
- ✅ State diff output (shows which balances changed and by how much)
- ✅ Merkle tree state hashing (opt-in via `use_merkle=True`)
- ✅ Pluggable transaction types (`register_handler`)
