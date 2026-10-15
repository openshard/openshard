"""Automatic verification of captured sessions.

Two triggers live here: commit verification (on by default, see
``schedule_commit_verify`` below) and the older opt-in post-session
verification of whatever the working tree holds, described first.

Opt-in post-session verification: run ``openshard verify`` when a captured session closes.

An external agent's Receipt ends with, at best, the agent's own account of
its checks. ``openshard verify`` replaces that with an outcome OpenShard
observed, but only if somebody runs it. With

    post_session_verify: safe

in ``.openshard/config.yml`` the capture path starts it when a session's
Receipt is finalised (a session-end event, or the stale sweep closing a
session that never sent one).

What it will and will not do
----------------------------
* Off unless the repository's config turns it on; ``OPENSHARD_NO_AUTO_VERIFY``
  turns it off for a process regardless.
* It starts exactly ``openshard verify --receipt <id> --json``: never
  ``--approve``, never ``--from-observed``. So only checks the shared safety
  rules class as ``safe`` run; ``needs_approval`` and ``blocked`` checks are
  recorded as skipped, as they are for a person who runs the command.
* The child is detached and gets no stdio: a session-end hook never waits
  for a test suite. Its result is the attestation it appends, nothing else.
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
ENV_DISABLE = "OPENSHARD_NO_AUTO_VERIFY"

Spawner = Callable[[list[str], Path], bool]


def auto_verify_enabled(repo_root: Path, env: dict | os._Environ | None = None) -> bool:
    """True only when this repository's config asks for post-session verification."""
    env = os.environ if env is None else env
    if env.get(ENV_DISABLE):
        return False
    try:
        path = repo_root / ".openshard" / "config.yml"
        # Cheap pre-check: session-end hooks run on a latency budget, so the
        # YAML parser is only loaded for a config that mentions the key.
        if not path.is_file() or CONFIG_KEY.encode() not in path.read_bytes():
            return False
        from openshard.config.settings import load_config_safe

        config, valid, _ = load_config_safe(cwd=repo_root)
        return bool(valid) and config.get(CONFIG_KEY) == MODE_SAFE
    except Exception:
        return False


def verify_argv(receipt_ref: str) -> list[str]:
    # -P: the child runs in the repository; its working tree must never shadow OpenShard's own modules.
    return [sys.executable, "-P", "-m", "openshard.cli.entrypoint", "verify", "--receipt", receipt_ref, "--json"]


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
    """Start a detached safe-only ``openshard verify`` for *entry* when enabled. Returns whether it started."""
    try:
        if not isinstance(entry, dict) or not auto_verify_enabled(repo_root, env):
            return False
        ref = entry.get("receipt_id") or entry.get("run_id")
        if not isinstance(ref, str) or not ref:
            return False
        return bool((spawner or _spawn_detached)(verify_argv(ref), repo_root))
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

MODE_OFF = "off"
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
    the opt-in working-tree run at session end: for a Receipt with a commit
    that unbound run would only add a weaker, later result.
    """
    try:
        if not isinstance(entry, dict) or not (finalized or turn_ended):
            return False
        from openshard.history.receipt_evidence import commit_value

        if commit_value(entry):
            return schedule_commit_verify(repo_root, entry)
        return finalized and schedule_post_session_verify(repo_root, entry)
    except Exception:
        return False


__all__ = [
    "CONFIG_KEY", "ENV_DISABLE", "MODE_OFF", "MODE_SAFE", "auto_verify_enabled", "commit_verify_argv",
    "commit_verify_enabled", "schedule_commit_verify", "schedule_for_receipt", "schedule_post_session_verify",
    "verify_argv",
]
