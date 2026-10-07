from __future__ import annotations

from pathlib import Path

from openshard.native.tools import (
    NativeToolCall,
    NativeToolResult,
    _exec_get_git_diff,
    _exec_list_files,
    _exec_read_file,
    _exec_run_verification,
    _exec_search_repo,
    _exec_write_file,
    classify_native_tool,
)


class NativeToolRunner:
    """Executes allowed deterministic native tools against a fixed repo root."""

    def __init__(
        self,
        repo_root: Path,
        *,
        blocked_write_patterns: tuple[str, ...] = (),
        approval_write_patterns: tuple[str, ...] = (),
    ) -> None:
        self._repo_root = repo_root
        # Organisation write-path policy, forwarded to write_file so a denied
        # pattern is refused even by an approved call.
        self._blocked_write_patterns = tuple(blocked_write_patterns)
        self._approval_write_patterns = tuple(approval_write_patterns)

    def run(self, call: NativeToolCall) -> NativeToolResult:
        risk = classify_native_tool(call.tool_name)

        if risk == "blocked":
            return NativeToolResult(
                tool_name=call.tool_name,
                ok=False,
                error=f"Tool '{call.tool_name}' is blocked.",
            )

        if risk == "needs_approval" and not call.approved:
            return NativeToolResult(
                tool_name=call.tool_name,
                ok=False,
                error=f"Tool '{call.tool_name}' requires approval.",
            )

        args = call.args if isinstance(call.args, dict) else {}

        if call.tool_name == "write_file":
            # The registry marks write_file needs_approval, so the check above
            # already refused an unapproved call; the executor re-validates the
            # path and the file-mutation policy before touching anything.
            return _exec_write_file(
                self._repo_root,
                args.get("path", ""),
                args.get("content"),
                approved=call.approved,
                blocked_patterns=self._blocked_write_patterns,
                approval_patterns=self._approval_write_patterns,
            )

        if call.tool_name == "list_files":
            return _exec_list_files(self._repo_root, args.get("subdir", "."))

        if call.tool_name == "read_file":
            limit = args.get("limit", 4000)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                limit = 4000
            return _exec_read_file(self._repo_root, args.get("path", ""), limit=limit)

        if call.tool_name == "search_repo":
            return _exec_search_repo(
                self._repo_root,
                args.get("query", ""),
                max_matches=args.get("max_matches", 50),
            )

        if call.tool_name == "get_git_diff":
            limit = args.get("limit", 4000)
            timeout = args.get("timeout", 10.0)
            if not isinstance(limit, int) or limit <= 0:
                limit = 4000
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                timeout = 10.0
            return _exec_get_git_diff(
                self._repo_root,
                limit=limit,
                timeout=timeout,
            )

        if call.tool_name == "run_verification":
            limit = args.get("limit", 4000)
            if not isinstance(limit, int) or limit <= 0:
                limit = 4000
            return _exec_run_verification(
                self._repo_root,
                approved=call.approved,
                limit=limit,
            )

        return NativeToolResult(
            tool_name=call.tool_name,
            ok=False,
            error=f"Tool '{call.tool_name}' is not yet implemented.",
        )

    def trace_entry(self, call: NativeToolCall, result: NativeToolResult) -> dict:
        return {
            "tool": call.tool_name,
            "ok": result.ok,
            "approved": call.approved,
            "output_chars": len(result.output),
            "error": result.error,
        }
