"""User-facing Receipt analytics and graph commands."""
from __future__ import annotations

import json
from pathlib import Path

import click

from openshard.insights.graph import build_receipt_graph
from openshard.insights.query import answer_question
from openshard.insights.warehouse import ReceiptWarehouse


def _root() -> Path:
    from openshard.cli.ingest import _repo_root

    return _repo_root(None, False)


def _warehouse() -> ReceiptWarehouse:
    return ReceiptWarehouse(_root())


def _echo_json(value: object) -> None:
    click.echo(json.dumps(value, indent=2, default=str))


def _rate(value: object) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{float(value) * 100:.1f}%"


def _money(value: object) -> str:
    return "-" if not isinstance(value, (int, float)) else f"${float(value):.4f}"


@click.group("insights")
def insights_group() -> None:
    """Query evidence-backed analytics across local Receipts."""


@insights_group.command("overview")
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_overview(as_json: bool) -> None:
    """Summarise the evidence Openshard has accumulated."""
    with _warehouse() as warehouse:
        data = warehouse.overview()
    if as_json:
        _echo_json(data)
        return
    click.echo("Receipt intelligence")
    click.echo(f"  Receipts        {data['receipts']}")
    click.echo(f"  Observed        {data['observed']} with independent pass/fail evidence")
    click.echo(f"  Verified passed {data['verified_passed']}")
    click.echo(f"  Verified failed {data['verified_failed']}")
    click.echo(f"  Known cost      {_money(data['known_cost_usd'])} across {data['receipts_with_cost']} Receipt(s)")
    click.echo(f"  Agents          {data['agents']}")
    click.echo(f"  Models          {data['models']}")


@insights_group.command("models")
@click.option("--task", "task_category", default=None, help="Only this recorded task category.")
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_models(task_category: str | None, as_json: bool) -> None:
    """Compare recorded model outcomes without guessing missing evidence."""
    with _warehouse() as warehouse:
        rows = warehouse.models(task_category=task_category)
    if as_json:
        _echo_json(rows)
        return
    click.echo("Model outcomes" + (f" · {task_category}" if task_category else ""))
    if not rows:
        click.echo("  No model evidence recorded.")
        return
    for row in rows:
        click.echo(
            f"  {row['model']:<28} {row['runs']} run(s) · "
            f"{row['observed']} observed · pass {_rate(row['pass_rate'])} · "
            f"retry {row['retries']} · known cost {_money(row['known_cost_usd'])}"
        )
    click.echo("  Pass rate excludes agent-reported success and unknown verification.")


@insights_group.command("agents")
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_agents(as_json: bool) -> None:
    """Compare observed failure rates by agent."""
    with _warehouse() as warehouse:
        rows = warehouse.agents()
    if as_json:
        _echo_json(rows)
        return
    click.echo("Agent outcomes")
    if not rows:
        click.echo("  No agent evidence recorded.")
        return
    for row in rows:
        click.echo(
            f"  {row['agent']:<24} {row['runs']} run(s) · "
            f"{row['observed']} observed · failure {_rate(row['failure_rate'])} · "
            f"known cost {_money(row['known_cost_usd'])}"
        )


@insights_group.command("costs")
@click.option("--by", type=click.Choice(["model", "agent", "task", "provider", "product", "surface"]), default="model", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_costs(by: str, as_json: bool) -> None:
    """Show recorded cost without treating unknown as zero."""
    with _warehouse() as warehouse:
        rows = warehouse.costs(by=by)
    if as_json:
        _echo_json(rows)
        return
    click.echo(f"Known cost by {by}")
    if not rows:
        click.echo("  No trustworthy cost evidence recorded.")
        return
    for row in rows:
        click.echo(
            f"  {row['label']:<28} {_money(row['known_cost_usd'])} · "
            f"{row['runs_with_cost']}/{row['runs']} run(s) with cost · "
            f"avg {_money(row['avg_known_cost_usd'])}"
        )
    click.echo("  Unknown cost is excluded, never counted as $0.")


@insights_group.command("checks")
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_checks(as_json: bool) -> None:
    """Show which recorded checks have actually caught failures."""
    with _warehouse() as warehouse:
        rows = warehouse.checks()
    if as_json:
        _echo_json(rows)
        return
    click.echo("Verification checks")
    if not rows:
        click.echo("  No structured check evidence recorded.")
        return
    for row in rows:
        click.echo(
            f"  {row['name']:<36} {row['runs']} run(s) · "
            f"caught {row['runs_caught']} · passed {row['runs_passed']}"
        )


@insights_group.command("failures")
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_failures(as_json: bool) -> None:
    """Show the recurring failure categories present in Receipt evidence."""
    with _warehouse() as warehouse:
        rows = warehouse.failures()
    if as_json:
        _echo_json(rows)
        return
    click.echo("Failure patterns")
    if not rows:
        click.echo("  No classified failures recorded.")
        return
    for row in rows:
        click.echo(
            f"  {row['failure_category']:<28} {row['runs']} run(s) · "
            f"{row['observed_failures']} observed failure(s) · {row['retried']} retried"
        )


@insights_group.command("learning")
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_learning(as_json: bool) -> None:
    """Show how often recorded learning was used and routed work."""
    with _warehouse() as warehouse:
        data = warehouse.learning()
    if as_json:
        _echo_json(data)
        return
    click.echo("Learning loop")
    click.echo(f"  Recorded on      {data['recorded']} run(s)")
    click.echo(f"  Used on          {data['used']} run(s)")
    click.echo(f"  Not used on      {data['not_used']} run(s)")
    click.echo(f"  Used + verified  {data['used_and_verified']} run(s)")
    click.echo(f"  Routing applied  {data['routing_applied']} run(s)")
    click.echo("  These are observations, not proof that learning caused an outcome.")


@insights_group.command("graph")
@click.option("--find", "search", default=None, help="Find nodes whose id or label contains this text.")
@click.option("--kind", default=None, help="Restrict --find to one node kind.")
@click.option("--depth", type=click.IntRange(0, 3), default=1, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_graph(search: str | None, kind: str | None, depth: int, as_json: bool) -> None:
    """Inspect evidence-backed relationships between Receipts and their context."""
    with _warehouse() as warehouse:
        graph = build_receipt_graph(warehouse)
    selected = graph
    matches = []
    if search:
        matches = graph.find_nodes(search, kind=kind)
        node_ids = {node.id for node in matches}
        if len(node_ids) == 1:
            selected = graph.neighborhood(next(iter(node_ids)), depth=depth)
    if as_json:
        _echo_json({
            "matches": [node.to_dict() for node in matches],
            "graph": selected.to_dict(),
        })
        return
    click.echo(f"Receipt graph · {len(graph.nodes)} nodes · {len(graph.edges)} edges")
    if search:
        click.echo(f"  Matches: {len(matches)}")
        for node in matches[:20]:
            click.echo(f"    {node.kind:<12} {node.label}  [{node.id}]")
        if len(matches) == 1:
            click.echo(f"  Neighborhood: {len(selected.nodes)} nodes · {len(selected.edges)} edges")


@insights_group.command("ask")
@click.argument("question", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def insights_ask(question: tuple[str, ...], as_json: bool) -> None:
    """Ask a common operational question in plain English."""
    text = " ".join(question).strip()
    with _warehouse() as warehouse:
        answer = answer_question(warehouse, text)
    if as_json:
        _echo_json(answer.to_dict())
        return
    click.echo(answer.summary)
    if answer.caveat:
        click.echo(f"  Note: {answer.caveat}")
