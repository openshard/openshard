"""``openshard ingest``: import past coding-agent history as honestly labelled receipts.

Nothing here is destructive: ``sources`` and ``scan`` only read, ``run``
appends new sealed receipts (or attachments) and never edits existing ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

SOURCE_CHOICES = ("claude-code", "codex")


def _repo_root(repo_path: str | None, allow_home_repo: bool) -> Path:
    from openshard.adapters.claude_hooks import _is_forbidden_capture_root
    from openshard.adapters.claude_mcp_install import find_repo_root

    start = Path(repo_path) if repo_path else Path.cwd()
    root = find_repo_root(start)
    if root is None:
        raise click.ClickException("Not inside a git repository. Run from a repository or pass --repo-path.")
    if _is_forbidden_capture_root(root) and not allow_home_repo:
        raise click.ClickException(
            "The repository resolved to your home directory; pass --allow-home-repo if that is intended."
        )
    return root.resolve()


def _sources(values: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(values)) if values else list(SOURCE_CHOICES)


def _emit(data: object, as_json: bool, human: str) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True) if as_json else human)


def _counts_line(c: dict) -> str:
    parts = [
        f"written {c.get('written', 0)}",
        f"attached {c.get('attached', 0)}",
        f"duplicates {c.get('skipped_duplicate', 0)}",
        f"filtered {c.get('skipped_filtered', 0)}",
        f"quarantined {c.get('quarantined', 0)}",
        f"failed {c.get('failed', 0)}",
    ]
    if c.get("superseded"):
        parts.insert(1, f"superseding {c['superseded']}")
    return ", ".join(parts)


_common_options = [
    click.option("--repo-path", default=None, type=click.Path(), help="Repository (default: current directory)."),
    click.option("--allow-home-repo", is_flag=True, help="Allow a repository rooted at your home directory."),
    click.option("--json", "as_json", is_flag=True, help="Machine-readable output."),
]


def _with(options):
    def deco(fn):
        for opt in reversed(options):
            fn = opt(fn)
        return fn
    return deco


_filter_options = [
    click.argument("sources", nargs=-1, type=click.Choice(SOURCE_CHOICES)),
    click.option("--since", default=None, help="Only sessions starting on/after this date (YYYY-MM-DD or ISO)."),
    click.option("--until", default=None, help="Only sessions starting on/before this date (YYYY-MM-DD or ISO)."),
    click.option("--all-repos", is_flag=True,
                 help="Also import into other repositories that already have .openshard/."),
]


@click.group("ingest")
def ingest_group() -> None:
    """Import past Claude Code / Codex history as receipts (reconstructed, never 'observed')."""


@ingest_group.command("sources")
@_with(_common_options)
def ingest_sources(repo_path: str | None, allow_home_repo: bool, as_json: bool) -> None:
    """Detect local agent history and how much of it belongs to this repository."""
    from openshard.ingest import discover_sources

    root = _repo_root(repo_path, allow_home_repo)
    rows = discover_sources(root)
    lines = []
    for r in rows:
        if not r["available"]:
            lines.append(f"{r['label']}: no local history found")
        else:
            lines.append(f"{r['label']}: {r['sessions']} session(s), {r['in_this_repo']} in this repository")
    _emit({"sources": rows}, as_json, "\n".join(lines))


@ingest_group.command("scan")
@_with(_filter_options + _common_options)
def ingest_scan(sources, since, until, all_repos, repo_path, allow_home_repo, as_json) -> None:
    """Dry run: show what would be imported, duplicates and evidence coverage. Writes nothing."""
    from openshard.ingest import JobSpec, scan

    root = _repo_root(repo_path, allow_home_repo)
    spec = JobSpec(sources=_sources(sources), since=since, until=until, all_repos=all_repos,
                   allow_home_repo=allow_home_repo)
    result = scan(spec, root)
    if as_json:
        _emit(result, True, "")
        return
    c = result["counters"]
    d = result["decisions"]
    lines = [
        f"Discovered {c['discovered']} source file(s).",
        f"Would import {d.get('write', 0)} new, {d.get('supersede', 0)} updated; "
        f"{d.get('attach_live', 0)} already captured live (attachment); "
        f"{d.get('skip_duplicate', 0)} already imported.",
        f"Filtered {c['skipped_filtered']} ({', '.join(f'{k} {v}' for k, v in c['filtered_by_reason'].items()) or 'none'}); "
        f"quarantined {c['quarantined']}.",
    ]
    if result["repos"]:
        lines.append("Repositories: " + ", ".join(f"{k} ({v})" for k, v in result["repos"].items()))
    rng = result["date_range"]
    if rng.get("first"):
        lines.append(f"Sessions from {rng['first'][:10]} to {rng['last'][:10]}")
    cov = result["evidence_coverage"]
    if cov:
        lines.append("Evidence coverage:")
        for fname in ("task", "model", "tokens", "branch", "head_at_start", "repo", "approvals", "cost"):
            if fname in cov:
                lines.append(f"  {fname}: " + ", ".join(f"{k} {v}" for k, v in sorted(cov[fname].items())))
    click.echo("\n".join(lines))


@ingest_group.command("run")
@_with(_filter_options + [
    click.option("--no-update", is_flag=True, help="Skip sessions whose source grew since the last import."),
    click.option("--no-enrich", is_flag=True, help="Do not add historical git evidence."),
] + _common_options)
def ingest_run(sources, since, until, all_repos, no_update, no_enrich, repo_path, allow_home_repo, as_json) -> None:
    """Import history into new sealed receipts. Safe to re-run; Ctrl-C pauses (resumable)."""
    from openshard.ingest import JobSpec, run_job
    from openshard.ingest.store import ActiveJobError

    root = _repo_root(repo_path, allow_home_repo)
    spec = JobSpec(sources=_sources(sources), since=since, until=until, all_repos=all_repos,
                   allow_home_repo=allow_home_repo, no_update=no_update, enrich_git=not no_enrich)

    def progress(ev: dict) -> None:
        if not as_json and ev.get("kind") == "item":
            c = ev["counters"]
            click.echo(f"\r  {c['discovered']} processed: {_counts_line(c)}", nl=False, err=True)

    try:
        result = run_job(spec, repo_path=root, progress_cb=progress)
    except ActiveJobError as exc:
        raise click.ClickException(str(exc)) from None
    if not as_json:
        click.echo("", err=True)
    _emit(result.to_dict(), as_json, _result_text(result.to_dict()))


def _result_text(r: dict) -> str:
    c = r["counters"]
    state = r["state"]
    lines = [f"Import {r['job_id']}: {state}", f"  {_counts_line(c)}"]
    if c.get("filtered_by_reason"):
        lines.append("  filtered: " + ", ".join(f"{k} {v}" for k, v in c["filtered_by_reason"].items()))
    if state == "paused":
        lines.append(f"  Resume with: openshard ingest resume {r['job_id']}")
    if c.get("written"):
        lines.append("  Receipts are labelled 'Reconstructed from history'. See: openshard history")
    return "\n".join(lines)


@ingest_group.command("resume")
@click.argument("job_id")
@_with(_common_options)
def ingest_resume(job_id, repo_path, allow_home_repo, as_json) -> None:
    """Resume a paused or interrupted import job. Items already written are never written again."""
    from openshard.ingest import resume_job
    from openshard.ingest.jobs import JobNotFound, JobNotResumable
    from openshard.ingest.store import ActiveJobError

    root = _repo_root(repo_path, allow_home_repo)
    try:
        result = resume_job(job_id, repo_path=root)
    except JobNotFound:
        raise click.ClickException(f"No import job {job_id}") from None
    except (JobNotResumable, ActiveJobError) as exc:
        raise click.ClickException(str(exc)) from None
    _emit(result.to_dict(), as_json, _result_text(result.to_dict()))


@ingest_group.command("cancel")
@click.argument("job_id")
@_with(_common_options)
def ingest_cancel(job_id, repo_path, allow_home_repo, as_json) -> None:
    """Request cancellation of an import job (checked between items)."""
    from openshard.ingest import cancel_job

    root = _repo_root(repo_path, allow_home_repo)
    ok = cancel_job(job_id, repo_path=root)
    if not ok:
        raise click.ClickException(f"No active import job {job_id}")
    _emit({"job_id": job_id, "cancel_requested": True}, as_json, f"Cancellation requested for {job_id}.")


@ingest_group.command("status")
@click.argument("job_id", required=False)
@_with(_common_options)
def ingest_status(job_id, repo_path, allow_home_repo, as_json) -> None:
    """Show one import job (default: the latest), with counts and quarantined items."""
    from openshard.ingest import job_status, list_jobs

    root = _repo_root(repo_path, allow_home_repo)
    if job_id is None:
        jobs = list_jobs(repo_path=root)
        if not jobs:
            raise click.ClickException("No import jobs in this repository.")
        job_id = jobs[-1]["job_id"]
    status = job_status(job_id, repo_path=root)
    if status is None:
        raise click.ClickException(f"No import job {job_id}")
    lines = [f"Import {job_id}: {status['state']} (updated {status.get('updated_at')})",
             f"  {_counts_line(status.get('counters') or {})}"]
    for q in status["items"]["quarantined"]:
        lines.append(f"  quarantined {q.get('locator_display')}: {q.get('error_class')}")
    _emit(status, as_json, "\n".join(lines))


@ingest_group.command("list")
@_with(_common_options)
def ingest_list(repo_path, allow_home_repo, as_json) -> None:
    """List import jobs in this repository."""
    from openshard.ingest import list_jobs

    root = _repo_root(repo_path, allow_home_repo)
    jobs = list_jobs(repo_path=root)
    rows = [{"job_id": j["job_id"], "state": j.get("state"), "created_at": j.get("created_at"),
             "counters": j.get("counters")} for j in jobs]
    human = "\n".join(f"{r['job_id']}  {r['state']:<10}  {_counts_line(r['counters'] or {})}" for r in rows)
    _emit({"jobs": rows}, as_json, human or "No import jobs.")
