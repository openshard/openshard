"""Supervisor routing for ``openshard osn run`` (V1, capability-gated, shadow-first).

The OSN loop climbs a fixed escalation ladder after each observed
verification failure. A supervisor re-evaluates at that boundary instead:
given what the attempts so far produced (which model ran, whether OpenShard
observed the verification fail, what it cost) it says *continue*, *escalate*
to a specific model, or *stop*. The decision function is the recovery
policy that already exists, ``routing.adaptive.recovery.next_recovery_action``:
pure, bounded, and refusing to act on evidence it does not have (an
unobserved failure, an unknown cost under a spend cap).

Boundaries: only after an attempt whose verification OpenShard ran and saw
fail, and only when the loop itself would retry. Trivial steps never
consult the supervisor.

Record modes:

* ``shadow`` records what the supervisor would have done; the ladder runs
  exactly as before (``acted_on`` is False and says why).
* ``applied`` (capability ``supervisor_routing`` on, adaptive routing
  applied, no user-typed ladder) lets the supervisor choose the next model
  or stop the run.

Historical performance is not consulted. Every input is something this run
observed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openshard.routing.adaptive.recovery import (
    ACTION_ESCALATE,
    ACTION_STOP,
    AttemptResult,
    RecoveryPlan,
    next_recovery_action,
)

CAPABILITY = "supervisor_routing"
RECORD_SHADOW = "shadow"
RECORD_APPLIED = "applied"

ACTION_CONTINUE = "continue"
ACTION_SWITCH = "escalate"
ACTION_HALT = "stop"

SOURCE_OBSERVED = "directly_observed"
NOT_ACTED_SHADOW = "shadow_mode"
NOT_ACTED_USER_LADDER = "user_ladder"
NOT_ACTED_ROUTING_NOT_APPLIED = "adaptive_routing_not_applied"
NOT_ACTED_PROVIDER = "provider_cannot_switch"

# The loop's stop reason when an applied supervisor halts the run.
STOP_REASON_PREFIX = "supervisor_stop:"

UsageLookup = Callable[[int], tuple[str | None, float | None]]


@dataclass
class SupervisorDecision:
    attempt: int
    action: str  # continue | escalate | stop
    reason: str
    recommended_model: str | None
    acted_on: bool
    not_acted_reason: str | None
    evidence: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "action": self.action,
            "reason": self.reason,
            "recommended_model": self.recommended_model,
            "acted_on": self.acted_on,
            "not_acted_reason": self.not_acted_reason,
            "evidence": dict(self.evidence),
        }


@dataclass
class RecoverySupervisor:
    """Drives ``next_recovery_action`` from what the loop observed."""

    plan: RecoveryPlan
    usage_for: UsageLookup  # attempt number -> (model that ran it, its estimated cost)
    record_mode: str = RECORD_SHADOW
    not_acted_reason: str | None = NOT_ACTED_SHADOW  # why an applied action would not be taken
    cost_budget_usd: float | None = None
    first_model: str | None = None
    first_class: str | None = None
    results: list[AttemptResult] = field(default_factory=list)
    decisions: list[SupervisorDecision] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.record_mode == RECORD_APPLIED

    def _class_for(self, model: str | None) -> str | None:
        if model is None:
            return None
        if model == self.first_model:
            return self.first_class
        for step in self.plan.steps:
            if step.model_id == model:
                return step.routing_class
        return None

    def after_failed_attempt(self, n: int, *, verification_observed: bool) -> SupervisorDecision:
        """Called by the loop after attempt *n* failed verification and it would retry."""
        model, cost = self.usage_for(n)
        self.results.append(AttemptResult(
            model_id=model or "unknown",
            routing_class=self._class_for(model),
            verification_status="failed",
            verification_source=SOURCE_OBSERVED if verification_observed else None,
            cost_usd=cost,
        ))
        action = next_recovery_action(self.plan, self.results, cost_budget_usd=self.cost_budget_usd)
        if action.action == ACTION_ESCALATE and action.step is not None:
            kind, model_id = ACTION_SWITCH, action.step.model_id
        elif action.action == ACTION_STOP:
            kind, model_id = ACTION_HALT, None
        else:
            kind, model_id = ACTION_CONTINUE, None
        known = [r.cost_usd for r in self.results]
        decision = SupervisorDecision(
            attempt=n,
            action=kind,
            reason=action.reason,
            recommended_model=model_id,
            acted_on=self.applied,
            not_acted_reason=None if self.applied else self.not_acted_reason,
            evidence={
                "verification_status": "failed",
                "verification_source": SOURCE_OBSERVED if verification_observed else None,
                "attempts_so_far": len(self.results),
                "models_tried": [r.model_id for r in self.results],
                "spend_usd": round(sum(c for c in known if c is not None), 6) if all(c is not None for c in known) else None,
                "spend_known": all(c is not None for c in known),
                "cost_budget_usd": self.cost_budget_usd,
                "plan_enabled": bool(self.plan.enabled),
                "plan_max_attempts": int(self.plan.max_attempts),
            },
        )
        self.decisions.append(decision)
        return decision

    def mark_not_acted(self, reason: str) -> None:
        """The loop could not carry out the last decision (e.g. the provider cannot switch)."""
        if self.decisions:
            last = self.decisions[-1]
            last.acted_on = False
            last.not_acted_reason = reason

    def to_record(self) -> dict[str, Any]:
        return {
            "capability": CAPABILITY,
            "record_mode": self.record_mode,
            "not_applied_reason": None if self.applied else self.not_acted_reason,
            "boundary": "observed_verification_failure_before_retry",
            "decisions": [d.to_record() for d in self.decisions],
            "evidence": {"verification": "openshard_observed", "spend": "provider_usage_estimate",
                         "history": "not_used"},
        }


__all__ = [
    "ACTION_CONTINUE",
    "ACTION_HALT",
    "ACTION_SWITCH",
    "CAPABILITY",
    "NOT_ACTED_PROVIDER",
    "NOT_ACTED_ROUTING_NOT_APPLIED",
    "NOT_ACTED_SHADOW",
    "NOT_ACTED_USER_LADDER",
    "RECORD_APPLIED",
    "RECORD_SHADOW",
    "STOP_REASON_PREFIX",
    "RecoverySupervisor",
    "SupervisorDecision",
]
