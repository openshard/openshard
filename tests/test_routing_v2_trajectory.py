"""Routing V2: trajectory-aware, deterministic, capability-gated routing for OSN.

Covers the V2 policy contract (step and trajectory context change routing;
an observed failure can change the recovery decision; unknown verification
is never success; a cost limit stops routing and recovery; dogfood
candidates compete only when enabled; explicit models, blocked providers and
required capabilities are enforced; history is used only when meaningful;
equal inputs reproduce), the supervisor's repair-step re-route inside the
recovery envelope, the run-level capability snapshot (a toggle applies to
the next run despite a fresh cache, one snapshot per run, off keeps the
stable behaviour, offline fails closed), and the Receipt evidence.
"""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.models.catalog import ModelEntry, build_catalog
from openshard.models.promotion import STATE_DOGFOOD_CANDIDATE, STATE_STABLE, STATE_VALIDATED
from openshard.osn.routing import resolve_osn_routing
from openshard.osn.supervisor import REASON_TRAJECTORY_REROUTE, RECORD_APPLIED, RecoverySupervisor
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.routing.adaptive import (
    TRAJECTORY_POLICY,
    RoutingContext,
    RoutingDecision,
    TrajectoryPolicyV2,
    build_candidate_set,
    decide_route,
    routing_context_for_run,
)
from openshard.routing.adaptive.decision import MODE_EXPLICIT, MODE_NONE, MODE_ROUTED
from openshard.routing.adaptive.history_evidence import (
    MIN_VERIFIED_SAMPLES,
    HistoryEvidence,
    ModelHistory,
    build_history_evidence,
)
from openshard.routing.adaptive.outcome import RoutingOutcome
from openshard.routing.adaptive.policy_v2 import (
    R_BUDGET_EXHAUSTED,
    R_DOGFOOD,
    R_ESCALATED,
    R_FAILURE_NOT_OBSERVED,
    R_HISTORY_USED,
    R_NO_CANDIDATE,
    R_SPEND_UNKNOWN,
)
from openshard.routing.adaptive.recovery import RecoveryStep, build_recovery_plan
from openshard.routing.adaptive.step_types import (
    STEP_EXECUTE,
    STEP_REPAIR,
    STEP_TYPES,
    step_type_for_attempt,
)
from openshard.routing.engine import route
from openshard.routing.model_policy import ModelPolicyConfig
from openshard.routing.provider_availability import ProviderAvailability
from openshard.sync import capabilities as caps
from openshard.sync import config as sync_config

PY = sys.executable
SYNCED_AT = "2026-09-24T00:00:00Z"
OPENROUTER = ProviderAvailability(("openrouter",), True, False, False)
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
TASK = "write ok into out.txt"


def _m(mid: str, **kw) -> ModelEntry:
    defaults = dict(
        display_name=mid, provider=mid.split("/")[0], tier="mid", cost_class="mid",
        supports_tools=True, context_length=200_000, lifecycle="active_default",
    )
    defaults.update(kw)
    return ModelEntry(id=mid, **defaults)


def _raw(mid: str, *, created: int = 1780000000, out_price: str = "0.000002", inputs=("text",),
         params=("tools", "structured_outputs", "reasoning"), context: int = 1_000_000) -> dict:
    return {
        "id": mid, "name": mid, "created": created, "context_length": context,
        "architecture": {"input_modalities": list(inputs), "output_modalities": ["text"]},
        "pricing": {"prompt": "0.000001", "completion": out_price},
        "supported_parameters": list(params),
    }


CURATED = [
    _m("acme/cheap-1", roles=("cheap_control",)),
    _m("acme/mid-1", roles=("routine_engineering", "standard_coding")),
    _m("zeta/mid-2", roles=("routine_engineering",)),
    _m("acme/frontier-1", supports_reasoning=True, roles=("escalation",), lifecycle="active_specialist"),
    _m("zeta/frontier-2", supports_reasoning=True, roles=("escalation",), lifecycle="active_specialist"),
    _m("acme/vision-1", input_modalities=("text", "image"), roles=("visual",), lifecycle="active_specialist"),
]
DISCOVERED = [
    _raw("acme/cheap-1", out_price="0.0000003"),
    _raw("acme/mid-1", out_price="0.000002"),
    _raw("zeta/mid-2", out_price="0.0000025"),
    _raw("acme/frontier-1", out_price="0.00002"),
    _raw("zeta/frontier-2", out_price="0.00003"),
    _raw("acme/vision-1", out_price="0.000003", inputs=("text", "image")),
    # A newly released, uncurated model: shadow candidate, dogfood on request.
    _raw("acme/new-9", created=1790000000, out_price="0.000001"),
]
NEW = "acme/new-9"
CATALOG = build_catalog(CURATED, DISCOVERED, synced_at=SYNCED_AT)


