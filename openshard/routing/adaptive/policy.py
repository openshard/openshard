"""RoutingPolicy interface and the deterministic baseline policy.

A policy maps (RoutingContext, CandidateSet) to a RoutingDecision. It never
widens the candidate set - eligibility is decided before any policy runs - so
every policy, including future learned or agent-as-a-router ones, is bounded by
the same catalog, lifecycle and user-policy rules. Adding a policy means
implementing :class:`RoutingPolicy`; execution does not change.

The deterministic baseline builds on #347's routing classes instead of naming
models:

1. an explicit model is honoured exactly, or the decision selects nothing -
   routing never substitutes a different model for an explicit choice;
2. otherwise a routing class is requested from context facts, first matching
   rule wins (``BASELINE_CLASS_RULES`` documents the order);
3. within the class, a valid config pin wins, else #347's
   ``filter_for_class`` ranking over the *eligible* candidates picks the model;
4. an empty class falls back along ``CLASS_FALLBACKS`` (recorded, never silent);
5. the recovery ladder above the resolved class is resolved the same way and
   fixed into the decision (see ``recovery``).

It computes no scores and no confidence: ranking is ordinal and the decision
says so by leaving those fields ``None``. It does not learn.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from openshard.routing.adaptive.candidates import Candidate, CandidateSet
from openshard.routing.adaptive.context import RoutingContext
from openshard.routing.adaptive.decision import (
    MODE_EXPLICIT,
    MODE_NONE,
    MODE_PINNED,
    MODE_ROUTED,
    RoutingDecision,
)
from openshard.routing.adaptive.recovery import (
    ESCALATION_TARGETS,
    RecoveryStep,
    build_recovery_plan,
)
from openshard.routing.routing_classes import (
    ROUTING_CLASSES,
    filter_for_class,
    pin_rejection_reason,
    promotion_candidates,
)

BASELINE_POLICY_NAME = "deterministic_baseline"
BASELINE_POLICY_VERSION = "1"

# Reason tokens (stable; recorded in Receipts).
R_CALLER_CLASS = "caller_requested_class"
R_REQUIRES_VISION = "requires_vision"
R_VISUAL_TASK = "visual_task"
R_SECURITY = "security_sensitive"
R_READ_ONLY_FAST = "read_only_fast"
R_BOILERPLATE = "low_risk_boilerplate"
R_BOILERPLATE_RISKY = "boilerplate_high_risk"
R_COMPLEX = "complex_task"
R_STANDARD = "standard_task"
R_CATEGORY_UNKNOWN = "category_unknown"
R_EXPLICIT = "explicit_model"
R_EXPLICIT_REJECTED = "explicit_model_not_eligible"
R_PINNED = "class_pin"
R_PIN_REJECTED = "class_pin_rejected"
R_CLASS_RANKED = "class_ranked"
R_CLASS_EMPTY = "class_empty_fallback"
R_NO_CANDIDATE = "no_eligible_candidate"

# Documentation of the rule order used by ``requested_class_for``.
BASELINE_CLASS_RULES: tuple[tuple[str, str], ...] = (
    (R_CALLER_CLASS, "context.requested_class, when it names a known class"),
    (R_REQUIRES_VISION, "vision in required_capabilities -> vision"),
    (R_VISUAL_TASK, "task_category visual -> vision"),
    (R_SECURITY, "task_category security -> frontier_reasoning"),
    (R_READ_ONLY_FAST, "read_only and latency_preference fast -> fast"),
    (R_BOILERPLATE_RISKY, "boilerplate on high risk -> balanced_coding"),
    (R_BOILERPLATE, "boilerplate -> cheap_coding"),
    (R_COMPLEX, "complex -> balanced_coding, escalating to frontier_reasoning"),
    (R_STANDARD, "otherwise -> balanced_coding"),
)

# Where a class with no eligible candidate falls back to, in order.
CLASS_FALLBACKS: dict[str, tuple[str, ...]] = {
    "cheap_coding": ("balanced_coding",),
    "fast": ("cheap_coding", "balanced_coding"),
    "balanced_coding": ("frontier_reasoning",),
    "frontier_reasoning": ("balanced_coding",),
    # A vision requirement is not satisfiable by a text model.
    "vision": (),
}


@runtime_checkable
class RoutingPolicy(Protocol):
    """A routing policy. Must be deterministic for equal inputs and must only
    select from ``candidates.eligible``."""

    name: str
    version: str

    def decide(
        self,
        context: RoutingContext,
        candidates: CandidateSet,
        *,
        class_pins: Mapping[str, str] | None = None,
    ) -> RoutingDecision: ...


def requested_class_for(context: RoutingContext) -> tuple[str, str]:
    """(routing class, reason token) for *context*; see ``BASELINE_CLASS_RULES``."""
    if context.requested_class in ROUTING_CLASSES:
        return context.requested_class, R_CALLER_CLASS
    if "vision" in context.required_capabilities:
        return "vision", R_REQUIRES_VISION
    category = context.task_category
    if category == "visual":
        return "vision", R_VISUAL_TASK
    if category == "security":
        return "frontier_reasoning", R_SECURITY
    if context.read_only and context.latency_preference == "fast":
        return "fast", R_READ_ONLY_FAST
    if category == "boilerplate":
        if context.risk == "high":
            return "balanced_coding", R_BOILERPLATE_RISKY
        return "cheap_coding", R_BOILERPLATE
    if category == "complex":
        return "balanced_coding", R_COMPLEX
    return "balanced_coding", R_STANDARD if category else R_CATEGORY_UNKNOWN


class DeterministicBaselinePolicy:
    name = BASELINE_POLICY_NAME
    version = BASELINE_POLICY_VERSION

    def _decision(
        self, *, context: RoutingContext, candidates: CandidateSet, **fields: Any
    ) -> RoutingDecision:
        return RoutingDecision(
            policy_name=self.name,
            policy_version=self.version,
            eligible_count=len(candidates.eligible),
            rejected_counts=candidates.rejection_counts(),
            context=context,
            catalog_fingerprint=candidates.catalog_fingerprint,
            candidate_set_version=candidates.version,
            **fields,
        )

    def _select_in_class(
        self,
        class_name: str,
        candidates: CandidateSet,
        pins: Mapping[str, str],
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[Candidate | None, str, tuple[str, ...], tuple[str, str] | None]:
        """(candidate, mode, ranked ids, rejected pin) for one class."""
        cls = ROUTING_CLASSES[class_name]
        rejected_pin: tuple[str, str] | None = None
        pin = pins.get(class_name)
        if pin:
            cand = candidates.get(pin)
            entry = candidates.catalog.get(pin)
            reason = pin_rejection_reason(cls, entry)
            if reason is None and cand is None:
                reason = "not_eligible:" + (candidates.rejection_reason(pin) or "unknown")
            if reason is None and cand is not None and cand.model_id not in exclude:
                return cand, MODE_PINNED, (cand.model_id,), None
            if reason is not None:
                rejected_pin = (pin, reason)
        ranked = [
            e for e in filter_for_class(cls, (c.entry for c in candidates.eligible))
            if e.id not in exclude
        ]
        if not ranked:
            return None, MODE_NONE, (), rejected_pin
        chosen = candidates.get(ranked[0].id)
        return chosen, MODE_ROUTED, tuple(e.id for e in ranked), rejected_pin

    def decide(
        self,
        context: RoutingContext,
        candidates: CandidateSet,
        *,
        class_pins: Mapping[str, str] | None = None,
    ) -> RoutingDecision:
        pins = dict(class_pins or {})
        requested, class_reason = requested_class_for(context)

        if context.explicit_model:
            cand = candidates.get(context.explicit_model)
            plan = build_recovery_plan(
                (),
                verification_available=context.verification_available,
                verification_requested=context.verification_requested,
                explicit_model=True,
            )
            if cand is not None:
                return self._decision(
                    selected_model=cand.model_id,
                    selection_mode=MODE_EXPLICIT,
                    requested_class=None,
                    resolved_class=None,
                    reasons=(R_EXPLICIT,),
                    considered=(cand.model_id,),
                    selected_via=cand.via,
                    recovery=plan,
                    context=context,
                    candidates=candidates,
                )
            return self._decision(
                selected_model=None,
                selection_mode=MODE_NONE,
                requested_class=None,
                resolved_class=None,
                reasons=(R_EXPLICIT_REJECTED,),
                rejected_explicit_reason=(
                    candidates.rejection_reason(context.explicit_model) or "unknown_model"
                ),
                recovery=plan,
                context=context,
                candidates=candidates,
            )

        fallbacks: list[str] = []
        rejected_pin: tuple[str, str] | None = None
        for class_name in (requested, *CLASS_FALLBACKS.get(requested, ())):
            cand, mode, ranked, pin_rej = self._select_in_class(class_name, candidates, pins)
            rejected_pin = rejected_pin or pin_rej
            if cand is None:
                fallbacks.append(class_name)
                continue
            reasons = [class_reason]
            if fallbacks:
                reasons.append(R_CLASS_EMPTY)
            if rejected_pin is not None:
                reasons.append(R_PIN_REJECTED)
            reasons.append(R_PINNED if mode == MODE_PINNED else R_CLASS_RANKED)
            return self._decision(
                selected_model=cand.model_id,
                selection_mode=mode,
                requested_class=requested,
                resolved_class=class_name,
                reasons=tuple(reasons),
                considered=ranked,
                selected_via=cand.via,
                class_fallbacks=tuple(fallbacks),
                rejected_pin=rejected_pin[0] if rejected_pin else None,
                rejected_pin_reason=rejected_pin[1] if rejected_pin else None,
                promotion_candidates=promotion_candidates(
                    ROUTING_CLASSES[class_name], candidates.catalog, cand.entry
                ),
                recovery=self._recovery(context, class_name, candidates, pins, cand),
                context=context,
                candidates=candidates,
            )

        return self._decision(
            selected_model=None,
            selection_mode=MODE_NONE,
            requested_class=requested,
            resolved_class=None,
            reasons=(class_reason, R_NO_CANDIDATE),
            class_fallbacks=tuple(fallbacks),
            rejected_pin=rejected_pin[0] if rejected_pin else None,
            rejected_pin_reason=rejected_pin[1] if rejected_pin else None,
            recovery=build_recovery_plan(
                (),
                verification_available=context.verification_available,
                verification_requested=context.verification_requested,
            ),
            context=context,
            candidates=candidates,
        )

    def _recovery(
        self,
        context: RoutingContext,
        resolved: str,
        candidates: CandidateSet,
        pins: Mapping[str, str],
        selected: Candidate,
    ):
        steps: list[RecoveryStep] = []
        used = {selected.model_id}
        for target in ESCALATION_TARGETS.get(resolved, ()):
            cand, _, _, _ = self._select_in_class(
                target, candidates, pins, exclude=frozenset(used)
            )
            steps.append(RecoveryStep(target, cand.model_id if cand else None))
            if cand is not None:
                used.add(cand.model_id)
        return build_recovery_plan(
            steps,
            verification_available=context.verification_available,
            verification_requested=context.verification_requested,
        )


BASELINE_POLICY = DeterministicBaselinePolicy()


def decide_route(
    context: RoutingContext,
    candidates: CandidateSet,
    *,
    policy: RoutingPolicy | None = None,
    class_pins: Mapping[str, str] | None = None,
) -> RoutingDecision:
    """Run *policy* (default: the deterministic baseline) and enforce the
    contract every policy must meet: the selection is an eligible candidate."""
    decision = (policy or BASELINE_POLICY).decide(context, candidates, class_pins=class_pins)
    if decision.selected_model is not None and candidates.get(decision.selected_model) is None:
        raise ValueError(
            f"routing policy {decision.policy_name!r} selected ineligible model "
            f"{decision.selected_model!r}"
        )
    return decision
