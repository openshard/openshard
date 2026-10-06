"""Per-repository sync state: ``.openshard/sync-outbox.jsonl``.

One JSON line per ``receipt_id`` that has reached a decision with the
Platform. Pending work is not stored: it is *derived* on every flush by
comparing ``runs.jsonl`` (the source of truth) with this file, so a
receipt can never be forgotten because a queue entry was lost, and a
replay of an already-synced receipt is free (the Platform answers
``duplicate``). States:

============  ==============================================================
``synced``    accepted (``created`` or ``duplicate``); ``synced_hash`` is
              the local payload hash and ``record_hash`` the record's
              ``content_hash`` at that moment
``stale``     the local record changed after it was synced (its
              ``content_hash`` moved). The hosted copy is the earlier one;
              nothing is resent in this version (see docs/platform-sync.md)
``conflict``  the Platform already holds different content under this
              ``receipt_id`` for this organisation (409); never retried
``rejected``  the Platform refused the payload (400/413/422); the error code
              and field paths are kept, the payload is not; never retried
============  ==============================================================

Later verification evidence (``sync/evidence.py``) rides on the same line:
``evidence_hash`` is the hash of the evidence + state last decided with the
Platform and ``evidence_state`` how that went (``synced`` / ``conflict`` /
``rejected``). Later usage evidence (``sync/usage.py``) uses ``usage_hash``
and ``usage_state`` the same way. A new attestation changes the hash, so
it is sent again; the same hash is never sent twice.

A record keyed to a different endpoint or organisation than the current
link is treated as unsynced for the current link, so reconnecting to a
new organisation resends everything there without touching the old state.
Writes go through the locked JSONL store shared with ``runs.jsonl``, keyed
by ``receipt_id`` (replace-in-place, else append). Nothing in this file is
secret: no key, no payload, no prompt text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.jsonl_store import upsert_jsonl

OUTBOX_FILENAME = "sync-outbox.jsonl"
OUTBOX_RELPATH = Path(".openshard") / OUTBOX_FILENAME

STATE_SYNCED = "synced"
STATE_STALE = "stale"
STATE_CONFLICT = "conflict"
STATE_REJECTED = "rejected"
STATES: frozenset[str] = frozenset({STATE_SYNCED, STATE_STALE, STATE_CONFLICT, STATE_REJECTED})
TERMINAL_STATES: frozenset[str] = frozenset({STATE_CONFLICT, STATE_REJECTED})

EVIDENCE_KEYS: tuple[str, ...] = ("evidence_hash", "evidence_state", "evidence_at", "evidence_error")
USAGE_KEYS: tuple[str, ...] = ("usage_hash", "usage_state", "usage_at", "usage_error")

_LOCK_TIMEOUT_SECONDS = 5.0
_MAX_DETAIL_ITEMS = 20
_MAX_DETAIL_CHARS = 200


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def outbox_path(root: Path) -> Path:
    return Path(root) / OUTBOX_RELPATH


def load_outbox(root: Path) -> dict[str, dict]:
    """``receipt_id -> record`` for every well-formed line; the last line per id wins. Never raises."""
    path = outbox_path(root)
    out: dict[str, dict] = {}
    try:
        if not path.exists():
            return out
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        rid = parsed.get("receipt_id")
        if isinstance(rid, str) and rid and parsed.get("state") in STATES:
            out[rid] = parsed
    return out


def _bounded_details(details: object) -> list[dict] | None:
    """Keep field paths and fixed messages from a Platform error body; nothing else.

    The Platform never echoes client values in its errors, but this side
    bounds them anyway: at most a few short ``{path, message}`` items, and
    only strings.
    """
    if not isinstance(details, (list, dict)):
        return None
    if isinstance(details, dict):
        supported = details.get("supported")
        if isinstance(supported, list):
            return [{"path": "", "message": "supported: " + ", ".join(str(s)[:32] for s in supported[:8])}]
        items = details.get("violations") or details.get("issues") or []
    else:
        items = details
    if not isinstance(items, list):
        return None
    out: list[dict] = []
    for item in items[:_MAX_DETAIL_ITEMS]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        message = item.get("message") or item.get("kind")
        out.append({
            "path": str(path)[:_MAX_DETAIL_CHARS] if isinstance(path, str) else "",
            "message": str(message)[:_MAX_DETAIL_CHARS] if isinstance(message, str) else "",
        })
    return out or None


def make_record(
    receipt_id: str,
    state: str,
    *,
    endpoint: str,
    organisation_id: str,
    payload_hash: str | None = None,
    record_hash: str | None = None,
    status: int | None = None,
    code: str | None = None,
    details: object = None,
    previous: dict | None = None,
) -> dict[str, Any]:
    """A fresh outbox record. ``previous`` carries ``synced_at`` / ``attempts`` forward."""
    if state not in STATES:
        raise ValueError(f"unknown outbox state: {state!r}")
    now = _now()
    prev = previous if isinstance(previous, dict) else {}
    attempts = int(prev.get("attempts") or 0) + 1
    record: dict[str, Any] = {
        "receipt_id": receipt_id,
        "state": state,
        "endpoint": endpoint,
        "organisation_id": organisation_id,
        "attempts": attempts,
        "updated_at": now,
        "synced_at": prev.get("synced_at") if isinstance(prev.get("synced_at"), str) else None,
        "synced_hash": prev.get("synced_hash") if isinstance(prev.get("synced_hash"), str) else None,
        "record_hash": prev.get("record_hash") if isinstance(prev.get("record_hash"), str) else None,
        "last_error": None,
    }
    # Evidence bookkeeping belongs to the hosted copy it was sent for.
    if prev.get("endpoint") == endpoint and prev.get("organisation_id") == organisation_id:
        for key in EVIDENCE_KEYS + USAGE_KEYS:
            if key in prev:
                record[key] = prev[key]
    if state == STATE_SYNCED:
        record["synced_at"] = now
        record["synced_hash"] = payload_hash
        record["record_hash"] = record_hash
    elif state == STATE_STALE:
        record["attempts"] = int(prev.get("attempts") or 0)  # nothing was sent
    if state in (STATE_CONFLICT, STATE_REJECTED):
        record["last_error"] = {"status": status, "code": code, "details": _bounded_details(details), "at": now}
    return record


def with_evidence(
    record: dict,
    state: str,
    *,
    evidence_hash: str,
    status: int | None = None,
    code: str | None = None,
    details: object = None,
) -> dict[str, Any]:
    """*record* with the outcome of sending its later verification evidence. The receipt's own state is untouched."""
    if state not in (STATE_SYNCED, STATE_CONFLICT, STATE_REJECTED):
        raise ValueError(f"unknown evidence state: {state!r}")
    now = _now()
    out = dict(record)
    out["evidence_hash"] = evidence_hash
    out["evidence_state"] = state
    out["evidence_at"] = now
    out["evidence_error"] = (
        None if state == STATE_SYNCED
        else {"status": status, "code": code, "details": _bounded_details(details), "at": now}
    )
    return out


