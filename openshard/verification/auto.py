"""Automatic verification of captured sessions.

Two triggers live here, both on by default:

* **commit verification** -- a Receipt that names its resulting commit gets
  that exact commit re-run (``schedule_commit_verify`` below);
* **post-session verification** -- a Receipt that changed files but made no
  commit gets the checks its change calls for re-run on a *private copy* of
  the uncommitted changes (``openshard verify --snapshot --auto``,
  ``verification/snapshot_run.py``), at session end, or for per-turn
  Receipts once their Task settles (``settle.py``).

``post_session_verify`` in ``.openshard/config.yml`` chooses the second:

    # unset (default) or "snapshot": a private copy, never the checkout
    post_session_verify: safe   # the older run in the checkout itself
    post_session_verify: off    # no automatic re-run at all (commits included)

``OPENSHARD_NO_AUTO_VERIFY`` turns both off for a process.

What it will and will not do
----------------------------
* Only checks the shared safety rules class as ``safe`` run; nothing is
  approved and ``--from-observed`` is never passed.
* A session that changed no files starts nothing.
* The child is detached and gets no stdio: a session-end hook never waits
  for a test suite. Its result is the attestation it appends, nothing else.
* A snapshot result is never bound to a commit, so it never counts as
  commit verification for the merge gate.
* Nothing is recorded here. If the child cannot start, the Receipt simply
  keeps the evidence it had.

Never raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

CONFIG_KEY = "post_session_verify"
MODE_SAFE = "safe"
MODE_OFF = "off"
# The default (key unset): uncommitted changes are re-run on a private copy
# (``snapshot_run``), never in the live checkout. ``safe`` keeps the older
# in-place run; ``off`` (or false) turns every automatic run off.
MODE_SNAPSHOT = "snapshot"
ENV_DISABLE = "OPENSHARD_NO_AUTO_VERIFY"

Spawner = Callable[[list[str], Path], bool]


def post_session_mode(repo_root: Path, env: dict | os._Environ | None = None) -> str | None:
    """How a Receipt without a commit is re-run: ``snapshot`` (default), ``safe``, or None (off)."""
    env = os.environ if env is None else env
    if env.get(ENV_DISABLE):
        return None
    try:
        path = repo_root / ".openshard" / "config.yml"
        # Cheap pre-check: session-end hooks run on a latency budget, so the
        # YAML parser is only loaded for a config that mentions the key.
        if not path.is_file() or CONFIG_KEY.encode() not in path.read_bytes():
            return MODE_SNAPSHOT
        from openshard.config.settings import load_config_safe

        config, valid, _ = load_config_safe(cwd=repo_root)
        if not valid:
            return None  # an unreadable config never starts anything new
        value = config.get(CONFIG_KEY)
        if value is False or value == MODE_OFF:
            return None
        return MODE_SAFE if value == MODE_SAFE else MODE_SNAPSHOT
    except Exception:
        return None


def auto_verify_enabled(repo_root: Path, env: dict | os._Environ | None = None) -> bool:
    """True unless this repository's config or the environment turns post-session verification off."""
    return post_session_mode(repo_root, env) is not None


def verify_argv(receipt_ref: str) -> list[str]:
    # -P: the child runs in the repository; its working tree must never shadow OpenShard's own modules.
    return [sys.executable, "-P", "-m", "openshard.cli.entrypoint", "verify", "--receipt", receipt_ref, "--json"]


def snapshot_verify_argv(receipt_ref: str) -> list[str]:
    return [sys.executable, "-P", "-m", "openshard.cli.entrypoint", "verify", "--receipt", receipt_ref,
            "--snapshot", "--auto", "--json"]


def _spawn_detached(argv: list[str], cwd: Path) -> bool:
    kwargs: dict = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "cwd": cwd, "close_fds": True,
        # The child's own checks must not re-trigger this path.
        "env": {**os.environ, ENV_DISABLE: "1"},
    }
    if sys.platform == "win32":
        # Same flags as the capture service spawn (claude_capture_client.spawn_service).
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 - fixed argv, no shell
    return True


def schedule_post_session_verify(
    repo_root: Path,
    entry: dict | None,
    *,
    spawner: Spawner | None = None,
    env: dict | os._Environ | None = None,
) -> bool:
    """Start a detached safe-only ``openshard verify`` for *entry* when enabled. Returns whether it started.

    The default mode runs on a private copy of the uncommitted changes
    (``--snapshot``); ``post_session_verify: safe`` runs in the checkout.
    """
    try:
        if not isinstance(entry, dict):
            return False
        mode = post_session_mode(repo_root, env)
        if mode is None:
            return False
        ref = entry.get("receipt_id") or entry.get("run_id")
        if not isinstance(ref, str) or not ref:
            return False
        argv = verify_argv(ref) if mode == MODE_SAFE else snapshot_verify_argv(ref)
        return bool((spawner or _spawn_detached)(argv, repo_root))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Commit verification (on by default)
# ---------------------------------------------------------------------------
#
# A session that *commits* has something exact to verify. When a captured
# Receipt names its resulting commit (``receipt_evidence.commit_value``: git
# observed the session create it), capture starts
#
#     openshard verify --receipt <id> --commit <sha> --auto --json
#
# detached. That run picks the checks the change calls for, runs them on
# exactly that commit (an isolated copy when the checkout is not clean at it),
# never approves anything, and records an attestation bound to the commit --
# which is what lets a Receipt show green Verified. It is on unless the
# repository says ``post_session_verify: off`` or ``OPENSHARD_NO_AUTO_VERIFY``
# is set. Each (Receipt, commit) pair is requested once per process; the
# child itself skips a commit already verified, under a per-repository lock.

_requested: set[tuple[str, str]] = set()


