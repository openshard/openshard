"""Turn an OSN loop receipt into a ``runs.jsonl`` Shard entry.

Evidence rules: verification is written as a stored block with source
``directly_observed`` / mode ``openshard_executed`` only when OpenShard itself
ran the verify command and read its exit code; a run that never reached
verification records ``not_run``. Model cost is recorded only when the
provider reported it. The routing block is a *shadow* decision: it records
what the adaptive baseline would pick next to the model actually executed and
never influences the choice.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.verification import (
    MODE_NONE,
    MODE_OPENSHARD_EXECUTED,
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


def _shadow_provenance(task: str, executed_model: str, verification_available: bool) -> dict | None:
    """What the adaptive baseline would choose, recorded beside the executed model."""
    try:
        from openshard.routing.adaptive import shadow_decision_for_run
        from openshard.routing.engine import route

        category = route(task).category
        decision = shadow_decision_for_run(
            task_category=category, read_only=False, write_requested=True, risk=None,
            verification_available=verification_available, verification_requested=True,
            harness=EXECUTOR,
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
        "task": safe_task,
        "task_title": derive_task_title(safe_task),
        "execution_model": final_model,
        "executor": EXECUTOR,
        "workflow": "osn_loop",
        "duration_seconds": round(duration_seconds, 3),
        "retry_triggered": retry,
        "verification_attempted": bool(verified_attempts),
        "verification_passed": (
            verified_attempts[-1].verification.passed if verified_attempts else None  # type: ignore[union-attr]
        ),
        "verification": verification,
        "files_created": files_created,
        "files_updated": files_updated,
        "files_deleted": 0,
        "files_detail": files_detail,
        "summary": f"OSN loop {receipt.status}: {receipt.stop_reason}",
        "osn_loop": _stored_loop_block(receipt),
    }
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

    prov = _shadow_provenance(safe_task, final_model, verification_available=True)
    if prov is not None:
        entry["routing_provenance"] = prov

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
