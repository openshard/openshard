"""Renderers for the local visibility commands (PR9).

    openshard last      -- what just happened
    openshard history   -- recent work
    openshard context   -- what OpenShard would surface for a task, and why
    openshard stats     -- honest counts over recorded history

Pure functions only: each takes canonical history objects and returns the
lines to print or the ``--json`` body. No I/O, no click, so they can be
unit-tested directly and reused by the TUI later. The click commands that
call them live in ``openshard.cli.main``.

Visual language (shared with ``setup`` / ``doctor``): a short heading, then
two-space-indented ``Label   value`` rows; plain English; no banners.
Missing data is always said out loud (``not recorded``, ``unknown``) and
every cost figure is labelled an estimate.
"""

from __future__ import annotations

from datetime import datetime

from openshard.history.locate import HistoryLocation
from openshard.history.proof_signals import verification_status_from_receipt
from openshard.history.query import HistoryPage, RecentShard, RelevantContext, RelevantMatch
from openshard.history.shard import (
    CAPTURE_FULL,
    CAPTURE_PARTIAL,
    ORIGIN_EXTERNAL_OBSERVED,
    ORIGIN_OPENSHARD_ROUTED,
)
from openshard.history.shard_contract import _EM, _UNICODE_OK, ShardReceipt, _format_token_count
from openshard.history.stats import HistoryStats
from openshard.history.views import receipt_to_dict, relevant_match_to_dict

_INDENT = "  "
_COL = 15
# Same stdout-encoding guard the receipt renderer uses, so a cp1252 console
# never crashes on a separator glyph.
_DOT = "·" if _UNICODE_OK else "|"
_ARROW = "→" if _UNICODE_OK else "->"
_TIMES = "×" if _UNICODE_OK else "x"

_ORIGIN_TEXT: dict[str, str] = {
    ORIGIN_OPENSHARD_ROUTED: "OpenShard ran it",
    ORIGIN_EXTERNAL_OBSERVED: "observed externally, not executed by OpenShard",
}
_CAPTURE_TEXT: dict[str, str] = {
    CAPTURE_FULL: "full capture",
    CAPTURE_PARTIAL: "partial capture",
}


# ---------------------------------------------------------------------------
# small formatting helpers
# ---------------------------------------------------------------------------


def _row(label: str, value: str, width: int = _COL) -> str:
    return f"{_INDENT}{label:<{width}}{value}"


def _join(parts: list[str]) -> str:
    return f" {_DOT} ".join(parts)


