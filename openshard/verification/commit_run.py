"""One commit verification run: plan from the change, run on the exact commit, record.

The CLI (``openshard verify --commit``) and nothing else calls this; capture
reaches it through a detached ``openshard verify --auto`` child
(``verification/auto.py``). One run at a time per repository: a sidecar
lock queues a burst of commits instead of racing them for the CPU.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openshard.verification.commit_checkout import commit_checkout
from openshard.verification.plan import CommandSafety
from openshard.verification.post_session import (
    CHECK_SKIPPED,
    CHECK_UNKNOWN,
    CheckRun,
    PlannedCheck,
    TreeState,
    build_attestation,
    check_environment,
    commit_attested,
    load_attestations,
    plan_commit,
    record_attestation,
    run_checks,
    tree_state,
)
from openshard.verification.setup_failure import rerun_setup_reason

STATUS_OK = "ok"
STATUS_DRY_RUN = "dry_run"
STATUS_ALREADY_VERIFIED = "already_verified"
STATUS_NOT_SESSION_COMMIT = "not_session_commit"
UNATTENDED_LOCK_WAIT_SECONDS = 3600.0
_JS_TOOLS = frozenset({"pnpm", "yarn", "npm", "bun"})


@dataclass
class CommitRun:
    status: str
    planned: list[PlannedCheck] = field(default_factory=list)
    results: list[CheckRun] = field(default_factory=list)
    attestation: dict | None = None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _runnable(planned: list[PlannedCheck], approve: bool) -> list[PlannedCheck]:
    return [c for c in planned if c.safety == CommandSafety.safe.value
            or (approve and c.safety == CommandSafety.needs_approval.value)]


def _not_run(planned: list[PlannedCheck], runnable: list[PlannedCheck], problem: str) -> list[CheckRun]:
    """No isolated copy: runnable checks are ``unknown`` (a missing package cache says nothing about the change)."""
    return [
        CheckRun(c, CHECK_UNKNOWN, note=rerun_setup_reason(problem), setup_failed=True) if c in runnable
        else CheckRun(c, CHECK_SKIPPED, note="needs approval (re-run with --approve)")
        for c in planned
    ]


def _missing_module(check: PlannedCheck, cwd: Path, env: dict[str, str]) -> str | None:
    """For ``<python> -m <module> ...``: the module when that interpreter cannot import it, else None.

    ``python -m pytest`` without pytest installed exits 1, which would read
    as a failed check; it says nothing about the change, so it is ``unknown``.
    """
    import subprocess

    argv = check.argv
    if len(argv) < 3 or argv[1] != "-m" or not argv[2].isidentifier():
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [argv[0], "-c", f"import {argv[2]}"], cwd=cwd, env=check_environment(env),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # the run itself will report it could not start
    return argv[2] if proc.returncode != 0 else None


def _run_in(path: Path, planned: list[PlannedCheck], runnable: list[PlannedCheck], env: dict[str, str], *,
            approve: bool, timeout: float, stream: bool,
            on_start: Callable[[PlannedCheck], None] | None) -> list[CheckRun]:
    missing = {id(c): m for c in runnable if (m := _missing_module(c, path, env))}
    ran = iter(run_checks([c for c in planned if id(c) not in missing], path, approve=approve, timeout=timeout,
                          stream=stream, env=env or None, on_start=on_start))
    return [
        CheckRun(c, CHECK_UNKNOWN, setup_failed=True,
                 note=rerun_setup_reason(f"{missing[id(c)]} is not installed for {Path(c.argv[0]).name}"))
        if id(c) in missing else next(ran)
        for c in planned
    ]


def _execute(repo_root: Path, sha: str, entry: dict, planned: list[PlannedCheck], *, approve: bool,
             timeout: float, stream: bool, on_start: Callable[[PlannedCheck], None] | None,
             truncated: bool, isolate: bool) -> CommitRun:
    runnable = _runnable(planned, approve)
    started_at = _now()
    prepare_js = any(c.argv and c.argv[0] in _JS_TOOLS for c in runnable)
    with commit_checkout(repo_root, sha, prepare_js=prepare_js, force_isolated=isolate) as checkout:
        if checkout.ready and checkout.path is not None:
            before = tree_state(checkout.path)
            results = _run_in(checkout.path, planned, runnable, checkout.env, approve=approve, timeout=timeout,
                              stream=stream, on_start=on_start)
            after = tree_state(checkout.path)
        else:
            results = _not_run(planned, runnable, checkout.problem)
            before = after = TreeState(head=None, dirty=None)
        where: dict = {"mode": checkout.mode, "notes": checkout.notes[:4]}
        if checkout.problem:
            where["problem"] = checkout.problem[:200]
    attestation = build_attestation(entry, results, before=before, after=after, started_at=started_at,
                                    completed_at=_now(), commit=sha, checkout=where, truncated=truncated)
    record_attestation(repo_root, attestation)
    return CommitRun(STATUS_OK, planned, results, attestation)


def verify_commit(
    repo_root: Path,
    history_dir: Path,
    entry: dict,
    sha: str,
    *,
    config: dict,
    base: str | None,
    approve: bool,
    unattended: bool,
    timeout: float,
    dry_run: bool = False,
    stream: bool = True,
    on_plan: Callable[[list[PlannedCheck]], None] | None = None,
    on_start: Callable[[PlannedCheck], None] | None = None,
) -> CommitRun:
    """Verify *sha* for *entry*. Raises ``LockTimeoutError`` when another run holds the lock too long."""
    from openshard.history.jsonl_store import history_file_lock

    if unattended and sha not in session_commits(entry):
        # Capture only ever verifies what the session itself committed.
        return CommitRun(STATUS_NOT_SESSION_COMMIT)
    wait = UNATTENDED_LOCK_WAIT_SECONDS if unattended else None
    with history_file_lock(history_dir / "verify-commit.state", timeout=wait):
        if unattended and commit_attested(load_attestations(history_dir), entry, sha):
            return CommitRun(STATUS_ALREADY_VERIFIED)
        planned, truncated = plan_commit(repo_root, config, entry, sha, base if base != sha else None)
        if on_plan is not None:
            on_plan(planned)
        if dry_run:
            return CommitRun(STATUS_DRY_RUN, planned)
        # Unattended runs never share the live checkout with the session's next turn.
        return _execute(repo_root, sha, entry, planned, approve=approve and not unattended, timeout=timeout,
                        stream=stream, on_start=on_start, truncated=truncated, isolate=unattended)


def session_commits(entry: dict) -> set[str]:
    """The commits git observed *entry*'s session create (its resulting commit among them)."""
    from openshard.history.receipt_evidence import commit_value

    raw = entry.get("session_commits")
    shas = raw.get("shas") if isinstance(raw, dict) else None
    out = {s.lower() for s in shas if isinstance(s, str)} if isinstance(shas, list) else set()
    result = commit_value(entry)
    return out | ({result.lower()} if result else set())


__all__ = [
    "STATUS_ALREADY_VERIFIED", "STATUS_DRY_RUN", "STATUS_NOT_SESSION_COMMIT", "STATUS_OK", "CommitRun",
    "session_commits", "verify_commit",
]
