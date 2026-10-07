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

from openshard.history.run_cost import run_total_cost, stored_retry_attempts

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


_PERMISSION_SCOPE_BY_ACTION = {
    "file_write": "repo:write",
    "command_exec": "verification:execute",
    "read_only_review": "repo:read",
}
_PERMISSION_STATE_RANK = {"granted": 0, "requested": 1, "blocked": 2}


def permission_scopes_block(decisions: Any) -> list[dict[str, str]] | None:
    """Privacy-safe capability outcomes derived from actual policy decisions.

    The scope names are fixed vocabulary; resource paths and raw commands are
    never copied. When multiple decisions touch one capability, the strictest
    observed state wins.
    """
    if not isinstance(decisions, list):
        return None
    states: dict[str, str] = {}
    for raw in decisions:
        if not isinstance(raw, dict):
            continue
        action = raw.get("action")
        if not isinstance(action, str):
            continue
        scope = _PERMISSION_SCOPE_BY_ACTION.get(action)
        if scope is None:
            continue
        decision = raw.get("decision")
        if decision == "deny":
            state = "blocked"
        elif decision == "ask":
            granted = raw.get("approval_granted")
            state = "granted" if granted is True else "blocked" if granted is False else "requested"
        elif decision == "allow":
            state = "granted"
        else:
            continue
        previous = states.get(scope)
        if previous is None or _PERMISSION_STATE_RANK[state] > _PERMISSION_STATE_RANK[previous]:
            states[scope] = state
    return [{"scope": scope, "state": states[scope]} for scope in sorted(states)] or None


_PERMISSION_SCOPES = frozenset({"repo:read", "repo:write", "verification:execute"})
_PERMISSION_STATES = frozenset({"granted", "requested", "blocked"})


def permission_evidence_block(entry: dict) -> list[dict[str, str]] | None:
    """Validated explicit permission evidence recorded by a controlled runtime."""
    raw = entry.get("permission_evidence")
    if not isinstance(raw, list):
        return None
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        state = item.get("state")
        if not isinstance(scope, str) or scope not in _PERMISSION_SCOPES:
            continue
        if not isinstance(state, str) or state not in _PERMISSION_STATES:
            continue
        if scope in seen:
            continue
        seen.add(scope)
        out.append({"scope": scope, "state": state})
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
    attempts: list[dict[str, Any]] = []
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
    block: dict[str, Any] = {
        "status": _text(loop.get("status"), 32),
        "stop_reason": _text(loop.get("stop_reason"), 120),
        "verification_state": _text(loop.get("verification_state"), 32),
        "attempts": attempts or None,
        "evidence": None if _all_none(evidence) else evidence,
    }
    return None if _all_none(block) else block


def agent_loop_block(entry: dict) -> dict[str, Any] | None:
    """The iterative agent loop of an OSN run: turns, action counts and every model call.

    Local Receipt surfaces only for now. The hosted sync contract
    (``packages/contracts/src/receipt-sync.ts``) validates ``execution_loop``
    strictly and does not yet know these keys, so they are kept out of that
    block and out of the hosted projection; nothing here names a path, a
    prompt or tool output.
    """
    loop = _dict(entry.get("osn_loop"))
    if not loop or loop.get("mode") != "turns":
        return None
    attempts: list[dict[str, Any]] = []
    raw_attempts = loop.get("attempts")
    if isinstance(raw_attempts, list):
        for a in raw_attempts[:MAX_LOOP_ATTEMPTS]:
            if not isinstance(a, dict) or _count(a.get("n")) is None:
                continue
            attempts.append({
                "n": _count(a.get("n")),
                "turns": _count(a.get("turns")),
                "turn_stop": _text(a.get("turn_stop"), 32),
                "verifications_in_turn": _count(a.get("verifications_in_turn")),
                "action_summary": _action_counts(a.get("action_summary")),
            })
    ev = _dict(loop.get("evidence"))
    block: dict[str, Any] = {
        "mode": "turns",
        "turns_total": _count(loop.get("turns_total")),
        "action_summary": _action_counts(loop.get("action_summary")),
        "attempts": attempts or None,
        "model_calls": model_calls_block(loop.get("model_calls")),
        "model_calls_truncated": _bool(loop.get("model_calls_truncated")),
        "roles": roles_block(loop.get("roles")),
        "plan": plan_block(loop.get("plan")),
        "reviews": reviews_block(loop.get("reviews")),
        "topology": topology_block(loop.get("topology")),
        "workers": workers_block(loop.get("workers")),
        "synthesis": synthesis_block(loop.get("synthesis")),
        "economics": economics_block(loop.get("economics")),
        "candidates": candidates_block(loop.get("candidates")),
        "agents": agents_block(loop.get("agents")),
        "resumed": resumed_block(loop.get("resumed")),
        "evidence": {
            "actions": _text(ev.get("actions"), 64),
            "action_results": _text(ev.get("action_results"), 64),
            "reviews": "model_reported" if loop.get("reviews") else None,
        },
    }
    return block


