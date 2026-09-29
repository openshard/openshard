from __future__ import annotations

import unittest

from openshard.models.registry import (
    _REGISTRY,
    ROLE_GROUPS,
    ModelEntry,
    display_name_for,
    get_model,
    is_experimental,
    models_by_capability,
    models_by_role,
    supports,
)
from openshard.routing.engine import MODEL_CHEAP, MODEL_MAIN, MODEL_STRONG

# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


class TestRegistryCompleteness(unittest.TestCase):
    def _ids(self) -> set[str]:
        return {e.id for e in _REGISTRY}

    def test_all_new_core_model_ids_in_registry(self) -> None:
        core_ids = {
            "google/gemini-3.1-flash-lite",
            "google/gemini-3.5-flash",
            "qwen/qwen3.7-max",
            "x-ai/grok-4.3",
            "~anthropic/claude-haiku-latest",
            "x-ai/grok-build-0.1",
            # OpenAI current family + compatibility
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-terra",
            "openai/gpt-6-astra",
            "openai/gpt-6-luna",
            "openai/gpt-6-sol",
            "openai/gpt-5.5",
            "openai/gpt-5.5-pro",
            "openai/gpt-5.4",
            "openai/gpt-5.4-pro",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.4-nano",
            "openai/gpt-5-mini",
            "openai/gpt-5-nano",
            "moonshotai/kimi-k2.6",
            "deepseek/deepseek-v4-pro",
        }
        missing = core_ids - self._ids()
        self.assertFalse(missing, f"Missing core models: {missing}")

    def test_all_new_experimental_model_ids_in_registry(self) -> None:
        experimental_ids = {
            "qwen/qwen3.6-flash",
            "qwen/qwen3-coder-30b-a3b-instruct",
            "mistralai/codestral-2508",
            "google/gemma-4-26b-a4b-it",
            "google/gemma-4-31b-it",
            "ibm-granite/granite-4.1-8b",
            "stepfun/step-3.5-flash",
            "poolside/laguna-xs.2:free",
            "poolside/laguna-m.1:free",
            # OpenRouter expansion — experimental
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "inclusionai/ring-2.6-1t",
            "minimax/minimax-m2.7",
            "inception/mercury-2",
        }
        missing = experimental_ids - self._ids()
        self.assertFalse(missing, f"Missing experimental models: {missing}")

    def test_existing_routing_models_in_registry(self) -> None:
        for model_id in (MODEL_CHEAP, MODEL_MAIN, MODEL_STRONG):
            self.assertIn(model_id, self._ids(), f"Routing model missing: {model_id}")

    def test_fable5_and_mythos5_in_registry(self) -> None:
        ids = self._ids()
        self.assertIn("anthropic/claude-fable-5", ids)
        self.assertIn("anthropic/claude-mythos-5", ids)

    def test_no_duplicate_model_ids(self) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for entry in _REGISTRY:
            if entry.id in seen:
                duplicates.add(entry.id)
            seen.add(entry.id)
        self.assertFalse(duplicates, f"Duplicate model IDs: {duplicates}")


# ---------------------------------------------------------------------------
# Experimental flag
# ---------------------------------------------------------------------------


