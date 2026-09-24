"""Adaptive Routing v1: catalog -> eligibility -> context -> candidates -> policy
-> decision -> (execute, verify) -> outcome.

Covers the deterministic baseline and its contracts: candidates come only from
the catalog under the existing lifecycle/availability/policy rules, explicit
choices are never substituted, decisions are reproducible and explain
themselves, recovery is bounded, and outcomes never invent evidence.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openshard.models.catalog import build_catalog, curated_catalog
from openshard.models.registry import ModelEntry
from openshard.routing.adaptive import (
    BASELINE_POLICY,
    AttemptResult,
    RoutingContext,
    RoutingDecision,
    build_candidate_set,
    decide_route,
    next_recovery_action,
    outcome_from_receipt,
    plan_route,
    shadow_decision_for_run,
)
from openshard.routing.adaptive.decision import (
    MODE_EXPLICIT,
    MODE_NONE,
    MODE_PINNED,
    MODE_ROUTED,
)
from openshard.routing.adaptive.evaluation import (
    REPRESENTATIVE_SCENARIOS,
    VIOLATION_POLICY_ERROR,
    evaluate_decisions,
    summarize_outcomes,
)
from openshard.routing.adaptive.outcome import RoutingOutcome
from openshard.routing.adaptive.policy import (
    R_CLASS_EMPTY,
    R_EXPLICIT_REJECTED,
    R_NO_CANDIDATE,
    R_PIN_REJECTED,
    requested_class_for,
)
from openshard.routing.adaptive.recovery import (
    ACTION_ESCALATE,
    ACTION_STOP,
    DISABLED_EXPLICIT_MODEL,
    DISABLED_NO_VERIFICATION,
    MAX_ATTEMPTS_CEILING,
    STOP_ATTEMPTS_EXHAUSTED,
    STOP_COST_EXHAUSTED,
    STOP_FAILURE_NOT_OBSERVED,
    STOP_LADDER_EXHAUSTED,
    STOP_OUTCOME_UNKNOWN,
    STOP_SUCCESS_NOT_OBSERVED,
    STOP_VERIFIED_SUCCESS,
    RecoveryPlan,
    RecoveryStep,
    build_recovery_plan,
)
from openshard.routing.model_policy import ModelPolicyConfig
from openshard.routing.provider_availability import ProviderAvailability
from openshard.routing.routing_classes import CLASS_NAMES, select_for_class

SYNCED_AT = "2026-09-24T00:00:00Z"
OPENROUTER = ProviderAvailability(("openrouter",), True, False, False)
NO_KEYS = ProviderAvailability((), False, False, False)


def _m(mid: str, **kw) -> ModelEntry:
    defaults = dict(
        display_name=mid, provider=mid.split("/")[0], tier="mid", cost_class="mid",
        supports_tools=True, context_length=200_000, lifecycle="active_default",
    )
    defaults.update(kw)
    return ModelEntry(id=mid, **defaults)


CURATED = [
    _m("acme/cheap-1", tier="cheap", cost_class="cheap", roles=("cheap_control", "boilerplate")),
    _m("acme/mid-1", roles=("standard_coding",)),
    _m("zeta/mid-2"),
    _m("acme/frontier-1", tier="frontier", cost_class="expensive", supports_reasoning=True,
       roles=("escalation",), lifecycle="active_specialist"),
    _m("acme/vision-1", input_modalities=("text", "image"), roles=("visual",),
       lifecycle="active_specialist"),
    _m("acme/fast-1", tier="tiny", cost_class="tiny", latency_class="fast",
       roles=("fast_chat",)),
    _m("acme/old-1", lifecycle="deprecated"),
    _m("acme/exp-1", lifecycle="experimental"),
]
DISCOVERED = [
    {
        "id": "acme/new-2",
        "name": "Acme: New 2",
        "created": 1790000000,
        "context_length": 1_000_000,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "pricing": {"prompt": "0.0000002", "completion": "0.0000008"},
        "supported_parameters": ["tools"],
    }
]


def _catalog(curated=CURATED, discovered=DISCOVERED):
    return build_catalog(curated, discovered, synced_at=SYNCED_AT)


def _cands(policy=None, availability=OPENROUTER, catalog=None, **kw):
    return build_candidate_set(catalog or _catalog(), availability, policy=policy, **kw)


def _decide(ctx=None, *, policy=None, availability=OPENROUTER, catalog=None, **kw):
    ctx = ctx or RoutingContext(task_category="standard")
    cs = _cands(policy, availability, catalog, explicit_model=ctx.explicit_model,
                required_capabilities=ctx.required_capabilities, **kw)
    pins = policy.class_pin_map if policy is not None else None
    return decide_route(ctx, cs, class_pins=pins)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_decision(self):
        a = _decide(RoutingContext(task_category="boilerplate", risk="low"))
        b = _decide(RoutingContext(task_category="boilerplate", risk="low"))
        assert a.selected_model == b.selected_model == "acme/cheap-1"
        assert a.decision_fingerprint == b.decision_fingerprint
        assert a.to_provenance() == b.to_provenance()

    def test_catalog_input_order_does_not_matter(self):
        shuffled = list(CURATED)
        random.Random(7).shuffle(shuffled)
        a = _decide(catalog=_catalog())
        b = _decide(catalog=_catalog(curated=shuffled))
        assert a.selected_model == b.selected_model
        assert a.considered == b.considered
        assert a.decision_fingerprint == b.decision_fingerprint

    def test_fingerprint_changes_with_context(self):
        a = _decide(RoutingContext(task_category="standard"))
        b = _decide(RoutingContext(task_category="standard", risk="high"))
        assert a.context.fingerprint != b.context.fingerprint
        assert a.decision_fingerprint != b.decision_fingerprint


# ---------------------------------------------------------------------------
# Candidate eligibility / lifecycle
# ---------------------------------------------------------------------------


class TestCandidates:
    def test_rejected_and_eligible_partition_catalog(self):
        cat = _catalog()
        cs = _cands(catalog=cat)
        ids = {c.model_id for c in cs.eligible} | {m for m, _ in cs.rejected}
        assert ids == {e.id for e in cat.entries}
        assert not {c.model_id for c in cs.eligible} & {m for m, _ in cs.rejected}

    def test_deprecated_rejected(self):
        assert _cands().rejection_reason("acme/old-1") == "status:deprecated"

    def test_unpromoted_lifecycle_rejected(self):
        assert _cands().rejection_reason("acme/exp-1") == "lifecycle:experimental"

    def test_discovered_model_is_not_a_candidate(self):
        cs = _cands()
        assert cs.get("acme/new-2") is None
        assert cs.rejection_reason("acme/new-2") == "eligibility:not_promoted"

    def test_no_provider_key_rejects_everything(self):
        cs = _cands(availability=NO_KEYS)
        assert cs.eligible == ()
        assert set(cs.rejection_counts()) == {"no_api_key"}

    def test_direct_provider_reaches_only_its_vendor(self):
        cat = _catalog(curated=[_m("openai/gpt-x"), _m("acme/mid-1")], discovered=[])
        cs = build_candidate_set(cat, ProviderAvailability(("openai",), False, False, True))
        assert [c.model_id for c in cs.eligible] == ["openai/gpt-x"]
        assert cs.get("openai/gpt-x").via == ("openai",)

    def test_policy_lifecycle_flag_admits_candidate_but_classes_never_pick_it(self):
        cs = _cands(ModelPolicyConfig(allow_experimental=True))
        assert cs.get("acme/exp-1") is not None
        for cls in CLASS_NAMES:
            d = decide_route(RoutingContext(requested_class=cls), cs)
            assert d.selected_model != "acme/exp-1"

    def test_harness_constraint(self):
        cat = _catalog(curated=[_m("openai/gpt-x")], discovered=[])
        avail = ProviderAvailability(("openai",), False, False, True)
        cs = build_candidate_set(cat, avail, harness="opencode")
        assert cs.rejection_reason("openai/gpt-x") == "executor_constraint"

    def test_min_context(self):
        cs = _cands(min_context_tokens=500_000)
        assert cs.get("acme/mid-1") is None
        assert cs.rejection_reason("acme/mid-1") == "context_window_too_small"


# ---------------------------------------------------------------------------
# Policy allow/block
# ---------------------------------------------------------------------------


class TestModelPolicy:
    def test_blocked_model_moves_selection(self):
        d = _decide(policy=ModelPolicyConfig(blocked_models=frozenset({"acme/mid-1"})))
        assert d.selected_model == "zeta/mid-2"
        assert d.rejected_counts.get("policy:blocked_model") == 1

    def test_allowed_providers(self):
        d = _decide(policy=ModelPolicyConfig(allowed_providers=frozenset({"zeta"})))
        assert d.selected_model == "zeta/mid-2"

    def test_cost_cap_falls_back_to_other_class(self):
        # Frontier is expensive; a cheap cap empties it and the fallback is recorded.
        d = _decide(
            RoutingContext(task_category="security"),
            policy=ModelPolicyConfig(max_cost_class="mid"),
        )
        assert d.requested_class == "frontier_reasoning"
        assert d.resolved_class == "balanced_coding"
        assert d.class_fallbacks == ("frontier_reasoning",)
        assert R_CLASS_EMPTY in d.reasons


# ---------------------------------------------------------------------------
# Explicit selection and pins
# ---------------------------------------------------------------------------


class TestExplicitAndPins:
    def test_explicit_model_selected(self):
        d = _decide(RoutingContext(task_category="boilerplate", explicit_model="acme/mid-1"))
        assert (d.selected_model, d.selection_mode) == ("acme/mid-1", MODE_EXPLICIT)
        assert d.recovery.enabled is False
        assert d.recovery.disabled_reason == DISABLED_EXPLICIT_MODEL

    def test_explicit_discovered_model_allowed(self):
        d = _decide(RoutingContext(explicit_model="acme/new-2"))
        assert d.selected_model == "acme/new-2"

    def test_explicit_blocked_model_is_not_substituted(self):
        d = _decide(
            RoutingContext(explicit_model="acme/mid-1"),
            policy=ModelPolicyConfig(blocked_models=frozenset({"acme/mid-1"})),
        )
        assert d.selected_model is None
        assert d.selection_mode == MODE_NONE
        assert d.reasons == (R_EXPLICIT_REJECTED,)
        assert d.rejected_explicit_reason == "policy:blocked_model"

    def test_explicit_deprecated_model_rejected(self):
        d = _decide(RoutingContext(explicit_model="acme/old-1"))
        assert d.selected_model is None
        assert d.rejected_explicit_reason == "status:deprecated"

    def test_pin_wins(self):
        pol = ModelPolicyConfig(class_pins=(("balanced_coding", "zeta/mid-2"),))
        d = _decide(policy=pol)
        assert (d.selected_model, d.selection_mode) == ("zeta/mid-2", MODE_PINNED)

    def test_pin_to_discovered_model(self):
        pol = ModelPolicyConfig(class_pins=(("cheap_coding", "acme/new-2"),))
        d = _decide(RoutingContext(task_category="boilerplate"), policy=pol)
        assert (d.selected_model, d.selection_mode) == ("acme/new-2", MODE_PINNED)

    def test_rejected_pin_is_recorded_and_routing_continues(self):
        pol = ModelPolicyConfig(
            class_pins=(("balanced_coding", "zeta/mid-2"),),
            blocked_models=frozenset({"zeta/mid-2"}),
        )
        d = _decide(policy=pol)
        assert d.selection_mode == MODE_ROUTED
        assert d.selected_model == "acme/mid-1"
        assert d.rejected_pin == "zeta/mid-2"
        assert d.rejected_pin_reason == "not_eligible:policy:blocked_model"
        assert R_PIN_REJECTED in d.reasons


# ---------------------------------------------------------------------------
# Routing classes
# ---------------------------------------------------------------------------


class TestRoutingClasses:
    @pytest.mark.parametrize(
        ("ctx", "cls"),
        [
            (RoutingContext(task_category="boilerplate", risk="low"), "cheap_coding"),
            (RoutingContext(task_category="boilerplate", risk="high"), "balanced_coding"),
            (RoutingContext(task_category="standard"), "balanced_coding"),
            (RoutingContext(task_category="complex"), "balanced_coding"),
            (RoutingContext(task_category="security"), "frontier_reasoning"),
            (RoutingContext(task_category="visual"), "vision"),
            (RoutingContext(required_capabilities=frozenset({"vision"})), "vision"),
            (RoutingContext(read_only=True, latency_preference="fast"), "fast"),
            (RoutingContext(requested_class="fast", task_category="security"), "fast"),
            (RoutingContext(), "balanced_coding"),
        ],
    )
    def test_requested_class(self, ctx, cls):
        assert requested_class_for(ctx)[0] == cls

    @pytest.mark.parametrize(
        ("cls", "model"),
        [
            ("cheap_coding", "acme/cheap-1"),
            ("balanced_coding", "acme/mid-1"),
            ("frontier_reasoning", "acme/frontier-1"),
            ("fast", "acme/fast-1"),
            ("vision", "acme/vision-1"),
        ],
    )
    def test_class_resolves_by_capability(self, cls, model):
        d = _decide(RoutingContext(requested_class=cls))
        assert d.resolved_class == cls
        assert d.selected_model == model

    def test_newer_discovered_model_surfaces_as_promotion_candidate_only(self):
        d = _decide(RoutingContext(requested_class="cheap_coding"))
        assert d.selected_model == "acme/cheap-1"
        assert "acme/new-2" not in d.considered


# ---------------------------------------------------------------------------
# Missing capability / no candidate
# ---------------------------------------------------------------------------


class TestMissingCapabilityAndNoCandidate:
    def test_missing_capability_has_no_text_fallback(self):
        cat = _catalog(curated=[c for c in CURATED if c.id != "acme/vision-1"])
        d = _decide(RoutingContext(required_capabilities=frozenset({"vision"})), catalog=cat)
        assert d.selected_model is None
        assert d.resolved_class is None
        assert R_NO_CANDIDATE in d.reasons
        assert d.rejected_counts.get("missing_capability")

    def test_no_candidates_never_raises(self):
        d = _decide(availability=NO_KEYS)
        assert d.selected_model is None
        assert d.selection_mode == MODE_NONE
        assert d.eligible_count == 0
        assert d.recovery.enabled is False


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_provenance_answers_the_questions(self):
        ctx = RoutingContext(task_category="boilerplate", risk="low", harness="native",
                             verification_available=True, verification_requested=True)
        d = _decide(ctx)
        p = d.to_provenance(executed_model="acme/mid-1")
        json.dumps(p)  # JSON-safe
        assert p["considered"][0] == p["selected_model"] == "acme/cheap-1"
        assert p["policy"] == {"name": "deterministic_baseline", "version": "1"}
        assert (p["requested_class"], p["resolved_class"]) == ("cheap_coding", "cheap_coding")
        assert p["selection_mode"] == MODE_ROUTED
        assert p["reasons"][0] == "low_risk_boilerplate"
        assert p["record_mode"] == "shadow"
        assert p["agrees_with_execution"] is False
        assert p["context"]["harness"] == "native"
        assert set(p["fingerprints"]) == {"context", "catalog", "decision"}

    def test_no_fake_scores_or_confidence(self):
        d = _decide()
        assert d.score is None and d.confidence is None
        p = d.to_provenance()
        assert p["score"] is None and p["confidence"] is None

    def test_policy_rejections_are_counts_not_lists(self):
        pol = ModelPolicyConfig(blocked_models=frozenset({"acme/mid-1"}))
        p = _decide(policy=pol).to_provenance()
        assert isinstance(p["rejected_counts"], dict)
        assert "acme/mid-1" not in json.dumps(p["rejected_counts"])

    def test_agreement_unknown_without_executed_model(self):
        assert _decide().to_provenance()["agrees_with_execution"] is None


# ---------------------------------------------------------------------------
# Bounded recovery
# ---------------------------------------------------------------------------


def _attempt(model, cls=None, status="failed", source="directly_observed", cost=0.01):
    return AttemptResult(model, cls, status, source, cost)


class TestRecovery:
    def _plan(self):
        ctx = RoutingContext(task_category="boilerplate", risk="low",
                             verification_available=True, verification_requested=True)
        return _decide(ctx).recovery

    def test_plan_escalates_up_the_ladder(self):
        plan = self._plan()
        assert plan.enabled
        assert [(s.routing_class, s.model_id) for s in plan.steps] == [
            ("balanced_coding", "acme/mid-1"),
            ("frontier_reasoning", "acme/frontier-1"),
        ]
        assert plan.max_attempts == 3

    def test_cheap_then_escalate_then_stop(self):
        plan = self._plan()
        a1 = _attempt("acme/cheap-1", "cheap_coding")
        act = next_recovery_action(plan, [a1])
        assert (act.action, act.step.model_id) == (ACTION_ESCALATE, "acme/mid-1")
        a2 = _attempt("acme/mid-1", "balanced_coding")
        act = next_recovery_action(plan, [a1, a2])
        assert (act.action, act.step.model_id) == (ACTION_ESCALATE, "acme/frontier-1")
        a3 = _attempt("acme/frontier-1", "frontier_reasoning")
        act = next_recovery_action(plan, [a1, a2, a3])
        assert (act.action, act.reason) == (ACTION_STOP, STOP_ATTEMPTS_EXHAUSTED)

    def test_success_stops(self):
        act = next_recovery_action(self._plan(), [_attempt("acme/cheap-1", status="passed")])
        assert (act.action, act.reason) == (ACTION_STOP, STOP_VERIFIED_SUCCESS)

    def test_agent_reported_results_never_escalate(self):
        plan = self._plan()
        passed = _attempt("acme/cheap-1", status="passed", source="agent_reported")
        failed = _attempt("acme/cheap-1", source="agent_reported")
        assert next_recovery_action(plan, [passed]).reason == STOP_SUCCESS_NOT_OBSERVED
        assert next_recovery_action(plan, [failed]).reason == STOP_FAILURE_NOT_OBSERVED

    @pytest.mark.parametrize("status", ["unknown", "not_run", "partial", None])
    def test_missing_evidence_stops(self, status):
        act = next_recovery_action(self._plan(), [_attempt("acme/cheap-1", status=status)])
        assert (act.action, act.reason) == (ACTION_STOP, STOP_OUTCOME_UNKNOWN)

    def test_disabled_without_verification(self):
        d = _decide(RoutingContext(task_category="boilerplate", verification_available=False))
        assert d.recovery.enabled is False
        assert d.recovery.disabled_reason == DISABLED_NO_VERIFICATION
        act = next_recovery_action(d.recovery, [_attempt("acme/cheap-1")])
        assert act.action == ACTION_STOP

    def test_cost_budget(self):
        plan = self._plan()
        assert next_recovery_action(
            plan, [_attempt("acme/cheap-1", cost=0.5)], cost_budget_usd=0.25
        ).reason == STOP_COST_EXHAUSTED
        # Unknown spend cannot be shown to be under budget.
        assert next_recovery_action(
            plan, [_attempt("acme/cheap-1", cost=None)], cost_budget_usd=10.0
        ).reason == STOP_COST_EXHAUSTED

    def test_never_retries_a_tried_model_or_class(self):
        plan = RecoveryPlan(
            steps=(RecoveryStep("balanced_coding", "acme/mid-1"),),
            max_attempts=3, enabled=True,
        )
        act = next_recovery_action(plan, [_attempt("acme/mid-1", "balanced_coding")])
        assert (act.action, act.reason) == (ACTION_STOP, STOP_LADDER_EXHAUSTED)

    def test_attempt_cap_is_bounded(self):
        steps = [RecoveryStep(f"c{i}", f"m/{i}") for i in range(10)]
        plan = build_recovery_plan(steps, verification_available=True, verification_requested=True,
                                   max_attempts=50)
        assert plan.max_attempts == MAX_ATTEMPTS_CEILING
        attempts = [_attempt("m/start")]
        for _ in range(20):
            act = next_recovery_action(plan, attempts)
            if act.action == ACTION_STOP:
                break
            attempts.append(_attempt(act.step.model_id, act.step.routing_class))
        assert len(attempts) == MAX_ATTEMPTS_CEILING


# ---------------------------------------------------------------------------
# Backwards compatibility with #347 catalog behaviour
# ---------------------------------------------------------------------------


class TestCatalogCompatibility:
    def test_curated_class_selection_unchanged(self):
        """With every model reachable and no policy, the baseline picks exactly
        what #347's ``select_for_class`` picks for each routing class."""
        cat = curated_catalog()
        cs = build_candidate_set(cat, OPENROUTER)
        for cls in CLASS_NAMES:
            d = decide_route(RoutingContext(requested_class=cls), cs)
            assert d.selected_model == select_for_class(cls, cat).model, cls

    def test_resolver_constants_unchanged(self):
        from openshard.routing import model_resolver as mr

        cat = curated_catalog()
        assert mr.MODEL_CHEAP == select_for_class("cheap_coding", cat).model
        assert mr.MODEL_MAIN == select_for_class("balanced_coding", cat).model

    def test_representative_scenarios_on_curated_catalog(self):
        cs = build_candidate_set(curated_catalog(), OPENROUTER)
        results = evaluate_decisions(REPRESENTATIVE_SCENARIOS, cs, [BASELINE_POLICY])
        assert len(results) == len(REPRESENTATIVE_SCENARIOS)
        for r in results:
            assert r.violations == (), r.scenario
            assert r.acceptable, (r.scenario, r.resolved_class)
            assert r.selected_model is not None


