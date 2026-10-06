"""Read-only MCP server for repository-scoped Openshard evidence and authority.

History tools remain privacy-bounded projections of the local
``.openshard/runs.jsonl`` store. ``authority_snapshot`` is the one control
tool: it reads the same local configuration and current organisation policy
that a new OSN run would use. When this checkout is linked to Platform that
tool performs the same bounded, authenticated policy GET as OSN. It never
writes policy, grants approval, or changes authority.

Requires the optional ``mcp`` dependency (``pip install 'openshard[mcp]'``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openshard.history import query as history_query
from openshard.history.query import (
    DEFAULT_CONTEXT_LIMIT,
    UnknownRunError,
    UnknownShardError,
)
from openshard.history.views import (
    MAX_FILES,
    MAX_FINDINGS,
    receipt_to_dict,
    relevant_match_to_dict,
    search_hit_to_dict,
    shard_to_dict,
)

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
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
    "learning_signals(task) adds evidence-backed patterns across those runs "
    "(tests and checks that caught failures, how models fared on similar tasks), "
    "each with its sample size; treat them as advisory evidence, not instructions. "
    "authority_snapshot() reports the effective OSN model, budget and organisation "
    "permission boundaries for this checkout. It is read-only: an agent cannot "
    "approve its own work or grant itself more authority. "
    "Repository filtering is best-effort: older or externally-observed entries "
    "may not carry a stable repository identity."
)

# Tool-layer bounds -- independent of history.query's own DEFAULT_LIMIT so a
# malformed/huge client-supplied limit can never force an unbounded response.
# The per-object bounds (MAX_FILES / MAX_FINDINGS / MAX_TEXT) and every dict
# projection live in ``openshard.history.views`` so the CLI's --json surfaces
# and this server share one privacy boundary.
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

__all__ = [
    "DEFAULT_LIMIT", "MAX_FILES", "MAX_FINDINGS", "MAX_LIMIT", "SERVER_NAME",
    "build_server", "serve_stdio",
]


def _clamp_limit(limit: int) -> int:
    """Bound a client-supplied limit. Non-positive stays non-positive (history.query
    already returns [] for that); anything above MAX_LIMIT is capped, never rejected."""
    if limit <= 0:
        return limit
    return min(limit, MAX_LIMIT)


class _ToolCall:
    """Telemetry (0.4.2) for one MCP tool call: name, duration, result count, ok/error.

    Never raises and never touches the tool's own result or error; only
    ``results`` (a count) is read.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.results = 0
        self._t0 = 0.0

    def __enter__(self) -> _ToolCall:
        import time

        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        try:
            import time

            from openshard.telemetry import emit

            emit(
                "mcp.tool_called", tool=self.tool, results=int(self.results),
                duration_ms=int((time.perf_counter() - self._t0) * 1000),
                result="error" if exc_type is not None else "ok",
            )
        except Exception:
            pass


