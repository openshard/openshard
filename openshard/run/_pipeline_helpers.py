"""Internal helpers extracted from pipeline.py.

These are module-level utility functions that support RunPipeline but are not
methods on the class.  They live here to keep pipeline.py from growing without
bound.  Nothing outside openshard.run should import from this module directly;
import from openshard.run.pipeline instead.
"""
from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click

from openshard.analysis.repo import RepoFacts
from openshard.execution.generator import ChangedFile, ExecutionGenerator, ExecutionResult
from openshard.history.jsonl_store import append_jsonl
from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
from openshard.providers.openrouter import MODEL_PRICING, compute_cost
from openshard.routing.form_factor_policy import ExecutionFormFactorDecision
from openshard.routing.profiles import ProfileDecision
from openshard.run.timeline import make_timeline_event
from openshard.scoring.scorer import ScoredRoutingResult
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path
from openshard.skills.matcher import MatchedSkill
from openshard.verification.plan import VerificationPlan

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_LOG_PATH = Path(".openshard") / "runs.jsonl"

_NATIVE_SIGNALS: list[tuple[list[str], str]] = [
    (["all files", "every file", "multiple files", "many files"], "multi-file scope"),
    (["throughout the", "across the codebase", "across all files"], "multi-file scope"),
    # refactor / architecture / codebase are now handled in direct mode via minimax
]

_SYSTEM_OVERHEAD_TOKENS = 500   # execution system-prompt is substantial

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _suggest_executor(
    task: str,
    *,
    category: str = "standard",
    read_only: bool = False,
    opencode_available: bool = False,
    opencode_preference: bool = False,
    risky_paths: list[str] | None = None,
    _for_dispatch: bool = False,
) -> tuple[str, str, object | None]:
    """Return ``(executor, reason, advisory_result)`` for *task*.

    When ``_for_dispatch=True`` the function calls ``rank_executors()`` and
    uses its top recommendation as the executor choice, setting
    ``advisory_result.advisory_only=False``. Falls back to the original
    heuristic on any error.

    When ``_for_dispatch=False`` (default — backward-compatible) only the
    heuristic runs and ``advisory_result`` is ``None``.

    ``"opencode"`` is never returned unless ``opencode_preference=True`` and
    ``opencode_available=True``.
    """
    if _for_dispatch:
        try:
            from openshard.routing.executor_advisory import rank_executors as _rank
            _ea = _rank(
                task,
                category=category,
                read_only=read_only,
                opencode_available=opencode_available,
                opencode_preference=opencode_preference,
                risky_paths=risky_paths or [],
                _for_dispatch=True,
            )
            _reason = _ea.recommended.reasons[0] if _ea.recommended.reasons else "advisory ranked"
            return _ea.recommended.executor, _reason, _ea
        except Exception:
            pass  # fall through to heuristic

    lower = task.lower()
    if len(task.split()) > 60:
        return "native", "large or complex task", None
    for keywords, label in _NATIVE_SIGNALS:
        if any(kw in lower for kw in keywords):
            return "native", label, None
    return "direct", "focused task", None


def _pre_run_cost_hint(model: str, task: str) -> str | None:
    """Return a conservative pre-run cost range, or None if pricing is unknown.

    Assumes 300–1500 completion tokens — small code output to a full module.
    """
    if model not in MODEL_PRICING:
        return None
    prompt_tokens = _SYSTEM_OVERHEAD_TOKENS + len(task) // 4
    cost_low  = compute_cost(model, prompt_tokens, 300)
    cost_high = compute_cost(model, prompt_tokens, 1500)
    if cost_low is None or cost_high is None:
        return None
    return f"~${cost_low:.4f}-${cost_high:.4f}"


def _parse_cost_hint(hint: str | None) -> float | None:
    # Temporary: parses dollar amount from formatted hint string.
    # TODO: replace with structured cost metadata from the routing layer.
    if not hint:
        return None
    m = re.search(r'\$([\d.]+)', hint)
    return float(m.group(1)) if m else None


def _serialize_verification_plan(plan: VerificationPlan) -> list[dict]:
    return [
        {
            "name": cmd.name,
            "argv": cmd.argv,
            "kind": cmd.kind.value,
            "source": cmd.source.value,
            "safety": cmd.safety.value,
            "reason": cmd.reason,
        }
        for cmd in plan.commands
    ]


