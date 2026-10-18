"""Per-turn Receipts for Claude Code (docs/per-turn-receipts.md). On by default since v0.5.0.

With the flag on (the default), one Claude Code prompt -> completed turn (``UserPromptSubmit``
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
OpenShard made from the session id it observed, never a declaration. Sync sends
both, labelled (``sync/envelope.receipt_payload``).

Open turn. The prompt Event that opens a turn carries ``turn_opened``, the
turn's ``receipt_id``, ``receipt_scope`` and ``turn_index``; the seal adds a
``receipt.sealed`` Event. Both are ordinary Events, so a linked session streams
them through remote / connected capture while the turn is still open.

Flag: ``OPENSHARD_TURN_RECEIPTS=on|off`` in the capture process's environment
wins; else ``turn_receipts: false`` in the repository's ``.openshard/config.yml``
opts out. It is read once per session buffer and then sticks to it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE
from openshard.history.capture_completeness import REASON_SESSION_END_NOT_OBSERVED, make_reason
from openshard.history.task_identity import new_task_id, stored_task_id

_RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9a-f]{32}$")

TURN_RECEIPTS_ENV = "OPENSHARD_TURN_RECEIPTS"
CONFIG_KEY = "turn_receipts"
SCOPE_TURN = "turn"
START_SOURCE_TURN = "turn"
GROUPING_EXPLICIT = "explicit"
GROUPING_SESSION = "session"

_ON = frozenset({"1", "on", "true", "yes"})
_OFF = frozenset({"0", "off", "false", "no"})
_STOP = "Stop"
_SESSION_END = "SessionEnd"
_CLOUD_TURN_SURFACES = frozenset({("cursor", "cursor-cloud"), ("claude_code", "claude-code-web")})


def enabled(repo_root: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether new Claude Code sessions in *repo_root* get per-turn Receipts (default: yes). Never raises."""
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
            return True
        from openshard.config.settings import load_config_safe

        config, valid, _ = load_config_safe(cwd=Path(repo_root))
        return not (bool(valid) and config.get(CONFIG_KEY) is False)
    except Exception:
        return True


def turn_scoped(buf: dict, repo_root: Path) -> bool:
    """True when *buf* records one turn. Decided once per buffer, then sticky.

    A buffer that already holds more than one prompt or turn was opened by a
    Core without this decision (an upgrade mid-session): it stays a session
    Receipt, so a record already on disk never turns into a turn Receipt.
    """
    scope = buf.get("receipt_scope")
    if scope is None:
        in_flight = _count(buf.get("prompt_count")) > 1 or _count(buf.get("turn_count")) > 1
        turn = buf.get("agent") == AGENT_CLAUDE_CODE and not in_flight and enabled(repo_root)
        scope = buf["receipt_scope"] = SCOPE_TURN if turn else "session"
    return scope == SCOPE_TURN


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _turn_facts(buf: dict) -> dict[str, Any]:
    index = buf.get("turn_index")
    return {"receipt_scope": SCOPE_TURN, "turn_index": index if _count(index) > 0 else 1}


def _name_receipt(event: dict, buf: dict) -> None:
    """Put the turn's ``receipt_id`` on *event*'s metadata.

    Set after ``make_event``: its free-text scrubber drops long hex runs, and
    this value is an identifier Core minted, checked against its exact shape
    here (as ``remote/spool.wire_link`` does), never free text.
    """
    receipt_id = (buf.get("record") or {}).get("receipt_id")
    if isinstance(receipt_id, str) and _RECEIPT_ID_RE.match(receipt_id) and isinstance(event.get("metadata"), dict):
        event["metadata"]["receipt_id"] = receipt_id


def mark_opened(buf: dict, repo_root: Path) -> None:
    """Mark the prompt Event that just opened a turn with the turn's Receipt. No-op otherwise.

    The open turn's Receipt exists from its first prompt; this puts its
    identity on an Event, so a linked session's journal says "turn N is open"
    before the turn completes. Nothing about the turn's outcome is claimed.
    """
    if not turn_scoped(buf, repo_root):
        return
    events = buf.get("events") or []
    last = events[-1] if events and isinstance(events[-1], dict) else {}
    metadata = last.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("hook") != "UserPromptSubmit":
        return  # the prompt Event was not staged (buffer full): say nothing rather than mark another
    metadata.update({"turn_opened": True, **_turn_facts(buf)})
    _name_receipt(last, buf)


def is_turn_sealed(capture: object) -> bool:
    """True for a persisted turn Receipt whose turn completed: immutable, like an ended session."""
    return (
        isinstance(capture, dict)
        and capture.get("receipt_scope") == SCOPE_TURN
        and isinstance(capture.get("turn_sealed_at"), str)
    )


def continues_after_seal(capture: object, first_hook: str) -> bool:
    """True when *first_hook* after a sealed turn is the agent continuing: it opens the next turn at once."""
    return is_turn_sealed(capture) and first_hook != _STOP


def closes_after_seal(capture: object, first_hook: str) -> bool:
    """True for a ``Stop`` after a sealed turn: ignored, it opens no segment.

    A segment a Stop opens holds no work; right after a sealed turn that Stop
    is late or repeated (for example after the ``SessionEnd`` the sealed turn
    could not record), and its segment would only be an empty buffer left
    behind until the idle sweep.
    """
    return is_turn_sealed(capture) and first_hook == _STOP


def restarted(buf: dict) -> bool:
    """True when a ``SessionStart`` reaches a segment opened as the next turn before any work.

    The agent process started again (``claude --resume`` after the session
    ended): the segment is a resume, not turn N+1 of a running session.
    """
    return buf.get("start_source") == START_SOURCE_TURN and not buf.get("record")


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
        _sealed_event(buf, now)
        return False, True
    return False, False


