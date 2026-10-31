"""Verify what a session left uncommitted, on a private copy, without touching the checkout.

A session that commits gets commit verification (``commit_run``). Most
sessions end with their changes still uncommitted, so their Receipt had no
independent result unless someone ran ``openshard verify`` by hand. This is
the default for those sessions (``post_session_verify`` unset):

    the session's Receipt is finalised with changed files and no commit
    -> git writes a *snapshot* of the checkout as it is now: HEAD plus every
       tracked change and every untracked file git does not ignore, built in
       a temporary index, so the user's index, branch and files are untouched
       and no ref is created
    -> the checks the change calls for are picked as for a commit
       (``plan_commit`` against HEAD) and only ``safe`` ones run, in an
       isolated ``git worktree`` of the snapshot (never the live checkout,
       which the next turn may be editing)
    -> the outcome is appended to ``verifications.jsonl`` as an OpenShard
       re-run, **not bound to a commit**: a snapshot is not something anyone
       will merge. The attestation names the snapshot's tree so a reader can
       see exactly what ran.

What it will not do: approve anything, run in the user's checkout, create a
branch or commit anyone can see, or count as commit verification for the
merge gate. If the snapshot or the copy cannot be made, the run records the
checks as ``unknown`` (or records nothing when there is no HEAD), never
``failed``. Never raises from ``verify_snapshot`` except for a lock timeout.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openshard.verification.commit_checkout import commit_checkout
from openshard.verification.commit_run import (
    _JS_TOOLS,
    CommitRun,
    _not_run,
    _now,
    _run_in,
    _runnable,
)
from openshard.verification.post_session import (
    KIND_POST_SESSION,
    PlannedCheck,
    TreeState,
    build_attestation,
    load_attestations,
    plan_commit,
    record_attestation,
    tree_state,
)

STATUS_OK = "ok"
STATUS_NO_SNAPSHOT = "no_snapshot"
STATUS_NO_CHANGE = "no_change"
STATUS_ALREADY_VERIFIED = "already_verified"
STATUS_DRY_RUN = "dry_run"

UNATTENDED_LOCK_WAIT_SECONDS = 1800.0
_GIT_TIMEOUT = 120.0
_OID = re.compile(r"[0-9a-f]{40,64}")
# A fixed identity: the snapshot commit object is OpenShard's, never the user's,
# and git must not fail for want of a configured user.
_SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Openshard", "GIT_AUTHOR_EMAIL": "snapshot@openshard.invalid",
    "GIT_COMMITTER_NAME": "Openshard", "GIT_COMMITTER_EMAIL": "snapshot@openshard.invalid",
}


@dataclass(frozen=True)
class Snapshot:
    commit: str  # an unreferenced commit object whose tree is the snapshot
    tree: str
    base: str  # HEAD when the snapshot was taken
    base_tree: str | None


def _git(root: Path, args: list[str], env: dict[str, str] | None = None) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False,
            env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def take_snapshot(repo_root: Path) -> Snapshot | None:
    """Write the checkout's current content as an unreferenced commit object. None when git cannot.

    Uses a throwaway index file, so the user's staging area is never read or
    changed. ``.gitignore`` is respected, as it is for ``git add -A``.
    """
    head = (_git(repo_root, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]) or "").lower()
    if not _OID.fullmatch(head):
        return None
    fd, index_path = tempfile.mkstemp(prefix="openshard-snapshot-", suffix=".index")
    os.close(fd)
    try:
        os.unlink(index_path)  # git creates it; an empty file is not a valid index
        env = {"GIT_INDEX_FILE": index_path}
        if _git(repo_root, ["read-tree", head], env) is None:
            return None
        if _git(repo_root, ["add", "-A", "--", "."], env) is None:
            return None
        tree = (_git(repo_root, ["write-tree"], env) or "").lower()
        if not _OID.fullmatch(tree):
            return None
        commit = (_git(repo_root, ["commit-tree", tree, "-p", head, "-m", "Openshard verification snapshot"],
                       {**env, **_SNAPSHOT_IDENTITY}) or "").lower()
        if not _OID.fullmatch(commit):
            return None
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass
    base_tree = (_git(repo_root, ["rev-parse", f"{head}^{{tree}}"]) or "").lower()
    return Snapshot(commit=commit, tree=tree, base=head, base_tree=base_tree if _OID.fullmatch(base_tree) else None)


def snapshot_attested(attestations: list[dict], entry: dict, tree: str) -> bool:
    """An OpenShard re-run of exactly this snapshot tree is already recorded for *entry*."""
    ref = entry.get("receipt_id") or entry.get("run_id")
    for item in attestations:
        snap = item.get("snapshot")
        if (item.get("kind") == KIND_POST_SESSION and isinstance(snap, dict) and snap.get("tree") == tree
                and ref and ref in (item.get("receipt_id"), item.get("run_id"))):
            return True
    return False


def verify_snapshot(
    repo_root: Path,
    history_dir: Path,
    entry: dict,
    *,
    config: dict,
    timeout: float,
    dry_run: bool = False,
    stream: bool = True,
    on_plan: Callable[[list[PlannedCheck]], None] | None = None,
    on_start: Callable[[PlannedCheck], None] | None = None,
    lock_wait: float | None = UNATTENDED_LOCK_WAIT_SECONDS,
) -> CommitRun:
    """Snapshot the checkout and run the change's safe checks on an isolated copy of it."""
    from openshard.history.jsonl_store import history_file_lock

    with history_file_lock(history_dir / "verify-commit.state", timeout=lock_wait):
        snap = take_snapshot(repo_root)
        if snap is None:
            return CommitRun(STATUS_NO_SNAPSHOT)
        if snap.tree == snap.base_tree:
            # Nothing uncommitted: whatever the session changed is already in a commit,
            # which commit verification covers.
            return CommitRun(STATUS_NO_CHANGE)
        if snapshot_attested(load_attestations(history_dir), entry, snap.tree):
            return CommitRun(STATUS_ALREADY_VERIFIED)
        planned, truncated = plan_commit(repo_root, config, entry, snap.commit, snap.base)
        if on_plan is not None:
            on_plan(planned)
        if dry_run:
            return CommitRun(STATUS_DRY_RUN, planned)
        return _execute_snapshot(repo_root, snap, entry, planned, timeout=timeout, stream=stream,
                                 on_start=on_start, truncated=truncated)