def _cands(policy=None, *, catalog=CATALOG, availability=OPENROUTER, **kw):
    return build_candidate_set(catalog, availability, policy=policy, **kw)


def _ctx(**kw) -> RoutingContext:
    base = dict(task_category="standard", write_requested=True, verification_available=True,
                verification_requested=True, harness="osn_loop", step_type=STEP_EXECUTE, attempt=1)
    base.update(kw)
    return routing_context_for_run(**base)


def _repair(models_tried, *, status="failed", source="directly_observed", **kw) -> RoutingContext:
    return _ctx(step_type=STEP_REPAIR, attempt=len(models_tried) + 1, models_tried=tuple(models_tried),
                last_verification_status=status, last_verification_source=source, **kw)


def _decide(ctx, cands=None, *, policy=None, pins=None) -> RoutingDecision:
    return decide_route(ctx, cands or _cands(), policy=policy or TRAJECTORY_POLICY, class_pins=pins)


# ---------------------------------------------------------------------------
# Step vocabulary
# ---------------------------------------------------------------------------


class TestStepTypes:
    def test_only_boundaries_osn_has(self):
        assert STEP_TYPES == {"execute", "repair"}
        assert step_type_for_attempt(1) == STEP_EXECUTE and step_type_for_attempt(2) == STEP_REPAIR

    def test_unknown_step_or_failure_class_is_dropped_not_guessed(self):
        c = routing_context_for_run(task_category="standard", step_type="plan", previous_failure_class="lint")
        assert c.step_type is None and c.previous_failure_class is None
        assert c.to_dict()["version"] == 2


# ---------------------------------------------------------------------------
# Policy V2
# ---------------------------------------------------------------------------