# ---------------------------------------------------------------------------
# Outcomes from Receipts
# ---------------------------------------------------------------------------

LEGACY_RECEIPT = {
    "schema_version": "1",
    "receipt_id": "rcpt_1",
    "execution_model": "z-ai/glm-5.1",
    "retry_triggered": False,
    "duration_seconds": 12.5,
    "estimated_cost": 0.02,
    "verification_attempted": True,
    "verification_passed": True,
}


class TestOutcome:
    def test_legacy_receipt_without_provenance(self):
        before = json.dumps(LEGACY_RECEIPT, sort_keys=True)
        o = outcome_from_receipt(LEGACY_RECEIPT)
        assert json.dumps(LEGACY_RECEIPT, sort_keys=True) == before  # not mutated
        assert o.final_model == "z-ai/glm-5.1"
        assert o.routed_model is None and o.decision_fingerprint is None
        assert o.attempts == 1
        assert o.latency_seconds == 12.5
        assert o.cost_usd == 0.02
        assert o.human_correction is None

    def test_retry_makes_attempts_and_partial_cost_unknown(self):
        o = outcome_from_receipt({**LEGACY_RECEIPT, "retry_triggered": True,
                                  "fixer_model": "anthropic/claude-opus-4.7"})
        assert o.attempts is None
        assert o.cost_usd is None
        assert o.escalation_model == "anthropic/claude-opus-4.7"

    def test_agent_reported_pass_is_not_verified_success(self):
        rec = {**LEGACY_RECEIPT, "verification": {
            "version": 1, "status": "passed", "source": "agent_reported",
            "observation_mode": "agent_claim",
        }}
        o = outcome_from_receipt(rec)
        assert o.verification_status == "passed"
        assert o.verification_source == "agent_reported"
        assert o.verified_success is None

    def test_observed_pass_is_verified_success(self):
        rec = {**LEGACY_RECEIPT, "verification": {
            "version": 1, "status": "passed", "source": "directly_observed",
            "observation_mode": "openshard_executed",
        }}
        assert outcome_from_receipt(rec).verified_success is True

    def test_links_to_decision(self):
        d = _decide()
        rec = {**LEGACY_RECEIPT, "routing_provenance": d.to_provenance(executed_model="acme/mid-1")}
        o = outcome_from_receipt(rec)
        assert o.decision_fingerprint == d.decision_fingerprint
        assert o.routed_model == "acme/mid-1"
        assert o.shadow_agreed is True
        assert o.policy_name == "deterministic_baseline"

    def test_garbage_never_raises(self):
        for bad in (None, [], "x", {"routing_provenance": "nope", "estimated_cost": "1"}):
            outcome_from_receipt(bad)


