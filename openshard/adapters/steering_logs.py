"""Steering from Cursor's, Antigravity's, Grok Build's and Hermes' own local session logs.

The same rule as the Claude Code transcript and the Codex rollout
(``adapters/steering.py``): each agent's own log is read where it is written
(the user's machine, or the VM it runs in), a message is classified there,
and its text is dropped at once. Nothing read here is stored but the facts:
kind, intent, length, times, a decision. The log's locator stays on the
transient staging buffer, like ``transcript_path`` today.

None of these formats is a published, versioned schema. Each reader takes
only the documented or widely observed shapes below, skips anything else
(an unknown shape under-reports, never invents), never raises into capture,
and names the format it read on the Receipt (``capture.steering.log_formats``).

Cursor -- ``transcript_path`` (``~/.cursor/projects/<project>/agent-transcripts/
<conversation>.jsonl``): one JSON object per line; a user message is
``role: user`` whose text holds ``<user_query>``; a ``{"type": "turn_ended"}``
line closes a turn. Lines carry no time, so a log line never stands alone: a
prompt the ``beforeSubmitPrompt`` hook saw (with its time) after no observed
turn end, whose transcript row also has no ``turn_ended`` before it, was sent
while the previous generation was still running: an interjection.
Permission decisions: Cursor's local store (``User/globalStorage/state.vscdb``,
table ``cursorDiskKV``, ``bubbleId:<conversation>:<bubble>`` rows), read
read-only: ``toolFormerData.userDecision`` ``accepted`` / ``rejected`` with
the row's ``createdAt``. A row without a time is skipped.

Antigravity -- ``transcriptPath`` (``~/.gemini/antigravity*/brain/<conversation>/
.system_generated/logs/transcript.jsonl``): one step per line (``step_index``,
``source``, ``type``, ``created_at``, ``content``). A ``USER_INPUT`` step is a
prompt the person typed (its ``<USER_REQUEST>`` part is classified): the first
is the initial prompt; a later one after a ``Stop`` is a follow-up; a later
one with no turn end since the previous prompt was sent while the agent ran
(an interjection). Steps are de-duplicated by ``step_index``.

Grok Build -- the session directory ``transcriptPath`` names
(``~/.grok/sessions/<cwd>/<session>/``), file ``updates.jsonl``: one ACP
``session/update`` per line (``params.update.sessionUpdate``). A user message
that starts after the agent produced output in the same turn and before
``turn_completed`` was sent mid-turn (``follow_up_behavior = "steer"``).
A permission answer, where a line records one, is read from its ACP option
kind (``allow_once``, ``allow_always``, ``reject_once``, ``reject_always``).

Hermes -- ``$HERMES_HOME/state.db`` (default ``~/.hermes``), table ``messages``
(read-only): a ``role = 'user'`` row with ``display_kind = 'steer'`` (an
out-of-band message injected mid-turn) or whose ``display_metadata`` carries
``_queued_prompt`` (typed while the agent was busy, delivered next) is an
interjection, at its own ``timestamp``. Hermes' ``schema_version`` is recorded.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.adapters import steering as st
from openshard.adapters.capture_agents import (
    AGENT_ANTIGRAVITY,
    AGENT_CURSOR,
    AGENT_GROK_BUILD,
)

LINE_AGENTS = frozenset({AGENT_CURSOR, AGENT_ANTIGRAVITY, AGENT_GROK_BUILD})

FORMAT_CURSOR = "cursor-agent-transcript-jsonl/unversioned"
FORMAT_CURSOR_STATE = "cursor-state-vscdb/unversioned"
FORMAT_ANTIGRAVITY = "antigravity-transcript-jsonl/unversioned"
FORMAT_GROK = "grok-build-updates-jsonl/acp"
FORMAT_HERMES = "hermes-state-db"

_MAX_ROWS = 500
_MAX_SEEN_STEPS = 1000
_SQL_LIMIT = 2000
_MAX_DB_VALUE = 4_000_000
_HOOK_PRECISION_SECONDS = 1.0
_ANTIGRAVITY_SLACK_SECONDS = 60.0  # a turn's USER_INPUT step is written just before the model call that opens it
_CURSOR_DECISIONS = {"accepted": "allow", "rejected": "deny"}
_ACP_DECISIONS = {"allow_once": "allow", "allow_always": "always_allow", "reject_once": "deny", "reject_always": "deny"}
_GROK_AGENT_UPDATES = frozenset({"agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan"})
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_OUT_OF_BAND_RE = re.compile(r"^\s*\[/?OUT-OF-BAND USER MESSAGE[^\]]*\]\s*$", re.M)


# ---------------------------------------------------------------------------
# Where each agent's log is (validated: never another program's file)
# ---------------------------------------------------------------------------


def _under(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def grok_home() -> Path:
    raw = os.environ.get("GROK_HOME")
    return Path(raw) if raw else Path.home() / ".grok"


def hermes_db() -> Path:
    raw = os.environ.get("HERMES_HOME")
    return (Path(raw) if raw else Path.home() / ".hermes") / "state.db"


def cursor_state_db() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def valid_log_path(agent: str, raw: object, session_id: object) -> str | None:
    """*raw* when it names this session's own log of *agent*, else None. Never raises."""
    if not isinstance(raw, str) or not raw or len(raw) > 2_000 or raw.startswith(("\\\\", "//")):
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        path = Path(raw)
        if not path.is_absolute():
            return None
        if agent == AGENT_CURSOR:
            ok = (path.suffix == ".jsonl" and session_id in path.stem and "agent-transcripts" in path.parts
                  and _under(path, Path.home() / ".cursor"))
        elif agent == AGENT_ANTIGRAVITY:
            ok = (path.name in ("transcript.jsonl", "transcript_full.jsonl") and session_id in path.parts
                  and _under(path, Path.home() / ".gemini"))
        elif agent == AGENT_GROK_BUILD:
            ok = session_id in (path.name, path.parent.name) and _under(path, grok_home() / "sessions")
        else:
            ok = False
    except (OSError, ValueError):
        return None
    return raw if ok else None


