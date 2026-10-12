"""``openshard import``: Receipts for runs OpenShard did not control.

``import claude`` records a Claude Code session from a task description and
the repository's git state; ``import claude-transcript`` reconstructs one from
a Claude Code transcript whose hook events were lost. Neither invents
verification, cost or approval, and both mark their Receipts as imported.
"""
from __future__ import annotations

import json
from pathlib import Path

import click


@click.group("import")
def import_group() -> None:
    """Import an external AI coding session as an OpenShard receipt."""


@import_group.command("claude")
@click.option("--task", required=True, help="Task description given to Claude Code.")
@click.option("--model", default=None, help="Model used (e.g. claude-sonnet-4-6). Default: unknown.")
@click.option(
    "--notes", "notes_file", default=None, type=click.Path(),
    help="Optional notes/summary file. First 300 chars stored (scrubbed); raw content not kept.",
)
@click.option(
    "--repo-path", "repo_path", default=None, type=click.Path(),
    help="Repository path (default: current directory).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print the Shard without writing it.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option(
    "--shard",
    "shard_id",
    default=None,
    help=(
        "Attach this import as another attempt on an existing Shard. Must "
        "reference a Shard ID from a previous run."
    ),
)
@click.option(
    "--task-id",
    "task_id",
    default=None,
    help=(
        "Attach this import's receipt to an explicitly declared engineering "
        "task. Must be a task id from 'openshard task new'."
    ),
)
def import_claude(
    task: str,
    model: str | None,
    notes_file: str | None,
    repo_path: str | None,
    dry_run: bool,
    as_json: bool,
    shard_id: str | None,
    task_id: str | None,
) -> None:
    """Import a Claude Code session as an OpenShard receipt.

    Creates a Shard from the task description and current git state.
    OpenShard did not control this run: verification, cost, and model
    details are not recorded unless explicitly provided.
    """
    from openshard.adapters.claude_code_import import (
        build_claude_code_import_entry,
        write_import_entry,
    )
    from openshard.history.metrics import load_runs
    from openshard.history.run_attempt import UnknownShardError, resolve_shard_for_attempt
    from openshard.history.task_identity import is_task_id

    cwd = Path(repo_path) if repo_path else Path.cwd()
    notes_path = Path(notes_file) if notes_file else None

    if notes_path is not None and not notes_path.is_file():
        raise click.BadParameter(
            f"Notes file not found: {notes_file}",
            param_hint="--notes",
        )
    if task_id is not None and not is_task_id(task_id):
        raise click.UsageError(
            f"--task-id {task_id!r} is not a well-formed task id "
            "(expected 'task_' + UUIDv7 from 'openshard task new')."
        )

    attempt_number = 1
    if shard_id:
        try:
            shard_id, attempt_number = resolve_shard_for_attempt(
                shard_id, load_runs(cwd), "", None,
            )
        except UnknownShardError as exc:
            raise click.UsageError(str(exc))

    runs_path = cwd / ".openshard" / "runs.jsonl"
    try:
        run_index = sum(1 for _ in runs_path.open(encoding="utf-8")) if runs_path.exists() else 0
    except Exception:
        run_index = None

    entry = build_claude_code_import_entry(
        task,
        model=model,
        notes_file=notes_path,
        repo_path=cwd,
        shard_id=shard_id,
        attempt_number=attempt_number,
        run_index=run_index,
        task_id=task_id,
    )

    if dry_run or as_json:
        click.echo(json.dumps(entry, indent=2))
        if dry_run:
            return

    if not dry_run:
        write_import_entry(entry, cwd)

    if not as_json:
        click.echo(f"Imported Claude Code receipt. Shard: {entry.get('shard_id')}")
        click.echo("OpenShard did not control this run.")


@import_group.command("claude-transcript")
@click.argument("transcript", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--repo", "repo_path", default=None, type=click.Path(file_okay=False),
    help="Repository the session worked in (default: current directory).",
)
@click.option("--write", is_flag=True, default=False, help="Write the reconstructed Receipt; the default is a dry run.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def import_claude_transcript(transcript: str, repo_path: str | None, write: bool, as_json: bool) -> None:
    """Reconstruct a Receipt from a Claude Code transcript whose hook events were lost.

    Everything in the result is labelled as reconstructed: tool calls, prompts
    and token usage are the transcript's; git state, cost and verification
    were not observed and are left unknown. Nothing is written without
    --write, and nothing is ever written over a session that already has a
    Receipt here. A written Receipt syncs like any other local Receipt.
    """
    from openshard.adapters.claude_transcript_import import import_transcript

    cwd = Path(repo_path) if repo_path else Path.cwd()
    report = import_transcript(Path(transcript), repo_path=cwd, write=write)
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    if report.status == "not_a_transcript":
        raise click.ClickException(f"{transcript} is not a Claude Code session transcript.")
    if report.status == "already_recorded":
        click.echo(f"Session {report.session_id} already has a Receipt here: {', '.join(report.existing)}. Nothing written.")
        return
    capture = (report.entry or {}).get("capture") or {}
    click.echo(f"{'Wrote' if write else 'Would write'} reconstructed Receipt {report.receipt_id} for session {report.session_id}.")
    click.echo(
        f"  tool calls: {capture.get('tool_call_count')}  prompts: {capture.get('prompt_count')}  "
        f"turns: {capture.get('turn_count')}  models: {', '.join(capture.get('models_seen') or []) or 'unknown'}"
    )
    click.echo(f"  tokens: {(report.entry or {}).get('total_tokens', 'not recorded')}  cost: not recorded  git state: not observed")
    click.echo(f"  completeness: partial ({', '.join((capture.get('completeness') or {}).get('reasons') or [])})")
    if not write:
        click.echo("Dry run: re-run with --write to record it. Reconstructed evidence stays labelled as such.")


