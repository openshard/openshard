"""Bounded projections of control / proof / cost evidence Core already stores.

Everything here reads fields a ``runs.jsonl`` record already carries and
re-validates them on the way out (the privacy/shape boundary is enforced
here, not trusted because the value came from a record): free text is
capped, malformed values become ``None``, counts replace path lists, and
nothing that names a local path, argv or prompt is ever copied. No new
capture happens here.

Every projector returns ``None`` when nothing usable was recorded -- never
an empty list or a default -- so a consumer can tell "not recorded" from
"recorded as empty". Projectors never raise.

Wire shapes are documented in the Platform contract
(``packages/contracts/src/receipt-sync.ts``); the keys are emitted only by
the extended projection (``history.views.receipt_to_dict(extended=True)``).
"""

from __future__ import annotations

import math
import re
from typing import Any

MAX_TEXT = 300
MAX_POLICY_DECISIONS = 20
MAX_LOOP_ATTEMPTS = 10
MAX_ROLES = 3

_DECISIONS = frozenset({"allow", "ask", "deny", "not_applicable"})
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROLES = ("planner", "executor", "validator")


# The Platform rejects a whole receipt when any string leaf looks like an
# absolute path or a secret, so a free-text value that does is dropped here
# (that one field becomes ``None``; the rest of its block is kept).
_SECRET_PATTERNS = tuple(
    re.compile(p, flags)
    for p, flags in (
        (r"sk-[A-Za-z0-9_-]{8,}", 0),
        (r"AKIA[0-9A-Z]{8,}", 0),
        (r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}", 0),
        (r"github_pat_[A-Za-z0-9_]{20,}", 0),
        (r"xox[abpr]-[A-Za-z0-9-]{10,}", 0),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0),
        (r"\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+", re.IGNORECASE),
        (r"\bbearer\s+\S+", re.IGNORECASE),
    )
)
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")


def unsafe_text(value: str) -> bool:
    """True when *value* looks like an absolute path or carries a secret-shaped token."""
    v = value.strip()
    if v.startswith(("/", "\\")) or v.lower().startswith("file://") or _DRIVE_PATH_RE.match(v):
        return True
    return any(p.search(v) for p in _SECRET_PATTERNS)


def _text(value: Any, limit: int) -> str | None:
    """A bounded, non-empty, path- and secret-free string, else ``None``."""
    if not isinstance(value, str) or not value.strip() or unsafe_text(value):
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _stamp(value: Any) -> str | None:
    text = _text(value, 64)
    return text if text is not None and _STAMP_RE.match(text) else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _all_none(d: dict[str, Any]) -> bool:
    return all(v is None for v in d.values())


def policy_decisions_block(decisions: Any) -> list[dict[str, Any]] | None:
    """Gate/policy outcomes. ``decision_id``, ``resource`` and ``scope`` are never sent."""
    if not isinstance(decisions, list):
        return None
    out: list[dict[str, Any]] = []
    for raw in decisions:
        if not isinstance(raw, dict) or raw.get("decision") not in _DECISIONS:
            continue
        out.append({
            "decision": raw["decision"],
            "action": _text(raw.get("action"), 64),
            "source": _text(raw.get("source"), 64),
            "severity": _text(raw.get("severity"), 32),
            "reason": _text(raw.get("reason"), MAX_TEXT),
            "approval_required": _bool(raw.get("approval_required")),
            "approval_granted": _bool(raw.get("approval_granted")),
            "created_at": _stamp(raw.get("created_at")),
        })
        if len(out) >= MAX_POLICY_DECISIONS:
            break
    return out or None


def approval_detail_block(entry: dict) -> dict[str, Any] | None:
    request = _dict(entry.get("approval_request"))
    receipt = _dict(entry.get("approval_receipt"))
    if not request and not receipt:
        return None
    block = {
        "requires_approval": _bool(request.get("requires_approval")),
        "request_source": _text(request.get("source"), 64),
        "request_action": _text(request.get("action"), 64),
        "request_reason": _text(request.get("reason"), MAX_TEXT),
        "proposed_files": _count(request.get("proposed_files")),
        "decision_source": _text(receipt.get("source"), 64),
        "granted": _bool(receipt.get("granted")),
        "decision_action": _text(receipt.get("action"), 64),
        "decision_reason": _text(receipt.get("reason"), MAX_TEXT),
    }
    return None if _all_none(block) else block


def sandbox_detail_block(entry: dict) -> dict[str, Any] | None:
    """Isolation evidence. ``worktree_path`` and the display name are never sent."""
    sandbox = _dict(entry.get("sandbox"))
    if not sandbox:
        return None
    block = {
        "enabled": _bool(sandbox.get("sandbox_enabled")),
        "type": _text(sandbox.get("sandbox_type"), 32),
        "fallback_reason": _text(sandbox.get("fallback_reason"), MAX_TEXT),
    }
    return None if _all_none(block) else block