class TestExperimentalFlag(unittest.TestCase):
    _CORE_NON_EXPERIMENTAL = {
        "google/gemini-3.1-flash-lite",
        "google/gemini-3.5-flash",
        "qwen/qwen3.7-max",
        "x-ai/grok-4.3",
        "~anthropic/claude-haiku-latest",
        # OpenAI current family + compatibility
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-terra",
        "openai/gpt-6-astra",
        "openai/gpt-6-luna",
        "openai/gpt-6-sol",
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.4",
        "openai/gpt-5.4-pro",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
        "openai/gpt-5-mini",
        "openai/gpt-5-nano",
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-pro",
    }
    _EXPERIMENTAL_MODELS = {
        "qwen/qwen3.6-flash",
        "qwen/qwen3-coder-30b-a3b-instruct",
        "mistralai/codestral-2508",
        "google/gemma-4-26b-a4b-it",
        "google/gemma-4-31b-it",
        "ibm-granite/granite-4.1-8b",
        "stepfun/step-3.5-flash",
        "poolside/laguna-xs.2:free",
        "poolside/laguna-m.1:free",
        "x-ai/grok-build-0.1",
        # OpenRouter expansion
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "inclusionai/ring-2.6-1t",
        "minimax/minimax-m2.7",
        "inception/mercury-2",
    }

    def test_experimental_false_for_core_models(self) -> None:
        for model_id in self._CORE_NON_EXPERIMENTAL:
            entry = get_model(model_id)
            self.assertIsNotNone(entry, model_id)
            self.assertFalse(entry.experimental, f"{model_id} should not be experimental")  # type: ignore[union-attr]

    def test_experimental_true_for_experimental_models(self) -> None:
        for model_id in self._EXPERIMENTAL_MODELS:
            entry = get_model(model_id)
            self.assertIsNotNone(entry, model_id)
            self.assertTrue(entry.experimental, f"{model_id} should be experimental")  # type: ignore[union-attr]

    def test_is_experimental_helper_true(self) -> None:
        self.assertTrue(is_experimental("x-ai/grok-build-0.1"))

    def test_is_experimental_helper_false(self) -> None:
        self.assertFalse(is_experimental("google/gemini-3.5-flash"))

    def test_is_experimental_returns_false_for_unknown_model(self) -> None:
        self.assertFalse(is_experimental("not/a-real-model"))


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------


class TestDisplayNames(unittest.TestCase):
    def test_display_name_for_known_model(self) -> None:
        self.assertEqual(
            display_name_for("google/gemini-3.1-flash-lite"),
            "Google: Gemini 3.1 Flash Lite",
        )

    def test_display_name_for_unknown_with_fallback(self) -> None:
        self.assertEqual(
            display_name_for("not/a-model", fallback="Fallback Name"),
            "Fallback Name",
        )

    def test_display_name_for_unknown_without_fallback(self) -> None:
        self.assertEqual(display_name_for("not/a-model"), "not/a-model")

    def test_display_name_spot_checks(self) -> None:
        cases = {
            "google/gemini-3.5-flash": "Google: Gemini 3.5 Flash",
            "qwen/qwen3.7-max": "Qwen: Qwen3.7 Max",
            "x-ai/grok-4.3": "xAI: Grok 4.3",
            "mistralai/codestral-2508": "Mistral: Codestral 2508",
            "ibm-granite/granite-4.1-8b": "IBM: Granite 4.1 8B",
            "poolside/laguna-xs.2:free": "Poolside: Laguna XS.2 (free)",
        }
        for model_id, expected in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(display_name_for(model_id), expected)


# ---------------------------------------------------------------------------
# Role lookups
# ---------------------------------------------------------------------------


class TestRoleLookups(unittest.TestCase):
    def _role_ids(self, role: str) -> list[str]:
        return [e.id for e in models_by_role(role)]

    def test_models_by_role_cheap_control_contains_gemini_flash_lite(self) -> None:
        self.assertIn("google/gemini-3.1-flash-lite", self._role_ids("cheap_control"))

    def test_models_by_role_planner_reviewer_contains_grok_and_qwen(self) -> None:
        ids = self._role_ids("planner")
        self.assertIn("x-ai/grok-4.3", ids)
        self.assertIn("qwen/qwen3.7-max", ids)

    def test_models_by_role_returns_empty_for_unknown_role(self) -> None:
        self.assertEqual(models_by_role("nonexistent_role_xyz"), [])

    def test_role_groups_cheap_control_exact_members(self) -> None:
        self.assertEqual(
            ROLE_GROUPS["cheap_control"],
            [
                "google/gemini-3.1-flash-lite",
                "qwen/qwen3.6-flash",
                "~anthropic/claude-haiku-latest",
                "ibm-granite/granite-4.1-8b",
                "google/gemma-4-26b-a4b-it",
            ],
        )

    def test_role_groups_experimental_coding_agent_exact_members(self) -> None:
        self.assertEqual(
            ROLE_GROUPS["experimental_coding_agent"],
            [
                "x-ai/grok-build-0.1",
                "poolside/laguna-xs.2:free",
                "poolside/laguna-m.1:free",
                "stepfun/step-3.5-flash",
            ],
        )


# ---------------------------------------------------------------------------
# Capability lookups
# ---------------------------------------------------------------------------


