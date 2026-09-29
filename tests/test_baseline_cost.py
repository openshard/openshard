from __future__ import annotations

import unittest
from unittest.mock import patch

from openshard.cost.baseline import (
    BASELINE_MODELS,
    FULL_COMPARISON_MODELS,
    compute_baseline_comparison,
    format_baseline_line,
    format_cost_difference,
    format_full_comparison_lines,
)


class TestFormatBaselineLine(unittest.TestCase):

    # --- Guard conditions ---

    def test_returns_none_when_both_tokens_zero(self):
        self.assertIsNone(format_baseline_line(0, 0))

    def test_returns_none_when_all_models_unresolvable(self):
        with patch("openshard.cost.baseline.compute_cost", return_value=None):
            self.assertIsNone(format_baseline_line(1_000, 500))

    # --- Label format ---

    def test_starts_with_baseline_estimate_label(self):
        result = format_baseline_line(1_000_000, 1_000_000)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("Baseline estimate: "))

    def test_contains_gpt56_sol_label(self):
        result = format_baseline_line(1_000_000, 1_000_000)
        self.assertIn("GPT-5.6 Sol", result)

    def test_contains_sonnet46_label(self):
        result = format_baseline_line(1_000_000, 1_000_000)
        self.assertIn("Sonnet 4.6", result)

    def test_models_separated_by_comma_space(self):
        result = format_baseline_line(1_000_000, 1_000_000)
        self.assertIn(", ", result)

    # --- Numeric correctness ---

    def test_sonnet46_cost_computed_correctly(self):
        # anthropic/claude-sonnet-4.6: (3.00, 15.00) per million
        # 500k prompt + 500k completion = 1.50 + 7.50 = $9.000
        result = format_baseline_line(500_000, 500_000)
        self.assertIn("Sonnet 4.6 $9.000", result)

    def test_gpt56_sol_cost_computed_correctly(self):
        # openai/gpt-5.6-sol: (2.00, 10.00) per million
        # 500k prompt + 500k completion = 1.00 + 5.00 = $6.000
        result = format_baseline_line(500_000, 500_000)
        self.assertIn("GPT-5.6 Sol $6.000", result)

    def test_three_decimal_places(self):
        import re
        result = format_baseline_line(1_000, 1_000)
        self.assertIsNotNone(result)
        prices = re.findall(r"\$(\d+\.\d+)", result)
        self.assertGreater(len(prices), 0)
        for p in prices:
            self.assertEqual(len(p.split(".")[1]), 3)

    # --- Multiplier: absent without actual_cost ---

    def test_no_multiplier_when_actual_cost_none(self):
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=None)
        self.assertNotIn("x higher", result)

    def test_no_multiplier_when_actual_cost_zero(self):
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=0.0)
        self.assertNotIn("x higher", result)

    def test_no_multiplier_when_baseline_cheaper_than_actual(self):
        # 1k/1k → Sonnet 4.6 ≈ $0.018, GPT-5.6 Sol ≈ $0.035; actual $1.00 → both ratios ≈ 0.02–0.04 (<1.05)
        result = format_baseline_line(1_000, 1_000, actual_cost=1.0)
        self.assertIsNotNone(result)
        self.assertNotIn("x higher", result)

    def test_no_multiplier_when_ratio_nearly_equal(self):
        # Sonnet 4.6 at 1k/1k = $0.018; actual $0.0175 → ratio ≈ 1.03 (<1.05, omit suffix)
        custom = [("Sonnet 4.6", "anthropic/claude-sonnet-4.6")]
        result = format_baseline_line(1_000, 1_000, actual_cost=0.0175, models=custom)
        self.assertIsNotNone(result)
        self.assertNotIn("x higher", result)

    def test_multiplier_appears_for_meaningful_higher_baseline(self):
        # 1M/1M → Sonnet 4.6 = $18; actual $10 → ratio 1.8x (>1.05), suffix shown
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=10.0)
        self.assertIsNotNone(result)
        self.assertIn("x higher", result)

    # --- Multiplier: present with actual_cost ---

    def test_multiplier_present_when_actual_cost_positive(self):
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=0.001)
        self.assertIn("x higher", result)

    def test_multiplier_whole_number_at_or_above_10x(self):
        # Sonnet 4.6 at 1M/1M tokens = $18.00; actual = $1.00 → ratio 18x (>=10)
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=1.0)
        # Should use round(), not .1f
        self.assertIn("~18x higher", result)

    def test_multiplier_one_decimal_below_10x(self):
        # Sonnet 4.6 at 1M/1M = $18.00; actual = $5.00 → ratio 3.6x (<10)
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=5.0)
        self.assertIn("~3.6x higher", result)

    def test_multiplier_in_parens_after_price(self):
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=0.01)
        # Each entry should look like "Label $X.XXX (~Nx higher)"
        self.assertRegex(result, r"\$\d+\.\d{3} \(~[\d.]+x higher\)")

    # --- Partial resolution ---

    def test_partial_model_resolution_still_renders(self):
        def fake_compute(model, pt, ct):
            if "gpt" in model:
                return None
            from openshard.providers.openrouter import compute_cost as real
            return real(model, pt, ct)

        with patch("openshard.cost.baseline.compute_cost", side_effect=fake_compute):
            result = format_baseline_line(1_000_000, 1_000_000)
        self.assertIsNotNone(result)
        self.assertIn("Sonnet 4.6", result)
        self.assertNotIn("GPT-5.6 Sol", result)

    # --- Custom models override ---

    def test_custom_models_parameter_respected(self):
        custom = [("Custom", "anthropic/claude-sonnet-4.6")]
        result = format_baseline_line(1_000_000, 1_000_000, models=custom)
        self.assertIn("Custom", result)
        self.assertNotIn("GPT-5.6 Sol", result)

    # --- No prohibited language ---

    def test_no_savings_claim(self):
        result = format_baseline_line(1_000_000, 1_000_000, actual_cost=0.01)
        self.assertNotIn("save", result.lower())
        self.assertNotIn("%", result)
        self.assertNotIn("cheaper", result.lower())


