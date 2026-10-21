"""What a Claude Code session routed across checkouts shares with per-turn Receipts.

The capture service records a session's events into the checkout each one
touched (``claude_workspace.WorkspaceRouter``): a linked worktree, another
repository. A checkout first reached mid-session opens a fresh buffer, with
the session's start and the user's prompt replayed at their original times.
With per-turn Receipts two facts must still hold across those checkouts:

* **One Task.** A buffer with no previous turn in its own checkout used to
  mint a ``task_id`` of its own, even within one turn. A buffer the router
  marked (``ATTR``) now takes the session's current ``task_id`` before
  minting one. Still the grouping OpenShard makes from the observed session
  id (``task_grouping: session``); nothing is inferred from prompts or
  repository state. A Task that ended in one checkout (``previous_task_ended``)
  mints a new id, which becomes the session's current Task.
* **Each turn's usage once.** The replayed session start dates such a buffer
  to the session's start, so its transcript window took in earlier turns that
  other checkouts' Receipts had already counted. The router stamps the
  current turn's prompt time (``SINCE_ATTR``) on the work it routes; a turn
  Receipt's usage window starts at the first one it received
  (``usage_since``), never earlier than the buffer's own start. A sealed turn's
  window ends with its ``Stop`` (``usage_until``), however late it is folded.

The session's Task is kept beside the capture service's state
(``<OPENSHARD_HOME>``), never in a repository, so a restarted service keeps it.
Entries hold a hashed session id and a task id, and are pruned once unused for
a week.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openshard.history.task_identity import is_task_id, new_task_id

ATTR = "session_task_shared"  # set by the router on every payload it routes
SINCE_ATTR = "turn_usage_since"  # the current turn's prompt time, on a turn-scoped session's work
_DIRNAME = "claude-session-tasks"
_MAX_AGE_SECONDS = 7 * 24 * 3600
_LOCK = threading.Lock()


def note(buf: dict, attrs: Any) -> None:
    """Copy the router's marks onto *buf*: shared Task, and the usage window's start (only moves later)."""
    if not isinstance(attrs, dict):
        return
    if attrs.get(ATTR) is True:
        buf[ATTR] = True
    since = attrs.get(SINCE_ATTR)
    if isinstance(since, str) and since and since > str(buf.get(SINCE_ATTR) or ""):
        buf[SINCE_ATTR] = since


def usage_since(buf: dict) -> Any:
    """Where *buf*'s transcript usage starts: its own start, or later for a turn Receipt the router dated."""
    started = buf.get("started_at")
    since = buf.get(SINCE_ATTR)
    turn = buf.get("receipt_scope") == "turn"
    if turn and isinstance(since, str) and isinstance(started, str) and since > started:
        return since
    return started


def usage_until(buf: dict) -> str | None:
    """Where a sealed turn Receipt's usage ends (exclusive): the end of the second its ``Stop`` was observed.

    The fold runs after the hook returned, sometimes after the next turn's
    replies are already in the transcript; they are that turn's, never this one's.
    """
    sealed = buf.get("turn_sealed_at")
    if buf.get("receipt_scope") != "turn" or not isinstance(sealed, str):
        return None
    return end_of_second(sealed)


def end_of_second(stamp: str) -> str | None:
    """The end of the whole second *stamp* falls in (hook times are whole seconds), or None."""
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC).replace(microsecond=0)
    except ValueError:
        return None
    return (at + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_for(buf: dict) -> str:
    """The ``task_id`` a turn Receipt that inherited none takes: the session's current Task, else a new one."""
    session_id = buf.get("session_id")
    if buf.get(ATTR) is not True or not isinstance(session_id, str) or not session_id:
        return new_task_id()
    with _LOCK:
        path = _path(session_id)
        current = None if isinstance(buf.get("previous_task_ended"), str) else _read(path)
        if current is not None:
            return current
        task_id = new_task_id()
        _write(path, task_id)
        return task_id


def _path(session_id: str) -> Path:
    from openshard.util.home import openshard_home

    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return Path(openshard_home()) / _DIRNAME / f"{digest}.task"


def _read(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        os.utime(path)  # in use: keep it from being pruned
    except OSError:
        return None
    return raw if is_task_id(raw) else None


def _write(path: Path, task_id: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task_id, encoding="utf-8")
        _prune(path.parent)
    except OSError:
        pass


def _prune(directory: Path) -> None:
    cutoff = time.time() - _MAX_AGE_SECONDS
    try:
        for entry in directory.iterdir():
            if entry.suffix == ".task" and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError:
        pass
