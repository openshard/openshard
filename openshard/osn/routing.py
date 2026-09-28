"""Adaptive routing for ``openshard osn run`` (dogfood, capability-gated).

Today the first model of an OSN run is the user's ``--model`` or the keyword
router's pick, and later attempts use the ``--escalate-model`` ladder the
user typed. With the Platform capability ``adaptive_routing`` on for the
linked organisation, and only when the user did not name a model, Routing V2
(``openshard.routing.adaptive.policy_v2``) chooses instead:

* the first model is decided for the ``execute`` step from the live catalog,
  the provider keys, the repository's ``models`` policy (including its
  dogfood candidates, which may compete because the capability is on), the
  run's spend cap, and observed history when the sample is meaningful;
* the recovery plan V2 fixed becomes the escalation ladder when the user gave
  none, and the same candidate pool is kept so the supervisor can re-route
  the ``repair`` step on what the run has observed since (``reroute``).

Everything else stays as it was:

* an explicit ``--model`` always wins and is never substituted, and the
  capability is not even looked up;
* an explicit ``--escalate-model`` ladder always wins;
* the ladder is cut to what ``--max-attempts`` (and a budget) can actually
  run, so the record never promises an escalation that cannot happen;
* a ``models:`` policy that cannot be parsed, no eligible candidate, a
  decision that cannot be computed, or a selected model the chosen provider
  cannot dispatch all fall back to the keyword router and say so;
* the capability off or unconfirmed changes nothing about what runs; the V1
  baseline is still recorded in shadow.

The record says which policy decided, for which step, the promotion state
of the model chosen, which discovered models would have qualified, whether
observed history was used and why not, and the ranking components of the
models compared. A dogfood candidate is never selected for a public run
because the candidate set only admits one when this module is on the
applied path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from openshard.routing.adaptive.decision import RECORD_APPLIED, RECORD_SHADOW, RoutingDecision

CAPABILITY = "adaptive_routing"
HARNESS = "osn_loop"

REASON_APPLIED = "applied"
REASON_EXPLICIT_MODEL = "explicit_model"
REASON_NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
REASON_DECISION_UNAVAILABLE = "decision_unavailable"
REASON_MODEL_POLICY_INVALID = "model_policy_invalid"
REASON_PROVIDER_MISMATCH = "provider_mismatch"

LADDER_USER = "user"
LADDER_RECOVERY_PLAN = "recovery_plan"
LADDER_NONE = "none"

HISTORY_NOT_USED = "not_used_insufficient_observed_data"
HISTORY_USED = "used_observed_verified_outcomes"
MAX_CONSIDERED = 8
MAX_RANKING = 3


@dataclass
class OsnRouting:
    """What will run, and what to record about how that was decided."""

    first_model: str
    ladder: list[str]
    decision: RoutingDecision | None
    record_mode: str  # RECORD_SHADOW | RECORD_APPLIED
    record: dict[str, Any] | None = None  # the ``adaptive_routing`` entry block, capability on only
    user_ladder: list[str] = field(default_factory=list)
    # Kept for the repair-step re-route (applied path only); never serialised.
    candidates: Any = field(default=None, repr=False, compare=False)
    policy: Any = field(default=None, repr=False, compare=False)
    class_pins: dict[str, str] | None = field(default=None, repr=False, compare=False)

    @property
    def models(self) -> list[str]:
        return [self.first_model, *self.ladder]

    @property
    def applied(self) -> bool:
        return self.record_mode == RECORD_APPLIED

    def fall_back(self, reason: str, legacy_model: str, **detail: Any) -> None:
        """Stop applying the decision: run the keyword router's model with the user's
        ladder, and record why. Used when something learned after the decision
        (the provider that will dispatch) makes the chosen model unusable."""
        self.first_model = legacy_model
        self.ladder = list(self.user_ladder)
        self.record_mode = RECORD_SHADOW
        base = dict(self.record or {"capability": CAPABILITY, "history_evidence": HISTORY_NOT_USED})
        rejected = base.get("selected_model")
        self.record = {
            "capability": CAPABILITY,
            "record_mode": RECORD_SHADOW,
            "history_evidence": HISTORY_NOT_USED,
            "applied": False,
            "reason": reason,
            "rejected_model": rejected,
            **detail,
        }
        self.candidates = None

    def reroute(
        self,
        *,
        attempt: int,
        models_tried: list[str],
        last_verification_status: str | None,
        last_verification_source: str | None,
        accumulated_cost_usd: float | None,
    ) -> RoutingDecision | None:
        """Re-decide the ``repair`` step over the run's own candidate pool from
        what the run has observed. None when nothing can be decided (the caller
        keeps its fixed plan). Never raises."""
        if not self.applied or self.decision is None or self.candidates is None or self.policy is None:
            return None
        from openshard.routing.adaptive.policy import decide_route
        from openshard.routing.adaptive.step_types import FAILURE_VERIFICATION_FAILED, STEP_REPAIR

        try:
            ctx = replace(
                self.decision.context,
                step_type=STEP_REPAIR,
                attempt=int(attempt),
                models_tried=tuple(models_tried)[:8],
                last_verification_status=last_verification_status,
                last_verification_source=last_verification_source,
                previous_failure_class=(
                    FAILURE_VERIFICATION_FAILED if last_verification_status == "failed" else None
                ),
                accumulated_cost_usd=accumulated_cost_usd,
            )
            return decide_route(ctx, self.candidates, policy=self.policy, class_pins=self.class_pins)
        except Exception:
            return None


def _decision_for(task: str, *, explicit_model: str | None, model_policy) -> RoutingDecision | None:
    """The V1 baseline in shadow: the stable record for explicit and capability-off runs."""
    from openshard.routing.adaptive import shadow_decision_for_run
    from openshard.routing.engine import route

    return shadow_decision_for_run(
        task_category=route(task).category,
        read_only=False,
        write_requested=True,
        risk=None,
        verification_available=True,  # osn run always has a verify command
        verification_requested=True,
        harness=HARNESS,
        model_policy=model_policy,
        explicit_model=explicit_model,
    )


def _v2_decision_for(
    task: str,
    *,
    model_policy,
    cost_budget_usd: float | None,
    history,
) -> tuple[RoutingDecision, Any, Any] | None:
    """(decision, candidate set, policy) for the execute step under Routing V2, or None."""
    from openshard.models.promotion import dogfood_ids
    from openshard.routing.adaptive import routing_context_for_run
    from openshard.routing.adaptive import runtime as adaptive_runtime
    from openshard.routing.adaptive.policy_v2 import TrajectoryPolicyV2
    from openshard.routing.adaptive.step_types import STEP_EXECUTE
    from openshard.routing.engine import route

    try:
        dogfood = model_policy.dogfood_map if model_policy is not None else {}
        policy = TrajectoryPolicyV2(dogfood=dogfood, history=history)
        context = routing_context_for_run(
            task_category=route(task).category,
            read_only=False,
            write_requested=True,
            risk=None,
            verification_available=True,
            verification_requested=True,
            harness=HARNESS,
            step_type=STEP_EXECUTE,
            attempt=1,
            cost_budget_usd=cost_budget_usd,
            dogfood_enabled=True,
        )
        decision, candidates = adaptive_runtime.plan_route_with_candidates(
            context, model_policy=model_policy, policy=policy, dogfood_ids=dogfood_ids(dogfood),
        )
        return decision, candidates, policy
    except Exception:
        return None


def _ladder_from(decision: RoutingDecision, *, max_rungs: int) -> list[str]:
    """The recovery plan's models, in order, without the selected model or repeats,
    cut to the rungs the run can actually climb."""
    steps = getattr(decision.recovery, "steps", ()) or ()
    out: list[str] = []
    for step in steps:
        mid = getattr(step, "model_id", None)
        if isinstance(mid, str) and mid and mid != decision.selected_model and mid not in out:
            out.append(mid)
    return out[: max(0, max_rungs)]


def _compact_ranking(decision: RoutingDecision) -> list[dict[str, Any]]:
    out = []
    for r in decision.ranking[:MAX_RANKING]:
        comps = r.get("components") or {}
        out.append({
            "model": r.get("model"),
            "promotion_state": r.get("promotion_state"),
            "promotion": comps.get("promotion"),
            "history": comps.get("history"),
            "requirement_fit_missing": comps.get("requirement_fit_missing"),
            "superseded_in_family": comps.get("superseded_in_family"),
            "within_price_band": comps.get("within_price_band"),
            "curated_hint_matches": comps.get("curated_hint_matches"),
            "output_price_per_mtok": comps.get("output_price_per_mtok"),
            "notes": list(r.get("notes") or [])[:4],
        })
    return out


def resolve_osn_routing(
    task: str,
    *,
    explicit_model: str | None,
    escalate: list[str],
    capability_enabled: Callable[[], bool],
    legacy_model: Callable[[str], str],
    model_policy_loader: Callable[[], Any] | None = None,
    max_attempts: int | None = None,
    cost_budget_usd: float | None = None,
    history_loader: Callable[[], Any] | None = None,
) -> OsnRouting:
    """Decide the first model and the escalation ladder for one OSN run.

    ``capability_enabled`` is called at most once, and only when no model was
    named. ``model_policy_loader`` and ``history_loader`` are consulted only on
    the applied path, so the repository's ``models`` policy and its recorded
    history cannot change a run the capability does not govern.
    ``max_attempts`` is the most attempts the loop (and any budget) will
    allow; the plan's ladder is cut to ``max_attempts - 1`` rungs.
    ``cost_budget_usd`` is the budget's spend cap, when one is enforced.
    """
    escalate = [m for m in escalate if m]
    if explicit_model:
        # The user's choice; recorded as explicit, never looked up, never substituted.
        decision = _decision_for(task, explicit_model=explicit_model, model_policy=None)
        return OsnRouting(explicit_model, escalate, decision, RECORD_SHADOW, None, escalate)

    if not capability_enabled():
        decision = _decision_for(task, explicit_model=None, model_policy=None)
        return OsnRouting(legacy_model(task), escalate, decision, RECORD_SHADOW, None, escalate)

    base = {
        "capability": CAPABILITY,
        "record_mode": RECORD_SHADOW,
        "history_evidence": HISTORY_NOT_USED,
    }
    model_policy = None
    if model_policy_loader is not None:
        try:
            model_policy = model_policy_loader()
        except Exception as exc:
            # A policy that cannot be read is not silently ignored: the decision
            # would claim to honour it. Route as before and say why.
            decision = _decision_for(task, explicit_model=None, model_policy=None)
            return OsnRouting(
                legacy_model(task), escalate, decision, RECORD_SHADOW,
                {**base, "applied": False, "reason": REASON_MODEL_POLICY_INVALID,
                 "detail": type(exc).__name__},
                escalate,
            )
    history = None
    if history_loader is not None:
        try:
            history = history_loader()
        except Exception:
            history = None  # no history is simply not used; the record says so
    planned = _v2_decision_for(
        task, model_policy=model_policy, cost_budget_usd=cost_budget_usd, history=history,
    )
    if planned is None:
        return OsnRouting(
            legacy_model(task), escalate, None, RECORD_SHADOW,
            {**base, "applied": False, "reason": REASON_DECISION_UNAVAILABLE}, escalate,
        )
    decision, candidates, policy = planned
    if not decision.selected_model:
        return OsnRouting(
            legacy_model(task), escalate, decision, RECORD_SHADOW,
            {
                **base,
                "applied": False,
                "reason": REASON_NO_ELIGIBLE_CANDIDATE,
                "policy": {"name": decision.policy_name, "version": decision.policy_version},
                "requested_class": decision.requested_class,
                "eligible_count": decision.eligible_count,
                "rejected_counts": dict(decision.rejected_counts or {}),
                "reasons": list(decision.reasons),
            },
            escalate,
        )

    rungs = (max(1, int(max_attempts)) - 1) if max_attempts is not None else len(decision.recovery.steps or ())
    if escalate:
        ladder, ladder_source = escalate, LADDER_USER
    else:
        ladder = _ladder_from(decision, max_rungs=rungs)
        ladder_source = LADDER_RECOVERY_PLAN if ladder else LADDER_NONE
    history_record = dict(decision.history_evidence or {})
    record = {
        **base,
        "record_mode": RECORD_APPLIED,
        "applied": True,
        "reason": REASON_APPLIED,
        "selected_model": decision.selected_model,
        "selection_mode": decision.selection_mode,
        "selected_via": list(decision.selected_via or ()),
        "routing_class": decision.resolved_class,
        "requested_class": decision.requested_class,
        "policy": {"name": decision.policy_name, "version": decision.policy_version},
        "step_type": decision.step_type,
        "promotion_state": decision.selected_promotion_state,
        "considered": list(decision.considered[:MAX_CONSIDERED]),
        "ranking": _compact_ranking(decision),
        "eligible_count": decision.eligible_count,
        "rejected_counts": dict(decision.rejected_counts or {}),
        "shadow_candidates": list(decision.shadow_candidates[:3]),
        "history_evidence": HISTORY_USED if history_record.get("used") else HISTORY_NOT_USED,
        "history": history_record or None,
        "reasons": list(decision.reasons),
        "escalation_ladder": list(ladder),
        "ladder_source": ladder_source,
        # True only when a rung exists that this run could actually reach.
        "recovery_enabled": bool(ladder) and ladder_source == LADDER_RECOVERY_PLAN,
        "max_attempts": int(max_attempts) if max_attempts is not None else None,
        "decision_fingerprint": decision.decision_fingerprint,
    }
    return OsnRouting(
        decision.selected_model, ladder, decision, RECORD_APPLIED, record, escalate,
        candidates=candidates, policy=policy,
        class_pins=model_policy.class_pin_map if model_policy is not None else None,
    )


__all__ = [
    "CAPABILITY",
    "HARNESS",
    "HISTORY_NOT_USED",
    "HISTORY_USED",
    "LADDER_NONE",
    "LADDER_RECOVERY_PLAN",
    "LADDER_USER",
    "REASON_APPLIED",
    "REASON_DECISION_UNAVAILABLE",
    "REASON_EXPLICIT_MODEL",
    "REASON_MODEL_POLICY_INVALID",
    "REASON_NO_ELIGIBLE_CANDIDATE",
    "REASON_PROVIDER_MISMATCH",
    "OsnRouting",
    "resolve_osn_routing",
]
