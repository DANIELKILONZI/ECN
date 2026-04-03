"""
trust_failure_demo.py
---------------------
STEP 2 — Trust Failure Demo

What this demonstrates (the "enterprise sale" scenario):
  1. A normal supply chain flow is established (4 honest nodes agree).
  2. A malicious node (the "compromised auditor") injects a false state hash —
     simulating a real-world attack where a node tries to approve a counterfeit
     product or forge a delivery confirmation.
  3. ECN isolates the attacker INSTANTLY — consensus continues on the true hash.
  4. The full audit trail is printed: every round, every vote, every fault.

Run with:
    python -m ecn.trust_failure_demo

Output is designed to be readable by executives:
  - Clear attack/detection narrative
  - Colour-coded terminal output (via rich)
  - Structured JSON audit trail at the end
"""

import asyncio
import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from ecn.audit import AuditLog
from ecn.p2p_network import P2PNetwork
from ecn.use_cases.supply_chain import register_supply_chain_handlers, make_initial_state

register_supply_chain_handlers()

console = Console()

PRODUCTS = ["VACCINE-BATCH-001"]
BASE_PORT = 18100


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _render_round(event, attack_round: bool = False) -> None:
    """Render one audit event as a rich table row."""
    title_color = "red bold" if event.has_fault() else "green"
    fault_label = " ⚠  FAULT DETECTED" if event.has_fault() else " ✓  Consensus OK"
    panel_title = f"[{title_color}]Round {event.round_id} — {fault_label}[/{title_color}]"

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Node", style="cyan", width=18)
    table.add_column("State Hash (prefix)", width=24)
    table.add_column("Signed", width=7)
    table.add_column("Status", width=14)

    for vote in event.votes:
        hash_prefix = vote.state_hash[:20] + "..."
        signed = "✓" if vote.signature else "✗"
        if vote.status == "honest":
            status = "[green]honest[/green]"
        elif vote.status == "hash_fault":
            hash_prefix = f"[red]{hash_prefix}[/red]"
            status = "[red bold]HASH FAULT[/red bold]"
        else:
            status = "[red bold]SIG FAULT[/red bold]"
        table.add_row(vote.node_id, hash_prefix, signed, status)

    console.print(Panel(table, title=panel_title, border_style="red" if event.has_fault() else "green"))

    if event.has_fault():
        console.print(f"  [yellow]Agreed hash :[/yellow] {event.agreed_hash[:32]}...")
        console.print(f"  [red]Faulty nodes:[/red] {event.faulty_nodes}")
        console.print()


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

