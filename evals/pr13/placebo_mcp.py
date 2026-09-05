"""Placebo OpenShard MCP server for the PR13 control arm.

The control arm must see exactly the tool surface the treatment arm sees --
the same server name, the same five read-only tools, the same descriptions
and input schemas -- while receiving *empty* OpenShard history. This
server provides that surface and nothing else:

* it never reads ``.openshard/`` or any other file,
* it never imports ``openshard`` (so no production history API can run),
* every tool returns the response the production server returns for a
  repository with no history at all: empty lists, "no relevant history"
  context, and the same not-found errors for lookups.

Run as a standalone script (Claude Code launches it over stdio)::

    python evals/pr13/placebo_mcp.py

``tests/test_pr13_benchmark.py`` proves the surface matches the production
server (``openshard.mcp.server``) tool for tool and that this module never
touches history.
"""

from __future__ import annotations

import sys
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover - the benchmark preflight requires the mcp extra
    raise ImportError("The 'mcp' package is required for the placebo server: pip install 'openshard[mcp]'") from exc

# Mirrors openshard.mcp.server: the values are copied, not imported, so this
# module has no dependency on the production package. The test suite asserts
# they are still identical.
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
DEFAULT_LIMIT = 20
DEFAULT_CONTEXT_LIMIT = 5
PLACEBO_KIND = "placebo"


def _unknown_shard(shard_id: str) -> ToolError:
    return ToolError(
        f"No existing Shard found with id '{shard_id}'. "
        "Shards are created automatically by a run; use list_shards() to see "
        "the ids recorded in this repository's history."
    )


def no_match_text(task: str) -> str:
    """The exact text the production server returns when nothing matches."""
    stripped = task.strip()
    header = f'Relevant OpenShard context for: "{stripped}"' if stripped else "Relevant OpenShard context"
    if not stripped:
        return header + "\n\nNo task given — nothing to match against local OpenShard history.\n"
    return header + "\n\nNo relevant prior OpenShard history found for this task.\n"


def build_server() -> MCPServer:
    """Build the placebo server: production tool surface, empty-history behaviour."""
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
        del limit, repo
        return []

    @mcp.tool()
    def get_shard(shard_id: str) -> dict[str, Any]:
        """Look up one canonical Shard (task identity) by its exact shard_id,
        as returned by recent_shards or search_history. Raises a clear error
        if no Shard with that id exists in this repository's history."""
        if not shard_id or not shard_id.strip():
            raise ToolError("shard_id must be a non-empty string.")
        raise _unknown_shard(shard_id)

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
        if shard_id:
            raise _unknown_shard(shard_id)
        raise ToolError(f"No run found with id '{run_id}'.")

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
        del query, limit, repo
        return []

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
        del limit, repo
        clean_task = task or ""
        return {"task": clean_task, "matches": [], "context_text": no_match_text(clean_task)}

    return mcp


def main(argv: list[str] | None = None) -> int:
    """Serve over stdio. Any arguments (e.g. a ``--repo-path``) are accepted and ignored."""
    del argv
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
