"""``openshard guard``: list the team's guards and run them on the working tree.

Guards are committed files in ``.openshard/guards/`` (see ``openshard/guards``).
Recorded, commit-bound evidence comes from ``openshard verify --commit``,
which runs a commit's guards alongside its other checks; ``guard run`` is the
quick local loop and records nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC
from pathlib import Path, PurePosixPath

import click


def _repo_root() -> Path:
    from openshard.util.git import run_git

    top = run_git(Path.cwd(), ["rev-parse", "--show-toplevel"])
    return Path(top.strip()) if top and top.strip() else Path.cwd()


def _ignored(root: Path) -> bool:
    """Whether git ignores new files in the guards folder (so guards would never be committed)."""
    from openshard.guards.model import GUARDS_DIR
    from openshard.util.git import run_git

    probe = f"{GUARDS_DIR.as_posix()}/example.yml"
    return run_git(root, ["check-ignore", "-q", "--no-index", probe]) is not None


_IGNORED_HINT = (
    "Git ignores .openshard/guards/, so guards would never be committed. Add these lines to .gitignore "
    "in place of '.openshard/':\n    .openshard/*\n    !.openshard/config.yml\n    !.openshard/guards/"
)


@click.group("guard")
def guard_group() -> None:
    """Guards: checks your team adds after correcting an agent, run on every later change."""


@guard_group.command("list")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def guard_list(as_json: bool) -> None:
    """List the guards in .openshard/guards/ and any file that is not a valid guard."""
    from openshard.guards.model import load_guards

    root = _repo_root()
    found = load_guards(root)
    ignored = _ignored(root)
    if as_json:
        click.echo(json.dumps({
            "schema": "openshard.guards.v1",
            "guards": [g.to_dict() for g in found.guards],
            "errors": [{"source_path": e.source_path, "message": e.message} for e in found.errors],
            "truncated": found.truncated,
            "git_ignored": ignored,
        }, indent=2))
        return
    if not found.guards and not found.errors:
        click.echo("No guards yet. Add one as .openshard/guards/<id>.yml (see docs/guards.md).")
    for g in found.guards:
        scope = ", ".join(g.paths)
        click.echo(f"  {g.id}  [{g.status}, {g.kind}]  {g.title}")
        click.echo(f"      scope: {scope}")
        if g.command:
            click.echo(f"      runs:  {' '.join(g.command)}" + (f"  (in {g.cwd})" if g.cwd else ""))
    for e in found.errors:
        click.echo(f"  ! {e.source_path}: {e.message} (not loaded, never run)")
    if found.truncated:
        click.echo("  ! More guard files than Openshard reads; the rest were skipped.")
    if ignored:
        click.echo("\n" + _IGNORED_HINT)


def _changed_since(root: Path, base: str) -> list[PurePosixPath] | None:
    from openshard.util.git import run_git

    out = run_git(root, ["diff", "--name-only", "-z", base])
    untracked = run_git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if out is None:
        return None
    names = [n for n in out.split("\0") if n] + [n for n in (untracked or "").split("\0") if n]
    return [PurePosixPath(n) for n in dict.fromkeys(names)]


@guard_group.command("run")
@click.argument("guard_ids", nargs=-1)
@click.option("--base", "base_ref", default="HEAD", show_default=True, metavar="REF",
              help="Run the guards whose scope covers files changed since REF (working tree included).")
@click.option("--all", "run_all", is_flag=True, default=False, help="Run every active guard.")
@click.option("--approve", is_flag=True, default=False, help="Also run guards whose command needs approval.")
@click.option("--timeout", default=600.0, show_default=True, type=click.FloatRange(min=1.0),
              help="Seconds before a guard is stopped (its outcome is then unknown).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def guard_run(guard_ids: tuple[str, ...], base_ref: str, run_all: bool, approve: bool, timeout: float,
              as_json: bool) -> None:
    """Run guards on the working tree, now. Records nothing.

    With no GUARD_IDS, runs the active guards whose scope covers what changed
    since --base (default: your uncommitted changes). Exit 0 when every guard
    that ran passed, 1 when one failed, 2 when one could not run.
    """
    from openshard.guards.model import load_guards
    from openshard.guards.select import guards_for_change
    from openshard.verification.plan import VerificationSource, classify_command_safety
    from openshard.verification.post_session import ORIGIN_GUARD, PlannedCheck, run_checks

    root = _repo_root()
    found = load_guards(root)
    active = [g for g in found.guards if g.runnable]
    if guard_ids:
        wanted = set(guard_ids)
        chosen = [(g, "named") for g in active if g.id in wanted]
        missing = sorted(wanted - {g.id for g, _ in chosen})
        if missing:
            click.echo(f"Not an active test guard: {', '.join(missing)}", err=True)
            sys.exit(2)
    elif run_all:
        chosen = [(g, "all") for g in active]
    else:
        changed = _changed_since(root, base_ref)
        if changed is None:
            click.echo(f"Could not list changes since {base_ref}.", err=True)
            sys.exit(2)
        chosen = guards_for_change(active, changed)

    planned = []
    for g, why in chosen:
        argv = list(g.command or ())
        safety, reason = classify_command_safety(argv, VerificationSource.config)
        planned.append(PlannedCheck(name=g.label, argv=argv, kind="test", origin=ORIGIN_GUARD,
                                    safety=safety.value, reason=reason, cwd=g.cwd, why=why))
    if not planned:
        message = "No guard applies to these changes." if active else "No active guards."
        if as_json:
            click.echo(json.dumps({"schema": "openshard.guard_run.v1", "results": [], "message": message}, indent=2))
        else:
            click.echo(message)
        return

    results = run_checks(planned, root, approve=approve, timeout=timeout, stream=not as_json,
                         on_start=None if as_json else (lambda c: click.echo(f"\n[guard] running {c.name}")))
    failed = any(r.status == "failed" for r in results)
    not_run = any(r.status in ("skipped", "unknown") for r in results)
    if as_json:
        click.echo(json.dumps({
            "schema": "openshard.guard_run.v1",
            "results": [{"guard": r.check.name.removeprefix("guard "), "status": r.status,
                         "exit_code": r.exit_code, "note": r.note} for r in results],
        }, indent=2))
    else:
        click.echo("")
        for r in results:
            click.echo(f"  {r.check.name}: {r.status}" + (f" ({r.note})" if r.note else ""))
        click.echo("\nLocal run only: nothing recorded. `openshard verify --commit <sha>` records guard results.")
    if failed:
        sys.exit(1)
    if not_run:
        sys.exit(2)


@guard_group.command("qualify")
@click.argument("guard_id")
@click.option("--bad", "bad_ref", required=True, metavar="REF",
              help="The commit with the mistake (usually the agent's commit). The guard must fail here.")
@click.option("--good", "good_ref", required=True, metavar="REF",
              help="The commit with the correction. The guard must pass here.")
@click.option("--runs", default=3, show_default=True, type=click.IntRange(1, 10),
              help="Times it must pass on the working tree, to catch a flaky guard.")
@click.option("--activate", is_flag=True, default=False,
              help="When qualified, also set 'status: active' in the guard file.")
@click.option("--approve", is_flag=True, default=False, help="Also run a guard command that needs approval.")
@click.option("--timeout", default=600.0, show_default=True, type=click.FloatRange(min=1.0),
              help="Seconds before one run is stopped (its outcome is then unknown).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def guard_qualify(guard_id: str, bad_ref: str, good_ref: str, runs: int, activate: bool, approve: bool,
                  timeout: float, as_json: bool) -> None:
    """Prove a guard catches the mistake: fails on --bad, passes on --good, stable on the working tree.

    Writes the result into the guard file's 'qualification' block (commit it
    with the guard). Exit 0 when qualified, 1 when it is not, 2 when it could
    not run.
    """
    from datetime import datetime

    from openshard.cli.gate_cmd import _resolve
    from openshard.guards.model import load_guards
    from openshard.guards.qualify import (
        COULD_NOT_RUN,
        QUALIFIED,
        qualify_guard,
        write_qualification,
    )

    root = _repo_root()
    guard = load_guards(root).by_id().get(guard_id)
    if guard is None:
        click.echo(f"No valid guard '{guard_id}' in .openshard/guards/ (see `openshard guard list`).", err=True)
        sys.exit(2)
    bad, good = _resolve(root, bad_ref), _resolve(root, good_ref)
    if not bad or not good:
        click.echo(f"Could not resolve {bad_ref if not bad else good_ref} to a commit.", err=True)
        sys.exit(2)
    if not as_json:
        click.echo(f"Qualifying guard {guard.id}: must fail on {bad[:12]}, pass on {good[:12]}, "
                   f"then pass {runs} times on the working tree.")
    result = qualify_guard(root, guard, bad, good, runs=runs, approve=approve, timeout=timeout)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if result.verdict != COULD_NOT_RUN:
        write_qualification(root / guard.source_path, result.to_record(at=stamp),
                            activate=activate and result.verdict == QUALIFIED)
    if as_json:
        click.echo(json.dumps({"schema": "openshard.guard_qualify.v1", **result.to_dict()}, indent=2))
    else:
        for r in result.runs:
            click.echo(f"  {r.where}: {r.status}" + (f" (exit {r.exit_code})" if r.exit_code is not None else "")
                       + (f" -- {r.note}" if r.note else ""))
        click.echo(f"\n{result.verdict}: {result.reason}")
        if result.verdict == QUALIFIED and not activate and guard.status != "active":
            click.echo("Set 'status: active' (or re-run with --activate) and commit the guard file to turn it on.")
        elif result.verdict != COULD_NOT_RUN:
            click.echo(f"Result written to {guard.source_path}.")
    sys.exit(0 if result.verdict == QUALIFIED else 2 if result.verdict == COULD_NOT_RUN else 1)


@guard_group.command("suggest")
@click.option("--scan", default=150, show_default=True, type=click.IntRange(1, 2000),
              help="How many recent commits (first parent) to read.")
@click.option("--window-days", default=14.0, show_default=True, type=click.FloatRange(min=0.1),
              help="How long after an agent commit a person's change still counts as a correction.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def guard_suggest(scan: int, window_days: float, as_json: bool) -> None:
    """List recent corrections: a person's commit that changed lines an agent commit wrote.

    Each one is a candidate guard. Agent commits are the ones marked in git
    (Openshard-Agent, Claude-Session, or an agent's Co-Authored-By). Reads git
    only; records nothing.
    """
    from openshard.guards.suggest import find_corrections

    root = _repo_root()
    found = find_corrections(root, scan=scan, window_days=window_days)
    if as_json:
        click.echo(json.dumps({"schema": "openshard.guard_suggest.v1",
                               "corrections": [c.to_dict() for c in found]}, indent=2))
        return
    if not found:
        click.echo(f"No corrections found in the last {scan} commits (a person's commit changing lines an "
                   f"agent commit wrote within {window_days:g} days).")
        return
    click.echo(f"{len(found)} correction(s) that could become guards:\n")
    for c in found:
        click.echo(f"  {c.good[:12]}  {c.subject}")
        click.echo(f"      fixed {c.lines_changed} line(s) from {c.agent} commit {c.bad[:12]} "
                   f"({c.evidence}), {c.days_after:.1f} days later")
        click.echo(f"      files: {', '.join(c.files[:5])}" + (" ..." if len(c.files) > 5 else ""))
    click.echo("\nTo turn one into a guard: write a test that fails on the agent's version, add "
               ".openshard/guards/<id>.yml, then\n  openshard guard qualify <id> --bad <agent commit> "
               "--good <correction> --activate")
