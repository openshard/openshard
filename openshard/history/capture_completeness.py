"""Capture completeness: what OpenShard knows it did *not* see (v0.4.4).

``history/shard.py`` already says how deep a capture could ever be
(``capture_depth``: ``full`` for runs OpenShard executed itself, ``partial``
for sessions it only observed through an agent's hooks, ``unknown``
otherwise). This module adds the orthogonal fact a Receipt was missing:
whether evidence that *should* have reached the record is known to be
lost, and why. "Everything OpenShard observed" must never read as
"everything".

Statuses (stored in ``capture.completeness.status`` on hook records, derived
at read time for everything else)::

    full        OpenShard ran the work itself; nothing is known to be missing
    partial     observed through an integration, no known loss (the normal
                external-agent case -- OpenShard did not execute or verify)
    incomplete  evidence is known to be lost, corrupt or never delivered;
                ``reasons`` says what
    unknown     origin unknown; nothing can be claimed either way

Reasons (``kind`` values) and what produces them:

* ``corrupt_queued_event`` -- a line in the durable capture queue could not
  be decoded; the bytes were quarantined (capture service).
* ``dropped_hook_events`` -- hook events beyond the per-session buffer cap
  were counted but not staged (``capture.hook_events_dropped``).
* ``session_end_not_observed`` -- the session went idle for so long that
  its buffer was swept without a SessionEnd; a later hook may still arrive.
* ``integration_limitation`` -- the agent integration cannot deliver a
  kind of evidence at all (documented per agent).

Nothing here infers a loss: every reason is backed by a counter the
capture path incremented when it happened.
"""

from __future__ import annotations

from collections.abc import Iterable

from openshard.history.shard import (
    CAPTURE_FULL,
    CAPTURE_PARTIAL,
    CAPTURE_UNKNOWN,
    derive_shard_identity,
)

COMPLETENESS_FULL = CAPTURE_FULL
COMPLETENESS_PARTIAL = CAPTURE_PARTIAL
COMPLETENESS_INCOMPLETE = "incomplete"
COMPLETENESS_UNKNOWN = CAPTURE_UNKNOWN
VALID_STATUSES = frozenset({
    COMPLETENESS_FULL, COMPLETENESS_PARTIAL, COMPLETENESS_INCOMPLETE, COMPLETENESS_UNKNOWN,
})

REASON_CORRUPT_QUEUED_EVENT = "corrupt_queued_event"
REASON_DROPPED_HOOK_EVENTS = "dropped_hook_events"
REASON_SESSION_END_NOT_OBSERVED = "session_end_not_observed"
REASON_INTEGRATION_LIMITATION = "integration_limitation"
VALID_REASONS = frozenset({
    REASON_CORRUPT_QUEUED_EVENT, REASON_DROPPED_HOOK_EVENTS,
    REASON_SESSION_END_NOT_OBSERVED, REASON_INTEGRATION_LIMITATION,
})

_MAX_REASONS = 8
_MAX_DETAIL = 160


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def reason_detail(kind: str, count: int) -> str:
    """The human sentence for one reason. Static vocabulary, no free text."""
    if kind == REASON_CORRUPT_QUEUED_EVENT:
        return f"{count} queued {_plural(count, 'event', 'events')} could not be decoded"
    if kind == REASON_DROPPED_HOOK_EVENTS:
        return f"{count} hook {_plural(count, 'event was', 'events were')} dropped (buffer full)"
    if kind == REASON_SESSION_END_NOT_OBSERVED:
        return "session end was not observed"
    if kind == REASON_INTEGRATION_LIMITATION:
        return "the integration cannot deliver some evidence"
    return kind


def make_reason(kind: str, count: int = 1, detail: str | None = None) -> dict:
    n = int(count) if isinstance(count, int) and count > 0 else 1
    text = detail if isinstance(detail, str) and detail else reason_detail(kind, n)
    return {"kind": kind, "count": n, "detail": text[:_MAX_DETAIL]}


def merge_reasons(reasons: Iterable[dict]) -> list[dict]:
    """Sum counts per kind, keep first-seen order, bounded, validated."""
    merged: dict[str, dict] = {}
    for raw in reasons:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind not in VALID_REASONS:
            continue
        count = raw.get("count")
        n = int(count) if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 1
        if kind in merged:
            merged[kind]["count"] += n
            merged[kind]["detail"] = reason_detail(kind, merged[kind]["count"])
        else:
            merged[kind] = make_reason(kind, n, raw.get("detail") if isinstance(raw.get("detail"), str) else None)
            if merged[kind]["detail"] != reason_detail(kind, n) and n > 1:
                merged[kind]["detail"] = reason_detail(kind, n)
    return list(merged.values())[:_MAX_REASONS]


def build_completeness(reasons: Iterable[dict], *, base: str = COMPLETENESS_PARTIAL) -> dict:
    """The stored block for a record: ``incomplete`` when any reason exists, else *base*."""
    merged = merge_reasons(reasons)
    status = COMPLETENESS_INCOMPLETE if merged else base
    return {"status": status, "reasons": merged}


def derive_capture_completeness(entry: object) -> dict:
    """The completeness block for any record, old or new. Pure, never raises.

    A block stored by the writer (``capture.completeness``) is authoritative
    and returned with ``derived: False``. Otherwise the status is derived
    from ``capture_depth`` plus the one loss counter older hook records
    already carry (``hook_events_dropped``) and labelled ``derived: True``,
    so a reader can tell a stored fact from a read-time reconstruction.
    """
    if not isinstance(entry, dict):
        return {"status": COMPLETENESS_UNKNOWN, "reasons": [], "derived": True}
    capture = entry.get("capture")
    capture = capture if isinstance(capture, dict) else {}
    stored = capture.get("completeness")
    if isinstance(stored, dict) and stored.get("status") in VALID_STATUSES:
        raw_reasons = stored.get("reasons")
        return {
            "status": stored["status"],
            "reasons": merge_reasons(raw_reasons if isinstance(raw_reasons, list) else []),
            "derived": False,
        }
    _agent, _origin, depth = derive_shard_identity(entry)
    reasons: list[dict] = []
    dropped = capture.get("hook_events_dropped")
    if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0:
        reasons.append(make_reason(REASON_DROPPED_HOOK_EVENTS, dropped))
    if depth == CAPTURE_FULL:
        status = COMPLETENESS_FULL
    elif depth == CAPTURE_PARTIAL:
        status = COMPLETENESS_INCOMPLETE if reasons else COMPLETENESS_PARTIAL
    else:
        status = COMPLETENESS_UNKNOWN
    return {"status": status, "reasons": merge_reasons(reasons), "derived": True}


def completeness_display(block: dict) -> str:
    """One receipt line: ``Incomplete -- 1 queued event could not be decoded``."""
    status = str(block.get("status") or COMPLETENESS_UNKNOWN)
    label = status.capitalize()
    reasons = block.get("reasons") if isinstance(block.get("reasons"), list) else []
    if status == COMPLETENESS_INCOMPLETE and reasons:
        details = "; ".join(str(r.get("detail") or r.get("kind")) for r in reasons[:3] if isinstance(r, dict))
        return f"{label} — {details}" if details else label
    return label
