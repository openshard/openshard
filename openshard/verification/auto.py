"""Opt-in post-session verification: run ``openshard verify`` when a captured session closes.

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
    return [sys.executable, "-m", "openshard.cli.entrypoint", "verify", "--receipt", receipt_ref, "--json"]


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


__all__ = ["CONFIG_KEY", "ENV_DISABLE", "MODE_SAFE", "auto_verify_enabled", "schedule_post_session_verify", "verify_argv"]