def _safe_git_info(path: Path) -> dict[str, object]:
    repo_name: str = path.name
    try:
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(path), capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0 or not r.stdout:
            return {"repo_name": repo_name}
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        branch_raw = lines[0].removeprefix("## ").strip()
        branch = branch_raw.split("...")[0].split(" ")[0] or None
        dirty = any(ln for ln in lines if not ln.startswith("##"))
        result: dict[str, object] = {"repo_name": repo_name, "git_branch": branch, "git_dirty": dirty}
        # Additive stable identity from the origin remote (never raises, no
        # network, credentials stripped). Absent when there is no remote.
        try:
            from openshard.history.repo_identity import REPO_IDENTITY_FIELD, capture_repo_identity
            identity = capture_repo_identity(path)
            if identity:
                result[REPO_IDENTITY_FIELD] = identity
        except Exception:
            pass
        try:
            rev = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(path), capture_output=True, text=True, timeout=3,
            )
            if rev.returncode == 0 and rev.stdout.strip():
                result["git_head_commit_hash"] = rev.stdout.strip()
        except Exception:
            pass
        return result
    except Exception:
        return {"repo_name": repo_name}


def _build_osn_verification_contract_with_loop(native_meta: Any, *, is_write_task: bool):
    """Build the OSN verification contract wired to actual execution results.

    Extracted so that the verification_loop wiring can be independently tested
    without standing up the full pipeline. Changing the signature here breaks
    test_osn_verification_contract.py::test_54_pipeline_helper_wires_verification_loop.
    """
    from openshard.native.verification_contract import build_osn_verification_contract
    return build_osn_verification_contract(
        osn_observation=native_meta.osn_observation,
        osn_loop_summary=native_meta.osn_loop_summary,
        is_write_task=is_write_task,
        verification_loop=native_meta.verification_loop,
    )


def _promote_sandbox_git_metadata(extra_metadata: dict | None) -> None:
    """Promote git_base_branch and git_base_commit_hash from sandbox sub-dict to top-level."""
    if extra_metadata is None:
        return
    _sandbox = extra_metadata.get("sandbox")
    if not isinstance(_sandbox, dict):
        return
    _gb = _sandbox.get("git_base_branch")
    _gc = _sandbox.get("git_base_commit_hash")
    if _gb is not None:
        extra_metadata["git_base_branch"] = _gb
    if _gc is not None:
        extra_metadata["git_base_commit_hash"] = _gc


def _populate_context_usage_metadata(extra_metadata: dict | None) -> None:
    """Promote context utilisation fields from nested context_provenance to top-level.

    Reads extra_metadata["context_provenance"]["total_items"] and
    ["injected_sources"] and writes three flat keys:
      context_files_considered_count
      context_files_injected_count
      context_utilisation_ratio   (None when total is zero)

    No-ops when extra_metadata is None, when context_provenance is absent or
    malformed, or when the counts are not integers. Never raises.
    """
    if extra_metadata is None:
        return
    try:
        cp = extra_metadata.get("context_provenance")
        if not isinstance(cp, dict):
            return
        total = cp.get("total_items")
        injected = cp.get("injected_sources")
        if not isinstance(total, int) or not isinstance(injected, int):
            return
        extra_metadata["context_files_considered_count"] = total
        extra_metadata["context_files_injected_count"] = injected
        extra_metadata["context_utilisation_ratio"] = (
            round(injected / total, 4) if total > 0 else None
        )
    except Exception:
        pass


