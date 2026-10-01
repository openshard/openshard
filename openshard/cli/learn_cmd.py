"""``openshard learn``: inspect what OpenShard has learned from this repository's runs.

    learn signals [TASK]   the signals derived from history, or the ones a task would get
    learn inspect ID       one signal in full: counts, freshness, supporting Receipts
    learn last             what learning did on the most recent OSN run
    learn impact           outcomes of runs with and without learning (observational)

Everything is derived from ``.openshard/runs.jsonl`` at read time; nothing is
written. Human output is compact; ``--json`` gives the structured records.
"""
from __future__ import annotations

import json
from pathlib import Path

import click


@click.group("learn")
def learn_group() -> None:
    """Evidence-backed learning from prior OpenShard runs (read-only)."""


def _root() -> Path:
    from openshard.cli.ingest import _repo_root

    return _repo_root(None, False)


def _index(repo: str | None):
    from openshard.learning.signals import load_learning_index, repo_key

    root = _root()
    key = repo or repo_key(root)
    return load_learning_index(root, repo=key), key


def _header(index, repo: str) -> list[str]:
    lines = [
        "Learning signals",
        f"  Repository   {repo}",
        f"  Receipts     {index.receipts_observed} recorded · {index.receipts_with_evidence} with "
        "OpenShard-observed verification",
    ]
    if index.excluded:
        parts = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(index.excluded.items()))
        lines.append(f"  Not evidence {parts}")
    if index.unreadable:
        lines.append(f"  Unreadable   {index.unreadable} history line(s) skipped")
    return lines


def _signal_line(n: int, s, reasons: tuple[str, ...] | None = None) -> list[str]:
    lines = [f"  {n}. {s.summary}",
             f"     {s.kind} · {s.strength} · {s.freshness} · {s.samples} Receipt(s) · {s.signal_id}"]
    if reasons:
        lines.append(f"     why: {', '.join(reasons)}")
    return lines


@learn_group.command("signals")
@click.argument("task", nargs=-1)
@click.option("--all", "show_all", is_flag=True, default=False,
              help="Include anecdotal (single-Receipt) and stale signals.")
