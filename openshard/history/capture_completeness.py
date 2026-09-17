"""Capture completeness: what OpenShard knows it did *not* see (v0.4.4).

Two separate questions, two separate answers:

* **Capture depth** (``history/shard.py``, unchanged): how deep OpenShard
  could ever observe or control this run. ``full`` for runs it executed
  itself, ``partial`` for sessions observed through an agent's hooks,
  ``unknown`` otherwise. This module reads it, it does not redefine it.
* **Completeness** (this module): within the evidence that integration is
  expected to deliver, is any evidence *known* to be lost?

    complete    no known loss -- not a claim that nothing was missed, only
                that every loss detector stayed at zero
    incomplete  evidence is known lost, corrupt or never delivered;
                ``reasons`` says what
    unknown     cannot be established (a record written before loss
                tracking existed, or of unknown origin)

Hook records written by this version store ``capture.completeness =
{"status", "reasons"}``; everything else is derived at read time and
labelled ``derived``. "Everything OpenShard observed" must never read as
"everything", and a healthy partial capture must never read as a full one.

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
capture path incremented when it happened. There is no score.
"""

from __future__ import annotations

from collections.abc import Iterable

from openshard.history.shard import (
    CAPTURE_FULL,
    CAPTURE_PARTIAL,
    CAPTURE_UNKNOWN,
    derive_shard_identity,
)

COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_INCOMPLETE = "incomplete"
COMPLETENESS_UNKNOWN = "unknown"
VALID_STATUSES = frozenset({COMPLETENESS_COMPLETE, COMPLETENESS_INCOMPLETE, COMPLETENESS_UNKNOWN})
# Pre-release spellings of a stored "no known loss" status; read as complete.
_LEGACY_COMPLETE_STATUSES = frozenset({CAPTURE_FULL, CAPTURE_PARTIAL})

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


def build_completeness(reasons: Iterable[dict]) -> dict:
    """The stored block for a record this version writes: ``incomplete`` when
    any loss reason exists, else ``complete`` (every loss detector at zero)."""
    merged = merge_reasons(reasons)
    status = COMPLETENESS_INCOMPLETE if merged else COMPLETENESS_COMPLETE
    return {"status": status, "reasons": merged}


def derive_capture_completeness(entry: object) -> dict:
    """``{"depth", "status", "reasons", "derived"}`` for any record, old or new. Pure, never raises.

    ``depth`` is the unchanged capture depth from ``history/shard.py``. A
    completeness block stored by the writer (``capture.completeness``) is
    authoritative and returned with ``derived: False``. Otherwise:

    * a hook-observed record (depth ``partial``) that predates loss tracking
      is ``incomplete`` when its one pre-existing counter
      (``hook_events_dropped``) is non-zero, else ``unknown`` -- its writer
      could not detect the other kinds of loss, so no claim is made;
    * a run OpenShard executed itself (depth ``full``) is ``complete``:
      there is no capture queue to lose evidence in;
    * unknown origin is ``unknown``.

    Derived answers are labelled ``derived: True`` so a reader can tell a
    stored fact from a read-time reconstruction.
    """
    if not isinstance(entry, dict):
        return {"depth": CAPTURE_UNKNOWN, "status": COMPLETENESS_UNKNOWN, "reasons": [], "derived": True}
    _agent, _origin, depth = derive_shard_identity(entry)
    capture = entry.get("capture")
    capture = capture if isinstance(capture, dict) else {}
    stored = capture.get("completeness")
    if isinstance(stored, dict) and (
        stored.get("status") in VALID_STATUSES or stored.get("status") in _LEGACY_COMPLETE_STATUSES
    ):
        raw_reasons = stored.get("reasons")
        merged = merge_reasons(raw_reasons if isinstance(raw_reasons, list) else [])
        status = stored["status"]
        if status in _LEGACY_COMPLETE_STATUSES:
            status = COMPLETENESS_INCOMPLETE if merged else COMPLETENESS_COMPLETE
        return {"depth": depth, "status": status, "reasons": merged, "derived": False}
    reasons: list[dict] = []
    dropped = capture.get("hook_events_dropped")
    if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0:
        reasons.append(make_reason(REASON_DROPPED_HOOK_EVENTS, dropped))
    if reasons:
        status = COMPLETENESS_INCOMPLETE
    elif depth == CAPTURE_FULL:
        status = COMPLETENESS_COMPLETE
    else:
        status = COMPLETENESS_UNKNOWN
    return {"depth": depth, "status": status, "reasons": merge_reasons(reasons), "derived": True}


def gaps_display(block: dict) -> str:
    """The ``Known gaps`` value: ``None known`` / the reasons / ``Unknown (…)``."""
    status = str(block.get("status") or COMPLETENESS_UNKNOWN)
    _reasons_raw = block.get("reasons")
    reasons: list = _reasons_raw if isinstance(_reasons_raw, list) else []
    if status == COMPLETENESS_INCOMPLETE:
        details = "; ".join(str(r.get("detail") or r.get("kind")) for r in reasons[:3] if isinstance(r, dict))
        return details or "evidence known lost"
    if status == COMPLETENESS_COMPLETE:
        return "None known"
    return "Unknown (record predates loss tracking)" if block.get("derived") else "Unknown"

