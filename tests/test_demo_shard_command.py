from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli


def _assert_no_unsafe(test: unittest.TestCase, text: str) -> None:
    """Demo output must never leak secrets or absolute paths."""
    for needle in ("C:\\", "C:/", "/Users/", "/home/", "sk-", "AKIA"):
        test.assertNotIn(needle, text, msg=f"unsafe substring {needle!r} leaked: {text}")


class TestDemoShardHuman(unittest.TestCase):

    def test_exits_zero_and_leads_with_header(self):
        result = CliRunner().invoke(cli, ["demo", "shard"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertTrue(result.output.startswith("OpenShard demo"), msg=result.output)

    def test_receipt_section(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        self.assertIn("Receipt:", out)
        self.assertIn("Task: Fix a failing test", out)
        self.assertIn("Status: completed", out)
        self.assertIn("Files changed: 1", out)
        self.assertIn("Verification: Passed (OpenShard ran the check(s))", out)

    def test_proof_section(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        self.assertIn("Proof:", out)
        self.assertIn("Status: usable", out)
        self.assertIn("Required proof: present", out)
        self.assertIn("Unsafe findings: none", out)

    def test_trust_section(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        self.assertIn("Trust:", out)
        self.assertIn("Band: good", out)
        self.assertIn("Score: 85/100", out)

    def test_mental_model_lines(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        self.assertIn("Receipt is what happened, and who observed it.", out)
        self.assertIn("Proof is whether the saved record is complete enough to use as evidence.", out)
        self.assertIn("Trust is a heuristic over the recorded proof signals, not a safety guarantee.", out)
        self.assertIn("A Shard is the saved proof record for one AI coding run.", out)

    def test_try_next_commands(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        self.assertIn("Try next:", out)
        self.assertIn("openshard last", out)
        self.assertIn("openshard proof last", out)
        self.assertIn("openshard trust last", out)

    def test_no_em_dash_in_output(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        self.assertNotIn("—", out)

    def test_no_unsafe_content(self):
        out = CliRunner().invoke(cli, ["demo", "shard"]).output
        _assert_no_unsafe(self, out)


class TestDemoShardJson(unittest.TestCase):

    def test_valid_json_only(self):
        result = CliRunner().invoke(cli, ["demo", "shard", "--json"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        data = json.loads(result.output)  # raises if not valid JSON-only
        self.assertEqual(data["command"], "demo shard")
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["demo"], True)
        self.assertTrue(data["shard_id"])

    def test_json_has_all_sections(self):
        data = json.loads(CliRunner().invoke(cli, ["demo", "shard", "--json"]).output)
        for key in ("run", "receipt", "proof_contract", "shard_quality", "trust", "next_commands"):
            self.assertIn(key, data)
        self.assertIsInstance(data["run"], dict)
        self.assertIsInstance(data["proof_contract"], dict)
        self.assertIsInstance(data["shard_quality"], dict)
        self.assertIsInstance(data["next_commands"], list)

    def test_json_receipt_compact(self):
        data = json.loads(CliRunner().invoke(cli, ["demo", "shard", "--json"]).output)
        receipt = data["receipt"]
        self.assertEqual(receipt["task"], "Fix a failing test")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["files_changed"], 1)
        self.assertEqual(receipt["verification"], "passed")

    def test_json_trust_band_good_score_in_range(self):
        data = json.loads(CliRunner().invoke(cli, ["demo", "shard", "--json"]).output)
        trust = data["trust"]
        self.assertEqual(trust["band"], "good")
        # Do not pin the exact score; trust scoring may evolve.
        self.assertIsInstance(trust["score"], int)
        self.assertGreaterEqual(trust["score"], 0)
        self.assertLessEqual(trust["score"], 100)

    def test_json_no_unsafe_content(self):
        out = CliRunner().invoke(cli, ["demo", "shard", "--json"]).output
        _assert_no_unsafe(self, out)

    def test_json_excludes_raw_content_keys(self):
        out = CliRunner().invoke(cli, ["demo", "shard", "--json"]).output
        for forbidden in ("raw_prompt", "raw_diff", "transcript", "stack_trace", "model_output"):
            self.assertNotIn(forbidden, out)


class TestDemoShardSafety(unittest.TestCase):

    def test_does_not_create_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["demo", "shard"])
            self.assertFalse((Path(".openshard") / "runs.jsonl").exists())

    def test_json_does_not_create_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["demo", "shard", "--json"])
            self.assertFalse((Path(".openshard") / "runs.jsonl").exists())

    def test_works_without_api_key(self):
        runner = CliRunner()
        env = {"ANTHROPIC_API_KEY": "", "OPENROUTER_API_KEY": "", "OPENAI_API_KEY": ""}
        result = runner.invoke(cli, ["demo", "shard"], env=env)
        self.assertEqual(result.exit_code, 0, msg=result.output)
        result_json = runner.invoke(cli, ["demo", "shard", "--json"], env=env)
        self.assertEqual(result_json.exit_code, 0, msg=result_json.output)


class TestDemoGroupRegression(unittest.TestCase):
    """Converting `demo` from a command to a group must preserve old behavior."""

    def test_bare_demo_exits_zero_with_header(self):
        result = CliRunner().invoke(cli, ["demo"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("OpenShard demo", result.output)

    def test_bare_demo_has_six_steps(self):
        out = CliRunner().invoke(cli, ["demo"]).output
        self.assertIn("1. Understand the task", out)
        self.assertIn("6. Capture feedback", out)

    def test_scenario_readonly(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "readonly"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Read-only tasks", result.output)

    def test_scenario_tier_dispatch(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "tier-dispatch"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Model plan / dispatch", result.output)

    def test_scenario_feedback(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "feedback"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Developer feedback capture", result.output)

    def test_invalid_scenario_rejected(self):
        result = CliRunner().invoke(cli, ["demo", "--scenario", "bad"])
        self.assertNotEqual(result.exit_code, 0)

    def test_demo_run_unchanged(self):
        result = CliRunner().invoke(cli, ["demo-run"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Task:", result.output)
        self.assertIn("Execution", result.output)


if __name__ == "__main__":
    unittest.main()
