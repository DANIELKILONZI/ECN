"""
dashboard.py
------------
STEP 3 — Visualization Layer: Live CLI Dashboard

An executive-ready terminal dashboard that shows:
  - Network topology (node list, health indicators)
  - Real-time transaction feed (latest rounds with status)
  - Consensus health meter (fault rate, round count)
  - Fault alert panel (recent attacks/anomalies)
  - Transaction replay viewer (scrollable history)

Built with `rich` — no web server required; works in any terminal.

Usage (live demo with supply-chain transactions):
    python -m ecn.dashboard

Usage (static replay from an AuditLog):
    from ecn.dashboard import render_static_dashboard
    render_static_dashboard(audit_log)
"""

import asyncio
import json
from typing import List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich import box
from rich.text import Text

from ecn.audit import AuditLog, AuditEvent
from ecn.p2p_network import P2PNetwork
from ecn.use_cases.supply_chain import register_supply_chain_handlers, make_initial_state

register_supply_chain_handlers()

console = Console()

# ---------------------------------------------------------------------------
# Rich rendering helpers
# ---------------------------------------------------------------------------

def _node_table(servers) -> Table:
    """Build a table showing all nodes and their health."""
    t = Table(title="Network Nodes", box=box.ROUNDED, border_style="blue", show_header=True)
    t.add_column("Node ID", style="cyan", width=14)
    t.add_column("Port", width=6)
    t.add_column("Mode", width=10)
    t.add_column("Key (prefix)", width=18)
    for s in servers:
        mode = "[red]MALICIOUS[/red]" if s.malicious else "[green]honest[/green]"
        t.add_row(s.node_id, str(s.port), mode, s.public_key_hex[:16] + "...")
    return t


