"""ReceiptBuilder: HistoricalSession -> a sealed, append-only run record.

Reuses the existing record shape (so ``build_shard_receipt``, ``views.py``,
MCP and the CLI render it unchanged) and adds two blocks: ``import``
(where it came from, how it was grouped, what it supersedes) and ``facts``
(every recovered fact with its evidence and source reference).

Write path: ``coerce_shard_entry`` (blocked fields stripped, content hash
stamped) -> ``receipt_id`` -> embedded Events -> ``sealed_at`` -> append.
``upsert_jsonl`` is never called; a written receipt is never touched again.
Before anything is written the record is checked to contain no
``directly_observed`` / ``openshard_executed`` evidence anywhere; a record
that would is refused rather than written.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from openshard.history.event import (
    EVENT_FILE_CHANGED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_INVOKED,
    EVIDENCE_AGENT_REPORTED,
    EVIDENCE_GIT_VERIFIED,
    EVIDENCE_IMPORTED_TRANSCRIPT,
    EVIDENCE_UNKNOWN,
    SOURCE_CLAUDE_CODE_HISTORY,
    SOURCE_CODEX_HISTORY,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    make_event,
)
from openshard.history.shard import ORIGIN_HISTORICAL_IMPORT
from openshard.ingest.model import (
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    HistoricalSession,
    SourceObject,
)
from openshard.ingest.shard_builder import grouping_block

IMPORTER_VERSION = "historical-ingestion@1"
CONNECTOR_LABEL = {"claude_code": "Claude Code", "codex": "Codex"}
EVENT_SOURCE = {"claude_code": SOURCE_CLAUDE_CODE_HISTORY, "codex": SOURCE_CODEX_HISTORY}
FORBIDDEN_EVIDENCE = frozenset({"directly_observed", "openshard_executed"})
MAX_CHECKS = 20


class ForbiddenEvidenceError(ValueError):
    """A historical record would have claimed live observation. Never written."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def locator_hash(locator: str) -> str:
    return "sha256:" + hashlib.sha256(locator.encode("utf-8", errors="replace")).hexdigest()


def object_key(object_id: str) -> str:
    return hashlib.sha256(object_id.encode("utf-8", errors="replace")).hexdigest()[:24]


def locator_display(obj: SourceObject) -> str:
    """Local-only, path-free display: ``<source>:<file name>``."""
    source = obj.object_id.split(":", 1)[0]
    name = obj.locator.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{source}:{name}"


def import_block(hs: HistoricalSession, obj: SourceObject, *, job_id: str | None,
                 supersedes: str | None = None) -> dict[str, Any]:
    return {
        "import_key": hs.import_key,
        "source_sha256": hs.source_sha256,
        "source": {
            "connector": obj.connector,
            "parser": hs.parser,
            "locator_hash": locator_hash(obj.locator),
            "locator_display": locator_display(obj),
        },
        "job_id": job_id,
        "imported_at": _now(),
        "importer_version": IMPORTER_VERSION,
        "session_window": hs.value("window"),
        "grouping": grouping_block(),
        "supersedes": supersedes,
        "source_losses": dict(hs.losses),
        "dropped": dict(hs.dropped),
    }