def _populate_execution_span_metadata(extra_metadata: dict | None) -> None:
    """Convert native_loop_trace phase events into execution_spans dicts.

    Each NativeLoopEvent (phase, status, summary) becomes a span dict
    compatible with ExecutionSpan. Timestamps and durations are not available
    from the loop trace and are left as None. Blank-phase events are skipped.
    Long summaries are capped at 200 characters. No raw content is stored.

    No-ops when extra_metadata is None, when native_loop_trace is absent,
    empty, or malformed. Never raises.
    """
    if extra_metadata is None:
        return
    try:
        loop_events = extra_metadata.get("native_loop_trace")
        if not isinstance(loop_events, list) or not loop_events:
            return
        spans: list[dict] = []
        for i, ev in enumerate(loop_events):
            if not isinstance(ev, dict):
                continue
            phase = str(ev.get("phase") or "").strip()
            if not phase:
                continue
            raw_summary = ev.get("summary") or None
            spans.append({
                "span_id": f"phase-{i}-{phase}",
                "name": phase,
                "kind": "phase",
                "started_at": None,
                "duration_ms": None,
                "status": str(ev.get("status") or "completed"),
                "error_class": None,
                "summary": str(raw_summary)[:200] if raw_summary else None,
            })
        if spans:
            extra_metadata["execution_spans"] = spans
    except Exception:
        pass


def _build_native_events(
    entry: dict,
    files: list[ChangedFile],
    verification_attempted: bool,
    verification_passed: bool | None,
    retry_triggered: bool,
) -> list[dict]:
    """Build this native (OSN) run's canonical Events at observation time (Migration 6).

    Mirrors ``wrap_exec._build_wrap_events`` in shape and never-raises
    contract, but the native run pipeline genuinely controls execution --
    it dispatches its own tools, applies its own sandbox diff, and runs its
    own verification commands -- so every event here legitimately uses
    EVIDENCE_DIRECTLY_OBSERVED rather than the agent_reported/git_observed
    evidence the external-observer adapters are limited to. Never raises;
    returns [] on any internal failure.
    """
    try:
        from openshard.history.event import (
            EVENT_FILE_CHANGED,
            EVENT_RETRY_STARTED,
            EVENT_RUN_COMPLETED,
            EVENT_RUN_FAILED,
            EVENT_RUN_STARTED,
            EVENT_TOOL_INVOKED,
            EVENT_VERIFICATION_FAILED,
            EVENT_VERIFICATION_PASSED,
            EVENT_VERIFICATION_SKIPPED,
            EVENT_VERIFICATION_STARTED,
            EVIDENCE_DIRECTLY_OBSERVED,
            SOURCE_NATIVE_RUN,
            STATUS_FAILED,
            STATUS_PASSED,
            STATUS_SKIPPED,
            STATUS_STARTED,
            STATUS_UNKNOWN,
            make_event,
        )

        common = {
            "run_id": entry.get("run_id"),
            "shard_id": entry.get("shard_id"),
            "attempt_number": entry.get("attempt_number"),
            "occurred_at": entry.get("timestamp"),
            "evidence": EVIDENCE_DIRECTLY_OBSERVED,
        }

        events = [
            make_event(
                event_type=EVENT_RUN_STARTED,
                source=SOURCE_NATIVE_RUN,
                action="native run started",
                status=STATUS_STARTED,
                **common,
            )
        ]

        tool_trace = entry.get("tool_trace")
        if isinstance(tool_trace, list):
            for call in tool_trace:
                if not isinstance(call, dict):
                    continue
                events.append(
                    make_event(
                        event_type=EVENT_TOOL_INVOKED,
                        source=SOURCE_NATIVE_RUN,
                        action=f"tool {call.get('tool', 'unknown')}",
                        status=STATUS_PASSED if call.get("ok") else STATUS_FAILED,
                        # metadata["tool"] is the canonical structured tool
                        # identity every tool.invoked producer exposes (the
                        # Claude hooks adapter sets the same key); readers use
                        # event.tool_identity rather than parsing ``action``.
                        metadata={
                            "tool": call.get("tool"),
                            "approved": call.get("approved"),
                            "output_chars": call.get("output_chars"),
                        },
                        **common,
                    )
                )

        # File-change status stays unknown: a changed file is an observed
        # fact, not a pass/fail outcome.
        for f in files:
            events.append(
                make_event(
                    event_type=EVENT_FILE_CHANGED,
                    source=SOURCE_NATIVE_RUN,
                    action=f"file {f.change_type}",
                    target=f.path,
                    status=STATUS_UNKNOWN,
                    **common,
                )
            )

        if retry_triggered:
            events.append(
                make_event(
                    event_type=EVENT_RETRY_STARTED,
                    source=SOURCE_NATIVE_RUN,
                    action="retry started",
                    status=STATUS_STARTED,
                    **common,
                )
            )

        if verification_attempted:
            if verification_passed is True:
                v_type, v_status = EVENT_VERIFICATION_PASSED, STATUS_PASSED
            elif verification_passed is False:
                v_type, v_status = EVENT_VERIFICATION_FAILED, STATUS_FAILED
            else:
                # Attempted but outcome unknown -- honestly represent as
                # started/unknown rather than fabricating pass or fail.
                v_type, v_status = EVENT_VERIFICATION_STARTED, STATUS_UNKNOWN
        else:
            v_type, v_status = EVENT_VERIFICATION_SKIPPED, STATUS_SKIPPED
        events.append(
            make_event(
                event_type=v_type,
                source=SOURCE_NATIVE_RUN,
                action="verification",
                status=v_status,
                **common,
            )
        )

        run_failed = verification_attempted and verification_passed is False
        events.append(
            make_event(
                event_type=EVENT_RUN_FAILED if run_failed else EVENT_RUN_COMPLETED,
                source=SOURCE_NATIVE_RUN,
                action="native run finished",
                status=(
                    STATUS_FAILED
                    if run_failed
                    else (STATUS_PASSED if verification_passed else STATUS_UNKNOWN)
                ),
                **common,
            )
        )

        return [e.to_dict() for e in events]
    except Exception:
        return []


