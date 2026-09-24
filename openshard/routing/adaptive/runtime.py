"""Run-path entry point: compute the adaptive decision for a run in shadow mode.

The run pipeline still executes the model chosen by legacy routing (keyword
category -> provider-aware resolver -> scored selection). This helper computes
what the adaptive baseline decides for the same run, from the same facts, so
the Receipt can record both. Recorded decisions next to real outcomes are the
evidence a future policy is evaluated against before it is allowed to route.

Offline and cheap: the catalog is read from the local cache only
(``refresh="never"``), availability from key presence. Never raises.
"""
from __future__ import annotations

from openshard.routing.adaptive.candidates import build_candidate_set
from openshard.routing.adaptive.context import RoutingContext, routing_context_for_run
from openshard.routing.adaptive.decision import RoutingDecision
from openshard.routing.adaptive.policy import RoutingPolicy, decide_route


def plan_route(
    context: RoutingContext,
    *,
    model_policy=None,
    policy: RoutingPolicy | None = None,
    catalog=None,
    availability=None,
) -> RoutingDecision:
    """Catalog -> eligibility -> candidate set -> policy -> decision."""
    if catalog is None:
        from openshard.models.catalog import load_catalog

        catalog = load_catalog(refresh="never")
    if availability is None:
        from openshard.routing.provider_availability import detect_provider_availability

        availability = detect_provider_availability()
    candidates = build_candidate_set(
        catalog,
        availability,
        policy=model_policy,
        harness=context.harness,
        required_capabilities=context.required_capabilities,
        min_context_tokens=context.min_context_tokens,
        explicit_model=context.explicit_model,
    )
    pins = model_policy.class_pin_map if model_policy is not None else None
    return decide_route(context, candidates, policy=policy, class_pins=pins)


def shadow_decision_for_run(
    *,
    task_category: str | None,
    read_only: bool | None,
    write_requested: bool | None,
    risk: str | None,
    repo_facts=None,
    verification_available: bool | None,
    verification_requested: bool | None,
    harness: str | None,
    model_policy=None,
) -> RoutingDecision | None:
    """The baseline decision for a pipeline run, or None if it cannot be
    computed. Never raises: shadow routing must not affect a run."""
    try:
        context = routing_context_for_run(
            task_category=task_category,
            read_only=read_only,
            write_requested=write_requested,
            risk=risk,
            repo_facts=repo_facts,
            verification_available=verification_available,
            verification_requested=verification_requested,
            harness=harness,
        )
        return plan_route(context, model_policy=model_policy)
    except Exception:
        return None