def facts_block(hs: HistoricalSession) -> dict[str, Any]:
    facts = {name: fact.to_dict() for name, fact in hs.facts.items()}
    if hs.commits:
        order = [EVIDENCE_GIT_VERIFIED, "git_observed", EVIDENCE_AGENT_REPORTED]
        strongest = next(e for e in order if any(c.evidence == e for c in hs.commits))
        facts["commits"] = {
            "value": [{"sha": c.sha, "evidence": c.evidence, "ref": c.ref, "reason": c.reason} for c in hs.commits],
            "evidence": strongest,
            "ref": "git",
        }
    else:
        facts["commits"] = {"value": None, "evidence": EVIDENCE_UNKNOWN, "ref": None}
    checks = [c for c in hs.commands if c.kind in ("test", "lint")]
    if checks:
        outcomes = {
            "attempted": len(checks),
            "passed": sum(1 for c in checks if c.outcome == OUTCOME_PASSED),
            "failed": sum(1 for c in checks if c.outcome == OUTCOME_FAILED),
        }
        outcomes["unknown"] = outcomes["attempted"] - outcomes["passed"] - outcomes["failed"]
        reported = outcomes["passed"] or outcomes["failed"]
        facts["test_runs"] = {"value": outcomes,
                              "evidence": EVIDENCE_AGENT_REPORTED if reported else EVIDENCE_IMPORTED_TRANSCRIPT,
                              "ref": checks[0].ref}
    else:
        facts["test_runs"] = {"value": None, "evidence": EVIDENCE_UNKNOWN, "ref": None}
    if hs.file_edits:
        facts["files_changed"] = {
            "value": len(hs.file_edits),
            "evidence": EVIDENCE_GIT_VERIFIED if all(e.evidence == EVIDENCE_GIT_VERIFIED for e in hs.file_edits)
            else EVIDENCE_IMPORTED_TRANSCRIPT,
            "ref": hs.file_edits[0].ref,
        }
    else:
        facts["files_changed"] = {"value": None, "evidence": EVIDENCE_UNKNOWN, "ref": None}
    return facts


def verification_block(hs: HistoricalSession) -> dict[str, Any]:
    from openshard.history.verification import (
        MODE_IMPORTED_TRANSCRIPT,
        REASON_CAPTURE_LOSS,
        REASON_OUTCOME_NOT_OBSERVED,
        SOURCE_AGENT_REPORTED,
        STATUS_NOT_RUN,
        STATUS_UNKNOWN,
        build_verification,
    )

    checks = [c for c in hs.commands if c.kind in ("test", "lint")][:MAX_CHECKS]
    if not checks:
        if hs.losses:
            return build_verification(
                source=None, observation_mode=MODE_IMPORTED_TRANSCRIPT, status=STATUS_UNKNOWN,
                reason="No check command found in the imported history, but some records could not be read.",
                incomplete_reasons=[REASON_CAPTURE_LOSS],
            )
        return build_verification(
            source=None, observation_mode=MODE_IMPORTED_TRANSCRIPT, status=STATUS_NOT_RUN, checks_attempted=0,
            reason="No check command recorded in the imported history.",
        )
    items = [{
        "name": c.action,
        "kind": c.kind,
        "status": c.outcome if c.outcome in (OUTCOME_PASSED, OUTCOME_FAILED) else "unknown",
        "exit_code": c.exit_code,
    } for c in checks]
    reported = any(i["status"] in ("passed", "failed") for i in items)
    incomplete = []
    if any(i["status"] == "unknown" for i in items):
        incomplete.append(REASON_OUTCOME_NOT_OBSERVED)
    if hs.losses:
        incomplete.append(REASON_CAPTURE_LOSS)
    starts = [c.at for c in checks if c.at]
    return build_verification(
        source=SOURCE_AGENT_REPORTED if reported else None,
        observation_mode=MODE_IMPORTED_TRANSCRIPT,
        checks=items,
        started_at=min(starts) if starts else None,
        reason=("Check outcomes as recorded in the agent's own history (agent-reported); "
                "OpenShard did not run these checks.") if reported
        else "Check command(s) recorded in the imported history; outcome not recorded.",
        incomplete_reasons=incomplete,
    )


