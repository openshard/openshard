"""Canonical Event — the smallest safe, first-class Event contract (Migration 3).

An Event describes one observed fact/action/state transition. It belongs
primarily to a Run/Attempt and, where known, its parent Shard — it is not a
replacement for Shard, RunAttempt, or Receipt.

An Event is never persisted to its own JSONL file or store — there is still
only ``runs.jsonl``. Two ways an Event reaches a caller now coexist, and are
never conflated:

* Legacy projection (Migration 3): most producers only ever leave behind
  loosely-typed traces (``run_timeline``, ``review_checks``,
  ``policy_decisions``, native step/checkpoint/session/interaction records,
  or an adapter entry with no Events of its own). This module derives
  Events from those traces at *read* time, on every call — nothing is
  cached or written back. This closed the "v1.3 concern" left open at
  ``shard_schema.TIMELINE_EVENT_FIELDS``.
* Embedded canonical Events (Migration 5, extended by Migration 6): a
  producer that knows how to build its own Events at *observation* time —
  the Claude Code import/wrap adapters (``adapters/claude_code_import.py``,
  ``adapters/wrap_exec.py``) and, since Migration 6, the OpenShard Native
  (OSN) run pipeline itself (``run/_pipeline_helpers.py::_build_native_events``,
  called from ``_log_run``) — calls ``make_event`` directly while the facts
  are still live, and stores the result as a plain ``events: list[dict]``
  field (via ``Event.to_dict()``) on the same run entry it is already
  writing. This is still not a separate store: it is one more field on the
  existing record, coerced and content-hashed exactly like ``files_detail``
  or ``run_timeline`` already are. ``events_from_entry`` reads it back with
  ``Event.from_dict()``, unchanged. Unlike the two adapters — external
  observers that can never claim stronger evidence than ``agent_reported``/
  ``git_observed`` for most facts — the native run pipeline genuinely
  controls execution (see ``shard.derive_shard_identity``: any
  ``executor == "native"`` entry earns ``ORIGIN_OPENSHARD_ROUTED``/
  ``CAPTURE_FULL``), so its embedded Events legitimately use
  ``EVIDENCE_DIRECTLY_OBSERVED`` throughout.

``events_from_entry`` is the single place that decides which of the two
applies to a given record, using **presence of the ``"events"`` key** —
never whether the list is non-empty — as the switch: absent means legacy
projection; present means the producer owns Events for this record, and
legacy derivation (``build_event_from_adapter_entry`` and the other
``build_events_from_*`` projectors) is skipped entirely for it, so the same
fact is never counted twice. A present-but-malformed ``events`` value fails
closed — non-list becomes ``[]``, non-dict items are dropped — rather than
silently falling back to projection: a producer that claims ownership of
Events for a record and gets it wrong should surface as "no Events", not as
a second, differently-computed set.

Honesty rules (see ``shard.derive_shard_identity`` for the model this
mirrors):

* ``actor``, ``agent`` (Shard-level), ``source`` (which producer converted
  this record), and ``evidence``/``origin`` are four separate concepts and
  must never be conflated. ``actor`` is set ONLY when the underlying source
  record carries its own explicit actor-like field (e.g.
  ``DeveloperInteractionEvent.actor``, a checkpoint's ``executor``, an
  adapter entry's ``import_source``) — never inferred from Shard identity
  heuristics. When no such explicit signal exists, ``actor`` stays ``None``.
* ``evidence`` generalizes Shard's ``origin``/``capture_depth`` honesty axis
  to per-fact granularity and legitimately reuses that reasoning (that is
  what it is for) — this is distinct from the ``actor`` rule above.
* Unknown information stays unknown: missing fields are never fabricated,
  and unrecognized enum values fall back to a safe ``*_unknown`` sentinel,
  never guessed.

Event IDs come from two distinct paths, never conflated:

* Projecting a legacy record that already carries its own id (native steps,
  checkpoints, session events, interactions) reuses that id unchanged.
* Projecting a legacy record with no id of its own (timeline events, review
  checks, policy decisions) uses ``_stable_event_id`` — deterministic, so
  repeated projection of the same historical record is idempotent, but
  never written back to disk and never derived from list position alone.
* A natively-created Event (``make_event`` called with no ``event_id``) gets
  a genuinely unique ``uuid4`` — never a deterministic hash — so two
  independently created Events can never collide.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from openshard.history.interactions import (
    DeveloperInteractionEvent,
    interaction_events_for_run,
)
from openshard.history.native_steps import NativeStepEvent, native_step_events_for_run
from openshard.history.run_checkpoints import (
    NativeRunCheckpointEvent,
    run_checkpoints_for_run,
)
from openshard.history.shard import (
    CAPTURE_FULL,
    ORIGIN_EXTERNAL_OBSERVED,
    ORIGIN_OPENSHARD_ROUTED,
    derive_shard_identity,
)
from openshard.history.shard_schema import SHARD_BLOCKED_FIELDS
from openshard.safety.sanitize import sanitize_metadata, sanitize_text

EVENT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_STARTED = "started"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_WARNING = "warning"
STATUS_UNKNOWN = "unknown"
# A superset of provenance.VALID_STATUSES (adds "started") -- an Event, unlike
# a ProvenanceRecord, must be able to represent an in-progress step.
VALID_EVENT_STATUSES: frozenset[str] = frozenset(
    {STATUS_STARTED, STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED, STATUS_WARNING, STATUS_UNKNOWN}
)

# ---------------------------------------------------------------------------
# Evidence vocabulary -- generalizes Shard.origin/capture_depth per-fact.
# Categorical, not numeric: nothing in this codebase scores evidence by
# "strength", so this deliberately avoids that framing.
# ---------------------------------------------------------------------------

EVIDENCE_DIRECTLY_OBSERVED = "directly_observed"
EVIDENCE_AGENT_REPORTED = "agent_reported"
EVIDENCE_GIT_OBSERVED = "git_observed"
EVIDENCE_INDEPENDENTLY_VERIFIED = "independently_verified"
EVIDENCE_UNKNOWN = "unknown"
VALID_EVIDENCE: frozenset[str] = frozenset(
    {
        EVIDENCE_DIRECTLY_OBSERVED,
        EVIDENCE_AGENT_REPORTED,
        EVIDENCE_GIT_OBSERVED,
        EVIDENCE_INDEPENDENTLY_VERIFIED,
        EVIDENCE_UNKNOWN,
    }
)

# ---------------------------------------------------------------------------
# event_type vocabulary -- describes WHAT HAPPENED, not which store produced
# it (that is `source`, below). Small, closed, consolidated from the real
# vocabularies already found across existing producers (timeline kind+status,
# checkpoint stage+status, interaction event_type, policy decision+approval
# flags) rather than invented.
# ---------------------------------------------------------------------------

EVENT_RUN_STARTED = "run.started"
EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_TOOL_INVOKED = "tool.invoked"
EVENT_FILE_CHANGED = "file.changed"
EVENT_VERIFICATION_STARTED = "verification.started"
EVENT_VERIFICATION_PASSED = "verification.passed"
EVENT_VERIFICATION_FAILED = "verification.failed"
EVENT_VERIFICATION_SKIPPED = "verification.skipped"
EVENT_POLICY_CHECKED = "policy.checked"
EVENT_APPROVAL_REQUESTED = "approval.requested"
EVENT_APPROVAL_GRANTED = "approval.granted"
EVENT_APPROVAL_DENIED = "approval.denied"
EVENT_RETRY_STARTED = "retry.started"
EVENT_RECEIPT_SEALED = "receipt.sealed"
EVENT_SESSION_STARTED = "session.started"
EVENT_SESSION_ACTIVITY = "session.activity"
EVENT_INTERACTION_ACCEPTED = "interaction.accepted"
EVENT_INTERACTION_REJECTED = "interaction.rejected"
EVENT_INTERACTION_EDITED = "interaction.edited"
EVENT_INTERACTION_FLAGGED = "interaction.flagged"
EVENT_UNKNOWN = "unknown.event"  # fallback when a type cannot be determined without guessing

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_RUN_STARTED,
        EVENT_RUN_COMPLETED,
        EVENT_RUN_FAILED,
        EVENT_TOOL_INVOKED,
        EVENT_FILE_CHANGED,
        EVENT_VERIFICATION_STARTED,
        EVENT_VERIFICATION_PASSED,
        EVENT_VERIFICATION_FAILED,
        EVENT_VERIFICATION_SKIPPED,
        EVENT_POLICY_CHECKED,
        EVENT_APPROVAL_REQUESTED,
        EVENT_APPROVAL_GRANTED,
        EVENT_APPROVAL_DENIED,
        EVENT_RETRY_STARTED,
        EVENT_RECEIPT_SEALED,
        EVENT_SESSION_STARTED,
        EVENT_SESSION_ACTIVITY,
        EVENT_INTERACTION_ACCEPTED,
        EVENT_INTERACTION_REJECTED,
        EVENT_INTERACTION_EDITED,
        EVENT_INTERACTION_FLAGGED,
        EVENT_UNKNOWN,
    }
)

# ---------------------------------------------------------------------------
# Source vocabulary -- which existing producer this Event was converted from.
# Purely mechanical bookkeeping (not a truth claim), so unlike event_type it
# is never allow-list-validated -- mirrors how provenance.source_name is
# never validated either, only provenance.source_type is.
# ---------------------------------------------------------------------------

SOURCE_RUN_TIMELINE = "run_timeline"
SOURCE_NATIVE_STEPS = "native_steps"
SOURCE_RUN_CHECKPOINTS = "run_checkpoints"
SOURCE_SESSION_EVENTS = "session_events"
SOURCE_INTERACTIONS = "interactions"
SOURCE_REVIEW_CHECKS = "review_checks"
SOURCE_POLICY_DECISIONS = "policy_decisions"
SOURCE_CLAUDE_CODE_IMPORT = "claude_code_import"
SOURCE_CLAUDE_CODE_WRAP = "claude_code_wrap"
SOURCE_NATIVE_RUN = "native_run"

_ACTION_LIMIT = 120
_TARGET_LIMIT = 80
_ACTOR_LIMIT = 60


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Event:
    event_id: str
    event_type: str
    occurred_at: str | None
    run_id: str | None
    shard_id: str | None
    attempt_number: int | None
    actor: str | None
    source: str
    action: str
    target: str | None
    status: str
    evidence: str
    metadata: dict = field(default_factory=dict)
    raw_content_stored: bool = False
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.raw_content_stored = False

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "run_id": self.run_id,
            "shard_id": self.shard_id,
            "attempt_number": self.attempt_number,
            "actor": self.actor,
            "source": self.source,
            "action": self.action,
            "target": self.target,
            "status": self.status,
            "evidence": self.evidence,
            "metadata": self.metadata,
            "raw_content_stored": False,
        }

    @classmethod
    def from_dict(cls, d: object) -> Event:
        if not isinstance(d, dict):
            d = {}
        attempt_number = d.get("attempt_number")
        if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
            attempt_number = None
        event_type = d.get("event_type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            event_type = EVENT_UNKNOWN
        status = d.get("status")
        if not isinstance(status, str) or status not in VALID_EVENT_STATUSES:
            status = STATUS_UNKNOWN
        evidence = d.get("evidence")
        if not isinstance(evidence, str) or evidence not in VALID_EVIDENCE:
            evidence = EVIDENCE_UNKNOWN
        metadata = d.get("metadata")
        return cls(
            event_id=str(d.get("event_id") or ""),
            event_type=event_type,
            occurred_at=d.get("occurred_at") if isinstance(d.get("occurred_at"), str) else None,
            run_id=d.get("run_id") if isinstance(d.get("run_id"), str) else None,
            shard_id=d.get("shard_id") if isinstance(d.get("shard_id"), str) else None,
            attempt_number=attempt_number,
            actor=d.get("actor") if isinstance(d.get("actor"), str) else None,
            source=str(d.get("source") or "unknown"),
            action=str(d.get("action") or ""),
            target=d.get("target") if isinstance(d.get("target"), str) else None,
            status=status,
            evidence=evidence,
            metadata=metadata if isinstance(metadata, dict) else {},
            raw_content_stored=False,
            schema_version=d.get("schema_version", EVENT_SCHEMA_VERSION),
        )


# ---------------------------------------------------------------------------
# ID helper -- for read-time projection of legacy records only.
# ---------------------------------------------------------------------------


def _stable_event_id(source: str, source_name: str, index: int, run_ref: str = "unknown-run") -> str:
    """Deterministic id for projecting a legacy record that has no id of its own.

    Same inputs always produce the same id, so re-projecting the same
    historical record on every read is idempotent -- but this id is never
    written back to disk. NOT for natively-created Events; those get a
    genuinely unique id via ``make_event`` (see module docstring).
    """
    key = f"{run_ref}:{source}:{source_name}:{index}"
    return "evt-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _sanitize_event_metadata(metadata: object) -> dict:
    """sanitize_metadata plus an explicit blocked-field-name cross-check.

    sanitize_metadata scrubs unsafe *values* but does not know about
    SHARD_BLOCKED_FIELDS *names* -- only shard_schema.coerce_shard_entry does,
    and only at the top-level run-entry. An Event's metadata is a second
    place arbitrary keys could carry a blocked field name, so it is
    cross-checked here too.
    """
    if not isinstance(metadata, dict):
        return {}
    filtered = {k: v for k, v in metadata.items() if k not in SHARD_BLOCKED_FIELDS}
    return sanitize_metadata(filtered)


def _evidence_from_origin(origin: str | None, capture_depth: str | None) -> str:
    if origin == ORIGIN_OPENSHARD_ROUTED and capture_depth == CAPTURE_FULL:
        return EVIDENCE_DIRECTLY_OBSERVED
    if origin == ORIGIN_EXTERNAL_OBSERVED:
        return EVIDENCE_AGENT_REPORTED
    return EVIDENCE_UNKNOWN


# ---------------------------------------------------------------------------
# Safe constructor
# ---------------------------------------------------------------------------


def make_event(
    *,
    event_type: object,
    source: object,
    action: object,
    event_id: str | None = None,
    occurred_at: object = None,
    run_id: object = None,
    shard_id: object = None,
    attempt_number: object = None,
    actor: object = None,
    target: object = None,
    status: object = STATUS_UNKNOWN,
    evidence: object = EVIDENCE_UNKNOWN,
    metadata: object = None,
) -> Event:
    """Safe Event constructor. Never raises.

    ``event_id``: pass the source record's own id when projecting a legacy
    event that already has one, or a value from ``_stable_event_id()`` when
    projecting one that doesn't. Omit entirely for a natively-created Event
    -- a genuinely unique id (uuid4) is minted, never a deterministic hash.
    """
    try:
        _event_type = event_type if isinstance(event_type, str) and event_type in EVENT_TYPES else EVENT_UNKNOWN
        _status = status if isinstance(status, str) and status in VALID_EVENT_STATUSES else STATUS_UNKNOWN
        _evidence = evidence if isinstance(evidence, str) and evidence in VALID_EVIDENCE else EVIDENCE_UNKNOWN

        _action = sanitize_text(action, _ACTION_LIMIT) or ""
        _target = sanitize_text(target, _TARGET_LIMIT) if isinstance(target, str) else None
        _actor = sanitize_text(actor, _ACTOR_LIMIT) if isinstance(actor, str) and actor else None
        _source = source if isinstance(source, str) and source else "unknown"

        _run_id = run_id if isinstance(run_id, str) and run_id else None
        _shard_id = shard_id if isinstance(shard_id, str) and shard_id else None
        _attempt_number = (
            attempt_number
            if isinstance(attempt_number, int) and not isinstance(attempt_number, bool)
            else None
        )
        _occurred_at = occurred_at if isinstance(occurred_at, str) and occurred_at else None

        _event_id = event_id or str(uuid.uuid4())

        return Event(
            event_id=_event_id,
            event_type=_event_type,
            occurred_at=_occurred_at,
            run_id=_run_id,
            shard_id=_shard_id,
            attempt_number=_attempt_number,
            actor=_actor,
            source=_source,
            action=_action,
            target=_target,
            status=_status,
            evidence=_evidence,
            metadata=_sanitize_event_metadata(metadata),
            raw_content_stored=False,
        )
    except Exception:
        return Event(
            event_id=event_id or str(uuid.uuid4()),
            event_type=EVENT_UNKNOWN,
            occurred_at=None,
            run_id=None,
            shard_id=None,
            attempt_number=None,
            actor=None,
            source="unknown",
            action="",
            target=None,
            status=STATUS_UNKNOWN,
            evidence=EVIDENCE_UNKNOWN,
            metadata={},
            raw_content_stored=False,
        )


# ---------------------------------------------------------------------------
# Per-source conversion functions -- the actual seam.
# All pure, never raise, tolerant of malformed/legacy shapes.
# ---------------------------------------------------------------------------


def _timeline_event_type(event_key: str, kind: str, status: str) -> str:
    if event_key == "receipt_saved" or kind == "receipt":
        return EVENT_RECEIPT_SEALED
    if kind == "run":
        if status == STATUS_STARTED:
            return EVENT_RUN_STARTED
        if status == STATUS_PASSED:
            return EVENT_RUN_COMPLETED
        if status == STATUS_FAILED:
            return EVENT_RUN_FAILED
        return EVENT_UNKNOWN
    if kind in ("check", "review"):
        if status == STATUS_PASSED:
            return EVENT_VERIFICATION_PASSED
        if status == STATUS_FAILED:
            return EVENT_VERIFICATION_FAILED
        if status == STATUS_SKIPPED:
            return EVENT_VERIFICATION_SKIPPED
        if status == STATUS_STARTED:
            return EVENT_VERIFICATION_STARTED
        return EVENT_UNKNOWN
    if kind == "tool":
        return EVENT_TOOL_INVOKED
    return EVENT_UNKNOWN


def build_events_from_timeline(
    timeline: object,
    *,
    run_id: str | None = None,
    shard_id: str | None = None,
    attempt_number: int | None = None,
    entry_origin: str | None = None,
    entry_capture_depth: str | None = None,
    run_ref: str = "unknown-run",
) -> list[Event]:
    """Project a RunTimelineEvent dict list into canonical Events.

    RunTimelineEvent carries no timestamp today, so ``occurred_at`` is always
    None here -- an honest gap, not fabricated. Reuses the same
    completed->passed style status mapping already established in
    ``provenance.build_provenance_from_timeline_events``.
    """
    if not isinstance(timeline, list):
        return []
    _status_map = {
        "completed": STATUS_PASSED,
        "failed": STATUS_FAILED,
        "skipped": STATUS_SKIPPED,
        "warning": STATUS_WARNING,
        "started": STATUS_STARTED,
    }
    evidence = _evidence_from_origin(entry_origin, entry_capture_depth)
    events: list[Event] = []
    for i, ev in enumerate(timeline):
        if not isinstance(ev, dict):
            continue
        try:
            kind = ev.get("kind") or "run"
            raw_status = ev.get("status") or "completed"
            status = _status_map.get(raw_status, STATUS_UNKNOWN)
            event_key = ev.get("event") or ""
            event_type = _timeline_event_type(event_key, kind, status)
            label = ev.get("label") or event_key or "event"
            target = ev.get("target")
            source_name = event_key or f"timeline-{i}"
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_RUN_TIMELINE,
                    action=label,
                    event_id=_stable_event_id(SOURCE_RUN_TIMELINE, source_name, i, run_ref),
                    occurred_at=None,
                    run_id=run_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    target=target if isinstance(target, str) else None,
                    status=status,
                    evidence=evidence,
                    metadata=ev.get("metadata"),
                )
            )
        except Exception:
            continue
    return events


def build_events_from_native_steps(steps: object) -> list[Event]:
    """Project NativeStepEvent records into canonical Events.

    Reuses each step's own event_id and timestamp unchanged -- this is a
    genuinely id-and-timestamp-bearing source, not a synthesis case.
    """
    if not isinstance(steps, list):
        return []
    _status_map = {
        "passed": STATUS_PASSED,
        "failed": STATUS_FAILED,
        "skipped": STATUS_SKIPPED,
        "started": STATUS_STARTED,
    }
    events: list[Event] = []
    for step in steps:
        if not isinstance(step, NativeStepEvent):
            continue
        try:
            if step.tool_name:
                event_type = EVENT_TOOL_INVOKED
            elif step.stage == "verify":
                _map = {
                    "passed": EVENT_VERIFICATION_PASSED,
                    "failed": EVENT_VERIFICATION_FAILED,
                    "skipped": EVENT_VERIFICATION_SKIPPED,
                    "started": EVENT_VERIFICATION_STARTED,
                }
                event_type = _map.get(step.status, EVENT_UNKNOWN)
            elif step.stage == "retry":
                event_type = EVENT_RETRY_STARTED
            else:
                event_type = EVENT_UNKNOWN
            status = _status_map.get(step.status, STATUS_UNKNOWN)
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_NATIVE_STEPS,
                    action=step.summary or step.step_name,
                    event_id=step.event_id or None,
                    occurred_at=step.timestamp or None,
                    run_id=step.run_id or None,
                    target=step.tool_name or None,
                    status=status,
                    evidence=EVIDENCE_DIRECTLY_OBSERVED,
                    metadata=step.metadata,
                )
            )
        except Exception:
            continue
    return events


def _checkpoint_event_type(cp: NativeRunCheckpointEvent) -> str:
    if cp.stage == "verify":
        _map = {
            "passed": EVENT_VERIFICATION_PASSED,
            "failed": EVENT_VERIFICATION_FAILED,
            "skipped": EVENT_VERIFICATION_SKIPPED,
            "started": EVENT_VERIFICATION_STARTED,
        }
        return _map.get(cp.status, EVENT_UNKNOWN)
    if cp.stage == "retry":
        return EVENT_RETRY_STARTED
    if cp.stage == "receipt" and cp.status == "passed":
        return EVENT_RECEIPT_SEALED
    if cp.stage == "final":
        if cp.status == "passed":
            return EVENT_RUN_COMPLETED
        if cp.status == "failed":
            return EVENT_RUN_FAILED
        return EVENT_UNKNOWN
    if cp.stage == "sandbox_write" and cp.status == "passed" and cp.files:
        return EVENT_FILE_CHANGED
    return EVENT_UNKNOWN


def build_events_from_checkpoints(checkpoints: object) -> list[Event]:
    """Project NativeRunCheckpointEvent records into canonical Events.

    ``actor`` is set from the checkpoint's own explicit ``executor`` field
    when present -- an explicit signal on the source record itself, not an
    inference from Shard identity.
    """
    if not isinstance(checkpoints, list):
        return []
    _status_map = {
        "passed": STATUS_PASSED,
        "failed": STATUS_FAILED,
        "skipped": STATUS_SKIPPED,
        "started": STATUS_STARTED,
    }
    events: list[Event] = []
    for cp in checkpoints:
        if not isinstance(cp, NativeRunCheckpointEvent):
            continue
        try:
            event_type = _checkpoint_event_type(cp)
            status = _status_map.get(cp.status, STATUS_UNKNOWN)
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_RUN_CHECKPOINTS,
                    action=cp.reason or cp.stage or "checkpoint",
                    event_id=cp.event_id or None,
                    occurred_at=cp.timestamp or None,
                    run_id=cp.run_id or None,
                    actor=cp.executor or None,
                    target=cp.stage or None,
                    status=status,
                    evidence=EVIDENCE_DIRECTLY_OBSERVED,
                )
            )
        except Exception:
            continue
    return events


def build_events_from_session_events(raw_events: object) -> list[Event]:
    """Project raw session_events.jsonl dicts into canonical Events."""
    if not isinstance(raw_events, list):
        return []
    events: list[Event] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        try:
            event_type_raw = raw.get("event_type") or ""
            event_type = EVENT_SESSION_STARTED if event_type_raw == "session_started" else EVENT_SESSION_ACTIVITY
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_SESSION_EVENTS,
                    action=raw.get("summary") or event_type_raw or "session activity",
                    event_id=raw.get("event_id") or None,
                    occurred_at=raw.get("timestamp") if isinstance(raw.get("timestamp"), str) else None,
                    run_id=raw.get("run_id") if isinstance(raw.get("run_id"), str) else None,
                    shard_id=raw.get("shard_id") if isinstance(raw.get("shard_id"), str) else None,
                    target=raw.get("command") if isinstance(raw.get("command"), str) else None,
                    status=STATUS_UNKNOWN,
                    evidence=EVIDENCE_DIRECTLY_OBSERVED,
                    metadata=raw.get("metadata"),
                )
            )
        except Exception:
            continue
    return events


def _interaction_event_type(event_type: str) -> str:
    if event_type in ("accepted", "feedback_accepted"):
        return EVENT_INTERACTION_ACCEPTED
    if event_type in ("rejected", "feedback_rejected"):
        return EVENT_INTERACTION_REJECTED
    if event_type in ("retried", "feedback_retried"):
        return EVENT_RETRY_STARTED
    if event_type in ("edited", "manual_edit"):
        return EVENT_INTERACTION_EDITED
    if event_type in (
        "wrong_file",
        "wrong_scope",
        "failed_tests",
        "bad_style",
        "missed_requirement",
        "too_expensive",
        "too_slow",
        "unsafe_command",
        "unclear_output",
        "feedback_partial",
        "feedback_abandoned",
        "feedback_noted",
    ):
        return EVENT_INTERACTION_FLAGGED
    return EVENT_UNKNOWN


def build_events_from_interactions(interactions: object) -> list[Event]:
    """Project DeveloperInteractionEvent records into canonical Events.

    ``actor`` is passed through unchanged from the source record (already
    explicit there, e.g. "developer") -- never re-derived.
    """
    if not isinstance(interactions, list):
        return []
    events: list[Event] = []
    for it in interactions:
        if not isinstance(it, DeveloperInteractionEvent):
            continue
        try:
            event_type = _interaction_event_type(it.event_type)
            if it.accepted is True:
                status = STATUS_PASSED
            elif it.accepted is False:
                status = STATUS_FAILED
            else:
                status = STATUS_UNKNOWN
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_INTERACTIONS,
                    action=it.summary or it.event_type,
                    event_id=it.event_id or None,
                    occurred_at=it.timestamp or None,
                    run_id=it.run_id or None,
                    actor=it.actor or None,
                    status=status,
                    evidence=EVIDENCE_DIRECTLY_OBSERVED,
                    metadata=it.metadata,
                )
            )
        except Exception:
            continue
    return events


def build_events_from_review_checks(
    checks: object,
    *,
    run_id: str | None = None,
    shard_id: str | None = None,
    attempt_number: int | None = None,
    run_ref: str = "unknown-run",
) -> list[Event]:
    """Project review-check result dicts into canonical Events.

    A check that actually ran and returned passed/failed is independent
    verification by definition (an external tool -- terraform/tflint --
    produced the result), so ``evidence`` is EVIDENCE_INDEPENDENTLY_VERIFIED
    for those two outcomes, not merely "observed".
    """
    if not isinstance(checks, list):
        return []
    _status_map = {"passed": STATUS_PASSED, "failed": STATUS_FAILED, "skipped": STATUS_SKIPPED}
    events: list[Event] = []
    for i, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        try:
            name = str(check.get("name") or "unknown_check")
            raw_status = str(check.get("status") or "")
            status = _status_map.get(raw_status, STATUS_UNKNOWN)
            if status in (STATUS_PASSED, STATUS_FAILED):
                evidence = EVIDENCE_INDEPENDENTLY_VERIFIED
            else:
                evidence = EVIDENCE_UNKNOWN
            event_type = {
                STATUS_PASSED: EVENT_VERIFICATION_PASSED,
                STATUS_FAILED: EVENT_VERIFICATION_FAILED,
                STATUS_SKIPPED: EVENT_VERIFICATION_SKIPPED,
            }.get(status, EVENT_UNKNOWN)
            summary = check.get("summary") or check.get("reason") or name
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_REVIEW_CHECKS,
                    action=str(summary),
                    event_id=_stable_event_id(SOURCE_REVIEW_CHECKS, name, i, run_ref),
                    run_id=run_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    target=name,
                    status=status,
                    evidence=evidence,
                )
            )
        except Exception:
            continue
    return events


def build_events_from_policy_decisions(
    decisions: object,
    *,
    run_id: str | None = None,
    shard_id: str | None = None,
    attempt_number: int | None = None,
    run_ref: str = "unknown-run",
) -> list[Event]:
    """Project policy-decision dicts into canonical Events.

    ``resource`` is deliberately never read (may be a file path), matching
    ``provenance.build_provenance_from_policy_decisions``. ``actor`` is
    intentionally left None: a decision's own ``source`` field names a
    policy subsystem ("approval_gate", "path_policy", ...), not a person or
    agent, so it is not conflated with actor identity.
    """
    if not isinstance(decisions, list):
        return []
    _decision_status_map = {
        "allow": STATUS_PASSED,
        "deny": STATUS_FAILED,
        "ask": STATUS_WARNING,
        "not_applicable": STATUS_SKIPPED,
    }
    events: list[Event] = []
    for i, d in enumerate(decisions):
        if not isinstance(d, dict):
            continue
        try:
            action = d.get("action")
            decision_val = d.get("decision")
            if not isinstance(action, str) or not action:
                continue
            if not isinstance(decision_val, str) or not decision_val:
                continue

            approval_required = bool(d.get("approval_required"))
            approval_granted = d.get("approval_granted")
            if approval_required:
                if approval_granted is True:
                    event_type, status = EVENT_APPROVAL_GRANTED, STATUS_PASSED
                elif approval_granted is False:
                    event_type, status = EVENT_APPROVAL_DENIED, STATUS_FAILED
                else:
                    event_type, status = EVENT_APPROVAL_REQUESTED, STATUS_STARTED
            else:
                event_type = EVENT_POLICY_CHECKED
                status = _decision_status_map.get(decision_val, STATUS_UNKNOWN)

            decision_id = d.get("decision_id")
            event_id = (
                decision_id
                if isinstance(decision_id, str) and decision_id
                else _stable_event_id(SOURCE_POLICY_DECISIONS, action, i, run_ref)
            )
            reason = d.get("reason")
            events.append(
                make_event(
                    event_type=event_type,
                    source=SOURCE_POLICY_DECISIONS,
                    action=f"{action} {decision_val}",
                    event_id=event_id,
                    run_id=run_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    status=status,
                    evidence=EVIDENCE_DIRECTLY_OBSERVED,
                    metadata={"reason": reason} if isinstance(reason, str) else None,
                )
            )
        except Exception:
            continue
    return events


def build_event_from_adapter_entry(
    entry: dict,
    *,
    run_id: str | None = None,
    shard_id: str | None = None,
    attempt_number: int | None = None,
    run_ref: str = "unknown-run",
) -> Event | None:
    """Project one external-adapter run entry (Claude Code import/wrap) into a
    canonical Event. Returns None for any entry that is not adapter-sourced.

    Legacy-projection path only (Migration 3) -- ``events_from_entry`` calls
    this solely for adapter entries with no ``"events"`` key of their own.
    Since Migration 5, ``adapters/claude_code_import.py`` and
    ``adapters/wrap_exec.py`` build their own Events at observation time and
    embed them directly on the entry; this function exists purely for
    entries written before that (or by any future adapter that hasn't
    adopted embedding yet) and is never called once an entry embeds
    ``"events"``, to avoid computing the same fact twice.

    ``actor`` is read directly from the entry's own explicit
    ``import_source`` field (e.g. "claude_code") when present -- an explicit
    signal on the record itself, never inferred via origin-classification
    heuristics. ``status`` is only ever passed/failed when the entry itself
    says verification was attempted; otherwise it stays unknown, never
    fabricated as passing.
    """
    executor = entry.get("executor")
    if executor not in (SOURCE_CLAUDE_CODE_IMPORT, SOURCE_CLAUDE_CODE_WRAP):
        return None
    try:
        source = SOURCE_CLAUDE_CODE_IMPORT if executor == SOURCE_CLAUDE_CODE_IMPORT else SOURCE_CLAUDE_CODE_WRAP
        files_source = entry.get("files_source")
        evidence = EVIDENCE_GIT_OBSERVED if files_source == "git_diff_inferred" else EVIDENCE_AGENT_REPORTED

        verification_attempted = bool(entry.get("verification_attempted"))
        verification_passed = entry.get("verification_passed")
        if verification_attempted and verification_passed is True:
            status = STATUS_PASSED
        elif verification_attempted and verification_passed is False:
            status = STATUS_FAILED
        else:
            status = STATUS_UNKNOWN

        actor = entry.get("import_source") if isinstance(entry.get("import_source"), str) else None
        summary = entry.get("summary") or entry.get("import_note") or "external run observed"

        return make_event(
            event_type=EVENT_RUN_COMPLETED,
            source=source,
            action=str(summary),
            event_id=_stable_event_id(source, str(executor), 0, run_ref),
            occurred_at=entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None,
            run_id=run_id,
            shard_id=shard_id,
            attempt_number=attempt_number,
            actor=actor,
            status=status,
            evidence=evidence,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Aggregators
# ---------------------------------------------------------------------------


def _embedded_events_from_entry(raw: object) -> list[Event]:
    """Deserialize Events a modern producer already embedded on its own entry.

    Caller (``events_from_entry``) checks ``"events" in entry`` first; this
    function only decides how to interpret the *value*. Fails closed rather
    than falling back to legacy projection: a non-list value yields ``[]``,
    and a non-dict item is dropped rather than repaired or guessed at. Each
    dict item still goes through ``Event.from_dict``'s own coercion (unknown
    enum values fall back to their ``*_unknown`` sentinel), so a partially
    malformed dict degrades to an honestly-unknown Event instead of being
    silently dropped.
    """
    if not isinstance(raw, list):
        return []
    events: list[Event] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            events.append(Event.from_dict(item))
        except Exception:
            continue
    return events


def events_from_entry(entry: object) -> list[Event]:
    """Derive all canonical Events embedded in one raw run-history entry.

    Mirrors ``provenance.build_provenance_from_entry``: dict-type-checks the
    input, never raises, returns [] for anything that doesn't fit.

    Presence of the ``"events"`` key (not whether its value is a non-empty
    list) decides the path: a key that is *absent* falls through to legacy
    projection (below); a key that is *present* means this entry's producer
    already built its own canonical Events at observation time (Migration 5)
    and owns them exclusively -- ``_embedded_events_from_entry`` is used and
    nothing else in this function runs, so the same underlying fact is never
    derived twice.
    """
    if not isinstance(entry, dict):
        return []
    try:
        if "events" in entry:
            return _embedded_events_from_entry(entry.get("events"))

        run_ref = entry.get("shard_id") or entry.get("timestamp") or "unknown-run"
        if not isinstance(run_ref, str) or not run_ref.strip():
            run_ref = "unknown-run"

        run_id = entry.get("run_id") if isinstance(entry.get("run_id"), str) else None
        shard_id = entry.get("shard_id") if isinstance(entry.get("shard_id"), str) else None
        raw_attempt = entry.get("attempt_number")
        attempt_number = raw_attempt if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool) else None

        # derive_shard_identity is only needed to source legacy timeline
        # evidence (via origin/capture_depth) below -- its returned ``agent``
        # value is deliberately discarded, never used for ``actor``.
        _, origin, capture_depth = derive_shard_identity(entry)

        events: list[Event] = []

        adapter_event = build_event_from_adapter_entry(
            entry, run_id=run_id, shard_id=shard_id, attempt_number=attempt_number, run_ref=run_ref
        )
        if adapter_event is not None:
            events.append(adapter_event)

        raw_timeline = entry.get("run_timeline")
        if isinstance(raw_timeline, list):
            events.extend(
                build_events_from_timeline(
                    raw_timeline,
                    run_id=run_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    entry_origin=origin,
                    entry_capture_depth=capture_depth,
                    run_ref=run_ref,
                )
            )

        raw_checks = entry.get("review_checks")
        if isinstance(raw_checks, list):
            events.extend(
                build_events_from_review_checks(
                    raw_checks, run_id=run_id, shard_id=shard_id, attempt_number=attempt_number, run_ref=run_ref
                )
            )

        raw_decisions = entry.get("policy_decisions")
        if isinstance(raw_decisions, list):
            events.extend(
                build_events_from_policy_decisions(
                    raw_decisions, run_id=run_id, shard_id=shard_id, attempt_number=attempt_number, run_ref=run_ref
                )
            )

        return events
    except Exception:
        return []


def _load_session_events_for_run(run_id: str) -> list[dict]:
    """Read session_events.jsonl and return dicts matching run_id.

    Replicates the same read-and-skip-malformed-lines pattern already used
    in ``session_signals.run_inference`` -- there is no dataclass loader for
    session events today.
    """
    path = Path.cwd() / ".openshard" / "session_events.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if isinstance(d, dict) and d.get("run_id") == run_id:
                out.append(d)
    except (OSError, json.JSONDecodeError):
        return out
    return out


def events_for_run(run_id: str, *, entry: dict | None = None) -> list[Event]:
    """Convenience aggregator: one canonical read-model across every existing
    producer for a single run_id.

    Not for hot paths (TUI/CLI render loops) -- reads up to five files with
    no caching. Pass the matching ``runs.jsonl`` entry via ``entry`` to also
    include timeline/review-check/policy-decision events; without it, only
    the four standalone per-run stores are covered.
    """
    if not isinstance(run_id, str) or not run_id:
        return []
    events: list[Event] = []
    _owns_embedded_events = isinstance(entry, dict) and "events" in entry
    if entry is not None:
        events.extend(events_from_entry(entry))
    # A producer that already embeds its own Events (Migration 5/6) owns the
    # facts native_steps/checkpoints would otherwise be projected from --
    # extending with those legacy projectors here would double-count them.
    if not _owns_embedded_events:
        try:
            events.extend(build_events_from_native_steps(native_step_events_for_run(run_id)))
        except Exception:
            pass
        try:
            events.extend(build_events_from_checkpoints(run_checkpoints_for_run(run_id)))
        except Exception:
            pass
    try:
        events.extend(build_events_from_interactions(interaction_events_for_run(run_id)))
    except Exception:
        pass
    try:
        events.extend(build_events_from_session_events(_load_session_events_for_run(run_id)))
    except Exception:
        pass
    return events