def log_file(agent: str, raw: str) -> Path:
    """The file to read for *agent* given its validated locator."""
    path = Path(raw)
    if agent == AGENT_GROK_BUILD:
        directory = path if path.suffix == "" and not path.is_file() else path.parent
        return directory / "updates.jsonl"
    return path


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _json(raw: bytes) -> dict | None:
    try:
        record = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _iso(value: object) -> str | None:
    """An ISO-8601 or epoch (seconds or milliseconds) time as the log's own UTC millisecond stamp."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            at = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return value if isinstance(value, str) and st._parse(value) is not None else None


def _texts(value: object, depth: int = 0) -> list[str]:
    """Text parts of a message body (string, ``{text}``, ``{content}`` or a list of parts)."""
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for item in value[:50] for t in _texts(item, depth + 1)]
    if isinstance(value, Mapping):
        for key in ("text", "content", "message"):
            if key in value:
                return _texts(value[key], depth + 1)
    return []


def _note_format(buf: dict, name: str) -> None:
    formats = st._state(buf).setdefault("log_formats", [])
    if isinstance(formats, list) and name not in formats and len(formats) < 4:
        formats.append(name)


def _turn_ends_between(buf: dict, start: str | None, end: str) -> list[str]:
    lo, hi = st._parse(start), st._parse(end)
    out = []
    for item in st._state(buf)["turn_ends"]:
        at = st._parse(item.get("at")) if isinstance(item, Mapping) else None
        if at is not None and hi is not None and at <= hi and (lo is None or at > lo):
            out.append(item["at"])
    return sorted(out, key=lambda s: st._parse(s) or st._EPOCH)


def _openers(buf: dict) -> list[dict]:
    return [e for e in st._state(buf)["events"] if e.get("kind") in (st.KIND_INITIAL, st.KIND_FOLLOW_UP)]


def _merge_or_interject(buf: dict, at: str, text: str | None) -> None:
    """A mid-turn message the log shows; the same message may also have reached a prompt hook (no turn end seen)."""
    for event in _openers(buf):
        gap = st._seconds(event.get("at"), at)
        if (event.get("kind") == st.KIND_FOLLOW_UP and event.get("source") == st.SOURCE_HOOK
                and event.get("after") == "unknown" and gap is not None and abs(gap) <= st._QUEUE_MATCH_SECONDS):
            _to_interjection(buf, event)
            return
    st._interjection(buf, at, text)


def _to_interjection(buf: dict, event: dict) -> None:
    """A prompt the hook saw, which the agent's log shows arrived while a turn was running."""
    event.update(kind=st.KIND_INTERJECTION, source=st.SOURCE_TRANSCRIPT, prompt_hook=True,
                 after=st._after_activity(buf, event["at"]))
    for key in ("think_seconds", "think_capped", "from_queue"):
        event.pop(key, None)
    event.setdefault("intent", "unknown")
    for timeline in buf.get("events") or []:
        meta = timeline.get("metadata") if isinstance(timeline, dict) else None
        if (isinstance(meta, dict) and meta.get("hook") == "UserPromptSubmit"
                and st._seconds(timeline.get("occurred_at"), event["at"]) == 0):
            meta["steering_kind"] = st.KIND_INTERJECTION


