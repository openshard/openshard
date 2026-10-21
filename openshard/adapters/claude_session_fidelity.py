"""What a hook-captured Receipt says about a long or interrupted session.

Three facts a reader needs that the staging buffer used to blur:

* **Agent notifications are not prompts.** Claude Code delivers a finished
  background task (``<task-notification>``) or a sub-agent's report
  (``<agent-message ...>``) to the session as a queued prompt, so it fires
  ``UserPromptSubmit`` like anything the user typed. Those are counted as
  agent notifications; they never become the task and never count as a prompt.
* **No event is silently dropped.** The buffer keeps a bounded window of the
  session's Events (its opening Events and the most recent ones). Older
  Events past the window are appended to a local, append-only event log
  (``.openshard/event_log/<receipt id>.jsonl``) instead of being discarded;
  only a log write that fails is a capture loss.
* **Silent hooks are said as such.** Claude Code's hooks fail open, so the
  hash of the hook configuration installed when the session was first
  observed is recorded (``claude_hook_health``), and work the session's
  transcript shows but no hook delivered becomes a capture gap
  (``claude_hook_coverage``).
* **An idle seal is said as such.** A buffer idle past the sweep threshold
  (an hour by default) is sealed into its Receipt without a session end
  (``sweep_stale_buffers``, moved here from claude_hooks); the Receipt says
  it was sealed idle rather than reading as an ordinary in-progress session.

Nothing here stores a prompt, a command or output: notification detection
reads only the prompt's opening tag, and archived Events are the same
scrubbed Events the Receipt already holds.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openshard.history.receipt_evidence import (
    mint_session_nonce,
    recorded_session_nonce,
    session_nonce_or_none,
)
from openshard.verification.gaming_signals import rebuilt_seen

PROMPT_SOURCE_AGENT_NOTIFICATION = "agent_notification"
_NOTIFICATION_PREFIXES = ("<task-notification>", "<agent-message ", "<agent-message>", "<teammate-message")

EVENT_HEAD_KEEP = 20  # the session's opening Events always stay in the Receipt
MAX_ARCHIVE_PENDING = 1_000  # Events waiting for the next buffer write
EVENT_LOG_DIRNAME = "event_log"
_MAX_EVENT_LOG_BYTES = 20 * 1024 * 1024

REASON_SESSION_IDLE_SEALED = "session_idle_sealed"
STALE_BUFFER_SECONDS = 60 * 60
MAX_STALE_SWEEP = 20
SWEEP_LOCK_TIMEOUT_SECONDS = 5.0


def prompt_source(prompt: str | None) -> str | None:
    """``agent_notification`` for a harness-delivered notification, else None (a user prompt)."""
    if not isinstance(prompt, str):
        return None
    head = prompt.lstrip()[:64]
    return PROMPT_SOURCE_AGENT_NOTIFICATION if head.startswith(_NOTIFICATION_PREFIXES) else None


def make_room(buf: dict, cap: int) -> bool:
    """Move the oldest Event after the opening window to the archive queue. False when it cannot."""
    events = buf.get("events")
    pending = buf.setdefault("events_archive_pending", [])
    if not isinstance(events, list) or not isinstance(pending, list) or len(events) <= EVENT_HEAD_KEEP:
        return False
    if len(pending) >= MAX_ARCHIVE_PENDING:
        return False
    while len(events) >= cap and len(events) > EVENT_HEAD_KEEP:
        pending.append(events.pop(EVENT_HEAD_KEEP))
    return True


def event_log_path(history_dir: Path, buf: dict) -> Path:
    """``<repo>/.openshard/event_log/<receipt id>.jsonl`` (the session id before a record exists)."""
    raw = buf.get("record")
    record: dict = raw if isinstance(raw, dict) else {}
    name = record.get("receipt_id") or buf.get("session_id") or "session"
    return history_dir / EVENT_LOG_DIRNAME / f"{name}.jsonl"


def flush_event_archive(history_dir: Path, buf: dict) -> None:
    """Append queued archived Events to the session's event log (*history_dir* is ``<repo>/.openshard``).

    A failed write is counted as dropped. Called before every record snapshot
    and every buffer write, so the record never counts an Event the log does
    not hold.
    """
    pending = buf.get("events_archive_pending")
    if not isinstance(pending, list) or not pending:
        buf.pop("events_archive_pending", None)
        return
    path = event_log_path(history_dir, buf)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= _MAX_EVENT_LOG_BYTES:
            raise OSError("event log full")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(ev, separators=(",", ":")) + "\n" for ev in pending))
            fh.flush()
            os.fsync(fh.fileno())
        buf["archived_events"] = int(buf.get("archived_events") or 0) + len(pending)
        buf["event_log"] = f".openshard/{EVENT_LOG_DIRNAME}/{path.name}"
    except OSError:
        buf["dropped_events"] = int(buf.get("dropped_events") or 0) + len(pending)
    buf.pop("events_archive_pending", None)


def note_agent_notification(buf: dict) -> int:
    buf["agent_notification_count"] = int(buf.get("agent_notification_count") or 0) + 1
    return buf["agent_notification_count"]


def start_metadata(agent: str, repo_root: Path) -> dict[str, str]:
    """Extra ``session.started`` metadata: the installed hook configuration's hash (Claude Code only)."""
    from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE
    from openshard.adapters.claude_hook_health import hook_config_hash

    digest = hook_config_hash(repo_root) if agent == AGENT_CLAUDE_CODE else None
    return {"hook_config_hash": digest} if digest else {}


