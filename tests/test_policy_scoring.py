from __future__ import annotations

import unittest

from openshard.providers.base import ModelInfo
from openshard.providers.manager import InventoryEntry
from openshard.scoring.policy import EXACT_BONUS, FAMILY_BONUS, policy_bonus
from openshard.scoring.requirements import TaskRequirements
from openshard.scoring.scorer import score_model, select_candidate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(
    model_id: str,
    pricing_per_m: float = 1.0,
    context_window: int | None = None,
    supports_vision: bool = False,
    supports_tools: bool = False,
) -> InventoryEntry:
    """Build an InventoryEntry with per-million pricing expressed directly."""
    per_token = pricing_per_m / 1_000_000
    return InventoryEntry(
        provider="openrouter",
        model=ModelInfo(
            id=model_id,
            name=model_id,
            pricing={"prompt": str(per_token)},
            context_window=context_window,
            max_output_tokens=None,
            supports_vision=supports_vision,
            supports_tools=supports_tools,
        ),
    )


# ---------------------------------------------------------------------------
# Unit tests: policy_bonus()
# ---------------------------------------------------------------------------

class TestPolicyBonus(unittest.TestCase):

    def test_exact_match_returns_exact_bonus(self):
        self.assertEqual(
            policy_bonus("anthropic/claude-sonnet-4.6", "standard"),
            EXACT_BONUS,
        )

    def test_exact_match_boilerplate(self):
        self.assertEqual(
            policy_bonus("deepseek/deepseek-v4-flash", "boilerplate"),
            EXACT_BONUS,
        )

    def test_current_openai_frontier_is_preferred_for_complex_work(self):
        self.assertEqual(policy_bonus("openai/gpt-5.6-sol", "complex"), EXACT_BONUS)
        self.assertEqual(policy_bonus("openai/gpt-5.5", "complex"), 0.0)

    def test_exact_match_visual(self):
        self.assertEqual(
            policy_bonus("google/gemini-3.1-pro-preview", "visual"),
            EXACT_BONUS,
        )

    def test_family_match_returns_family_bonus(self):
        # "claude-sonnet-3.7" slug contains "claude-sonnet" family for standard
        self.assertEqual(
            policy_bonus("anthropic/claude-sonnet-3.7", "standard"),
            FAMILY_BONUS,
        )

    def test_family_match_deepseek_boilerplate(self):
        # A deepseek model not in the exact list still matches "deepseek" family
        self.assertEqual(
            policy_bonus("deepseek/deepseek-v3.2", "boilerplate"),
            FAMILY_BONUS,
        )

    def test_family_match_gemini_visual(self):
        self.assertEqual(
            policy_bonus("google/gemini-3.1-flash", "visual"),
            FAMILY_BONUS,
        )

    def test_no_match_returns_zero(self):
        # Gemini flash is not in the standard preferred list or families
        self.assertEqual(policy_bonus("google/gemini-3.1-flash-lite", "standard"), 0.0)

    def test_unknown_category_returns_zero(self):
        self.assertEqual(policy_bonus("anthropic/claude-sonnet-4.6", "unknown"), 0.0)

    def test_empty_category_returns_zero(self):
        self.assertEqual(policy_bonus("anthropic/claude-sonnet-4.6", ""), 0.0)

    def test_exact_beats_family_bonus_value(self):
        self.assertGreater(EXACT_BONUS, FAMILY_BONUS)


# ---------------------------------------------------------------------------
# Integration tests: score_model() with category
# ---------------------------------------------------------------------------

class TestScoreModelWithPolicy(unittest.TestCase):

    def test_exact_preferred_id_scores_higher_than_family_match(self):
        """claude-sonnet-4.6 (exact preferred) outscores claude-sonnet-3.7 (family)."""
        req = TaskRequirements()  # no hard filters, category passed separately
        preferred = _entry("anthropic/claude-sonnet-4.6", pricing_per_m=1.0)
        older = _entry("anthropic/claude-sonnet-3.7", pricing_per_m=1.0)
        self.assertGreater(
            score_model(preferred, req, "standard"),
            score_model(older, req, "standard"),
        )

    def test_older_cheaper_family_model_does_not_beat_exact_preferred(self):
        """Even when the older family model is cheaper (more cost bonus), the exact
        preferred ID still wins thanks to EXACT_BONUS > FAMILY_BONUS + cost delta."""
        req = TaskRequirements()
        # Older model: $0.10/M → cost bonus +3.0, family match +1.0 → total +4.0 extra
        older = _entry("anthropic/claude-sonnet-3.7", pricing_per_m=0.10)
        # Preferred: $1.00/M → cost bonus +2.0, exact match +3.0 → total +5.0 extra
        preferred = _entry("anthropic/claude-sonnet-4.6", pricing_per_m=1.0)
        self.assertGreater(
            score_model(preferred, req, "standard"),
            score_model(older, req, "standard"),
        )

    def test_unrelated_model_gets_no_policy_bonus(self):
        req = TaskRequirements()
        spine = _entry("anthropic/claude-sonnet-4.6", pricing_per_m=1.0)
        unrelated = _entry("google/gemini-3.1-flash-lite", pricing_per_m=1.0)
        diff = score_model(spine, req, "standard") - score_model(unrelated, req, "standard")
        self.assertEqual(diff, EXACT_BONUS)  # exactly EXACT_BONUS advantage

    def test_no_policy_bonus_when_category_empty(self):
        """score_model with default category="" applies no policy bonus."""
        req = TaskRequirements()
        preferred = _entry("anthropic/claude-sonnet-4.6", pricing_per_m=1.0)
        unrelated = _entry("some/other-model", pricing_per_m=1.0)
        # Both identical pricing and metadata → scores must be equal
        self.assertEqual(score_model(preferred, req), score_model(unrelated, req))


