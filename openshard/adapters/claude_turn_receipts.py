"""Per-turn Receipts for Claude Code (docs/per-turn-receipts.md). Behind a flag, off by default.

With the flag on, one Claude Code prompt -> completed turn (``UserPromptSubmit``
... ``Stop``) is one Receipt instead of one Receipt per agent session. The
turn's Receipt is sealed when its ``Stop`` is applied and becomes
sync-eligible at once; the next hook of the same session opens the next
turn's Receipt through the existing segment path
(``claude_hooks._resumed_segment``), so the turn gets its own ``receipt_id``,
a working-tree baseline and HEAD taken at the turn's start, and transcript
usage counted from that start. Nothing about the session-level record changes
while the flag is off: every function here returns early on a buffer that is
not turn-scoped.

Grouping. Turns of one session share a ``task_id``. A declared task
(``OPENSHARD_TASK_ID``) wins and is recorded as ``task_grouping: explicit``;
otherwise the first turn mints one and the later turns of the same agent
session inherit it, recorded as ``task_grouping: session`` -- a grouping
OpenShard made from the session id it observed, never a declaration. The sync
projection does not send a session-grouped ``task_id`` (the Platform contract
defines ``task_id`` as explicit); see ``sync/envelope.receipt_payload``.

Flag: ``OPENSHARD_TURN_RECEIPTS=on|off`` in the capture process's environment
wins; else ``turn_receipts: true`` in the repository's ``.openshard/config.yml``.
It is read once per session buffer and then sticks to it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE
from openshard.history.capture_completeness import REASON_SESSION_END_NOT_OBSERVED, make_reason
from openshard.history.task_identity import new_task_id, stored_task_id

TURN_RECEIPTS_ENV = "OPENSHARD_TURN_RECEIPTS"
CONFIG_KEY = "turn_receipts"
SCOPE_TURN = "turn"
START_SOURCE_TURN = "turn"
GROUPING_EXPLICIT = "explicit"
GROUPING_SESSION = "session"

_ON = frozenset({"1", "on", "true", "yes"})
_OFF = frozenset({"0", "off", "false", "no"})
_STOP = "Stop"
_CLOUD_TURN_SURFACES = frozenset({("cursor", "cursor-cloud"), ("claude_code", "claude-code-web")})


def enabled(repo_root: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether new Claude Code sessions in *repo_root* get per-turn Receipts. Never raises."""
    source = os.environ if env is None else env
    value = str(source.get(TURN_RECEIPTS_ENV) or "").strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    try:
        path = Path(repo_root) / ".openshard" / "config.yml"
        # Same cheap pre-check as verification/auto.py: YAML only when the key is there.
        if not path.is_file() or CONFIG_KEY.encode() not in path.read_bytes():
            return False
        from openshard.config.settings import load_config_safe

        config, valid, _ = load_config_safe(cwd=Path(repo_root))
        return bool(valid) and config.get(CONFIG_KEY) is True
    except Exception:
        return False


def turn_scoped(buf: dict, repo_root: Path) -> bool:
    """True when *buf* records one turn. Decided once per buffer, then sticky."""
    scope = buf.get("receipt_scope")
    if scope is None:
        scope = SCOPE_TURN if buf.get("agent") == AGENT_CLAUDE_CODE and enabled(repo_root) else "session"
        buf["receipt_scope"] = scope
    return scope == SCOPE_TURN


def is_turn_sealed(capture: object) -> bool:
    """True for a persisted turn Receipt whose turn completed: immutable, like an ended session."""
    return (
        isinstance(capture, dict)
        and capture.get("receipt_scope") == SCOPE_TURN
        and isinstance(capture.get("turn_sealed_at"), str)
    )