_TOPOLOGIES = frozenset({"single", "planner_executor", "planner_executor_verifier", "parallel_subtasks",
                         "parallel_candidates"})
_WORKER_STATUSES = frozenset({"changed", "no_change", "failed", "blocked"})
MAX_WORKERS_PROJECTED = 3


def topology_block(raw: Any) -> dict[str, Any] | None:
    """The execution topology decision: requested, selected, why, how many workers, expected and actual extra cost."""
    d = _dict(raw)
    if not d:
        return None
    selected = d.get("topology_selected")
    return {
        "requested": _text(d.get("topology_requested"), 16),
        "selected": selected if selected in _TOPOLOGIES else None,
        "reason": _text(d.get("topology_reason"), 80),
        "worker_count": _count(d.get("worker_count")),
        "distinct_models": _count(d.get("distinct_models")),
        "expected_extra_cost_usd": _number(d.get("expected_extra_cost_usd")),
        "actual_extra_cost_usd": _number(d.get("actual_extra_cost_usd")),
    }


def workers_block(raw: Any) -> list[dict[str, Any]] | None:
    """Parallel writing workers: outcome, model, scope and usage per worker; file counts, never paths."""
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, Any]] = []
    for w in raw[:MAX_WORKERS_PROJECTED]:
        if not isinstance(w, dict):
            continue
        status = w.get("status")
        verification = _dict(w.get("verification"))
        out.append({
            "worker_id": _text(w.get("worker_id"), 32),
            "subtask_id": _text(w.get("subtask_id"), 32),
            "status": status if status in _WORKER_STATUSES else None,
            "reason": _text(w.get("reason"), 64),
            "required": _bool(w.get("required")),
            "model": _text(w.get("model"), 256),
            "requested_model": _text(w.get("requested_model"), 256),
            "model_source": _text(w.get("model_source"), 32),
            "files_changed": len(w["changed_files"]) if isinstance(w.get("changed_files"), list) else None,
            "files_blocked": len(w["blocked"]) if isinstance(w.get("blocked"), list) else None,
            "turns": _count(w.get("turns")),
            "calls": _count(w.get("calls")),
            "prompt_tokens": _count(w.get("prompt_tokens")),
            "completion_tokens": _count(w.get("completion_tokens")),
            "cost_usd": _number(w.get("cost_usd")),
            "cost_source": _text(w.get("cost_source"), 32),
            "duration_ms": _count(w.get("duration_ms")),
            "own_copy_verification": _text(verification.get("status"), 16) if verification else None,
        })
    return out or None


def synthesis_block(raw: Any) -> dict[str, Any] | None:
    """How workers' results were combined: counts and the resolution route, never paths."""
    d = _dict(raw)
    if not d:
        return None
    return {
        "applied_count": len(d["applied"]) if isinstance(d.get("applied"), list) else None,
        "conflict_count": len(d["conflicts"]) if isinstance(d.get("conflicts"), list) else None,
        "rejected_count": len(d["rejected"]) if isinstance(d.get("rejected"), list) else None,
        "workers_accepted": len(d["workers_accepted"]) if isinstance(d.get("workers_accepted"), list) else None,
        "workers_rejected": len(d["workers_rejected"]) if isinstance(d.get("workers_rejected"), list) else None,
        "missing_required": len(d["missing_required"]) if isinstance(d.get("missing_required"), list) else None,
        "resolution": _text(d.get("resolution"), 32),
    }


def candidates_block(raw: Any) -> dict[str, Any] | None:
    """Parallel candidates: the policy, every candidate's rank and verification, the winner; never paths."""
    d = _dict(raw)
    if not d:
        return None
    evaluated: list[dict[str, Any]] = []
    for e in (d.get("evaluated") or [])[:MAX_WORKERS_PROJECTED]:
        if not isinstance(e, dict):
            continue
        evaluated.append({
            "worker_id": _text(e.get("worker_id"), 32),
            "model": _text(e.get("model"), 256),
            "rank": _count(e.get("rank")),
            "selected": _bool(e.get("selected")),
            "verified": _bool(e.get("verified")),
            "verification": _text(e.get("verification"), 16),
            "failed_tests": _count(e.get("failed_tests")),
            "files_changed": _count(e.get("files_changed")),
            "writes_refused": _count(e.get("writes_refused")),
            "cost_usd": _number(e.get("cost_usd")),
            "turns": _count(e.get("turns")),
        })
    return {
        "policy": _text(d.get("policy"), 96),
        "count": _count(d.get("count")),
        "winner": _text(d.get("winner"), 32),
        "winner_model": _text(d.get("winner_model"), 256),
        "reason": _text(d.get("reason"), 48),
        "candidates_cost_usd": _number(d.get("candidates_cost_usd")),
        "losers_cost_usd": _number(d.get("losers_cost_usd")),
        "evaluated": evaluated or None,
        "verification_evidence": _text(_dict(d.get("evidence")).get("verification"), 48),
    }