def _execute_snapshot(repo_root: Path, snap: Snapshot, entry: dict, planned: list[PlannedCheck], *, timeout: float,
                      stream: bool, on_start: Callable[[PlannedCheck], None] | None, truncated: bool) -> CommitRun:
    runnable = _runnable(planned, approve=False)  # unattended: never approves
    started_at = _now()
    prepare_js = any(c.argv and c.argv[0] in _JS_TOOLS for c in runnable)
    with commit_checkout(repo_root, snap.commit, prepare_js=prepare_js, force_isolated=True) as checkout:
        if checkout.ready and checkout.path is not None:
            before = tree_state(checkout.path)
            results = _run_in(checkout.path, planned, runnable, checkout.env, approve=False, timeout=timeout,
                              stream=stream, on_start=on_start)
            after = tree_state(checkout.path)
        else:
            results = _not_run(planned, runnable, checkout.problem)
            before = after = TreeState(head=None, dirty=None)
        where: dict = {"mode": checkout.mode, "notes": ["ran in an isolated copy of a snapshot of uncommitted changes"]}
        if checkout.problem:
            where["problem"] = checkout.problem[:200]
    attestation = build_attestation(entry, results, before=before, after=after, started_at=started_at,
                                    completed_at=_now(), checkout=where, truncated=truncated,
                                    snapshot={"tree": snap.tree, "base": snap.base})
    record_attestation(repo_root, attestation)
    return CommitRun(STATUS_OK, planned, results, attestation)


__all__ = [
    "STATUS_ALREADY_VERIFIED", "STATUS_DRY_RUN", "STATUS_NO_CHANGE", "STATUS_NO_SNAPSHOT", "STATUS_OK",
    "Snapshot", "snapshot_attested", "take_snapshot", "verify_snapshot",
]