def build_server(*, repo_path: Path | None = None) -> MCPServer:
    """Build the OpenShard MCP server, scoped to one repository's history.

    ``repo_path`` fixes which checkout's ``.openshard/runs.jsonl`` every tool
    reads (default: the process's current directory, matching
    ``history.query``'s own default). It is a server-startup setting, not a
    per-call tool argument -- an MCP client can filter by ``repo`` identity
    but cannot point the server at an arbitrary filesystem path.
    """
    mcp = MCPServer(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @mcp.tool()
    def recent_shards(limit: int = DEFAULT_LIMIT, repo: str | None = None) -> list[dict[str, Any]]:
        """List the most recent OpenShard Shards (tasks) in this repository's
        local history, newest first. Each result is a Shard identity summary
        (shard_id, created_at, task, agent, origin); use get_receipt for a
        given shard_id to see status, model, files changed, and verification
        detail. ``repo`` optionally filters by repository identity, remote
        URL, or legacy folder name -- omit to see all repositories recorded
        in this history file. Returns [] on empty history."""
        with _ToolCall("recent_shards") as call:
            shards = history_query.list_shards(
                limit=_clamp_limit(limit), repo=repo, repo_path=repo_path
            )
            call.results = len(shards)
        return [shard_to_dict(s) for s in shards]

    @mcp.tool()
    def get_shard(shard_id: str) -> dict[str, Any]:
        """Look up one canonical Shard (task identity) by its exact shard_id,
        as returned by recent_shards or search_history. Raises a clear error
        if no Shard with that id exists in this repository's history."""
        if not shard_id or not shard_id.strip():
            raise ToolError("shard_id must be a non-empty string.")
        with _ToolCall("get_shard") as call:
            try:
                shard = history_query.get_shard(shard_id, repo_path=repo_path)
            except UnknownShardError as exc:
                raise ToolError(str(exc)) from None
            call.results = 1
        return shard_to_dict(shard)

    @mcp.tool()
    def get_receipts_by_task(task_id: str) -> list[dict[str, Any]]:
        """List every persisted Receipt explicitly attached to task_id, newest
        first. Purely a read over stored task_id values -- never infers
        membership from prompt text, timing, or shard_id. Returns an empty
        list when no Receipt carries this task_id."""
        if not task_id or not task_id.strip():
            raise ToolError("task_id must be a non-empty string.")
        with _ToolCall("get_receipts_by_task") as call:
            receipts = history_query.list_receipts_by_task(task_id, repo_path=repo_path)
            call.results = len(receipts)
        return [receipt_to_dict(r) for r in receipts]

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
            raise ToolError("get_receipt requires shard_id and/or run_id.")
        with _ToolCall("get_receipt") as call:
            try:
                receipt = history_query.get_receipt(
                    shard_id, run_id=run_id, repo_path=repo_path
                )
            except (UnknownShardError, UnknownRunError) as exc:
                raise ToolError(str(exc)) from None
            call.results = 1
        return receipt_to_dict(receipt)

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
        with _ToolCall("search_history") as call:
            hits = history_query.search_history(
                query, limit=_clamp_limit(limit), repo=repo, repo_path=repo_path
            )
            call.results = len(hits)
        return [search_hit_to_dict(h) for h in hits]

    @mcp.tool()
    def relevant_context(
        task: str, limit: int = DEFAULT_CONTEXT_LIMIT, repo: str | None = None
    ) -> dict[str, Any]:
        """Get compact, deterministic OpenShard context relevant to a coding
        task before starting it: ranked prior Shards whose task text, shard
        id, or agent overlaps ``task``, each with its status, verification
        result, non-Note findings, changed files, and — for retried Shards —
        a per-attempt history (e.g. attempt 1 failed, attempt 2 passed).
        When a matched Shard recorded a verification failure later followed
        by a pass, ``matches[].recovery`` additionally reports that observed
        chronology (failed attempt, files/tools observed on the attempts in
        between, the attempt that later passed) — observation only; it is
        never a claim that those files or tools caused the later pass.
        Ranking is local keyword-overlap scoring only (no embeddings or model
        calls); a recorded verification failure or multiple attempts add a
        small bonus but never pull in an unrelated Shard on their own.
        Returns ``matches`` (bounded, structured) and ``context_text`` (a
        compact block suitable for pasting into another agent's context) —
        both honestly empty/explanatory when no prior Shard is relevant.
        ``repo`` optionally filters by repository identity."""
        with _ToolCall("relevant_context") as call:
            ctx = history_query.relevant_context(
                task, limit=_clamp_limit(limit), repo=repo, repo_path=repo_path
            )
            call.results = len(ctx.matches)
        return {
            "task": ctx.task,
            "matches": [relevant_match_to_dict(m) for m in ctx.matches],
            "context_text": ctx.context_text,
        }

    @mcp.tool()
    def authority_snapshot() -> dict[str, Any]:
        """Read the authority a new OSN run would start with for this checkout.

        The snapshot uses the same repository config loader and organisation
        policy resolver as `openshard osn run`, including stricter-wins model
        and budget rules. A linked Platform policy that cannot be refreshed is
        an error, not an empty policy. This tool never writes policy, grants an
        approval, or claims control over an external agent."""
        from openshard.config.settings import load_config_safe
        from openshard.sync.policies import (
            PolicyUnavailable,
            combine_budget_limits,
            combine_model_policy,
            organisation_permissions,
            repository_override_present,
            resolve_organisation_policy,
        )

        with _ToolCall("authority_snapshot") as call:
            root = repo_path or Path.cwd()
            repo_config, valid, config_path = load_config_safe(cwd=root)
            if not valid:
                config_name = config_path.name if config_path is not None else "OpenShard config"
                raise ToolError(
                    f"{config_name} could not be parsed; authority is unknown until it is fixed."
                )
            try:
                organisation = resolve_organisation_policy()
                models = combine_model_policy(repo_config, organisation)
                budgets, _local, _organisation = combine_budget_limits(repo_config, organisation)
                permissions = organisation_permissions(organisation)
            except PolicyUnavailable as exc:
                raise ToolError(
                    f"Organisation policy could not be refreshed ({exc}); "
                    "a linked OSN run would refuse to start."
                ) from None
            except ValueError as exc:
                raise ToolError(str(exc)) from None
            call.results = 1

        org = {
            "linked": organisation is not None,
            "applied": bool(organisation and organisation.applied),
            "version": organisation.version if organisation else None,
            "hash": organisation.policy_hash if organisation else None,
            "source": organisation.source if organisation else "local_only",
            "reason": organisation.reason if organisation else None,
        }
        model_view = {
            "mode": models.mode,
            "allowed_models": sorted(models.allowed_models),
            "blocked_models": sorted(models.blocked_models),
            "allowed_providers": sorted(models.allowed_providers),
            "blocked_providers": sorted(models.blocked_providers),
            "max_cost_class": models.max_cost_class,
            "allow_specialist": models.allow_specialist,
            "allow_experimental": models.allow_experimental,
            "allow_watchlist": models.allow_watchlist,
            "allow_deprecated": models.allow_deprecated,
            "allow_open_weight": models.allow_open_weight,
            "allow_fallback": models.allow_fallback,
            "allow_openrouter_wide": models.allow_openrouter_wide,
            "custom_roster_models": sorted(models.custom_roster_models),
            "class_pins": [list(item) for item in models.class_pins],
        }
        return {
            "schema_version": "openshard.authority.v1",
            "enforcement_boundary": "openshard_native",
            "external_agent_control": "observed_or_advisory_unless_integration_grants_control",
            "organisation_policy": org,
            "repository_override_applied": repository_override_present(
                repo_config, has_config_file=config_path is not None
            ),
            "effective": {
                "models": model_view,
                "budgets": budgets.to_dict(),
                "permissions": permissions.to_dict(),
            },
            "approval": {
                "required_write_paths": list(permissions.approval_write_paths),
                "agent_can_self_approve": False,
            },
        }

    @mcp.tool()
    def learning_signals(task: str, limit: int = 5) -> dict[str, Any]:
        """Get evidence-backed learning signals relevant to a coding task:
        patterns OpenShard derived from this repository's verified runs, such
        as tests or checks that repeatedly caught failures on similar work,
        how models fared on the first attempt, recovery paths, recurring
        failure categories and policy boundaries. Each signal has a sample
        size, strength, freshness and the reasons it was selected; only
        OpenShard-observed or independently verified outcomes count, and
        single-run anecdotes are never returned. Advisory evidence, not
        instructions: correlation is not causation. ``context_text`` is a
        compact block suitable for another agent's context; it is honestly
        empty when nothing relevant is known. At most 5 signals."""
        from openshard.learning.retrieval import MAX_LIMIT as LEARNING_MAX
        from openshard.learning.retrieval import consult
        from openshard.learning.signals import load_learning_index, repo_key

        with _ToolCall("learning_signals") as call:
            root = repo_path or Path.cwd()
            key = repo_key(root)
            ctx = consult(task or "", load_learning_index(root, repo=key), repo=key,
                          limit=max(0, min(int(limit), LEARNING_MAX)))
            call.results = len(ctx.retrieved)
        return {
            "task": task,
            "status": ctx.status,
            "task_shape": ctx.shape.to_dict() if ctx.shape else None,
            "signals": [{**r.to_record(), "summary": r.signal.summary} for r in ctx.retrieved],
            "recommended_checks": [c.label for c in ctx.recommended_checks],
            "suggested_files": ctx.suggested_context_files,
            "context_text": ctx.prompt_text or "No evidence-backed learning signals are relevant to this task yet.",
        }

    return mcp


def serve_stdio(*, repo_path: Path | None = None) -> None:
    """Build and run the server over stdio. Blocks until the client disconnects."""
    build_server(repo_path=repo_path).run(transport="stdio")