_AGENT_ROLES = frozenset({"planner", "executor", "verifier", "explorer", "worker", "candidate", "harness"})


def agents_block(raw: Any) -> dict[str, Any] | None:
    """The run's agent graph: ids, roles, models, outcomes and edges; never paths or prompts."""
    d = _dict(raw)
    if not d:
        return None
    nodes: list[dict[str, Any]] = []
    for n in (d.get("nodes") or [])[:16]:
        if not isinstance(n, dict):
            continue
        role = n.get("role")
        nodes.append({
            "agent_id": _text(n.get("agent_id"), 32),
            "role": role if role in _AGENT_ROLES else None,
            "parent": _text(n.get("parent"), 32),
            "status": _text(n.get("status"), 16),
            "reason": _text(n.get("reason"), 64),
            "model": _text(n.get("model"), 256),
            "independent": _bool(n.get("independent")),
            "calls": _count(n.get("calls")),
            "turns": _count(n.get("turns")),
            "cost_usd": _number(n.get("cost_usd")),
            "cost_source": _text(n.get("cost_source"), 32),
            "outcome": _text(n.get("outcome"), 96),
        })
    edges = [
        {"from": _text(e.get("from"), 32), "to": _text(e.get("to"), 32), "kind": _text(e.get("kind"), 32)}
        for e in (d.get("edges") or [])[:48] if isinstance(e, dict)
    ]
    return {
        "topology": _text(d.get("topology"), 32),
        "agents": _count(d.get("agents")),
        "models_distinct": [str(m)[:256] for m in (d.get("models_distinct") or [])[:8] if isinstance(m, str)],
        "cost_usd": _number(d.get("cost_usd")),
        "nodes": nodes or None,
        "edges": edges or None,
        "evidence": _text(_dict(d.get("evidence")).get("graph"), 48),
    }


def resumed_block(raw: Any) -> dict[str, Any] | None:
    """A run continued from a checkpoint: what was carried over, as counts and provenance; never paths."""
    d = _dict(raw)
    if not d:
        return None
    interrupted = _dict(d.get("interrupted"))
    return {
        "attempts_restored": _count(d.get("attempts_restored")),
        "plan_restored": _bool(d.get("plan_restored")),
        "topology_restored": _bool(d.get("topology_restored")),
        "files_restored": _count(d.get("files_restored")),
        "checkpoint_phase": _text(d.get("checkpoint_phase"), 32),
        "checkpoint_status": _text(d.get("checkpoint_status"), 16),
        "interrupted_reason": _text(interrupted.get("reason"), 64) if interrupted else None,
        "prior_model_calls": _count(d.get("prior_model_calls")),
        "prior_cost_usd": _number(d.get("prior_cost_usd")),
        "unsaved_progress_discarded": _bool(d.get("unsaved_progress_discarded")),
        "times_resumed": _count(d.get("times_resumed")),
        "evidence": _text(d.get("evidence"), 48),
    }


def economics_block(raw: Any) -> dict[str, Any] | None:
    """Cost per role / worker / model / attempt and per verified success; unknown stays None."""
    d = _dict(raw)
    if not d:
        return None

    def _map(key: str) -> dict[str, float | None] | None:
        m = _dict(d.get(key))
        if not m:
            return None
        return {str(k)[:64]: _number(v) if v is not None else None for k, v in list(m.items())[:12]}

    return {
        "total_cost_usd": _number(d.get("total_cost_usd")),
        "cost_complete": _bool(d.get("cost_complete")),
        "model_calls": _count(d.get("model_calls")),
        "verified": _bool(d.get("verified")),
        "cost_per_verified_success": _number(d.get("cost_per_verified_success")),
        "by_role": _map("by_role"),
        "by_worker": _map("by_worker"),
        "by_attempt": _map("by_attempt"),
    }


_ROLE_STATUSES = frozenset({"ran", "skipped", "failed"})