def close_turn(payload: Any, buf: dict, repo_root: Path, *, now: str, has_activity: bool) -> tuple[bool, bool]:
    """Decide whether this hook closes the Receipt: ``(cloud_turn_closed, turn_sealed)``.

    ``cloud_turn_closed``: hosted runtimes may reclaim their VM after Stop
    without SessionEnd, so that turn is sealed with ``session_end_not_observed``
    (unchanged behaviour, moved here from ``apply_reduced_hook``).
    ``turn_sealed``: a turn-scoped buffer's Stop with work in it; no session
    end is claimed. Either way the caller folds and drops the buffer.
    """
    if payload.event != _STOP:
        return False, False
    if (payload.agent, os.environ.get("OPENSHARD_CONNECTED_SURFACE")) in _CLOUD_TURN_SURFACES:
        losses = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict)]
        if not any(r.get("kind") == REASON_SESSION_END_NOT_OBSERVED for r in losses):
            losses.append(make_reason(REASON_SESSION_END_NOT_OBSERVED))
        buf["capture_losses"] = losses
        return True, False
    if has_activity and not buf.get("ended") and turn_scoped(buf, repo_root):
        buf["turn_sealed_at"] = now
        return False, True
    return False, False


def inherit(buf: dict, persisted: Mapping[str, Any], *, same_receipt: bool) -> None:
    """Carry turn scope and session grouping from *persisted* into *buf*.

    *same_receipt*: *buf* was rebuilt from *persisted* (a lost staging buffer),
    so it keeps that Receipt's turn index and task. Otherwise *buf* is the next
    segment of the same agent session: the next turn, in the same Task.
    """
    capture = persisted.get("capture")
    if not isinstance(capture, dict) or capture.get("receipt_scope") != SCOPE_TURN:
        if same_receipt:
            buf["receipt_scope"] = "session"  # a session Receipt never turns into a turn Receipt mid-life
        return
    buf["receipt_scope"] = SCOPE_TURN
    raw_index = capture.get("turn_index")
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index > 0 else 1
    task_id = stored_task_id(dict(persisted))
    if same_receipt:
        buf["turn_index"] = index
        if task_id is not None and capture.get("task_grouping") == GROUPING_SESSION:
            buf["turn_task_id"] = task_id
        if isinstance(capture.get("turn_sealed_at"), str):
            buf["turn_sealed_at"] = capture["turn_sealed_at"]
        return
    buf["turn_index"] = index + 1
    if is_turn_sealed(capture) and capture.get("session_end_observed") is not True:
        buf["start_source"] = START_SOURCE_TURN  # the previous turn completed; the session did not end
    if task_id is not None:
        buf["turn_task_id"] = task_id


def stamp(entry: dict, buf: dict, repo_root: Path) -> None:
    """Record the turn facts on a turn-scoped Receipt being built. No-op otherwise.

    Runs after ``_stamp_task_context``: a declared task already on *entry*
    wins (``explicit``); else the session's task (inherited, or minted now at
    this Receipt's creation) is used (``session``).
    """
    if not turn_scoped(buf, repo_root):
        return
    capture = entry.setdefault("capture", {})
    capture["receipt_scope"] = SCOPE_TURN
    index = buf.get("turn_index")
    capture["turn_index"] = index if isinstance(index, int) and index > 0 else 1
    if isinstance(buf.get("turn_sealed_at"), str):
        capture["turn_sealed_at"] = buf["turn_sealed_at"]
        capture["status"] = "turn_completed"  # final: the turn completed; the session itself may still be open
    if stored_task_id(entry) is not None:
        capture["task_grouping"] = GROUPING_EXPLICIT
        return
    task_id = buf.get("turn_task_id")
    if stored_task_id({"task_id": task_id}) is None:
        task_id = buf["turn_task_id"] = new_task_id()
    entry["task_id"] = task_id
    capture["task_grouping"] = GROUPING_SESSION


def after_seal() -> None:
    """Ask a running Platform sync loop (capture service) to flush now. No-op elsewhere."""
    try:
        from openshard.sync.client import request_sync

        request_sync()
    except Exception:
        pass
