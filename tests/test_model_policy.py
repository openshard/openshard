"""Model policy config v1.

Covers: config parsing, apply_model_policy filtering, eligible_lifecycles,
policy_summary, build_routable_pool integration, Shard metadata recording,
and edge cases (invalid IDs, empty pool, provider_family mode).

All env access in pool-building helpers goes through the injected mapping
(same pattern as test_provider_eligibility.py).
"""
from __future__ import annotations

import pytest

from openshard.models.registry import models_by_lifecycle
from openshard.routing.model_policy import (
    COST_CLASS_ORDER,
    REASON_POLICY_BLOCKED_MODEL,
    REASON_POLICY_BLOCKED_PROVIDER,
    REASON_POLICY_COST_CLASS,
    REASON_POLICY_CUSTOM_ROSTER,
    REASON_POLICY_NOT_ALLOWED_MODEL,
    REASON_POLICY_NOT_ALLOWED_PROVIDER,
    REASON_POLICY_OPENROUTER_WIDE,
    ModelPolicyConfig,
    _model_vendor,
    apply_model_policy,
    eligible_lifecycles,
    model_policy_from_config,
    policy_summary,
)
from openshard.routing.provider_availability import (
    build_available_pool,
    build_routable_pool,
    detect_provider_availability,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

OPENROUTER_ONLY = {"OPENROUTER_API_KEY": "k"}
ANTHROPIC_ONLY = {"ANTHROPIC_API_KEY": "k"}
ALL_KEYS = {"OPENROUTER_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"}


def _pool(env: dict, policy: ModelPolicyConfig | None = None):
    return build_routable_pool(detect_provider_availability(env), policy=policy)


def _available(env: dict):
    return build_available_pool(detect_provider_availability(env))


# Stable model IDs from the registry used across tests.
SONNET_46 = "anthropic/claude-sonnet-4.6"
HAIKU_45 = "anthropic/claude-haiku-4.5"
OPUS_47 = "anthropic/claude-opus-4.7"         # active_specialist
FALLBACK_ID = "~anthropic/claude-haiku-latest"  # fallback lifecycle
MYTHOS_ID = "anthropic/claude-mythos-5"         # watchlist + RESTRICTED
OPEN_WEIGHT_ID = "google/gemma-4-26b-a4b-it"    # open_weight lifecycle
DEEPSEEK_ID = "deepseek/deepseek-v4-flash"       # active_default, cheap


# ---------------------------------------------------------------------------
# _model_vendor
# ---------------------------------------------------------------------------


class TestModelVendor:
    def test_standard_id(self):
        assert _model_vendor("anthropic/claude-sonnet-4.6") == "anthropic"

    def test_tilde_fallback_alias(self):
        assert _model_vendor("~anthropic/claude-haiku-latest") == "anthropic"

    def test_openai_vendor(self):
        assert _model_vendor("openai/gpt-4o") == "openai"

    def test_deepseek_vendor(self):
        assert _model_vendor("deepseek/deepseek-v4-flash") == "deepseek"


# ---------------------------------------------------------------------------
# eligible_lifecycles
# ---------------------------------------------------------------------------


class TestEligibleLifecycles:
    def test_none_returns_active_default_only(self):
        assert eligible_lifecycles(None) == frozenset({"active_default"})

    def test_default_policy_returns_active_default_only(self):
        assert eligible_lifecycles(ModelPolicyConfig()) == frozenset({"active_default"})

    def test_allow_specialist_adds_active_specialist(self):
        p = ModelPolicyConfig(allow_specialist=True)
        assert "active_specialist" in eligible_lifecycles(p)
        assert "active_default" in eligible_lifecycles(p)

    def test_allow_experimental_adds_experimental(self):
        p = ModelPolicyConfig(allow_experimental=True)
        assert "experimental" in eligible_lifecycles(p)

    def test_allow_watchlist_adds_watchlist(self):
        p = ModelPolicyConfig(allow_watchlist=True)
        assert "watchlist" in eligible_lifecycles(p)

    def test_allow_deprecated_adds_deprecated(self):
        p = ModelPolicyConfig(allow_deprecated=True)
        assert "deprecated" in eligible_lifecycles(p)

    def test_allow_open_weight_adds_open_weight(self):
        p = ModelPolicyConfig(allow_open_weight=True)
        assert "open_weight" in eligible_lifecycles(p)

    def test_allow_fallback_adds_fallback(self):
        p = ModelPolicyConfig(allow_fallback=True)
        assert "fallback" in eligible_lifecycles(p)


# ---------------------------------------------------------------------------
# Default policy preserves current routable pool
# ---------------------------------------------------------------------------


class TestDefaultPolicyPreservesPool:
    def test_default_policy_same_size_as_no_policy(self):
        env = OPENROUTER_ONLY
        without = _pool(env, policy=None)
        with_default = _pool(env, policy=ModelPolicyConfig())
        assert set(m.id for m in with_default.routable) == set(
            m.id for m in without.routable
        ), "Default policy must not change the routable pool"

    def test_default_policy_anthropic_only_same(self):
        env = ANTHROPIC_ONLY
        without = _pool(env, policy=None)
        with_default = _pool(env, policy=ModelPolicyConfig())
        assert set(m.id for m in with_default.routable) == set(
            m.id for m in without.routable
        )


# ---------------------------------------------------------------------------
# blocked_models
# ---------------------------------------------------------------------------


class TestBlockedModels:
    def test_blocked_model_absent_from_routable(self):
        p = ModelPolicyConfig(blocked_models=frozenset({HAIKU_45}))
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert all(m.id != HAIKU_45 for m in pool.routable)

    def test_blocked_model_reason_recorded(self):
        p = ModelPolicyConfig(blocked_models=frozenset({HAIKU_45}))
        pool = _pool(OPENROUTER_ONLY, policy=p)
        excluded_reasons = {mid: r for mid, r in pool.excluded}
        assert excluded_reasons.get(HAIKU_45) == REASON_POLICY_BLOCKED_MODEL

    def test_blocked_wins_over_allowed(self):
        # Model in both lists: blocked must win.
        p = ModelPolicyConfig(
            allowed_models=frozenset({SONNET_46}),
            blocked_models=frozenset({SONNET_46}),
        )
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert all(m.id != SONNET_46 for m in pool.routable)

    def test_blocked_model_via_apply_model_policy_directly(self):
        # HAIKU_45 is available with OPENROUTER_ONLY; policy should exclude it.
        avail = _available(OPENROUTER_ONLY)
        p = ModelPolicyConfig(blocked_models=frozenset({HAIKU_45}))
        filtered = apply_model_policy(avail, p)
        haiku_entry = next((ma for ma in filtered if ma.entry.id == HAIKU_45), None)
        assert haiku_entry is not None
        assert not haiku_entry.available
        assert haiku_entry.reason == REASON_POLICY_BLOCKED_MODEL

    def test_blocked_model_does_not_overwrite_upstream_reason(self):
        # HAIKU_45 with NO_KEYS is already unavailable (no_api_key).
        # Blocking it via policy must not change the reason — upstream wins.
        avail = _available({})  # no keys: all unavailable
        p = ModelPolicyConfig(blocked_models=frozenset({HAIKU_45}))
        filtered = apply_model_policy(avail, p)
        haiku_entry = next((ma for ma in filtered if ma.entry.id == HAIKU_45), None)
        assert haiku_entry is not None
        assert not haiku_entry.available
        # Reason should be the upstream no_api_key, NOT policy:blocked_model.
        assert haiku_entry.reason != REASON_POLICY_BLOCKED_MODEL


# ---------------------------------------------------------------------------
# allowed_models
# ---------------------------------------------------------------------------


class TestAllowedModels:
    def test_allowed_restricts_to_list(self):
        p = ModelPolicyConfig(allowed_models=frozenset({SONNET_46}))
        pool = _pool(OPENROUTER_ONLY, policy=p)
        # Only SONNET_46 may be routable (subject to lifecycle).
        for m in pool.routable:
            assert m.id == SONNET_46, f"Unexpected model in pool: {m.id}"

    def test_others_get_not_allowed_reason(self):
        p = ModelPolicyConfig(allowed_models=frozenset({SONNET_46}))
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        for ma in filtered:
            if ma.entry.id != SONNET_46 and not ma.available:
                # Some may have been excluded earlier for non-policy reasons;
                # those that were available before policy should get policy reason.
                pass
        policy_excluded = {
            ma.entry.id
            for ma in filtered
            if ma.reason == REASON_POLICY_NOT_ALLOWED_MODEL
        }
        # At least some models should have been excluded by the allowlist.
        assert len(policy_excluded) > 0

    def test_empty_allowed_is_not_a_filter(self):
        # allowed_models=frozenset() (default) → no filtering.
        p = ModelPolicyConfig(allowed_models=frozenset())
        pool_no_policy = _pool(OPENROUTER_ONLY, policy=None)
        pool_with_policy = _pool(OPENROUTER_ONLY, policy=p)
        assert set(m.id for m in pool_with_policy.routable) == set(
            m.id for m in pool_no_policy.routable
        )


# ---------------------------------------------------------------------------
# blocked_providers / allowed_providers
# ---------------------------------------------------------------------------


class TestProviderFilters:
    def test_blocked_provider_excludes_family(self):
        p = ModelPolicyConfig(blocked_providers=frozenset({"anthropic"}))
        pool = _pool(OPENROUTER_ONLY, policy=p)
        for m in pool.routable:
            assert _model_vendor(m.id) != "anthropic", m.id

    def test_blocked_provider_reason_recorded(self):
        p = ModelPolicyConfig(blocked_providers=frozenset({"anthropic"}))
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        blocked = [ma for ma in filtered if ma.reason == REASON_POLICY_BLOCKED_PROVIDER]
        assert len(blocked) > 0

    def test_allowed_providers_restricts_to_family(self):
        p = ModelPolicyConfig(allowed_providers=frozenset({"anthropic"}))
        pool = _pool(OPENROUTER_ONLY, policy=p)
        for m in pool.routable:
            assert _model_vendor(m.id) == "anthropic", m.id

    def test_allowed_providers_not_allowed_reason(self):
        p = ModelPolicyConfig(allowed_providers=frozenset({"anthropic"}))
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        not_allowed = [
            ma for ma in filtered if ma.reason == REASON_POLICY_NOT_ALLOWED_PROVIDER
        ]
        assert len(not_allowed) > 0

    def test_empty_allowed_providers_is_not_a_filter(self):
        p = ModelPolicyConfig(allowed_providers=frozenset())
        pool_no = _pool(OPENROUTER_ONLY, policy=None)
        pool_p = _pool(OPENROUTER_ONLY, policy=p)
        assert set(m.id for m in pool_p.routable) == set(m.id for m in pool_no.routable)

    def test_filter_uses_id_prefix_not_entry_provider(self):
        # Verify the vendor extraction function, not entry.provider.
        assert _model_vendor(SONNET_46) == "anthropic"
        assert _model_vendor(OPEN_WEIGHT_ID) == "google"
        assert _model_vendor(DEEPSEEK_ID) == "deepseek"


# ---------------------------------------------------------------------------
# max_cost_class
# ---------------------------------------------------------------------------


class TestMaxCostClass:
    def test_null_no_filtering(self):
        p = ModelPolicyConfig(max_cost_class=None)
        pool_no = _pool(OPENROUTER_ONLY, policy=None)
        pool_p = _pool(OPENROUTER_ONLY, policy=p)
        assert set(m.id for m in pool_p.routable) == set(m.id for m in pool_no.routable)

    def test_cheap_excludes_expensive_models(self):
        p = ModelPolicyConfig(max_cost_class="cheap")
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        for ma in filtered:
            if ma.available:
                cost = ma.entry.cost_class or "unknown"
                assert COST_CLASS_ORDER.get(cost, 5) <= COST_CLASS_ORDER["cheap"], (
                    f"{ma.entry.id} with cost_class={cost!r} should be excluded"
                )

    def test_cost_class_reason_recorded(self):
        p = ModelPolicyConfig(max_cost_class="cheap")
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        cost_excluded = [ma for ma in filtered if ma.reason == REASON_POLICY_COST_CLASS]
        assert len(cost_excluded) > 0


# ---------------------------------------------------------------------------
# Lifecycle flags via build_routable_pool
# ---------------------------------------------------------------------------


class TestLifecycleFlags:
    def test_specialist_excluded_by_default(self):
        pool = _pool(OPENROUTER_ONLY, policy=None)
        assert all(m.lifecycle != "active_specialist" for m in pool.routable)

    def test_allow_specialist_includes_specialist_models(self):
        p = ModelPolicyConfig(allow_specialist=True)
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert any(m.lifecycle == "active_specialist" for m in pool.routable), (
            "allow_specialist=True should admit active_specialist models"
        )

    def test_allow_specialist_false_still_excludes(self):
        p = ModelPolicyConfig(allow_specialist=False)
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert all(m.lifecycle != "active_specialist" for m in pool.routable)

    def test_watchlist_excluded_by_default(self):
        pool = _pool(OPENROUTER_ONLY, policy=None)
        assert all(m.lifecycle != "watchlist" for m in pool.routable)

    def test_allow_watchlist_admits_watchlist_lifecycle(self):
        p = ModelPolicyConfig(allow_watchlist=True)
        pool = _pool(OPENROUTER_ONLY, policy=p)
        # Watchlist models (excluding RESTRICTED) should now be eligible.
        # Mythos is still blocked by RESTRICTED_MODEL_IDS.
        non_mythos_watchlist = [
            m for m in pool.routable
            if m.lifecycle == "watchlist" and m.id != MYTHOS_ID
        ]
        # If there are watchlist models besides Mythos, they should appear.
        all_watchlist = models_by_lifecycle("watchlist")
        non_restricted_watchlist = [m for m in all_watchlist if m.id != MYTHOS_ID]
        if non_restricted_watchlist:
            assert len(non_mythos_watchlist) > 0

    def test_open_weight_excluded_by_default(self):
        pool = _pool(OPENROUTER_ONLY, policy=None)
        assert all(m.lifecycle != "open_weight" for m in pool.routable)

    def test_allow_open_weight_includes_open_weight_models(self):
        p = ModelPolicyConfig(allow_open_weight=True)
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert any(m.lifecycle == "open_weight" for m in pool.routable), (
            "allow_open_weight=True should admit open_weight models"
        )

    def test_fallback_excluded_by_default(self):
        pool = _pool(OPENROUTER_ONLY, policy=None)
        assert all(m.lifecycle != "fallback" for m in pool.routable)

    def test_allow_fallback_includes_fallback_models(self):
        p = ModelPolicyConfig(allow_fallback=True)
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert any(m.lifecycle == "fallback" for m in pool.routable), (
            "allow_fallback=True should admit fallback models"
        )


# ---------------------------------------------------------------------------
# Mythos access restriction is upstream of policy
# ---------------------------------------------------------------------------


class TestMythosAccessRestriction:
    def test_mythos_not_routable_with_default_policy(self):
        pool = _pool(OPENROUTER_ONLY, policy=None)
        assert all(m.id != MYTHOS_ID for m in pool.routable)

    def test_mythos_not_routable_even_with_watchlist_allowed(self):
        p = ModelPolicyConfig(allow_watchlist=True)
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert all(m.id != MYTHOS_ID for m in pool.routable), (
            "Mythos must remain blocked by RESTRICTED_MODEL_IDS regardless of policy"
        )

    def test_mythos_not_routable_even_when_explicitly_allowed(self):
        p = ModelPolicyConfig(
            allow_watchlist=True,
            allowed_models=frozenset({MYTHOS_ID}),
        )
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert all(m.id != MYTHOS_ID for m in pool.routable), (
            "Mythos must remain blocked even if listed in allowed_models"
        )

    def test_mythos_retains_access_restricted_reason_when_also_blocked_by_policy(self):
        # Mythos is access_restricted upstream. Adding it to blocked_models
        # must not overwrite that reason — upstream exclusion always wins.
        avail = _available(OPENROUTER_ONLY)
        p = ModelPolicyConfig(blocked_models=frozenset({MYTHOS_ID}))
        filtered = apply_model_policy(avail, p)
        mythos_entry = next((ma for ma in filtered if ma.entry.id == MYTHOS_ID), None)
        assert mythos_entry is not None
        assert not mythos_entry.available
        assert mythos_entry.reason == "access_restricted", (
            f"Expected 'access_restricted', got {mythos_entry.reason!r}"
        )


# ---------------------------------------------------------------------------
# Custom roster mode
# ---------------------------------------------------------------------------


class TestCustomRosterMode:
    def test_custom_roster_restricts_to_roster(self):
        p = ModelPolicyConfig(
            mode="custom_roster",
            custom_roster_models=frozenset({SONNET_46}),
        )
        pool = _pool(OPENROUTER_ONLY, policy=p)
        for m in pool.routable:
            assert m.id == SONNET_46, f"Unexpected model in custom roster pool: {m.id}"

    def test_custom_roster_reason_recorded_for_excluded(self):
        p = ModelPolicyConfig(
            mode="custom_roster",
            custom_roster_models=frozenset({SONNET_46}),
        )
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        roster_excluded = [
            ma for ma in filtered if ma.reason == REASON_POLICY_CUSTOM_ROSTER
        ]
        assert len(roster_excluded) > 0

    def test_custom_roster_empty_roster_excludes_all(self):
        p = ModelPolicyConfig(mode="custom_roster", custom_roster_models=frozenset())
        pool = _pool(OPENROUTER_ONLY, policy=p)
        assert len(pool.routable) == 0


# ---------------------------------------------------------------------------
# allow_openrouter_wide
# ---------------------------------------------------------------------------


class TestAllowOpenrouterWide:
    def test_false_excludes_openrouter_only_models(self):
        # With Anthropic-only keys, models reachable via openrouter but not
        # directly should NOT be admitted when allow_openrouter_wide=False.
        # First confirm that with ANTHROPIC_ONLY there are still models only
        # reachable via openrouter excluded by the no-key check.
        # With OPENROUTER_ONLY all models are "via openrouter".
        p = ModelPolicyConfig(allow_openrouter_wide=False)
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        excluded = [ma for ma in filtered if ma.reason == REASON_POLICY_OPENROUTER_WIDE]
        # All models in OPENROUTER_ONLY env are "via openrouter" only.
        assert len(excluded) > 0

    def test_true_does_not_exclude(self):
        p = ModelPolicyConfig(allow_openrouter_wide=True)
        avail = _available(OPENROUTER_ONLY)
        filtered = apply_model_policy(avail, p)
        excluded = [ma for ma in filtered if ma.reason == REASON_POLICY_OPENROUTER_WIDE]
        assert len(excluded) == 0


# ---------------------------------------------------------------------------
# model_policy_from_config — valid configs
# ---------------------------------------------------------------------------


class TestModelPolicyFromConfig:
    def test_empty_config_returns_defaults(self):
        p = model_policy_from_config({})
        assert p == ModelPolicyConfig()

    def test_missing_models_key_returns_defaults(self):
        p = model_policy_from_config({"other_key": "value"})
        assert p == ModelPolicyConfig()

    def test_mode_auto(self):
        p = model_policy_from_config({"models": {"mode": "auto"}})
        assert p.mode == "auto"

    def test_mode_custom_roster(self):
        p = model_policy_from_config({"models": {"mode": "custom_roster"}})
        assert p.mode == "custom_roster"

    def test_allowed_models_parsed(self):
        p = model_policy_from_config({"models": {"allowed_models": [SONNET_46]}})
        assert SONNET_46 in p.allowed_models

    def test_blocked_models_parsed(self):
        p = model_policy_from_config({"models": {"blocked_models": [HAIKU_45]}})
        assert HAIKU_45 in p.blocked_models

    def test_max_cost_class_null(self):
        p = model_policy_from_config({"models": {"max_cost_class": None}})
        assert p.max_cost_class is None

    def test_max_cost_class_cheap(self):
        p = model_policy_from_config({"models": {"max_cost_class": "cheap"}})
        assert p.max_cost_class == "cheap"

    def test_boolean_flags_parsed(self):
        p = model_policy_from_config({"models": {
            "allow_specialist": True,
            "allow_experimental": True,
            "allow_watchlist": True,
        }})
        assert p.allow_specialist is True
        assert p.allow_experimental is True
        assert p.allow_watchlist is True

    def test_custom_roster_models_parsed(self):
        p = model_policy_from_config({"models": {
            "mode": "custom_roster",
            "custom_roster": {"name": "myteam", "models": [SONNET_46]},
        }})
        assert SONNET_46 in p.custom_roster_models
        assert p.custom_roster_name == "myteam"

    def test_provider_family_mode_treated_as_auto(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            p = model_policy_from_config({"models": {"mode": "provider_family"}})
        assert p.mode == "auto"
        assert any("provider_family" in str(warning.message) for warning in w)


# ---------------------------------------------------------------------------
# model_policy_from_config — invalid configs raise clearly
# ---------------------------------------------------------------------------


class TestModelPolicyFromConfigErrors:
    def test_invalid_allowed_model_id_raises(self):
        with pytest.raises(ValueError, match="not/real-model"):
            model_policy_from_config({"models": {"allowed_models": ["not/real-model"]}})

    def test_invalid_blocked_model_id_raises(self):
        with pytest.raises(ValueError, match="bad/model"):
            model_policy_from_config({"models": {"blocked_models": ["bad/model"]}})

    def test_invalid_custom_roster_model_id_raises(self):
        with pytest.raises(ValueError, match="missing/model"):
            model_policy_from_config({"models": {
                "mode": "custom_roster",
                "custom_roster": {"models": ["missing/model"]},
            }})

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="not_a_mode"):
            model_policy_from_config({"models": {"mode": "not_a_mode"}})

    def test_invalid_max_cost_class_raises(self):
        with pytest.raises(ValueError, match="ultra_expensive"):
            model_policy_from_config({"models": {"max_cost_class": "ultra_expensive"}})

    def test_error_message_contains_model_id(self):
        bad_id = "totally/fake-model"
        with pytest.raises(ValueError) as exc_info:
            model_policy_from_config({"models": {"allowed_models": [bad_id]}})
        assert bad_id in str(exc_info.value)


# ---------------------------------------------------------------------------
# policy_summary
# ---------------------------------------------------------------------------


class TestPolicySummary:
    def test_summary_has_expected_keys(self):
        p = ModelPolicyConfig()
        s = policy_summary(p)
        expected_keys = {
            "mode", "blocked_models_count", "allowed_models_count",
            "blocked_providers_count", "allowed_providers_count",
            "max_cost_class", "allow_specialist", "allow_experimental",
            "allow_watchlist", "allow_deprecated", "allow_open_weight",
            "allow_fallback", "custom_roster_name", "class_pins_count",
        }
        assert set(s.keys()) == expected_keys

    def test_summary_no_model_id_lists(self):
        p = ModelPolicyConfig(
            allowed_models=frozenset({SONNET_46}),
            blocked_models=frozenset({HAIKU_45}),
        )
        s = policy_summary(p)
        # Must not contain the actual ID values.
        assert SONNET_46 not in str(s)
        assert HAIKU_45 not in str(s)
        # But counts should be present.
        assert s["allowed_models_count"] == 1
        assert s["blocked_models_count"] == 1

    def test_custom_roster_name_present_in_custom_roster_mode(self):
        p = ModelPolicyConfig(mode="custom_roster", custom_roster_name="myteam")
        s = policy_summary(p)
        assert s["custom_roster_name"] == "myteam"

    def test_custom_roster_name_none_in_auto_mode(self):
        p = ModelPolicyConfig(mode="auto", custom_roster_name="myteam")
        s = policy_summary(p)
        assert s["custom_roster_name"] is None

    def test_summary_is_json_serialisable(self):
        import json
        p = ModelPolicyConfig(
            allowed_models=frozenset({SONNET_46}),
            max_cost_class="cheap",
        )
        # Should not raise.
        json.dumps(policy_summary(p))


# ---------------------------------------------------------------------------
# RoutingTruth.policy_summary integration
# ---------------------------------------------------------------------------


class TestRoutingTruthPolicySummary:
    def test_policy_summary_recorded_in_routing_truth(self):
        from openshard.history.routing_truth import build_routing_truth

        p = ModelPolicyConfig(allow_specialist=True)
        ps = policy_summary(p)
        entry = {"model_policy_summary": ps}
        truth = build_routing_truth(entry)
        assert truth.policy_summary == ps

    def test_policy_summary_none_when_not_in_entry(self):
        from openshard.history.routing_truth import build_routing_truth

        truth = build_routing_truth({})
        assert truth.policy_summary is None

    def test_policy_summary_non_dict_falls_back_to_none(self):
        from openshard.history.routing_truth import build_routing_truth

        truth = build_routing_truth({"model_policy_summary": "bad_value"})
        assert truth.policy_summary is None


# ---------------------------------------------------------------------------
# Excluded accumulation: provider reasons are preserved alongside policy
# ---------------------------------------------------------------------------


class TestExcludedAccumulation:
    def test_policy_adds_to_existing_excluded(self):
        # With Anthropic-only keys: non-anthropic models are excluded for
        # no_api_key. Adding a policy block on haiku should add haiku to
        # excluded with policy reason, while non-anthropic models retain
        # their provider reason.
        p = ModelPolicyConfig(blocked_models=frozenset({HAIKU_45}))
        pool = _pool(ANTHROPIC_ONLY, policy=p)
        excluded_map = {mid: r for mid, r in pool.excluded}
        # Haiku should appear with the policy reason.
        assert excluded_map.get(HAIKU_45) == REASON_POLICY_BLOCKED_MODEL
        # At least one non-anthropic model should appear with a non-policy reason.
        non_policy_excluded = [r for r in excluded_map.values() if not r.startswith("policy:")]
        assert len(non_policy_excluded) > 0


# ---------------------------------------------------------------------------
# apply_model_policy does not reorder existing unavailable entries
# ---------------------------------------------------------------------------


class TestApplyModelPolicyIdempotentOnUnavailable:
    def test_already_unavailable_stays_unavailable(self):
        avail = _available({})  # no keys: everything unavailable
        p = ModelPolicyConfig()
        filtered = apply_model_policy(avail, p)
        assert all(not ma.available for ma in filtered)
