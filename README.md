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
ECN/
├── Dockerfile                  # Multi-stage container image for the ECN API
├── docker-compose.yml          # Single-command local deployment (API + demo)
├── requirements.txt            # Runtime dependencies
├── k8s/
│   └── ecn.yaml                # Kubernetes Namespace + Deployment + Service + HPA
└── ecn/
    ├── execution_engine.py     # Pure deterministic execution (state, tx) → new_state
    ├── state_manager.py        # State storage, SHA-256 / Merkle hashing, trace
    ├── node.py                 # Node: honest or malicious, optional Ed25519 signing
    ├── consensus.py            # Plurality-vote consensus + signature verification
    ├── network.py              # Simulated broadcast network (in-process)
    ├── crypto.py               # Ed25519 key-gen, sign, verify; SignedResult type
    ├── node_server.py          # Real asyncio TCP server wrapping a Node
    ├── p2p_network.py          # Real TCP broadcast client + consensus
    ├── audit.py                # Structured audit trail (AuditEvent, AuditLog)
    ├── api.py                  # FastAPI REST API + webhook event streaming
    ├── sdk.py                  # Python SDK client (ECNClient)
    ├── dashboard.py            # Rich CLI live dashboard + replay viewer
    ├── trust_failure_demo.py   # Rich terminal trust-failure/attack detection demo
    ├── main.py                 # Demo: 3 simulated scenarios
    ├── demo_p2p.py             # Demo: 4 real-TCP + signed + supply-chain scenarios
    ├── use_cases/
    │   └── supply_chain.py     # Ship/receive/inspect/quarantine/release handlers
    └── tests/
        ├── test_execution_engine.py
        ├── test_state_manager.py
        ├── test_consensus.py
        ├── test_node.py
        ├── test_network.py
        ├── test_crypto.py
        ├── test_p2p_network.py
        ├── test_supply_chain.py
        ├── test_audit.py
        ├── test_api.py         # FastAPI REST API + webhook integration tests
        └── test_sdk.py         # Python SDK unit tests
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

### Trust Failure Demo (STEP 2 — enterprise showpiece)
```bash
python -m ecn.trust_failure_demo
```

Shows a pharmaceutical supply chain where a compromised auditor node attempts
to inject a false state.  ECN detects the attack instantly, isolates the
attacker, and prints a full cryptographically-verifiable audit trail.

### Live CLI Dashboard (STEP 3 — visualization)
```bash
python -m ecn.dashboard
```

Launches a `rich`-powered live terminal dashboard with:
- Network topology table (node IDs, ports, key prefixes, health)
- Real-time transaction feed (round history)
- Consensus health meter (fault rate, round counter)
- Fault alert panel (recent attacks/anomalies)

---

## REST API — Enterprise Integration (GAP 3)

Start the API server:
```bash
pip install fastapi "uvicorn[standard]"
python -m ecn.api
# or: uvicorn ecn.api:app --reload
```

API auto-docs: `http://127.0.0.1:8000/docs`

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/transactions` | Submit transaction; returns consensus result + all votes |
| `GET` | `/network/nodes` | List all nodes (ID, port, public key, mode) |
| `GET` | `/network/state` | Network health (fault rate, round counts) |
| `GET` | `/audit/events` | Full paginated audit trail |
| `GET` | `/audit/events/faults` | Only rounds with detected faults |
| `GET` | `/audit/events/{round_id}` | Single round by number |
| `GET` | `/audit/summary` | Aggregate statistics |
| `GET` | `/audit/replay` | Transaction replay log (for dashboards) |

### Example: Submit a supply-chain transaction

```bash
curl -X POST http://localhost:8000/transactions \
  -H "Content-Type: application/json" \
  -d '{"type":"ship","product_id":"LAPTOP-001","destination":"port","shipper":"DHL"}'
```

Response:
```json
{
  "round_id": 1,
  "timestamp": "2026-04-03T21:34:48Z",
  "consensus_reached": true,
  "agreed_hash": "910b2fbb...",
  "honest_nodes": ["Warehouse", "Shipper", "Customs", "Insurer", "Retailer"],
  "faulty_nodes": [],
  "votes": [
    { "node_id": "Warehouse", "state_hash": "910b2fbb...", "signature": "4696af...", "status": "honest" },
    ...
  ]
}
```

---

## Running the Tests

```bash
pip install pytest fastapi "uvicorn[standard]" httpx requests
python -m pytest ecn/tests/ -v
```

172 tests, all passing.

---

## Deployment

### Docker (single container)

```bash
# Build the image
docker build -t ecn:latest .