class TestCapabilityLookups(unittest.TestCase):
    def _cap_ids(self, cap: str) -> list[str]:
        return [e.id for e in models_by_capability(cap)]

    def test_models_by_capability_reasoning_contains_grok_4_3(self) -> None:
        self.assertIn("x-ai/grok-4.3", self._cap_ids("reasoning"))

    def test_models_by_capability_multimodal_contains_gemini_flash(self) -> None:
        self.assertIn("google/gemini-3.5-flash", self._cap_ids("multimodal"))

    def test_models_by_capability_returns_empty_for_unknown_capability(self) -> None:
        self.assertEqual(models_by_capability("telekinesis"), [])

    def test_supports_reasoning_true(self) -> None:
        self.assertTrue(supports("x-ai/grok-4.3", "reasoning"))

    def test_supports_tools_false_for_poolside(self) -> None:
        self.assertFalse(supports("poolside/laguna-xs.2:free", "tools"))

    def test_supports_returns_false_for_unknown_model(self) -> None:
        self.assertFalse(supports("not/a-model", "tools"))

    def test_supports_returns_false_for_unknown_capability(self) -> None:
        self.assertFalse(supports("x-ai/grok-4.3", "telekinesis"))

    def test_models_by_capability_tools_includes_most_models(self) -> None:
        ids = set(self._cap_ids("tools"))
        self.assertIn("google/gemini-3.1-flash-lite", ids)
        self.assertIn("qwen/qwen3.7-max", ids)
        # Poolside free models have no tool support
        self.assertNotIn("poolside/laguna-xs.2:free", ids)


# ---------------------------------------------------------------------------
# get_model
# ---------------------------------------------------------------------------


class TestGetModel(unittest.TestCase):
    def test_get_model_returns_correct_entry(self) -> None:
        entry = get_model("google/gemini-3.5-flash")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.tier, "mid")
        self.assertEqual(entry.provider, "Google")
        self.assertFalse(entry.experimental)

    def test_get_model_returns_none_for_unknown(self) -> None:
        self.assertIsNone(get_model("not/a-model"))

    def test_get_model_returns_model_entry_instance(self) -> None:
        entry = get_model("ibm-granite/granite-4.1-8b")
        self.assertIsInstance(entry, ModelEntry)


# ---------------------------------------------------------------------------
# Expansion model correctness
# ---------------------------------------------------------------------------


class TestExpansionModels(unittest.TestCase):
    _NEW_IDS = [
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.4",
        "openai/gpt-5.4-pro",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
        "openai/gpt-5-mini",
        "openai/gpt-5-nano",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-pro",
        "inclusionai/ring-2.6-1t",
        "minimax/minimax-m2.7",
        "inception/mercury-2",
    ]
    _VALID_COST = {"free", "tiny", "cheap", "mid", "expensive", "unknown"}
    _VALID_LATENCY = {"fast", "normal", "slow", "unknown"}

    def test_expansion_models_have_required_fields(self) -> None:
        for model_id in self._NEW_IDS:
            with self.subTest(model_id=model_id):
                entry = get_model(model_id)
                self.assertIsNotNone(entry, f"Not found: {model_id}")
                assert entry is not None
                self.assertTrue(entry.display_name)
                self.assertTrue(entry.provider)
                self.assertTrue(entry.tier)
                self.assertTrue(entry.roles)
                self.assertIn(entry.cost_class, self._VALID_COST)
                self.assertIn(entry.latency_class, self._VALID_LATENCY)
                self.assertIsInstance(entry.context_length, int)

    def test_low_cost_control_role_has_results(self) -> None:
        ids = [e.id for e in models_by_role("low_cost_control")]
        self.assertGreater(len(ids), 0)
        self.assertIn("openai/gpt-5-nano", ids)

    def test_cheap_control_role_includes_new_utility_models(self) -> None:
        ids = [e.id for e in models_by_role("cheap_control")]
        self.assertIn("google/gemini-3.1-flash-lite", ids)
        self.assertIn("openai/gpt-5-nano", ids)
        self.assertIn("openai/gpt-5.4-nano", ids)
        self.assertIn("openai/gpt-5-mini", ids)

    def test_value_worker_role_has_results(self) -> None:
        ids = [e.id for e in models_by_role("value_worker")]
        self.assertIn("deepseek/deepseek-v4-pro", ids)

    def test_gpt_oss_120b_is_reasoning_capable(self) -> None:
        self.assertTrue(supports("openai/gpt-oss-120b", "reasoning"))

    def test_kimi_k2_6_is_not_experimental(self) -> None:
        self.assertFalse(is_experimental("moonshotai/kimi-k2.6"))

    def test_gpt_oss_20b_is_experimental(self) -> None:
        self.assertTrue(is_experimental("openai/gpt-oss-20b"))

    def test_no_duplicate_model_ids_after_expansion(self) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for entry in _REGISTRY:
            if entry.id in seen:
                duplicates.add(entry.id)
            seen.add(entry.id)
        self.assertFalse(duplicates, f"Duplicate model IDs: {duplicates}")