# ---------------------------------------------------------------------------
# Evaluation harness
# ---------------------------------------------------------------------------


def _outcome(**kw) -> RoutingOutcome:
    base = dict(
        receipt_id=None, decision_fingerprint=None, policy_name=None, routed_model=None,
        final_model="m", routing_class=None, harness=None, verification_status="unknown",
        verification_source=None, verification_observation_mode="none",
        verified_success=None, retry_observed=None, escalation_model=None, attempts=None,
        latency_seconds=None, cost_usd=None,
    )
    base.update(kw)
    return RoutingOutcome(**base)


class TestEvaluation:
    def test_empty_evidence_gives_no_numbers(self):
        s = summarize_outcomes([_outcome(), _outcome()])
        assert s.outcomes == 2
        assert s.verified_success_rate is None
        assert s.cost_per_verified_success is None
        assert s.mean_latency_seconds is None
        assert s.mean_attempts is None
        assert s.shadow_agreement_rate is None

    def test_cost_per_verified_success_counts_failed_spend(self):
        s = summarize_outcomes([
            _outcome(verified_success=True, cost_usd=0.10, attempts=1),
            _outcome(verified_success=False, cost_usd=0.30, attempts=1),
            _outcome(verified_success=None, cost_usd=5.0),  # unverified: excluded
        ])
        assert s.verified_success_rate == 0.5
        assert s.cost_per_verified_success == pytest.approx(0.40)
        assert s.mean_attempts == 1.0

    def test_unknown_cost_on_a_verified_outcome_withholds_cost_metric(self):
        s = summarize_outcomes([
            _outcome(verified_success=True, cost_usd=0.10),
            _outcome(verified_success=False, cost_usd=None),
        ])
        assert s.cost_per_verified_success is None
        assert s.cost_known == 1

    def test_policies_compared_and_errors_reported(self):
        class Broken:
            name, version = "broken", "0"

            def decide(self, context, candidates, *, class_pins=None):
                d = BASELINE_POLICY.decide(context, candidates)
                return replace(d, selected_model="acme/old-1", policy_name=self.name)

        cs = _cands()
        results = evaluate_decisions(REPRESENTATIVE_SCENARIOS[:2], cs, [BASELINE_POLICY, Broken()])
        assert [r.policy for r in results] == [
            "deterministic_baseline@1", "broken@0", "deterministic_baseline@1", "broken@0",
        ]
        assert all(r.violations == (VIOLATION_POLICY_ERROR,) for r in results if r.policy == "broken@0")
        with pytest.raises(ValueError):
            decide_route(RoutingContext(), cs, policy=Broken())


