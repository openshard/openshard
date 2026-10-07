"""Turn an OSN loop receipt into a ``runs.jsonl`` Shard entry.

Evidence rules: verification is written as a stored block with source
``directly_observed`` / mode ``openshard_executed`` only when OpenShard itself
ran the verify command and read its exit code; a run that never reached
verification records ``not_run``. Model cost is recorded only when the
provider reported it. The routing provenance block says how the adaptive
decision related to the run: ``record_mode: shadow`` when it was only recorded
beside the model actually executed, ``applied`` when (behind the
``adaptive_routing`` capability) it chose the first model.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.verification import (
    MODE_NONE,
    MODE_OPENSHARD_EXECUTED,
    REASON_CHECK_NOT_COMPLETED,
    SOURCE_DIRECTLY_OBSERVED,
    build_verification,
)
from openshard.osn.loop import LoopReceipt
from openshard.osn.model_provider import AttemptUsage

EXECUTOR = "osn_loop"


def _effective_attempts(receipt: LoopReceipt) -> list:
    """Attempts whose effects remain (a reverted review recovery describes bytes that were undone)."""
    getter = getattr(receipt, "effective_attempts", None)
    return list(getter()) if callable(getter) else list(receipt.attempts)


def _verification_block(receipt: LoopReceipt) -> dict[str, Any]:
    last = next((a.verification for a in reversed(_effective_attempts(receipt)) if a.verification), None)
    if last is None or not last.ran:
        # No outcome was observed: nothing ran, or the verifier could not even
        # be started. That is unknown evidence, never a pass or a model failure.
        return build_verification(
            source=None, observation_mode=MODE_NONE, status="not_run",
            reason=f"verification did not run ({receipt.stop_reason})",
            incomplete_reasons=["verifier_setup_failed"] if getattr(last, "setup_failure", None) else None,
        )
    if last.timed_out:
        return build_verification(
            source=SOURCE_DIRECTLY_OBSERVED,
            observation_mode=MODE_OPENSHARD_EXECUTED,
            checks=[{"name": "verify_command", "status": "unknown", "kind": "other", "exit_code": None}],
            status="unknown",
            exit_code=None,
            checks_attempted=1,
            checks_passed=0,
            checks_failed=0,
            reason="verification command timed out before an outcome was observed",
            incomplete_reasons=[REASON_CHECK_NOT_COMPLETED],
        )
    status = "passed" if last.passed else "failed"
    return build_verification(
        source=SOURCE_DIRECTLY_OBSERVED,
        observation_mode=MODE_OPENSHARD_EXECUTED,
        checks=[{"name": "verify_command", "status": status, "kind": "other",
                 "exit_code": last.exit_code}],
        status=status,
        exit_code=last.exit_code,
        checks_attempted=1,
        checks_passed=1 if last.passed else 0,
        checks_failed=0 if last.passed else 1,
        reason="timed out" if last.timed_out else None,
    )


MAX_MODEL_CALLS = 60


def _stored_loop_block(receipt: LoopReceipt) -> dict[str, Any]:
    d = receipt.to_dict()
    d.pop("sandbox_path", None)  # absolute local path; not stored
    attempts = [a for a in d.get("attempts") or [] if isinstance(a, dict) and "actions" in a]
    if attempts:
        # Iterative runs: how the agent spent its turns, summed over attempts.
        totals: dict[str, int] = {}
        for a in attempts:
            for key, value in (a.get("action_summary") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
        d["action_summary"] = totals
        d["turns_total"] = sum(int(a.get("turns") or 0) for a in attempts)
    return d


def _sum_costs(usage: list[AttemptUsage]) -> float | None:
    if not usage or any(u.cost_usd is None for u in usage):
        return None
    return sum(u.cost_usd for u in usage if u.cost_usd is not None)


_STAGE_FOR_ROLE = {"planner": "planning", "executor": "implementation", "verifier": "review"}
_ROLE_ORDER = {"planner": 0, "executor": 1, "verifier": 2}


def _ordered_usage(usage: list[AttemptUsage]) -> list[AttemptUsage]:
    """Model calls in run order: the planner's (attempt 0), then per attempt the executor's calls, then its review."""
    indexed = list(enumerate(usage))
    indexed.sort(key=lambda iu: (iu[1].attempt, _ROLE_ORDER.get(getattr(iu[1], "role", "executor"), 1), iu[0]))
    return [u for _, u in indexed]


def _worker_usage_in_order(receipt: LoopReceipt, usage: list[AttemptUsage]) -> list[AttemptUsage]:
    """Worker calls ordered by worker id (threads finish in any order; the Receipt must not)."""
    order = {str(w.get("worker_id")): i for i, w in enumerate(getattr(receipt, "workers", None) or [])
             if isinstance(w, dict)}
    mine = [u for u in usage if getattr(u, "role", "executor") == "worker"]
    return sorted(mine, key=lambda u: (order.get(str(getattr(u, "worker_id", "")), len(order)), u.turn))


def _implementation_models(receipt: LoopReceipt, usage: list[AttemptUsage]) -> list[str]:
    """Every model that made implementation calls (executor and workers), first use first."""
    out: list[str] = []
    for u in [*[u for u in usage if getattr(u, "role", "executor") == "executor"],
              *_worker_usage_in_order(receipt, usage)]:
        if u.model not in out:
            out.append(u.model)
    return out


def _record_roles(entry: dict, receipt: LoopReceipt, usage: list[AttemptUsage], final_model: str) -> None:
    """Role evidence: ``osn_loop.roles`` (executor filled from its calls), ``stage_runs`` and a tier dispatch receipt.

    ``stage_runs`` (planning / implementation / review) is the existing per-stage
    usage record every Receipt surface already renders; ``tier_dispatch_receipt``
    is the existing role-dispatch truth, with ``*_model_actual`` set only for a
    role that really made a call. A role that did not run says ``skipped`` and
    why; usage a provider did not report stays ``None``.
    """
    from openshard.osn.roles import ROLE_EXECUTOR, ROLE_WORKER, RoleRun

    roles: dict[str, Any] = dict(entry["osn_loop"].get("roles") or {})
    worker_records = [w for w in (getattr(receipt, "workers", None) or []) if isinstance(w, dict)]
    executor_calls = [u for u in usage if u.role == ROLE_EXECUTOR]
    if worker_records and not executor_calls and receipt.attempts and receipt.attempts[0].parallel_stage:
        # The workers' synthesised files verified without an executor turn: the
        # executor was not needed, which is not a failure.
        executor = RoleRun.from_usage(ROLE_EXECUTOR, usage, choice=None, provider=None,
                                      status="skipped", reason="workers_synthesised_cleanly")
    else:
        executor = RoleRun.from_usage(ROLE_EXECUTOR, usage, choice=None, provider=None)
    executor_record = executor.to_record()
    executor_record.pop("actions", None)
    executor_record["turns"] = entry["osn_loop"].get("turns_total")
    executor_record["source"] = "routing"
    roles[ROLE_EXECUTOR] = executor_record
    worker_usage = [u for u in usage if u.role == ROLE_WORKER]
    if worker_usage:
        # Workers are one role with several agents: the aggregate here, each
        # worker's own model/usage/outcome under ``osn_loop.workers``.
        worker_run = RoleRun.from_usage(
            ROLE_WORKER, usage, choice=None, provider=None,
            turns=sum(int(w.get("turns") or 0) for w in worker_records) or None,
        )
        worker_record = worker_run.to_record()
        worker_record.pop("actions", None)
        worker_record["source"] = "routing"
        worker_record["workers"] = len(worker_records)
        worker_record["models"] = sorted({u.model for u in worker_usage})
        roles[ROLE_WORKER] = worker_record
    entry["osn_loop"]["roles"] = roles
    if len(roles) == 1 and receipt.mode != "turns" and not usage:
        return

    stage_runs: list[dict[str, Any]] = []
    for role in ("planner", "executor", "verifier"):
        rec = roles.get(role)
        if not isinstance(rec, dict) or rec.get("status") != "ran" or not rec.get("model"):
            continue
        duration = rec.get("duration_ms")
        stage_runs.append({
            "stage_type": _STAGE_FOR_ROLE[role],
            "model": rec["model"],
            "duration": round(duration / 1000.0, 3) if isinstance(duration, int) else None,
            "cost": rec.get("cost_usd"),
            "summary": f"OSN {role}",
            "tokens_input": rec.get("prompt_tokens"),
            "tokens_output": rec.get("completion_tokens"),
        })
    for w in (getattr(receipt, "workers", None) or []):
        if not isinstance(w, dict) or not w.get("model"):
            continue
        duration = w.get("duration_ms")
        stage_runs.append({
            "stage_type": _STAGE_FOR_ROLE["executor"],
            "model": w["model"],
            "duration": round(duration / 1000.0, 3) if isinstance(duration, int) else None,
            "cost": w.get("cost_usd"),
            "summary": f"OSN {w.get('worker_id') or 'worker'} ({w.get('subtask_id') or 'subtask'})",
            "tokens_input": w.get("prompt_tokens"),
            "tokens_output": w.get("completion_tokens"),
        })
    if len(stage_runs) > 1:
        # One stage alone is the plain execution model; several stages are worth listing.
        entry["stage_runs"] = stage_runs

    planner, verifier = roles.get("planner"), roles.get("verifier")
    if isinstance(planner, dict) or isinstance(verifier, dict):
        def _model(rec: Any) -> str | None:
            return rec.get("requested_model") or rec.get("model") if isinstance(rec, dict) else None

        def _actual(rec: Any) -> str | None:
            return rec.get("model") if isinstance(rec, dict) and rec.get("status") == "ran" else None

        validator_status = (
            "applied" if isinstance(verifier, dict) and verifier.get("status") == "ran" else "skipped"
        )
        implementation = entry["osn_loop"].get("implementation_models") or []
        worker_models = sorted({str(w.get("model")) for w in worker_records if w.get("model")})
        warnings: list[str] = []
        if worker_models:
            warnings.append(f"implementation by {len(worker_records)} parallel worker(s): " + ", ".join(worker_models))
        entry["tier_dispatch_receipt"] = {
            "enabled": True,
            "applied": True,
            "tier_source": "osn_roles",
            "planner_tier": (planner or {}).get("source") or "",
            "planner_model": _model(planner),
            "executor_tier": "routing",
            "executor_model": final_model,
            "validator_tier": (verifier or {}).get("source") or "",
            "validator_model": _model(verifier),
            "planner_model_actual": _actual(planner),
            "executor_model_actual": final_model if implementation else None,
            "validator_model_actual": _actual(verifier),
            "validator_dispatch_status": validator_status,
            "fallback_used": False,
            "fallback_reason": "",
            "warnings": warnings,
        }


def _record_economics(entry: dict, receipt: LoopReceipt, usage: list[AttemptUsage]) -> None:
    """``osn_loop.economics``: what the run cost per role, worker, model and attempt, and per verified success.

    Every figure is a sum of provider-reported or list-rate figures recorded per
    call; a role or worker with an unknown call cost makes that figure None and
    the total incomplete. ``cost_per_verified_success`` is the complete total
    when the run verified, else None: unknown is never zero.
    """
    def _sum(items: list[float | None]) -> float | None:
        return sum(c for c in items if c is not None) if items and all(c is not None for c in items) else None

    by_role: dict[str, float | None] = {}
    by_model: dict[str, float | None] = {}
    by_attempt: dict[str, float | None] = {}
    for role in sorted({getattr(u, "role", "executor") for u in usage}):
        by_role[role] = _sum([u.cost_usd for u in usage if getattr(u, "role", "executor") == role])
    for model in sorted({u.model for u in usage}):
        by_model[model] = _sum([u.cost_usd for u in usage if u.model == model])
    for n in sorted({u.attempt for u in usage}):
        by_attempt[str(n)] = _sum([u.cost_usd for u in usage if u.attempt == n])
    by_worker = {
        str(w.get("worker_id")): (w.get("cost_usd") if isinstance(w.get("cost_usd"), (int, float)) else None)
        for w in getattr(receipt, "workers", []) or [] if isinstance(w, dict)
    }
    total = _sum([u.cost_usd for u in usage])
    verified = receipt.status == "verified"
    entry["osn_loop"]["economics"] = {
        "total_cost_usd": total,
        "cost_complete": total is not None and bool(usage),
        "model_calls": len(usage),
        "verified": verified,
        "cost_per_verified_success": total if (verified and total is not None) else None,
        "by_role": by_role,
        "by_worker": by_worker,
        "by_model": by_model,
        "by_attempt": by_attempt,
        "evidence": "provider_usage_per_call",
    }


def _cost_provenance(usage: list[AttemptUsage]) -> str | None:
    """How the run's cost figure was obtained, from every call's own provenance.

    ``provider_reported`` only when every call's cost is the provider's own
    figure; ``official_rate_estimate`` when any call's cost is OpenShard's
    list-rate arithmetic (the whole figure is then an estimate); ``None`` when
    a cost is unknown or a call did not say where its figure came from.
    """
    from openshard.models.pricing import COST_PROVENANCE_OFFICIAL_RATE
    from openshard.providers.base import COST_SOURCE_LIST_RATE, COST_SOURCE_PROVIDER

    if not usage or any(u.cost_usd is None for u in usage):
        return None
    sources = {u.cost_source for u in usage}
    if sources == {COST_SOURCE_PROVIDER}:
        return COST_SOURCE_PROVIDER
    if sources <= {COST_SOURCE_PROVIDER, COST_SOURCE_LIST_RATE}:
        return COST_PROVENANCE_OFFICIAL_RATE
    return None


def _file_effects(changed: list[str], repo_path: Path) -> tuple[list[dict], int, int]:
    """``(files_detail, created, updated)`` for the files the loop applied.

    The type is read from the real repository as it stands when the entry is
    built (the loop only ever wrote to an isolated copy, and promotion happens
    afterwards): a path that is already a regular file there is an ``update``,
    a path that is absent from a readable repository is a ``create``. Anything
    else (unreadable, a directory, a symlink, an unsafe path) is the neutral
    ``changed``, never a guess. Neutral files are counted by the receipt from
    the list itself.
    """
    detail: list[dict] = []
    created = updated = 0
    for p in changed:
        kind = "changed"
        try:
            target = repo_path / p
            if repo_path.is_dir() and not target.is_symlink():
                if target.is_file():
                    kind = "update"
                elif not target.exists():
                    kind = "create"
        except (OSError, ValueError):
            kind = "changed"
        if kind == "update":
            updated += 1
        elif kind == "create":
            created += 1
        detail.append({"path": p, "change_type": kind})
    return detail, created, updated


def _retry_attempts(usage: list[AttemptUsage]) -> list[dict]:
    """One record per retry attempt (attempt 2 onwards): the model that ran it and what it spent."""
    out: list[dict] = []
    for n in sorted({u.attempt for u in usage if u.attempt > 1}):
        uses = [u for u in usage if u.attempt == n]
        prompt = sum(u.prompt_tokens for u in uses)
        completion = sum(u.completion_tokens for u in uses)
        out.append({
            "model": uses[-1].model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "estimated_cost": _sum_costs(uses),
        })
    return out


def _human_summary(receipt: LoopReceipt) -> str:
    """Short result text that explains the run at a glance."""
    if receipt.status == "verified":
        if len(receipt.attempts) > 1:
            return f"Verified after {len(receipt.attempts)} attempts."
        return "Verified on the first attempt."
    if receipt.stop_reason == "provider_error":
        return (
            "Provider failed before verification; repository unchanged."
            if not receipt.changed_files
            else "Provider failed during recovery; repository unchanged."
        )
    if receipt.stop_reason == "verifier_timeout":
        return "Verification timed out; no result was claimed."
    if receipt.stop_reason == "verifier_setup_failed":
        return "Verification could not run; no result was claimed."
    if receipt.status == "blocked":
        return "Blocked by policy before changes could be promoted."
    if receipt.status == "budget_exhausted":
        return "Stopped by the configured agent budget."
    if receipt.status == "no_actions":
        return "Model returned no usable changes."
    if receipt.status == "failed":
        return "Verification failed; repository unchanged."
    return f"OSN run ended: {receipt.stop_reason}."


MAX_POLICY_DECISIONS = 50
APPROVAL_SOURCE = "file_mutation_policy"


def _policy_decisions(receipt: LoopReceipt) -> list[dict]:
    """Every write decision the loop observed, oldest attempt first, bounded."""
    out: list[dict] = []
    for a in receipt.attempts:
        for d in getattr(a, "decisions", None) or []:
            if isinstance(d, dict) and d.get("decision_id") and d.get("decision"):
                out.append(dict(d))
            if len(out) >= MAX_POLICY_DECISIONS:
                return out
    return out


_PERMISSION_RANK = {"granted": 0, "requested": 1, "blocked": 2}


def _permission_evidence(receipt: LoopReceipt, decisions: list[dict]) -> list[dict]:
    """Explicit capability outcomes only; never paths, argv or free text."""
    from openshard.history.receipt_evidence import permission_scopes_block

    items = permission_scopes_block(decisions) or []
    command = getattr(receipt, "command_decision", None)
    if isinstance(command, dict):
        scope = command.get("scope")
        state = command.get("state")
        if scope == "verification:execute" and state in _PERMISSION_RANK:
            items.append({"scope": scope, "state": state})

    strictest: dict[str, str] = {}
    for item in items:
        scope = item.get("scope")
        state = item.get("state")
        if not isinstance(scope, str) or state not in _PERMISSION_RANK:
            continue
        previous = strictest.get(scope)
        if previous is None or _PERMISSION_RANK[state] > _PERMISSION_RANK[previous]:
            strictest[scope] = state
    return [{"scope": scope, "state": strictest[scope]} for scope in sorted(strictest)]


APPROVAL_GRANTED = "granted"
APPROVAL_REFUSED = "refused"
APPROVAL_UNANSWERED = "unanswered"
APPROVAL_APPROVER_ERROR = "approver_error"


def _approval_receipt(decisions: list[dict]) -> dict | None:
    """What approval was needed and what came of it, from the ask decisions alone.

    ``granted`` is True only when every sensitive write was approved. The
    structured ``outcome`` keeps the cases apart that a boolean cannot: an
    approver said no (``refused``), no approver existed so the loop failed
    closed (``unanswered``), or the approver itself failed (``approver_error``).
    Nobody is recorded as having refused unless somebody did.
    """
    asks = [d for d in decisions if d.get("decision") == "ask"]
    if not asks:
        return None
    granted = [d for d in asks if d.get("approval_granted") is True]
    errored = [d for d in asks if d.get("approval_granted") is False
               and d.get("approval_source") == APPROVAL_APPROVER_ERROR]
    refused = [d for d in asks if d.get("approval_granted") is False and d not in errored]
    unanswered = [d for d in asks if d.get("approval_granted") is None]
    if refused:
        outcome, reason = APPROVAL_REFUSED, f"approval refused for {len(refused)} sensitive path(s)"
    elif errored:
        outcome, reason = APPROVAL_APPROVER_ERROR, f"the approver failed for {len(errored)} sensitive path(s); treated as not approved"
    elif unanswered:
        outcome = APPROVAL_UNANSWERED
        reason = f"approval required for {len(unanswered)} sensitive path(s) but no approver was available"
    else:
        outcome, reason = APPROVAL_GRANTED, f"approval granted for {len(granted)} sensitive path(s)"
    return {
        "source": APPROVAL_SOURCE,
        "requested": True,
        "granted": outcome == APPROVAL_GRANTED,
        "outcome": outcome,
        "action": "file_write",
        "reason": reason,
    }


def _shadow_provenance(
    task: str, executed_model: str | None, verification_available: bool, explicit_model: str | None = None,
) -> dict | None:
    """What the adaptive baseline would choose, recorded beside the executed model."""
    try:
        from openshard.routing.adaptive import shadow_decision_for_run
        from openshard.routing.engine import route

        category = route(task).category
        decision = shadow_decision_for_run(
            task_category=category, read_only=False, write_requested=True, risk=None,
            verification_available=verification_available, verification_requested=True,
            harness=EXECUTOR, explicit_model=explicit_model,
        )
        if decision is None:
            return None
        return decision.to_provenance(record_mode="shadow", executed_model=executed_model)
    except Exception:
        return None


def build_osn_run_entry(
    receipt: LoopReceipt,
    *,
    task: str,
    usage: list[AttemptUsage],
    duration_seconds: float,
    repo_path: Path,
    task_id: str | None = None,
    budget_record: dict | None = None,
    routing_decision: Any | None = None,
    routing_record_mode: str = "shadow",
    routing_record: dict | None = None,
    explicit_model: str | None = None,
    supervisor_record: dict | None = None,
    capability_snapshot: dict | None = None,
    organisation_policy: dict | None = None,
    learning_record: dict | None = None,
) -> dict:
    from openshard.adapters.claude_code_import import _sanitize_model, _sanitize_task
    from openshard.history.receipt_identity import ensure_receipt_id
    from openshard.history.shard_contract import _make_shard_id
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
    from openshard.history.task_identity import ensure_task_id
    from openshard.history.task_title import derive_task_title

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_task = _sanitize_task(task, placeholder="OSN loop task", cap=500)
    # The run's model is the executor's: planner and verifier calls are recorded
    # per role and per call, never flattened into the execution model.
    usage = _ordered_usage(usage)
    executor_usage = [u for u in usage if getattr(u, "role", "executor") == "executor"]
    implementation_models = _implementation_models(receipt, usage)
    workers_only = not executor_usage
    if workers_only:
        # No executor call: the implementation, if any, was parallel workers'.
        # Their calls, in worker order, stand in for the executor's; a planner's
        # or verifier's model never becomes the execution model.
        executor_usage = _worker_usage_in_order(receipt, usage)
    first_model = _sanitize_model(executor_usage[0].model) if executor_usage else "unknown"
    final_model = _sanitize_model(executor_usage[-1].model) if executor_usage else "unknown"
    if workers_only:
        # Workers run side by side: there is no "final" rung, so the execution
        # model is the first worker's (the primary routed model); every worker's
        # model is listed in ``osn_loop.implementation_models`` and per worker.
        final_model = first_model
    verified_attempts = [
        a for a in _effective_attempts(receipt) if a.verification is not None and a.verification.ran
    ]
    retry = len(receipt.attempts) > 1
    verification = _verification_block(receipt)
    files_detail, files_created, files_updated = _file_effects(receipt.changed_files, repo_path)

    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": now,
        "repo_name": repo_path.name,
        "task": safe_task,
        "task_title": derive_task_title(safe_task),
        "execution_model": final_model,
        "executor": EXECUTOR,
        "workflow": "osn_loop",
        "duration_seconds": round(duration_seconds, 3),
        "retry_triggered": retry,
        "verification_attempted": bool(verified_attempts),
        "verification_passed": (
            None
            if verified_attempts and verified_attempts[-1].verification.timed_out  # type: ignore[union-attr]
            else verified_attempts[-1].verification.passed if verified_attempts else None  # type: ignore[union-attr]
        ),
        "verification": verification,
        "files_created": files_created,
        "files_updated": files_updated,
        "files_deleted": 0,
        "files_detail": files_detail,
        "summary": f"OSN loop {receipt.status}: {receipt.stop_reason}",
        "human_summary": _human_summary(receipt),
        "osn_loop": _stored_loop_block(receipt),
        "write_path": "sandbox",
        "sandbox": {"sandbox_enabled": True, "sandbox_type": "isolated_copy"},
    }
    decisions = _policy_decisions(receipt)
    permissions = _permission_evidence(receipt, decisions)
    if permissions:
        entry["permission_evidence"] = permissions
    if decisions:
        # The file gate's allow / ask / deny per proposed write, so history, failure
        # classification and trust scoring see an OSN policy block as a policy block.
        entry["policy_decisions"] = decisions
        approval = _approval_receipt(decisions)
        if approval is not None:
            sources = sorted({
                s for a in receipt.attempts for s in ((a.policy or {}).get("approval_sources") or [])
            })
            if sources:
                approval["approval_sources"] = sources
            entry["approval_receipt"] = approval
    if budget_record:
        # Agent Budgets: configured limits, observed usage, the limit reached and
        # what OpenShard did -- or, when the capability was off/unconfirmed,
        # the configured limits and the fact that they were not enforced.
        entry["agent_budgets"] = dict(budget_record)
    if retry and usage:
        entry["fixer_model"] = final_model if final_model != first_model else None
        # Attempt 1 carries the planner's calls (attempt 0) and any verifier call
        # made for it, so the first-attempt cost is the whole run minus retries.
        first_cost = _sum_costs([u for u in usage if u.attempt <= 1])
        retry_cost = _sum_costs([u for u in usage if u.attempt > 1])
        entry["estimated_cost"] = first_cost
        entry["retry_estimated_cost"] = retry_cost
        attempts = _retry_attempts(usage)
        if attempts:
            entry["retry_attempts"] = attempts
    else:
        entry["estimated_cost"] = _sum_costs(usage)
    entry["osn_loop"]["implementation_models"] = implementation_models
    _record_roles(entry, receipt, usage, final_model)
    _record_economics(entry, receipt, usage)
    entry["prompt_tokens"] = sum(u.prompt_tokens for u in usage)
    entry["completion_tokens"] = sum(u.completion_tokens for u in usage)
    entry["total_tokens"] = entry["prompt_tokens"] + entry["completion_tokens"]
    if usage:
        entry["tokens_provenance"] = "provider_reported"
        cached = [u.cache_read_tokens for u in usage]
        if any(c is not None for c in cached):
            entry["cache_read_tokens"] = sum(c or 0 for c in cached)
        provenance = _cost_provenance(usage)
        if provenance:
            entry["cost_provenance"] = provenance
        # Every model call this run made: attempt, turn, role, requested and
        # reported model, tokens, cost and where that cost figure came from.
        entry["osn_loop"]["model_calls"] = [u.to_record() for u in usage[:MAX_MODEL_CALLS]]
        if len(usage) > MAX_MODEL_CALLS:
            entry["osn_loop"]["model_calls_truncated"] = True

    try:
        from openshard.analysis.repo_map import collect_git_info

        git = collect_git_info(repo_path)
        if git.branch:
            entry["git_branch"] = git.branch
        if git.head_commit:
            entry["git_head_commit_hash"] = git.head_commit
        entry["git_dirty"] = git.dirty
    except Exception:
        pass

    try:
        from openshard.config.settings import stamp_owner

        stamp_owner(entry, repo_path)
    except Exception:
        pass

    if receipt.stop_reason == "provider_error":
        failed = next((a for a in reversed(receipt.attempts) if a.error_class), None)
        entry["error_class"] = "provider_error"
        if failed is not None and failed.error_message:
            entry["error_message"] = failed.error_message

    setup_kind = next(
        (a.verification.setup_failure for a in reversed(receipt.attempts)
         if a.verification is not None and a.verification.setup_failure),
        None,
    )
    if setup_kind:
        from openshard.verification.setup_failure import setup_failure_metadata

        # Attribute the outcome to the environment; keep the not_run verification block above.
        entry["outcome_classification"] = setup_failure_metadata(
            setup_kind, None, model=final_model, attempt=len(receipt.attempts),
        )["outcome_classification"]

    executed_first: str | None = first_model if usage else None  # None: no model call ever ran
    executed_final: str | None = final_model if usage else None
    if routing_decision is not None:
        # The decision the CLI computed for this run (it knows about --model and,
        # when applied, chose the first model). An applied decision is compared
        # with the model that ran first; escalation past it is the plan working.
        try:
            executed = executed_first if routing_record_mode == "applied" else executed_final
            prov = routing_decision.to_provenance(record_mode=routing_record_mode, executed_model=executed)
        except Exception:
            prov = None
    elif routing_record:
        # The CLI tried and could not compute a decision; a second attempt here
        # would contradict the recorded reason. Nothing is recomputed.
        prov = None
    else:
        prov = _shadow_provenance(safe_task, executed_final, verification_available=True,
                                  explicit_model=explicit_model)
    if prov is not None:
        entry["routing_provenance"] = prov
    if routing_record:
        entry["adaptive_routing"] = dict(routing_record)
    if supervisor_record:
        # Supervisor routing: each decision taken at an observed-failure boundary,
        # whether it was acted on, and the evidence it had. Present whenever the
        # capability governed the run, even if it was never consulted.
        entry["supervisor_routing"] = dict(supervisor_record)
    if capability_snapshot:
        # Which Platform capabilities governed this run, read once at its start.
        entry["capability_snapshot"] = dict(capability_snapshot)
    if organisation_policy:
        # Policy identity only. The full organisation document never enters the Receipt.
        entry["organisation_policy"] = dict(organisation_policy)
    if learning_record:
        # Learning Loop V1: which prior signals this run consulted, whether they
        # reached the model, routing or verification, and what later runs can
        # learn from this one. Compact; the signals themselves stay re-derivable.
        entry["learning"] = dict(learning_record)

    try:
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD, capture_repo_identity
        ident = capture_repo_identity(repo_path)
        if ident:
            entry[REPO_IDENTITY_FIELD] = ident
    except Exception:
        pass

    entry["run_id"] = entry["timestamp"]
    entry["shard_id"] = _make_shard_id(entry["timestamp"], None)
    entry["attempt_number"] = 1
    ensure_receipt_id(entry)
    ensure_task_id(entry, task_id)
    return coerce_shard_entry(entry)
