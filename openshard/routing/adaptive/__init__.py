"""Adaptive routing foundation.

    Dynamic model catalog (#347)
      -> eligibility / policy filtering   (candidates.build_candidate_set)
      -> RoutingContext                    (context)
      -> CandidateSet                      (candidates)
      -> RoutingPolicy                     (policy; deterministic baseline today)
      -> RoutingDecision                   (decision; Receipt ``routing_provenance``)
      -> execute -> verification (#348)
      -> RoutingOutcome                    (outcome; derived from the Receipt)
      -> evaluation                        (evaluation) -> future policies

The baseline is deterministic and does not learn. Decisions are recorded in
shadow mode next to the model legacy routing executed, so later policies
(historical Receipt performance, learned or agent-as-a-router routing,
model/harness pair routing) can be evaluated on real outcomes before they are
allowed to choose a model.
"""
from openshard.routing.adaptive.candidates import Candidate, CandidateSet, build_candidate_set
from openshard.routing.adaptive.context import RoutingContext, routing_context_for_run
from openshard.routing.adaptive.decision import RoutingDecision
from openshard.routing.adaptive.outcome import RoutingOutcome, outcome_from_receipt
from openshard.routing.adaptive.policy import (
    BASELINE_POLICY,
    DeterministicBaselinePolicy,
    RoutingPolicy,
    decide_route,
)
from openshard.routing.adaptive.recovery import (
    AttemptResult,
    RecoveryAction,
    RecoveryPlan,
    RecoveryStep,
    next_recovery_action,
)
from openshard.routing.adaptive.runtime import plan_route, shadow_decision_for_run

__all__ = [
    "AttemptResult",
    "BASELINE_POLICY",
    "Candidate",
    "CandidateSet",
    "DeterministicBaselinePolicy",
    "RecoveryAction",
    "RecoveryPlan",
    "RecoveryStep",
    "RoutingContext",
    "RoutingDecision",
    "RoutingOutcome",
    "RoutingPolicy",
    "build_candidate_set",
    "decide_route",
    "next_recovery_action",
    "outcome_from_receipt",
    "plan_route",
    "routing_context_for_run",
    "shadow_decision_for_run",
]
