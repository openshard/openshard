"""Routing V2: a deterministic, trajectory-aware policy over requirement classes.

``TrajectoryPolicyV2`` implements the same ``RoutingPolicy`` interface as the
V1 baseline, so execution does not change. What changes is what it reads and
how it chooses:

1. an explicit model is honoured exactly, or nothing is selected;
2. the run's budget is respected: a repair step with spend at or over the cap,
   or with unknown spend under a cap, selects nothing (``stop``);
3. a repair step needs an *observed* verification failure; an unknown or
   agent-reported outcome is never success and never a reason to escalate,
   so nothing is selected and the reason says so;
4. the step's requirement class comes from the context (caller's class,
   hard capability needs, then the keyword category); a repair step escalates
   along ``requirements.ESCALATION_TARGET`` once per failed attempt, and
   models already tried are excluded;
5. a valid pin (legacy or requirement name) wins inside the class;
6. otherwise ``requirements.rank_for_requirement`` orders the eligible
   candidates: promotion state (dogfood candidate named for the class first,
   when dogfood is enabled), observed history only when
   ``history_evidence`` says the sample is meaningful, requirement fit,
   in-family supersession, price band, legacy hint, price;
7. an empty class falls back to its escalation target (recorded);
8. discovered models that would qualify are reported as ``shadow_candidates``
   and never selected;
9. every decision records the step, the ranking components of the models it
   compared, whether history was used and why not, the recovery plan it
   fixed, and fingerprints, so equal inputs reproduce and a Receipt can
   explain the choice.

It computes no aggregate score and does not learn. A future learned router
replaces ``rank_for_requirement``'s ordering with a policy over the same
``RoutingContext`` and candidate features; the contract enforced by
``decide_route`` (select only from the eligible set) still applies to it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openshard.history.verification import SOURCE_AGENT_REPORTED, SOURCES, STATUS_FAILED
from openshard.models.catalog import LONG_CONTEXT_TOKENS
from openshard.models.promotion import STATE_ELIGIBLE_FOR_SHADOW, promotion_state
from openshard.routing.adaptive.candidates import (
    REASON_LIFECYCLE_PREFIX,
    REASON_NOT_PROMOTED,
    Candidate,
    CandidateSet,
)
from openshard.routing.adaptive.context import RoutingContext
from openshard.routing.adaptive.decision import (
    MODE_EXPLICIT,
    MODE_NONE,
    MODE_PINNED,
    MODE_ROUTED,
    RoutingDecision,
)
from openshard.routing.adaptive.history_evidence import HistoryEvidence
from openshard.routing.adaptive.recovery import RecoveryStep, build_recovery_plan
from openshard.routing.adaptive.step_types import STEP_REPAIR
from openshard.routing.requirements import (
    ESCALATION_TARGET,
    LEGACY_CLASS_TO_REQUIREMENT,
    REQUIREMENT_CLASSES,
    REQUIREMENTS_VERSION,
    ObservedEvidence,
    RankedCandidate,
    hard_requirement_failure,
    rank_for_requirement,
    requirement_for_class_name,
    shadow_candidates,
)

POLICY_V2_NAME = "deterministic_trajectory_v2"
POLICY_V2_VERSION = "1"

MAX_RANKING_RECORDED = 8

# Reason tokens (stable; recorded in Receipts).
R_EXPLICIT = "explicit_model"
R_EXPLICIT_REJECTED = "explicit_model_not_eligible"
R_CALLER_CLASS = "caller_requested_class"
R_REQUIRES_VISION = "requires_vision"
R_REQUIRES_LONG_CONTEXT = "requires_long_context"
R_VISUAL_TASK = "visual_task"
R_SECURITY = "security_sensitive"
R_READ_ONLY_FAST = "read_only_fast"
R_BOILERPLATE = "low_risk_boilerplate"
R_BOILERPLATE_RISKY = "boilerplate_high_risk"
R_COMPLEX = "complex_task"
R_STANDARD = "standard_task"
R_CATEGORY_UNKNOWN = "category_unknown"
R_STEP_EXECUTE = "step_execute"
R_STEP_REPAIR = "step_repair"
R_ESCALATED = "escalated_after_observed_failure"
R_RETRY_SAME_CLASS = "same_class_excluding_tried"
R_FAILURE_NOT_OBSERVED = "failure_not_directly_observed"
R_BUDGET_EXHAUSTED = "cost_budget_exhausted"
R_SPEND_UNKNOWN = "spend_unknown_under_cap"
R_PINNED = "class_pin"
R_PIN_REJECTED = "class_pin_rejected"
R_RANKED = "requirement_ranked"
R_DOGFOOD = "dogfood_candidate_selected"
R_HISTORY_USED = "history_used"
R_HISTORY_NOT_USED = "history_not_used"
R_CLASS_EMPTY = "class_empty_fallback"
R_NO_CANDIDATE = "no_eligible_candidate"

# Documentation of the class rule order used by ``requested_requirement_for``.
V2_CLASS_RULES: tuple[tuple[str, str], ...] = (
    (R_CALLER_CLASS, "context.requested_class (legacy or requirement name)"),
    (R_REQUIRES_VISION, "vision in required_capabilities -> vision"),
    (R_REQUIRES_LONG_CONTEXT, "min_context_tokens >= 500k or long_context required -> long_context"),
    (R_VISUAL_TASK, "task_category visual -> vision"),
    (R_SECURITY, "task_category security -> deep_reasoning"),
    (R_READ_ONLY_FAST, "read_only and latency_preference fast -> fast_control"),
    (R_BOILERPLATE_RISKY, "boilerplate on high risk -> routine_coding"),
    (R_BOILERPLATE, "boilerplate -> routine_coding at high cost sensitivity"),
    (R_COMPLEX, "complex -> routine_coding, escalating to deep_reasoning"),
    (R_STANDARD, "otherwise -> routine_coding"),
)

_OBSERVED_SOURCES = frozenset(SOURCES) - {SOURCE_AGENT_REPORTED}


def requested_requirement_for(context: RoutingContext) -> tuple[str, str | None, str]:
    """(requirement class, cost sensitivity, reason) before any trajectory step."""
    mapped = requirement_for_class_name(context.requested_class)
    if mapped is not None:
        req, sens = mapped
        return req, sens or context.cost_sensitivity, R_CALLER_CLASS
    if "vision" in context.required_capabilities:
        return "vision", context.cost_sensitivity, R_REQUIRES_VISION
    if "long_context" in context.required_capabilities or (
        context.min_context_tokens is not None and context.min_context_tokens >= LONG_CONTEXT_TOKENS
    ):
        return "long_context", context.cost_sensitivity, R_REQUIRES_LONG_CONTEXT
    category = context.task_category
    if category == "visual":
        return "vision", context.cost_sensitivity, R_VISUAL_TASK
    if category == "security":
        return "deep_reasoning", context.cost_sensitivity, R_SECURITY
    if context.read_only and context.latency_preference == "fast":
        return "fast_control", context.cost_sensitivity, R_READ_ONLY_FAST
    if category == "boilerplate":
        if context.risk == "high":
            return "routine_coding", context.cost_sensitivity, R_BOILERPLATE_RISKY
        return "routine_coding", context.cost_sensitivity or "high", R_BOILERPLATE
    if category == "complex":
        return "routine_coding", context.cost_sensitivity, R_COMPLEX
    return "routine_coding", context.cost_sensitivity, (R_STANDARD if category else R_CATEGORY_UNKNOWN)


def escalate_requirement(start: str, steps: int) -> tuple[str, bool]:
    """Walk ``ESCALATION_TARGET`` *steps* times. Returns (class, moved)."""
    current, moved = start, False
    for _ in range(max(0, steps)):
        nxt = ESCALATION_TARGET.get(current)
        if nxt is None:
            break
        current, moved = nxt, True
    return current, moved


def failure_observed(context: RoutingContext) -> bool:
    return (
        context.last_verification_status == STATUS_FAILED
        and context.last_verification_source in _OBSERVED_SOURCES
    )


def budget_stop_reason(context: RoutingContext) -> str | None:
    """Why the budget forbids another model call, or None."""
    cap = context.cost_budget_usd
    if cap is None:
        return None
    spent = context.accumulated_cost_usd
    if context.step_type == STEP_REPAIR and spent is None:
        return R_SPEND_UNKNOWN  # unknown spend cannot be shown to be under the cap
    if spent is not None and spent >= cap:
        return R_BUDGET_EXHAUSTED
    return None


class TrajectoryPolicyV2:
    name = POLICY_V2_NAME
    version = POLICY_V2_VERSION

    def __init__(
        self,
        *,
        dogfood: Mapping[str, frozenset[str]] | None = None,
        history: HistoryEvidence | None = None,
    ) -> None:
        self.dogfood = {k: frozenset(v) for k, v in (dogfood or {}).items()}
        self.history = history

    # -- helpers ---------------------------------------------------------------

    def _decision(self, *, context: RoutingContext, candidates: CandidateSet, **fields: Any) -> RoutingDecision:
        return RoutingDecision(
            policy_name=self.name,
            policy_version=self.version,
            eligible_count=len(candidates.eligible),
            rejected_counts=candidates.rejection_counts(),
            context=context,
            catalog_fingerprint=candidates.catalog_fingerprint,
            candidate_set_version=f"{candidates.version}+{REQUIREMENTS_VERSION}",
            step_type=context.step_type,
            **fields,
        )

    def _states(self, candidates: CandidateSet, requirement: str) -> dict[str, str]:
        return {c.model_id: c.promotion_state for c in candidates.eligible}

    def _dogfood_for(self, requirement: str, candidates: CandidateSet) -> frozenset[str]:
        named = self.dogfood.get(requirement, frozenset())
        return frozenset(m for m in named if candidates.get(m) is not None)

    def _rank(
        self,
        requirement: str,
        context: RoutingContext,
        candidates: CandidateSet,
        cost_sensitivity: str | None,
        exclude: frozenset[str],
    ) -> tuple[list[RankedCandidate], list[tuple[str, str]], dict[str, Any]]:
        cls = REQUIREMENT_CLASSES[requirement]
        entries = [c.entry for c in candidates.eligible]
        states = self._states(candidates, requirement)
        for_class = self._dogfood_for(requirement, candidates)

        def _rank(history: Mapping[str, ObservedEvidence] | None):
            return rank_for_requirement(
                cls, entries, states=states, dogfood_enabled=context.dogfood_enabled,
                dogfood_for_class=for_class, history=history, cost_sensitivity=cost_sensitivity,
                min_context_tokens=context.min_context_tokens, exclude=exclude,
            )

        ranked, rejected = _rank(None)
        record: dict[str, Any] = {"used": False, "reason": "no_history_source"}
        if self.history is not None:
            evidence, record = self.history.gate([r.model_id for r in ranked])
            if evidence:
                ranked, rejected = _rank(evidence)
        return ranked, rejected, record

    def _pin_for(self, requirement: str, pins: Mapping[str, str]) -> str | None:
        if pins.get(requirement):
            return pins[requirement]
        for legacy, (req, _) in LEGACY_CLASS_TO_REQUIREMENT.items():
            if req == requirement and pins.get(legacy):
                return pins[legacy]
        return None

    def _select(
        self,
        requirement: str,
        context: RoutingContext,
        candidates: CandidateSet,
        pins: Mapping[str, str],
        cost_sensitivity: str | None,
        exclude: frozenset[str],
    ) -> tuple[Candidate | None, str, list[RankedCandidate], list[tuple[str, str]], dict, tuple[str, str] | None]:
        """(candidate, mode, ranked, rejected, history record, rejected pin) for one class."""
        cls = REQUIREMENT_CLASSES[requirement]
        rejected_pin: tuple[str, str] | None = None
        pin = self._pin_for(requirement, pins)
        if pin:
            cand = candidates.get(pin)
            reason: str | None
            if cand is None:
                reason = "not_eligible:" + (candidates.rejection_reason(pin) or "unknown_model")
            elif cand.model_id in exclude:
                reason = "already_tried"
            else:
                reason = hard_requirement_failure(cls, cand.entry, min_context_tokens=context.min_context_tokens)
            if reason is None and cand is not None:
                return cand, MODE_PINNED, [], [], {"used": False, "reason": "pinned"}, None
            rejected_pin = (pin, reason or "unknown")
        ranked, rejected, record = self._rank(requirement, context, candidates, cost_sensitivity, exclude)
        if not ranked:
            return None, MODE_NONE, ranked, rejected, record, rejected_pin
        return candidates.get(ranked[0].model_id), MODE_ROUTED, ranked, rejected, record, rejected_pin

    def _shadow(self, requirement: str, context: RoutingContext, candidates: CandidateSet) -> tuple[str, ...]:
        """Discovered models that passed availability and policy but not promotion."""
        stale = bool(candidates.catalog.snapshot.stale)
        pool = []
        states: dict[str, str] = {}
        for mid, reason in candidates.rejected:
            if reason != REASON_NOT_PROMOTED and not reason.startswith(REASON_LIFECYCLE_PREFIX):
                continue
            entry = candidates.catalog.get(mid)
            if entry is None:
                continue
            state = promotion_state(entry, snapshot_stale=stale)
            if state == STATE_ELIGIBLE_FOR_SHADOW:
                pool.append(entry)
                states[mid] = state
        return shadow_candidates(
            REQUIREMENT_CLASSES[requirement], pool, states=states,
            min_context_tokens=context.min_context_tokens,
        )

    def _recovery(
        self,
        resolved: str,
        context: RoutingContext,
        candidates: CandidateSet,
        pins: Mapping[str, str],
        cost_sensitivity: str | None,
        selected: Candidate,
    ):
        """Fix the ladder above the selection: each escalation target resolved
        now, then one different model in the last class when nothing is above."""
        steps: list[RecoveryStep] = []
        used = set(context.models_tried) | {selected.model_id}
        current = resolved
        while True:
            target = ESCALATION_TARGET.get(current)
            cand, _, _, _, _, _ = self._select(
                target or current, context, candidates, pins, cost_sensitivity, frozenset(used),
            )
            steps.append(RecoveryStep(target or current, cand.model_id if cand else None))
            if cand is not None:
                used.add(cand.model_id)
            if target is None:
                break
            current = target
        return build_recovery_plan(
            steps,
            verification_available=context.verification_available,
            verification_requested=context.verification_requested,
        )

    # -- interface --------------------------------------------------------------

    def decide(
        self,
        context: RoutingContext,
        candidates: CandidateSet,
        *,
        class_pins: Mapping[str, str] | None = None,
    ) -> RoutingDecision:
        pins = dict(class_pins or {})
        step_reason = R_STEP_REPAIR if context.step_type == STEP_REPAIR else R_STEP_EXECUTE

        if context.explicit_model:
            cand = candidates.get(context.explicit_model)
            plan = build_recovery_plan(
                (), verification_available=context.verification_available,
                verification_requested=context.verification_requested, explicit_model=True,
            )
            if cand is not None:
                return self._decision(
                    selected_model=cand.model_id, selection_mode=MODE_EXPLICIT,
                    requested_class=None, resolved_class=None,
                    reasons=(step_reason, R_EXPLICIT), considered=(cand.model_id,),
                    selected_via=cand.via, selected_promotion_state=cand.promotion_state,
                    recovery=plan, context=context, candidates=candidates,
                )
            return self._decision(
                selected_model=None, selection_mode=MODE_NONE, requested_class=None, resolved_class=None,
                reasons=(step_reason, R_EXPLICIT_REJECTED),
                rejected_explicit_reason=candidates.rejection_reason(context.explicit_model) or "unknown_model",
                recovery=plan, context=context, candidates=candidates,
            )

        base, cost_sensitivity, class_reason = requested_requirement_for(context)
        reasons: list[str] = [step_reason, class_reason]
        empty_plan = build_recovery_plan(
            (), verification_available=context.verification_available,
            verification_requested=context.verification_requested,
        )

        stop = budget_stop_reason(context)
        if stop is not None:
            return self._decision(
                selected_model=None, selection_mode=MODE_NONE, requested_class=base, resolved_class=None,
                reasons=(*reasons, stop), recovery=empty_plan, context=context, candidates=candidates,
            )

        requested = base
        if context.step_type == STEP_REPAIR:
            if not failure_observed(context):
                # No observed failure: not a success either, but nothing to act on.
                return self._decision(
                    selected_model=None, selection_mode=MODE_NONE, requested_class=base, resolved_class=None,
                    reasons=(*reasons, R_FAILURE_NOT_OBSERVED), recovery=empty_plan,
                    context=context, candidates=candidates,
                )
            failed_steps = (context.attempt - 1) if context.attempt else 1
            requested, moved = escalate_requirement(base, failed_steps)
            reasons.append(R_ESCALATED if moved else R_RETRY_SAME_CLASS)

        exclude = frozenset(context.models_tried)
        fallbacks: list[str] = []
        rejected_pin: tuple[str, str] | None = None
        current: str | None = requested
        while current is not None:
            cand, mode, ranked, rejected, history_record, pin_rej = self._select(
                current, context, candidates, pins, cost_sensitivity, exclude,
            )
            rejected_pin = rejected_pin or pin_rej
            if cand is None:
                fallbacks.append(current)
                nxt = ESCALATION_TARGET.get(current)
                current = nxt if nxt not in fallbacks else None
                continue
            out = list(reasons)
            if fallbacks:
                out.append(R_CLASS_EMPTY)
            if rejected_pin is not None:
                out.append(R_PIN_REJECTED)
            out.append(R_PINNED if mode == MODE_PINNED else R_RANKED)
            if mode == MODE_ROUTED:
                out.append(R_HISTORY_USED if history_record.get("used") else R_HISTORY_NOT_USED)
                if cand.dogfood and ranked and "dogfood_candidate_for_class" in ranked[0].notes:
                    out.append(R_DOGFOOD)
            return self._decision(
                selected_model=cand.model_id,
                selection_mode=mode,
                requested_class=requested,
                resolved_class=current,
                reasons=tuple(out),
                considered=tuple(r.model_id for r in ranked) if ranked else (cand.model_id,),
                selected_via=cand.via,
                selected_promotion_state=cand.promotion_state,
                class_fallbacks=tuple(fallbacks),
                rejected_pin=rejected_pin[0] if rejected_pin else None,
                rejected_pin_reason=rejected_pin[1] if rejected_pin else None,
                ranking=tuple(r.to_dict() for r in ranked[:MAX_RANKING_RECORDED]),
                history_evidence=dict(history_record),
                shadow_candidates=self._shadow(current, context, candidates),
                recovery=self._recovery(current, context, candidates, pins, cost_sensitivity, cand),
                context=context,
                candidates=candidates,
            )

        return self._decision(
            selected_model=None, selection_mode=MODE_NONE, requested_class=requested, resolved_class=None,
            reasons=(*reasons, R_NO_CANDIDATE), class_fallbacks=tuple(fallbacks),
            rejected_pin=rejected_pin[0] if rejected_pin else None,
            rejected_pin_reason=rejected_pin[1] if rejected_pin else None,
            shadow_candidates=self._shadow(requested, context, candidates),
            recovery=empty_plan, context=context, candidates=candidates,
        )


TRAJECTORY_POLICY = TrajectoryPolicyV2()


__all__ = [
    "MAX_RANKING_RECORDED",
    "POLICY_V2_NAME",
    "POLICY_V2_VERSION",
    "R_BUDGET_EXHAUSTED",
    "R_CLASS_EMPTY",
    "R_DOGFOOD",
    "R_ESCALATED",
    "R_EXPLICIT",
    "R_EXPLICIT_REJECTED",
    "R_FAILURE_NOT_OBSERVED",
    "R_HISTORY_NOT_USED",
    "R_HISTORY_USED",
    "R_NO_CANDIDATE",
    "R_PINNED",
    "R_PIN_REJECTED",
    "R_RANKED",
    "R_RETRY_SAME_CLASS",
    "R_SPEND_UNKNOWN",
    "R_STEP_EXECUTE",
    "R_STEP_REPAIR",
    "TRAJECTORY_POLICY",
    "V2_CLASS_RULES",
    "TrajectoryPolicyV2",
    "budget_stop_reason",
    "escalate_requirement",
    "failure_observed",
    "requested_requirement_for",
]