# ---------------------------------------------------------------------------
# Curated roster update v1
# ---------------------------------------------------------------------------


class TestCuratedRosterV1(unittest.TestCase):
    _NEW_IDS = [
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.8-fast",
        "minimax/minimax-m3",
        "qwen/qwen3.7-plus",
    ]
    _VALID_COST = {"free", "tiny", "cheap", "mid", "expensive", "unknown"}
    _VALID_LATENCY = {"fast", "normal", "slow", "unknown"}

    def test_curated_models_present_with_required_fields(self) -> None:
        for model_id in self._NEW_IDS:
            with self.subTest(model_id=model_id):
                entry = get_model(model_id)
                self.assertIsNotNone(entry, f"Not found: {model_id}")
                assert entry is not None
                self.assertTrue(entry.display_name)
                self.assertTrue(entry.provider)
                self.assertTrue(entry.tier)
                self.assertTrue(entry.roles)
                self.assertIn(entry.cost_class, self._VALID_COST)
                self.assertIn(entry.latency_class, self._VALID_LATENCY)
                self.assertIsInstance(entry.context_length, int)
                self.assertFalse(entry.experimental)

    def test_curated_key_metadata(self) -> None:
        opus = get_model("anthropic/claude-opus-4.8")
        assert opus is not None
        self.assertEqual(opus.tier, "frontier")
        self.assertEqual(opus.context_length, 1_000_000)
        self.assertTrue(opus.supports_reasoning)
        self.assertTrue(supports("minimax/minimax-m3", "tools"))
        self.assertTrue(supports("qwen/qwen3.7-plus", "structured_outputs"))

    def test_curated_models_not_duplicated(self) -> None:
        ids = [e.id for e in _REGISTRY]
        for model_id in self._NEW_IDS:
            self.assertEqual(ids.count(model_id), 1, f"Dup/missing: {model_id}")

    def test_curated_modalities_limited_to_text_and_image(self) -> None:
        # input_modalities for this batch is deliberately limited to
        # text/image for registry consistency. OpenRouter reports file
        # input for the Opus 4.8 entries and video input for MiniMax M3;
        # those tokens are deferred to a future modality-schema cleanup
        # branch. This test documents the deferral as intentional.
        for model_id in self._NEW_IDS:
            entry = get_model(model_id)
            assert entry is not None
            self.assertEqual(entry.input_modalities, ("text", "image"))


class TestOpenAIModelFreshness(unittest.TestCase):
    def test_gpt56_luna_is_stable_and_sol_is_validated(self) -> None:
        luna = get_model("openai/gpt-5.6-luna")
        sol = get_model("openai/gpt-5.6-sol")
        assert luna is not None and sol is not None
        self.assertEqual(luna.lifecycle, "active_default")
        self.assertEqual(sol.lifecycle, "active_specialist")
        self.assertIn("cheap_control", luna.roles)
        self.assertIn("escalation", sol.roles)

    def test_gpt55_is_compatibility_only(self) -> None:
        for model_id in ("openai/gpt-5.5", "openai/gpt-5.5-pro"):
            entry = get_model(model_id)
            assert entry is not None
            self.assertEqual(entry.lifecycle, "fallback")
            self.assertNotIn("escalation", entry.roles)

    def test_gpt6_family_is_watchlisted_until_evaluated(self) -> None:
        for model_id in ("openai/gpt-6-astra", "openai/gpt-6-luna", "openai/gpt-6-sol"):
            entry = get_model(model_id)
            assert entry is not None
            self.assertEqual(entry.lifecycle, "watchlist")
            self.assertEqual(entry.roles, ())


if __name__ == "__main__":
    unittest.main()
