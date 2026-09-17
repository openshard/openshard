"""Safe, observational ``git`` subprocess helper.

Every OpenShard git call is a local read with a short timeout that must
never raise into the caller (a hook, the capture-service worker, a repo
scan). This module is that one call site.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 5.0

# These git calls can run from OpenShard's background capture-service worker
# (adapters/claude_capture_service.py), a process that may itself have no
# console (e.g. spawned with CREATE_NO_WINDOW). Without this flag, such a
# console-less parent causes Windows to allocate a brand-new visible console
# for each git.exe child. Output is always captured via PIPE here regardless,
# so no window is ever needed; harmless for every other (already-consoled)
# caller.
# subprocess.CREATE_NO_WINDOW only exists in typeshed's Windows stubs, so a
# bare attribute access fails mypy on this cross-platform module even inside
# a sys.platform guard (the guard narrows reachability, not module-attribute
# existence) -- getattr sidesteps the static lookup; the fallback is never
# used since the whole expression is a no-op off Windows anyway.
NO_WINDOW_KW: dict = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
)


def run_git(
    root: Path,
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stdin: str | None = None,
) -> str | None:
    """stdout of ``git <args>`` run in *root*, or ``None`` on any failure.

    ``None`` covers a non-zero exit, a timeout, a missing ``git`` binary and
    any other OS error. Output is decoded as UTF-8 with undecodable bytes
    replaced, so an odd file name can never turn into an exception. Never
    raises.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **NO_WINDOW_KW,
        )
    except Exception:
        return None
    return result.stdout if result.returncode == 0 else None
