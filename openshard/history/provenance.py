"""Provenance v1 — safe, derived provenance records for Shard proof claims.

Provenance records are derived at read-time from existing stored fields
(evidence_capsules, review_checks, run_timeline, policy_decisions).
They are never persisted to JSONL.
Old records without those fields produce an empty list safely.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from openshard.safety.sanitize import sanitize_metadata, sanitize_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SOURCE_TYPES: frozenset[str] = frozenset({
    "verification",  # review checks run during the verification phase
    "ci_check",      # reserved for openshard ci check (policy_check.py) outputs
    "policy",        # policy gate decisions
    "repo_map",      # repo structure / context map
    "timeline",      # timeline stage events
    "evidence",      # evidence capsules
    "check",         # general check type for future use
    "unknown",       # unrecognized or missing
})

VALID_STATUSES: frozenset[str] = frozenset({
    "passed",
    "failed",
    "skipped",
    "warning",
    "unknown",
})

_TEXT_LIMIT = 200  # chars for claim / safe_summary / source_name / related fields
_SOURCE_NAME_FALLBACK = "unknown"
_CLAIM_FALLBACK = ""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ProvenanceRecord:
    provenance_id: str
    source_type: str
    source_name: str
    claim: str
    status: str
    produced_at: str | None = None
    related_stage: str | None = None
    related_check: str | None = None
    related_event_id: str | None = None
    safe_summary: str | None = None
    metadata: dict = field(default_factory=dict)
    raw_content_stored: bool = False

    def __post_init__(self) -> None:
        self.raw_content_stored = False


# ---------------------------------------------------------------------------
# ID helper
# ---------------------------------------------------------------------------

def _stable_provenance_id(
    source_type: str,
    source_name: str,
    index: int,
    run_ref: str = "unknown-run",
    related_check: str | None = None,
    related_event_id: str | None = None,
) -> str:
    """Return a deterministic, run-scoped provenance ID.

    Same inputs always produce the same ID; changing run_ref produces a
    different ID for the same proof source so IDs are unique across runs.
    """
    key = (
        f"{run_ref}:{source_type}:{source_name}"
        f":{related_check or ''}:{related_event_id or ''}:{index}"
    )
    return "prov-" + hashlib.sha256(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Core constructor
# ---------------------------------------------------------------------------

def make_provenance_record(
    *,
    source_type: str,
    source_name: str,
    claim: str,
    status: str,
    provenance_id: str | None = None,
    run_ref: str = "unknown-run",
    index: int = 0,
    produced_at: str | None = None,
    related_stage: str | None = None,
    related_check: str | None = None,
    related_event_id: str | None = None,
    safe_summary: object = None,
    metadata: object = None,
) -> ProvenanceRecord:
    """Safe constructor for provenance records.

    Coerces unrecognized source_type / status to sentinel values, sanitizes
    all string fields, and generates a deterministic ID when none is provided.
    Never raises.
    """
    # source_type
    if source_type not in VALID_SOURCE_TYPES:
        source_type = "unknown"

    # status
    if status not in VALID_STATUSES:
        status = "unknown"

    # string fields — sanitize; fall back to safe defaults on None
    clean_source_name = sanitize_text(source_name, _TEXT_LIMIT) or _SOURCE_NAME_FALLBACK
    clean_claim = sanitize_text(claim, _TEXT_LIMIT)
    if clean_claim is None:
        clean_claim = _CLAIM_FALLBACK
    clean_summary = sanitize_text(safe_summary, _TEXT_LIMIT) if safe_summary is not None else None
    clean_related_check = sanitize_text(related_check, _TEXT_LIMIT) if related_check else None
    clean_related_event_id = sanitize_text(related_event_id, _TEXT_LIMIT) if related_event_id else None
    clean_produced_at = sanitize_text(produced_at, 40) if produced_at else None
    clean_related_stage = sanitize_text(related_stage, 40) if related_stage else None

    # metadata
    safe_meta = sanitize_metadata(metadata) if metadata else {}

    # provenance ID
    pid = provenance_id or _stable_provenance_id(
        source_type,
        clean_source_name,
        index,
        run_ref=run_ref,
        related_check=clean_related_check,
        related_event_id=clean_related_event_id,
    )

    return ProvenanceRecord(
        provenance_id=pid,
        source_type=source_type,
        source_name=clean_source_name,
        claim=clean_claim,
        status=status,
        produced_at=clean_produced_at,
        related_stage=clean_related_stage,
        related_check=clean_related_check,
        related_event_id=clean_related_event_id,
        safe_summary=clean_summary,
        metadata=safe_meta,
        raw_content_stored=False,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_provenance_from_evidence_capsules(
    capsules: list[Any],
    run_ref: str = "unknown-run",
) -> list[ProvenanceRecord]:
    """Derive provenance records from a list of EvidenceCapsule objects.

    Returns an empty list for None, non-list, or empty input.
    """
    if not isinstance(capsules, list):
        return []
    records: list[ProvenanceRecord] = []
    for i, capsule in enumerate(capsules):
        try:
            # Accept both EvidenceCapsule dataclass instances and plain dicts.
            if hasattr(capsule, "capsule_id"):
                capsule_id = capsule.capsule_id
                kind = capsule.kind or "evidence"
                summary = capsule.summary or ""
                source = capsule.source
                severity = capsule.severity
            elif isinstance(capsule, dict):
                capsule_id = capsule.get("capsule_id") or ""
                kind = capsule.get("kind") or "evidence"
                summary = capsule.get("summary") or ""
                source = capsule.get("source")
                severity = capsule.get("severity")
            else:
                continue

            source_name = source or kind or "evidence"
            status = "warning" if severity else "passed"

            records.append(make_provenance_record(
                source_type="evidence",
                source_name=source_name,
                claim=summary,
                status=status,
                run_ref=run_ref,
                index=i,
                related_event_id=capsule_id or None,
                safe_summary=summary,
                metadata={"kind": kind, "severity": severity} if severity else {"kind": kind},
            ))
        except Exception:
            continue
    return records


def build_provenance_from_review_checks(
    review_checks_raw: list[Any],
    run_ref: str = "unknown-run",
) -> list[ProvenanceRecord]:
    """Derive provenance records from raw review check dicts.

    Each dict is expected to have: name, status, summary, reason, skip_reason.
    Non-dict items are silently skipped.  Returns [] for non-list input.
    """
    if not isinstance(review_checks_raw, list):
        return []
    records: list[ProvenanceRecord] = []
    for i, check in enumerate(review_checks_raw):
        if not isinstance(check, dict):
            continue
        try:
            name = str(check.get("name") or "unknown_check")
            raw_status = str(check.get("status") or "")
            summary = str(check.get("summary") or "")

            # Normalize check status to VALID_STATUSES
            status = raw_status if raw_status in VALID_STATUSES else "unknown"

            claim = summary or f"{name} {status}"

            records.append(make_provenance_record(
                source_type="verification",
                source_name=name,
                claim=claim,
                status=status,
                run_ref=run_ref,
                index=i,
                related_stage="verify",
                related_check=name,
                safe_summary=summary or None,
            ))
        except Exception:
            continue
    return records


def build_provenance_from_timeline_events(
    timeline: list[Any],
    run_ref: str = "unknown-run",
) -> list[ProvenanceRecord]:
    """Derive provenance records from run_timeline event dicts.

    Each event must have an ``event`` machine key (used as source_name and
    related_event_id).  ``label`` is optional; claim falls back to the event
    key when label is absent.  Returns [] for non-list input.  Never raises.
    """
    if not isinstance(timeline, list):
        return []
    _status_map = {
        "completed": "passed",
        "failed": "failed",
        "skipped": "skipped",
        "warning": "warning",
    }
    records: list[ProvenanceRecord] = []
    for i, event in enumerate(timeline):
        if not isinstance(event, dict):
            continue
        try:
            event_key = event.get("event")
            if not event_key or not isinstance(event_key, str):
                continue

            label = event.get("label")
            claim = label if isinstance(label, str) and label else event_key

            raw_status = event.get("status") or ""
            status = _status_map.get(raw_status, "unknown")

            kind = event.get("kind")
            detail = event.get("detail")
            target = event.get("target")
            count = event.get("count")

            metadata: dict = {}
            if target is not None:
                metadata["target"] = target
            if count is not None:
                metadata["count"] = count

            records.append(make_provenance_record(
                source_type="timeline",
                source_name=event_key,
                claim=claim,
                status=status,
                run_ref=run_ref,
                index=i,
                related_stage=str(kind) if kind else None,
                related_event_id=event_key,
                safe_summary=detail if isinstance(detail, str) else None,
                metadata=metadata or None,
            ))
        except Exception:
            continue
    return records


def build_provenance_from_policy_decisions(
    decisions: list[Any],
    run_ref: str = "unknown-run",
) -> list[ProvenanceRecord]:
    """Derive provenance records from policy_decisions dicts.

    Requires ``action`` and ``decision`` fields.  ``decision_id`` is optional;
    when absent related_event_id is None but the record is still produced.
    ``resource`` is intentionally never read (may be a file path).
    Returns [] for non-list input.  Never raises.
    """
    if not isinstance(decisions, list):
        return []
    _status_map = {
        "allow": "passed",
        "deny": "failed",
        "ask": "warning",
        "not_applicable": "skipped",
    }
    records: list[ProvenanceRecord] = []
    for i, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            continue
        try:
            action = decision.get("action")
            decision_val = decision.get("decision")
            if not action or not isinstance(action, str):
                continue
            if not decision_val or not isinstance(decision_val, str):
                continue

            decision_id = decision.get("decision_id")
            # decision_id is a UUID (36-char alphanumeric+hyphen); it matches
            # the 32+ char opaque-blob pattern in sanitize_text and will be
            # sanitized to None.  That is intentional — opaque long strings
            # should not survive the secret filter.
            related_event_id = decision_id if isinstance(decision_id, str) and decision_id else None

            status = _status_map.get(decision_val, "unknown")
            claim = f"{action} {decision_val}"

            reason = decision.get("reason")
            source = decision.get("source")
            severity = decision.get("severity")
            # "resource" deliberately not accessed — may be a file path

            metadata: dict = {}
            if source is not None:
                metadata["source"] = source
            if severity is not None:
                metadata["severity"] = severity

            records.append(make_provenance_record(
                source_type="policy",
                source_name=action,
                claim=claim,
                status=status,
                run_ref=run_ref,
                index=i,
                related_event_id=related_event_id,
                safe_summary=reason if isinstance(reason, str) else None,
                metadata=metadata or None,
            ))
        except Exception:
            continue
    return records


def build_provenance_from_entry(entry: object) -> list[ProvenanceRecord]:
    """Top-level builder: derive all provenance from a run entry dict.

    Returns [] for non-dict input, missing fields, or any error.
    Never raises.
    """
    if not isinstance(entry, dict):
        return []
    try:
        from openshard.history.shard import control_evidence_view

        entry = control_evidence_view(entry)
        run_ref: str = (
            entry.get("shard_id")
            or entry.get("timestamp")
            or "unknown-run"
        )
        if not isinstance(run_ref, str) or not run_ref.strip():
            run_ref = "unknown-run"

        records: list[ProvenanceRecord] = []

        # Evidence capsules
        raw_capsules = entry.get("evidence_capsules")
        if isinstance(raw_capsules, list):
            records.extend(build_provenance_from_evidence_capsules(raw_capsules, run_ref=run_ref))

        # Review checks
        raw_checks = entry.get("review_checks")
        if isinstance(raw_checks, list):
            records.extend(build_provenance_from_review_checks(raw_checks, run_ref=run_ref))

        # Timeline events
        raw_timeline = entry.get("run_timeline")
        if isinstance(raw_timeline, list):
            records.extend(build_provenance_from_timeline_events(raw_timeline, run_ref=run_ref))

        # Policy decisions
        raw_decisions = entry.get("policy_decisions")
        if isinstance(raw_decisions, list):
            records.extend(build_provenance_from_policy_decisions(raw_decisions, run_ref=run_ref))

        return records
    except Exception:
        return []