class TestTrajectoryPolicy:
    def test_execute_step_selects_and_explains(self):
        d = _decide(_ctx())
        assert d.selected_model == "acme/mid-1" and d.resolved_class == "routine_coding"
        assert d.selection_mode == MODE_ROUTED and d.step_type == STEP_EXECUTE
        assert d.selected_promotion_state == STATE_STABLE
        assert d.policy_name == "deterministic_trajectory_v2"
        assert d.ranking[0]["model"] == "acme/mid-1" and "components" in d.ranking[0]
        assert d.history_evidence["used"] is False
        assert NEW in d.shadow_candidates
        assert d.recovery.enabled and [s.model_id for s in d.recovery.steps] == ["acme/frontier-1", "zeta/frontier-2"]

    def test_repair_after_observed_failure_escalates_and_never_retries_the_tried_model(self):
        d = _decide(_repair(["acme/mid-1"]))
        assert d.selected_model == "acme/frontier-1" and d.resolved_class == "deep_reasoning"
        assert R_ESCALATED in d.reasons and d.step_type == STEP_REPAIR
        d3 = _decide(_repair(["acme/mid-1", "acme/frontier-1"]))
        assert d3.selected_model == "zeta/frontier-2"
        d4 = _decide(_repair(["acme/mid-1", "acme/frontier-1", "zeta/frontier-2"]))
        assert d4.selected_model is None and R_NO_CANDIDATE in d4.reasons
        assert ("acme/frontier-1", "already_tried") not in d4.recovery.steps  # nothing promised

    @pytest.mark.parametrize("status,source", [
        ("unknown", None), ("not_run", None), ("passed", "directly_observed"),
        ("failed", "agent_reported"), ("failed", None),
    ])
    def test_unknown_or_unobserved_verification_is_never_a_reason_to_escalate(self, status, source):
        d = _decide(_repair(["acme/mid-1"], status=status, source=source))
        assert d.selected_model is None and d.selection_mode == MODE_NONE
        assert R_FAILURE_NOT_OBSERVED in d.reasons

    def test_cost_limit_stops_routing(self):
        d = _decide(_repair(["acme/mid-1"], accumulated_cost_usd=0.5, cost_budget_usd=0.5))
        assert d.selected_model is None and R_BUDGET_EXHAUSTED in d.reasons
        d = _decide(_repair(["acme/mid-1"], accumulated_cost_usd=None, cost_budget_usd=0.5))
        assert d.selected_model is None and R_SPEND_UNKNOWN in d.reasons
        # Nothing spent yet on the first step: the cap does not block the run.
        d = _decide(_ctx(cost_budget_usd=0.5))
        assert d.selected_model == "acme/mid-1"
        d = _decide(_ctx(cost_budget_usd=0.5, accumulated_cost_usd=0.6))
        assert d.selected_model is None and R_BUDGET_EXHAUSTED in d.reasons

    def test_explicit_model_always_wins_when_eligible_and_is_never_substituted(self):
        ctx = _ctx(explicit_model="zeta/mid-2")
        d = _decide(ctx, _cands(explicit_model="zeta/mid-2"))
        assert d.selected_model == "zeta/mid-2" and d.selection_mode == MODE_EXPLICIT
        blocked = ModelPolicyConfig(blocked_models=frozenset({"zeta/mid-2"}))
        d = _decide(ctx, _cands(blocked, explicit_model="zeta/mid-2"))
        assert d.selected_model is None and d.rejected_explicit_reason == "policy:blocked_model"

    def test_blocked_provider_is_never_selected(self):
        policy = ModelPolicyConfig(blocked_providers=frozenset({"acme"}))
        d = _decide(_ctx(), _cands(policy))
        assert d.selected_model == "zeta/mid-2"
        assert "acme/mid-1" not in d.considered
        d = _decide(_repair(["zeta/mid-2"]), _cands(policy))
        assert d.selected_model == "zeta/frontier-2"

    def test_required_capability_is_enforced(self):
        d = _decide(_ctx(required_capabilities=frozenset({"vision"})),
                    _cands(required_capabilities=frozenset({"vision"})))
        assert d.selected_model == "acme/vision-1" and d.resolved_class == "vision"
        text_only = build_catalog([m for m in CURATED if m.id != "acme/vision-1"],
                                  [r for r in DISCOVERED if r["id"] != "acme/vision-1"], synced_at=SYNCED_AT)
        d = _decide(_ctx(required_capabilities=frozenset({"vision"})),
                    _cands(catalog=text_only, required_capabilities=frozenset({"vision"})))
        assert d.selected_model is None and R_NO_CANDIDATE in d.reasons

    def test_dogfood_candidate_competes_only_when_enabled_and_named_for_the_class(self):
        pol = TrajectoryPolicyV2(dogfood={"routine_coding": frozenset({NEW})})
        # Public run: the candidate set never admits it.
        d = _decide(_ctx(dogfood_enabled=False), policy=pol)
        assert d.selected_model == "acme/mid-1" and NEW in d.shadow_candidates
        # Dogfood run: admitted and preferred for its class, recorded as such.
        d = _decide(_ctx(dogfood_enabled=True), _cands(dogfood_ids=[NEW]), policy=pol)
        assert d.selected_model == NEW and R_DOGFOOD in d.reasons
        assert d.selected_promotion_state == STATE_DOGFOOD_CANDIDATE
        assert NEW not in d.shadow_candidates
        # Named for another class: not preferred here.
        other = TrajectoryPolicyV2(dogfood={"deep_reasoning": frozenset({NEW})})
        d = _decide(_ctx(dogfood_enabled=True), _cands(dogfood_ids=[NEW]), policy=other)
        assert d.selected_model == "acme/mid-1"
        # After it fails an observed verification, repair moves on without it.
        d = _decide(_repair([NEW], dogfood_enabled=True), _cands(dogfood_ids=[NEW]), policy=pol)
        assert d.selected_model == "acme/frontier-1" and d.selected_promotion_state == STATE_VALIDATED

    def test_pins_apply_by_legacy_or_requirement_name(self):
        assert _decide(_ctx(), pins={"balanced_coding": "zeta/mid-2"}).selected_model == "zeta/mid-2"
        assert _decide(_ctx(), pins={"routine_coding": "zeta/mid-2"}).selected_model == "zeta/mid-2"
        d = _decide(_repair(["zeta/mid-2"]), pins={"routine_coding": "zeta/mid-2"})
        assert d.selected_model == "acme/frontier-1"  # a tried pin is not retried

    def test_history_is_used_only_when_meaningful_and_prefers_cost_per_verified_success(self):
        def hist(model, verified, successes, cost):
            return ModelHistory(model, "osn_loop", verified, successes, verified, cost)

        thin = HistoryEvidence({"acme/mid-1": hist("acme/mid-1", MIN_VERIFIED_SAMPLES - 1, 1, 0.01)}, "osn_loop", 4)
        d = _decide(_ctx(), policy=TrajectoryPolicyV2(history=thin))
        assert d.history_evidence["used"] is False and d.history_evidence["reason"] == "insufficient_observed_data"
        lonely = HistoryEvidence({"acme/mid-1": hist("acme/mid-1", 8, 8, 0.08)}, "osn_loop", 8)
        d = _decide(_ctx(), policy=TrajectoryPolicyV2(history=lonely))
        assert d.history_evidence["used"] is False
        assert d.history_evidence["reason"] == "too_few_candidates_with_evidence"
        enough = HistoryEvidence({
            "acme/mid-1": hist("acme/mid-1", 8, 8, 0.08),   # $0.010 per verified success
            "zeta/mid-2": hist("zeta/mid-2", 6, 6, 0.03),   # $0.005 per verified success
        }, "osn_loop", 14)
        d = _decide(_ctx(), policy=TrajectoryPolicyV2(history=enough))
        assert d.selected_model == "zeta/mid-2" and R_HISTORY_USED in d.reasons
        assert d.history_evidence["used"] is True and d.ranking[0]["components"]["history"] == "used"
        failing = HistoryEvidence({
            "acme/mid-1": hist("acme/mid-1", 8, 0, 0.08),
            "zeta/mid-2": hist("zeta/mid-2", 6, 6, 0.03),
        }, "osn_loop", 14)
        d = _decide(_ctx(), policy=TrajectoryPolicyV2(history=failing))
        assert d.selected_model == "zeta/mid-2"
        assert [r for r in d.ranking if r["model"] == "acme/mid-1"][0]["components"]["history"] == "observed_failures_only"

    def test_history_from_receipts_counts_only_observed_verification(self):
        def o(model, verified_success, cost=0.01):
            return RoutingOutcome(
                receipt_id=None, decision_fingerprint=None, policy_name=None, routed_model=model,
                final_model=model, routing_class=None, harness="osn_loop", verification_status="x",
                verification_source=None, verification_observation_mode="x",
                verified_success=verified_success, retry_observed=False, escalation_model=None,
                attempts=1, latency_seconds=None, cost_usd=cost,
            )

        ev = build_history_evidence([o("a/x", True), o("a/x", False), o("a/x", None), o("a/x", True, None)])
        h = ev.per_model["a/x"]
        assert (h.verified, h.successes, h.cost_known) == (3, 2, 2)
        assert h.as_evidence().cost_per_verified_success is None  # a verified outcome lacks cost
        assert build_history_evidence([o("a/x", True)], harness="native").per_model == {}

    def test_equal_inputs_reproduce(self):
        a, b = _decide(_repair(["acme/mid-1"])), _decide(_repair(["acme/mid-1"]))
        assert a.decision_fingerprint == b.decision_fingerprint
        assert a.to_provenance() == b.to_provenance()
        c = _decide(_repair(["acme/mid-1"], accumulated_cost_usd=0.1, cost_budget_usd=1.0))
        assert c.selected_model == a.selected_model and c.decision_fingerprint != a.decision_fingerprint

    def test_provenance_answers_the_questions_and_is_bounded(self):
        prov = _decide(_ctx()).to_provenance(record_mode="applied", executed_model="acme/mid-1")
        assert prov["step_type"] == "execute" and prov["record_mode"] == "applied"
        assert prov["eligible_count"] == 6 and prov["rejected_counts"]["eligibility:not_promoted"] == 1
        assert prov["selected_model"] == "acme/mid-1" and prov["policy"]["name"] == "deterministic_trajectory_v2"
        assert prov["history_evidence"]["used"] is False
        assert len(prov["ranking"]) <= 5 and len(prov["shadow_candidates"]) <= 3
        assert prov["selected_promotion_state"] == "stable"
        assert prov["score"] is None and prov["confidence"] is None
        assert TASK not in json.dumps(prov)

    def test_v1_context_still_routes_under_v2(self):
        d = _decide(RoutingContext(task_category="standard"))
        assert d.selected_model == "acme/mid-1" and d.step_type is None


