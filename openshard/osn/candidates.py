"""Parallel candidates: the whole task on several distinct models at once, judged deterministically.

Each candidate is a worker (``openshard.osn.workers``) with the whole repository
as its write scope, in its own isolated copy. When all have finished OpenShard
runs the run's own verify command in every candidate's copy (observed
evidence, never the model's word), then ranks them by a fixed, documented
order:

1. verified in its own copy;
2. fewest writes refused by policy;
3. a usable outcome (``changed``) over anything else;
4. fewest files changed (the smallest verified change wins);
5. lowest provider-reported cost (an unknown cost ranks last);
6. fewest turns;
7. candidate order.

The winner's files are synthesised into the run's copy and verified again
there; the losers stay in their copies and only their records reach the
Receipt. No candidate verified means no winner: the executor runs with an
advisory naming what each candidate tried, and the run continues as a
single-executor run would.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openshard.osn.decompose import Subtask
from openshard.osn.workers import STATUS_CHANGED, WorkerResult, WorkerSpec

CANDIDATE_SCOPE: tuple[str, ...] = ("**",)
EVALUATION_POLICY = "verified_then_fewest_refused_then_fewest_files_then_cheapest_then_fewest_turns"
EVIDENCE = {"verification": "openshard_observed_in_candidate_copy", "ranking": "deterministic_policy"}
REASON_WINNER = "best_verified_candidate"
REASON_NO_WINNER = "no_candidate_verified"
CANDIDATE_MAX_TURNS = 10


def candidate_specs(models: list[tuple[str, str]], task: str, provider_name: str | None) -> list[WorkerSpec]:
    """One spec per (model, source): the whole task, the whole repository as scope."""
    specs: list[WorkerSpec] = []
    for i, (model, source) in enumerate(models):
        cid = f"candidate-{i + 1}"
        subtask = Subtask(
            id=cid, objective=task, allowed_write_paths=CANDIDATE_SCOPE, parallel_safe=True, required=False,
            expected_output="a complete, verified solution to the whole task",
            verification_criteria=("the run's verify command passes in this copy",),
        )
        specs.append(WorkerSpec(worker_id=cid, subtask=subtask, model=model, model_source=source,
                                provider_name=provider_name, max_turns=CANDIDATE_MAX_TURNS))
    return specs


def verify_candidates(results: list[WorkerResult], verify: Any) -> None:
    """Run the run's verify command in every candidate copy that changed something; record the observed result."""
    for r in results:
        if r.status != STATUS_CHANGED or not r.sandbox_path or not r.changed_files:
            r.verification = None if r.verification is None else {**r.verification, "scope": "candidate_copy_stale"}
            continue
        try:
            result, _output = verify(Path(r.sandbox_path), list(r.changed_files))
        except Exception as exc:  # a verifier that cannot start is an environment problem, not a pass
            r.verification = {"status": "unknown", "exit_code": None, "failed_tests": [],
                              "scope": "candidate_copy_observed", "error": type(exc).__name__}
            continue
        r.verification = {
            "status": "unknown" if (result.timed_out or not result.ran) else "passed" if result.passed else "failed",
            "exit_code": result.exit_code, "failed_tests": list(result.failed_tests or []),
            "tainted": bool(result.tainted), "scope": "candidate_copy_observed",
        }


def _verified(r: WorkerResult) -> bool:
    v = r.verification or {}
    return v.get("status") == "passed" and not v.get("tainted") and v.get("scope") == "candidate_copy_observed"


def _sort_key(indexed: tuple[int, WorkerResult]) -> tuple:
    i, r = indexed
    cost = r.cost_usd if isinstance(r.cost_usd, (int, float)) else float("inf")
    return (not _verified(r), len(r.blocked), r.status != STATUS_CHANGED, len(r.changed_files), cost, r.turns, i)


def rank_candidates(results: list[WorkerResult]) -> list[dict[str, Any]]:
    """Every candidate with its rank and why it placed there; the first is the winner when verified."""
    ordered = sorted(enumerate(results), key=_sort_key)
    out: list[dict[str, Any]] = []
    for rank, (i, r) in enumerate(ordered, start=1):
        v = r.verification or {}
        out.append({
            "worker_id": r.worker_id,
            "model": r.model,
            "requested_model": r.requested_model,
            "status": r.status,
            "reason": r.reason,
            "verified": _verified(r),
            "verification": v.get("status"),
            "failed_tests": len(v.get("failed_tests") or []),
            "files_changed": len(r.changed_files),
            "writes_refused": len(r.blocked),
            "cost_usd": r.cost_usd,
            "cost_source": r.cost_source,
            "turns": r.turns,
            "rank": rank,
            "selected": rank == 1 and _verified(r),
            "order": i + 1,
        })
    return out


def select_candidate(results: list[WorkerResult]) -> tuple[WorkerResult | None, dict[str, Any]]:
    """The winner (or None) and the evaluation record for the Receipt."""
    evaluation = rank_candidates(results)
    winner = None
    if evaluation and evaluation[0]["selected"]:
        winner = next(r for r in results if r.worker_id == evaluation[0]["worker_id"])
    costs = [r.cost_usd for r in results]
    losers = [r.cost_usd for r in results if winner is None or r.worker_id != winner.worker_id]
    record = {
        "policy": EVALUATION_POLICY,
        "count": len(results),
        "models": [r.model for r in results],
        "evaluated": evaluation,
        "winner": winner.worker_id if winner else None,
        "winner_model": winner.model if winner else None,
        "reason": REASON_WINNER if winner else REASON_NO_WINNER,
        "candidates_cost_usd": sum(c for c in costs if c is not None) if costs and all(c is not None for c in costs) else None,
        "losers_cost_usd": sum(c for c in losers if c is not None) if losers and all(c is not None for c in losers) else (0.0 if not losers else None),
        "evidence": dict(EVIDENCE),
    }
    return winner, record


def candidates_advisory(record: dict[str, Any]) -> str:
    """What the executor is told when no candidate verified: each candidate's model and how it ended."""
    lines = [f"{record.get('count', 0)} parallel candidate solution(s) were tried on distinct models; none verified. "
             "Solve the task yourself; what each tried, for context:"]
    for e in record.get("evaluated") or []:
        how = e.get("verification") or e.get("status") or "unknown"
        extra = f", {e['failed_tests']} failing test(s)" if e.get("failed_tests") else ""
        lines.append(f"- {e['worker_id']} ({e.get('model')}): {how}{extra}; {e.get('files_changed', 0)} file(s) changed"
                     + (f"; {e['reason']}" if e.get("reason") else ""))
    return "\n".join(lines)


__all__ = [
    "CANDIDATE_MAX_TURNS",
    "CANDIDATE_SCOPE",
    "EVALUATION_POLICY",
    "REASON_NO_WINNER",
    "REASON_WINNER",
    "candidate_specs",
    "candidates_advisory",
    "rank_candidates",
    "select_candidate",
    "verify_candidates",
]