def note_coverage_gaps(buf: dict, repo_root: Path | None = None) -> None:
    """Record work the transcript shows but no hook delivered (see claude_hook_coverage). Never raises."""
    from openshard.adapters.claude_hook_coverage import note_gaps

    note_gaps(buf, repo_root)


def _hook_config_hash(buf: dict) -> str | None:
    """The hash the ``session.started`` Event carries, or the one a rebuilt buffer kept."""
    for event in buf.get("events") or []:
        if isinstance(event, dict) and event.get("event_type") == "session.started":
            value = (event.get("metadata") or {}).get("hook_config_hash")
            if isinstance(value, str):
                return value
    value = buf.get("hook_config_hash")
    return value if isinstance(value, str) else None


def capture_status(buf: dict, ended: object) -> dict[str, Any]:
    """``capture.status`` (ended / idle / in_progress) and the extra facts below."""
    status = "ended" if ended else ("idle" if buf.get("idle_sealed_at") else "in_progress")
    return {"status": status, **capture_fields(buf)}


def rebuilt_counters(capture: dict) -> dict[str, Any]:
    """Buffer counters carried from a record into a buffer rebuilt from it (an idle seal is not: activity resumed)."""
    return {
        "dropped_events": int(capture.get("hook_events_dropped") or 0),
        "agent_notification_count": capture.get("agent_notification_count"),
        "archived_events": capture.get("hook_events_archived"),
        "event_log": capture.get("event_log"),
        "hook_config_hash": capture.get("hook_config_hash"),
        "gaming_seen": rebuilt_seen(capture),  # gaming-check observations (verification/gaming_signals)
    }


def session_nonce(carried: object = None) -> str:
    """The nonce for a new buffer's ``session.started`` Event: one carried over, else a fresh one.

    It lives in that Event's metadata, which the Receipt keeps (the opening
    Events are never archived away) and remote capture streams first, so a
    buffer rebuilt from its record keeps it without minting another. A
    ``--resume`` segment of an ended session carries the nonce the session
    started with, so one agent session binds to its remote-capture journal
    through one nonce (as it does through one ``session_key``).
    """
    return session_nonce_or_none(carried) or mint_session_nonce()


def carried_session_nonce(entry: dict) -> str | None:
    """The nonce an earlier Receipt of the same agent session started with, or None."""
    return recorded_session_nonce(entry)


def capture_fields(buf: dict) -> dict[str, Any]:
    """Extra ``capture`` facts for the Receipt; absent keys when there is nothing to say."""
    out: dict[str, Any] = {}
    if buf.get("agent_notification_count"):
        out["agent_notification_count"] = int(buf["agent_notification_count"])
    if buf.get("archived_events"):
        out["hook_events_archived"] = int(buf["archived_events"])
        out["event_log"] = buf.get("event_log")
    if buf.get("idle_sealed_at"):
        out["idle_sealed_at"] = buf["idle_sealed_at"]
    digest = _hook_config_hash(buf)
    if digest:
        out["hook_config_hash"] = digest
    return out