# Run the API (all 5 supply-chain nodes start in-process)
docker run -p 8000:8000 ecn:latest

# API docs: http://localhost:8000/docs
```

### Docker Compose (multi-container demo)

```bash
# Start the ECN API in the background
docker compose up -d

# Run the trust-failure demo against the live container
docker compose --profile demo up ecn-trust-demo

# Tail API logs
docker compose logs -f ecn-api

# Stop everything
docker compose down
```

### Kubernetes

```bash
# Deploy to the current cluster context
kubectl apply -f k8s/ecn.yaml

# Watch rollout
kubectl -n ecn rollout status deployment/ecn-api

# Port-forward to access the API locally
kubectl -n ecn port-forward svc/ecn-api 8000:80

# Scale up
kubectl -n ecn scale deployment ecn-api --replicas=4

# Tear down
kubectl delete namespace ecn
```

The HorizontalPodAutoscaler automatically scales from 2 to 10 replicas based on CPU usage.

---

## Python SDK

```python
from ecn.sdk import ECNClient

with ECNClient("http://localhost:8000") as client:
    # Supply-chain transactions
    result = client.ship("LAPTOP-001", destination="port", shipper="DHL")
    result = client.inspect("LAPTOP-001", check_name="label_check", inspector="QA")
    result = client.receive("LAPTOP-001", receiver="customs", at_customs=True)
    result = client.quarantine("LAPTOP-001", reason="suspicious_origin")
    result = client.release("LAPTOP-001", released_by="RegulatorA")

    print(result["consensus_reached"])   # True
    print(result["faulty_nodes"])        # [] (or list of malicious nodes)

    # Network health
    state = client.network_state()
    print(f"Fault rate: {state['fault_rate'] * 100:.1f}%")

    # Audit trail
    summary = client.audit_summary()
    print(summary["top_faulty_nodes"])

    # Webhook event streaming (fire-and-forget HTTP POST to your system)
    sub = client.subscribe_webhook("https://my-siem.example.com/ecn", events=["fault"])
    print(sub["webhook_id"])             # save this to unsubscribe later
    client.unsubscribe_webhook(sub["webhook_id"])
```

#### Error handling

```python
from ecn.sdk import ECNClient, ECNTransactionError, ECNNotFoundError

with ECNClient("http://localhost:8000") as client:
    try:
        client.ship("UNKNOWN-PRODUCT", destination="port", shipper="DHL")
    except ECNTransactionError as e:
        print(f"Transaction rejected: {e.detail}")

    try:
        client.audit_event(9999)
    except ECNNotFoundError:
        print("Round not found")
```

---

## Webhook Event Streaming

Subscribe any HTTPS endpoint to receive real-time POST notifications:

```bash
# Subscribe to all events
curl -X POST http://localhost:8000/webhooks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://my-system.com/ecn-hook", "events": []}'

# Subscribe to fault events only
curl -X POST http://localhost:8000/webhooks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://my-siem.com/alerts", "events": ["fault"]}'

# List active subscriptions
curl http://localhost:8000/webhooks

# Unsubscribe
curl -X DELETE http://localhost:8000/webhooks/<webhook_id>
```

Each POST delivery contains:
```json
{
  "webhook_id": "...",
  "event_types": ["transaction", "fault"],
  "payload": { "<full AuditEvent>" }
}
```

---

## Extensions Implemented

- ✅ Real TCP networking (asyncio, `node_server.py` + `p2p_network.py`)
- ✅ Ed25519 digital signatures per node result (`crypto.py`)
- ✅ Signature verification in consensus (bad/missing sigs → faulty)
- ✅ Supply chain domain use case (`use_cases/supply_chain.py`)
- ✅ Structured audit trail — every round logged as `AuditEvent` (`audit.py`)
- ✅ REST API — enterprise HTTP integration layer (`api.py`) with FastAPI
- ✅ Webhook event streaming — subscribe any URL to real-time round notifications
- ✅ Python SDK — `ECNClient` with typed helpers for all endpoints (`sdk.py`)
- ✅ Docker — multi-stage `Dockerfile` + `docker-compose.yml`
- ✅ Kubernetes — `k8s/ecn.yaml` with Deployment, Services, and HPA
- ✅ Trust Failure Demo — rich terminal attack/detection narrative (`trust_failure_demo.py`)
- ✅ Live CLI Dashboard — `rich`-powered visualization layer (`dashboard.py`)
- ✅ Execution trace logging (per-node, per-transaction)
- ✅ State diff output (shows which fields changed and by how much)
- ✅ Merkle tree state hashing (opt-in via `use_merkle=True`)
- ✅ Pluggable transaction types (`register_handler`)