def fmt_time(iso: str | None) -> str:
    """``2026-09-02T14:03:11Z`` -> ``2026-09-02 14:03``; unparsable input is echoed."""
    if not iso:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_day(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return iso


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "not recorded"
    total = int(round(seconds))
    if total < 60:
        return f"{seconds:.1f}s"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def cost_label(receipt: ShardReceipt) -> str:
    """Cost with an explicit estimate marker, or ``not recorded``.

    Every cost OpenShard knows is an estimate (OpenShard's own figure is
    tokens x list price; agent-reported figures are documented as
    approximate), so the marker is never dropped here.
    """
    if receipt.cost_raw is None:
        return "not recorded"
    display = receipt.cost_display
    return display if display.endswith("est.") else f"{display} est."


def status_label(receipt: ShardReceipt) -> str:
    """The most truthful one-word-ish status for a row.

    Claude Code captures carry a turn-completion signal (``Completed`` /
    ``In progress``) that is *not* verification; native runs carry the
    verification-derived status (``Passed`` / ``Failed`` / ``No checks run``).
    """
    if receipt.task_completion:
        return receipt.task_completion
    status = receipt.status or "Not recorded"
    if status.startswith("Checks:"):
        # Review-style checks carry their detail in the checks column already.
        return "Failed" if verification_status_from_receipt(receipt) == "failed" else "Passed"
    return status


def checks_label(receipt: ShardReceipt) -> str:
    display = (receipt.checks_display or "Not recorded").strip()
    return display[:1].lower() + display[1:] if display else "not recorded"


def model_label(receipt: ShardReceipt) -> str:
    if receipt.model_stages:
        unique = list(dict.fromkeys(m for _, m in receipt.model_stages))
        return f" {_ARROW} ".join(unique) if len(unique) <= 2 else ", ".join(unique)
    display = (receipt.model_display or "").strip()
    if not display or display.lower() in ("unknown", "not recorded"):
        return "unknown"
    return display


def repo_heading(loc: HistoryLocation) -> str:
    return loc.display_name


def repo_note_lines(loc: HistoryLocation) -> list[str]:
    """One explanatory line when history was resolved above the current directory."""
    if not loc.from_subdirectory:
        return []
    return [f"{_INDENT}History read from the repository root ({loc.repo_name})."]


def no_history_lines(loc: HistoryLocation) -> list[str]:
    return [
        f"No run history found for {loc.display_name}.",
        f"{_INDENT}Run `openshard setup`, use Claude Code normally, then come back --",
        f"{_INDENT}or record a task directly with `openshard run \"...\"`.",
    ]


# ---------------------------------------------------------------------------
# openshard history
# ---------------------------------------------------------------------------


def _history_row_lines(item: RecentShard) -> list[str]:
    r = item.receipt
    parts = [r.agent, status_label(r), f"checks: {checks_label(r)}", f"cost: {cost_label(r)}"]
    if r.files_changed:
        parts.append(f"{r.files_changed} file{'s' if r.files_changed != 1 else ''}")
    if item.attempt_count > 1:
        parts.append(f"{item.attempt_count} attempts")
    if item.shard.capture_depth == CAPTURE_PARTIAL:
        parts.append("partial capture")
    return [
        f"{_INDENT}{fmt_time(r.created_at)}  {r.shard_id}",
        f"{_INDENT}  {r.task_short or 'Task not recorded'}",
        f"{_INDENT}  {_join(parts)}",
    ]


def render_history(page: HistoryPage, loc: HistoryLocation) -> list[str]:
    lines = [f"Recent work {_EM} {repo_heading(loc)}"]
    lines += repo_note_lines(loc)
    shown = len(page.items)
    noun = "Shard" if page.total_shards == 1 else "Shards"
    attempts = "" if page.total_attempts == page.total_shards else f", {page.total_attempts} attempts"
    lines.append(f"{_INDENT}Showing {shown} of {page.total_shards} {noun}{attempts}, newest first.")
    for item in page.items:
        lines.append("")
        lines += _history_row_lines(item)
    lines.append("")
    lines.append(f"{_INDENT}Costs are estimates. `openshard last` shows the newest receipt in full.")
    return lines


def history_json_body(page: HistoryPage, loc: HistoryLocation) -> dict:
    return {
        "repo": loc.to_dict(),
        "total_shards": page.total_shards,
        "total_attempts": page.total_attempts,
        "shown": len(page.items),
        "shards": [
            {
                **receipt_to_dict(item.receipt, extended=True),
                "attempt_count": item.attempt_count,
                "has_failure": item.has_failure,
            }
            for item in page.items
        ],
    }


# ---------------------------------------------------------------------------
# openshard context
# ---------------------------------------------------------------------------


def _evidence_line(match: RelevantMatch) -> str:
    shard = match.shard
    origin = _ORIGIN_TEXT.get(shard.origin, "origin unknown")
    capture = _CAPTURE_TEXT.get(shard.capture_depth, "capture depth unknown")
    return f"{shard.agent} {_DOT} {origin} {_DOT} {capture}"


def _match_lines(index: int, match: RelevantMatch) -> list[str]:
    col = 14
    ind = f"{_INDENT}   "

    def row(label: str, value: str) -> str:
        return f"{ind}{label:<{col}}{value}"

    lines = [f"{_INDENT}{index}. {match.shard.shard_id} {_EM} {match.shard.task_short}"]
    lines.append(row("Why matched", f"{'; '.join(match.signals) or 'no signal recorded'}  (score {match.score})"))
    status = match.status or "Not recorded"
    if match.verification_status:
        status += f" {_DOT} verification: {match.verification_status}"
        if match.verification_reason:
            status += f" {_EM} {match.verification_reason}"
    lines.append(row("Status", status))
    if match.result:
        lines.append(row("Result", match.result))
    if len(match.attempts) > 1:
        seq = f" {_ARROW} ".join(
            f"{a.attempt_number if a.attempt_number is not None else n}: {a.status}"
            for n, a in enumerate(match.attempts, start=1)
        )
        lines.append(row("Attempts", seq))
    if match.files:
        lines.append(row("Files", ", ".join(match.files)))
    for f in match.findings:
        lines.append(row("Finding", f"[{f.severity}] {f.message}"))
    lines.append(row("Evidence", _evidence_line(match)))
    return lines


def render_context(ctx: RelevantContext, loc: HistoryLocation, total_shards: int, ranking: dict) -> list[str]:
    task = ctx.task.strip()
    lines = [f'Context for: "{task}"' if task else "Context"]
    lines += repo_note_lines(loc)
    if not task:
        lines.append(f"{_INDENT}No task given {_EM} nothing to match against local history.")
        return lines
    noun = "Shard" if total_shards == 1 else "Shards"
    if not ctx.matches:
        lines.append(
            f"{_INDENT}No relevant prior work: none of the {total_shards} recorded {noun} "
            "shares a keyword with this task."
        )
    else:
        n = len(ctx.matches)
        lines.append(
            f"{_INDENT}{n} of {total_shards} recorded {noun} matched {_EM} this is what an agent "
            "would be given, ranked."
        )
        for i, match in enumerate(ctx.matches, start=1):
            lines.append("")
            lines += _match_lines(i, match)
    lines.append("")
    lines += ranking_lines(ranking)
    return lines


def ranking_lines(ranking: dict) -> list[str]:
    weights = ranking.get("weights", {})
    bonuses = ranking.get("bonuses", {})
    return [
        f"{_INDENT}How ranking works",
        f"{_INDENT}  {ranking.get('method', 'deterministic evidence scoring')}.",
        (
            f"{_INDENT}  File overlap: touched {_TIMES}{weights.get('file_touched', '?')}, "
            f"referenced by a finding {_TIMES}{weights.get('file_referenced', '?')}."
        ),
        (
            f"{_INDENT}  Keyword overlap: task text {_TIMES}{weights.get('task_text', '?')}, "
            f"shard id {_TIMES}{weights.get('shard_id', '?')}, agent {_TIMES}{weights.get('agent', '?')}."
        ),
        (
            f"{_INDENT}  Bonuses: +{bonuses.get('prior_verification_failure', '?')} for a recorded failure, "
            f"+{bonuses.get('multiple_attempts', '?')} for retries, "
            f"+{bonuses.get('resolved_after_failure', '?')} when a later attempt passed after an earlier failure."
        ),
        (
            f"{_INDENT}  Reads only: {', '.join(ranking.get('fields_read', []))}. "
            f"Never: {', '.join(ranking.get('fields_never_read', []))}."
        ),
    ]


def context_json_body(ctx: RelevantContext, loc: HistoryLocation, total_shards: int, ranking: dict) -> dict:
    return {
        "repo": loc.to_dict(),
        "task": ctx.task,
        "total_shards": total_shards,
        "matched": len(ctx.matches),
        "matches": [relevant_match_to_dict(m) for m in ctx.matches],
        "context_text": ctx.context_text,
        "ranking": ranking,
    }


# ---------------------------------------------------------------------------
# openshard stats
# ---------------------------------------------------------------------------


def _counts(counts: dict[str, int], *, labels: dict[str, str] | None = None) -> str:
    if not counts:
        return "none recorded"
    labels = labels or {}
    return _join([f"{labels.get(k, k)} {v}" for k, v in counts.items()])


_VERIFICATION_LABELS = {
    "passed": "passed",
    "failed": "failed",
    "not_run": "not run",
    "skipped": "skipped",
    "manual_review": "manual review",
    "unknown": "not recorded",
}
_COMPLETION_LABELS = {
    "completed": "completed",
    "in_progress": "in progress",
    "ended_no_turn": "ended without a turn",
}
_CAPTURE_LABELS = {CAPTURE_FULL: "full", CAPTURE_PARTIAL: "partial", "unknown": "unknown"}
_ORIGIN_LABELS = {
    ORIGIN_OPENSHARD_ROUTED: "run by OpenShard",
    ORIGIN_EXTERNAL_OBSERVED: "observed externally",
    "unknown": "origin unknown",
}


def render_stats(stats: HistoryStats, loc: HistoryLocation, *, limited_to: int | None = None) -> list[str]:
    lines = [f"OpenShard stats {_EM} {repo_heading(loc)}"]
    lines += repo_note_lines(loc)
    if limited_to is not None:
        lines.append(f"{_INDENT}Most recent {limited_to} Shards only.")
    shard_word = "Shard" if stats.shards == 1 else "Shards"
    extra = []
    if stats.attempts != stats.shards:
        extra.append(f"{stats.attempts} attempts")
    if stats.retried_shards:
        extra.append(f"{stats.retried_shards} retried")
    lines.append(_row("Shards", f"{stats.shards} {shard_word}" + (f" ({', '.join(extra)})" if extra else "")))
    if stats.first_at or stats.last_at:
        first, last = fmt_day(stats.first_at), fmt_day(stats.last_at)
        lines.append(_row("Period", first if first == last else f"{first} {_ARROW} {last}"))
    lines.append("")
    lines.append(_row("Agents", _counts(stats.agents)))
    lines.append(_row("Origin", _counts(stats.origins, labels=_ORIGIN_LABELS)))
    lines.append(_row("Capture", _counts(stats.capture_depths, labels=_CAPTURE_LABELS)))
    lines.append(_row("Models", _counts(stats.models)))
    lines.append(_row("Verification", _counts(stats.verification, labels=_VERIFICATION_LABELS)))
    if stats.task_completion:
        lines.append(_row("Turn status", _counts(stats.task_completion, labels=_COMPLETION_LABELS)
                          + " (Claude Code sessions; not verification)"))
    lines.append("")

    if stats.cost_total_usd is None:
        cost = f"not recorded for any of {stats.shards} {shard_word}"
    else:
        cost = f"${stats.cost_total_usd:.2f} estimated across {stats.cost_shards} {shard_word}"
        if stats.cost_provider_reported_shards:
            cost += f" ({stats.cost_provider_reported_shards} agent-reported)"
        if stats.cost_missing_shards:
            cost += f"; not recorded for {stats.cost_missing_shards}"
    lines.append(_row("Cost", cost))

    if stats.tokens_shards:
        tok = (
            f"{_format_token_count(stats.tokens_input or 0)} input / "
            f"{_format_token_count(stats.tokens_output or 0)} output"
        )
        if stats.tokens_cache_read:
            tok += f" (+{_format_token_count(stats.tokens_cache_read)} cache read)"
        tok += f" {_EM} provider-reported for {stats.tokens_shards} {shard_word}"
    else:
        tok = "no provider-reported token counts"
    lines.append(_row("Tokens", tok))

    if stats.duration_total_seconds is not None:
        dur = f"{fmt_duration(stats.duration_total_seconds)} observed across {stats.duration_shards} {shard_word}"
    else:
        dur = "not recorded"
    lines.append(_row("Duration", dur))

    if stats.files_changed_total:
        files = f"{stats.files_changed_total} across {stats.files_changed_shards} {shard_word}"
    else:
        files = "none recorded"
    lines.append(_row("Files changed", files))
    if stats.top_files:
        lines.append(f"{_INDENT}  most often: " + ", ".join(f"{p} {_TIMES}{n}" for p, n in stats.top_files))
    lines.append("")
    lines.append(f"{_INDENT}These are counts of what was recorded, not a judgement of the work.")
    return lines


def stats_json_body(stats: HistoryStats, loc: HistoryLocation, *, limited_to: int | None = None) -> dict:
    return {"repo": loc.to_dict(), "limit": limited_to, **stats.to_dict()}