# ---------------------------------------------------------------------------
# Supervisor re-route
# ---------------------------------------------------------------------------


def _stub_decision(selected: str | None, *reasons: str) -> RoutingDecision:
    return RoutingDecision(
        selected_model=selected, selection_mode=MODE_ROUTED if selected else MODE_NONE,
        requested_class="deep_reasoning", resolved_class="deep_reasoning" if selected else None,
        policy_name="deterministic_trajectory_v2", policy_version="1", reasons=reasons or ("step_repair",),
        considered=(selected,) if selected else (), step_type=STEP_REPAIR,
        selected_promotion_state="validated" if selected else None,
    )


class TestSupervisorReroute:
    def _sup(self, reroute, usage=("acme/mid-1", 0.01), cap=None):
        plan = build_recovery_plan(
            [RecoveryStep("deep_reasoning", "acme/frontier-1"), RecoveryStep("deep_reasoning", "zeta/frontier-2")],
            verification_available=True, verification_requested=True,
        )
        return RecoverySupervisor(
            plan=plan, usage_for=lambda n: usage, record_mode=RECORD_APPLIED, not_acted_reason=None,
            cost_budget_usd=cap, first_model="acme/mid-1", first_class="routine_coding", reroute=reroute,
        )

    def test_reroute_can_change_the_next_model_and_is_recorded(self):
        seen = {}

        def reroute(**kw):
            seen.update(kw)
            return _stub_decision("zeta/frontier-2", "step_repair", "escalated_after_observed_failure")

        d = self._sup(reroute).after_failed_attempt(1, verification_observed=True, loop_max_attempts=3)
        assert d.action == "escalate" and d.recommended_model == "zeta/frontier-2"
        assert d.reason == REASON_TRAJECTORY_REROUTE
        assert d.evidence["reroute"]["changed_from_plan"] is True
        assert d.evidence["reroute"]["policy"]["name"] == "deterministic_trajectory_v2"
        assert seen["attempt"] == 2 and seen["models_tried"] == ["acme/mid-1"]
        assert seen["last_verification_status"] == "failed" and seen["accumulated_cost_usd"] == 0.01

    def test_reroute_that_finds_nothing_stops_the_run_with_its_reason(self):
        d = self._sup(lambda **kw: _stub_decision(None, "step_repair", "cost_budget_exhausted")) \
            .after_failed_attempt(1, verification_observed=True, loop_max_attempts=3)
        assert d.action == "stop" and d.reason == "cost_budget_exhausted"
        assert d.evidence["reroute"]["selected_model"] is None

    def test_reroute_never_reruns_a_tried_model(self):
        d = self._sup(lambda **kw: _stub_decision("acme/mid-1")) \
            .after_failed_attempt(1, verification_observed=True, loop_max_attempts=3)
        assert d.action == "stop"

    def test_envelope_runs_first(self):
        calls = []

        def reroute(**kw):
            calls.append(kw)
            return _stub_decision("zeta/frontier-2")

        # Unobserved failure: the recovery policy stops; the re-route is never asked.
        d = self._sup(reroute).after_failed_attempt(1, verification_observed=False, loop_max_attempts=3)
        assert d.action == "stop" and calls == []
        # Spend at the cap: same.
        d = self._sup(reroute, cap=0.01).after_failed_attempt(1, verification_observed=True, loop_max_attempts=3)
        assert d.action == "stop" and d.reason == "cost_budget_exhausted" and calls == []

    def test_reroute_failure_keeps_the_fixed_plan(self):
        def boom(**kw):
            raise RuntimeError("no")

        d = self._sup(boom).after_failed_attempt(1, verification_observed=True, loop_max_attempts=3)
        assert d.action == "escalate" and d.recommended_model == "acme/frontier-1"
        assert "reroute" not in d.evidence


