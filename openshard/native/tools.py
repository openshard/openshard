from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from openshard.security.paths import UnsafePathError, resolve_safe_repo_path


@dataclass
class NativeTool:
    name: str
    description: str
    risk: str  # "safe" | "needs_approval" | "blocked"
    categories: list[str]


@dataclass
class NativeToolCall:
    tool_name: str
    args: dict
    approved: bool = False


@dataclass
class NativeToolResult:
    tool_name: str
    ok: bool
    output: str = ""
    error: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class NativeToolSearchEvent:
    """Compact provenance record for a single safe read/search/observation tool call.

    Never stores raw output, snippets, diffs, stdout, stderr, or model output.
    """
    tool_name: str
    selected_reason: str = ""
    query: str = ""
    result_count: int = 0
    result_quality: str = "unknown"  # unknown | empty | weak | useful
    zero_result_case: bool = False
    retry_count: int = 0
    fallback_tool: str | None = None
    context_injected: bool = False
    changed_plan: bool = False
    warnings: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)


_BUILTIN_TOOLS: list[NativeTool] = [
    NativeTool(
        name="list_files",
        description="List files in the repository or a subdirectory.",
        risk="safe",
        categories=["repo", "navigation"],
    ),
    NativeTool(
        name="read_file",
        description="Read the contents of a file within the repository.",
        risk="safe",
        categories=["repo", "navigation"],
    ),
    NativeTool(
        name="search_repo",
        description="Search for patterns or symbols across repository files.",
        risk="safe",
        categories=["repo", "navigation"],
    ),
    NativeTool(
        name="get_git_diff",
        description="Retrieve the current git diff for inspection.",
        risk="safe",
        categories=["repo", "inspection"],
    ),
    NativeTool(
        name="write_file",
        description="Write or overwrite a file within the repository.",
        risk="needs_approval",
        categories=["repo", "mutation"],
    ),
    NativeTool(
        name="run_verification",
        description="Run the project verification plan (tests, lint, typecheck).",
        risk="safe",
        categories=["verification"],
    ),
    NativeTool(
        name="run_command",
        description="Execute an arbitrary shell command.",
        risk="blocked",
        categories=["shell"],
    ),
]


def list_native_tools() -> list[NativeTool]:
    return list(_BUILTIN_TOOLS)


def get_native_tool(name: str) -> NativeTool | None:
    for tool in _BUILTIN_TOOLS:
        if tool.name == name:
            return tool
    return None


def classify_native_tool(name: str) -> str:
    tool = get_native_tool(name)
    return tool.risk if tool is not None else "blocked"


def compact_tool_result(output: str, limit: int = 4000) -> str:
    if len(output) <= limit:
        return output
    return output[:limit] + f"\n[truncated: output exceeded {limit} chars]"


_IGNORE_DIRS: frozenset[str] = frozenset({".git", "__pycache__", ".openshard"})
_IGNORE_SUFFIXES: frozenset[str] = frozenset({".pyc"})
_BINARY_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".exe", ".bin",
    ".woff", ".woff2", ".ttf", ".eot",
})


def _exec_list_files(repo_root: Path, subdir: str = ".") -> NativeToolResult:
    try:
        if subdir == ".":
            base = repo_root.resolve()
        else:
            base = resolve_safe_repo_path(repo_root, subdir)
    except UnsafePathError as exc:
        return NativeToolResult(tool_name="list_files", ok=False, error=str(exc))

    repo_resolved = repo_root.resolve()
    paths: list[str] = []

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fname in filenames:
            if Path(fname).suffix in _IGNORE_SUFFIXES:
                continue
            full = Path(dirpath) / fname
            try:
                rel = full.relative_to(repo_resolved)
                paths.append(str(rel))
            except ValueError:
                continue

    output = "\n".join(sorted(paths))
    return NativeToolResult(tool_name="list_files", ok=True, output=output)


def _exec_search_repo(
    repo_root: Path,
    query: str,
    *,
    max_matches: int = 50,
) -> NativeToolResult:
    if not query or not query.strip():
        return NativeToolResult(
            tool_name="search_repo",
            ok=False,
            error="search_repo requires a non-empty query.",
        )

    try:
        max_matches_int = int(max_matches)
    except (TypeError, ValueError):
        max_matches_int = 50
    max_matches_int = max(1, max_matches_int)

    query_lower = query.strip().lower()
    repo_resolved = repo_root.resolve()
    matches: list[str] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(repo_resolved):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fname in filenames:
            p = Path(fname)
            if p.suffix in _IGNORE_SUFFIXES or p.suffix in _BINARY_SUFFIXES:
                continue
            full = Path(dirpath) / fname
            try:
                rel = str(full.relative_to(repo_resolved))
            except ValueError:
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if query_lower in line.lower():
                    matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                    if len(matches) >= max_matches_int:
                        truncated = True
                        break
            if truncated:
                break

    output = compact_tool_result("\n".join(matches))
    return NativeToolResult(
        tool_name="search_repo",
        ok=True,
        output=output,
        metadata={"matches": len(matches), "truncated": truncated},
    )


