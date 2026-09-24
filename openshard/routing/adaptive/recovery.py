"""Bounded, policy-driven recovery: cheap attempt -> verification -> stop or escalate.

A :class:`RecoveryPlan` is fixed when the routing decision is made: an ordered
escalation ladder of (routing class, model) steps above the first attempt, a
hard attempt cap, and whether escalation is allowed at all. Given the attempts
so far, :func:`next_recovery_action` returns exactly one action - ``stop`` or
``escalate`` to the next step. It is pure; the caller executes.

Why it cannot loop:

* at most ``max_attempts`` attempts, itself capped by ``MAX_ATTEMPTS_CEILING``;
* every step is a different class, higher on the ladder, used at most once;
* a model already tried is never tried again;
* escalation needs *observed* failure. An unknown or unobserved verification
  outcome stops recovery - missing evidence is not a reason to spend more. An
  agent-reported failure is a claim OpenShard did not observe, so it stops too.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from openshard.history.verification import (
    SOURCE_AGENT_REPORTED,
    SOURCES,
    STATUS_FAILED,
    STATUS_PASSED,
)

# The coding escalation ladder, cheapest first. Classes outside it (fast,
# vision) escalate per ``ESCALATION_TARGETS``.
ESCALATION_LADDER: tuple[str, ...] = ("cheap_coding", "balanced_coding", "frontier_reasoning")
ESCALATION_TARGETS: dict[str, tuple[str, ...]] = {
    "cheap_coding": ("balanced_coding", "frontier_reasoning"),
    "balanced_coding": ("frontier_reasoning",),
    "frontier_reasoning": (),
    "fast": ("balanced_coding",),
    # A vision task needs a vision model; the ladder has none above it.
    "vision": (),
}

DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_CEILING = 4

TRIGGER_VERIFICATION_FAILED = "verification_failed"

ACTION_STOP = "stop"
ACTION_ESCALATE = "escalate"

# Stop reasons (stable tokens).
STOP_VERIFIED_SUCCESS = "verified_success"
STOP_SUCCESS_NOT_OBSERVED = "success_agent_reported"
STOP_OUTCOME_UNKNOWN = "outcome_not_observed"
STOP_FAILURE_NOT_OBSERVED = "failure_not_directly_observed"
STOP_ATTEMPTS_EXHAUSTED = "attempt_budget_exhausted"
STOP_COST_EXHAUSTED = "cost_budget_exhausted"
STOP_LADDER_EXHAUSTED = "escalation_ladder_exhausted"
STOP_NO_ATTEMPT = "no_attempt_recorded"

# Disabled reasons.
DISABLED_NO_VERIFICATION = "verification_unavailable"
DISABLED_NOT_REQUESTED = "verification_not_requested"
DISABLED_EXPLICIT_MODEL = "explicit_model"
DISABLED_NO_TARGETS = "no_escalation_target"

# Sources strong enough to act on: anything OpenShard or an independent system saw.
_ACTIONABLE_SOURCES = frozenset(SOURCES) - {SOURCE_AGENT_REPORTED}


@dataclass(frozen=True)
class RecoveryStep:
    routing_class: str
    model_id: str | None
    trigger: str = TRIGGER_VERIFICATION_FAILED

    def to_dict(self) -> dict:
        return {"class": self.routing_class, "model": self.model_id, "trigger": self.trigger}


@dataclass(frozen=True)
class RecoveryPlan:
    steps: tuple[RecoveryStep, ...] = ()
    max_attempts: int = 1
    enabled: bool = False
    disabled_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "max_attempts": self.max_attempts,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass(frozen=True)
class AttemptResult:
    """What one executed attempt produced. ``None`` = not observed."""

    model_id: str
    routing_class: str | None = None
    verification_status: str | None = None
    verification_source: str | None = None
    cost_usd: float | None = None
    latency_seconds: float | None = None


@dataclass(frozen=True)
class RecoveryAction:
    action: str
    reason: str
    step: RecoveryStep | None = None


def build_recovery_plan(
    steps: Sequence[RecoveryStep],
    *,
    verification_available: bool | None,
    verification_requested: bool | None,
    explicit_model: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RecoveryPlan:
    """Fix the plan at decision time. Escalation needs checks that will run."""
    max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS_CEILING))
    usable = tuple(s for s in steps if s.model_id)[: max_attempts - 1]
    disabled: str | None = None
    if explicit_model:
        disabled = DISABLED_EXPLICIT_MODEL
    elif verification_available is not True:
        disabled = DISABLED_NO_VERIFICATION
    elif verification_requested is False:
        disabled = DISABLED_NOT_REQUESTED
    elif not usable:
        disabled = DISABLED_NO_TARGETS
    if disabled is not None:
        return RecoveryPlan(steps=usable, max_attempts=1, enabled=False, disabled_reason=disabled)
    return RecoveryPlan(steps=usable, max_attempts=1 + len(usable), enabled=True)


def next_recovery_action(
    plan: RecoveryPlan,
    attempts: Sequence[AttemptResult],
    *,
    cost_budget_usd: float | None = None,
) -> RecoveryAction:
    """The single next action after *attempts* (oldest first). Pure; never raises."""
    if not attempts:
        return RecoveryAction(ACTION_STOP, STOP_NO_ATTEMPT)
    last = attempts[-1]
    status, source = last.verification_status, last.verification_source
    if status == STATUS_PASSED:
        reason = STOP_VERIFIED_SUCCESS if source in _ACTIONABLE_SOURCES else STOP_SUCCESS_NOT_OBSERVED
        return RecoveryAction(ACTION_STOP, reason)
    if status != STATUS_FAILED:
        return RecoveryAction(ACTION_STOP, STOP_OUTCOME_UNKNOWN)
    if source not in _ACTIONABLE_SOURCES:
        return RecoveryAction(ACTION_STOP, STOP_FAILURE_NOT_OBSERVED)
    if not plan.enabled:
        return RecoveryAction(ACTION_STOP, plan.disabled_reason or DISABLED_NO_TARGETS)
    if len(attempts) >= plan.max_attempts:
        return RecoveryAction(ACTION_STOP, STOP_ATTEMPTS_EXHAUSTED)
    if cost_budget_usd is not None:
        costs = [a.cost_usd for a in attempts]
        # Unknown spend cannot be shown to be under budget.
        if any(c is None for c in costs) or sum(c for c in costs if c is not None) >= cost_budget_usd:
            return RecoveryAction(ACTION_STOP, STOP_COST_EXHAUSTED)
    tried = {a.model_id for a in attempts}
    tried_classes = {a.routing_class for a in attempts if a.routing_class}
    for step in plan.steps:
        if step.model_id in tried or step.routing_class in tried_classes:
            continue
        return RecoveryAction(ACTION_ESCALATE, TRIGGER_VERIFICATION_FAILED, step)
    return RecoveryAction(ACTION_STOP, STOP_LADDER_EXHAUSTED)