# ---------------------------------------------------------------------------
# OSN routing entry point
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog():
    with patch("openshard.models.catalog.load_catalog", return_value=CATALOG), \
         patch("openshard.routing.provider_availability.detect_provider_availability", return_value=OPENROUTER):
        yield


class TestOsnRoutingV2:
    def _resolve(self, *, policy_loader=None, history_loader=None, enabled=True, cost_budget_usd=None):
        return resolve_osn_routing(
            TASK, explicit_model=None, escalate=[], capability_enabled=lambda: enabled,
            legacy_model=lambda t: route(t).model, model_policy_loader=policy_loader,
            max_attempts=3, cost_budget_usd=cost_budget_usd, history_loader=history_loader,
        )

    def test_dogfood_candidate_from_config_is_applied(self, catalog):
        policy = ModelPolicyConfig(dogfood_candidates=(("routine_coding", NEW),))
        r = self._resolve(policy_loader=lambda: policy)
        assert r.applied and r.first_model == NEW
        assert r.record["promotion_state"] == "dogfood_candidate" and "dogfood_candidate_selected" in r.record["reasons"]
        assert r.ladder == ["acme/frontier-1", "zeta/frontier-2"]

    def test_history_loader_is_consulted_only_on_the_applied_path(self, catalog):
        loads = {"n": 0}

        def loader():
            loads["n"] += 1
            return HistoryEvidence()

        r = self._resolve(history_loader=loader)
        assert r.applied and loads["n"] == 1 and r.record["history"]["reason"] == "no_history"
        self._resolve(history_loader=loader, enabled=False)
        assert loads["n"] == 1

    def test_reroute_decides_the_repair_step_over_the_same_pool(self, catalog):
        r = self._resolve(cost_budget_usd=1.0)
        d = r.reroute(attempt=2, models_tried=[r.first_model], last_verification_status="failed",
                      last_verification_source="directly_observed", accumulated_cost_usd=0.02)
        assert d is not None and d.selected_model == "acme/frontier-1" and d.step_type == STEP_REPAIR
        assert d.context.cost_budget_usd == 1.0 and d.context.accumulated_cost_usd == 0.02
        stop = r.reroute(attempt=2, models_tried=[r.first_model], last_verification_status="failed",
                         last_verification_source="directly_observed", accumulated_cost_usd=1.0)
        assert stop is not None and stop.selected_model is None and R_BUDGET_EXHAUSTED in stop.reasons
        r.fall_back("provider_mismatch", route(TASK).model)
        assert r.reroute(attempt=2, models_tried=[], last_verification_status="failed",
                         last_verification_source="directly_observed", accumulated_cost_usd=0.0) is None

    def test_capability_off_keeps_the_v1_shadow_record_and_never_admits_dogfood(self, catalog):
        policy = ModelPolicyConfig(dogfood_candidates=(("routine_coding", NEW),))
        r = self._resolve(policy_loader=lambda: policy, enabled=False)
        assert not r.applied and r.first_model == route(TASK).model and r.record is None
        assert r.decision.policy_name == "deterministic_baseline"
        assert r.reroute(attempt=2, models_tried=[], last_verification_status="failed",
                         last_verification_source="directly_observed", accumulated_cost_usd=0.0) is None