def _events(hs: HistoricalSession, common: dict) -> list[dict]:
    source = EVENT_SOURCE.get(hs.agent, "historical_import")
    window = hs.value("window") or {}
    label = CONNECTOR_LABEL.get(hs.agent, hs.agent)
    events = [make_event(
        event_type=EVENT_RUN_STARTED, source=source, action=f"{label} session started (imported history)",
        occurred_at=window.get("start"), status=STATUS_UNKNOWN, evidence=EVIDENCE_IMPORTED_TRANSCRIPT,
        metadata={"ref": hs.fact("window").ref}, **common,
    )]
    for te in hs.tool_events:
        idx = te.get("command")
        cmd = hs.commands[idx] if isinstance(idx, int) and 0 <= idx < len(hs.commands) else None
        meta: dict[str, Any] = {"tool": te.get("tool"), "ref": te.get("ref")}
        status, evidence, action = STATUS_UNKNOWN, EVIDENCE_IMPORTED_TRANSCRIPT, f"{te.get('tool')} (imported)"
        if cmd is not None:
            action = cmd.action
            meta["command_kind"] = cmd.kind
            if cmd.outcome in (OUTCOME_PASSED, OUTCOME_FAILED):
                # The outcome is the agent host's own account of a command
                # OpenShard never ran (Verification v2 rule).
                status = STATUS_PASSED if cmd.outcome == OUTCOME_PASSED else STATUS_FAILED
                evidence = EVIDENCE_AGENT_REPORTED
                meta["outcome_ref"] = cmd.outcome_ref
            else:
                meta["outcome"] = cmd.outcome
        events.append(make_event(
            event_type=EVENT_TOOL_INVOKED, source=source, action=action, occurred_at=te.get("at"),
            status=status, evidence=evidence, metadata=meta, **common,
        ))
    for edit in hs.file_edits:
        events.append(make_event(
            event_type=EVENT_FILE_CHANGED, source=source, action=f"file {edit.change_type}",
            target=edit.path, target_is_path=True, status=STATUS_UNKNOWN, evidence=edit.evidence,
            metadata={"ref": edit.ref}, **common,
        ))
    events.append(make_event(
        event_type=EVENT_RUN_COMPLETED, source=source, action=f"{label} session activity ended (imported history)",
        occurred_at=window.get("end"), status=STATUS_UNKNOWN, evidence=EVIDENCE_IMPORTED_TRANSCRIPT,
        metadata={"ref": hs.fact("window").ref}, **common,
    ))
    return [e.to_dict() for e in events]


def _summary(hs: HistoricalSession) -> str:
    label = CONNECTOR_LABEL.get(hs.agent, hs.agent)
    tools = sum(hs.tool_counts.values())
    verified = sum(1 for c in hs.commits if c.evidence == EVIDENCE_GIT_VERIFIED)
    return (
        f"Reconstructed from {label} history. "
        f"{tools} tool call(s), {len(hs.commands)} command(s), {len(hs.file_edits)} file(s) edited, "
        f"{verified} git-verified commit(s). Outcomes are agent-reported; OpenShard did not observe this session live."
    )


def _completeness(hs: HistoricalSession) -> dict:
    from openshard.history.capture_completeness import (
        REASON_INTEGRATION_LIMITATION,
        REASON_UNPARSED_SOURCE_RECORDS,
        build_completeness,
        make_reason,
    )

    reasons = [make_reason(REASON_INTEGRATION_LIMITATION, 1,
                           "imported history does not record approvals or independent check results")]
    lost = sum(hs.losses.values())
    if lost:
        reasons.append(make_reason(REASON_UNPARSED_SOURCE_RECORDS, lost))
    return build_completeness(reasons)


