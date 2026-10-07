from __future__ import annotations

import unittest

from openshard.providers.base import ModelInfo
from openshard.providers.manager import InventoryEntry
from openshard.scoring.requirements import TaskRequirements, requirements_from_category
from openshard.scoring.scorer import score_model, select_candidate, select_with_info


def _entry(
    id="m1",
    context_window=None,
    supports_vision=False,
    supports_tools=False,
    pricing=None,
) -> InventoryEntry:
    return InventoryEntry(
        provider="openrouter",
        model=ModelInfo(
            id=id,
            name=id,
            pricing=pricing or {},
            context_window=context_window,
            supports_vision=supports_vision,
            supports_tools=supports_tools,
        ),
    )


class TestScoreModel(unittest.TestCase):

    def test_base_score(self):
        entry = _entry("basic")
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 10.0)

    def test_vision_bonus_when_needed(self):
        entry = _entry("vision", supports_vision=True)
        req = TaskRequirements(needs_vision=True)
        self.assertEqual(score_model(entry, req), 12.0)

    def test_no_vision_bonus_when_not_needed(self):
        entry = _entry("vision", supports_vision=True)
        req = TaskRequirements(needs_vision=False)
        self.assertEqual(score_model(entry, req), 10.0)

    def test_security_claude_bonus(self):
        entry = _entry("anthropic/claude-sonnet-4.6")
        req = TaskRequirements(security_sensitive=True)
        self.assertEqual(score_model(entry, req), 12.0)

    def test_security_prefers_opus_over_haiku(self):
        opus = _entry("anthropic/claude-opus-4.7")
        haiku = _entry("anthropic/claude-haiku-4.5", pricing={"prompt": "0.0000001"})
        req = TaskRequirements(security_sensitive=True)
        self.assertGreater(score_model(opus, req), score_model(haiku, req))

    def test_security_ignores_cheap_bonus(self):
        entry = _entry("anthropic/claude-haiku-4.5", pricing={"prompt": "0.0000001"})
        req = TaskRequirements(security_sensitive=True)
        self.assertEqual(score_model(entry, req), 8.0)

    def test_standard_tasks_still_prefer_cheap_models(self):
        cheap = _entry("cheap-model", pricing={"prompt": "0.0000001"})
        pricey = _entry("pricey-model", pricing={"prompt": "0.000005"})
        req = TaskRequirements()
        self.assertGreater(score_model(cheap, req), score_model(pricey, req))

    def test_security_bonus_not_applied_to_non_claude(self):
        entry = _entry("openai/gpt-4o")
        req = TaskRequirements(security_sensitive=True)
        self.assertEqual(score_model(entry, req), 10.0)

    def test_context_window_100k_bonus(self):
        entry = _entry("big", context_window=128_000)
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 11.0)

    def test_context_window_200k_bonus(self):
        entry = _entry("huge", context_window=200_000)
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 12.0)

    def test_cheap_cost_bonus(self):
        # $0.10/M = 0.0000001 per token; qualifies for both <=1.0 (+2) and <=0.25 (+1) bonuses
        entry = _entry("cheap", pricing={"prompt": "0.0000001"})
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 13.0)

    def test_expensive_model_no_cost_bonus(self):
        # $5.00/M = 0.000005 per token; no bonus
        entry = _entry("pricey", pricing={"prompt": "0.000005"})
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 10.0)


    def test_alias_id_penalised(self):
        entry = _entry("~anthropic/claude-opus-latest")
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 9.0)

    def test_versioned_id_beats_alias_when_otherwise_equal(self):
        versioned = _entry("anthropic/claude-opus-4.7")
        alias = _entry("~anthropic/claude-opus-latest")
        req = TaskRequirements()
        self.assertGreater(score_model(versioned, req), score_model(alias, req))

    def test_non_alias_id_unaffected(self):
        entry = _entry("anthropic/claude-opus-4.7")
        req = TaskRequirements()
        self.assertEqual(score_model(entry, req), 10.0)


class TestSelectCandidate(unittest.TestCase):

    def test_empty_list_returns_none(self):
        req = TaskRequirements()
        self.assertIsNone(select_candidate([], req))

    def test_returns_top_scorer(self):
        # $0.10/M cheap model beats $5.00/M expensive model via scoring bonuses
        cheap = _entry("cheap", pricing={"prompt": "0.0000001"})
        pricey = _entry("pricey", pricing={"prompt": "0.000005"})
        req = TaskRequirements()
        result = select_candidate([cheap, pricey], req)
        self.assertEqual(result.model.id, "cheap")

    def test_returns_none_when_all_filtered(self):
        entry = _entry("no-vision", supports_vision=False)
        req = TaskRequirements(needs_vision=True)
        self.assertIsNone(select_candidate([entry], req))