def roles_block(raw: Any) -> dict[str, Any] | None:
    """Per-role evidence: status (ran / skipped / failed and why), model, provider, usage, cost provenance."""
    d = _dict(raw)
    if not d:
        return None
    out: dict[str, Any] = {}
    for role in ("planner", "executor", "verifier"):
        rec = _dict(d.get(role))
        if not rec:
            continue
        status = rec.get("status")
        block: dict[str, Any] = {
            "status": status if status in _ROLE_STATUSES else None,
            "reason": _text(rec.get("reason"), 64),
            "model": _text(rec.get("model"), 256),
            "requested_model": _text(rec.get("requested_model"), 256),
            "provider": _text(rec.get("provider"), 64),
            "source": _text(rec.get("source"), 32),
            "independent": _bool(rec.get("independent")),
            "calls": _count(rec.get("calls")),
            "turns": _count(rec.get("turns")),
            "prompt_tokens": _count(rec.get("prompt_tokens")),
            "completion_tokens": _count(rec.get("completion_tokens")),
            "cost_usd": _number(rec.get("cost_usd")),
            "cost_source": _text(rec.get("cost_source"), 32),
            "duration_ms": _count(rec.get("duration_ms")),
            "usage_complete": _bool(rec.get("usage_complete")),
        }
        explorers = explorers_block(rec.get("explorers"))
        if explorers is not None:
            block["explorers"] = explorers
        out[role] = block
    return out or None


_EXPLORER_STATUSES = frozenset({"answered", "no_answer", "failed"})


def explorers_block(raw: Any) -> list[dict[str, Any]] | None:
    """Parallel read-only exploration workers: outcome, model, usage and cost per worker; never the question text's paths or findings."""
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, Any]] = []
    for r in raw[:MAX_EXPLORERS]:
        if not isinstance(r, dict):
            continue
        status = r.get("status")
        out.append({
            "index": _count(r.get("index")),
            "status": status if status in _EXPLORER_STATUSES else None,
            "reason": _text(r.get("reason"), 64),
            "model": _text(r.get("model"), 256),
            "turns": _count(r.get("turns")),
            "calls": _count(r.get("calls")),
            "findings_count": _count(r.get("findings_count")),
            "sources_count": len(r["sources"]) if isinstance(r.get("sources"), list) else None,
            "prompt_tokens": _count(r.get("prompt_tokens")),
            "completion_tokens": _count(r.get("completion_tokens")),
            "cost_usd": _number(r.get("cost_usd")),
            "cost_source": _text(r.get("cost_source"), 32),
            "duration_ms": _count(r.get("duration_ms")),
        })
    return out or None


MAX_EXPLORERS = 3


def plan_block(raw: Any) -> dict[str, Any] | None:
    d = _dict(raw)
    if not d:
        return None
    raw_steps, raw_files = d.get("steps"), d.get("files")
    return {
        "summary": _text(d.get("summary"), MAX_TEXT),
        "file_count": len(raw_files) if isinstance(raw_files, list) else None,
        "step_count": len(raw_steps) if isinstance(raw_steps, list) else 0,
        "simple": _bool(d.get("simple")),
    }


_VERDICTS = frozenset({"pass", "warn", "fail"})


def reviews_block(raw: Any) -> list[dict[str, Any]] | None:
    """Independent model reviews: verdict, summary, whether a recovery attempt followed and how it ended."""
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for r in raw[:4]:
        if not isinstance(r, dict) or r.get("verdict") not in _VERDICTS:
            continue
        out.append({
            "attempt": _count(r.get("attempt")),
            "verdict": r["verdict"],
            "summary": _text(r.get("summary"), MAX_TEXT),
            "concern_count": len(r["concerns"]) if isinstance(r.get("concerns"), list) else None,
            "model": _text(r.get("model"), 256),
            "independent": _bool(r.get("independent")),
            "evidence": "model_reported",
            "recovery_requested": _bool(r.get("recovery_requested")),
            "recovery_outcome": _text(r.get("recovery_outcome"), 40),
        })
    return out or None


_ACTION_COUNT_KEYS = (
    "actions", "reads", "searches", "listings", "diffs", "writes_proposed", "writes_applied",
    "writes_blocked", "verifications", "invalid",
)
MAX_MODEL_CALLS = 20
_ROLE_NAMES = frozenset({"planner", "executor", "verifier", "validator", "explorer"})


def _action_counts(raw: Any) -> dict[str, int] | None:
    d = _dict(raw)
    out = {k: _count(d.get(k)) for k in _ACTION_COUNT_KEYS}
    return None if _all_none(out) else {k: v for k, v in out.items() if v is not None}


