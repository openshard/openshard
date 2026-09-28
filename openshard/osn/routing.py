"""Adaptive routing for ``openshard osn run`` (dogfood V1, capability-gated).

Today the first model of an OSN run is the user's ``--model`` or the keyword
router's pick, and later attempts use the ``--escalate-model`` ladder the
user typed. The adaptive baseline (``openshard.routing.adaptive``) already
computes a full decision for every run -- candidates from the catalog and
the provider keys, the repository's model policy, a routing class for the
task, and a recovery ladder that escalates only after an observed
verification failure -- but it has only ever been *recorded*.

With the Platform capability ``adaptive_routing`` on for the linked
organisation, and only when the user did not name a model, that decision
now *chooses*: its selected model runs first and its recovery steps become
the escalation ladder when the user gave none. Everything else stays as it
was:

* an explicit ``--model`` always wins and is never substituted;
* an explicit ``--escalate-model`` ladder always wins;
* no eligible candidate, or no decision at all, falls back to the keyword
  router exactly as before;
* the capability off or unconfirmed changes nothing, and no lookup is made
  when the user named a model.

Evidence used: task category (keyword classifier), routing class, catalog
eligibility and capability tags, provider availability, the repository's
model policy, and the user's explicit choice. Historical per-model success
is *not* used: there is not enough observed data to route on, and the
record says so rather than implying otherwise. Retry evidence (an observed
verification failure) is what the loop already uses before it climbs the
ladder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openshard.routing.adaptive.decision import RECORD_APPLIED, RECORD_SHADOW, RoutingDecision

CAPABILITY = "adaptive_routing"
HARNESS = "osn_loop"

REASON_APPLIED = "applied"
REASON_EXPLICIT_MODEL = "explicit_model"
REASON_NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
REASON_DECISION_UNAVAILABLE = "decision_unavailable"

LADDER_USER = "user"
LADDER_RECOVERY_PLAN = "recovery_plan"
LADDER_NONE = "none"

HISTORY_NOT_USED = "not_used_insufficient_observed_data"


@dataclass
class OsnRouting:
    """What will run, and what to record about how that was decided."""

    first_model: str
    ladder: list[str]
    decision: RoutingDecision | None
    record_mode: str  # RECORD_SHADOW | RECORD_APPLIED
    record: dict[str, Any] | None = None  # the ``adaptive_routing`` entry block, capability on only
    considered: list[str] = field(default_factory=list)

    @property
    def models(self) -> list[str]:
        return [self.first_model, *self.ladder]


def _decision_for(task: str, *, explicit_model: str | None, model_policy) -> RoutingDecision | None:
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


def _ladder_from(decision: RoutingDecision) -> list[str]:
    steps = getattr(decision.recovery, "steps", ()) or ()
    out: list[str] = []
    for step in steps:
        mid = getattr(step, "model_id", None)
        if isinstance(mid, str) and mid and mid not in out:
            out.append(mid)
    return out


def resolve_osn_routing(
    task: str,
    *,
    explicit_model: str | None,
    escalate: list[str],
    capability_enabled: Callable[[], bool],
    legacy_model: Callable[[str], str],
    model_policy_loader: Callable[[], Any] | None = None,
) -> OsnRouting:
    """Decide the first model and the escalation ladder for one OSN run.

    ``capability_enabled`` is called at most once, and only when no model was
    named. ``model_policy_loader`` is consulted only on the applied path, so
    the repository's ``models`` policy cannot change a run the capability does
    not govern.
    """
    escalate = [m for m in escalate if m]
    if explicit_model:
        # The user's choice; recorded as explicit, never looked up, never substituted.
        decision = _decision_for(task, explicit_model=explicit_model, model_policy=None)
        return OsnRouting(explicit_model, escalate, decision, RECORD_SHADOW)

    if not capability_enabled():
        decision = _decision_for(task, explicit_model=None, model_policy=None)
        return OsnRouting(legacy_model(task), escalate, decision, RECORD_SHADOW)

    model_policy = None
    if model_policy_loader is not None:
        try:
            model_policy = model_policy_loader()
        except Exception:
            model_policy = None
    decision = _decision_for(task, explicit_model=None, model_policy=model_policy)
    base = {
        "capability": CAPABILITY,
        "record_mode": RECORD_SHADOW,
        "history_evidence": HISTORY_NOT_USED,
    }
    if decision is None:
        return OsnRouting(
            legacy_model(task), escalate, None, RECORD_SHADOW,
            {**base, "applied": False, "reason": REASON_DECISION_UNAVAILABLE},
        )
    if not decision.selected_model:
        return OsnRouting(
            legacy_model(task), escalate, decision, RECORD_SHADOW,
            {
                **base,
                "applied": False,
                "reason": REASON_NO_ELIGIBLE_CANDIDATE,
                "requested_class": decision.requested_class,
                "eligible_count": decision.eligible_count,
                "rejected_counts": dict(decision.rejected_counts or {}),
            },
        )

    if escalate:
        ladder, ladder_source = escalate, LADDER_USER
    else:
        ladder = _ladder_from(decision)
        ladder_source = LADDER_RECOVERY_PLAN if ladder else LADDER_NONE
    record = {
        **base,
        "record_mode": RECORD_APPLIED,
        "applied": True,
        "reason": REASON_APPLIED,
        "selected_model": decision.selected_model,
        "selection_mode": decision.selection_mode,
        "routing_class": decision.resolved_class,
        "requested_class": decision.requested_class,
        "policy": {"name": decision.policy_name, "version": decision.policy_version},
        "considered": list(decision.considered[:8]),
        "escalation_ladder": list(ladder),
        "ladder_source": ladder_source,
        "recovery_enabled": bool(getattr(decision.recovery, "enabled", False)),
        "decision_fingerprint": decision.decision_fingerprint,
    }
    return OsnRouting(
        decision.selected_model, ladder, decision, RECORD_APPLIED, record, list(decision.considered[:8]),
    )


__all__ = [
    "CAPABILITY",
    "HARNESS",
    "HISTORY_NOT_USED",
    "LADDER_NONE",
    "LADDER_RECOVERY_PLAN",
    "LADDER_USER",
    "REASON_APPLIED",
    "REASON_DECISION_UNAVAILABLE",
    "REASON_EXPLICIT_MODEL",
    "REASON_NO_ELIGIBLE_CANDIDATE",
    "OsnRouting",
    "resolve_osn_routing",
]
