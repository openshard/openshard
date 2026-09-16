from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from openshard.history.jsonl_store import append_jsonl
from openshard.safety.sanitize import (
    is_absolute_path as _is_absolute_path,
)
from openshard.safety.sanitize import (
    sanitize_metadata as _sanitize_metadata,
)
from openshard.safety.sanitize import (
    sanitize_text as _sanitize_text,
)

_INTERACTIONS_PATH = Path(".openshard") / "interactions.jsonl"

# Caps for sanitised free-text fields.
_MAX_SUMMARY_CHARS = 120
_MAX_REASON_CHARS = 80
_MAX_PATH_CHARS = 120
_MAX_FILE_PATHS = 20

# Canonical interaction vocabulary (the flat v1 enum)...
_CANONICAL_EVENT_TYPES = frozenset(
    {
        "accepted",
        "rejected",
        "edited",
        "retried",
        "manual_edit",
        "wrong_file",
        "wrong_scope",
        "failed_tests",
        "bad_style",
        "missed_requirement",
        "too_expensive",
        "too_slow",
        "unsafe_command",
        "unclear_output",
    }
)
# ...plus the namespaced types emitted by the existing feedback integration.
_LEGACY_EVENT_TYPES = frozenset(
    {
        "feedback_accepted",
        "feedback_rejected",
        "feedback_partial",
        "feedback_abandoned",
        "feedback_retried",
        "feedback_noted",
    }
)
ALLOWED_EVENT_TYPES = _CANONICAL_EVENT_TYPES | _LEGACY_EVENT_TYPES
_EVENT_TYPE_FALLBACK = "unclear_output"

ALLOWED_SEVERITIES = frozenset({"info", "low", "medium", "high"})
_SEVERITY_FALLBACK = "info"


def _sanitize_file_paths(paths) -> list[str]:
    """Keep only relative, secret-free, capped file paths. Absolute paths are dropped."""
    if not isinstance(paths, list):
        return []
    safe: list[str] = []
    for p in paths:
        if len(safe) >= _MAX_FILE_PATHS:
            break
        if not isinstance(p, str) or _is_absolute_path(p):
            continue
        clean = _sanitize_text(p, _MAX_PATH_CHARS)
        if clean is not None:
            safe.append(clean)
    return safe


def sanitize_event(event: DeveloperInteractionEvent) -> DeveloperInteractionEvent:
    """Return a privacy-safe copy of ``event``.

    Caps + scrubs free text (drops absolute paths/secrets), keeps only relative file
    paths, reduces metadata to small scalars, validates event_type/severity against the
    allowed enums (falling back safely), and forces raw_content_stored False. Applied at
    both write time and load time so legacy on-disk entries are sanitised before display
    or export.
    """
    event_type = event.event_type if event.event_type in ALLOWED_EVENT_TYPES else _EVENT_TYPE_FALLBACK
    severity = event.severity if event.severity in ALLOWED_SEVERITIES else _SEVERITY_FALLBACK
    return DeveloperInteractionEvent(
        schema_version=event.schema_version,
        event_id=event.event_id,
        run_id=event.run_id,
        timestamp=event.timestamp,
        actor=event.actor,
        event_type=event_type,
        summary=_sanitize_text(event.summary, _MAX_SUMMARY_CHARS) or "",
        related_stage=event.related_stage,
        related_file_paths=_sanitize_file_paths(event.related_file_paths),
        correction_reason=_sanitize_text(event.correction_reason, _MAX_REASON_CHARS),
        severity=severity,
        accepted=event.accepted,
        metadata=_sanitize_metadata(event.metadata),
        raw_content_stored=False,
    )


@dataclass
class DeveloperInteractionEvent:
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.datetime.now(
            datetime.UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    actor: str = "developer"
    event_type: str = ""
    summary: str = ""
    related_stage: str = ""
    related_file_paths: list[str] = field(default_factory=list)
    correction_reason: str | None = None
    severity: str = "info"
    accepted: bool | None = None
    metadata: dict = field(default_factory=dict)
    raw_content_stored: bool = False

    def __post_init__(self) -> None:
        self.raw_content_stored = False


def _event_to_dict(event: DeveloperInteractionEvent) -> dict:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "timestamp": event.timestamp,
        "actor": event.actor,
        "event_type": event.event_type,
        "summary": event.summary,
        "related_stage": event.related_stage,
        "related_file_paths": event.related_file_paths,
        "correction_reason": event.correction_reason,
        "severity": event.severity,
        "accepted": event.accepted,
        "metadata": event.metadata,
        "raw_content_stored": False,
    }


def _dict_to_event(d: dict) -> DeveloperInteractionEvent:
    return DeveloperInteractionEvent(
        schema_version=d.get("schema_version", 1),
        event_id=d.get("event_id", ""),
        run_id=d.get("run_id", ""),
        timestamp=d.get("timestamp", ""),
        actor=d.get("actor", "developer"),
        event_type=d.get("event_type", ""),
        summary=d.get("summary", ""),
        related_stage=d.get("related_stage", ""),
        related_file_paths=d.get("related_file_paths") or [],
        correction_reason=d.get("correction_reason"),
        severity=d.get("severity", "info"),
        accepted=d.get("accepted"),
        metadata=d.get("metadata") or {},
        raw_content_stored=False,
    )


def log_interaction_event(event: DeveloperInteractionEvent, cwd: Path | None = None) -> None:
    interactions_path = (cwd if cwd is not None else Path.cwd()) / _INTERACTIONS_PATH
    d = _event_to_dict(sanitize_event(event))
    d["raw_content_stored"] = False
    append_jsonl(interactions_path, d)


def load_interaction_events() -> list[DeveloperInteractionEvent]:
    interactions_path = Path.cwd() / _INTERACTIONS_PATH
    if not interactions_path.exists():
        return []
    events: list[DeveloperInteractionEvent] = []
    for line in interactions_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(sanitize_event(_dict_to_event(json.loads(line))))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return events


def interaction_events_for_run(run_id: str) -> list[DeveloperInteractionEvent]:
    return [e for e in load_interaction_events() if e.run_id == run_id]