# ---------------------------------------------------------------------------
# Run-level capability snapshot
# ---------------------------------------------------------------------------


def _env(tmp_path) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), sync_config.ENDPOINT_ENV: "https://platform.example.test",
            sync_config.ORG_ENV: ORG, sync_config.API_KEY_ENV: KEY}


def _link() -> sync_config.PlatformLink:
    return sync_config.PlatformLink(endpoint="https://platform.example.test", organisation_id=ORG,
                                    api_key=KEY, linked_at=None, source="env")


class TestCapabilitySnapshot:
    def test_a_toggle_applies_to_the_next_run_despite_a_fresh_cache(self, tmp_path):
        env = _env(tmp_path)
        now = 1_800_000_000.0
        caps._write_cache(caps.cache_path(env), _link(), frozenset(), now=now)  # fresh: everything off
        with patch("openshard.sync.capabilities.time.time", return_value=now + 5):
            cached = caps.LazyCapabilities(env, fetcher=lambda link: frozenset({"adaptive_routing"}))
            assert cached.enabled("adaptive_routing") is False  # an ordinary command trusts the cache
            run = caps.LazyCapabilities(env, refresh=True, fetcher=lambda link: frozenset({"adaptive_routing"}))
            assert run.enabled("adaptive_routing") is True  # a new run reads the Platform once
            assert run.state.source == caps.SOURCE_FRESH
        rec = run.to_record()
        assert rec["refreshed_at_run_start"] is True and rec["enabled"]["adaptive_routing"] is True
        assert set(rec["enabled"]) == {"agent_budgets", "adaptive_routing", "supervisor_routing"}

    def test_one_snapshot_is_used_throughout_a_run(self, tmp_path):
        env = _env(tmp_path)
        answers = [frozenset({"adaptive_routing", "supervisor_routing"}), frozenset()]
        calls = []

        def fetcher(link):
            calls.append(1)
            return answers.pop(0)

        run = caps.LazyCapabilities(env, refresh=True, fetcher=fetcher)
        assert run.enabled("adaptive_routing") and run.enabled("supervisor_routing")
        assert not run.enabled("agent_budgets")
        # The Platform flips mid-run; this run does not notice.
        assert run.enabled("adaptive_routing") and run.enabled("supervisor_routing")
        assert calls == [1]

    def test_nothing_is_read_until_a_feature_asks(self, tmp_path):
        run = caps.LazyCapabilities(_env(tmp_path), refresh=True, fetcher=lambda link: frozenset({"x"}))
        assert run.to_record() is None and not run.looked_up

    def test_offline_fails_closed_and_the_negative_cache_is_honoured(self, tmp_path):
        env = _env(tmp_path)
        calls = []

        def down(link):
            calls.append(1)
            return None

        now = 1_800_000_000.0
        with patch("openshard.sync.capabilities.time.time", return_value=now):
            first = caps.LazyCapabilities(env, refresh=True, fetcher=down)
            assert first.enabled("adaptive_routing") is False and first.state.source == caps.SOURCE_UNAVAILABLE
        with patch("openshard.sync.capabilities.time.time", return_value=now + 10):
            second = caps.LazyCapabilities(env, refresh=True, fetcher=down)
            assert second.enabled("adaptive_routing") is False
        assert calls == [1]  # the remembered failure spared a second request
        assert first.to_record()["reason"] == caps.REASON_UNAVAILABLE

    def test_no_link_means_off_without_a_request(self, tmp_path):
        run = caps.LazyCapabilities({"OPENSHARD_HOME": str(tmp_path)}, refresh=True,
                                    fetcher=lambda link: pytest.fail("must not fetch"))
        assert run.enabled("adaptive_routing") is False
        assert run.to_record()["reason"] == caps.REASON_NO_LINK


