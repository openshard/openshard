"""Execution topology: an explicit, deterministic decision, recorded in the Receipt.

OSN can run a task as:

* ``single``                      one executor (the default; a one-file fix stays here)
* ``planner_executor``            a read-only planner, then the executor
* ``planner_executor_verifier``   the above plus an independent model review
* ``parallel_subtasks``           planner -> bounded isolated writing workers -> synthesis -> verification
* ``parallel_candidates``         independent candidate solutions, evaluated deterministically

The choice comes from bounded rules over what the harness knows (the user's
request, whether a planner and a verifier are wanted, whether the planner's
decomposition validated, whether the budget leaves headroom, how many
distinct models routing can offer), never from asking a model how many
agents to use. The record keeps what was requested, what was selected and
why, the worker count, and the extra cost expected and actually incurred
where that can be stated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TOPOLOGY_SINGLE = "single"
TOPOLOGY_PLANNER_EXECUTOR = "planner_executor"
TOPOLOGY_PLANNER_EXECUTOR_VERIFIER = "planner_executor_verifier"
TOPOLOGY_PARALLEL_SUBTASKS = "parallel_subtasks"
TOPOLOGY_PARALLEL_CANDIDATES = "parallel_candidates"
TOPOLOGIES: tuple[str, ...] = (
    TOPOLOGY_SINGLE, TOPOLOGY_PLANNER_EXECUTOR, TOPOLOGY_PLANNER_EXECUTOR_VERIFIER,
    TOPOLOGY_PARALLEL_SUBTASKS, TOPOLOGY_PARALLEL_CANDIDATES,
)

REQUEST_AUTO = "auto"
REQUEST_SINGLE = "single"
REQUEST_ROLES = "roles"
REQUEST_PARALLEL = "parallel"
REQUEST_CANDIDATES = "candidates"
REQUESTS: tuple[str, ...] = (REQUEST_AUTO, REQUEST_SINGLE, REQUEST_ROLES, REQUEST_PARALLEL, REQUEST_CANDIDATES)

DEFAULT_MAX_WORKERS = 3
HARD_MAX_WORKERS = 3

REASON_USER_SINGLE = "user_requested_single"
REASON_NO_PLANNER = "no_planner_ran"
REASON_NO_DECOMPOSITION = "planner_proposed_no_decomposition"
REASON_DECOMPOSITION_INVALID = "decomposition_invalid"
REASON_TASK_SIMPLE = "task_not_complex_enough"
REASON_BUDGET = "budget_headroom_insufficient"
REASON_WORKERS_CAPPED = "worker_cap"
REASON_PARALLEL_SELECTED = "independent_subtasks_with_disjoint_scopes"
REASON_CANDIDATES_SELECTED = "difficult_task_with_distinct_models"
REASON_CANDIDATES_NO_MODELS = "fewer_than_two_distinct_models"
REASON_ROLES = "planner_and_or_verifier_wanted"


@dataclass
class TopologyDecision:
    requested: str
    selected: str
    reason: str
    worker_count: int = 0
    max_workers: int = DEFAULT_MAX_WORKERS
    expected_extra_cost_usd: float | None = None  # None: not estimable from available evidence
    expected_extra_cost_basis: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "topology_requested": self.requested,
            "topology_selected": self.selected,
            "topology_reason": self.reason,
            "worker_count": self.worker_count,
            "max_workers": self.max_workers,
            "expected_extra_cost_usd": self.expected_extra_cost_usd,
            "expected_extra_cost_basis": self.expected_extra_cost_basis,
            "notes": list(self.notes)[:6],
        }


def _roles_topology(planner_ran: bool, verifier_wanted: bool) -> str:
    if planner_ran and verifier_wanted:
        return TOPOLOGY_PLANNER_EXECUTOR_VERIFIER
    if planner_ran:
        return TOPOLOGY_PLANNER_EXECUTOR
    return TOPOLOGY_SINGLE


def decide_topology(
    requested: str,
    *,
    planner_ran: bool,
    verifier_wanted: bool,
    decomposition: Any | None,
    task_complex: bool,
    budget_headroom: bool | None,
    distinct_models_available: int,
    max_workers: int = DEFAULT_MAX_WORKERS,
    planner_cost_usd: float | None = None,
) -> TopologyDecision:
    """Pick the simplest topology the evidence justifies.

    *decomposition* is the validated ``Decomposition`` (or None). *budget_headroom*
    is None when no budget is enforced, False when the ledger says another unit
    of work would be refused. *distinct_models_available* counts models routing
    could put on different workers (1 when only one model exists).
    """
    max_workers = max(1, min(int(max_workers), HARD_MAX_WORKERS))
    base = _roles_topology(planner_ran, verifier_wanted)
    base_reason = REASON_ROLES if base != TOPOLOGY_SINGLE else REASON_NO_PLANNER
    if requested == REQUEST_SINGLE:
        return TopologyDecision(requested, TOPOLOGY_SINGLE, REASON_USER_SINGLE, max_workers=max_workers)
    if requested == REQUEST_ROLES:
        return TopologyDecision(requested, base, base_reason, max_workers=max_workers)

    want_parallel = requested in (REQUEST_AUTO, REQUEST_PARALLEL)
    if want_parallel:
        notes: list[str]
        if not planner_ran:
            notes, fallback_reason = [], REASON_NO_PLANNER
        elif decomposition is None:
            notes, fallback_reason = [], REASON_NO_DECOMPOSITION
        elif not getattr(decomposition, "valid", False):
            notes = [f"decomposition:{r}" for r in getattr(decomposition, "reasons", [])][:6]
            fallback_reason = REASON_DECOMPOSITION_INVALID
        elif requested == REQUEST_AUTO and not task_complex:
            notes, fallback_reason = [], REASON_TASK_SIMPLE
        elif budget_headroom is False:
            notes, fallback_reason = [], REASON_BUDGET
        else:
            parallel = list(getattr(decomposition, "parallel_subtasks", []))
            count = min(len(parallel), max_workers)
            notes = [REASON_WORKERS_CAPPED] if len(parallel) > max_workers else []
            expected = planner_cost_usd * count if isinstance(planner_cost_usd, (int, float)) else None
            return TopologyDecision(
                requested, TOPOLOGY_PARALLEL_SUBTASKS, REASON_PARALLEL_SELECTED, worker_count=count,
                max_workers=max_workers, expected_extra_cost_usd=expected,
                expected_extra_cost_basis="planner_cost_per_worker_estimate" if expected is not None else None,
                notes=notes,
            )
        if requested == REQUEST_PARALLEL:
            return TopologyDecision(requested, base, fallback_reason, max_workers=max_workers, notes=notes)
        # auto: fall through to the roles topology, carrying the reason parallel work was not chosen
        return TopologyDecision(requested, base, fallback_reason if base == TOPOLOGY_SINGLE else base_reason,
                                max_workers=max_workers, notes=[fallback_reason, *notes][:6])

    if requested == REQUEST_CANDIDATES:
        if distinct_models_available < 2:
            return TopologyDecision(requested, base, REASON_CANDIDATES_NO_MODELS, max_workers=max_workers)
        if budget_headroom is False:
            return TopologyDecision(requested, base, REASON_BUDGET, max_workers=max_workers)
        count = min(distinct_models_available, max_workers)
        return TopologyDecision(
            requested, TOPOLOGY_PARALLEL_CANDIDATES, REASON_CANDIDATES_SELECTED, worker_count=count,
            max_workers=max_workers,
        )
    return TopologyDecision(requested, base, base_reason, max_workers=max_workers)


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "HARD_MAX_WORKERS",
    "REQUESTS",
    "REQUEST_AUTO",
    "REQUEST_CANDIDATES",
    "REQUEST_PARALLEL",
    "REQUEST_ROLES",
    "REQUEST_SINGLE",
    "TOPOLOGIES",
    "TOPOLOGY_PARALLEL_CANDIDATES",
    "TOPOLOGY_PARALLEL_SUBTASKS",
    "TOPOLOGY_PLANNER_EXECUTOR",
    "TOPOLOGY_PLANNER_EXECUTOR_VERIFIER",
    "TOPOLOGY_SINGLE",
    "TopologyDecision",
    "decide_topology",
]
