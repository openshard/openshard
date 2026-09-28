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

Two deterministic policies implement ``RoutingPolicy``: the V1 baseline
(``policy``), the stable behaviour recorded in shadow for ``openshard run``,
and the trajectory-aware V2 (``policy_v2``), applied to ``openshard osn run``
behind the ``adaptive_routing`` capability. Neither learns. A future learned
router is a third implementation over the same ``RoutingContext`` and
candidate features; execution does not change.
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
from openshard.routing.adaptive.policy_v2 import TRAJECTORY_POLICY, TrajectoryPolicyV2
from openshard.routing.adaptive.recovery import (
    AttemptResult,
    RecoveryAction,
    RecoveryPlan,
    RecoveryStep,
    next_recovery_action,
)
from openshard.routing.adaptive.runtime import (
    plan_route,
    plan_route_with_candidates,
    shadow_decision_for_run,
)

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
    "TRAJECTORY_POLICY",
    "TrajectoryPolicyV2",
    "build_candidate_set",
    "decide_route",
    "next_recovery_action",
    "outcome_from_receipt",
    "plan_route",
    "plan_route_with_candidates",
    "routing_context_for_run",
    "shadow_decision_for_run",
]
