"""CLI integration tests for `openshard task new` / `openshard task attempts`."""

from __future__ import annotations

import json
import unittest

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.task_identity import is_task_id


class TestTaskNew(unittest.TestCase):
    def test_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "new"])
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_prints_well_formed_task_id(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "new"])
        self.assertIn("Task ID:", result.output)
        printed = result.output.splitlines()[0].split("Task ID:", 1)[1].strip()
        self.assertTrue(is_task_id(printed))

    def test_two_calls_mint_distinct_ids(self):
        runner = CliRunner()
        a = runner.invoke(cli, ["task", "new", "--json"])
        b = runner.invoke(cli, ["task", "new", "--json"])
        tid_a = json.loads(a.output)["task_id"]
        tid_b = json.loads(b.output)["task_id"]
        self.assertNotEqual(tid_a, tid_b)

    def test_json_output_well_formed(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "new", "--json"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        data = json.loads(result.output)
        self.assertEqual(data["command"], "task new")
        self.assertEqual(data["status"], "ok")
        self.assertTrue(is_task_id(data["task_id"]))


class TestTaskAttempts(unittest.TestCase):
    def test_unknown_task_id_reports_no_attempts(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            tid = json.loads(runner.invoke(cli, ["task", "new", "--json"]).output)["task_id"]
            result = runner.invoke(cli, ["task", "attempts", tid])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("No receipts found", result.output)

    def test_malformed_task_id_rejected(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "attempts", "not-well-formed"])
        self.assertNotEqual(result.exit_code, 0)

    def test_attempts_across_two_writers_are_grouped_by_task_id(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            tid = json.loads(runner.invoke(cli, ["task", "new", "--json"]).output)["task_id"]
            runner.invoke(cli, ["import", "claude", "--task", "first pass", "--task-id", tid])
            runner.invoke(cli, ["import", "claude", "--task", "second pass", "--task-id", tid])
            other = json.loads(runner.invoke(cli, ["task", "new", "--json"]).output)["task_id"]
            runner.invoke(cli, ["import", "claude", "--task", "unrelated", "--task-id", other])

            result = runner.invoke(cli, ["task", "attempts", tid, "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            self.assertEqual(len(data["receipts"]), 2)
            self.assertTrue(all(r["shard_id"] for r in data["receipts"]))


if __name__ == "__main__":
    unittest.main()
