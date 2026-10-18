"""``openshard verdict``: one machine-readable verdict for a commit or a task.

The schema and exit codes are documented in docs/verdict.md; the logic is
``verification/verdict.py``. Nothing runs and nothing is written.
"""

from __future__ import annotations

import json
import sys

import click


def _resolve_commit(root, ref: str) -> str | None:
    from openshard.util.git import run_git
    from openshard.verification.change_checks import is_sha

    resolved = (run_git(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]) or "").strip().lower()
    if is_sha(resolved):
        return resolved
    # A full commit id this clone does not have can still be named by evidence recorded here.
    return ref.lower() if is_sha(ref) and len(ref) in (40, 64) else None


def _answer(commit_ref: str | None, task_ref: str | None) -> dict:
    from openshard.history.locate import locate_history
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.store import load_history
    from openshard.verification import verdict
    from openshard.verification.post_session import load_attestations

    kind, ident = ("commit", commit_ref) if commit_ref else ("task", task_ref or "")
    try:
        loc = locate_history(with_identity=False)
    except Exception:
        return verdict.error(kind, ident, "No OpenShard history found from this directory.")
    attestations = load_attestations(loc.runs_path.parent)
    if commit_ref:
        sha = _resolve_commit(loc.root, commit_ref)
        if sha is None:
            return verdict.error(kind, ident, f"{commit_ref!r} is not a commit in this repository.")
        from openshard.verification.post_session import commit_tree

        return verdict.commit_verdict(sha, attestations, tree_oid=commit_tree(loc.root, sha))
    entries = load_history(loc.runs_path, coerce=False) if loc.runs_path.exists() else []
    return verdict.task_verdict(
        ident, entries, attestations,
        build_receipt=lambda entry, index, post: build_shard_receipt(
            entry, index=index, post_session_verification=post),
    )


class _UsageError(click.UsageError):
    """Usage errors exit 3 (error), never 2: 2 means "unknown" to a caller of this command."""

    exit_code = 3


class _VerdictCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            raise _UsageError(exc.message, exc.ctx) from exc


@click.command("verdict", cls=_VerdictCommand)
@click.option("--commit", "commit_ref", default=None, metavar="SHA", help="The commit to give a verdict on.")
@click.option("--task", "task_ref", default=None, metavar="ID", help="The task (task_id) to give a verdict on.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the verdict document.")
def verdict_cmd(commit_ref: str | None, task_ref: str | None, as_json: bool) -> None:
    """Say whether a commit or a task is verified, from the evidence already recorded.

    Exit 0: verified (an outcome OpenShard or CI observed passed). 1: failed.
    2: unknown or not checked yet (an agent's own pass is unknown). 3: error.
    Nothing runs; use ``openshard verify`` to produce evidence.
    """
    if (commit_ref is None) == (task_ref is None):
        raise _UsageError("give exactly one of --commit or --task")
    try:
        doc = _answer(commit_ref, task_ref)
    except Exception as exc:  # an unreadable store is an error verdict, never a pass
        from openshard.verification import verdict

        doc = verdict.error("commit" if commit_ref else "task", commit_ref or task_ref or "", type(exc).__name__)
    if as_json:
        click.echo(json.dumps(doc, indent=2))
    else:
        subject = doc["subject"]
        click.echo(f"{subject['kind']} {subject['id']}: {doc['verdict']} ({doc['reason']})")
        if doc.get("message"):
            click.echo(doc["message"])
    sys.exit(doc["exit_code"])


__all__ = ["verdict_cmd"]