def _permission(buf: dict, at: str, decision: str) -> None:
    for seen in st._state(buf)["events"]:
        if seen.get("kind") == st.KIND_PERMISSION and seen.get("source") == st.SOURCE_HOOK:
            gap = st._seconds(seen.get("at"), at)
            if gap is not None and abs(gap) <= st._DUPLICATE_SECONDS:
                return  # the same answer, already reported by a hook
    if st._add(buf, {"at": at, "turn_index": st._turn_index_at(buf, at), "kind": st.KIND_PERMISSION,
                     "decision": decision, "after": "permission_prompt", "source": st.SOURCE_TRANSCRIPT}):
        st._activity_event(buf, f"permission prompt answered: {decision}",
                           {"steering_kind": st.KIND_PERMISSION, "decision": decision}, at, "agent_reported")


def _sqlite_rows(path: Path, sql: str, params: tuple) -> list[tuple] | None:
    """Rows of one read-only query, or None when the database cannot be read (busy, locked, other schema)."""
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        return list(connection.execute(sql, params).fetchmany(_SQL_LIMIT))
    except sqlite3.Error:
        return None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Line readers (called from steering._scan, one log line at a time)
# ---------------------------------------------------------------------------


def line(agent: str, buf: dict, raw: bytes, window: datetime | None, log: dict) -> None:
    if agent == AGENT_CURSOR:
        _cursor_line(raw, log)
    elif agent == AGENT_ANTIGRAVITY:
        _antigravity_line(buf, raw, window, log)
    elif agent == AGENT_GROK_BUILD:
        _grok_line(buf, raw, window, log)


def settle(agent: str, buf: dict, log: dict) -> None:
    """After a read: what needs the whole read (and, for Cursor, the hook's prompts) to decide."""
    if agent == AGENT_CURSOR:
        if log.get("cursor"):
            _note_format(buf, FORMAT_CURSOR)
        _cursor_settle(buf, log)
        _cursor_decisions(buf)
    elif agent == AGENT_ANTIGRAVITY and log.get("antigravity"):
        _note_format(buf, FORMAT_ANTIGRAVITY)
    elif agent == AGENT_GROK_BUILD and log.get("grok"):
        _note_format(buf, FORMAT_GROK)


def _cursor_line(raw: bytes, log: dict) -> None:
    if b"user_query" not in raw and b"turn_ended" not in raw:
        return
    record = _json(raw)
    if record is None:
        return
    state = log.setdefault("cursor", {"open": False, "rows": []})
    if record.get("type") == "turn_ended":
        state["open"] = False
        return
    message = record.get("message")
    role = record.get("role") or (message.get("role") if isinstance(message, Mapping) else None)
    if role != "user" or not any("<user_query>" in t for t in _texts(message)):
        return  # harness context (git status, tool lists, subagent results), not a person's message
    state["rows"] = (list(state.get("rows") or []) + [bool(state.get("open"))])[-_MAX_ROWS:]
    state["open"] = True


def _cursor_settle(buf: dict, log: dict) -> None:
    """Pair the transcript's user rows with the hook's prompts, newest first; mark those sent mid-generation."""
    rows = (log.get("cursor") or {}).get("rows") or []
    prompts = [e for e in st._state(buf)["events"]
               if (e.get("kind") in (st.KIND_INITIAL, st.KIND_FOLLOW_UP) and e.get("source") == st.SOURCE_HOOK)
               or e.get("prompt_hook")]
    if not prompts or len(rows) < len(prompts):
        return  # the transcript does not yet hold every prompt this Receipt saw: no pairing is claimed
    for event, mid_turn in zip(prompts, rows[-len(prompts):], strict=True):
        if mid_turn and event.get("kind") == st.KIND_FOLLOW_UP and event.get("after") == "unknown":
            _to_interjection(buf, event)


def _cursor_decisions(buf: dict) -> None:
    session = buf.get("session_id")
    database = cursor_state_db()
    if not isinstance(session, str) or not session or not database.is_file():
        return
    rows = _sqlite_rows(database, "SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ?",
                        (f"bubbleId:{session}:", f"bubbleId:{session};"))
    if rows is None:
        return
    window = st._parse(buf.get("started_at"))
    read = False
    for (value,) in rows:
        blob = value.encode("utf-8", "replace") if isinstance(value, str) else value
        if not isinstance(blob, bytes) or len(blob) > _MAX_DB_VALUE or b"userDecision" not in blob:
            continue
        record = _json(blob)
        tool = record.get("toolFormerData") if record else None
        choice = tool.get("userDecision") if isinstance(tool, Mapping) else None
        decision = _CURSOR_DECISIONS.get(choice) if isinstance(choice, str) else None
        at = st._in_window(_iso(record.get("createdAt")), window) if record and decision else None
        if decision and at:
            read = True
            _permission(buf, at, decision)
    if read:
        _note_format(buf, FORMAT_CURSOR_STATE)