class TestBaselineModelsConstant(unittest.TestCase):

    def test_has_two_entries(self):
        self.assertEqual(len(BASELINE_MODELS), 2)

    def test_gpt56_sol_is_first(self):
        self.assertEqual(BASELINE_MODELS[0][1], "openai/gpt-5.6-sol")

    def test_sonnet46_is_second(self):
        self.assertEqual(BASELINE_MODELS[1][1], "anthropic/claude-sonnet-4.6")


class TestComputeBaselineComparison(unittest.TestCase):

    def test_returns_none_when_tokens_zero(self):
        self.assertIsNone(compute_baseline_comparison(0, 0, 0.01))

    def test_returns_none_when_actual_cost_none(self):
        self.assertIsNone(compute_baseline_comparison(1_000, 500, None))

    def test_returns_correct_dict(self):
        # anthropic/claude-sonnet-4.6: (3.00, 15.00) per million
        # 500k prompt + 500k completion = 1.50 + 7.50 = $9.000
        result = compute_baseline_comparison(500_000, 500_000, 1.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["actual_cost_usd"], 1.0)
        self.assertAlmostEqual(result["frontier_baseline_cost_usd"], 9.0)
        self.assertAlmostEqual(result["estimated_saving_usd"], 8.0)
        # round(8.0 / 9.0 * 100) = round(88.89) = 89
        self.assertEqual(result["estimated_saving_percent"], 89)

    def test_saving_percent_none_when_actual_cost_zero(self):
        result = compute_baseline_comparison(500_000, 500_000, 0.0)
        self.assertIsNotNone(result)
        self.assertIsNone(result["estimated_saving_percent"])
        self.assertAlmostEqual(result["frontier_baseline_cost_usd"], 9.0)
        self.assertAlmostEqual(result["estimated_saving_usd"], 9.0)

    def test_returns_none_when_model_unresolvable(self):
        with patch("openshard.cost.baseline.compute_cost", return_value=None):
            self.assertIsNone(compute_baseline_comparison(1_000, 500, 0.01))