def commit_verify_enabled(repo_root: Path, env: dict | os._Environ | None = None) -> bool:
    """True unless the environment or this repository's config turns commit verification off."""
    env = os.environ if env is None else env
    if env.get(ENV_DISABLE):
        return False
    try:
        path = repo_root / ".openshard" / "config.yml"
        if not path.is_file() or CONFIG_KEY.encode() not in path.read_bytes():
            return True
        from openshard.config.settings import load_config_safe

        config, valid, _ = load_config_safe(cwd=repo_root)
        value = config.get(CONFIG_KEY) if valid else None
        return not (value is False or value == MODE_OFF)
    except Exception:
        return True


REQUEST_MARKER_SECONDS = 2 * 3600
_MARKER_DIR = "verify-requests"


def _marker(repo_root: Path, ref: str, commit: str) -> Path:
    safe_ref = "".join(ch for ch in ref if ch.isalnum() or ch in "_-")[:64]
    return repo_root / ".openshard" / _MARKER_DIR / f"{safe_ref}-{commit[:16]}"


def _recently_requested(repo_root: Path, ref: str, commit: str) -> bool:
    """A child for this (Receipt, commit) was started recently -- by this or another hook process."""
    import time

    try:
        return time.time() - _marker(repo_root, ref, commit).stat().st_mtime < REQUEST_MARKER_SECONDS
    except OSError:
        return False


def _mark_requested(repo_root: Path, ref: str, commit: str) -> None:
    try:
        path = _marker(repo_root, ref, commit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def commit_verify_argv(receipt_ref: str, commit: str) -> list[str]:
    return [sys.executable, "-P", "-m", "openshard.cli.entrypoint", "verify", "--receipt", receipt_ref,
            "--commit", commit, "--auto", "--json"]


def schedule_commit_verify(
    repo_root: Path,
    entry: dict | None,
    *,
    spawner: Spawner | None = None,
    env: dict | os._Environ | None = None,
) -> bool:
    """Start a detached unattended verification of *entry*'s resulting commit. Returns whether it started."""
    try:
        if not isinstance(entry, dict) or not commit_verify_enabled(repo_root, env):
            return False
        from openshard.history.receipt_evidence import commit_value

        commit = commit_value(entry)
        ref = entry.get("receipt_id") or entry.get("run_id")
        if not commit or not isinstance(ref, str) or not ref:
            return False
        key = (ref, commit)
        if key in _requested or _recently_requested(repo_root, ref, commit):
            return False
        from openshard.verification.post_session import commit_attested, load_attestations

        if commit_attested(load_attestations(repo_root / ".openshard"), entry, commit):
            _requested.add(key)
            return False
        started = bool((spawner or _spawn_detached)(commit_verify_argv(ref, commit), repo_root))
        if started:
            _requested.add(key)
            _mark_requested(repo_root, ref, commit)
        return started
    except Exception:
        return False


def schedule_for_receipt(repo_root: Path, entry: dict | None, *, finalized: bool, turn_ended: bool) -> bool:
    """Capture's one entry point after a fold. Never raises.

    A Receipt that names its resulting commit gets commit verification at a
    completed turn and at session end. Only a Receipt *without* a commit gets
    the opt-in working-tree run: at session end, or for per-turn Receipts once
    their Task settles (``verification/settle.py``: no new prompt for the
    settle period after a turn that changed files, or the branch changed).
    For a Receipt with a commit that unbound run would only add a weaker,
    later result.
    """
    try:
        if not isinstance(entry, dict) or not (finalized or turn_ended):
            return False
        from openshard.history.receipt_evidence import commit_value
        from openshard.verification import settle

        settle.run_due_all(lambda: [repo_root])  # inline hooks have no timer: settle quiet sessions at any later seal
        has_commit = bool(commit_value(entry))
        settled_now = _is_sealed_turn(entry) and auto_verify_enabled(repo_root) and settle.note_sealed_turn(
            repo_root, entry, has_commit=has_commit)
        if has_commit:
            return schedule_commit_verify(repo_root, entry) or settled_now
        if _is_sealed_turn(entry):  # a turn is re-run when its Task settles, not at its Stop
            return settled_now
        if not finalized:
            return False
        if post_session_mode(repo_root) == MODE_SNAPSHOT and not changed_files(entry):
            return False  # nothing changed, nothing to verify (the explicit ``safe`` mode keeps its old behaviour)
        return schedule_post_session_verify(repo_root, entry)
    except Exception:
        return False


_NOT_THIS_TURNS = frozenset({"pre_existing", "other_session"})


def _is_sealed_turn(entry: dict) -> bool:
    capture = entry.get("capture")
    return isinstance(capture, dict) and capture.get("receipt_scope") == "turn" and isinstance(
        capture.get("turn_sealed_at"), str)


def changed_files(entry: dict) -> bool:
    """*entry* records at least one file its own work changed (not pre-existing, not another session's)."""
    files = entry.get("files_detail")
    return any(
        isinstance(f, dict) and f.get("path") and f.get("attribution") not in _NOT_THIS_TURNS
        for f in (files if isinstance(files, list) else [])
    )


def sealed_turn_changed_files(entry: dict) -> bool:
    """A sealed per-turn Receipt (docs/per-turn-receipts.md) with at least one file the turn changed."""
    return _is_sealed_turn(entry) and changed_files(entry)


__all__ = [
    "CONFIG_KEY", "ENV_DISABLE", "MODE_OFF", "MODE_SAFE", "MODE_SNAPSHOT", "auto_verify_enabled", "changed_files",
    "commit_verify_argv", "commit_verify_enabled", "post_session_mode", "schedule_commit_verify",
    "schedule_for_receipt", "schedule_post_session_verify", "sealed_turn_changed_files", "snapshot_verify_argv",
    "verify_argv",
]