def _sealed_event(buf: dict, now: str) -> None:
    """The turn's Receipt is final from this Stop on: say so on an Event (streamed when linked).

    Final, not signed: whether a signature follows depends on the install's key.
    """
    from openshard.adapters.claude_hooks import _append_event

    before = len(buf.get("events") or [])
    _append_event(buf, event_type="receipt.sealed", action="turn completed; Receipt final", status="passed",
                  evidence="directly_observed", occurred_at=now,
                  metadata={**_turn_facts(buf), "turn_status": "turn_completed"})
    events = buf.get("events") or []
    if len(events) > before and isinstance(events[-1], dict):
        _name_receipt(events[-1], buf)


def inherit(buf: dict, persisted: Mapping[str, Any], *, same_receipt: bool, repo_root: Path | None = None) -> None:
    """Carry turn scope and session grouping from *persisted* into *buf*.

    *same_receipt*: *buf* was rebuilt from *persisted* (a lost staging buffer),
    so it keeps that Receipt's turn index and task. Otherwise *buf* is the next
    segment of the same agent session: the next turn, in the same Task unless
    that Task ended (``previous_task_ended``): the branch changed, or the
    capture service saw the Task's PR merged or closed.
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
        for key in ("turn_sealed_at", "previous_task_ended", "task_pr_url"):
            if isinstance(capture.get(key), str):
                buf[key] = capture[key]
        return
    buf["turn_index"] = index + 1
    if _opened_by(buf) == _SESSION_END:
        # The SessionEnd that ends the chat after its last sealed turn: this segment
        # holds no work and is dropped at once; never stream its opening event to
        # remote capture as a session that started.
        buf["remote_spooled"] = len(buf.get("events") or [])
    if is_turn_sealed(capture) and capture.get("session_end_observed") is not True:
        buf["start_source"] = START_SOURCE_TURN  # the previous turn completed; the session did not end
    pr_url = _task_pr_url(persisted)
    ended = "branch_changed" if branch_changed(buf, persisted) else _pr_ended(pr_url, buf, repo_root)
    if ended is not None:
        buf["previous_task_ended"] = ended  # the previous turn's Task ended; this prompt starts a new one
        return
    if task_id is not None:
        buf["turn_task_id"] = task_id
    if pr_url is not None:
        buf["task_pr_url"] = pr_url


def _task_pr_url(persisted: Mapping[str, Any]) -> str | None:
    """The pull request the previous turn's Task is known to have: its own, or one an earlier turn found."""
    pr = persisted.get("pull_request")
    if isinstance(pr, dict) and isinstance(pr.get("url"), str):
        return str(pr["url"])
    capture = persisted.get("capture")
    url = capture.get("task_pr_url") if isinstance(capture, dict) else None
    return url if isinstance(url, str) else None


_PR_ENDED = {"MERGED": "pr_merged", "CLOSED": "pr_closed"}


def _pr_ended(url: str | None, buf: Mapping[str, Any], repo_root: Path | None) -> str | None:
    """``pr_merged`` / ``pr_closed`` when GitHub says the Task's PR is no longer open, else None.

    One read-only ``gh pr view <url> --json state`` at a turn's start, only in
    the capture service (never on a hook Claude Code is waiting on), and only
    when an earlier turn of the Task found a PR. No ``gh``, no network: None.
    """
    if url is None or buf.get("agent") != AGENT_CLAUDE_CODE:
        return None
    from openshard.adapters import claude_hooks

    if not claude_hooks._PR_LOOKUP_ENABLED or os.environ.get(claude_hooks.PR_LOOKUP_ENV, "").lower() == "off":
        return None
    return _PR_ENDED.get(pr_state(url, repo_root) or "")


def pr_state(url: str, cwd: Path | None) -> str | None:
    """GitHub's state for the pull request *url* (``OPEN``, ``MERGED``, ``CLOSED``) via ``gh``. Never raises."""
    import json
    import shutil
    import subprocess

    from openshard.util.git import NO_WINDOW_KW

    try:
        exe = shutil.which("gh")
        if exe is None:
            return None
        proc = subprocess.run(
            [exe, "pr", "view", url, "--json", "state"], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5.0, check=False,
            **NO_WINDOW_KW,
        )
        state = json.loads(proc.stdout or "").get("state") if proc.returncode == 0 else None
    except Exception:
        return None
    return state if isinstance(state, str) else None


def branch_changed(buf: Mapping[str, Any], persisted: Mapping[str, Any]) -> bool:
    """The Task ended at a branch change: this turn starts off the branch the previous turn started on.

    Covers both an agent switching branch during the previous turn and a
    switch between turns. Then the next prompt starts a new Task (a new
    session-grouped ``task_id``). Unknown (no branch, detached HEAD) on one
    side only also counts as a change: when unsure, a new Task.
    """
    capture = persisted.get("capture")
    capture = capture if isinstance(capture, dict) else {}
    previous = capture["start_branch"] if "start_branch" in capture else persisted.get("git_branch")
    current = buf.get("git_branch")
    previous = previous if isinstance(previous, str) and previous else None
    current = current if isinstance(current, str) and current else None
    return previous != current


def _opened_by(buf: dict) -> object:
    """The hook that opened *buf* (recorded on its ``session.started`` event)."""
    events = buf.get("events") or []
    first = events[0] if events and isinstance(events[0], dict) else {}
    metadata = first.get("metadata")
    return metadata.get("hook") if isinstance(metadata, dict) else None


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
    for key in ("previous_task_ended", "task_pr_url"):
        if isinstance(buf.get(key), str):
            capture[key] = buf[key]
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
