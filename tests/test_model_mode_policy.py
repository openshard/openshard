from __future__ import annotations

import unittest

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.models.mode_policy import ModeModelPolicy, model_policy_for_mode
from openshard.models.registry import get_model
from openshard.tui.ask_mode import answer_ask_mode


class TestModeModePolicy(unittest.TestCase):
    # --- ask ---

    def test_ask_returns_policy(self) -> None:
        self.assertIsInstance(model_policy_for_mode("ask"), ModeModelPolicy)

    def test_ask_default_is_deepseek_v41_flash(self) -> None:
        policy = model_policy_for_mode("ask")
        self.assertEqual(policy.default_model_id, "deepseek/deepseek-v4.1-flash")

    def test_ask_default_exists_in_registry(self) -> None:
        policy = model_policy_for_mode("ask")
        self.assertIsNotNone(get_model(policy.default_model_id))

    def test_ask_fallback_ids_exist_in_registry(self) -> None:
        policy = model_policy_for_mode("ask")
        for mid in policy.fallback_model_ids:
            with self.subTest(model_id=mid):
                self.assertIsNotNone(get_model(mid), f"{mid} not in registry")

    def test_ask_advisory_only_true(self) -> None:
        self.assertTrue(model_policy_for_mode("ask").advisory_only)

    def test_ask_normalizes_uppercase(self) -> None:
        self.assertEqual(model_policy_for_mode("ASK"), model_policy_for_mode("ask"))

    def test_ask_normalizes_padded(self) -> None:
        self.assertEqual(model_policy_for_mode(" ask "), model_policy_for_mode("ask"))

    # --- plan ---

    def test_plan_returns_policy(self) -> None:
        self.assertIsInstance(model_policy_for_mode("plan"), ModeModelPolicy)

    def test_plan_default_is_deepseek_v4_pro(self) -> None:
        policy = model_policy_for_mode("plan")
        self.assertEqual(policy.default_model_id, "deepseek/deepseek-v4-pro")

    def test_plan_default_exists_in_registry(self) -> None:
        policy = model_policy_for_mode("plan")
        self.assertIsNotNone(get_model(policy.default_model_id))

    def test_plan_fallback_ids_exist_in_registry(self) -> None:
        policy = model_policy_for_mode("plan")
        for mid in policy.fallback_model_ids:
            with self.subTest(model_id=mid):
                self.assertIsNotNone(get_model(mid), f"{mid} not in registry")

    def test_plan_advisory_only_false(self) -> None:
        # Plan mode is now wired — advisory_only must be False.
        self.assertFalse(model_policy_for_mode("plan").advisory_only)

    def test_plan_includes_kimi_k2_6(self) -> None:
        policy = model_policy_for_mode("plan")
        self.assertIn("moonshotai/kimi-k2.6", policy.fallback_model_ids)

    # --- run ---

    def test_run_returns_none(self) -> None:
        self.assertIsNone(model_policy_for_mode("run"))

    def test_run_does_not_introduce_execution_default(self) -> None:
        # run routing stays with existing policy — no ModeModelPolicy should be returned
        self.assertIsNone(model_policy_for_mode("run"))

    # --- unknown ---

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(model_policy_for_mode("bogus"))

    def test_unknown_empty_returns_none(self) -> None:
        self.assertIsNone(model_policy_for_mode(""))

    # --- dataclass contract ---

    def test_returns_frozen_dataclass(self) -> None:
        policy = model_policy_for_mode("ask")
        with self.assertRaises((AttributeError, TypeError)):
            policy.mode = "tampered"  # type: ignore[misc]


class TestModelsModeCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    # --- ask ---

    def test_mode_ask_exits_zero(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "ask"])
        self.assertEqual(result.exit_code, 0)

    def test_mode_ask_includes_deepseek_v41_flash(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "ask"])
        self.assertIn("DeepSeek: V4.1 Flash", result.output)

    def test_mode_ask_includes_gpt5_nano(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "ask"])
        self.assertIn("GPT-5 Nano", result.output)

    def test_mode_ask_includes_gemini_flash_lite(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "ask"])
        self.assertIn("Gemini 3.1 Flash Lite", result.output)

    def test_mode_ask_says_advisory_only(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "ask"])
        self.assertIn("Advisory only", result.output)

    def test_mode_ask_normalizes_uppercase(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "ASK"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("DeepSeek: V4.1 Flash", result.output)

    # --- plan ---

    def test_mode_plan_exits_zero(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "plan"])
        self.assertEqual(result.exit_code, 0)

    def test_mode_plan_includes_deepseek_v4_pro(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "plan"])
        self.assertIn("DeepSeek V4 Pro", result.output)

    def test_mode_plan_includes_kimi_k2_6(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "plan"])
        self.assertIn("Kimi K2.6", result.output)

    def test_mode_plan_says_advisory_only(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "plan"])
        self.assertIn("Advisory only", result.output)

    # --- run ---

    def test_mode_run_exits_zero(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "run"])
        self.assertEqual(result.exit_code, 0)

    def test_mode_run_says_routing_policy(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "run"])
        self.assertIn("existing routing policy", result.output)

    def test_mode_run_has_no_default_model(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "run"])
        self.assertNotIn("Default", result.output)

    # --- unknown ---

    def test_mode_unknown_nonzero(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "bogus"])
        self.assertNotEqual(result.exit_code, 0)

    def test_mode_unknown_error_message(self) -> None:
        result = self.runner.invoke(cli, ["models", "mode", "bogus"])
        self.assertIn("Unknown mode", result.output)


class TestAskModeModelPolicy(unittest.TestCase):
    def test_model_policy_question_not_fallback(self) -> None:
        from openshard.tui.ask_mode import _ASK_FALLBACK

        result = answer_ask_mode("what model policy do you use")
        self.assertNotEqual(result, _ASK_FALLBACK)

    def test_model_policy_says_local_deterministic(self) -> None:
        result = answer_ask_mode("what model policy do you use")
        self.assertIn("local deterministic", result)

    def test_ask_mode_model_question(self) -> None:
        result = answer_ask_mode("what model would ask mode use")
        self.assertIn("local deterministic", result)

    def test_plan_mode_model_question(self) -> None:
        result = answer_ask_mode("what model would plan mode use")
        self.assertIn("local deterministic", result)

    def test_answer_mentions_ask_policy(self) -> None:
        result = answer_ask_mode("what model policy do you use")
        self.assertIn("Ask Mode", result)

    def test_answer_mentions_plan_policy(self) -> None:
        result = answer_ask_mode("what model policy do you use")
        self.assertIn("Plan Mode", result)

    def test_answer_mentions_run_routing(self) -> None:
        result = answer_ask_mode("what model policy do you use")
        self.assertIn("Run routing", result)

    def test_answer_is_string(self) -> None:
        result = answer_ask_mode("ask model policy")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)
