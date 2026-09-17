"""CLI integration tests for openshard import claude."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Basic write path
# ---------------------------------------------------------------------------

class TestImportClaudeWritePath(unittest.TestCase):

    def test_creates_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix the login bug"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertTrue(Path(".openshard/runs.jsonl").exists())

    def test_writes_one_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Refactor auth"])
            entries = _load_jsonl(Path(".openshard/runs.jsonl"))
            self.assertEqual(len(entries), 1)

    def test_import_source_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Update README"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["import_source"], "claude_code")

    def test_executor_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Update README"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["executor"], "claude_code_import")

    def test_task_stored_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Fix the parser bug"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIn("Fix the parser bug", entry["task"])

    def test_model_stored_when_provided(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Add tests", "--model", "claude-sonnet-4-6"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["execution_model"], "claude-sonnet-4-6")

    def test_model_unknown_when_not_provided(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Add tests"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["execution_model"], "unknown")

    def test_content_hash_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIn("content_hash", entry)
            self.assertTrue(entry["content_hash"].startswith("sha256:"))

    def test_no_estimated_cost_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertNotIn("estimated_cost", entry)

    def test_no_token_fields_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.assertNotIn(field, entry, msg=f"{field} must not be set on import")

    def test_verification_attempted_false(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIs(entry["verification_attempted"], False)

    def test_no_blocked_fields_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            for field in ("raw_prompt", "raw_diff", "transcript", "stack_trace", "model_output"):
                self.assertNotIn(field, entry, msg=f"blocked field {field!r} leaked")

    def test_output_mentions_openshard_did_not_control(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            self.assertIn("OpenShard did not control", result.output)


# ---------------------------------------------------------------------------
# Integration with openshard last / proof / trust
# ---------------------------------------------------------------------------

class TestImportedShardWithExistingCommands(unittest.TestCase):

    def _write_imported_shard(self, runner: CliRunner) -> None:
        runner.invoke(cli, ["import", "claude", "--task", "Fix the auth service"])

    def test_last_exits_zero_after_import(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_imported_shard(runner)
            result = runner.invoke(cli, ["last"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_last_json_has_import_source(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_imported_shard(runner)
            result = runner.invoke(cli, ["last", "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            run = data.get("run", {})
            self.assertEqual(run.get("import_source"), "claude_code")

    def test_proof_last_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_imported_shard(runner)
            result = runner.invoke(cli, ["proof", "last"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_trust_last_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_imported_shard(runner)
            result = runner.invoke(cli, ["trust", "last"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_trust_last_shows_score(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_imported_shard(runner)
            result = runner.invoke(cli, ["trust", "last"])
            self.assertIn("100", result.output)

    def test_trust_last_json_has_score(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_imported_shard(runner)
            result = runner.invoke(cli, ["trust", "last", "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            self.assertIn("score", data)
            self.assertIsInstance(data["score"], int)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def test_dry_run_does_not_write_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--dry-run"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertFalse(Path(".openshard/runs.jsonl").exists())

    def test_dry_run_prints_json(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--dry-run"])
            data = json.loads(result.output)
            self.assertEqual(data["import_source"], "claude_code")

    def test_dry_run_json_has_no_cost(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--dry-run"])
            data = json.loads(result.output)
            self.assertNotIn("estimated_cost", data)


# ---------------------------------------------------------------------------
# --json flag
# ---------------------------------------------------------------------------

class TestJsonOutput(unittest.TestCase):

    def test_json_flag_emits_valid_json_only(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            self.assertEqual(data["import_source"], "claude_code")

    def test_json_flag_still_writes_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--json"])
            self.assertTrue(Path(".openshard/runs.jsonl").exists())


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling(unittest.TestCase):

    def test_missing_task_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude"])
            self.assertNotEqual(result.exit_code, 0)

    def test_invalid_notes_file_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--notes", "nonexistent.md"])
            self.assertNotEqual(result.exit_code, 0)

    def test_invalid_notes_file_has_clear_message(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--notes", "nonexistent.md"])
            combined = (result.output or "") + (str(result.exception) if result.exception else "")
            self.assertTrue(
                "nonexistent.md" in combined or "not found" in combined.lower(),
                msg=f"Expected path or 'not found' in output: {combined!r}",
            )


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

class TestHelp(unittest.TestCase):

    def test_import_claude_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "claude", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_import_group_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_import_group_help_shows_claude_subcommand(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "--help"])
        self.assertIn("claude", result.output)

    def test_import_claude_help_shows_task_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "claude", "--help"])
        self.assertIn("--task", result.output)

    def test_import_claude_help_shows_notes_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "claude", "--help"])
        self.assertIn("--notes", result.output)


# ---------------------------------------------------------------------------
# Notes file
# ---------------------------------------------------------------------------

class TestNotesFile(unittest.TestCase):

    def test_valid_notes_file_accepted(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("notes.md").write_text("Session notes here.", encoding="utf-8")
            result = runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--notes", "notes.md"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_notes_summary_stored_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("notes.md").write_text("Refactored the payment module.", encoding="utf-8")
            runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--notes", "notes.md"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIn("payment module", entry.get("summary", ""))

    def test_secret_in_notes_is_scrubbed(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("notes.md").write_text(
                "Used sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 in test",
                encoding="utf-8",
            )
            runner.invoke(cli, ["import", "claude", "--task", "Fix bug", "--notes", "notes.md"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            raw_summary = json.dumps(entry)
            self.assertNotIn("sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890", raw_summary)


# ---------------------------------------------------------------------------
# --task-id
# ---------------------------------------------------------------------------

class TestTaskIdOption(unittest.TestCase):

    def _new_task_id(self) -> str:
        from openshard.history.task_identity import new_task_id
        return new_task_id()

    def test_no_task_id_flag_means_no_task_id_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix"])
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertNotIn("task_id", entry)

    def test_explicit_task_id_attached(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            tid = self._new_task_id()
            result = runner.invoke(cli, ["import", "claude", "--task", "Minor fix", "--task-id", tid])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["task_id"], tid)

    def test_malformed_task_id_rejected(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["import", "claude", "--task", "Minor fix", "--task-id", "not-well-formed"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(Path(".openshard/runs.jsonl").exists())

    def test_task_new_then_import_round_trip(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            new_result = runner.invoke(cli, ["task", "new", "--json"])
            self.assertEqual(new_result.exit_code, 0, msg=new_result.output)
            tid = json.loads(new_result.output)["task_id"]
            runner.invoke(cli, ["import", "claude", "--task", "Minor fix", "--task-id", tid])
            attempts_result = runner.invoke(cli, ["task", "attempts", tid, "--json"])
            self.assertEqual(attempts_result.exit_code, 0, msg=attempts_result.output)
            data = json.loads(attempts_result.output)
            self.assertEqual(len(data["receipts"]), 1)


if __name__ == "__main__":
    unittest.main()
