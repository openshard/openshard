"""Step types: the routing boundaries OpenShard can honestly identify today.

Trajectory-aware routing re-decides the model at each step of a run instead of
choosing one tier up front. That only means something where a run really has
step boundaries OpenShard observes. Today that is the OSN loop:

    execute   the first proposal for the task (attempt 1)
    repair    a later attempt, after OpenShard itself observed the previous
              attempt fail verification and the loop decided to retry

Inspection (listing the repository) and verification (running the verify
command) are OpenShard's own work, not model calls, so they are not routed and
are not step types here. Planning and review stages exist in the ``openshard
run`` pipeline, but that path still runs legacy routing and records only a
shadow decision; when it is driven by Routing V2 those names can be added.
Nothing here is inferred from task text.
"""
from __future__ import annotations

STEP_EXECUTE = "execute"
STEP_REPAIR = "repair"

# Every step type a context may carry today.
STEP_TYPES: frozenset[str] = frozenset({STEP_EXECUTE, STEP_REPAIR})

# What the OSN loop emits, and at which boundary.
OSN_STEP_TYPES: dict[str, str] = {
    STEP_EXECUTE: "attempt 1: first proposal",
    STEP_REPAIR: "attempt n>1: retry after an observed verification failure",
}

# Failure classifications routing may react to. OSN retries only after an
# observed verification failure, so that is the only class reaching routing.
FAILURE_VERIFICATION_FAILED = "verification_failed"
FAILURE_CLASSES: frozenset[str] = frozenset({FAILURE_VERIFICATION_FAILED})


def step_type_for_attempt(attempt: int) -> str:
    """The OSN step type of attempt number *attempt* (1-based)."""
    return STEP_EXECUTE if attempt <= 1 else STEP_REPAIR


__all__ = [
    "FAILURE_CLASSES",
    "FAILURE_VERIFICATION_FAILED",
    "OSN_STEP_TYPES",
    "STEP_EXECUTE",
    "STEP_REPAIR",
    "STEP_TYPES",
    "step_type_for_attempt",
]