def _antigravity_line(buf: dict, raw: bytes, window: datetime | None, log: dict) -> None:
    if b"USER_INPUT" not in raw:
        return
    record = _json(raw)
    if record is None or record.get("type") != "USER_INPUT" or record.get("source") == "SYSTEM":
        return
    state = log.setdefault("antigravity", {"seen": []})
    step = record.get("step_index")
    seen = list(state.get("seen") or [])
    if isinstance(step, int) and not isinstance(step, bool):
        if step in seen:
            return  # the transcript repeats steps
        state["seen"] = (seen + [step])[-_MAX_SEEN_STEPS:]
    at = st._in_window(_iso(record.get("created_at") or record.get("createdAt") or record.get("timestamp")),
                       window, slack=_ANTIGRAVITY_SLACK_SECONDS)
    floor = st._parse(st._state(buf).get("inherited_end"))
    if at is None or (floor is not None and (st._parse(at) or st._EPOCH) <= floor):
        return  # an earlier Receipt's prompt
    text = "\n".join(_texts(record.get("content")))
    requests = _USER_REQUEST_RE.findall(text)
    _log_prompt(buf, at, "\n".join(requests) if requests else text)


def _log_prompt(buf: dict, at: str, text: str | None) -> None:
    """A prompt only the agent's log shows (no prompt hook): initial, follow-up, or sent mid-turn."""
    state = st._state(buf)
    openers = _openers(buf)
    facts = st.prompt_facts(text)  # classified here; the text goes no further
    previous = openers[-1]["at"] if openers else state.get("inherited_end")
    ends = _turn_ends_between(buf, previous if isinstance(previous, str) else None, at)
    index = st._turn_index_at(buf, at)
    event: dict[str, Any] = {"at": at, "source": st.SOURCE_TRANSCRIPT}
    if not openers and not state.get("prompts_before"):
        event.update(kind=st.KIND_INITIAL, after="none", turn_index=(index or 0) + 1)
    elif openers and not ends:
        st._interjection(buf, at, text)
        return
    else:
        event.update(kind=st.KIND_FOLLOW_UP, turn_index=(index or 0) + 1)
        pause = st._seconds(ends[-1], at) if ends else None
        if pause is None or pause < _HOOK_PRECISION_SECONDS:
            event["after"] = "unknown"  # hook times are whole seconds: an end this close may be after the prompt
        else:
            event["after"] = "turn_end"
            event["think_seconds"] = round(min(pause, st.THINK_CAP_SECONDS), 1)
            if pause > st.THINK_CAP_SECONDS:
                event["think_capped"] = True
    if facts is not None:
        event["prompt_chars"] = facts["chars"]
    if event["kind"] == st.KIND_FOLLOW_UP:
        if facts is not None:
            event.update(intent=facts["intent"], intent_confidence=facts["confidence"], intent_rule=facts["rule"])
        else:
            event.update(intent="unknown", intent_confidence="low", intent_rule="no_prompt_text")
    if st._add(buf, event):
        meta = {"steering_kind": event["kind"], "intent": event.get("intent")}
        st._activity_event(buf, "user prompt (from the agent's own log)", meta, at, "agent_reported")


def _grok_line(buf: dict, raw: bytes, window: datetime | None, log: dict) -> None:
    if b"sessionUpdate" not in raw:
        return
    record = _json(raw)
    params = record.get("params") if record else None
    update = params.get("update") if isinstance(params, Mapping) else (record or {}).get("update")
    if not isinstance(update, Mapping):
        return
    kind = update.get("sessionUpdate")
    if not isinstance(kind, str):
        return
    state = log.setdefault("grok", {"open": False, "agent": False, "in_user": False})
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    stamp = record.get("timestamp") if record else None
    if stamp is None and isinstance(meta, Mapping):
        stamp = meta.get("agentTimestampMs")
    if kind == "user_message_chunk":
        if state.get("in_user"):
            return  # the rest of a message already counted at its first chunk
        state["in_user"] = True
        if state.get("open") and state.get("agent"):
            at = st._in_window(_iso(stamp), window)
            if at is not None:
                _merge_or_interject(buf, at, "\n".join(_texts(update.get("content"))))
        else:
            state.update(open=True, agent=False)
        return
    state["in_user"] = False
    if kind in _GROK_AGENT_UPDATES:
        state["agent"] = True
    elif kind == "turn_completed":
        state.update(open=False, agent=False)
    elif "permission" in kind.lower():
        decision = _acp_decision(update)
        at = st._in_window(_iso(stamp), window) if decision else None
        if decision and at:
            _permission(buf, at, decision)