class TestBoilerplateScoring(unittest.TestCase):

    def _make(self, model_id: str, prompt_per_token: str, ctx: int) -> InventoryEntry:
        return InventoryEntry(
            provider="openrouter",
            model=ModelInfo(
                id=model_id,
                name=model_id,
                pricing={"prompt": prompt_per_token},
                context_window=ctx,
                supports_vision=False,
                supports_tools=False,
            ),
        )

    def test_v4_flash_beats_minimax_for_boilerplate(self):
        v4_flash = self._make("deepseek/deepseek-v4-flash", "0.00000014", 1_048_576)
        minimax = self._make("minimax/minimax-m2.7", "0.0000003", 196_608)
        req = requirements_from_category("boilerplate")
        winner = select_candidate([v4_flash, minimax], req, category="boilerplate")
        self.assertIsNotNone(winner)
        self.assertEqual(winner.model.id, "deepseek/deepseek-v4-flash")

    def test_v4_flash_beats_v3_2_for_boilerplate(self):
        v4_flash = self._make("deepseek/deepseek-v4-flash", "0.00000014", 1_048_576)
        v3_2 = self._make("deepseek/deepseek-v3.2", "0.000000252", 131_072)
        req = requirements_from_category("boilerplate")
        winner = select_candidate([v4_flash, v3_2], req, category="boilerplate")
        self.assertIsNotNone(winner)
        self.assertEqual(winner.model.id, "deepseek/deepseek-v4-flash")


class TestSelectWithInfoHistoryAdjustments(unittest.TestCase):

    def _make(self, model_id: str, prompt_per_token: str = "0.000001") -> InventoryEntry:
        return InventoryEntry(
            provider="openrouter",
            model=ModelInfo(
                id=model_id,
                name=model_id,
                pricing={"prompt": prompt_per_token},
                context_window=None,
                supports_vision=False,
                supports_tools=False,
            ),
        )

    def test_none_adjustments_is_noop(self):
        e1 = self._make("m/alpha")
        e2 = self._make("m/beta")
        req = TaskRequirements()
        result_without = select_with_info([e1, e2], req, "standard")
        result_with_none = select_with_info([e1, e2], req, "standard", history_adjustments=None)
        self.assertEqual(result_without.selected_model, result_with_none.selected_model)
        self.assertEqual(result_without.scores, result_with_none.scores)

    def test_history_adjustment_changes_winner_when_scores_close(self):
        # Both models start with identical base scores; history tips the balance.
        e1 = self._make("m/alpha")
        e2 = self._make("m/beta")
        req = TaskRequirements()
        # Without adjustment: same score, stable ordering
        _baseline = select_with_info([e1, e2], req, "standard")
        # Give m/beta a +1.0 history bonus → it should win
        adjustments = {"m/alpha": 0.0, "m/beta": 1.0}
        result = select_with_info([e1, e2], req, "standard", history_adjustments=adjustments)
        self.assertEqual(result.selected_model, "m/beta")
        # Raw scores still reflect unadjusted values
        self.assertAlmostEqual(result.scores_raw["m/alpha"], result.scores_raw["m/beta"])
        # Final scores reflect the adjustment
        self.assertGreater(result.scores["m/beta"], result.scores["m/alpha"])

    def test_history_bonus_cannot_beat_exact_policy_preference(self):
        # m/preferred holds the +3.0 exact-match policy bonus for "boilerplate".
        # m/other gets a +1.0 history bonus. The policy winner must still win.
        preferred = self._make("deepseek/deepseek-v4.1-flash", prompt_per_token="0.00000014")
        other = self._make("z-ai/glm-5.1", prompt_per_token="0.000001")
        req = requirements_from_category("boilerplate")
        adjustments = {"deepseek/deepseek-v4.1-flash": 0.0, "z-ai/glm-5.1": 1.0}
        result = select_with_info([preferred, other], req, "boilerplate", history_adjustments=adjustments)
        self.assertEqual(result.selected_model, "deepseek/deepseek-v4.1-flash")

    def test_scores_raw_empty_when_no_adjustments(self):
        e1 = self._make("m/a")
        req = TaskRequirements()
        result = select_with_info([e1], req, "standard", history_adjustments=None)
        self.assertEqual(result.scores_raw, {})
        self.assertEqual(result.history_adjustments, {})

    def test_scores_raw_populated_when_adjustments_given(self):
        e1 = self._make("m/a")
        req = TaskRequirements()
        result = select_with_info([e1], req, "standard", history_adjustments={"m/a": 0.5})
        self.assertIn("m/a", result.scores_raw)
        self.assertIn("m/a", result.history_adjustments)
        self.assertAlmostEqual(result.history_adjustments["m/a"], 0.5)
        self.assertAlmostEqual(result.scores["m/a"], result.scores_raw["m/a"] + 0.5)