@click.option("--repo", default=None, help="Repository identity to learn from (default: this checkout's).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def learn_signals(task: tuple[str, ...], show_all: bool, repo: str | None, as_json: bool) -> None:
    """List learning signals, or with TASK, the ones OSN would supply to it and why."""
    from openshard.learning.retrieval import consult

    index, key = _index(repo)
    task_text = " ".join(task).strip()
    if task_text:
        ctx = consult(task_text, index, repo=key)
        if as_json:
            click.echo(json.dumps({
                "repo": key, "task_shape": ctx.shape.to_dict() if ctx.shape else None, "status": ctx.status,
                "signals": [{**r.to_record(), "summary": r.signal.summary} for r in ctx.retrieved],
                "recommended_checks": [c.label for c in ctx.recommended_checks],
                "signals_considered": ctx.signals_considered,
            }, indent=2))
            return
        for line in _header(index, key):
            click.echo(line)
        shape = ctx.shape
        click.echo(f"  Task class   {shape.task_category if shape else 'unknown'}")
        if not ctx.retrieved:
            click.echo(f"\n  No relevant signals ({ctx.status.replace('_', ' ')}).")
            return
        click.echo("\nSignals OSN would supply (advisory)")
        for i, r in enumerate(ctx.retrieved, start=1):
            for line in _signal_line(i, r.signal, r.reasons):
                click.echo(line)
        for rec in ctx.recommended_checks:
            click.echo(f"\n  Check history suggests: `{rec.label}` (caught {rec.runs_caught} of {rec.runs}; not run automatically)")
        supporting = len(ctx.supporting_receipt_ids)
        click.echo(f"\nEvidence: {supporting} supporting Receipt(s). `openshard learn inspect <id>` for detail.")
        return

    signals = [s for s in index.signals if show_all or s.surfaceable]
    if as_json:
        click.echo(json.dumps({
            "repo": key,
            "receipts_observed": index.receipts_observed,
            "receipts_with_evidence": index.receipts_with_evidence,
            "excluded": index.excluded,
            "unreadable": index.unreadable,
            "signals": [s.to_dict() for s in signals],
            "hidden": len(index.signals) - len(signals),
        }, indent=2))
        return
    for line in _header(index, key):
        click.echo(line)
    if not signals:
        hidden = len(index.signals)
        click.echo("\n  No signals with enough evidence yet." + (
            f" {hidden} anecdotal or stale signal(s) hidden; --all shows them." if hidden else ""))
        return
    by_cat: dict[str, list] = {}
    for s in signals:
        by_cat.setdefault(s.task_category or "unclassified", []).append(s)
    n = 0
    for cat in sorted(by_cat):
        click.echo(f"\nTask class: {cat}")
        for s in by_cat[cat]:
            n += 1
            for line in _signal_line(n, s):
                click.echo(line)
    hidden = len(index.signals) - len(signals)
    if hidden:
        click.echo(f"\n  {hidden} anecdotal or stale signal(s) hidden; --all shows them.")


@learn_group.command("inspect")
@click.argument("signal_id")
@click.option("--repo", default=None, help="Repository identity to learn from (default: this checkout's).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def learn_inspect(signal_id: str, repo: str | None, as_json: bool) -> None:
    """Show one signal: its counts, evidence, freshness and supporting Receipts."""
    index, key = _index(repo)
    s = index.get(signal_id.strip())
    if s is None:
        raise click.ClickException(
            f"No signal {signal_id} in this repository's history. `openshard learn signals --all` lists them."
        )
    if as_json:
        click.echo(json.dumps(s.to_dict(), indent=2))
        return
    click.echo(f"Signal {s.signal_id}")
    click.echo(f"  {s.summary}")
    click.echo(f"  Kind         {s.kind}")
    click.echo(f"  Scope        {key} · {s.task_category or 'unclassified'}")
    click.echo(f"  Subject      {', '.join(f'{k}={v}' for k, v in s.subject.items())}")
    click.echo(f"  Evidence     {s.samples} Receipt(s) · {s.strength} · "
               f"{', '.join(s.evidence_sources) or 'no observed verification source'}")
    click.echo(f"  Seen         {s.first_seen or 'unknown'} → {s.last_seen or 'unknown'} ({s.freshness})")
    for k, v in s.stats.items():
        click.echo(f"    {k}: {'unknown' if v is None else v}")
    if s.areas:
        click.echo(f"  Areas        {', '.join(s.areas)}")
    click.echo(f"  Receipts     {', '.join(s.receipt_ids)}")
    if s.shard_ids:
        click.echo(f"  Shards       {', '.join(s.shard_ids)}")
    if not s.surfaceable:
        why = "one Receipt is an anecdote" if s.strength == "anecdotal" else "its evidence is stale"
        click.echo(f"  Not surfaced to OSN: {why}.")


@learn_group.command("last")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def learn_last(as_json: bool) -> None:
    """What learning did on the most recent OSN run, and how that run ended."""
    from openshard.learning.signals import read_entries

    entries, _ = read_entries(_root() / ".openshard" / "runs.jsonl")
    last = next((e for e in reversed(entries) if e.get("executor") == "osn_loop"), None)
    if last is None:
        raise click.ClickException("No OSN run recorded in this repository yet.")
    raw_rec, raw_ver = last.get("learning"), last.get("verification")
    rec = raw_rec if isinstance(raw_rec, dict) else None
    ver: dict = raw_ver if isinstance(raw_ver, dict) else {}
    outcome = {
        "receipt_id": last.get("receipt_id"),
        "task_title": last.get("task_title"),
        "verification": ver.get("status"),
        "attempts": len(((last.get("osn_loop") or {}).get("attempts")) or []),
        "estimated_cost": last.get("estimated_cost"),
        "duration_seconds": last.get("duration_seconds"),
    }
    if as_json:
        click.echo(json.dumps({"learning": rec, "outcome": outcome}, indent=2))
        return
    click.echo(f"Last OSN run · {outcome['task_title'] or 'untitled'} · Receipt {outcome['receipt_id']}")
    if rec is None:
        click.echo("  Learning     not recorded (this run predates Learning Loop V1)")
    elif not rec.get("used"):
        click.echo(f"  Learning     {rec.get('status', 'unknown').replace('_', ' ')}")
    else:
        routing = rec.get("routing") or {}
        verification = rec.get("verification") or {}
        click.echo(f"  Learning     {rec.get('signals_used', 0)} prior verified signal(s) considered")
        click.echo(f"  Context      {'supplied to the model' if rec.get('context_supplied') else 'not supplied'}")
        click.echo(f"  Routing      influenced: {'yes' if routing.get('influenced') else 'no'}"
                   f" ({routing.get('reason')})")
        click.echo(f"  Verification influenced: {'yes' if verification.get('influenced') else 'no'}"
                   " (recommendations are advisory)")
        for c in verification.get("recommended_checks") or []:
            click.echo(f"    suggested check: `{c.get('label')}`")
        for s in rec.get("signals") or []:
            click.echo(f"    {s.get('signal_id')} · {s.get('kind')} · {s.get('strength')} · "
                       f"{', '.join(s.get('reasons') or [])}")
    cost = outcome["estimated_cost"]
    click.echo(f"  Outcome      verification {outcome['verification'] or 'unknown'} · "
               f"{outcome['attempts']} attempt(s) · cost {'unknown' if cost is None else f'${cost:.4f}'}")


@learn_group.command("impact")
@click.option("--task-class", "task_category", default=None, help="Only runs of this task class.")
@click.option("--repo", default=None, help="Repository identity to learn from (default: this checkout's).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def learn_impact(task_category: str | None, repo: str | None, as_json: bool) -> None:
    """Compare outcomes of OSN runs that used learning with those that did not."""
    from openshard.learning.impact import measure

    index, key = _index(repo)
    report = measure(index, task_category=task_category)
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    click.echo(f"Learning impact · {key}" + (f" · {task_category}" if task_category else ""))
    labels = {"learning_used": "Used learning", "learning_not_used": "No learning used",
              "before_learning_recorded": "Before V1"}
    for c in report.cohorts:
        if not c.runs:
            click.echo(f"  {labels[c.name]:<17} no runs")
            continue
        cost = c.cost_per_verified_success_usd
        click.echo(
            f"  {labels[c.name]:<17} {c.runs} run(s) · verified {c.verified_successes}/{c.observed} observed · "
            f"first-attempt pass {c.first_attempt_passed}/{c.observed} · retried {c.retried} · "
            f"cost/verified success {'unknown' if cost is None else f'${cost:.4f}'}"
        )
    if report.followups:
        click.echo("\n  Signals given to later runs")
        for f in report.followups:
            click.echo(f"    {f.signal_id}: {f.later_runs} later run(s), "
                       f"{f.verified_successes}/{f.observed} verified, "
                       f"first-attempt pass {f.first_attempt_passed}/{f.observed}")
    click.echo(f"\n  {report.disclaimer}")
