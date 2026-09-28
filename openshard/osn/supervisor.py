"""Supervisor routing for ``openshard osn run`` (V1, capability-gated, shadow-first).

The OSN loop climbs a fixed escalation ladder after each observed
verification failure. A supervisor re-evaluates at that boundary instead:
given what the attempts so far produced (which model ran, whether OpenShard
observed the verification fail, what it cost) it says *escalate* to a
specific model or *stop*. The decision function is the recovery policy that
already exists, ``routing.adaptive.recovery.next_recovery_action``: pure,
bounded, and refusing to act on evidence it does not have (an unobserved
failure, an unknown cost under a spend cap).

Boundaries: only after an attempt whose verification OpenShard ran and saw
fail, only when the loop itself would retry, and only when a budget would
not already refuse the next attempt (the budget's own stop is never
pre-empted). Trivial steps never consult the supervisor.

Record modes:

* ``shadow`` records what the supervisor would have done; the ladder runs
  exactly as before (``acted_on`` is False and says why).
* ``applied`` (capability ``supervisor_routing`` on, adaptive routing
  applied, no user-typed ladder) lets the supervisor choose the next model
  or stop the run. An escalation is recorded as acted on only once the next
  attempt actually called that model; if the run ends first, the record says
  so.

With Routing V2 applied, the supervisor also carries a ``reroute`` hook: once
the recovery policy has said *escalate* (so the attempt cap, the spend cap
and the observed-failure rule all held), the ``repair`` step is re-decided
over the run's own candidate pool from what the run observed (models tried,
the observed failure, spend so far). The re-route may pick a different model
than the fixed plan or say nothing is eligible, which stops the run; it can
never widen what the plan allowed, because the envelope ran first. The
decision's reason, policy and whether it changed the model are recorded.

Historical performance reaches routing only through the V2 policy's
evidence gate. Every other input is something this run observed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openshard.routing.adaptive.recovery import (
    ACTION_ESCALATE,
    STOP_ATTEMPTS_EXHAUSTED,
    AttemptResult,
    RecoveryPlan,
    next_recovery_action,
)

CAPABILITY = "supervisor_routing"
RECORD_SHADOW = "shadow"
RECORD_APPLIED = "applied"

ACTION_SWITCH = "escalate"
ACTION_HALT = "stop"

SOURCE_OBSERVED = "directly_observed"
REASON_TRAJECTORY_REROUTE = "trajectory_reroute"
MAX_REROUTE_CONSIDERED = 4
NOT_ACTED_SHADOW = "shadow_mode"
NOT_ACTED_USER_LADDER = "user_ladder"
NOT_ACTED_ROUTING_NOT_APPLIED = "adaptive_routing_not_applied"
NOT_ACTED_PROVIDER = "provider_cannot_switch"
NOT_ACTED_RUN_ENDED = "run_ended_before_retry"
NOT_ACTED_USAGE_UNKNOWN = "attempt_usage_unknown"

# The recovery policy's attempt cap is the *plan's* cap (distinct from the
# loop's --max-attempts and from an Agent Budgets max_attempts); name it so.
REASON_PLAN_ATTEMPTS = "plan_attempts_exhausted"
REASON_USAGE_UNKNOWN = "attempt_usage_unknown"

# The loop's stop reason when an applied supervisor halts the run.
STOP_REASON_PREFIX = "supervisor_stop:"

UsageLookup = Callable[[int], tuple[str | None, float | None]]
LadderLookup = Callable[[int], str | None]
# (attempt, models tried, last status, last source, accumulated cost) -> decision | None
RerouteFn = Callable[..., Any]


@dataclass
class SupervisorDecision:
    attempt: int
    action: str  # escalate | stop
    reason: str
    recommended_model: str | None
    acted_on: bool | None  # None: an applied escalation not yet confirmed by the next attempt
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
    usage_for: UsageLookup  # attempt number -> (requested model that ran it, its estimated cost)
    record_mode: str = RECORD_SHADOW
    not_acted_reason: str | None = NOT_ACTED_SHADOW  # why a shadow decision is not acted on
    cost_budget_usd: float | None = None
    first_model: str | None = None
    first_class: str | None = None
    ladder_model_for: LadderLookup | None = None  # what the ladder would run at attempt n
    reroute: RerouteFn | None = None  # Routing V2 repair-step re-route (applied path only)
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

    def after_failed_attempt(
        self, n: int, *, verification_observed: bool, loop_max_attempts: int | None = None,
    ) -> SupervisorDecision:
        """Called by the loop after attempt *n* failed verification and it would retry."""
        model, cost = self.usage_for(n)
        ladder_next = None
        if self.ladder_model_for is not None:
            try:
                ladder_next = self.ladder_model_for(n + 1)
            except Exception:
                ladder_next = None
        known = [r.cost_usd for r in self.results] + [cost]
        evidence: dict[str, Any] = {
            "verification_status": "failed",
            "verification_source": SOURCE_OBSERVED if verification_observed else None,
            "attempts_so_far": len(self.results) + 1,
            "models_tried": [r.model_id for r in self.results] + [model or "unknown"],
            "spend_usd": round(sum(c for c in known if c is not None), 6) if all(c is not None for c in known) else None,
            "spend_known": all(c is not None for c in known),
            "cost_budget_usd": self.cost_budget_usd,
            "plan_enabled": bool(self.plan.enabled),
            "plan_max_attempts": int(self.plan.max_attempts),
            "loop_max_attempts": loop_max_attempts,
            "ladder_model": ladder_next,
        }
        if model is None:
            # Nothing recorded which model ran this attempt: the policy would
            # reason about an "unknown" model. Say so instead of guessing.
            decision = SupervisorDecision(
                attempt=n, action=ACTION_HALT, reason=REASON_USAGE_UNKNOWN, recommended_model=None,
                acted_on=False, not_acted_reason=NOT_ACTED_USAGE_UNKNOWN, evidence=evidence,
            )
            self.decisions.append(decision)
            return decision
        self.results.append(AttemptResult(
            model_id=model,
            routing_class=self._class_for(model),
            verification_status="failed",
            verification_source=SOURCE_OBSERVED if verification_observed else None,
            cost_usd=cost,
        ))
        action = next_recovery_action(self.plan, self.results, cost_budget_usd=self.cost_budget_usd)
        if action.action == ACTION_ESCALATE and action.step is not None:
            kind, model_id = ACTION_SWITCH, action.step.model_id
        else:
            kind, model_id = ACTION_HALT, None
        reason = REASON_PLAN_ATTEMPTS if action.reason == STOP_ATTEMPTS_EXHAUSTED else action.reason
        if kind == ACTION_SWITCH and self.reroute is not None:
            # The envelope allowed an escalation; let the step be re-decided on
            # what this run observed. It may narrow (stop), never widen.
            costs = [r.cost_usd for r in self.results]
            rerouted = self._reroute(n, costs)
            if rerouted is not None:
                evidence["reroute"] = {
                    "policy": {"name": rerouted.policy_name, "version": rerouted.policy_version},
                    "step_type": rerouted.step_type,
                    "requested_class": rerouted.requested_class,
                    "resolved_class": rerouted.resolved_class,
                    "selected_model": rerouted.selected_model,
                    "promotion_state": rerouted.selected_promotion_state,
                    "reasons": list(rerouted.reasons)[:8],
                    "considered": list(rerouted.considered[:MAX_REROUTE_CONSIDERED]),
                    "history_used": bool((rerouted.history_evidence or {}).get("used")),
                    "changed_from_plan": (rerouted.selected_model or None) != model_id,
                }
                if rerouted.selected_model and rerouted.selected_model not in {r.model_id for r in self.results}:
                    model_id = rerouted.selected_model
                    reason = REASON_TRAJECTORY_REROUTE
                else:
                    kind, model_id = ACTION_HALT, None
                    reason = rerouted.reasons[-1] if rerouted.reasons else "no_eligible_candidate"
        evidence["changed_next_model"] = (
            (model_id != ladder_next) if kind == ACTION_SWITCH and ladder_next is not None else None
        )
        if not self.applied:
            acted_on: bool | None = False
        elif kind == ACTION_HALT:
            acted_on = True  # the loop returns immediately on a stop
        else:
            acted_on = None  # confirmed when the next attempt actually calls the model
        decision = SupervisorDecision(
            attempt=n,
            action=kind,
            reason=reason,
            recommended_model=model_id,
            acted_on=acted_on,
            not_acted_reason=None if self.applied else self.not_acted_reason,
            evidence=evidence,
        )
        self.decisions.append(decision)
        return decision

    def _reroute(self, n: int, costs: list[float | None]):
        if self.reroute is None:
            return None
        try:
            return self.reroute(
                attempt=n + 1,
                models_tried=[r.model_id for r in self.results],
                last_verification_status="failed",
                last_verification_source=self.results[-1].verification_source if self.results else None,
                accumulated_cost_usd=(
                    round(sum(c for c in costs if c is not None), 6) if all(c is not None for c in costs) else None
                ),
            )
        except Exception:
            return None

    def mark_acted(self) -> None:
        """The next attempt ran with the recommended model."""
        if self.decisions and self.decisions[-1].acted_on is None:
            self.decisions[-1].acted_on = True

    def mark_not_acted(self, reason: str) -> None:
        """The loop could not carry out the last decision (provider cannot switch, run ended first)."""
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
                         "history": "via_routing_v2_evidence_gate" if self.reroute is not None else "not_used"},
        }


__all__ = [
    "ACTION_HALT",
    "ACTION_SWITCH",
    "CAPABILITY",
    "REASON_TRAJECTORY_REROUTE",
    "NOT_ACTED_PROVIDER",
    "NOT_ACTED_ROUTING_NOT_APPLIED",
    "NOT_ACTED_RUN_ENDED",
    "NOT_ACTED_SHADOW",
    "NOT_ACTED_USAGE_UNKNOWN",
    "NOT_ACTED_USER_LADDER",
    "REASON_PLAN_ATTEMPTS",
    "REASON_USAGE_UNKNOWN",
    "RECORD_APPLIED",
    "RECORD_SHADOW",
    "STOP_REASON_PREFIX",
    "RecoverySupervisor",
    "SupervisorDecision",
]
