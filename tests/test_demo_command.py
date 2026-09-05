from __future__ import annotations

import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli


class TestDemoCommand(unittest.TestCase):

    def test_demo_exits_zero(self):
        result = CliRunner().invoke(cli, ["demo"])
        self.assertEqual(result.exit_code, 0)

    def test_demo_default_contains_header(self):
        result = CliRunner().invoke(cli, ["demo"])
        self.assertIn("OpenShard demo", result.output)

    def test_demo_default_contains_all_six_steps(self):
        result = CliRunner().invoke(cli, ["demo"])
        self.assertIn("1. Understand the task", result.output)
        self.assertIn("2. Choose a workflow", result.output)
        self.assertIn("3. Choose models", result.output)
        self.assertIn("4. Protect files", result.output)
        self.assertIn("5. Record the run", result.output)
        self.assertIn("6. Capture feedback", result.output)

    def test_scenario_readonly_explains_readonly_tasks(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "readonly"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Read-only tasks", result.output)

    def test_scenario_readonly_explains_file_protection(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "readonly"])
        self.assertIn("File protection behaviour", result.output)

    def test_scenario_tier_dispatch_explains_model_plan(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "tier-dispatch"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Model plan / dispatch", result.output)

    def test_scenario_tier_dispatch_mentions_work_model(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "tier-dispatch"])
        self.assertIn("work model", result.output)

    def test_scenario_feedback_explains_developer_feedback(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "feedback"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Developer feedback capture", result.output)

    def test_scenario_feedback_mentions_real_subcommands(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "feedback"])
        self.assertIn("openshard feedback accept", result.output)
        self.assertIn("openshard feedback retry --reason", result.output)
        self.assertIn("openshard feedback reject --reason", result.output)
        self.assertNotIn("--rating", result.output)
        self.assertNotIn("--outcome", result.output)

    def test_invalid_scenario_rejected(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "bad"])
        self.assertNotEqual(result.exit_code, 0)

    def test_demo_does_not_create_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["demo"])
            self.assertFalse((Path(".openshard") / "runs.jsonl").exists())

    def test_demo_scenario_does_not_create_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["demo", "--scenario", "readonly"])
            self.assertFalse((Path(".openshard") / "runs.jsonl").exists())

    def test_default_output_no_forbidden_labels(self):
        result = CliRunner().invoke(cli, ["demo"])
        for label in ("[routing]", "[profile]", "[verification]"):
            self.assertNotIn(label, result.output)

    def test_all_scenarios_no_forbidden_labels(self):
        runner = CliRunner()
        forbidden = ["[routing]", "[profile]", "[verification]"]
        for scenario in ["readonly", "tier-dispatch", "feedback"]:
            result = runner.invoke(cli, ["demo", "--scenario", scenario])
            for label in forbidden:
                self.assertNotIn(
                    label,
                    result.output,
                    msg=f"Found '{label}' in --scenario {scenario} output",
                )


class TestDemoRunCommand(unittest.TestCase):

    def test_demo_run_exits_zero(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertEqual(result.exit_code, 0)

    def test_demo_run_shows_task(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Task:", result.output)

    def test_demo_run_shows_execution(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Execution", result.output)
        self.assertIn("Mode: Run", result.output)

    def test_demo_run_shows_routing(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Routing", result.output)
        self.assertIn("Initial candidate:", result.output)

    def test_demo_run_shows_model_plan(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Model plan", result.output)

    def test_demo_run_shows_dispatch(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Dispatch", result.output)

    def test_demo_run_shows_verification(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Verification", result.output)

    def test_demo_run_shows_feedback(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertIn("Feedback", result.output)

    def test_demo_run_does_not_create_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["demo-run"])
            self.assertFalse((Path(".openshard") / "runs.jsonl").exists())

    def test_demo_run_no_forbidden_labels(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        for label in ("[routing]", "[profile]", "[verification]"):
            self.assertNotIn(label, result.output)


if __name__ == "__main__":
    unittest.main()