def _acp_decision(update: Mapping[str, Any], depth: int = 0) -> str | None:
    """The ACP permission option kind a person picked, wherever the line nests it."""
    if depth > 3:
        return None
    for key in ("optionKind", "kind", "outcome", "decision", "selected", "optionId"):
        value = update.get(key)
        if isinstance(value, str) and value in _ACP_DECISIONS:
            return _ACP_DECISIONS[value]
        if isinstance(value, Mapping):
            found = _acp_decision(value, depth + 1)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# Hermes: SQLite, read-only
# ---------------------------------------------------------------------------


def scan_hermes(buf: dict) -> None:
    database = hermes_db()
    session = buf.get("session_id")
    if not isinstance(session, str) or not session or not database.is_file():
        return
    columns = _sqlite_rows(database, "PRAGMA table_info(messages)", ())
    names = {row[1] for row in columns or [] if len(row) > 1}
    if not {"session_id", "role", "content", "timestamp"} <= names:
        return  # not the schema read here: nothing claimed
    state = st._state(buf)
    raw_log = state.get("log")
    log: dict = raw_log if isinstance(raw_log, dict) else {}
    window = st._parse(buf.get("started_at"))
    after = log.get("hermes_after") if log.get("path") == str(database) else None
    floor = max(window.timestamp() if window else 0.0, after if isinstance(after, int | float) else 0.0)
    kind_col = "display_kind" if "display_kind" in names else "NULL"
    meta_col = "display_metadata" if "display_metadata" in names else "NULL"
    rows = _sqlite_rows(database, f"SELECT content, timestamp, {kind_col}, {meta_col} FROM messages "  # noqa: S608
                                  "WHERE session_id = ? AND role = 'user' AND timestamp > ? ORDER BY timestamp",
                        (session, floor))
    if rows is None:
        return
    version = _sqlite_rows(database, "SELECT MAX(version) FROM schema_version", ())
    number = version[0][0] if version and version[0] and isinstance(version[0][0], int) else None
    _note_format(buf, f"{FORMAT_HERMES}/v{number}" if number is not None else f"{FORMAT_HERMES}/unversioned")
    latest = floor
    for content, stamp, display_kind, metadata in rows:
        if not isinstance(stamp, int | float) or isinstance(stamp, bool):
            continue
        latest = max(latest, float(stamp))
        queued = _queued(metadata)
        if display_kind != "steer" and not queued:
            continue  # an ordinary turn prompt: the pre_llm_call hook already has it
        at = _iso(float(stamp))
        if at is None:
            continue
        st._interjection(buf, at, _hermes_text(content))
        if queued:
            for event in st._state(buf)["events"]:
                if event.get("kind") == st.KIND_INTERJECTION and event.get("at") == at:
                    event["queued"] = True
    state["log"] = {"path": str(database), "offset": 0, "hermes_after": latest}
    _hermes_from_queue(buf)


def _queued(metadata: object) -> bool:
    if isinstance(metadata, bytes | str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, RecursionError):
            return False
    return isinstance(metadata, Mapping) and "_queued_prompt" in metadata


def _hermes_text(content: object) -> str | None:
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    if isinstance(text, str) and text.lstrip().startswith("["):
        try:
            text = "\n".join(_texts(json.loads(text))) or text
        except (ValueError, RecursionError):
            pass
    return _OUT_OF_BAND_RE.sub("", text).strip() if isinstance(text, str) else None


def _hermes_from_queue(buf: dict) -> None:
    """A queued message Hermes delivered as the next turn's prompt was typed (and counted) mid-turn already."""
    events = st._state(buf)["events"]
    queued = [e for e in events if e.get("queued") and isinstance(e.get("prompt_chars"), int)]
    for event in events:
        if event.get("kind") != st.KIND_FOLLOW_UP or event.get("from_queue"):
            continue
        for typed in queued:
            gap = st._seconds(typed.get("at"), event.get("at"))
            if typed.get("prompt_chars") == event.get("prompt_chars") and gap is not None and 0 <= gap <= 3600:
                event["from_queue"] = True
                break


__all__ = [
    "LINE_AGENTS",
    "cursor_state_db",
    "grok_home",
    "hermes_db",
    "line",
    "log_file",
    "scan_hermes",
    "settle",
    "valid_log_path",
]
