"""Model lifecycle tags v1 completeness and safety tests.

Every ModelEntry must carry a valid lifecycle value, and routing default
eligibility must be derivable from lifecycle alone. This branch adds metadata
only: routing/engine.py is unchanged and does not read these fields. These tests
pin the contract that experimental/watchlist/deprecated (and fallback /
open_weight) models are never default-routing eligible.
"""

from __future__ import annotations

import unittest

from openshard.models.registry import (
    _REGISTRY,
    LIFECYCLE_VALUES,
    ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES,
    is_routing_default_eligible,
    lifecycle_for,
    models_by_lifecycle,
)


class TestLifecycleValues(unittest.TestCase):
    def test_every_entry_lifecycle_is_valid(self) -> None:
        for entry in _REGISTRY:
            with self.subTest(model_id=entry.id):
                self.assertIn(entry.lifecycle, LIFECYCLE_VALUES)

    def test_every_entry_lifecycle_is_str(self) -> None:
        for entry in _REGISTRY:
            with self.subTest(model_id=entry.id):
                self.assertIsInstance(entry.lifecycle, str)

    def test_registry_size_matches_curated_roster(self) -> None:
        # Six current OpenAI family entries were added in the model-freshness update.
        self.assertEqual(len(_REGISTRY), 49)


class TestRoutingDefaultEligibility(unittest.TestCase):
    def test_eligible_iff_lifecycle_in_eligible_set(self) -> None:
        for entry in _REGISTRY:
            with self.subTest(model_id=entry.id):
                expected = entry.lifecycle in ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES
                self.assertEqual(is_routing_default_eligible(entry.id), expected)

    def test_only_active_default_is_eligible(self) -> None:
        # Locks the v1 derivation: active_default is the only eligible stage.
        self.assertEqual(
            ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES, frozenset({"active_default"})
        )

    def test_non_default_stages_are_never_eligible(self) -> None:
        non_default = {
            "active_specialist",
            "fallback",
            "open_weight",
            "experimental",
            "watchlist",
            "deprecated",
        }
        for entry in _REGISTRY:
            if entry.lifecycle in non_default:
                with self.subTest(model_id=entry.id):
                    self.assertFalse(is_routing_default_eligible(entry.id))

    def test_eligible_models_are_not_experimental_flag(self) -> None:
        # Defensive cross-check: nothing routable by default is still flagged
        # experimental under the old bool.
        for entry in _REGISTRY:
            if is_routing_default_eligible(entry.id):
                with self.subTest(model_id=entry.id):
                    self.assertFalse(entry.experimental)


class TestLifecycleHelpers(unittest.TestCase):
    def test_lifecycle_for_known_and_unknown(self) -> None:
        self.assertEqual(
            lifecycle_for("anthropic/claude-haiku-4.5"), "active_default"
        )
        self.assertIsNone(lifecycle_for("does/not-exist"))

    def test_is_routing_default_eligible_unknown_is_false(self) -> None:
        self.assertFalse(is_routing_default_eligible("does/not-exist"))

    def test_models_by_lifecycle_partitions_registry(self) -> None:
        total = sum(
            len(models_by_lifecycle(value)) for value in LIFECYCLE_VALUES
        )
        self.assertEqual(total, len(_REGISTRY))

    def test_models_by_lifecycle_unknown_is_empty(self) -> None:
        self.assertEqual(models_by_lifecycle("not_a_stage"), [])


class TestLifecycleClassifications(unittest.TestCase):
    def test_spot_check_classifications(self) -> None:
        cases = {
            "anthropic/claude-haiku-4.5": "active_default",
            "~anthropic/claude-haiku-latest": "fallback",
            "x-ai/grok-4.3": "active_default",
            "mistralai/codestral-2508": "active_specialist",
            "openai/gpt-oss-120b": "open_weight",
            "minimax/minimax-m2.7": "watchlist",
            "x-ai/grok-build-0.1": "experimental",
            "anthropic/claude-fable-5": "active_specialist",
            "anthropic/claude-mythos-5": "watchlist",
            "openai/gpt-5.6-luna": "active_default",
            "openai/gpt-5.6-sol": "active_specialist",
            "openai/gpt-5.6-terra": "watchlist",
            "openai/gpt-6-sol": "watchlist",
            "openai/gpt-5.5": "fallback",
        }
        for model_id, expected in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(lifecycle_for(model_id), expected)


class TestNewAnthropicModelsRoutingSafety(unittest.TestCase):
    """Prove Fable 5 and Mythos 5 cannot enter any default routing path."""

    def test_fable5_not_default_eligible(self) -> None:
        self.assertFalse(is_routing_default_eligible("anthropic/claude-fable-5"))

    def test_mythos5_not_default_eligible(self) -> None:
        self.assertFalse(is_routing_default_eligible("anthropic/claude-mythos-5"))

    def test_fable5_lifecycle_is_active_specialist(self) -> None:
        self.assertEqual(lifecycle_for("anthropic/claude-fable-5"), "active_specialist")

    def test_mythos5_lifecycle_is_watchlist(self) -> None:
        self.assertEqual(lifecycle_for("anthropic/claude-mythos-5"), "watchlist")

    def test_mythos5_not_in_active_default_pool(self) -> None:
        ids = {e.id for e in models_by_lifecycle("active_default")}
        self.assertNotIn("anthropic/claude-mythos-5", ids)

    def test_mythos5_not_in_active_specialist_pool(self) -> None:
        ids = {e.id for e in models_by_lifecycle("active_specialist")}
        self.assertNotIn("anthropic/claude-mythos-5", ids)


if __name__ == "__main__":
    unittest.main()
