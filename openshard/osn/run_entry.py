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


def _verification_block(receipt: LoopReceipt) -> dict[str, Any]:
    last = next((a.verification for a in reversed(receipt.attempts) if a.verification), None)
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


def _stored_loop_block(receipt: LoopReceipt) -> dict[str, Any]:
    d = receipt.to_dict()
    d.pop("sandbox_path", None)  # absolute local path; not stored
    return d


def _sum_costs(usage: list[AttemptUsage]) -> float | None:
    if not usage or any(u.cost_usd is None for u in usage):
        return None
    return sum(u.cost_usd for u in usage if u.cost_usd is not None)


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
) -> dict:
    from openshard.adapters.claude_code_import import _sanitize_model, _sanitize_task
    from openshard.history.receipt_identity import ensure_receipt_id
    from openshard.history.shard_contract import _make_shard_id
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
    from openshard.history.task_identity import ensure_task_id
    from openshard.history.task_title import derive_task_title

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_task = _sanitize_task(task, placeholder="OSN loop task", cap=500)
    first_model = _sanitize_model(usage[0].model) if usage else "unknown"
    final_model = _sanitize_model(usage[-1].model) if usage else "unknown"
    verified_attempts = [a for a in receipt.attempts if a.verification is not None and a.verification.ran]
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
        first_cost = _sum_costs([u for u in usage if u.attempt == 1])
        retry_cost = _sum_costs([u for u in usage if u.attempt > 1])
        entry["estimated_cost"] = first_cost
        entry["retry_estimated_cost"] = retry_cost
        attempts = _retry_attempts(usage)
        if attempts:
            entry["retry_attempts"] = attempts
    else:
        entry["estimated_cost"] = _sum_costs(usage)
    entry["prompt_tokens"] = sum(u.prompt_tokens for u in usage)
    entry["completion_tokens"] = sum(u.completion_tokens for u in usage)
    entry["total_tokens"] = entry["prompt_tokens"] + entry["completion_tokens"]
    if usage:
        entry["tokens_provenance"] = "provider_reported"

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
        from openshard.config.settings import load_config_safe
        from openshard.safety.sanitize import sanitize_text

        config, valid, _ = load_config_safe(cwd=repo_path)
        identity = config.get("identity") if valid and isinstance(config, dict) else None
        owner = sanitize_text(identity.get("owner"), 120) if isinstance(identity, dict) else None
        if owner:
            entry["owner"] = owner
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