def _log_run(
    start: float,
    task: str,
    generator: ExecutionGenerator,
    retry_triggered: bool,
    files: list[ChangedFile],
    verification_attempted: bool,
    verification_passed: bool | None,
    workspace: Path | None,
    usage=None,
    retry_usage=None,
    model: str | None = None,
    summary: str = "",
    notes: list | None = None,
    stage_runs=None,
    routing_decision=None,
    _scored: ScoredRoutingResult | None = None,
    repo_facts: RepoFacts | None = None,
    matched_skills: list[MatchedSkill] | None = None,
    profile_decision: ProfileDecision | None = None,
    verification_plan: VerificationPlan | None = None,
    form_factor_decision: ExecutionFormFactorDecision | None = None,
    extra_metadata: dict | None = None,
    run_id: str | None = None,
    run_timeline: list | None = None,
    run_index: int | None = None,
    executor_advisory_result=None,
    executor_source: str = "unknown",
    fra_result: dict | None = None,
    effective_executor: str | None = None,
    provider_enforcement_result=None,
    routable_pool=None,
    model_policy_summary: dict | None = None,
    shard_id: str | None = None,
    attempt_number: int = 1,
) -> None:
    _timestamp_val = run_id if run_id else datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": _timestamp_val,
        "run_id": _timestamp_val,
        "task": task,
        "execution_model": model or generator.model,
        "retry_triggered": retry_triggered,
        "duration_seconds": round(time.time() - start, 2),
        "files_created": sum(1 for f in files if f.change_type == "create"),
        "files_updated": sum(1 for f in files if f.change_type == "update"),
        "files_deleted": sum(1 for f in files if f.change_type == "delete"),
        "verification_attempted": verification_attempted,
        "verification_passed": verification_passed,
        "workspace_path": str(workspace) if workspace else None,
        **_safe_git_info(workspace if workspace is not None else Path.cwd()),
        "summary": summary,
        "files_detail": [
            {"path": f.path, "change_type": f.change_type, "summary": f.summary or ""}
            for f in files
        ],
    }
    if notes:
        entry["notes"] = [n.split("\n")[0][:300] for n in notes if n][:5]
    if stage_runs:
        entry["stage_runs"] = [
            {
                "stage_type": sr.stage.stage_type,
                "model": sr.model,
                "duration": round(sr.duration, 2),
                "cost": sr.cost,
            }
            for sr in stage_runs
        ]
    if routing_decision is not None:
        entry["routing_model"] = routing_decision.model
        entry["routing_rationale"] = routing_decision.rationale
        try:
            # Determine which symbolic role maps to this model and check its source
            from openshard.routing.model_resolver import (
                MODEL_CHEAP,
                MODEL_COMPLEX,
                MODEL_ESCALATE,
                MODEL_MAIN,
                MODEL_STRONG,
                MODEL_VISUAL,
            )
            from openshard.routing.model_resolver import resolution_source as _res_src
            _role_map = {
                MODEL_CHEAP: "cheap", MODEL_MAIN: "main", MODEL_STRONG: "strong",
                MODEL_ESCALATE: "escalate", MODEL_VISUAL: "visual", MODEL_COMPLEX: "complex",
            }
            _role = _role_map.get(routing_decision.model)
            if _role is not None:
                entry["model_resolution"] = _res_src(_role)
            else:
                entry["model_resolution"] = "unknown"
        except Exception:
            pass
    if _scored is not None:
        entry["routing_category"] = _scored.category
        entry["routing_candidate_count"] = _scored.candidate_count
        entry["routing_selected_model"] = _scored.selected_model
        entry["routing_selected_provider"] = _scored.selected_provider
        entry["routing_used_fallback"] = _scored.used_fallback
        if _scored.candidates:
            entry["routing_candidates"] = _scored.candidates
        if _scored.scores:
            entry["routing_scores"] = _scored.scores
        if _scored.scores_raw:
            entry["routing_scores_raw"] = _scored.scores_raw
        if _scored.history_adjustments:
            entry["routing_adjustments"] = _scored.history_adjustments
    if retry_triggered:
        entry["fixer_model"] = generator.fixer_model
    if usage is not None:
        entry["prompt_tokens"] = usage.prompt_tokens
        entry["completion_tokens"] = usage.completion_tokens
        entry["total_tokens"] = usage.total_tokens
        entry["estimated_cost"] = usage.estimated_cost
    if retry_usage is not None:
        entry["retry_prompt_tokens"] = retry_usage.prompt_tokens
        entry["retry_completion_tokens"] = retry_usage.completion_tokens
        entry["retry_total_tokens"] = retry_usage.total_tokens
        entry["retry_estimated_cost"] = retry_usage.estimated_cost
    if repo_facts is not None:
        entry["repo_facts"] = {
            "languages": repo_facts.languages,
            "package_files": repo_facts.package_files,
            "framework": repo_facts.framework,
            "test_command": repo_facts.test_command,
            "risky_paths_count": len(repo_facts.risky_paths),
            "risky_paths_sample": repo_facts.risky_paths[:3],
            "changed_files_count": len(repo_facts.changed_files),
            "changed_files_sample": repo_facts.changed_files[:3],
        }

    if matched_skills:
        entry["matched_skills"] = [m.skill.slug for m in matched_skills]
        entry["matched_skill_reasons"] = {m.skill.slug: m.reasons for m in matched_skills}

    if profile_decision is not None:
        entry["execution_profile"] = profile_decision.profile
        entry["execution_profile_reason"] = profile_decision.reason

    if form_factor_decision is not None:
        entry["form_factor"] = {
            "public_mode":            form_factor_decision.public_mode,
            "internal_form_factor":   form_factor_decision.internal_form_factor,
            "reason":                 form_factor_decision.reason,
            "confidence":             form_factor_decision.confidence,
            "risk_level":             form_factor_decision.risk_level,
            "read_only":              form_factor_decision.read_only,
            "write_requested":        form_factor_decision.write_requested,
            "verification_available": form_factor_decision.verification_available,
            "context_quality":        form_factor_decision.context_quality,
            "warnings":               form_factor_decision.warnings,
        }

    if verification_plan is not None and verification_plan.has_commands:
        entry["verification_plan"] = _serialize_verification_plan(verification_plan)

    # Record executor advisory result and source when available.
    if executor_source and executor_source != "unknown":
        entry["executor_source"] = executor_source
    if executor_advisory_result is not None:
        try:
            _ea = executor_advisory_result
            entry["executor_advisory"] = {
                "recommended": _ea.recommended.executor,
                "score": _ea.recommended.score,
                "top_reasons": _ea.recommended.reasons[:2],
                "advisory_only": _ea.advisory_only,
                "alternatives": [
                    {"executor": a.executor, "score": a.score}
                    for a in (_ea.alternatives or [])[:3]
                ],
            }
        except Exception:
            pass

    # Generate model advisory from risk signal — stored for display, never affects routing.
    _advisory_risk: str | None = None
    if form_factor_decision is not None:
        _advisory_risk = form_factor_decision.risk_level
    if not _advisory_risk and extra_metadata:
        _plan_raw = extra_metadata.get("plan") or {}
        if isinstance(_plan_raw, dict):
            _advisory_risk = _plan_raw.get("risk") or None
    if _advisory_risk:
        from openshard.models.advisory import build_advisory_for_storage as _build_adv
        _adv_candidates, _adv_meta = _build_adv(risk=_advisory_risk)
        entry["model_advisory_meta"] = _adv_meta
        if _adv_candidates:
            entry["model_advisory"] = _adv_candidates

    # Store FRA result passed from pipeline (already computed before scoring).
    # Falls back to re-computation for backward compatibility when not provided.
    if fra_result is not None:
        entry["feedback_routing_advisory"] = fra_result
        # Record whether FRA actually influenced scoring (recommendation present)
        if fra_result.get("recommendation") == "consider_stronger_review":
            entry["feedback_routing_applied"] = True
    else:
        try:
            from openshard.models.feedback_advisory import (
                _load_recent_session_signals as _load_sigs,
            )
            from openshard.models.feedback_advisory import (
                build_feedback_routing_advisory as _build_fra,
            )
            _sig_path = Path.cwd() / ".openshard" / "session_signals.jsonl"
            _recent_sigs = _load_sigs(_sig_path)
            _fra = _build_fra(_recent_sigs)
            if _fra is not None:
                entry["feedback_routing_advisory"] = _fra
        except Exception:
            pass

    # Record provider-aware eligibility (v1): which providers had keys present
    # and how the routable pool narrowed. Uses the pool built during enforcement
    # when available, otherwise builds it fresh. Counts only — no key material.
    try:
        from openshard.routing.provider_availability import (
            build_routable_pool as _brp,
        )
        from openshard.routing.provider_availability import (
            detect_provider_availability as _dpa,
        )
        from openshard.routing.provider_availability import (
            routing_constraints_metadata as _rcm,
        )
        _pool_for_meta = routable_pool
        if _pool_for_meta is None:
            _avail_meta = _dpa()
            _pool_for_meta = _brp(_avail_meta, executor=effective_executor)
        else:
            _avail_meta = None
        if _avail_meta is None:
            # Reconstruct availability from the pool's recorded providers tuple.
            _prov_list = list(_pool_for_meta.available_providers)
        else:
            _prov_list = list(_avail_meta.detected)
        entry["available_providers"] = _prov_list
        entry["routing_constraints"] = _rcm(_pool_for_meta)
    except Exception:
        pass

    # Record provider-aware enforcement result (v2): what enforcement selected
    # or rejected. Both layers (v1 routing_constraints + v2 provider_enforcement)
    # are kept so receipts can show availability shape AND enforcement decision.
    if provider_enforcement_result is not None:
        try:
            entry["provider_enforcement"] = {
                "applied": provider_enforcement_result.enforcement_applied,
                "source": provider_enforcement_result.source,
                "selected_model": provider_enforcement_result.selected_model,
                "rejected_model": provider_enforcement_result.rejected_model,
                "routable_pool_size": provider_enforcement_result.routable_pool_size,
            }
        except Exception:
            pass

    if extra_metadata:
        entry.update(extra_metadata)

    if run_timeline is not None:
        _tl = list(run_timeline)
        _tl.append(make_timeline_event("receipt_saved", "Saved Shard receipt", kind="receipt").to_dict())
        entry["run_timeline"] = _tl

    if shard_id:
        entry["shard_id"] = shard_id
    else:
        from openshard.history.shard_contract import _make_shard_id as _msi
        entry["shard_id"] = _msi(entry["timestamp"], run_index)
    entry["attempt_number"] = attempt_number

    if effective_executor == "native":
        entry["events"] = _build_native_events(
            entry, files, verification_attempted, verification_passed, retry_triggered,
        )

    # Record model policy summary when policy was active this run.
    if model_policy_summary is not None:
        try:
            entry["model_policy_summary"] = model_policy_summary
        except Exception:
            pass

    # Stamp an honest routing-truth block for forward records. Read surfaces
    # recompute this from the same fields, so it is belt-and-suspenders, not
    # load-bearing. Runs after extra_metadata merge so it sees tier dispatch.
    try:
        from openshard.history.routing_truth import (
            build_routing_truth as _brt,
        )
        from openshard.history.routing_truth import (
            routing_truth_to_dict as _rttd,
        )
        entry["routing_truth"] = _rttd(_brt(entry))
    except Exception:
        pass

    entry = coerce_shard_entry(entry)

    log_path = Path.cwd() / _LOG_PATH
    append_jsonl(log_path, entry)


