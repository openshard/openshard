"""Capture gaps: Claude Code work the transcript shows but no hook delivered.

Claude Code's hooks fail open, so a hook that timed out, errored or was
switched off leaves no trace in the hook stream. The session's own
transcript (``~/.claude/projects/<project>/<session>.jsonl``) still records
the work. Comparing the two finds the silence; it never explains it, and it
never says OpenShard blocked, allowed or controlled anything.

Two detectors, both lower bounds, both judged only at a settled point (a
turn just ended, the session ended, or an idle buffer was sealed) so a hook
still in flight is never counted as missing:

* ``tool_calls_without_hook``: successful Edit / Write / MultiEdit /
  NotebookEdit / Bash calls in the main transcript, finished by the last hook
  OpenShard received, outnumber the PostToolUse / PostToolUseFailure hooks it
  received. Failed, denied or interrupted calls are not counted (they may
  legitimately reach no PostToolUse hook); sub-agent transcripts are not
  read, and their hooks only make the hook count larger, so the count can
  miss gaps but never invents one.
* ``hook_events_stopped``: when the session ended or was sealed idle, the
  transcript kept recording the conversation more than two minutes after the
  last hook arrived.

Read incrementally from a byte offset kept on the session buffer, like the
token-usage reader (at most 16 MB per fold; the rest is read at the next
one, and until then the counts are only smaller). No transcript content,
path or tool input is stored: only tool-use ids awaiting a result, result
timestamps (folded into a count once settled) and the last activity time. No transcript (other agents, an unvalidated path) means no
judgement: unknown stays unknown, no reason is recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE
from openshard.adapters.claude_transcript_usage import (
    _file_head,
    _parse_utc,
    _valid_transcript_path,
)
from openshard.history.capture_completeness import (
    REASON_HOOK_EVENTS_STOPPED as GAP_HOOKS_STOPPED,
)
from openshard.history.capture_completeness import (
    REASON_TOOL_CALLS_WITHOUT_HOOK as GAP_TOOL_CALLS,
)
from openshard.history.capture_completeness import make_reason

GAP_KINDS = frozenset({GAP_TOOL_CALLS, GAP_HOOKS_STOPPED})

# Tools every installed PostToolUse matcher has covered (PowerShell joined
# later, so an older install may not match it; leaving it out keeps the
# count a lower bound).
COUNTED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})
STOPPED_AFTER_SECONDS = 120
_CUTOFF_SLACK_SECONDS = 1  # hook times are whole seconds; transcript times carry milliseconds
_MAX_PENDING = 256
_MAX_RESULTS = 5_000
_MAX_READ_BYTES = 16 * 1024 * 1024
STATE_KEY = "hook_coverage"


def _epoch(stamp: object) -> int | None:
    at = _parse_utc(stamp)
    return int(at.timestamp()) if at is not None else None


def _new_state(since: object) -> dict[str, Any]:
    return {"since": since, "offset": 0, "head": None, "pending": {}, "ok": [], "ok_settled": 0, "last_at": None}


def _note_line(state: dict[str, Any], rec: dict, since: int | None) -> None:
    kind = rec.get("type")
    if kind not in ("assistant", "user") or rec.get("isMeta") is True:
        return
    at = _epoch(rec.get("timestamp"))
    if at is None or (since is not None and at < since):
        return
    if state["last_at"] is None or at > state["last_at"]:
        state["last_at"] = at
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    pending: dict = state["pending"]
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if kind == "assistant" and block.get("type") == "tool_use" and block.get("name") in COUNTED_TOOLS:
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                while len(pending) >= _MAX_PENDING:  # a call that never got a result: forget the oldest
                    pending.pop(next(iter(pending)))
                pending[tool_id] = 1
        elif kind == "user" and block.get("type") == "tool_result":
            tool_id = block.get("tool_use_id")
            if not isinstance(tool_id, str) or pending.pop(tool_id, None) is None:
                continue
            if block.get("is_error") is True:
                continue  # failed, denied or interrupted: may reach no PostToolUse hook
            if len(state["ok"]) < _MAX_RESULTS:  # past the bound the count only gets smaller: a lower bound
                state["ok"].append(at)


def refresh(buf: dict) -> dict[str, Any] | None:
    """Read what the session's main transcript gained since the last call. Returns the state, or None. Never raises."""
    try:
        if (buf.get("agent") or AGENT_CLAUDE_CODE) != AGENT_CLAUDE_CODE:
            return None
        raw = _valid_transcript_path(buf.get("transcript_path"), buf.get("session_id"))
        if raw is None:
            return None
        path = Path(raw)
        since = buf.get("started_at")
        state = buf.get(STATE_KEY)
        head = _file_head(path)
        if (not isinstance(state, dict) or state.get("since") != since
                or (state.get("head") and head != state.get("head"))):
            state = _new_state(since)
        size = path.stat().st_size
        offset = int(state.get("offset") or 0)
        if size < offset:  # truncated or replaced: start over
            state, offset = _new_state(since), 0
        state["head"] = head
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(min(size - offset, _MAX_READ_BYTES))
        end = chunk.rfind(b"\n")
        if end >= 0:
            state["offset"] = offset + end + 1
            since_at = _epoch(since)
            for line in chunk[: end + 1].splitlines():
                if b'"assistant"' not in line and b'"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(rec, dict):
                    _note_line(state, rec, since_at)
        buf[STATE_KEY] = state
        return state
    except (OSError, ValueError, TypeError):
        return None


