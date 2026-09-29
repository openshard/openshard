"""Routing around dynamic model candidates.

Product rules under test:

* a model's route into routing is a promotion state derived from facts and
  curation, never a hand-assigned tier;
* a retired or unlisted model is never a fresh-run default while a validated
  model can serve the same requirement;
* a newly discovered model appears in the catalog (and as a shadow candidate)
  without a Core release, but never becomes a public default on its own;
* a dogfood candidate named in config competes only when dogfood is enabled;
* explicit choices, blocked providers/models and hard requirements are
  enforced before any ranking;
* old Receipts that name the previous defaults stay readable;
* equal inputs give equal selections, offline.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.models.catalog import build_catalog, curated_catalog, load_catalog
from openshard.models.promotion import (
    PROMOTION_STATES,
    STATE_DISCOVERED,
    STATE_DOGFOOD_CANDIDATE,
    STATE_ELIGIBLE_FOR_SHADOW,
    STATE_RETIRED,
    STATE_STABLE,
    STATE_VALIDATED,
    promotion_state,
)
from openshard.models.registry import LEGACY_ADVISORY_FIELDS, ModelEntry, all_models
from openshard.routing.adaptive import build_candidate_set, outcome_from_receipt
from openshard.routing.adaptive.candidates import REASON_UNLISTED_ON_PROVIDER
from openshard.routing.model_policy import ModelPolicyConfig, model_policy_from_config
from openshard.routing.provider_availability import ProviderAvailability
from openshard.routing.requirements import (
    ESCALATION_TARGET,
    LEGACY_CLASS_TO_REQUIREMENT,
    REQUIREMENT_CLASSES,
    REQUIREMENT_NAMES,
    ObservedEvidence,
    rank_for_requirement,
    select_all_requirements,
    select_for_requirement,
    states_for,
)
from openshard.routing.routing_classes import ROUTING_CLASSES, select_all_classes

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_snapshot_2026-09-25.json"
OPENROUTER = ProviderAvailability(("openrouter",), True, False, False)
OLD_MAIN = "z-ai/glm-5.1"
OLD_CHEAP = "deepseek/deepseek-v4-flash"
NEW_GLM = "z-ai/glm-5.3"
NEW_FLASH = "deepseek/deepseek-v4.1-flash"


def _live_snapshot() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live_catalog():
    """The curated registry merged with a real (trimmed) OpenRouter list."""
    snap = _live_snapshot()
    return build_catalog(all_models(), snap["models"], synced_at=snap["synced_at"])


def _m(mid: str, **kw) -> ModelEntry:
    defaults = dict(
        display_name=mid, provider=mid.split("/")[0], tier="mid", cost_class="mid",
        supports_tools=True, context_length=200_000, lifecycle="active_default",
    )
    defaults.update(kw)
    return ModelEntry(id=mid, **defaults)


def _raw(mid: str, *, created: int, out_price: str = "0.000002", inputs=("text",),
         params=("tools", "structured_outputs", "reasoning"), context: int = 1_000_000) -> dict:
    return {
        "id": mid, "name": mid, "created": created, "context_length": context,
        "architecture": {"input_modalities": list(inputs), "output_modalities": ["text"]},
        "pricing": {"prompt": "0.000001", "completion": out_price},
        "supported_parameters": list(params),
    }


# ---------------------------------------------------------------------------
# Field categories
# ---------------------------------------------------------------------------


class TestFieldCategories:
    def test_legacy_advisory_fields_are_named_and_not_required_by_requirement_classes(self):
        assert set(LEGACY_ADVISORY_FIELDS) == {"tier", "roles", "latency_class", "experimental", "cost_class"}
        for cls in REQUIREMENT_CLASSES.values():
            # Hard requirements are capability facts and context only.
            assert cls.required_tags <= {"tools", "reasoning", "vision", "long_context", "structured_outputs"}

    def test_catalog_projection_labels_tier_as_legacy_and_carries_promotion_state(self, live_catalog):
        from openshard.models.catalog import catalog_entry_to_dict

        d = catalog_entry_to_dict(live_catalog.get(OLD_MAIN))
        assert d["promotion_state"] == STATE_STABLE
        assert d["tier"] == "mid"  # still shown; not a routing input for V2


# ---------------------------------------------------------------------------
# Promotion state
# ---------------------------------------------------------------------------


class TestPromotionState:
    def test_states_against_live_data(self, live_catalog):
        get = live_catalog.get
        assert promotion_state(get(OLD_MAIN)) == STATE_STABLE
        assert promotion_state(get("anthropic/claude-opus-4.8")) == STATE_VALIDATED
        assert promotion_state(get(NEW_GLM)) == STATE_ELIGIBLE_FOR_SHADOW
        assert promotion_state(get(NEW_GLM), dogfood=frozenset({NEW_GLM})) == STATE_DOGFOOD_CANDIDATE
        # Curated but never listed by the provider: retired on a fresh snapshot.
        assert promotion_state(get("minimax/m2.7")) == STATE_RETIRED
        assert promotion_state(get("anthropic/claude-opus-4.8-fast")) == STATE_RETIRED
        assert promotion_state(get("~anthropic/claude-haiku-latest")) == STATE_DISCOVERED

    def test_stale_snapshot_cannot_retire_a_model(self, live_catalog):
        e = live_catalog.get("anthropic/claude-opus-4.8-fast")
        assert e.status == "unlisted"
        assert promotion_state(e, snapshot_stale=True) != STATE_RETIRED

    def test_every_live_entry_has_a_known_state(self, live_catalog):
        for e in live_catalog.entries:
            assert promotion_state(e) in PROMOTION_STATES

    def test_retired_wins_over_dogfood_naming(self, live_catalog):
        e = live_catalog.get("minimax/m2.7")
        assert promotion_state(e, dogfood=frozenset({e.id})) == STATE_RETIRED


# ---------------------------------------------------------------------------
# Stale / retired defaults
# ---------------------------------------------------------------------------


class TestStaleDefaults:
    def test_no_requirement_class_defaults_to_a_retired_or_unlisted_model(self, live_catalog):
        for name, sel in select_all_requirements(live_catalog).items():
            assert sel.model is not None, name
            e = live_catalog.get(sel.model)
            assert e.status == "current", (name, sel.model, e.status)
            assert promotion_state(e) in (STATE_STABLE, STATE_VALIDATED)

    def test_no_legacy_class_defaults_to_a_retired_or_unlisted_model(self, live_catalog):
        for name, sel in select_all_classes(live_catalog).items():
            assert sel.model is not None, name
            assert live_catalog.get(sel.model).status == "current", (name, sel.model)

    def test_public_default_pool_ids_are_all_listed_by_the_provider(self, live_catalog):
        """A curated stable/validated id the provider does not list is a stale default
        waiting to happen; curation must retire it (as minimax/m2.7 was)."""
        unlisted = [
            e.id for e in live_catalog.entries
            if e.lifecycle in ("active_default", "active_specialist") and e.status == "unlisted"
        ]
        assert unlisted == []

    def test_retired_model_with_validated_successor_is_never_selected(self):
        old = _m("acme/worker-1", lifecycle="deprecated", roles=("routine_engineering",))
        new = _m("acme/worker-2", roles=("routine_engineering",))
        cat = build_catalog([old, new], [
            _raw("acme/worker-2", created=1790000000),
        ], synced_at="2026-09-25T00:00:00Z")
        sel = select_for_requirement("routine_coding", cat)
        assert sel.model == "acme/worker-2"
        assert ("acme/worker-1", "promotion:retired") in sel.rejected

    def test_unlisted_model_with_validated_successor_is_never_selected_on_a_fresh_snapshot(self):
        gone = _m("acme/worker-1", roles=("routine_engineering",))
        here = _m("acme/worker-2", roles=("routine_engineering",))
        cat = build_catalog([gone, here], [_raw("acme/worker-2", created=1790000000)],
                            synced_at="2026-09-25T00:00:00Z")
        assert cat.get("acme/worker-1").status == "unlisted"
        sel = select_for_requirement("routine_coding", cat)
        assert sel.model == "acme/worker-2"
        assert ("acme/worker-1", "promotion:retired") in sel.rejected
        # The adaptive candidate set rejects it too, with a reason.
        cs = build_candidate_set(cat, OPENROUTER)
        assert cs.rejection_reason("acme/worker-1") == REASON_UNLISTED_ON_PROVIDER

    def test_stale_snapshot_keeps_the_unlisted_model_selectable(self):
        gone = _m("acme/worker-1", roles=("routine_engineering",))
        cat = build_catalog([gone], [_raw("acme/other", created=1790000000)],
                            synced_at="2026-09-25T00:00:00Z", stale=True)
        assert select_for_requirement("routine_coding", cat).model == "acme/worker-1"
        assert build_candidate_set(cat, OPENROUTER).get("acme/worker-1") is not None

    def test_complex_role_no_longer_resolves_to_the_retired_id(self):
        from openshard.routing.model_resolver import MODEL_COMPLEX

        assert MODEL_COMPLEX != "minimax/m2.7"
        assert curated_catalog().get(MODEL_COMPLEX).lifecycle in ("active_default", "active_specialist")
        assert "long_context" in curated_catalog().get(MODEL_COMPLEX).capability_tags


# ---------------------------------------------------------------------------
# Successors: curation moves a default; discovery alone never does
# ---------------------------------------------------------------------------


class TestSuccessors:
    def test_validated_successor_replaces_an_old_default(self, live_catalog):
        # Promote GLM 5.3 by curation (the release-time act) and retire 5.1.
        curated = [replace(m, lifecycle="deprecated") if m.id == OLD_MAIN else m for m in all_models()]
        curated.append(_m(NEW_GLM, provider="Z-AI", roles=("routine_engineering", "standard_coding")))
        snap = _live_snapshot()
        cat = build_catalog(curated, snap["models"], synced_at=snap["synced_at"])
        assert cat.get(OLD_MAIN).status == "deprecated"
        # Legacy public default (capability off) moves with curation, no id in code.
        assert select_all_classes(cat)["balanced_coding"].model == NEW_GLM
        # Requirement-class ranking admits the successor and retires the old default.
        sel = select_for_requirement("routine_coding", cat)
        assert NEW_GLM in {r.model_id for r in sel.ranked}
        assert (OLD_MAIN, "promotion:retired") in sel.rejected
        # With the same observed evidence the successor is chosen over its family.
        history = {NEW_GLM: ObservedEvidence(samples=6, verified_success_rate=0.85, cost_per_verified_success=0.01),
                   sel.model: ObservedEvidence(samples=6, verified_success_rate=0.85, cost_per_verified_success=0.02)}
        assert select_for_requirement("routine_coding", cat, history=history).model == NEW_GLM

    def test_newer_family_member_supersedes_older_when_both_validated(self):
        older = _m("acme/opus-1", lifecycle="active_specialist", supports_reasoning=True, roles=("escalation",))
        newer = _m("acme/opus-2", lifecycle="active_specialist", supports_reasoning=True, roles=("escalation",))
        cat = build_catalog([older, newer], [
            _raw("acme/opus-1", created=1770000000, out_price="0.000025"),
            _raw("acme/opus-2", created=1790000000, out_price="0.000025"),
        ], synced_at="2026-09-25T00:00:00Z")
        sel = select_for_requirement("deep_reasoning", cat)
        assert sel.model == "acme/opus-2"
        assert "superseded_in_family" in sel.ranked[1].notes
        assert sel.ranked[1].components["superseded_in_family"] is True

    def test_supersession_never_crosses_families_or_providers(self):
        a = _m("acme/alpha-1", roles=("routine_engineering",))
        b = _m("beta/alpha-1", provider="Beta", roles=("routine_engineering",))
        cat = build_catalog([a, b], [
            _raw("acme/alpha-1", created=1770000000), _raw("beta/alpha-1", created=1790000000),
        ], synced_at="2026-09-25T00:00:00Z")
        sel = select_for_requirement("routine_coding", cat)
        assert all(r.components["superseded_in_family"] is False for r in sel.ranked)

    def test_discovered_model_appears_without_a_core_release(self, live_catalog):
        e = live_catalog.get(NEW_GLM)
        assert e is not None and not e.curated
        assert e.release_date == "2026-08-18" and e.pricing.source == "openrouter"
        assert live_catalog.resolve("glm-5.3") == NEW_GLM

    def test_discovered_model_is_a_shadow_candidate_never_a_public_default(self, live_catalog):
        from openshard.routing.requirements import shadow_candidates

        sel = select_for_requirement("routine_coding", live_catalog)
        assert sel.model != NEW_GLM
        assert all(
            promotion_state(live_catalog.get(m)) == STATE_ELIGIBLE_FOR_SHADOW
            for m in sel.shadow_candidates
        )
        everything = shadow_candidates(
            REQUIREMENT_CLASSES["routine_coding"], live_catalog.entries, states=states_for(live_catalog), limit=100,
        )
        assert NEW_GLM in everything and NEW_FLASH in everything
        for name, s in select_all_requirements(live_catalog).items():
            assert s.model is None or live_catalog.get(s.model).curated, name
            assert not any(live_catalog.get(m) and not live_catalog.get(m).curated
                           for m in (r.model_id for r in s.ranked))

    def test_shadow_candidates_are_bounded_and_exclude_variants(self, live_catalog):
        sel = select_for_requirement("routine_coding", live_catalog)
        assert len(sel.shadow_candidates) <= 3
        assert all(":" not in m for m in sel.shadow_candidates)


# ---------------------------------------------------------------------------
# Dogfood candidates
# ---------------------------------------------------------------------------


class TestDogfoodCandidates:
    def _policy(self, live_catalog, monkeypatch, tmp_path):
        from openshard.models import openrouter_fetcher as orf

        snap = _live_snapshot()
        monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path))
        orf.save_openrouter_cache(snap["models"], synced_at=snap["synced_at"])
        return model_policy_from_config({"models": {"dogfood_candidates": {"routine_coding": [NEW_GLM]}}})

    def test_config_parses_per_requirement_class(self, live_catalog, monkeypatch, tmp_path):
        policy = self._policy(live_catalog, monkeypatch, tmp_path)
        assert policy.dogfood_map == {"routine_coding": frozenset({NEW_GLM})}
        # Not an explicit selection: it never bypasses public gates by itself.
        from openshard.routing.model_policy import explicit_selection_ids, policy_summary

        assert NEW_GLM not in explicit_selection_ids(policy)
        assert policy_summary(policy)["dogfood_candidates_count"] == 1

    def test_unknown_class_or_model_is_a_config_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="unknown requirement class"):
            model_policy_from_config({"models": {"dogfood_candidates": {"turbo": [OLD_MAIN]}}})
        with pytest.raises(ValueError, match="unknown model ID"):
            model_policy_from_config({"models": {"dogfood_candidates": {"routine_coding": ["nope/none"]}}})

    def test_dogfood_candidate_participates_only_when_enabled(self, live_catalog):
        dogfood = {"routine_coding": frozenset({NEW_GLM})}
        off = select_for_requirement("routine_coding", live_catalog, dogfood=dogfood, dogfood_enabled=False)
        assert off.model != NEW_GLM
        assert NEW_GLM not in {r.model_id for r in off.ranked}
        on = select_for_requirement("routine_coding", live_catalog, dogfood=dogfood, dogfood_enabled=True)
        assert on.model == NEW_GLM
        assert on.ranked[0].promotion_state == STATE_DOGFOOD_CANDIDATE
        assert "dogfood_candidate_for_class" in on.ranked[0].notes

    def test_dogfood_candidate_for_another_class_is_not_preferred(self, live_catalog):
        dogfood = {"deep_reasoning": frozenset({NEW_GLM})}
        on = select_for_requirement("routine_coding", live_catalog, dogfood=dogfood, dogfood_enabled=True)
        assert on.model != NEW_GLM

    def test_dogfood_candidate_must_still_meet_hard_requirements(self, live_catalog):
        dogfood = {"vision": frozenset({NEW_GLM})}  # GLM 5.3 is text-only
        on = select_for_requirement("vision", live_catalog, dogfood=dogfood, dogfood_enabled=True)
        assert on.model != NEW_GLM
        assert (NEW_GLM, "missing_capability:vision") in on.rejected

    def test_candidate_set_admits_dogfood_ids_and_marks_them(self, live_catalog):
        cs = build_candidate_set(live_catalog, OPENROUTER, dogfood_ids=[NEW_GLM])
        c = cs.get(NEW_GLM)
        assert c is not None and c.dogfood and c.promotion_state == STATE_DOGFOOD_CANDIDATE
        assert not c.explicit
        assert build_candidate_set(live_catalog, OPENROUTER).get(NEW_GLM) is None


# ---------------------------------------------------------------------------
# Hard rules before ranking
# ---------------------------------------------------------------------------


class TestHardRules:
    def test_explicit_model_always_wins_when_eligible(self, live_catalog):
        cs = build_candidate_set(live_catalog, OPENROUTER, explicit_model="glm-5.3")
        assert cs.get(NEW_GLM) is not None and cs.get(NEW_GLM).explicit

    def test_blocked_provider_and_model_never_selected(self, live_catalog):
        policy = ModelPolicyConfig(blocked_providers=frozenset({"z-ai"}), blocked_models=frozenset({OLD_CHEAP}))
        cs = build_candidate_set(live_catalog, OPENROUTER, policy=policy)
        assert cs.get(OLD_MAIN) is None and cs.rejection_reason(OLD_MAIN) == "policy:blocked_provider"
        assert cs.get(OLD_CHEAP) is None and cs.rejection_reason(OLD_CHEAP) == "policy:blocked_model"
        states = states_for(live_catalog)
        ranked, _ = rank_for_requirement(
            REQUIREMENT_CLASSES["routine_coding"], (c.entry for c in cs.eligible), states=states,
        )
        assert OLD_MAIN not in {r.model_id for r in ranked}

    def test_required_capability_is_enforced(self, live_catalog):
        sel = select_for_requirement("vision", live_catalog)
        assert all("vision" in live_catalog.get(r.model_id).capability_tags for r in sel.ranked)
        assert (OLD_MAIN, "missing_capability:vision") in sel.rejected

    def test_min_context_is_enforced(self):
        small = _m("acme/small", context_length=32_000, roles=("routine_engineering",))
        big = _m("acme/big", context_length=1_000_000)
        cat = build_catalog([small, big], [
            _raw("acme/small", created=1780000000, context=32_000), _raw("acme/big", created=1780000000),
        ], synced_at="2026-09-25T00:00:00Z")
        sel = select_for_requirement("routine_coding", cat, min_context_tokens=100_000)
        assert sel.model == "acme/big"
        assert ("acme/small", "context_window_too_small") in sel.rejected

    def test_tried_models_are_excluded(self, live_catalog):
        first = select_for_requirement("routine_coding", live_catalog).model
        second = select_for_requirement("routine_coding", live_catalog, exclude=frozenset({first}))
        assert second.model != first
        assert (first, "already_tried") in second.rejected

    def test_pins_apply_by_legacy_or_requirement_name(self, live_catalog):
        by_legacy = select_for_requirement("routine_coding", live_catalog, pins={"balanced_coding": "qwen/qwen3.7-plus"})
        by_v2 = select_for_requirement("routine_coding", live_catalog, pins={"routine_coding": "qwen/qwen3.7-plus"})
        assert by_legacy.model == by_v2.model == "qwen/qwen3.7-plus"
        assert by_v2.source == "pinned"

    def test_retired_pin_is_rejected_and_ranking_continues(self, live_catalog):
        sel = select_for_requirement("routine_coding", live_catalog, pins={"routine_coding": "minimax/m2.7"})
        assert sel.rejected_pin == "minimax/m2.7" and sel.rejected_pin_reason == "deprecated_or_blocked"
        assert sel.model is not None and sel.source == "ranked"

    def test_requirement_pin_names_are_accepted_in_config(self, live_catalog, monkeypatch, tmp_path):
        from openshard.models import openrouter_fetcher as orf

        snap = _live_snapshot()
        monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path))
        orf.save_openrouter_cache(snap["models"], synced_at=snap["synced_at"])
        policy = model_policy_from_config({"models": {"routing_classes": {"routine_coding": NEW_GLM}}})
        assert policy.class_pin_map == {"routine_coding": NEW_GLM}
        with pytest.raises(ValueError, match="missing_capability:vision"):
            model_policy_from_config({"models": {"routing_classes": {"vision": NEW_GLM}}})


# ---------------------------------------------------------------------------
# Ranking is decomposable and evidence-gated
# ---------------------------------------------------------------------------


class TestRanking:
    def test_components_are_recorded_per_candidate(self, live_catalog):
        sel = select_for_requirement("routine_coding", live_catalog)
        top = sel.ranked[0].to_dict()
        assert set(top["components"]) >= {
            "promotion", "history", "requirement_fit_missing", "superseded_in_family",
            "price_band", "within_price_band", "curated_hint_matches", "output_price_per_mtok",
        }
        assert top["components"]["history"] == "not_used"
        assert top["components"]["price_source"] == "openrouter"

    def test_price_band_follows_cost_sensitivity(self, live_catalog):
        normal = select_for_requirement("routine_coding", live_catalog).ranked[0]
        assert normal.components["price_band"] == "mid"
        high = select_for_requirement("routine_coding", live_catalog, cost_sensitivity="high").ranked[0]
        assert high.components["price_band"] == "cheap"
        low = select_for_requirement("routine_coding", live_catalog, cost_sensitivity="low").ranked[0]
        assert low.components["price_band"] is None

    def test_history_is_used_only_when_passed_and_ranks_by_cost_per_verified_success(self, live_catalog):
        base = select_for_requirement("routine_coding", live_catalog)
        ids = [r.model_id for r in base.ranked[:3]]
        history = {
            ids[2]: ObservedEvidence(samples=12, verified_success_rate=0.9, cost_per_verified_success=0.02),
            ids[0]: ObservedEvidence(samples=10, verified_success_rate=0.8, cost_per_verified_success=0.05),
        }
        sel = select_for_requirement("routine_coding", live_catalog, history=history)
        assert sel.model == ids[2]
        assert sel.ranked[0].components["history"] == "used"
        assert sel.ranked[0].components["history_samples"] == 12
        without = [r for r in sel.ranked if r.model_id == ids[1]][0]
        assert without.components["history"] == "no_meaningful_evidence"

    def test_observed_failures_only_ranks_last(self, live_catalog):
        base = select_for_requirement("routine_coding", live_catalog)
        first = base.ranked[0].model_id
        history = {first: ObservedEvidence(samples=8, verified_success_rate=0.0, cost_per_verified_success=None)}
        sel = select_for_requirement("routine_coding", live_catalog, history=history)
        assert sel.model != first
        assert sel.ranked[-1].model_id == first
        assert "observed_failures_only" in sel.ranked[-1].notes

    def test_escalation_targets_are_requirement_classes(self):
        assert set(ESCALATION_TARGET) == set(REQUIREMENT_NAMES)
        for target in ESCALATION_TARGET.values():
            assert target is None or target in REQUIREMENT_CLASSES
        assert set(LEGACY_CLASS_TO_REQUIREMENT) == set(ROUTING_CLASSES)


# ---------------------------------------------------------------------------
# Determinism and offline behaviour
# ---------------------------------------------------------------------------


class TestDeterminismAndOffline:
    def test_equal_inputs_reproduce_selection(self, live_catalog):
        a = select_all_requirements(live_catalog)
        b = select_all_requirements(live_catalog)
        assert {k: v.model for k, v in a.items()} == {k: v.model for k, v in b.items()}
        assert [r.to_dict() for r in a["routine_coding"].ranked] == [r.to_dict() for r in b["routine_coding"].ranked]

    def test_curated_only_catalog_selects_every_class_offline(self, tmp_path):
        def no_network():
            raise AssertionError("offline selection must not fetch")

        cat = load_catalog(refresh="never", cache_path=tmp_path / "missing.json", fetcher=no_network)
        for name, sel in select_all_requirements(cat).items():
            assert sel.model is not None, name


# ---------------------------------------------------------------------------
# Old Receipts remain readable
# ---------------------------------------------------------------------------


class TestOldReceipts:
    @pytest.mark.parametrize("model", [OLD_MAIN, OLD_CHEAP, "minimax/m2.7"])
    def test_receipt_naming_a_previous_default_still_renders_and_derives_an_outcome(self, model):
        entry = {
            "schema_version": 1, "timestamp": "2026-05-01T00:00:00Z", "task": "add helper",
            "execution_model": model, "executor": "native", "verification_attempted": True,
            "verification_passed": True, "estimated_cost": 0.01, "duration_seconds": 4.2,
            "files_created": 1, "files_updated": 0, "files_deleted": 0, "retry_triggered": False,
            "routing_provenance": {
                "version": 1, "record_mode": "shadow",
                "policy": {"name": "deterministic_baseline", "version": "1"},
                "selected_model": model, "selection_mode": "routed", "resolved_class": "balanced_coding",
                "fingerprints": {"decision": "abc"}, "context": {"harness": "native"},
            },
        }
        receipt = build_shard_receipt(entry)
        text = render_full_shard_receipt(receipt)
        assert model.split("/")[-1].split("-")[0] in text.lower()
        outcome = outcome_from_receipt(entry)
        assert outcome.final_model == model and outcome.routed_model == model
        assert outcome.routing_class == "balanced_coding"