# ---------------------------------------------------------------------------
# `openshard osn run` end to end
# ---------------------------------------------------------------------------


class FakeProvider(BaseProvider):
    def __init__(self, replies, cost=0.001):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.cost = cost

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append((model, prompt))
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, self.cost))


def _writes(*pairs):
    return json.dumps({"writes": [{"path": p, "content": c} for p, c in pairs]})


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append(self.path)
        status, body = type(self).routes.get(self.path, (404, b'{"error":{"code":"not_found"}}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _caps_body(org, keys):
    return json.dumps({"organisation_id": org, "capabilities": [
        {"key": k, "name": k, "description": "", "stage": "internal", "enabled": True, "enabled_at": "x"} for k in keys
    ]}).encode()


@pytest.fixture
def platform():
    _Handler.routes, _Handler.seen = {}, []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cli_repo(tmp_path, monkeypatch, platform, keys):
    repo = tmp_path / "repo"
    (repo / ".openshard").mkdir(parents=True)
    (repo / "out.txt").write_text("bad")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    monkeypatch.setenv(sync_config.ENDPOINT_ENV, f"http://127.0.0.1:{platform.server_address[1]}")
    monkeypatch.setenv(sync_config.ORG_ENV, ORG)
    monkeypatch.setenv(sync_config.API_KEY_ENV, KEY)
    _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, keys))
    return repo


def _invoke(monkeypatch, fp, *extra, max_attempts="3"):
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("openrouter", fp))
    return CliRunner().invoke(cli, [
        "osn", "run", TASK, "--verify-cmd",
        f'"{PY}" -c "import sys; c=open(\'out.txt\').read(); print(c); sys.exit(0 if c==\'ok\' else 1)"',
        "--max-attempts", max_attempts, "--json", *extra,
    ])


def _last_run(repo):
    return json.loads((repo / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])