def _exec_get_git_diff(
    repo_root: Path,
    *,
    limit: int = 4000,
    timeout: float = 10.0,
) -> NativeToolResult:
    repo_resolved = repo_root.resolve()

    if not (repo_resolved / ".git").exists():
        return NativeToolResult(
            tool_name="get_git_diff",
            ok=False,
            error="not a git repository (or any of the parent directories): .git",
        )

    try:
        completed = subprocess.run(
            [
                "git",
                "-c", "core.externalDiff=",
                "-c", "diff.external=",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ],
            cwd=repo_resolved,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return NativeToolResult(
            tool_name="get_git_diff",
            ok=False,
            error=f"git diff timed out after {timeout} seconds.",
            metadata={"timeout": timeout},
        )
    except OSError as exc:
        return NativeToolResult(
            tool_name="get_git_diff",
            ok=False,
            error=str(exc),
        )

    if completed.returncode != 0:
        err = completed.stderr.strip() or completed.stdout.strip()
        return NativeToolResult(
            tool_name="get_git_diff",
            ok=False,
            error=compact_tool_result(err, limit=limit),
            metadata={"returncode": completed.returncode},
        )

    output = compact_tool_result(completed.stdout, limit=limit)
    return NativeToolResult(
        tool_name="get_git_diff",
        ok=True,
        output=output,
        metadata={
            "returncode": completed.returncode,
            "output_chars": len(output),
            "truncated": len(completed.stdout) > limit,
        },
    )


def _exec_run_verification(
    repo_root: Path,
    *,
    approved: bool = False,
    limit: int = 4000,
) -> NativeToolResult:
    from openshard.analysis.repo import analyze_repo
    from openshard.verification.executor import run_verification_plan
    from openshard.verification.plan import CommandSafety, build_verification_plan

    repo_facts = analyze_repo(repo_root)
    plan = build_verification_plan({}, repo_facts)

    if not plan.has_commands:
        return NativeToolResult(
            tool_name="run_verification",
            ok=False,
            error="No verification command detected.",
            metadata={"attempted": False},
        )

    blocked = [c for c in plan.commands if c.safety == CommandSafety.blocked]
    needs_approval_cmds = [c for c in plan.commands if c.safety == CommandSafety.needs_approval]

    first_cmd = plan.commands[0]

    if blocked:
        reasons = "; ".join(c.reason for c in blocked)
        return NativeToolResult(
            tool_name="run_verification",
            ok=False,
            error=f"Verification command is blocked: {reasons}",
            metadata={
                "attempted": False,
                "command_count": len(plan.commands),
                "classification": "blocked",
                "decision_reason": blocked[0].reason,
                "exit_code": 1,
                "duration_ms": 0,
                "output_chars": 0,
                "raw_content_stored": False,
            },
        )

    if needs_approval_cmds and not approved:
        return NativeToolResult(
            tool_name="run_verification",
            ok=False,
            error="Verification command requires approval. Set approved=True to run.",
            metadata={
                "attempted": False,
                "command_count": len(plan.commands),
                "classification": "needs_approval",
                "decision_reason": needs_approval_cmds[0].reason,
                "exit_code": None,
                "duration_ms": 0,
                "output_chars": 0,
                "raw_content_stored": False,
            },
        )

    t0 = time.monotonic()
    _sink: list = []
    exit_code, raw_output = run_verification_plan(  # type: ignore[misc]  # capture=True always returns tuple; return type is int | tuple
        plan, repo_root, capture=True,
        pre_approved_by="native_tool_approved_flag" if approved else None,
        outcome_sink=_sink,
    )
    if _sink and not _sink[0].permitted:
        # Refused by command policy: not a failed test run.
        return NativeToolResult(
            tool_name="run_verification",
            ok=False,
            error=f"Verification not run: {_sink[0].decision.reason}",
            metadata={
                "attempted": False,
                "command_count": len(plan.commands),
                "classification": _sink[0].decision.decision,
                "decision_reason": _sink[0].decision.reason,
                "exit_code": None,
                "duration_ms": 0,
                "output_chars": 0,
                "raw_content_stored": False,
            },
        )
    duration_ms = int((time.monotonic() - t0) * 1000)

    output = compact_tool_result(raw_output, limit)
    passed = exit_code == 0

    return NativeToolResult(
        tool_name="run_verification",
        ok=passed,
        output=output,
        error=None if passed else f"Verification failed (exit code {exit_code})",
        metadata={
            "attempted": True,
            "passed": passed,
            "exit_code": exit_code,
            "command_count": len(plan.commands),
            "output_chars": len(raw_output),
            "truncated": len(raw_output) > limit,
            "classification": first_cmd.safety.value,
            "decision_reason": first_cmd.reason,
            "duration_ms": duration_ms,
            "raw_content_stored": False,
        },
    )


def _exec_read_file(repo_root: Path, path: str) -> NativeToolResult:
    try:
        safe = resolve_safe_repo_path(repo_root, path)
        text = safe.read_text(encoding="utf-8", errors="replace")
        return NativeToolResult(
            tool_name="read_file",
            ok=True,
            output=compact_tool_result(text),
        )
    except UnsafePathError as exc:
        return NativeToolResult(tool_name="read_file", ok=False, error=str(exc))
    except OSError as exc:
        return NativeToolResult(tool_name="read_file", ok=False, error=str(exc))
