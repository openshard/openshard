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