def _clock(at: int, other: int) -> str:
    a = datetime.fromtimestamp(at, UTC)
    b = datetime.fromtimestamp(other, UTC)
    return a.strftime("%H:%M UTC") if a.date() == b.date() else a.strftime("%Y-%m-%d %H:%M UTC")


def evaluate(buf: dict, state: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The coverage gaps for *buf* now, or None when nothing can be judged (not a settled point, no transcript)."""
    finished = bool(buf.get("ended")) or bool(buf.get("idle_sealed_at"))
    turn_closed = bool(buf.get("last_stop_at")) and buf.get("last_stop_at") == buf.get("last_activity_at")
    if not (finished or turn_closed):
        return None
    last_hook = _epoch(buf.get("last_activity_at"))
    if state is None or last_hook is None:
        return None  # no transcript to compare: judge nothing, keep whatever was recorded
    cutoff = last_hook + _CUTOFF_SLACK_SECONDS
    ok = [t for t in state.get("ok") or [] if isinstance(t, int)]
    # Results finished by this cutoff stay finished by every later one: fold them into a count.
    state["ok_settled"] = int(state.get("ok_settled") or 0) + sum(1 for t in ok if t <= cutoff)
    ok = state["ok"] = [t for t in ok if t > cutoff]
    before = state["ok_settled"]
    hooks = int(buf.get("tool_call_count") or 0)
    reasons: list[dict[str, Any]] = []
    if before > hooks:
        reasons.append(make_reason(GAP_TOOL_CALLS, before - hooks))
    last_at = state.get("last_at")
    if finished and isinstance(last_at, int) and last_at - last_hook > STOPPED_AFTER_SECONDS:
        after = len(ok)
        tail = f" ({after} more tool {'call' if after == 1 else 'calls'})" if after else ""
        reasons.append(make_reason(GAP_HOOKS_STOPPED, 1, (
            f"no hook arrived after {_clock(last_hook, last_at)}; "
            f"the session continued until {_clock(last_at, last_hook)}{tail}"
        )))
    return reasons


def note_gaps(buf: dict) -> None:
    """Refresh the transcript view and replace this session's coverage gaps in ``capture_losses``. Never raises."""
    try:
        reasons = evaluate(buf, refresh(buf))
        if reasons is None:
            return
        kept = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict) and r.get("kind") not in GAP_KINDS]
        buf["capture_losses"] = kept + reasons
    except (OSError, ValueError, TypeError, KeyError):
        return