def summary_suffix(buf: dict) -> str:
    """Sentences appended to the Receipt summary."""
    parts = []
    notes = int(buf.get("agent_notification_count") or 0)
    if notes:
        parts.append(f" {notes} agent notification(s) delivered (not counted as prompts).")
    if buf.get("idle_sealed_at"):
        minutes = max(1, int(buf.get("idle_sealed_after_seconds") or STALE_BUFFER_SECONDS) // 60)
        parts.append(
            f" Sealed after {minutes} min without activity (last activity {buf.get('last_activity_at')});"
            " no session end was observed, and any later activity continues in a new segment that names this Receipt."
        )
    archived = int(buf.get("archived_events") or 0)
    if archived:
        parts.append(f" {archived} earlier event(s) kept in {buf.get('event_log')}.")
    return "".join(parts)


def sweep_stale_buffers(
    repo_root: Path, *, max_age_seconds: float = STALE_BUFFER_SECONDS, now: datetime | None = None,
    deadline_seconds: float | None = None,
) -> list[str]:
    """Fold and remove staging buffers of sessions idle for *max_age_seconds*.

    Called (outside the caller's own session lock) on SessionStart, and by
    Platform sync before it picks what to send (sync/client.py). A stale
    buffer is sealed into runs.jsonl as a partial segment --
    ``capture.session_end_observed`` stays False and no ``run.completed``
    Event is fabricated -- then removed; a later hook for that session
    opens a new Receipt segment. Returns the session ids
    folded. Never raises.
    """
    from openshard.adapters import claude_hooks as ch

    folded: list[str] = []
    started = time.monotonic()
    try:
        directory = ch.sessions_dir(repo_root)
        if not directory.is_dir():
            return folded
        from openshard.history.jsonl_store import history_file_lock

        candidates = sorted(p for p in directory.glob("*.json") if p.is_file())
        for path in candidates[: MAX_STALE_SWEEP * 4]:
            if len(folded) >= MAX_STALE_SWEEP:
                break
            if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:
                break  # the rest are swept by the next sweep
            peek = ch._read_buffer(path)
            if peek is None:
                continue
            age = ch._seconds_since(peek.get("last_activity_at"), now)
            if age is None or age < max_age_seconds:
                continue
            sid = str(peek.get("session_id") or path.stem)
            if not ch._SESSION_ID_RE.match(sid):
                continue
            try:
                with history_file_lock(path, timeout=SWEEP_LOCK_TIMEOUT_SECONDS):
                    buf = ch._read_buffer(path)
                    if buf is None:
                        continue
                    age = ch._seconds_since(buf.get("last_activity_at"), now)
                    if age is None or age < max_age_seconds:
                        continue
                    if ch._has_activity(buf):
                        # Swept without a SessionEnd: say so on the record
                        # instead of leaving it looking normally finished.
                        losses = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict)]
                        if not any(r.get("kind") == ch.REASON_SESSION_END_NOT_OBSERVED for r in losses):
                            losses.append(ch.make_reason(ch.REASON_SESSION_END_NOT_OBSERVED))
                        buf["capture_losses"] = losses
                        buf["idle_sealed_at"] = ch._now()
                        buf["idle_sealed_after_seconds"] = int(max_age_seconds)
                        swept_entry, _ = ch._fold(buf, repo_root, finalize=True)
                        # The idle boundary seals a partial Receipt, not a session end.
                        # Reuse the existing remote spool and delivery path.
                        remote_events = ch._remote_capture_new_events(buf)
                        if remote_events is not None:
                            ch._remote_capture_record(
                                repo_root, remote_events, buf.get("record") or {}, swept_entry,
                                finalized=True,
                            )
                        ch._schedule_post_session_verify(repo_root, swept_entry)
                    path.unlink()
                folded.append(sid)
            except Exception:
                continue
    except Exception:
        pass
    return folded
