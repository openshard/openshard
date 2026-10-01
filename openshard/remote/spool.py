"""The durable local spool for remote capture.

Every Event is appended here first, under a lock, before any network is
touched; a batch is acknowledged (``acked_seq`` advanced) only after the
Platform accepted it. So:

* a failed or interrupted send loses nothing: the next flush resends from
  ``acked_seq``, and the Platform de-duplicates on ``event_id``;
* a collector restart picks up where the state file says it was;
* a hook never waits for the network.

Layout (``<OPENSHARD_HOME>/remote-capture/``):

``spool.jsonl``   one line per Event: ``{"seq": n, "event": {...}}``. Lines
                  at or below ``acked_seq`` are dropped when the file is
                  next compacted.
``state.json``    ``collector_id`` (this spool's identity), ``next_seq``,
                  ``acked_seq``, the Receipt identities to link, the
                  repositories seen, and the retry/backoff bookkeeping.

The spool belongs to one capture. Attaching to a different capture starts a
fresh spool: Events captured for one capture are never sent to another.

What is spooled is what may be sent: ``wire_event`` bounds every field to
the Platform contract and drops anything that must not leave (absolute
paths, forbidden keys, non-scalar metadata) *before* the Event is written,
so the spool never holds more than the wire would carry. Nothing here is
secret: no token is ever written to the spool or its state.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.adapters.claude_capture_client import capture_home
from openshard.history.jsonl_store import history_file_lock
from openshard.safety.sanitize import is_absolute_path

SPOOL_DIRNAME = "remote-capture"
SPOOL_FILENAME = "spool.jsonl"
STATE_FILENAME = "state.json"

BATCH_EVENTS = 100  # Platform: REMOTE_CAPTURE_LIMITS.batchEvents
MAX_LINKS = 10  # Platform: receiptsPerCapture
MAX_SPOOLED_EVENTS = 5000  # Platform: eventsPerCapture; past it the journal is full anyway
_MAX_FILE_EVENT_IDS = 2000
_MAX_REPOS = 16
_LOCK_TIMEOUT_SECONDS = 5.0

_ACTION_LIMIT = 200
_TEXT_LIMIT = 120
_IDENT_LIMIT = 256
_META_VALUE_LIMIT = 300
_META_KEYS = 24

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9a-f]{32}$")
# Platform packages/sync/src/privacy.ts FORBIDDEN_KEYS: never a metadata key on the wire.
_FORBIDDEN_KEYS = frozenset({
    "prompt", "prompts", "system_prompt", "transcript", "transcripts", "messages", "conversation", "stdout",
    "stderr", "output", "raw_output", "diff", "patch", "agent_notes", "run_timeline", "timeline", "env",
    "environ", "environment", "secrets", "secret", "api_key", "apikey", "access_token", "refresh_token",
    "password", "authorization", "cookie", "private_key", "adapter_stdout_summary", "adapter_stderr_summary",
})
# Platform packages/sync/src/privacy.ts SECRET_PATTERNS, mirrored so that what
# the Platform would refuse is withheld here instead of poisoning a batch.
# Core's own scrubber already ran when the Event was built; this is the
# Platform's narrower list, not a second heuristic.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bos[acfkmrs]_[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
)


def spool_dir(env: dict | os._Environ | None = None) -> Path:
    return Path(capture_home(env)) / SPOOL_DIRNAME


def _spool_path(env: dict | os._Environ | None) -> Path:
    return spool_dir(env) / SPOOL_FILENAME


def _state_path(env: dict | os._Environ | None) -> Path:
    return spool_dir(env) / STATE_FILENAME


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# What may leave: one Event, bounded to the wire contract
# ---------------------------------------------------------------------------


def _unsafe(text: str) -> bool:
    stripped = text.strip()
    if is_absolute_path(stripped) or stripped.startswith("file://"):
        return True
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return None if _unsafe(value) else value[:limit]


def _token(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and _TOKEN_RE.match(value) and len(value) <= 48 else fallback


def wire_event(event: object) -> dict[str, Any] | None:
    """*event* (``Event.to_dict()``) as the remote-capture contract carries it, or None when unusable.

    Core already scrubbed and bounded the Event; this applies the Platform's
    own rules on top so an Event the Platform would refuse is never spooled.
    An unsafe field is dropped (its Event still counts), never sent.
    """
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        return None
    occurred_at = event.get("occurred_at")
    action = event.get("action")
    metadata: dict[str, Any] = {}
    raw_meta = event.get("metadata")
    for key, value in (raw_meta.items() if isinstance(raw_meta, dict) else ()):
        if not isinstance(key, str) or len(key) > 64 or key.lower() in _FORBIDDEN_KEYS or len(metadata) >= _META_KEYS:
            continue
        if isinstance(value, str):
            if _unsafe(value):
                continue
            metadata[key] = value[:_META_VALUE_LIMIT]
        elif value is None or isinstance(value, (bool, int, float)):
            metadata[key] = value
        # Nested values never cross: the contract carries scalars only.
    attempt = event.get("attempt_number")
    schema_version = event.get("schema_version")
    return {
        "event_id": event_id,
        "schema_version": schema_version if isinstance(schema_version, int) and not isinstance(schema_version, bool) else None,
        "event_type": event.get("event_type") if isinstance(event.get("event_type"), str) else "unknown.event",
        "occurred_at": occurred_at if isinstance(occurred_at, str) and _STAMP_RE.match(occurred_at) else None,
        "run_id": _text(event.get("run_id"), _IDENT_LIMIT),
        "shard_id": _text(event.get("shard_id"), _IDENT_LIMIT),
        "attempt_number": attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0 else None,
        "actor": _text(event.get("actor"), _TEXT_LIMIT),
        "source": (_text(event.get("source"), 64) or "unknown"),
        # An action that still looks unsafe after Core's scrub is withheld, not sent.
        "action": (_text(action, _ACTION_LIMIT) or ("[withheld]" if isinstance(action, str) and action else "")),
        "target": _text(event.get("target"), _TEXT_LIMIT),
        "status": _token(event.get("status"), "unknown"),
        "evidence": _token(event.get("evidence"), "unknown"),
        "metadata": metadata,
        "raw_content_stored": False,
    }


def wire_link(record: object) -> dict[str, Any] | None:
    """The Receipt identity a session is building (``receipt_id`` / ``shard_id`` / ``run_id``), or None."""
    if not isinstance(record, dict):
        return None
    receipt_id = record.get("receipt_id")
    if not isinstance(receipt_id, str) or not _RECEIPT_ID_RE.match(receipt_id):
        return None
    return {
        "receipt_id": receipt_id,
        "shard_id": _text(record.get("shard_id"), _IDENT_LIMIT),
        "run_id": _text(record.get("run_id"), _IDENT_LIMIT),
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _new_state(capture_id: str) -> dict[str, Any]:
    return {
        "capture_id": capture_id,
        "collector_id": "col_" + secrets.token_hex(8),
        "next_seq": 1,
        "acked_seq": 0,
        "links": [],
        "repos": [],
        "file_event_ids": [],
        "spooled": 0,
        "sent": 0,
        "rejected": 0,
        "dropped": 0,
        "failures": 0,
        "backoff_until": None,
        "last_attempt_at": None,
        "last_contact_at": None,
        "stopped": None,
    }


def _read_state(env: dict | os._Environ | None) -> dict[str, Any] | None:
    try:
        data = json.loads(_state_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("collector_id"), str) else None


def _write_state(env: dict | os._Environ | None, state: dict[str, Any]) -> None:
    path = _state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _state_for(env: dict | os._Environ | None, capture_id: str) -> dict[str, Any]:
    """The state for *capture_id*; a spool left by another capture is discarded, never resent elsewhere."""
    state = _read_state(env)
    if state is None or state.get("capture_id") != capture_id:
        try:
            _spool_path(env).unlink()
        except OSError:
            pass
        state = _new_state(capture_id)
        _write_state(env, state)
    return state


def read_state(env: dict | os._Environ | None = None) -> dict[str, Any] | None:
    """The spool state as last written (no lock, read-only). None when there is no spool."""
    return _read_state(env)


def _lock(env: dict | os._Environ | None):
    spool_dir(env).mkdir(parents=True, exist_ok=True)
    return history_file_lock(_spool_path(env), timeout=_LOCK_TIMEOUT_SECONDS)


def update_state(env: dict | os._Environ | None, capture_id: str, **fields: Any) -> dict[str, Any]:
    """Merge *fields* into the state under the lock. Returns the new state."""
    with _lock(env):
        state = _state_for(env, capture_id)
        state.update(fields)
        _write_state(env, state)
        return state


# ---------------------------------------------------------------------------
# Append / read / acknowledge
# ---------------------------------------------------------------------------


def append(
    env: dict | os._Environ | None,
    capture_id: str,
    events: list[dict],
    *,
    link: dict | None = None,
    repo_root: Path | None = None,
    file_events: list[dict] | None = None,
) -> int:
    """Append *events* (and unseen *file_events*) to the spool. Returns how many were written.

    *file_events* carry stable ids and are re-derived at every fold, so only
    ids this spool has not written before are appended.
    """
    wire = [w for w in (wire_event(e) for e in events) if w is not None]
    with _lock(env):
        state = _state_for(env, capture_id)
        file_ids = [i for i in (state.get("file_event_ids") or []) if isinstance(i, str)]
        seen = set(file_ids)
        for raw in file_events or []:
            w = wire_event(raw)
            if w is None or w["event_id"] in seen:
                continue
            seen.add(w["event_id"])
            file_ids.append(w["event_id"])
            wire.append(w)
        state["file_event_ids"] = file_ids[-_MAX_FILE_EVENT_IDS:]

        wire_link_value = wire_link(link)
        links = [item for item in (state.get("links") or []) if isinstance(item, dict)]
        if wire_link_value and all(item.get("receipt_id") != wire_link_value["receipt_id"] for item in links):
            if len(links) < MAX_LINKS:
                links.append(wire_link_value)
        state["links"] = links
        if repo_root is not None:
            repos = [r for r in (state.get("repos") or []) if isinstance(r, str)]
            text = str(repo_root)
            if text not in repos:
                repos = (repos + [text])[-_MAX_REPOS:]
            state["repos"] = repos

        room = MAX_SPOOLED_EVENTS - int(state.get("spooled") or 0)
        if len(wire) > room:
            state["dropped"] = int(state.get("dropped") or 0) + len(wire) - max(room, 0)
            wire = wire[: max(room, 0)]
        if wire:
            seq = int(state.get("next_seq") or 1)
            with _spool_path(env).open("a", encoding="utf-8") as fh:
                for w in wire:
                    fh.write(json.dumps({"seq": seq, "event": w}, separators=(",", ":")) + "\n")
                    seq += 1
                fh.flush()
                os.fsync(fh.fileno())
            state["next_seq"] = seq
            state["spooled"] = int(state.get("spooled") or 0) + len(wire)
        _write_state(env, state)
        return len(wire)


def _read_lines(env: dict | os._Environ | None) -> list[dict]:
    try:
        text = _spool_path(env).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a torn final line from a killed writer: the Event was never acknowledged as spooled
        if isinstance(item, dict) and isinstance(item.get("seq"), int) and isinstance(item.get("event"), dict):
            out.append(item)
    return out


def pending(env: dict | os._Environ | None, capture_id: str, *, limit: int = BATCH_EVENTS) -> tuple[list[dict], dict[str, Any]]:
    """The next unacknowledged Events as wire Events (each with ``seq``), and the state."""
    with _lock(env):
        state = _state_for(env, capture_id)
        acked = int(state.get("acked_seq") or 0)
        items = [item for item in _read_lines(env) if item["seq"] > acked]
        items.sort(key=lambda item: item["seq"])
        return [{"seq": item["seq"], **item["event"]} for item in items[:limit]], state


def pending_count(env: dict | os._Environ | None = None) -> int:
    state = _read_state(env)
    if state is None:
        return 0
    return max(int(state.get("next_seq") or 1) - 1 - int(state.get("acked_seq") or 0), 0)


def acknowledge(
    env: dict | os._Environ | None, capture_id: str, upto_seq: int, *, sent: int = 0, rejected: int = 0,
) -> dict[str, Any]:
    """Mark every Event up to *upto_seq* as settled (accepted, or refused for good) and compact when drained."""
    with _lock(env):
        state = _state_for(env, capture_id)
        state["acked_seq"] = max(int(state.get("acked_seq") or 0), int(upto_seq))
        state["sent"] = int(state.get("sent") or 0) + sent
        state["rejected"] = int(state.get("rejected") or 0) + rejected
        state["failures"] = 0
        state["backoff_until"] = None
        state["last_contact_at"] = now_stamp()
        if state["acked_seq"] >= int(state.get("next_seq") or 1) - 1:
            try:
                _spool_path(env).write_text("", encoding="utf-8")
            except OSError:
                pass
        _write_state(env, state)
        return state


__all__ = [
    "BATCH_EVENTS",
    "MAX_SPOOLED_EVENTS",
    "acknowledge",
    "append",
    "now_stamp",
    "pending",
    "pending_count",
    "read_state",
    "spool_dir",
    "update_state",
    "wire_event",
    "wire_link",
]