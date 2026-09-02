"""Local, read-only MCP server exposing this repository's OpenShard history.

    MCP tool -> history/query.py -> canonical OpenShard objects

No business logic lives here: every tool below is a thin, privacy-bounded
JSON conversion of a single ``openshard.history.query`` call. Nothing is
cached, nothing is written, and nothing reaches outside the local
``.openshard/runs.jsonl`` store PR1 already reads.

Requires the optional ``mcp`` dependency (``pip install 'openshard[mcp]'``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openshard.history import query as history_query
from openshard.history.query import (
    DEFAULT_CONTEXT_LIMIT,
    RelevantAttempt,
    RelevantMatch,
    SearchHit,
    UnknownRunError,
    UnknownShardError,
)
from openshard.history.shard import Shard
from openshard.history.shard_contract import ShardFinding, ShardReceipt

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - exercised via CLI, not tests
    raise ImportError(
        "The 'mcp' package is required for the OpenShard MCP server. "
        "Install it with: pip install 'openshard[mcp]'"
    ) from exc

SERVER_NAME = "openshard"
SERVER_INSTRUCTIONS = (
    "Read-only access to this repository's local OpenShard engineering history "
    "(.openshard/runs.jsonl). Use recent_shards or search_history to find past "
    "tasks (Shards), then get_shard / get_receipt for details on one of them. "
    "Before starting a new coding task, call relevant_context(task) to get a "
    "compact, ranked summary of prior Shards likely to help — including past "
    "failures, retries, and verification results for similar work. "
    "Repository filtering is best-effort: older or externally-observed entries "
    "may not carry a stable repository identity."
)

# Tool-layer bounds -- independent of history.query's own DEFAULT_LIMIT so a
# malformed/huge client-supplied limit can never force an unbounded response.
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
MAX_FILES = 50
MAX_FINDINGS = 20
_MAX_TEXT = 300


def _clamp_limit(limit: int) -> int:
    """Bound a client-supplied limit. Non-positive stays non-positive (history.query
    already returns [] for that); anything above MAX_LIMIT is capped, never rejected."""
    if limit <= 0:
        return limit
    return min(limit, MAX_LIMIT)


def _truncate(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _shard_dict(shard: Shard) -> dict[str, Any]:
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


def _search_hit_dict(hit: SearchHit) -> dict[str, Any]:
    d = _shard_dict(hit.shard)
    d.update(
        {
            "score": hit.score,
            "matched_fields": list(hit.matched_fields),
            "status": hit.status,
            "repo": hit.repo,
        }
    )
    return d


def _finding_dict(finding: ShardFinding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "message": _truncate(finding.message),
        "path": finding.path,
        "line": finding.line,
    }


def _file_dict(raw: dict) -> dict[str, Any]:
    return {
        "path": raw.get("path"),
        "change_type": raw.get("change_type"),
        "summary": _truncate(raw.get("summary")) if isinstance(raw.get("summary"), str) else None,
    }


def _receipt_dict(receipt: ShardReceipt) -> dict[str, Any]:
    """Canonical ShardReceipt -> bounded, privacy-safe dict.

    Deliberately omits: raw prompts/transcripts, stdout/stderr (including
    adapter_stdout_summary/adapter_stderr_summary), diffs, agent_notes and
    run_timeline (free-text/internal debug traces), and any field carrying an
    absolute filesystem path. Only counts, short structured findings, and
    relative file paths already safe to display in the CLI receipt cross the
    MCP boundary.
    """
    shard = receipt.shard
    files_raw = receipt.files_detail or [{"path": p} for p in receipt.files_touched]

    return {
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
        "files": [_file_dict(f) for f in files_raw[:MAX_FILES] if isinstance(f, dict)],
        "diff_added": receipt.diff_added,
        "diff_removed": receipt.diff_removed,
        "checks": receipt.checks_display,
        "status": receipt.status,
        "verification_status": receipt.verification_status or None,
        "verification_reason": _truncate(receipt.verification_reason) or None,
        "verification_returncode": receipt.verification_returncode,
        "verification_duration_seconds": receipt.verification_duration_seconds,
        "approval": receipt.approval,
        "cost": receipt.cost_display,
        "result": _truncate(receipt.result),
        "repo": receipt.repo,
        "branch": receipt.branch,
        "git_state": receipt.git_state,
        "duration_seconds": receipt.duration_seconds,
        "context_quality": receipt.context_quality,
        "findings": [_finding_dict(f) for f in receipt.findings[:MAX_FINDINGS]],
    }


def _relevant_attempt_dict(attempt: RelevantAttempt) -> dict[str, Any]:
    return {
        "run_id": attempt.run_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "verification_status": attempt.verification_status or None,
        "verification_reason": _truncate(attempt.verification_reason) or None,
    }


def _relevant_match_dict(match: RelevantMatch) -> dict[str, Any]:
    """Canonical RelevantMatch -> bounded, privacy-safe dict.

    Same privacy boundary as ``_receipt_dict``: no raw prompts/transcripts/
    diffs/stdout/stderr/absolute paths. Additionally excludes every
    Note-severity finding (see ``history.query`` module docstring) — only
    Critical/High/Medium/Low findings, which come from actual review/
    verification results rather than free-form agent notes, are included.
    """
    d = _shard_dict(match.shard)
    d.update({
        "score": match.score,
        "why_relevant": list(match.signals),
        "status": match.status,
        "verification_status": match.verification_status or None,
        "verification_reason": _truncate(match.verification_reason) or None,
        "result": _truncate(match.result),
        "repo": match.repo,
        "files": list(match.files[:MAX_FILES]),
        "findings": [_finding_dict(f) for f in match.findings[:MAX_FINDINGS]],
        "attempts": [_relevant_attempt_dict(a) for a in match.attempts],
    })
    return d


def build_server(*, repo_path: Path | None = None) -> FastMCP:
    """Build the OpenShard MCP server, scoped to one repository's history.

    ``repo_path`` fixes which checkout's ``.openshard/runs.jsonl`` every tool
    reads (default: the process's current directory, matching
    ``history.query``'s own default). It is a server-startup setting, not a
    per-call tool argument -- an MCP client can filter by ``repo`` identity
    but cannot point the server at an arbitrary filesystem path.
    """
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @mcp.tool()
    def recent_shards(limit: int = DEFAULT_LIMIT, repo: str | None = None) -> list[dict[str, Any]]:
        """List the most recent OpenShard Shards (tasks) in this repository's
        local history, newest first. Each result is a Shard identity summary
        (shard_id, created_at, task, agent, origin); use get_receipt for a
        given shard_id to see status, model, files changed, and verification
        detail. ``repo`` optionally filters by repository identity, remote
        URL, or legacy folder name -- omit to see all repositories recorded
        in this history file. Returns [] on empty history."""
        shards = history_query.list_shards(
            limit=_clamp_limit(limit), repo=repo, repo_path=repo_path
        )
        return [_shard_dict(s) for s in shards]

    @mcp.tool()
    def get_shard(shard_id: str) -> dict[str, Any]:
        """Look up one canonical Shard (task identity) by its exact shard_id,
        as returned by recent_shards or search_history. Raises a clear error
        if no Shard with that id exists in this repository's history."""
        if not shard_id or not shard_id.strip():
            raise ValueError("shard_id must be a non-empty string.")
        try:
            shard = history_query.get_shard(shard_id, repo_path=repo_path)
        except UnknownShardError as exc:
            raise ValueError(str(exc)) from None
        return _shard_dict(shard)

    @mcp.tool()
    def get_receipt(
        shard_id: str | None = None, run_id: str | None = None
    ) -> dict[str, Any]:
        """Get the canonical Receipt (status, model, files changed,
        verification, findings) for a Shard or one specific run attempt.
        Pass shard_id alone for that Shard's latest attempt; run_id alone for
        one exact run; both to require that run belong to that Shard. At
        least one of shard_id/run_id is required. Raises a clear error when
        the Shard or run is not found."""
        if not shard_id and not run_id:
            raise ValueError("get_receipt requires shard_id and/or run_id.")
        try:
            receipt = history_query.get_receipt(
                shard_id, run_id=run_id, repo_path=repo_path
            )
        except (UnknownShardError, UnknownRunError) as exc:
            raise ValueError(str(exc)) from None
        return _receipt_dict(receipt)

    @mcp.tool()
    def search_history(
        query: str, limit: int = DEFAULT_LIMIT, repo: str | None = None
    ) -> list[dict[str, Any]]:
        """Deterministic local search over past Shards: every whitespace-
        separated term in ``query`` must appear as a case-insensitive
        substring of the task text, shard id, agent, or status of a Shard's
        latest attempt (never summaries, notes, or any raw model output).
        Results are ordered by match strength, newest first. An empty query
        returns []. ``repo`` optionally filters by repository identity."""
        hits = history_query.search_history(
            query, limit=_clamp_limit(limit), repo=repo, repo_path=repo_path
        )
        return [_search_hit_dict(h) for h in hits]

    @mcp.tool()
    def relevant_context(
        task: str, limit: int = DEFAULT_CONTEXT_LIMIT, repo: str | None = None
    ) -> dict[str, Any]:
        """Get compact, deterministic OpenShard context relevant to a coding
        task before starting it: ranked prior Shards whose task text, shard
        id, or agent overlaps ``task``, each with its status, verification
        result, non-Note findings, changed files, and — for retried Shards —
        a per-attempt history (e.g. attempt 1 failed, attempt 2 passed).
        Ranking is local keyword-overlap scoring only (no embeddings or model
        calls); a recorded verification failure or multiple attempts add a
        small bonus but never pull in an unrelated Shard on their own.
        Returns ``matches`` (bounded, structured) and ``context_text`` (a
        compact block suitable for pasting into another agent's context) —
        both honestly empty/explanatory when no prior Shard is relevant.
        ``repo`` optionally filters by repository identity."""
        ctx = history_query.relevant_context(
            task, limit=_clamp_limit(limit), repo=repo, repo_path=repo_path
        )
        return {
            "task": ctx.task,
            "matches": [_relevant_match_dict(m) for m in ctx.matches],
            "context_text": ctx.context_text,
        }

    return mcp


def serve_stdio(*, repo_path: Path | None = None) -> None:
    """Build and run the server over stdio. Blocks until the client disconnects."""
    build_server(repo_path=repo_path).run(transport="stdio")