def _copy_cwd_to_workspace(workspace: Path) -> None:
    """Copy the current working directory into *workspace*, excluding noise."""
    shutil.copytree(
        Path.cwd(),
        workspace,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            # version control
            ".git",
            # dependency trees
            "node_modules", ".venv", "venv", "env", ".tox",
            # build / cache artefacts
            "__pycache__", "*.pyc", "*.egg-info",
            ".pytest_cache", ".mypy_cache",
            "dist", "build", "coverage", ".next",
            # openshard-internal files OpenCode doesn't need
            ".openshard", ".claude",
            # binary assets — waste context tokens for no gain
            "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico",
            "*.pdf", "*.zip", "*.tar", "*.gz",
        ),
    )


def _build_retry_prompt(task: str, result: ExecutionResult, verify_output: str) -> str:
    lines = [
        "The previous implementation failed verification.",
        "Fix the issues and return updated files only.",
        "",
        f"Original task: {task}",
        f"Previous summary: {result.summary}",
        "",
        "Files that were written:",
    ]
    for f in result.files:
        lines += [
            f"  path: {f.path}",
            f"  change_type: {f.change_type}",
            f"  summary: {f.summary}",
            "  content:",
            f.content,
            "",
        ]
    lines += [
        "Verification output:",
        verify_output.strip() if verify_output.strip() else "(no output)",
        "",
        "Instructions:",
        "- Goal: make the existing tests pass by fixing the implementation, not by changing the tests.",
        "- Prefer fixing implementation files. Only modify a test file if it contains a clear bug.",
        "- Do NOT mark any test as xfail or skip.",
        "- Do NOT delete any failing test.",
        "- Do NOT weaken any assertion.",
        "- Do NOT change expected values to match wrong output.",
        "",
        "Respond with valid JSON only — no markdown, no prose, no code fences.",
        'Use exactly this schema: {"summary": "...", "files": [{"path": "...", '
        '"change_type": "create|update|delete", "content": "...", "summary": "..."}], "notes": [...]}',
    ]
    return "\n".join(lines)