# ---------------------------------------------------------------------------
# Integration tests: select_candidate() with category
# ---------------------------------------------------------------------------

class TestSelectCandidatePolicy(unittest.TestCase):

    def test_exact_preferred_id_wins_over_family_match(self):
        req = TaskRequirements(preferred_max_cost_per_m=1.5)
        preferred = _entry("anthropic/claude-sonnet-4.6", pricing_per_m=1.0)
        older = _entry("anthropic/claude-sonnet-3.7", pricing_per_m=1.0)
        result = select_candidate([older, preferred], req, "standard")
        self.assertEqual(result.model.id, "anthropic/claude-sonnet-4.6")

    def test_preferred_model_wins_over_cheap_generic(self):
        """A spine model at moderate cost beats a cheap generic model with no policy bonus."""
        req = TaskRequirements(preferred_max_cost_per_m=2.0)
        # Cheap generic: $0.10/M → cost +3.0, no policy → total above base = +3.0
        generic = _entry("google/gemini-3.1-flash-lite", pricing_per_m=0.10)
        # Spine model: $0.50/M → cost +2.0, exact policy +3.0 → total above base = +5.0
        spine = _entry("deepseek/deepseek-v4-flash", pricing_per_m=0.50)
        result = select_candidate([generic, spine], req, "boilerplate")
        self.assertEqual(result.model.id, "deepseek/deepseek-v4-flash")

    def test_hard_filter_eliminates_preferred_model(self):
        """A preferred model that fails a hard filter is excluded; the non-preferred
        model that passes the filter wins — policy bonus cannot override hard filters."""
        # standard cost cap: $1.5/M
        req = TaskRequirements(preferred_max_cost_per_m=1.5)
        # Preferred spine model but priced above the cost cap → hard-filtered out
        expensive_spine = _entry("anthropic/claude-sonnet-4.6", pricing_per_m=3.0)
        # Non-preferred cheap generic that passes the cap
        cheap_generic = _entry("google/gemini-3.1-flash-lite", pricing_per_m=0.10)
        result = select_candidate([expensive_spine, cheap_generic], req, "standard")
        self.assertEqual(result.model.id, "google/gemini-3.1-flash-lite")

    def test_family_fallback_wins_when_no_exact_id_present(self):
        """When no exact preferred ID is in the pool, the family-matching model still
        scores higher than a completely unrelated model."""
        req = TaskRequirements()
        # Family match: "claude-sonnet-3.7" slug contains "claude-sonnet"
        family_model = _entry("anthropic/claude-sonnet-3.7", pricing_per_m=1.0)
        # No match at all
        unrelated = _entry("google/gemini-3.1-flash-lite", pricing_per_m=1.0)
        result = select_candidate([family_model, unrelated], req, "standard")
        self.assertEqual(result.model.id, "anthropic/claude-sonnet-3.7")

    def test_visual_preferred_beats_generic_vision_model(self):
        """In the visual category, the preferred gemini-3.1-pro-preview beats a
        generic vision model that only earns the vision capability bonus."""
        req = TaskRequirements(needs_vision=True)
        preferred_visual = _entry(
            "google/gemini-3.1-pro-preview",
            pricing_per_m=1.0,
            supports_vision=True,
        )
        generic_vision = _entry(
            "some/vision-model",
            pricing_per_m=1.0,
            supports_vision=True,
        )
        result = select_candidate([generic_vision, preferred_visual], req, "visual")
        self.assertEqual(result.model.id, "google/gemini-3.1-pro-preview")


if __name__ == "__main__":
    unittest.main()