def model_calls_block(raw: Any) -> list[dict[str, Any]] | None:
    """Per-call model usage of an OSN run: role, model, tokens, cost and its provenance. Never prompts."""
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for c in raw[:MAX_MODEL_CALLS]:
        if not isinstance(c, dict):
            continue
        model = _text(c.get("model"), 256)
        if model is None:
            continue
        role = c.get("role")
        out.append({
            "attempt": _count(c.get("attempt")),
            "turn": _count(c.get("turn")),
            "role": role if isinstance(role, str) and role in _ROLE_NAMES else None,
            "model": model,
            "requested_model": _text(c.get("requested_model"), 256),
            "prompt_tokens": _count(c.get("prompt_tokens")),
            "completion_tokens": _count(c.get("completion_tokens")),
            "cache_read_tokens": _count(c.get("cache_read_tokens")),
            "cost_usd": _number(c.get("cost_usd")),
            "cost_source": _text(c.get("cost_source"), 32),
            "duration_ms": _count(c.get("duration_ms")),
        })
    return out or None


_BUDGET_LIMIT_KEYS = ("max_spend_usd", "max_attempts", "max_commands", "max_writes")
_BUDGET_USAGE_KEYS = ("spend_usd", "model_calls", "attempts", "commands", "writes")


def _budget_number(value: Any) -> int | float | None:
    """A finite number; whole counts stay ``int`` so ``max_attempts=3`` does not become ``3.0``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return _number(value)


def agent_budgets_block(entry: dict) -> dict[str, Any] | None:
    """Agent Budgets: configured limits, observed usage, the limit reached, what OpenShard did.

    Projected to local and hosted Receipt surfaces. ``enforced`` is False when a budget was configured but the
    capability was off or unconfirmed; then only the limits and the reason
    are kept.
    """
    raw = _dict(entry.get("agent_budgets"))
    if not raw or raw.get("capability") != "agent_budgets":
        return None
    enforced = raw.get("enforced")
    if not isinstance(enforced, bool):
        return None
    limits_raw = _dict(raw.get("limits"))
    limits = {
        k: _budget_number(limits_raw.get(k))
        for k in _BUDGET_LIMIT_KEYS
        if _budget_number(limits_raw.get(k)) is not None
    }
    if not enforced:
        return {
            "enforced": False,
            "reason": _text(raw.get("reason"), 64),
            "limits": limits or None,
        }
    usage_raw = _dict(raw.get("usage"))
    usage: dict[str, Any] = {k: _budget_number(usage_raw.get(k)) for k in _BUDGET_USAGE_KEYS}
    for flag in ("spend_known", "spend_is_estimate"):
        usage[flag] = usage_raw.get(flag) if isinstance(usage_raw.get(flag), bool) else None
    ev_raw = _dict(raw.get("evidence"))
    evidence = {"counts": _text(ev_raw.get("counts"), 64), "spend": _text(ev_raw.get("spend"), 64)}
    return {
        "enforced": True,
        "limits": limits or None,
        "usage": None if _all_none(usage) else usage,
        "limit_reached": _text(raw.get("limit_reached"), 32),
        "action": _text(raw.get("action"), 48),
        "evidence": None if _all_none(evidence) else evidence,
    }


_ROUTING_RECORD_MODES = frozenset({"shadow", "applied"})


def adaptive_routing_block(entry: dict) -> dict[str, Any] | None:
    """Adaptive routing (OSN dogfood): whether the decision chose the model, and what it chose.

    Projected to local and hosted Receipt surfaces. Model ids are identifiers, never paths.
    """
    raw = _dict(entry.get("adaptive_routing"))
    if not raw or raw.get("capability") != "adaptive_routing":
        return None
    applied = raw.get("applied")
    if not isinstance(applied, bool):
        return None
    mode = raw.get("record_mode")
    block: dict[str, Any] = {
        "applied": applied,
        "record_mode": mode if mode in _ROUTING_RECORD_MODES else None,
        "reason": _text(raw.get("reason"), 64),
        "history_evidence": _text(raw.get("history_evidence"), 64),
    }
    if not applied:
        block["requested_class"] = _text(raw.get("requested_class"), 64)
        block["eligible_count"] = _count(raw.get("eligible_count"))
        return block
    ladder_raw = raw.get("escalation_ladder")
    ladder = [_text(m, 256) for m in ladder_raw if isinstance(m, str)] if isinstance(ladder_raw, list) else []
    shadow_raw = raw.get("shadow_candidates")
    shadow = [_text(m, 256) for m in shadow_raw if isinstance(m, str)] if isinstance(shadow_raw, list) else []
    policy = _dict(raw.get("policy"))
    history = _dict(raw.get("history"))
    block.update({
        "selected_model": _text(raw.get("selected_model"), 256),
        "selection_mode": _text(raw.get("selection_mode"), 32),
        "routing_class": _text(raw.get("routing_class"), 64),
        "escalation_ladder": [m for m in ladder if m][:8] or None,
        "ladder_source": _text(raw.get("ladder_source"), 32),
        "recovery_enabled": _bool(raw.get("recovery_enabled")),
        "decision_fingerprint": _text(raw.get("decision_fingerprint"), 32),
        # Routing V2 fields; None on V1 records.
        "policy": (
            {"name": _text(policy.get("name"), 64), "version": _text(policy.get("version"), 16)}
            if policy else None
        ),
        "step_type": _text(raw.get("step_type"), 16),
        "promotion_state": _text(raw.get("promotion_state"), 32),
        "shadow_candidates": [m for m in shadow if m][:3] or None,
        "history": (
            {"used": _bool(history.get("used")), "reason": _text(history.get("reason"), 64),
             "candidates_with_evidence": _count(history.get("candidates_with_evidence"))}
            if history else None
        ),
    })
    return block


def capability_snapshot_block(entry: dict) -> dict[str, Any] | None:
    """Which Platform capabilities governed an OSN run, read once at its start."""
    raw = _dict(entry.get("capability_snapshot"))
    if not raw:
        return None
    enabled = _dict(raw.get("enabled"))
    return {
        "source": _text(raw.get("source"), 16),
        "reason": _text(raw.get("reason"), 64),
        "refreshed_at_run_start": _bool(raw.get("refreshed_at_run_start")),
        "enabled": {str(k)[:64]: bool(v) for k, v in list(enabled.items())[:16] if isinstance(v, bool)},
    }


_POLICY_SOURCES = frozenset({"fresh", "stale_cache", "none", "unavailable"})


def organisation_policy_block(entry: dict) -> dict[str, Any] | None:
    """Privacy-safe identity of the organisation policy that governed a run."""
    raw = _dict(entry.get("organisation_policy"))
    if raw.get("schema_version") != 1 or raw.get("source") not in _POLICY_SOURCES:
        return None
    applied = _bool(raw.get("applied"))
    override = _bool(raw.get("repository_override_applied"))
    refreshed = _bool(raw.get("refreshed_at_run_start"))
    if applied is None or override is None or refreshed is None:
        return None
    version = raw.get("organisation_policy_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        version = None
    policy_hash = raw.get("organisation_policy_hash")
    if not isinstance(policy_hash, str) or not _CONTENT_HASH_RE.match(policy_hash):
        policy_hash = None
    effective_hash = raw.get("effective_policy_hash")
    if not isinstance(effective_hash, str) or not _CONTENT_HASH_RE.match(effective_hash):
        effective_hash = None
    return {
        "schema_version": 1,
        "organisation_policy_version": version,
        "organisation_policy_hash": policy_hash,
        "source": raw["source"],
        "applied": applied,
        "repository_override_applied": override,
        "effective_policy_hash": effective_hash,
        "refreshed_at_run_start": refreshed,
        "reason": _text(raw.get("reason"), 64),
    }


_LEARNING_STATUSES = frozenset({
    "used", "no_relevant_signals", "no_history", "disabled", "error",
    # The precomputed snapshot was late or unusable: history unknown, not absent.
    "unavailable", "timeout",
})


def learning_block(entry: dict) -> dict[str, Any] | None:
    """Learning Loop V1: whether prior signals were consulted and what they influenced.

    Privacy-bounded local and hosted evidence. Context delivery never proves
    the agent followed it, and V1 recommendations never change verification.
    """
    raw = _dict(entry.get("learning"))
    status = raw.get("status")
    if not raw or status not in _LEARNING_STATUSES:
        return None
    routing = _dict(raw.get("routing"))
    verification = _dict(raw.get("verification"))
    checks = verification.get("recommended_checks")
    files = raw.get("context_files_added")
    ids = raw.get("signal_ids")
    out: dict[str, Any] = {
        "status": status,
        "used": raw.get("used") is True,
        "signals_used": _count(raw.get("signals_used")),
        "signals_considered": _count(raw.get("signals_considered")),
        "signal_ids": [label for label in (_text(i, 32) for i in ids[:5] if isinstance(i, str)) if label] if isinstance(ids, list) else [],
        "context_supplied": raw.get("context_supplied") is True,
        "context_files_added": (
            [f for f in (_text(x, 160) for x in files[:2] if isinstance(x, str)) if f]
            if isinstance(files, list) else []
        ),
        "routing_influenced": routing.get("influenced") is True,
        "routing_reason": _text(routing.get("reason"), 64),
        "verification_influenced": verification.get("influenced") is True,
        "recommended_checks": [
            label for label in (_text(_dict(c).get("label"), 120) for c in checks[:3]) if label
        ] if isinstance(checks, list) else [],
    }
    snapshot = _learning_snapshot(raw.get("snapshot"))
    if raw.get("context_delivery") in ("hook_response_emitted", "not_emitted"):
        out["context_delivery"] = raw["context_delivery"]
    if snapshot is not None and snapshot.get("source") == "hosted_context":
        ids = raw.get("supporting_receipt_ids")
        if isinstance(ids, list):
            out["supporting_receipt_ids"] = list(dict.fromkeys(r for r in ids[:20] if isinstance(r, str) and re.fullmatch(r"rcpt_[0-9a-f]{32}", r)))
    if snapshot is not None:  # only runs that read a bounded history snapshot carry it
        out["snapshot"] = snapshot
    return out


def _learning_snapshot(value: object) -> dict[str, Any] | None:
    """Where OSN's signals came from: the precomputed snapshot and how its lookup went."""
    raw = _dict(value)
    if not raw:
        return None
    out: dict[str, Any] = {
        "status": _text(raw.get("status"), 32),
        "snapshot_id": _text(raw.get("snapshot_id"), 64),
        "generated_at": _text(raw.get("generated_at"), 32),
        "lookup_ms": _number(raw.get("lookup_ms")),
        "budget_ms": _number(raw.get("budget_ms")),
    }
    if raw.get("source") == "hosted_context":
        out["source"] = "hosted_context"
    if raw.get("trimmed") is True:  # trimmed to fit: only the stored signals could be considered
        out.update(trimmed=True, signals_stored=_count(raw.get("signals_stored")),
                   signals_derived=_count(raw.get("signals_derived")))
    return out