def _write_files(files: list[ChangedFile], root: Path) -> None:
    cwd = root.resolve()
    for f in files:
        try:
            target = resolve_safe_repo_path(cwd, f.path)
        except UnsafePathError:
            click.echo(f"  [skip] unsafe path rejected: {f.path!r}")
            continue

        if f.change_type == "delete":
            if target.exists():
                target.unlink()
                click.echo(f"  [deleted] {f.path}")
            else:
                click.echo(f"  [skip] not found: {f.path}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")
            click.echo(f"  [written] {f.path}")


def _detect_command(cwd: Path, config: dict | None = None) -> list[str] | None:
    if config is None:
        try:
            from openshard.config.settings import load_config
            config = load_config()
        except Exception:
            config = {}
    cmd = config.get("verification_command")
    if cmd:
        return list(cmd)
    if (cwd / "package.json").exists():
        return ["npm", "test"]
    if (cwd / "pyproject.toml").exists() or (cwd / "tests").is_dir():
        return [sys.executable, "-m", "pytest"]
    return None


def _run_verification(
    cwd: Path, label: str = "[verify]", capture: bool = False, detail: str = "default"
) -> int | tuple[int, str]:
    """Run the detected test command.

    capture=False (default): streams output live, returns exit code as int.
    capture=True: captures stdout+stderr silently, returns (exit_code, output).
    """
    cmd = _detect_command(cwd)
    if cmd is None:
        if not capture:
            click.echo(f"  {label} no test command detected")
        return (0, "") if capture else 0

    if capture:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout or ""

    click.echo(f"  {label} running: {' '.join(cmd)}")
    live = subprocess.run(cmd, cwd=cwd)
    if live.returncode == 0:
        click.echo(f"  {label} passed")
    else:
        click.echo(f"  {label} failed (exit code {live.returncode})")
    return live.returncode
