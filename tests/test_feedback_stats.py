from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli


def _make_entry(**kwargs) -> dict:
    return {
        "task": "test task",
        "timestamp": "2025-01-01T00:00:00Z",
        "execution_model": "gpt-4o",
        "duration_seconds": 1.0,
        "files_created": 0,
        "files_updated": 0,
        "files_deleted": 0,
        "retry_triggered": False,
        "verification_attempted": False,
        "verification_passed": None,
        "workspace_path": None,
        "summary": "",
        **kwargs,
    }


def _write_runs(entries: list[dict]) -> Path:
    log_dir = Path(".openshard")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runs.jsonl"
    with log_path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return log_path


def _make_feedback(rating: str, note: str = "") -> dict:
    return {
        "schema_version": 1,
        "rating": rating,
        "note": note,
        "created_at": "2025-01-01T00:00:00Z",
    }


class TestFeedbackStats(unittest.TestCase):

    def test_no_run_history_file(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("No run history found", result.output)

    def test_empty_runs_file(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/runs.jsonl").write_text("", encoding="utf-8")
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("No runs recorded yet", result.output)

    def test_runs_with_no_feedback_shows_zero_and_tip(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_entry(), _make_entry()])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("0 recorded", result.output)
            self.assertIn("openshard feedback accept", result.output)

    def test_good_mixed_bad_counts(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback("good")),
                _make_entry(feedback=_make_feedback("good")),
                _make_entry(feedback=_make_feedback("mixed")),
                _make_entry(feedback=_make_feedback("bad")),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertIn("Good:  2", result.output)
            self.assertIn("Mixed: 1", result.output)
            self.assertIn("Bad:   1", result.output)

    def test_feedback_percentage_shown(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback("good")),
                _make_entry(),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertIn("50%", result.output)

    def test_groups_ratings_by_model(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(execution_model="gpt-4o", feedback=_make_feedback("good")),
                _make_entry(execution_model="gpt-4o", feedback=_make_feedback("mixed")),
                _make_entry(execution_model="claude-3-5-sonnet-20241022", feedback=_make_feedback("bad")),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertIn("By model", result.output)
            lines = result.output.splitlines()
            gpt_line = next((ln for ln in lines if "gpt" in ln.lower() or "GPT" in ln), None)
            self.assertIsNotNone(gpt_line)
            self.assertIn("good=1", gpt_line)
            self.assertIn("mixed=1", gpt_line)

    def test_unknown_model_fallback(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            entry = _make_entry(feedback=_make_feedback("good"))
            del entry["execution_model"]
            _write_runs([entry])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("By model", result.output)
            self.assertIn("Unknown", result.output)

    def test_recent_notes_newest_first(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback("good", note="first note")),
                _make_entry(feedback=_make_feedback("bad", note="second note")),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            first_pos = result.output.index("first note")
            second_pos = result.output.index("second note")
            self.assertLess(second_pos, first_pos)

    def test_recent_notes_limited_to_five(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback("good", note=f"note {i}"))
                for i in range(6)
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertNotIn("note 0", result.output)
            self.assertIn("note 5", result.output)

    def test_no_recent_notes_section_when_all_notes_empty(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback("good", note="")),
                _make_entry(feedback=_make_feedback("bad", note="")),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertNotIn("Recent notes", result.output)

    def test_invalid_json_lines_skipped_safely(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            log_dir = Path(".openshard")
            log_dir.mkdir()
            log_path = log_dir / "runs.jsonl"
            with log_path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(_make_entry(feedback=_make_feedback("good"))) + "\n")
                fh.write("not valid json\n")
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Runs: 1", result.output)

    def test_exit_code_zero_for_valid_file_with_feedback(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_entry(feedback=_make_feedback("good"))])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)


def _make_feedback_full(rating: str = "bad", action: str | None = None,
                        reason: str | None = None, note: str = "") -> dict:
    fb: dict = {
        "schema_version": 1,
        "rating": rating,
        "note": note,
        "created_at": "2025-01-01T00:00:00Z",
    }
    if action is not None:
        fb["action"] = action
    if reason is not None:
        fb["correction_reason"] = reason
    return fb


class TestFeedbackStatsActionBreakdown(unittest.TestCase):

    def test_action_counts_accepted_rejected_edited_retried(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback_full("good", action="accepted")),
                _make_entry(feedback=_make_feedback_full("bad", action="rejected")),
                _make_entry(feedback=_make_feedback_full("bad", action="edited")),
                _make_entry(feedback=_make_feedback_full("bad", action="retried")),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("By action", result.output)
            self.assertIn("accepted: 1", result.output)
            self.assertIn("rejected: 1", result.output)
            self.assertIn("edited: 1", result.output)
            self.assertIn("retried: 1", result.output)

    def test_action_section_absent_when_no_actions_recorded(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_entry(feedback=_make_feedback("good"))])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertNotIn("By action", result.output)

    def test_correction_reason_counts_shown(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(feedback=_make_feedback_full("bad", action="rejected", reason="wrong-file")),
                _make_entry(feedback=_make_feedback_full("bad", action="rejected", reason="wrong-file")),
                _make_entry(feedback=_make_feedback_full("bad", action="edited", reason="failed-tests")),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertIn("Correction reasons", result.output)
            self.assertIn("wrong-file: 2", result.output)
            self.assertIn("failed-tests: 1", result.output)

    def test_correction_reasons_section_absent_when_none_recorded(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_entry(feedback=_make_feedback("good"))])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertNotIn("Correction reasons", result.output)

    def test_groups_by_task_category_when_present(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([
                _make_entry(
                    routing_category="standard",
                    feedback=_make_feedback_full("good"),
                ),
                _make_entry(
                    routing_category="standard",
                    feedback=_make_feedback_full("bad"),
                ),
                _make_entry(
                    routing_category="security",
                    feedback=_make_feedback_full("good"),
                ),
            ])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertIn("By category", result.output)
            lines = result.output.splitlines()
            standard_line = next((ln for ln in lines if "standard" in ln), None)
            self.assertIsNotNone(standard_line)
            self.assertIn("good=1", standard_line)
            self.assertIn("bad=1", standard_line)

    def test_category_section_absent_when_no_routing_category(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_entry(feedback=_make_feedback("good"))])
            result = runner.invoke(cli, ["feedback-stats"])
            self.assertNotIn("By category", result.output)


if __name__ == "__main__":
    unittest.main()