def with_usage(
    record: dict,
    state: str,
    *,
    usage_hash: str,
    status: int | None = None,
    code: str | None = None,
    details: object = None,
) -> dict[str, Any]:
    """*record* with the outcome of sending its later usage evidence. The receipt's own state is untouched."""
    if state not in (STATE_SYNCED, STATE_CONFLICT, STATE_REJECTED):
        raise ValueError(f"unknown usage state: {state!r}")
    now = _now()
    out = dict(record)
    out["usage_hash"] = usage_hash
    out["usage_state"] = state
    out["usage_at"] = now
    out["usage_error"] = (
        None if state == STATE_SYNCED
        else {"status": status, "code": code, "details": _bounded_details(details), "at": now}
    )
    return out


def put(root: Path, record: dict) -> str:
    """Write *record* (replace the line for its ``receipt_id``, else append). Returns ``replaced``/``appended``."""
    rid = record["receipt_id"]
    return upsert_jsonl(
        outbox_path(root),
        record,
        lambda existing: existing.get("receipt_id") == rid,
        timeout=_LOCK_TIMEOUT_SECONDS,
    )


def matches_link(record: dict, *, endpoint: str, organisation_id: str) -> bool:
    return record.get("endpoint") == endpoint and record.get("organisation_id") == organisation_id


def summarize(records: dict[str, dict], *, endpoint: str | None = None, organisation_id: str | None = None) -> dict[str, int]:
    """Counts by state, optionally only for one link."""
    counts = {state: 0 for state in sorted(STATES)}
    for record in records.values():
        if endpoint is not None and organisation_id is not None:
            if not matches_link(record, endpoint=endpoint, organisation_id=organisation_id):
                continue
        counts[record["state"]] = counts.get(record["state"], 0) + 1
    return counts