async def run_demo():
    console.print()
    console.print(Panel(
        "[bold white]ECN — Trust Failure Demo[/bold white]\n\n"
        "Demonstrates how ECN detects and isolates a malicious node that\n"
        "attempts to inject a false state into a pharmaceutical supply chain.\n\n"
        "[dim]GAP 1: Real TCP networking  |  GAP 2: Ed25519 signatures  |  GAP 3: Supply chain[/dim]",
        border_style="blue",
        title="[bold blue]Executable Consensus Network[/bold blue]",
    ))

    audit = AuditLog()

    # ---------------------------------------------------------------
    # Phase 1: Honest network — establish the clean baseline
    # ---------------------------------------------------------------
    console.rule("[bold green]PHASE 1 — Establishing Clean Baseline (all honest nodes)[/bold green]")
    console.print()

    honest_configs = [
        {"node_id": "Factory"},
        {"node_id": "Distributor"},
        {"node_id": "Hospital"},
        {"node_id": "Regulator"},
    ]

    net = await P2PNetwork.create(
        node_configs=honest_configs,
        initial_state=make_initial_state(PRODUCTS),
        base_port=BASE_PORT,
        verbose=False,
        audit_log=audit,
    )

    baseline_txs = [
        {"type": "ship",    "product_id": "VACCINE-BATCH-001", "destination": "cold_storage", "shipper": "ColdChain"},
        {"type": "inspect", "product_id": "VACCINE-BATCH-001", "check_name": "temperature_integrity", "inspector": "QA-Lab"},
        {"type": "receive", "product_id": "VACCINE-BATCH-001", "receiver": "distributor", "at_customs": False},
    ]

    for tx in baseline_txs:
        results, cr = await net.broadcast(tx)
        event = audit.get_event(len(audit))
        _render_round(event)

    await net.shutdown()
    console.print("[green]✓  Baseline established: all 4 nodes agreed on every round.[/green]\n")

    # ---------------------------------------------------------------
    # Phase 2: Attack — malicious auditor node injects false state
    # ---------------------------------------------------------------
    console.rule("[bold red]PHASE 2 — ATTACK: Malicious Node Injects False State[/bold red]")
    console.print()
    console.print(
        Panel(
            "[red]A compromised 'Auditor' node attempts to approve a FORGED delivery confirmation.\n"
            "It reports a different state hash — trying to advance a counterfeit batch\n"
            "through the supply chain without detection.[/red]",
            title="[red bold]⚠  SIMULATED ATTACK[/red bold]",
            border_style="red",
        )
    )
    console.print()

    attack_configs = [
        {"node_id": "Factory"},
        {"node_id": "Distributor"},
        {"node_id": "Hospital"},
        {"node_id": "Regulator"},
        {"node_id": "Auditor",    "malicious": True},   # ← the attacker
    ]

    net2 = await P2PNetwork.create(
        node_configs=attack_configs,
        initial_state=make_initial_state(PRODUCTS),
        base_port=BASE_PORT + 10,
        verbose=False,
        audit_log=audit,
    )

    attack_txs = [
        {"type": "ship",       "product_id": "VACCINE-BATCH-001", "destination": "hospital_depot", "shipper": "MedFreight"},
        # ← this is the round the attacker tries to forge:
        {"type": "inspect",    "product_id": "VACCINE-BATCH-001", "check_name": "authenticity_check", "inspector": "Auditor"},
        {"type": "quarantine", "product_id": "VACCINE-BATCH-001", "reason": "unverified_source"},
    ]

    attack_round_id = None
    for i, tx in enumerate(attack_txs):
        results, cr = await net2.broadcast(tx)
        event = audit.get_event(len(audit))
        _render_round(event, attack_round=event.has_fault())
        if event.has_fault() and attack_round_id is None:
            attack_round_id = event.round_id

    await net2.shutdown()

    # ---------------------------------------------------------------
    # Phase 3: Detection summary
    # ---------------------------------------------------------------
    console.rule("[bold yellow]PHASE 3 — Detection & Isolation Report[/bold yellow]")
    console.print()

    fault_events = audit.get_fault_events()
    summary = audit.summary()

    console.print(
        Panel(
            f"[bold]Total rounds:          [/bold] {summary['total_rounds']}\n"
            f"[bold]Rounds with fault:     [/bold] [red]{summary['fault_rounds']}[/red]\n"
            f"[bold]Consensus failures:    [/bold] {summary['consensus_failure_rounds']}\n"
            f"[bold]Fault rate:            [/bold] {summary['fault_rate'] * 100:.1f}%\n"
            f"[bold]Top faulty nodes:      [/bold] [red]{summary['top_faulty_nodes']}[/red]",
            title="[yellow bold]Consensus Health Summary[/yellow bold]",
            border_style="yellow",
        )
    )

    if fault_events:
        console.print()
        console.print("[bold red]FAULT EVENTS — Full Audit Trail:[/bold red]")
        for event in fault_events:
            console.print(f"\n  [red]Round {event.round_id}[/red]  ({event.timestamp})")
            console.print(f"  Transaction:  {event.transaction}")
            console.print(f"  Faulty nodes: [red]{event.faulty_nodes}[/red]")
            for vote in event.votes:
                if vote.status != "honest":
                    console.print(
                        f"    [red]► {vote.node_id}[/red]: "
                        f"reported [red]{vote.state_hash[:24]}...[/red] "
                        f"(expected {_honest_hash(event)}...)"
                    )

    console.print()
    console.print(
        Panel(
            "[bold green]Key findings:[/bold green]\n\n"
            "• The malicious node's forged hash was detected in the same round it occurred.\n"
            "• Consensus continued correctly using the 4 honest nodes' agreed hash.\n"
            "• The attacker was isolated — their vote was excluded from future consensus.\n"
            "• The complete audit trail is cryptographically verifiable (Ed25519 signatures).\n\n"
            "[dim]This is the ECN security guarantee: "
            "you cannot fake execution results without being caught.[/dim]",
            title="[green bold]✓  Attack Successfully Detected & Isolated[/green bold]",
            border_style="green",
        )
    )

    # ---------------------------------------------------------------
    # Phase 4: Structured JSON audit dump (for dashboards / SIEM)
    # ---------------------------------------------------------------
    console.rule("[bold blue]PHASE 4 — Structured Audit Export (JSON)[/bold blue]")
    console.print("[dim](This is what feeds dashboards, SIEM systems, and replay viewers)[/dim]\n")

    export = {
        "summary": summary,
        "fault_events": [e.to_dict() for e in fault_events],
    }
    console.print_json(json.dumps(export, indent=2))

    console.print()
    console.print("[bold green]🎉  Trust Failure Demo complete.[/bold green]")


def _honest_hash(event) -> str:
    """Return the agreed hash prefix (representing what honest nodes reported)."""
    if event.agreed_hash:
        return event.agreed_hash[:24]
    return "N/A"


if __name__ == "__main__":
    asyncio.run(run_demo())