MAX_SUPERVISOR_DECISIONS = 8
_SUPERVISOR_ACTIONS = frozenset({"escalate", "stop"})


def supervisor_routing_block(entry: dict) -> dict[str, Any] | None:
    """Supervisor routing (OSN dogfood): the decisions considered at each observed-failure
    boundary, why, on what evidence, and whether the loop acted on them. Local and hosted."""
    raw = _dict(entry.get("supervisor_routing"))
    if not raw or raw.get("capability") != "supervisor_routing":
        return None
    mode = raw.get("record_mode")
    if mode not in _ROUTING_RECORD_MODES:
        return None
    decisions: list[dict[str, Any]] = []
    raw_decisions = raw.get("decisions")
    items: list[Any] = raw_decisions[:MAX_SUPERVISOR_DECISIONS] if isinstance(raw_decisions, list) else []
    for item in items:
        d = _dict(item)
        if d.get("action") not in _SUPERVISOR_ACTIONS:
            continue
        ev = _dict(d.get("evidence"))
        rr = _dict(ev.get("reroute"))
        decisions.append({
            "attempt": _count(d.get("attempt")),
            "action": d["action"],
            "reason": _text(d.get("reason"), 64),
            "recommended_model": _text(d.get("recommended_model"), 256),
            "acted_on": _bool(d.get("acted_on")),
            "not_acted_reason": _text(d.get("not_acted_reason"), 64),
            "evidence": {
                "verification_status": _text(ev.get("verification_status"), 32),
                "verification_source": _text(ev.get("verification_source"), 32),
                "attempts_so_far": _count(ev.get("attempts_so_far")),
                "spend_usd": _number(ev.get("spend_usd")),
                "spend_known": _bool(ev.get("spend_known")),
                "cost_budget_usd": _number(ev.get("cost_budget_usd")),
                "ladder_model": _text(ev.get("ladder_model"), 256),
                "changed_next_model": _bool(ev.get("changed_next_model")),
                # Routing V2 repair-step re-route; None when the fixed plan was followed.
                "reroute": (
                    {"resolved_class": _text(rr.get("resolved_class"), 64),
                     "selected_model": _text(rr.get("selected_model"), 256),
                     "changed_from_plan": _bool(rr.get("changed_from_plan")),
                     "history_used": _bool(rr.get("history_used"))}
                    if rr else None
                ),
            },
        })
    return {
        "record_mode": mode,
        "not_applied_reason": _text(raw.get("not_applied_reason"), 64),
        "boundary": _text(raw.get("boundary"), 64),
        "decisions": decisions or None,
    }