class TestFormatCostDifference(unittest.TestCase):

    def test_cheaper_contains_cheaper(self):
        self.assertIn("cheaper", format_cost_difference(1.0, 9.0))

    def test_cheaper_contains_pct(self):
        self.assertIn("%", format_cost_difference(1.0, 9.0))

    def test_cheaper_contains_x_lower(self):
        self.assertIn("lower cost", format_cost_difference(1.0, 9.0))

    def test_more_expensive_contains_phrase(self):
        self.assertIn("more expensive", format_cost_difference(9.0, 1.0))

    def test_more_expensive_contains_x_higher(self):
        self.assertIn("higher cost", format_cost_difference(9.0, 1.0))

    def test_equal_returns_equal(self):
        self.assertEqual("equal", format_cost_difference(5.0, 5.0))

    def test_zero_actual_with_positive_baseline(self):
        result = format_cost_difference(0.0, 5.0)
        self.assertIn("cheaper", result)
        self.assertIn("100%", result)
        self.assertNotIn("x lower", result)

    def test_baseline_zero_returns_equal(self):
        self.assertEqual("equal", format_cost_difference(5.0, 0.0))

    def test_x_10x_uses_integer_format(self):
        # 0.1 vs 100.0 → x = 1000, should be "1000x" not "1000.0x"
        result = format_cost_difference(0.1, 100.0)
        self.assertIn("1000x", result)
        self.assertNotIn("1000.0x", result)

    def test_pct_uses_baseline_as_denominator_cheaper(self):
        # saving=1.0, baseline=2.0, pct = round(1/2*100) = 50
        result = format_cost_difference(1.0, 2.0)
        self.assertIn("50%", result)

    def test_pct_uses_baseline_as_denominator_more_expensive(self):
        # excess=1.0, baseline=1.0, pct = round(1/1*100) = 100
        result = format_cost_difference(2.0, 1.0)
        self.assertIn("100%", result)


class TestFullComparisonModels(unittest.TestCase):

    def test_full_comparison_models_includes_sonnet46(self):
        ids = [m for _, m in FULL_COMPARISON_MODELS]
        self.assertTrue(any("claude-sonnet-4.6" in m for m in ids))

    def test_full_comparison_models_includes_gpt56_sol(self):
        ids = [m for _, m in FULL_COMPARISON_MODELS]
        self.assertTrue(any("gpt-5.5" in m for m in ids))

    def test_full_comparison_models_includes_opus48(self):
        ids = [m for _, m in FULL_COMPARISON_MODELS]
        self.assertTrue(any("claude-opus-4.8" in m for m in ids))


class TestFormatFullComparisonLines(unittest.TestCase):

    def test_returns_list_of_strings(self):
        result = format_full_comparison_lines(500_000, 500_000, 1.0)
        self.assertIsInstance(result, list)
        for row in result:
            self.assertIsInstance(row, str)

    def test_sonnet46_row_present(self):
        result = format_full_comparison_lines(500_000, 500_000, 1.0)
        self.assertTrue(any("Sonnet 4.6" in row for row in result))

    def test_each_row_contains_dollar(self):
        result = format_full_comparison_lines(500_000, 500_000, 1.0)
        self.assertGreater(len(result), 0)
        for row in result:
            self.assertIn("$", row)

    def test_skips_unpriced_model(self):
        def fake_compute(model_id, pt, ct):
            if "gpt" in model_id:
                return None
            from openshard.providers.openrouter import compute_cost as real
            return real(model_id, pt, ct)

        with patch("openshard.cost.baseline.compute_cost", side_effect=fake_compute):
            result = format_full_comparison_lines(500_000, 500_000, 1.0)
        self.assertFalse(any("GPT-5.6 Sol" in row for row in result))
        self.assertTrue(any("Sonnet 4.6" in row for row in result))