def _round_table(events: List[AuditEvent], max_rows: int = 10) -> Table:
    """Build a table of the most recent rounds."""
    t = Table(title=f"Recent Rounds (last {max_rows})", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("#", width=5)
    t.add_column("Timestamp", width=22)
    t.add_column("Tx Type", width=12)
    t.add_column("Product / Subject", width=18)
    t.add_column("Consensus", width=12)
    t.add_column("Faults", width=22)

    for event in reversed(events[-max_rows:]):
        tx_type = event.transaction.get("type", "?")
        subject = (
            event.transaction.get("product_id")
            or event.transaction.get("to")
            or event.transaction.get("from")
            or "—"
        )
        if event.consensus_reached:
            consensus = "[green]✓ reached[/green]"
        else:
            consensus = "[red]✗ failed[/red]"

        if event.faulty_nodes:
            faults = "[red]" + ", ".join(event.faulty_nodes) + "[/red]"
        else:
            faults = "[dim]none[/dim]"

        t.add_row(
            str(event.round_id),
            event.timestamp[11:22],   # show HH:MM:SS.mmm
            tx_type,
            subject,
            consensus,
            faults,
        )
    return t


def _health_panel(summary: dict) -> Panel:
    """Build a consensus health summary panel."""
    total = summary["total_rounds"]
    faults = summary["fault_rounds"]
    rate = summary["fault_rate"]

    # Fault-rate progress bar (text only for static render)
    if total == 0:
        bar = "[dim]No rounds yet[/dim]"
    else:
        filled = int(rate * 20)
        bar_chars = "█" * filled + "░" * (20 - filled)
        color = "green" if rate < 0.1 else "yellow" if rate < 0.3 else "red"
        bar = f"[{color}]{bar_chars}[/{color}]  {rate * 100:.1f}%"

    top = summary.get("top_faulty_nodes", [])
    top_str = ", ".join(f"{n}({c})" for n, c in top[:3]) if top else "[dim]none[/dim]"

    content = (
        f"[bold]Total rounds:       [/bold]{total}\n"
        f"[bold]Fault rounds:       [/bold][red]{faults}[/red]\n"
        f"[bold]Consensus failures: [/bold]{summary['consensus_failure_rounds']}\n"
        f"[bold]Fault rate:         [/bold]{bar}\n"
        f"[bold]Top faulty nodes:   [/bold][red]{top_str}[/red]"
    )
    return Panel(content, title="[yellow bold]Consensus Health[/yellow bold]", border_style="yellow")


def _fault_panel(fault_events: List[AuditEvent]) -> Panel:
    """Build a panel listing recent fault events."""
    if not fault_events:
        return Panel("[green]No faults detected yet.[/green]", title="[green]Fault Alerts[/green]", border_style="green")

    lines = []
    for event in reversed(fault_events[-5:]):
        ts = event.timestamp[11:22]
        faulters = ", ".join(event.faulty_nodes)
        tx_type = event.transaction.get("type", "?")
        lines.append(
            f"[red]⚠  Round {event.round_id}[/red]  [{ts}]  "
            f"tx=[cyan]{tx_type}[/cyan]  "
            f"faulty=[red]{faulters}[/red]"
        )

    return Panel(
        "\n".join(lines),
        title=f"[red bold]⚠  Fault Alerts ({len(fault_events)} total)[/red bold]",
        border_style="red",
    )


# ---------------------------------------------------------------------------
# Static dashboard (renders once — for demos and tests)
# ---------------------------------------------------------------------------

def render_static_dashboard(audit_log: AuditLog, servers=None) -> None:
    """
    Render a full ECN dashboard snapshot to the terminal.

    Parameters
    ----------
    audit_log : AuditLog
    servers : list[NodeServer], optional
        If provided, a node-topology table is rendered.
    """
    console.print()
    console.print(Panel(
        "[bold white]ECN Supply-Chain Network[/bold white]\n"
        "[dim]Live Consensus Dashboard[/dim]",
        border_style="blue",
        title="[bold blue]Executable Consensus Network[/bold blue]",
    ))

    if servers:
        console.print(_node_table(servers))
        console.print()

    summary = audit_log.summary()
    events = audit_log.get_events()
    fault_events = audit_log.get_fault_events()

    console.print(_health_panel(summary))
    console.print()
    console.print(_fault_panel(fault_events))
    console.print()

    if events:
        console.print(_round_table(events, max_rows=15))
    else:
        console.print("[dim]No rounds recorded yet.[/dim]")


# ---------------------------------------------------------------------------
# Live dashboard (auto-refreshes as transactions come in)
# ---------------------------------------------------------------------------

def _build_layout(audit_log: AuditLog, servers=None) -> Layout:
    """Build a rich Layout for the live dashboard."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=1),
    )

    layout["header"].update(Panel(
        "[bold white]ECN Supply-Chain Network[/bold white]  [dim]Live Dashboard[/dim]",
        border_style="blue",
    ))

    summary = audit_log.summary()
    events = audit_log.get_events()
    fault_events = audit_log.get_fault_events()

    layout["left"].update(
        Panel(_round_table(events, max_rows=12), title="Transaction Feed", border_style="cyan")
    )

    right = Layout()
    right.split_column(
        Layout(name="health"),
        Layout(name="faults"),
    )
    right["health"].update(_health_panel(summary))
    right["faults"].update(_fault_panel(fault_events))
    layout["right"].update(right)

    if servers:
        layout["footer"].update(
            Panel(
                "  ".join(
                    f"[{'red' if s.malicious else 'green'}]●[/] {s.node_id}"
                    for s in servers
                ),
                title="Nodes",
                border_style="dim",
            )
        )
    else:
        layout["footer"].update(Panel("[dim]Press Ctrl+C to exit[/dim]", border_style="dim"))

    return layout


async def run_live_dashboard(
    node_configs: List[dict],
    transactions: List[dict],
    initial_state: dict,
    base_port: int = 18200,
    tx_delay: float = 0.8,
) -> AuditLog:
    """
    Run the live dashboard: spin up the ECN network, broadcast transactions
    one-by-one with a short delay, and refresh the dashboard after each round.

    Parameters
    ----------
    node_configs : list[dict]
    transactions : list[dict]
    initial_state : dict
    base_port : int
    tx_delay : float
        Seconds to wait between transactions (makes the live effect visible).

    Returns
    -------
    AuditLog
        The log accumulated during the run (for further inspection).
    """
    audit = AuditLog()
    net = await P2PNetwork.create(
        node_configs=node_configs,
        initial_state=initial_state,
        base_port=base_port,
        verbose=False,
        audit_log=audit,
    )

    try:
        with Live(
            _build_layout(audit, net._servers),
            console=console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            for tx in transactions:
                await net.broadcast(tx)
                live.update(_build_layout(audit, net._servers))
                await asyncio.sleep(tx_delay)
    finally:
        await net.shutdown()

    return audit


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PRODUCTS = ["LAPTOP-001", "PHONE-002", "TABLET-003"]

    node_configs = [
        {"node_id": "Warehouse"},
        {"node_id": "Shipper"},
        {"node_id": "Customs"},
        {"node_id": "Insurer"},
        {"node_id": "Retailer"},
        {"node_id": "BadActor", "malicious": True},
    ]

    transactions = [
        {"type": "ship",       "product_id": "LAPTOP-001",  "destination": "port",          "shipper": "DHL"},
        {"type": "inspect",    "product_id": "LAPTOP-001",  "check_name": "physical_check",  "inspector": "Lab"},
        {"type": "receive",    "product_id": "LAPTOP-001",  "receiver": "customs",           "at_customs": True},
        {"type": "ship",       "product_id": "PHONE-002",   "destination": "airport",        "shipper": "FedEx"},
        {"type": "quarantine", "product_id": "PHONE-002",   "reason": "suspicious_label"},
        {"type": "release",    "product_id": "PHONE-002",   "released_by": "inspector"},
        {"type": "ship",       "product_id": "TABLET-003",  "destination": "retailer_depot", "shipper": "UPS"},
        {"type": "receive",    "product_id": "TABLET-003",  "receiver": "retailer",          "at_customs": False},
        {"type": "inspect",    "product_id": "TABLET-003",  "check_name": "label_check",     "inspector": "QA"},
    ]

    audit_result = asyncio.run(
        run_live_dashboard(
            node_configs=node_configs,
            transactions=transactions,
            initial_state=make_initial_state(PRODUCTS),
            base_port=18200,
            tx_delay=1.2,
        )
    )

    # Print static summary after the live session
    console.print("\n[bold]Session complete — final dashboard:[/bold]\n")
    render_static_dashboard(audit_result)
