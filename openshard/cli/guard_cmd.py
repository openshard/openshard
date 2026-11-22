"""``openshard guard``: list the team's guards and run them on the working tree.

Guards are committed files in ``.openshard/guards/`` (see ``openshard/guards``).
Recorded, commit-bound evidence comes from ``openshard verify --commit``,
which runs a commit's guards alongside its other checks; ``guard run`` is the
quick local loop and records nothing.
"""

from __future__ import annotations

import json
import sys
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
