"""Turn OpenShard records into telemetry properties -- counts and enums only.

The one place that reads a ``runs.jsonl`` entry for telemetry. It extracts
numbers and closed-vocabulary values and nothing else: no task text, no
file names, no repository identity, no model slug (only its public family).
"""

from __future__ import annotations

from typing import Any

from openshard.history.shard import derive_shard_identity
from openshard.telemetry.schema import AGENTS, model_family

_EXECUTOR_AGENTS: dict[str, str] = {
    "claude_code_hooks": "claude_code",
    "codex_hooks": "codex",
    "opencode_plugin": "opencode",
    "cursor_hooks": "cursor",
    "antigravity_hooks": "antigravity",
    "grok_build_hooks": "grok_build",
    "hermes_hooks": "hermes",
    "claude_code_wrap": "wrap",
    "claude_code_import": "import",
    "native": "native",
}

_ERROR_CATEGORIES: tuple[tuple[type[BaseException], str], ...] = (
    (TimeoutError, "timeout"),
    (PermissionError, "permission"),
    (FileNotFoundError, "io"),
    (IsADirectoryError, "io"),
    (OSError, "io"),
    (ValueError, "parse"),
)


def error_category(exc: BaseException | None) -> str:
    """A closed category for an exception -- never its message or type name."""
    if exc is None:
        return "unknown"
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if name in ("UsageError", "BadParameter", "MissingParameter", "NoSuchOption", "BadOptionUsage", "BadArgumentUsage"):
        return "usage"
    for cls, category in _ERROR_CATEGORIES:
        if isinstance(exc, cls):
            return category
    if "JSON" in name or "Decode" in name or "Parse" in name:
        return "parse"
    return "unknown"


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def receipt_properties(entry: dict[str, Any]) -> dict[str, Any]:
    """``receipt.created`` / ``receipt.completed`` properties for one run entry."""
    raw_capture = entry.get("capture")
    capture: dict[str, Any] = raw_capture if isinstance(raw_capture, dict) else {}
    executor = str(entry.get("executor") or "")
    agent = capture.get("agent") if isinstance(capture.get("agent"), str) else _EXECUTOR_AGENTS.get(executor)
    if agent not in AGENTS:
        agent = "native" if entry.get("workflow") == "native" else "other"
    _label, origin, depth = derive_shard_identity(entry)

    files_source_raw = str(entry.get("files_source") or "")
    if files_source_raw == "git_diff_inferred":
        files_source = "git_diff"
    elif files_source_raw.endswith("_reported") or files_source_raw == "hook_reported":
        files_source = "hook_reported"
    elif files_source_raw in ("", "not_available"):
        files_source = "not_available"
    else:
        files_source = "other"

    attempted = entry.get("verification_attempted")
    passed = entry.get("verification_passed")
    if attempted is True and passed is True:
        checks = "passed"
    elif attempted is True and passed is False:
        checks = "failed"
    elif attempted is True:
        checks = "attempted_unverified"
    else:
        checks = "none"

    attempt = entry.get("attempt_number")
    attempt_number = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1 else 1
    duration = entry.get("duration_seconds")
    cost = entry.get("estimated_cost")
    return {
        "agent": agent,
        "origin": origin,
        "capture_depth": depth,
        "files_changed": _int_or_zero(entry.get("files_created")) + _int_or_zero(entry.get("files_updated"))
        + _int_or_zero(entry.get("files_deleted")),
        "files_source": files_source,
        "tool_calls": _int_or_zero(capture.get("tool_call_count")),
        "tool_failures": _int_or_zero(capture.get("tool_failure_count")),
        "checks": checks,
        "attempt_number": min(attempt_number, 1_000),
        "is_retry": attempt_number > 1,
        "turn_count": _int_or_zero(capture.get("turn_count")),
        "duration_s": _int_or_zero(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        "cost_usd": round(float(cost), 2) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        "model_family": model_family(entry.get("execution_model")),
    }