def execution_loop_block(entry: dict) -> dict[str, Any] | None:
    """OSN bounded-loop summary: counts and the loop's own provenance labels, never paths."""
    loop = _dict(entry.get("osn_loop"))
    if not loop:
        return None
    attempts: list[dict[str, int]] = []
    raw_attempts = loop.get("attempts")
    if isinstance(raw_attempts, list):
        for a in raw_attempts[:MAX_LOOP_ATTEMPTS]:
            if not isinstance(a, dict):
                continue
            n = _count(a.get("n"))
            if n is None:
                continue
            attempts.append({
                "n": n,
                "proposed_count": len(a["proposed"]) if isinstance(a.get("proposed"), list) else 0,
                "applied_count": len(a["applied"]) if isinstance(a.get("applied"), list) else 0,
                "blocked_count": len(a["blocked"]) if isinstance(a.get("blocked"), list) else 0,
            })
    ev = _dict(loop.get("evidence"))
    evidence = {
        "actions": _text(ev.get("actions"), 64),
        "policy_and_file_effects": _text(ev.get("policy_and_file_effects"), 64),
        "verification": _text(ev.get("verification"), 64),
    }
    block = {
        "status": _text(loop.get("status"), 32),
        "stop_reason": _text(loop.get("stop_reason"), 120),
        "verification_state": _text(loop.get("verification_state"), 32),
        "attempts": attempts or None,
        "evidence": None if _all_none(evidence) else evidence,
    }
    return None if _all_none(block) else block


def base_commit_value(entry: dict) -> str | None:
    """HEAD at run/session start (every producer records it then): a base, not a result."""
    value = entry.get("git_head_commit_hash")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _SHA_RE.match(value) else None


def content_hash_value(entry: dict) -> str | None:
    value = entry.get("content_hash")
    return value if isinstance(value, str) and _CONTENT_HASH_RE.match(value) else None


def session_block(entry: dict) -> dict[str, Any] | None:
    capture = _dict(entry.get("capture"))
    if not capture:
        return None
    block = {
        "started_at": _stamp(capture.get("started_at")),
        "first_prompt_at": _stamp(capture.get("first_prompt_at")),
        "last_turn_completed_at": _stamp(capture.get("last_turn_completed_at")),
        "last_activity_at": _stamp(capture.get("last_activity_at")),
        "ended": _bool(capture.get("session_end_observed")),
        "end_reason": _text(capture.get("session_end_reason"), 64),
        "start_source": _text(capture.get("start_source"), 64),
        "prompt_count": _count(capture.get("prompt_count")),
        "turn_count": _count(capture.get("turn_count")),
        "tool_call_count": _count(capture.get("tool_call_count")),
        "tool_failure_count": _count(capture.get("tool_failure_count")),
    }
    return None if _all_none(block) else block


def routing_block(entry: dict) -> dict[str, Any] | None:
    """How the run routed, from Core's own routing truth. Only when routing was recorded.

    A role model is advisory unless ``dispatched`` is true; Core's routing
    truth sets that flag only when the run records that the role ran.
    """
    from openshard.history.routing_truth import (
        ROLE_UNAVAILABLE,
        ROUTING_UNKNOWN,
        build_routing_truth,
    )

    truth = build_routing_truth(entry)
    provider = _text(entry.get("routing_selected_provider"), 128)
    if truth.routing_mode == ROUTING_UNKNOWN and truth.role_selection_mode == ROLE_UNAVAILABLE and provider is None:
        return None
    roles: list[dict[str, Any]] = []
    for role in _ROLES:
        model = _text(getattr(truth, f"{role}_model"), 256)
        if model is not None:
            roles.append({"role": role, "model": model, "dispatched": bool(getattr(truth, f"{role}_dispatched"))})
    tdr = _dict(entry.get("tier_dispatch_receipt"))
    return {
        "mode": _text(truth.routing_mode, 64),
        "selection_source": _text(truth.selection_source, 64),
        "runtime_model": _text(truth.runtime_model, 256),
        "provider": provider,
        "role_dispatch_status": _text(truth.role_dispatch_status, 64),
        "role_selection_mode": _text(truth.role_selection_mode, 64),
        "roles": roles[:MAX_ROLES] or None,
        "fallback_used": _bool(tdr.get("fallback_used")),
        "fallback_reason": _text(tdr.get("fallback_reason"), MAX_TEXT),
    }


def retry_block(entry: dict) -> dict[str, Any] | None:
    block = {
        "triggered": _bool(entry.get("retry_triggered")),
        "fixer_model": _text(entry.get("fixer_model"), 256),
        "total_tokens": _count(entry.get("retry_total_tokens")),
        "cost_usd": _number(entry.get("retry_estimated_cost")),
    }
    return None if _all_none(block) else block


def stage_metrics(entry: dict) -> list[dict[str, float | None]]:
    """Per-stage duration/cost, parallel to the ``model_stages`` built from ``stage_runs``."""
    runs = entry.get("stage_runs")
    if not isinstance(runs, list):
        return []
    return [
        {"duration_seconds": _number(s.get("duration")), "cost_usd": _number(s.get("cost"))}
        for s in runs
        if isinstance(s, dict) and "stage_type" in s and "model" in s
    ]


def project_entry_evidence(entry: Any) -> dict[str, Any]:
    """Every entry-derived block in one dict (``ShardReceipt.recorded_evidence``). Never raises."""
    if not isinstance(entry, dict):
        return {}
    projectors = {
        "approval_detail": approval_detail_block,
        "sandbox_detail": sandbox_detail_block,
        "execution_loop": execution_loop_block,
        "base_commit": base_commit_value,
        "content_hash": content_hash_value,
        "session": session_block,
        "routing": routing_block,
        "retry": retry_block,
    }
    out: dict[str, Any] = {}
    for key, fn in projectors.items():
        try:
            out[key] = fn(entry)
        except Exception:
            out[key] = None
    try:
        out["model_stage_metrics"] = stage_metrics(entry)
    except Exception:
        out["model_stage_metrics"] = []
    return out


__all__ = [
    "approval_detail_block",
    "base_commit_value",
    "content_hash_value",
    "execution_loop_block",
    "policy_decisions_block",
    "project_entry_evidence",
    "retry_block",
    "routing_block",
    "sandbox_detail_block",
    "session_block",
    "stage_metrics",
    "unsafe_text",
]