def assert_no_live_evidence(obj: object, depth: int = 0) -> None:
    """Raise when any evidence/source/mode field claims live observation."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("evidence", "source", "observation_mode") and isinstance(v, str) and v in FORBIDDEN_EVIDENCE:
                raise ForbiddenEvidenceError(f"{k}={v}")
            assert_no_live_evidence(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            assert_no_live_evidence(v, depth + 1)


def build_entry(
    hs: HistoricalSession,
    obj: SourceObject,
    *,
    executor: str,
    job_id: str | None,
    run_index: int,
    supersedes: str | None = None,
) -> dict:
    """The sealed record for *hs*. Raises ``ForbiddenEvidenceError`` / ``ValueError``."""
    from openshard.adapters.claude_code_import import _sanitize_model
    from openshard.history.receipt_identity import ensure_receipt_id
    from openshard.history.shard_contract import _make_shard_id
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
    from openshard.history.task_title import derive_task_title

    window = hs.value("window") or {}
    start = window.get("start")
    if not start:
        raise ValueError("no_session_timestamp")
    label = CONNECTOR_LABEL.get(hs.agent, hs.agent)
    task = hs.value("task") or f"Imported {label} session with no recorded prompt"
    run_id = "hist-" + hashlib.sha256(f"{hs.import_key}:{hs.source_sha256}".encode()).hexdigest()[:20]
    tokens = hs.value("tokens") or {}
    files = [{
        "path": e.path,
        "change_type": e.change_type,
        "summary": "verified in a git commit" if e.evidence == EVIDENCE_GIT_VERIFIED else "recorded in imported history",
        "attribution": e.evidence,
    } for e in hs.file_edits]
    verified = [c.sha for c in hs.commits if c.evidence == EVIDENCE_GIT_VERIFIED]
    verification = verification_block(hs)
    checks = [c for c in hs.commands if c.kind in ("test", "lint")]

    entry: dict[str, Any] = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": start,
        "task": task,
        "task_title": derive_task_title(task),
        "execution_model": _sanitize_model(hs.value("model")),
        "executor": executor,
        "origin": ORIGIN_HISTORICAL_IMPORT,
        "import_source": hs.agent,
        "import_method": IMPORTER_VERSION,
        "files_source": "imported_transcript" if files else "not_available",
        "files_detail": files,
        "files_created": sum(1 for f in files if f["change_type"] == "create"),
        "files_updated": sum(1 for f in files if f["change_type"] == "update"),
        "files_deleted": sum(1 for f in files if f["change_type"] == "delete"),
        # Older readers: attempted is a fact from the transcript; passed is never inferred.
        "verification_attempted": bool(checks),
        "verification_passed": None,
        "verification": verification,
        "git_branch": hs.value("branch"),
        "git_base_commit_hash": hs.value("head_at_start"),
        "summary": _summary(hs),
        "capture": {"completeness": _completeness(hs)},
        "run_id": run_id,
        "attempt_number": 1,
    }
    if len(verified) == 1:
        entry["git_head_commit_hash"] = verified[0]
    repo = hs.value("repo")
    if isinstance(repo, str) and repo:
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD

        entry[REPO_IDENTITY_FIELD] = repo
    window_start, window_end = window.get("start"), window.get("end")
    if window_start and window_end:
        try:
            a = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
            b = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
            entry["duration_seconds"] = max(0.0, (b - a).total_seconds())
        except ValueError:
            pass
    if tokens:
        entry["tokens_provenance"] = EVIDENCE_IMPORTED_TRANSCRIPT
        for src, dst in (("input", "prompt_tokens"), ("output", "completion_tokens"),
                         ("cache_read", "cache_read_tokens"), ("cache_creation", "cache_creation_tokens")):
            if isinstance(tokens.get(src), int):
                entry[dst] = tokens[src]
    entry["shard_id"] = _make_shard_id(start, run_index)
    ensure_receipt_id(entry)
    entry["events"] = _events(hs, {
        "run_id": run_id, "shard_id": entry["shard_id"], "attempt_number": 1, "actor": hs.agent,
    })
    entry["import"] = import_block(hs, obj, job_id=job_id, supersedes=supersedes)
    entry["facts"] = facts_block(hs)
    entry["sealed_at"] = _now()
    assert_no_live_evidence(entry)
    return coerce_shard_entry(entry)


def build_attachment(
    hs: HistoricalSession,
    obj: SourceObject,
    *,
    receipt_id: str,
    pins_content_hash: str | None,
    job_id: str | None,
    supersedes: str | None = None,
) -> dict:
    """An attachment carrying imported facts about a session already captured live."""
    from openshard.history.shard_hash import compute_shard_hash

    facts = facts_block(hs)
    att: dict[str, Any] = {
        "attachment_id": "att_" + hashlib.sha256(
            f"{receipt_id}:{hs.import_key}:{hs.source_sha256}".encode()).hexdigest()[:32],
        "receipt_id": receipt_id,
        "pins_content_hash": pins_content_hash,
        "kind": "reimport_note",
        "facts": [{"field": k, **v} for k, v in facts.items()],
        "produced_by": f"{hs.parser}+{IMPORTER_VERSION}",
        "produced_at": _now(),
        "supersedes": supersedes,
        "import_key": hs.import_key,
        "source_sha256": hs.source_sha256,
        "source": import_block(hs, obj, job_id=job_id)["source"],
        "job_id": job_id,
    }
    assert_no_live_evidence(att)
    att["content_hash"] = compute_shard_hash(att)
    return att
