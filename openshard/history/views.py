"""Privacy-bounded dict projections of canonical history objects.

One place defines what a ``Shard`` / ``ShardReceipt`` / ``RelevantMatch``
looks like when it crosses a machine boundary -- the local MCP server
(``openshard.mcp.server``) and the CLI ``--json`` surfaces of
``openshard history`` / ``openshard context`` all use these same
functions, so the privacy boundary cannot drift between them.

Deliberately omitted everywhere: raw prompts/transcripts, assistant
responses, stdout/stderr (including adapter summaries), diffs, agent
notes, run timelines (free-text/internal traces), environment values, and
any field carrying an absolute filesystem path. Only counts, short
structured findings, and repo-relative file paths already safe to display
in the CLI receipt are included.
"""

from __future__ import annotations

from typing import Any

from openshard.history.query import RelevantAttempt, RelevantMatch, SearchHit
from openshard.history.shard import Shard
from openshard.history.shard_contract import ShardFinding, ShardReceipt

MAX_FILES = 50
MAX_FINDINGS = 20
MAX_TEXT = 300


def truncate_text(text: str | None, limit: int = MAX_TEXT) -> str | None:
    """Bound a short free-text field; ``None``/empty stays ``None``."""
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def shard_to_dict(shard: Shard) -> dict[str, Any]:
    """Canonical Shard -> bounded dict. Identity/origin only -- no evidence fields."""
    return {
        "shard_id": shard.shard_id,
        "created_at": shard.created_at,
        "task_short": shard.task_short,
        "task_full": shard.task_full,
        "agent": shard.agent,
        "origin": shard.origin,
        "capture_depth": shard.capture_depth,
    }


def search_hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    d = shard_to_dict(hit.shard)
    d.update({
        "score": hit.score,
        "matched_fields": list(hit.matched_fields),
        "status": hit.status,
        "repo": hit.repo,
    })
    return d


def finding_to_dict(finding: ShardFinding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "message": truncate_text(finding.message),
        "path": finding.path,
        "line": finding.line,
    }


def file_to_dict(raw: dict) -> dict[str, Any]:
    summary = raw.get("summary")
    return {
        "path": raw.get("path"),
        "change_type": raw.get("change_type"),
        "summary": truncate_text(summary) if isinstance(summary, str) else None,
    }


def receipt_to_dict(receipt: ShardReceipt, *, extended: bool = False) -> dict[str, Any]:
    """Canonical ShardReceipt -> bounded, privacy-safe dict.

    The default key set is the MCP ``get_receipt`` contract and is kept
    stable. ``extended=True`` (used by ``openshard history --json``) adds the
    provenance-labelled cost/token fields and the turn-completion status:
    ``model`` stays the display string (``"Not recorded"`` / ``"Unknown"``
    when that is the truth), ``cost_is_estimate`` is always ``True`` whenever
    a cost exists, and token fields are ``None`` unless a provider/agent
    reported them (``tokens_provenance`` says who). The extended form also
    drops Note-severity findings -- that is where free-form ``agent_notes``
    land (see ``history.query``), and a recent-work listing has no need for
    agent prose.
    """
    shard = receipt.shard
    files_raw = receipt.files_detail or [{"path": p} for p in receipt.files_touched]
    findings = receipt.findings if not extended else [f for f in receipt.findings if f.severity != "Note"]

    d: dict[str, Any] = {
        "shard_id": receipt.shard_id,
        "run_id": receipt.run_id,
        "attempt_number": receipt.attempt_number,
        "created_at": receipt.created_at,
        "task_short": receipt.task_short,
        "task_full": receipt.task_full,
        "agent": receipt.agent,
        "origin": shard.origin if shard else None,
        "capture_depth": shard.capture_depth if shard else None,
        "model": receipt.model_display,
        "model_stages": [{"stage": s, "model": m} for s, m in receipt.model_stages],
        "strategy": receipt.strategy,
        "risk": receipt.risk,
        "sandbox": receipt.sandbox,
        "files_changed": receipt.files_changed,
        "files": [file_to_dict(f) for f in files_raw[:MAX_FILES] if isinstance(f, dict)],
        "diff_added": receipt.diff_added,
        "diff_removed": receipt.diff_removed,
        "checks": receipt.checks_display,
        "status": receipt.status,
        "verification_status": receipt.verification_status or None,
        "verification_reason": truncate_text(receipt.verification_reason) or None,
        "verification_returncode": receipt.verification_returncode,
        "verification_duration_seconds": receipt.verification_duration_seconds,
        "approval": receipt.approval,
        "cost": receipt.cost_display,
        "result": truncate_text(receipt.result),
        "repo": receipt.repo,
        "branch": receipt.branch,
        "git_state": receipt.git_state,
        "duration_seconds": receipt.duration_seconds,
        "context_quality": receipt.context_quality,
        "findings": [finding_to_dict(f) for f in findings[:MAX_FINDINGS]],
    }
    if extended:
        d.update({
            "task_completion": receipt.task_completion,
            "cost_usd": receipt.cost_raw,
            "cost_provenance": receipt.cost_provenance,
            "cost_is_estimate": receipt.cost_raw is not None,
            "tokens_input": receipt.tokens_input,
            "tokens_output": receipt.tokens_output,
            "tokens_cache_read": receipt.tokens_cache_read,
            "tokens_cache_creation": receipt.tokens_cache_creation,
            "tokens_provenance": receipt.tokens_provenance,
        })
    return d


def relevant_attempt_to_dict(attempt: RelevantAttempt) -> dict[str, Any]:
    return {
        "run_id": attempt.run_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "verification_status": attempt.verification_status or None,
        "verification_reason": truncate_text(attempt.verification_reason) or None,
    }


def relevant_match_to_dict(match: RelevantMatch) -> dict[str, Any]:
    """Canonical RelevantMatch -> bounded, privacy-safe dict.

    Same privacy boundary as ``receipt_to_dict``. Additionally excludes every
    Note-severity finding (see the ``history.query`` module docstring) --
    only Critical/High/Medium/Low findings, which come from actual review/
    verification results rather than free-form agent notes, are included.
    """
    d = shard_to_dict(match.shard)
    d.update({
        "score": match.score,
        "why_relevant": list(match.signals),
        "status": match.status,
        "verification_status": match.verification_status or None,
        "verification_reason": truncate_text(match.verification_reason) or None,
        "result": truncate_text(match.result),
        "repo": match.repo,
        "files": list(match.files[:MAX_FILES]),
        "findings": [finding_to_dict(f) for f in match.findings[:MAX_FINDINGS]],
        "attempts": [relevant_attempt_to_dict(a) for a in match.attempts],
    })
    return d
