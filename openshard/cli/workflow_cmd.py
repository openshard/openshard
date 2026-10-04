"""Inspect explicitly correlated work without changing sealed Receipts."""

from __future__ import annotations

import json

import click


@click.group("workflow")
def workflow_group() -> None:
    """Inspect work linked by an explicit task identity."""


@workflow_group.command("timeline")
@click.option("--task-id", required=True, help="Explicit task identity; never inferred.")
@click.option("--repo-path", type=click.Path(), default=None)
def timeline(task_id: str, repo_path: str | None) -> None:
    """Print the local run/Receipt links as JSON; links are declared, not proof."""
    from openshard.cli.ingest import _repo_root
    from openshard.history.correlation import workflow_timeline
    from openshard.history.store import load_history

    root = _repo_root(repo_path, False)
    try:
        rows = workflow_timeline(load_history(root / ".openshard" / "runs.jsonl"), task_id=task_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"task_id": task_id, "runs": rows}, indent=2))