def base_commit_value(entry: dict) -> str | None:
    """HEAD at run/session start (every producer records it then): a base, not a result."""
    value = entry.get("git_head_commit_hash")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _SHA_RE.match(value) else None


def provider_value(entry: dict) -> str | None:
    """The model provider the capture recorded (``capture.provider``): reported by the
    agent, or for Claude Code read from its own environment (``provider_source``)."""
    return _text(_dict(entry.get("capture")).get("provider"), 128)


def surface_value(entry: dict) -> str | None:
    """How the agent was launched (``capture.surface``: Claude Code's raw entrypoint)."""
    return _text(_dict(entry.get("capture")).get("surface"), 64)


def commit_value(entry: dict) -> str | None:
    """The resulting commit, only when git observed the session create it.

    Capture records the HEAD the session ended on (``git_end_head``) and the
    commits created during the session (``session_commits``, git_observed).
    The end HEAD is the result only when it is one of those -- a HEAD that
    merely moved (checkout, reset, pull) is never presented as the work.
    """
    end = entry.get("git_end_head")
    block = _dict(entry.get("session_commits"))
    shas = block.get("shas")
    if not isinstance(end, str) or not isinstance(shas, list) or end not in shas:
        return None
    value = end.strip().lower()
    return value if _SHA_RE.match(value) else None


