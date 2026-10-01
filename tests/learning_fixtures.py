"""Receipt fixtures for Learning Loop tests, shaped like real ``osn_loop`` entries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count

from openshard.history.verification import build_verification

REPO = "github.com/acme/shop"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_seq = count(1)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def osn_entry(
    task: str = "Fix responsive dashboard layout",
    *,
    attempts: list[tuple[str, str]] = (("model/a", "passed"),),
    repo: str | None = REPO,
    category: str | None = "visual",
    cost: float | None = 0.01,
    days_ago: float = 1,
    check: dict | None = None,
    stop_reason: str | None = None,
    files: tuple[str, ...] = ("src/dashboard/layout.tsx",),
    source: str = "directly_observed",
    policy_decisions: list[dict] | None = None,
    learning: dict | None = None,
    record_models: bool = True,
    receipt_id: str | None = None,
) -> dict:
    """One OSN Receipt. *attempts* is ``[(model, state)]`` with state
    ``passed`` / ``failed`` / ``timeout`` / ``setup`` / ``none``."""
    n = next(_seq)
    loop_attempts = []
    for i, (_model, state) in enumerate(attempts, start=1):
        v = None
        if state in ("passed", "failed"):
            v = {"ran": True, "passed": state == "passed", "timed_out": False, "exit_code": 0 if state == "passed" else 1}
        elif state == "timeout":
            v = {"ran": True, "passed": False, "timed_out": True, "exit_code": None}
        elif state == "setup":
            v = {"ran": False, "passed": False, "timed_out": False, "setup_failure": "missing_module"}
        loop_attempts.append({"n": i, "verification": v})
    final_state = attempts[-1][1] if attempts else "none"
    status = {"passed": "verified", "failed": "failed"}.get(final_state, "error")
    reason = stop_reason or {
        "passed": "verification_passed", "failed": "max_attempts_exhausted",
        "timeout": "verifier_timeout", "setup": "verifier_setup_failed",
    }.get(final_state, "provider_error")
    if final_state in ("passed", "failed"):
        verification = build_verification(
            source=source, observation_mode="openshard_executed",
            checks=[{"name": "verify_command", "status": final_state, "kind": "other"}],
            status=final_state, checks_attempted=1,
            checks_passed=1 if final_state == "passed" else 0,
            checks_failed=0 if final_state == "passed" else 1,
        )
    else:
        verification = build_verification(source=None, observation_mode="none", status="not_run")
    models = [m for m, _ in attempts]
    retried = len(attempts) > 1
    entry: dict = {
        "schema_version": "1.2",
        "timestamp": _ts(days_ago),
        "receipt_id": receipt_id or f"rcpt-{n:04d}",
        "shard_id": f"shard-20260930-{n:04d}",
        "task": task,
        "executor": "osn_loop",
        "workflow": "osn_loop",
        "execution_model": models[-1] if models else "unknown",
        "retry_triggered": retried,
        "verification": verification,
        "verification_passed": {"passed": True, "failed": False}.get(final_state),
        "duration_seconds": 12.5,
        "files_detail": [{"path": f, "change_type": "update"} for f in files],
        "osn_loop": {"status": status, "stop_reason": reason, "attempts": loop_attempts},
        "routing_provenance": {
            "record_mode": "shadow",
            "executed_model": models[-1] if models else None,
            "context": {"task_category": category, "category_source": "keyword_classifier",
                        "harness": "osn_loop"} if category else {"harness": "osn_loop"},
        },
    }
    if repo:
        entry["repo_identity"] = repo
    if retried:
        entry["fixer_model"] = models[-1] if models[-1] != models[0] else None
        entry["estimated_cost"] = cost
        entry["retry_estimated_cost"] = cost
        entry["retry_attempts"] = [{"model": m} for m in models[1:]]
    else:
        entry["estimated_cost"] = cost
    block = dict(learning or {})
    if record_models:
        block.setdefault("attempt_models", [{"attempt": i, "model": m} for i, m in enumerate(models, start=1)])
    if check:
        block["check"] = check
    if policy_decisions:
        entry["policy_decisions"] = policy_decisions
    if block:
        entry["learning"] = block
    return entry


MOBILE_CHECK = {"fingerprint": "f00dcafe00000001", "label": "pnpm test:e2e -- mobile",
                "label_complete": True, "kind": "test"}
UNIT_CHECK = {"fingerprint": "f00dcafe00000002", "label": "pnpm test", "label_complete": True, "kind": "test"}