class TestCli:
    def test_a_fresh_off_cache_does_not_hide_a_new_grant(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        link = sync_config.PlatformLink(
            endpoint=f"http://127.0.0.1:{platform.server_address[1]}", organisation_id=ORG,
            api_key=KEY, linked_at=None, source="env",
        )
        import time as _time

        caps._write_cache(caps.cache_path(None), link, frozenset(), now=_time.time())  # cache: all off
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["adaptive_routing"]["applied"] is True and body["models"][0] == "acme/mid-1"
        assert body["capability_snapshot"]["source"] == "fresh"
        assert _Handler.seen == [f"/v1/orgs/{ORG}/capabilities"]
        entry = _last_run(repo)
        assert entry["capability_snapshot"]["enabled"] == {
            "agent_budgets": False, "adaptive_routing": True, "supervisor_routing": False,
        }

    def test_one_capability_read_serves_routing_budget_and_supervisor(self, tmp_path, monkeypatch, platform, catalog):
        import yaml

        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing", "supervisor_routing", "agent_budgets"])
        (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"agent_budgets": {"max_spend_usd": 1.0}}))
        fp = FakeProvider([_writes(("out.txt", "no")), _writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "verified"
        assert [m for m, _ in fp.calls] == ["acme/mid-1", "acme/frontier-1", "zeta/frontier-2"]
        assert _Handler.seen == [f"/v1/orgs/{ORG}/capabilities"]  # one read for the whole run
        snap = body["capability_snapshot"]["enabled"]
        assert snap == {"agent_budgets": True, "adaptive_routing": True, "supervisor_routing": True}
        sup = body["supervisor_routing"]
        assert sup["record_mode"] == "applied" and sup["evidence"]["history"] == "via_routing_v2_evidence_gate"
        first, second = sup["decisions"]
        assert first["action"] == "escalate" and first["recommended_model"] == "acme/frontier-1"
        assert first["evidence"]["reroute"]["resolved_class"] == "deep_reasoning"
        assert first["evidence"]["reroute"]["changed_from_plan"] is False and first["acted_on"] is True
        assert second["recommended_model"] == "zeta/frontier-2" and second["acted_on"] is True

        entry = _last_run(repo)
        receipt = build_shard_receipt(entry, index=0)
        ev = receipt.recorded_evidence
        assert ev["adaptive_routing"]["policy"] == {"name": "deterministic_trajectory_v2", "version": "1"}
        assert ev["adaptive_routing"]["step_type"] == "execute" and ev["adaptive_routing"]["promotion_state"] == "stable"
        assert ev["adaptive_routing"]["shadow_candidates"] == [NEW]
        assert ev["adaptive_routing"]["history"]["used"] is False
        assert ev["supervisor_routing"]["decisions"][0]["evidence"]["reroute"]["selected_model"] == "acme/frontier-1"
        assert ev["capability_snapshot"]["enabled"]["supervisor_routing"] is True
        text = render_full_shard_receipt(receipt)
        assert "deterministic_trajectory_v2@1, step execute" in text
        assert "CAPABILITIES" in text and "adaptive_routing, agent_budgets, supervisor_routing" in text
        wire = receipt_to_dict(receipt, extended=True)
        assert not {"adaptive_routing", "supervisor_routing", "capability_snapshot"} & set(wire)

    def test_capability_off_keeps_the_stable_behaviour(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, [])
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["models"] == [route(TASK).model] and "adaptive_routing" not in body
        assert body["capability_snapshot"]["enabled"]["adaptive_routing"] is False
        entry = _last_run(repo)
        assert entry["routing_provenance"]["policy"]["name"] == "deterministic_baseline"

    def test_offline_after_snapshot_needs_no_network(self, tmp_path, monkeypatch, platform, catalog):
        _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        with patch("openshard.models.openrouter_fetcher.fetch_openrouter_models",
                   side_effect=AssertionError("the run path must not fetch the model list")):
            r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        assert json.loads(r.stdout)["adaptive_routing"]["applied"] is True
        assert _Handler.seen == [f"/v1/orgs/{ORG}/capabilities"]


class TestDecisionCompat:
    def test_v1_decision_provenance_has_empty_v2_fields(self):
        d = RoutingDecision(selected_model="a/b", selection_mode="routed", requested_class="balanced_coding",
                            resolved_class="balanced_coding", policy_name="deterministic_baseline", policy_version="1")
        prov = d.to_provenance()
        assert prov["step_type"] is None and prov["ranking"] == [] and prov["history_evidence"] is None
        assert prov["shadow_candidates"] == [] and prov["selected_promotion_state"] is None
        assert replace(d, step_type="execute").to_provenance()["step_type"] == "execute"