def pr_url_value(entry: dict) -> str | None:
    """A pull request URL whose head the hosting provider reported as a session-created commit."""
    pr = _dict(entry.get("pull_request"))
    url, head = pr.get("url"), pr.get("head")
    shas = _dict(entry.get("session_commits")).get("shas")
    if not isinstance(url, str) or not isinstance(shas, list) or head not in shas:
        return None
    if not re.match(r"^https://[^\s]{1,2040}$", url):
        return None
    return url


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
    """The retry pass. ``attempts`` and ``cost_included`` come only from a record that
    stored every escalation (``retry_attempts``); older records leave both ``None``
    and keep exactly what they recorded, never completed by guessing.

    ``cost_included`` says whether the receipt's ``cost_usd`` is the true run total
    (first attempt plus every escalation): True only when every attempt's cost is
    stored, False when attempts are recorded but one cost is unknown.
    """
    stored = stored_retry_attempts(entry)
    attempts = (
        [
            {"model": _text(a["model"], 256), "total_tokens": a["total_tokens"], "cost_usd": a["estimated_cost"]}
            for a in stored
        ]
        if stored
        else None
    )
    cost_included: bool | None = None
    if stored and entry.get("retry_triggered") is True:
        cost_included = run_total_cost(entry)[1]
    block = {
        "triggered": _bool(entry.get("retry_triggered")),
        "fixer_model": _text(entry.get("fixer_model"), 256),
        "attempts": attempts,
        "cost_included": cost_included,
        "total_tokens": _count(entry.get("retry_total_tokens")),
        "cost_usd": _number(entry.get("retry_estimated_cost")),
    }
    return None if _all_none(block) else block


def stage_metrics(entry: dict) -> list[dict[str, float | int | None]]:
    """Per-stage duration/cost/token usage, parallel to ``model_stages`` built from ``stage_runs``."""
    runs = entry.get("stage_runs")
    if not isinstance(runs, list):
        return []
    return [
        {
            "duration_seconds": _number(s.get("duration")),
            "cost_usd": _number(s.get("cost")),
            "tokens_input": _count(s.get("tokens_input")),
            "tokens_output": _count(s.get("tokens_output")),
        }
        for s in runs
        if isinstance(s, dict) and "stage_type" in s and "model" in s
    ]


def runtime_configuration_block(entry: dict) -> dict | None:
    """Effective settings explicitly reported by Claude hooks, never requested env settings."""
    capture = _dict(entry.get("capture"))
    level = capture.get("effort_level")
    if capture.get("effort_source") != "claude_hook" or level not in ("low", "medium", "high", "xhigh", "max"):
        return None
    return {"effort": level, "source": "claude_hook", "evidence": "agent_reported"}


def project_entry_evidence(entry: Any) -> dict[str, Any]:
    """Every entry-derived block in one dict (``ShardReceipt.recorded_evidence``). Never raises."""
    if not isinstance(entry, dict):
        return {}
    from openshard.history.correlation import correlation_block

    projectors = {
        "runtime_configuration": runtime_configuration_block,
        "correlation": lambda record: correlation_block(record.get("correlation")),
        "approval_detail": approval_detail_block,
        "sandbox_detail": sandbox_detail_block,
        "execution_loop": execution_loop_block,
        "agent_loop": agent_loop_block,  # local surfaces only; see its docstring
        "agent_budgets": agent_budgets_block,
        "adaptive_routing": adaptive_routing_block,
        "supervisor_routing": supervisor_routing_block,
        "capability_snapshot": capability_snapshot_block,
        "organisation_policy": organisation_policy_block,
        "learning": learning_block,
        "permissions": permission_evidence_block,
        "base_commit": base_commit_value,
        "provider": provider_value,
        "surface": surface_value,
        "commit": commit_value,
        "pr_url": pr_url_value,
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
    "commit_value",
    "pr_url_value",
    "provider_value",
    "surface_value",
    "content_hash_value",
    "execution_loop_block",
    "learning_block",
    "organisation_policy_block",
    "permission_evidence_block",
    "permission_scopes_block",
    "policy_decisions_block",
    "project_entry_evidence",
    "retry_block",
    "routing_block",
    "sandbox_detail_block",
    "session_block",
    "stage_metrics",
    "unsafe_text",
]
