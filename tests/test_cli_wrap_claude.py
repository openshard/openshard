"""CLI integration tests for openshard wrap claude."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.cli.main import cli

# Cross-platform substitute for the Unix `echo hello` shell builtin.
_ECHO_ARGV = [sys.executable, "-c", "print('hello')"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

class TestHelp(unittest.TestCase):

    def test_wrap_claude_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "claude", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_wrap_group_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_wrap_group_help_shows_claude_subcommand(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "--help"])
        self.assertIn("claude", result.output)

    def test_wrap_claude_help_shows_task_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "claude", "--help"])
        self.assertIn("--task", result.output)

    def test_wrap_claude_help_shows_model_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "claude", "--help"])
        self.assertIn("--model", result.output)

    def test_wrap_claude_help_shows_dry_run_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "claude", "--help"])
        self.assertIn("--dry-run", result.output)

    def test_wrap_claude_help_does_not_show_notes(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["wrap", "claude", "--help"])
        self.assertNotIn("--notes", result.output)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def test_dry_run_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix the auth service", "--dry-run", "--", "echo", "hello"],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_dry_run_does_not_write_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix the auth service", "--dry-run", "--", "echo", "hello"],
            )
            self.assertFalse(Path(".openshard/runs.jsonl").exists())

    def test_dry_run_does_not_run_subprocess(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("openshard.adapters.wrap_exec.run_wrapped_command") as mock_run:
                runner.invoke(
                    cli,
                    ["wrap", "claude", "--task", "Fix it", "--dry-run", "--", "echo", "hello"],
                )
                mock_run.assert_not_called()

    def test_dry_run_outputs_valid_json(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--dry-run", "--", "echo", "hello"],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            self.assertEqual(data["import_source"], "claude_code")

    def test_dry_run_json_has_no_cost(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--dry-run", "--", "echo", "hello"],
            )
            data = json.loads(result.output)
            self.assertNotIn("estimated_cost", data)

    def test_dry_run_json_has_content_hash(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--dry-run", "--", "echo", "hello"],
            )
            data = json.loads(result.output)
            self.assertIn("content_hash", data)
            self.assertTrue(data["content_hash"].startswith("sha256:"))

    def test_dry_run_json_has_import_method_wrap_v0(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--dry-run", "--", "echo", "hello"],
            )
            data = json.loads(result.output)
            self.assertEqual(data["import_method"], "openshard_wrap_v0")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling(unittest.TestCase):

    def test_missing_task_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["wrap", "claude", "--", "echo", "hello"])
            self.assertNotEqual(result.exit_code, 0)

    def test_missing_command_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["wrap", "claude", "--task", "Fix bug"])
            self.assertNotEqual(result.exit_code, 0)

    def test_command_not_found_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--", "__no_such_command_openshard__"],
            )
            # Should exit nonzero (command not found = exit 127)
            self.assertNotEqual(result.exit_code, 0)

    def test_command_not_found_still_writes_receipt(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--", "__no_such_command_openshard__"],
            )
            # Receipt is still written even if command not found
            self.assertTrue(Path(".openshard/runs.jsonl").exists())

    def test_failed_command_marks_subprocess_failed(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--", "false"],
                catch_exceptions=False,
            )
            if Path(".openshard/runs.jsonl").exists():
                entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
                metadata = entry.get("metadata", {})
                self.assertEqual(metadata.get("wrap_status"), "subprocess_failed")


# ---------------------------------------------------------------------------
# Basic write path (with mocked subprocess)
# ---------------------------------------------------------------------------

class TestWrapWritePath(unittest.TestCase):

    def _invoke_with_echo(self, runner: CliRunner, task: str = "Fix the bug", **extra_args) -> object:
        args = ["wrap", "claude", "--task", task] + list(extra_args.get("extra", [])) + ["--"] + _ECHO_ARGV
        return runner.invoke(cli, args)

    def test_creates_runs_jsonl(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = self._invoke_with_echo(runner)
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertTrue(Path(".openshard/runs.jsonl").exists())

    def test_writes_one_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner)
            entries = _load_jsonl(Path(".openshard/runs.jsonl"))
            self.assertEqual(len(entries), 1)

    def test_import_source_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Update README")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["import_source"], "claude_code")

    def test_executor_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Update README")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["executor"], "claude_code_wrap")

    def test_import_method_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Update README")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["import_method"], "openshard_wrap_v0")

    def test_task_stored_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Fix the parser bug")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIn("Fix the parser bug", entry["task"])

    def test_model_stored_when_provided(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Add tests", "--model", "claude-sonnet-4-6", "--"] + _ECHO_ARGV,
            )
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["execution_model"], "claude-sonnet-4-6")

    def test_model_unknown_when_not_provided(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Add tests")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertEqual(entry["execution_model"], "unknown")

    def test_content_hash_in_written_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Minor fix")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIn("content_hash", entry)
            self.assertTrue(entry["content_hash"].startswith("sha256:"))

    def test_no_estimated_cost_in_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Minor fix")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertNotIn("estimated_cost", entry)

    def test_verification_attempted_false(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._invoke_with_echo(runner, task="Minor fix")
            entry = _load_jsonl(Path(".openshard/runs.jsonl"))[0]
            self.assertIs(entry["verification_attempted"], False)

    def test_output_mentions_wrapped_claude_code_receipt(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = self._invoke_with_echo(runner, task="Minor fix")
            self.assertIn("Wrapped Claude Code receipt", result.output)

    def test_output_mentions_openshard_did_not_control(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = self._invoke_with_echo(runner, task="Minor fix")
            self.assertIn("OpenShard did not control", result.output)

    def test_output_mentions_running_command(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = self._invoke_with_echo(runner, task="Minor fix")
            self.assertIn("Running:", result.output)


# ---------------------------------------------------------------------------
# --json flag
# ---------------------------------------------------------------------------

class TestJsonOutput(unittest.TestCase):

    def test_json_flag_emits_valid_json_only(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--json", "--"] + _ECHO_ARGV,
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            # Find and parse the JSON block (multi-line) in the output
            output = result.output
            start = output.find("{")
            self.assertGreater(start, -1, "No JSON object found in output")
            # Find the matching closing brace by counting depth
            depth = 0
            end = start
            for i, ch in enumerate(output[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            data = json.loads(output[start:end + 1])
            self.assertEqual(data["import_source"], "claude_code")

    def test_json_flag_still_writes_entry(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--json", "--"] + _ECHO_ARGV,
            )
            self.assertTrue(Path(".openshard/runs.jsonl").exists())


# ---------------------------------------------------------------------------
# Integration with openshard last
# ---------------------------------------------------------------------------

class TestWrappedShardWithLastCommand(unittest.TestCase):

    def _write_wrapped_shard(self, runner: CliRunner) -> None:
        runner.invoke(
            cli,
            ["wrap", "claude", "--task", "Fix the auth service", "--"] + _ECHO_ARGV,
        )

    def test_last_exits_zero_after_wrap(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_wrapped_shard(runner)
            result = runner.invoke(cli, ["last"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_last_json_has_import_source_claude_code(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_wrapped_shard(runner)
            result = runner.invoke(cli, ["last", "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            run = data.get("run", {})
            self.assertEqual(run.get("import_source"), "claude_code")

    def test_last_json_has_import_method_wrap_v0(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_wrapped_shard(runner)
            result = runner.invoke(cli, ["last", "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            run = data.get("run", {})
            self.assertEqual(run.get("import_method"), "openshard_wrap_v0")

    def test_proof_last_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_wrapped_shard(runner)
            result = runner.invoke(cli, ["proof", "last"])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_trust_last_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self._write_wrapped_shard(runner)
            result = runner.invoke(cli, ["trust", "last"])
            self.assertEqual(result.exit_code, 0, msg=result.output)


# ---------------------------------------------------------------------------
# --task-id
# ---------------------------------------------------------------------------

class TestTaskIdOption(unittest.TestCase):

    def test_no_task_id_flag_dry_run_has_no_task_id(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli, ["wrap", "claude", "--task", "Fix bug", "--dry-run", "--", *_ECHO_ARGV],
            )
            data = json.loads(result.output)
            self.assertNotIn("task_id", data)

    def test_explicit_task_id_attached_in_dry_run(self):
        from openshard.history.task_identity import new_task_id

        tid = new_task_id()
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--task-id", tid, "--dry-run", "--", *_ECHO_ARGV],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            self.assertEqual(data["task_id"], tid)

    def test_malformed_task_id_rejected(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["wrap", "claude", "--task", "Fix bug", "--task-id", "not-well-formed",
                 "--dry-run", "--", *_ECHO_ARGV],
            )
            self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