# ---------------------------------------------------------------------------
# Run-path integration (shadow) and Receipt recording
# ---------------------------------------------------------------------------


class TestRunIntegration:
    def test_plan_route_uses_catalog_and_availability(self):
        d = plan_route(RoutingContext(task_category="standard"),
                       catalog=_catalog(), availability=OPENROUTER)
        assert isinstance(d, RoutingDecision)
        assert d.selected_model == "acme/mid-1"

    def test_shadow_decision_never_raises(self):
        with patch("openshard.routing.adaptive.runtime.plan_route", side_effect=RuntimeError):
            assert shadow_decision_for_run(
                task_category="standard", read_only=False, write_requested=True, risk="low",
                verification_available=True, verification_requested=True, harness="native",
            ) is None

    def test_log_run_records_provenance_and_legacy_fields(self, tmp_path):
        from openshard.routing.engine import RoutingDecision as LegacyDecision
        from openshard.run._pipeline_helpers import _log_run

        legacy = LegacyDecision(model="acme/mid-1", category="standard", rationale="standard")
        legacy.adaptive = _decide()
        gen = MagicMock()
        gen.model = "acme/mid-1"
        gen.fixer_model = "acme/frontier-1"
        with patch("openshard.run._pipeline_helpers.Path.cwd", return_value=tmp_path):
            _log_run(start=time.time(), task="t", generator=gen, retry_triggered=False,
                     files=[], verification_attempted=False, verification_passed=None,
                     workspace=None, routing_decision=legacy)
        line = (tmp_path / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        entry = json.loads(line)
        assert entry["routing_model"] == "acme/mid-1"  # legacy fields unchanged
        prov = entry["routing_provenance"]
        assert prov["selected_model"] == "acme/mid-1"
        assert prov["executed_model"] == "acme/mid-1"
        assert prov["agrees_with_execution"] is True
        assert outcome_from_receipt(entry).decision_fingerprint == legacy.adaptive.decision_fingerprint

    def test_pipeline_attaches_shadow_decision_without_changing_model(self):
        from click.testing import CliRunner

        from openshard.cli.main import cli
        from openshard.providers.base import ModelInfo
        from openshard.providers.manager import InventoryEntry

        inv_entry = InventoryEntry(provider="openrouter", model=ModelInfo(
            id="openrouter/fast-model", name="fast", pricing={"prompt": "0.0000005"},
            context_window=None, max_output_tokens=None, supports_vision=False,
            supports_tools=False,
        ))
        manager = MagicMock()
        manager.get_inventory.return_value = MagicMock(models=[inv_entry])
        manager.providers = {"openrouter": MagicMock()}
        result = MagicMock(usage=None, files=[], summary="done", notes=[])
        generator = MagicMock(model="mock-default-model", fixer_model="mock-fixer-model")
        generator.generate.return_value = result
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value={"approval_mode": "smart"}), \
             patch("openshard.run.pipeline._log_run") as mock_log:
            CliRunner().invoke(cli, ["run", "add a simple email validation helper"])

        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        legacy = kwargs["routing_decision"]
        assert isinstance(legacy.adaptive, RoutingDecision)
        assert legacy.adaptive.context.task_category == "boilerplate"
        assert legacy.adaptive.context.harness is not None
        # Shadow only: the executed model is still legacy scored routing's choice.
        assert kwargs["model"] == "openrouter/fast-model"

    def test_log_run_without_adaptive_has_no_provenance(self, tmp_path):
        from openshard.routing.engine import RoutingDecision as LegacyDecision
        from openshard.run._pipeline_helpers import _log_run

        gen = MagicMock()
        gen.model = "acme/mid-1"
        gen.fixer_model = "acme/frontier-1"
        with patch("openshard.run._pipeline_helpers.Path.cwd", return_value=tmp_path):
            _log_run(start=time.time(), task="t", generator=gen, retry_triggered=False,
                     files=[], verification_attempted=False, verification_passed=None,
                     workspace=None,
                     routing_decision=LegacyDecision("acme/mid-1", "standard", "standard"))
        entry = json.loads(Path(tmp_path / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])
        assert "routing_provenance" not in entry
