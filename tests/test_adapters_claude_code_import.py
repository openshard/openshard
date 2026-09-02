"""Unit tests for openshard.adapters.claude_code_import."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openshard.adapters.claude_code_import import (
    _parse_git_changed_files,
    _sanitize_model,
    _sanitize_task,
    _scrub_notes_file,
    build_claude_code_import_entry,
    write_import_entry,
)
from openshard.history.event import (
    EVENT_FILE_CHANGED,
    EVENT_RUN_COMPLETED,
    EVIDENCE_GIT_OBSERVED,
    SOURCE_CLAUDE_CODE_IMPORT,
    STATUS_UNKNOWN,
    events_from_entry,
)
from openshard.history.metrics import load_runs
from openshard.history.shard_contract import build_shard_receipt

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(repo_path: Path, **kwargs) -> dict:
    defaults = dict(task="Fix the bug", model=None, notes_file=None, repo_path=repo_path)
    defaults.update(kwargs)
    return build_claude_code_import_entry(
        defaults.pop("task"),
        model=defaults.pop("model"),
        notes_file=defaults.pop("notes_file"),
        repo_path=defaults.pop("repo_path"),
    )


# ---------------------------------------------------------------------------
# Provenance markers
# ---------------------------------------------------------------------------

class TestProvenanceMarkers(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_import_source_is_claude_code(self):
        entry = _make_entry(self.repo)
        self.assertEqual(entry["import_source"], "claude_code")

    def test_import_method_is_v0(self):
        entry = _make_entry(self.repo)
        self.assertEqual(entry["import_method"], "openshard_import_v0")

    def test_executor_field(self):
        entry = _make_entry(self.repo)
        self.assertEqual(entry["executor"], "claude_code_import")

    def test_import_note_present_and_non_empty(self):
        entry = _make_entry(self.repo)
        note = entry.get("import_note", "")
        self.assertIsInstance(note, str)
        self.assertTrue(len(note) > 0)

    def test_import_note_mentions_claude_code(self):
        entry = _make_entry(self.repo)
        self.assertIn("Claude Code", entry["import_note"])

    def test_import_note_mentions_openshard(self):
        entry = _make_entry(self.repo)
        self.assertIn("OpenShard", entry["import_note"])


# ---------------------------------------------------------------------------
# Model handling
# ---------------------------------------------------------------------------

class TestModelHandling(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_model_when_not_provided(self):
        entry = _make_entry(self.repo, model=None)
        self.assertEqual(entry["execution_model"], "unknown")

    def test_model_stored_when_provided(self):
        entry = _make_entry(self.repo, model="claude-sonnet-4-6")
        self.assertEqual(entry["execution_model"], "claude-sonnet-4-6")

    def test_sanitize_model_returns_unknown_for_none(self):
        self.assertEqual(_sanitize_model(None), "unknown")

    def test_sanitize_model_returns_unknown_for_empty(self):
        self.assertEqual(_sanitize_model(""), "unknown")

    def test_sanitize_model_passes_through_valid_slug(self):
        self.assertEqual(_sanitize_model("claude-opus-4-7"), "claude-opus-4-7")


# ---------------------------------------------------------------------------
# Cost and token fields must never appear
# ---------------------------------------------------------------------------

class TestNoFinancialFields(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_estimated_cost(self):
        entry = _make_entry(self.repo)
        self.assertNotIn("estimated_cost", entry)

    def test_no_prompt_tokens(self):
        entry = _make_entry(self.repo)
        self.assertNotIn("prompt_tokens", entry)

    def test_no_completion_tokens(self):
        entry = _make_entry(self.repo)
        self.assertNotIn("completion_tokens", entry)

    def test_no_total_tokens(self):
        entry = _make_entry(self.repo)
        self.assertNotIn("total_tokens", entry)


# ---------------------------------------------------------------------------
# Verification fields
# ---------------------------------------------------------------------------

class TestVerificationFields(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_verification_attempted_is_false(self):
        entry = _make_entry(self.repo)
        self.assertIs(entry["verification_attempted"], False)

    def test_verification_passed_is_none(self):
        entry = _make_entry(self.repo)
        self.assertIsNone(entry.get("verification_passed"))


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------

class TestContentHash(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_content_hash_present(self):
        entry = _make_entry(self.repo)
        self.assertIn("content_hash", entry)

    def test_content_hash_starts_with_sha256(self):
        entry = _make_entry(self.repo)
        self.assertTrue(entry["content_hash"].startswith("sha256:"))


# ---------------------------------------------------------------------------
# Blocked fields
# ---------------------------------------------------------------------------

class TestBlockedFields(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_blocked_fields_stripped_by_coerce(self):
        # Patch the builder to inject a blocked field; coerce must strip it.
        from openshard.history.shard_schema import coerce_shard_entry
        dirty = {"task": "test", "raw_prompt": "SECRET", "import_source": "claude_code"}
        coerced = coerce_shard_entry(dirty)
        self.assertNotIn("raw_prompt", coerced)

    def test_schema_version_present(self):
        entry = _make_entry(self.repo)
        self.assertEqual(entry["schema_version"], "1.2")


# ---------------------------------------------------------------------------
# Task sanitization
# ---------------------------------------------------------------------------

class TestTaskSanitization(unittest.TestCase):

    def test_task_stored_normally(self):
        result = _sanitize_task("Fix the login bug")
        self.assertEqual(result, "Fix the login bug")

    def test_task_with_secret_is_scrubbed(self):
        result = _sanitize_task("Use sk-ant-abc12345678901234567890 to call API")
        self.assertNotIn("sk-ant-abc12345678901234567890", result)

    def test_empty_task_returns_placeholder(self):
        result = _sanitize_task("")
        self.assertEqual(result, "Claude Code session import")

    def test_non_string_task_returns_placeholder(self):
        result = _sanitize_task(None)  # type: ignore[arg-type]
        self.assertEqual(result, "Claude Code session import")

    def test_task_capped_at_500_chars(self):
        long_task = "a" * 600
        result = _sanitize_task(long_task)
        self.assertLessEqual(len(result), 500)


# ---------------------------------------------------------------------------
# git diff parsing
# ---------------------------------------------------------------------------

class TestParseGitChangedFiles(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_empty_outside_repo(self):
        files, source = _parse_git_changed_files(self.repo)
        self.assertEqual(files, [])
        self.assertEqual(source, "not_available")

    def test_classifies_add_as_create(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "A\tsrc/new_file.py\n"
        with patch("subprocess.run", return_value=mock_result):
            files, source = _parse_git_changed_files(self.repo)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["change_type"], "create")
        self.assertEqual(files[0]["path"], "src/new_file.py")
        self.assertEqual(source, "git_diff_inferred")

    def test_classifies_modify_as_update(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/existing.py\n"
        with patch("subprocess.run", return_value=mock_result):
            files, source = _parse_git_changed_files(self.repo)
        self.assertEqual(files[0]["change_type"], "update")

    def test_classifies_delete_as_delete(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "D\tsrc/removed.py\n"
        with patch("subprocess.run", return_value=mock_result):
            files, source = _parse_git_changed_files(self.repo)
        self.assertEqual(files[0]["change_type"], "delete")

    def test_caps_at_20_files(self):
        lines = "\n".join(f"M\tsrc/file{i}.py" for i in range(25))
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = lines
        with patch("subprocess.run", return_value=mock_result):
            files, _ = _parse_git_changed_files(self.repo)
        self.assertLessEqual(len(files), 20)

    def test_returns_empty_and_not_available_on_git_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            files, source = _parse_git_changed_files(self.repo)
        self.assertEqual(files, [])
        self.assertEqual(source, "not_available")

    def test_files_source_git_diff_inferred_when_empty_diff(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            files, source = _parse_git_changed_files(self.repo)
        self.assertEqual(files, [])
        self.assertEqual(source, "git_diff_inferred")

    def test_summary_field_is_honest(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/foo.py\n"
        with patch("subprocess.run", return_value=mock_result):
            files, _ = _parse_git_changed_files(self.repo)
        self.assertIn("inferred", files[0]["summary"])


# ---------------------------------------------------------------------------
# Notes file scrubbing
# ---------------------------------------------------------------------------

class TestScrubNotesFile(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_notes_stored(self):
        notes = self.tmpdir / "notes.md"
        notes.write_text("Fixed the auth bug by rewriting the token check.", encoding="utf-8")
        result = _scrub_notes_file(notes)
        self.assertIn("auth bug", result)

    def test_secret_in_notes_is_redacted(self):
        notes = self.tmpdir / "notes.md"
        notes.write_text("Used key sk-ant-abcdefghijklmnopqrst12345 for testing.", encoding="utf-8")
        result = _scrub_notes_file(notes)
        self.assertNotIn("sk-ant-abcdefghijklmnopqrst12345", result)

    def test_notes_capped_at_summary_limit(self):
        notes = self.tmpdir / "notes.md"
        notes.write_text("x" * 1000, encoding="utf-8")
        result = _scrub_notes_file(notes)
        self.assertLessEqual(len(result), 300)

    def test_missing_file_returns_empty_string(self):
        result = _scrub_notes_file(self.tmpdir / "nonexistent.md")
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# files_source field
# ---------------------------------------------------------------------------

class TestFilesSource(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_files_source_not_available_outside_repo(self):
        entry = _make_entry(self.repo)
        self.assertEqual(entry["files_source"], "not_available")

    def test_files_source_git_diff_inferred_with_mocked_git(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/foo.py\n"
        with patch("subprocess.run", return_value=mock_result):
            entry = _make_entry(self.repo)
        self.assertEqual(entry["files_source"], "git_diff_inferred")


# ---------------------------------------------------------------------------
# write_import_entry
# ---------------------------------------------------------------------------

class TestWriteImportEntry(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_openshard_dir(self):
        entry = _make_entry(self.repo)
        write_import_entry(entry, self.repo)
        self.assertTrue((self.repo / ".openshard").is_dir())

    def test_creates_runs_jsonl(self):
        entry = _make_entry(self.repo)
        write_import_entry(entry, self.repo)
        self.assertTrue((self.repo / ".openshard" / "runs.jsonl").exists())

    def test_written_entry_appears_in_load_runs(self):
        import os
        orig = os.getcwd()
        os.chdir(self.repo)
        try:
            entry = _make_entry(self.repo)
            write_import_entry(entry, self.repo)
            runs = load_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["import_source"], "claude_code")
        finally:
            os.chdir(orig)

    def test_written_entry_has_content_hash(self):
        import os
        orig = os.getcwd()
        os.chdir(self.repo)
        try:
            entry = _make_entry(self.repo)
            write_import_entry(entry, self.repo)
            runs = load_runs()
            self.assertIn("content_hash", runs[0])
        finally:
            os.chdir(orig)


# ---------------------------------------------------------------------------
# Embedded canonical Events (Migration 5) — producer at observation time
# ---------------------------------------------------------------------------

class TestEmbeddedEvents(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_events_key_present_on_entry(self):
        entry = _make_entry(self.repo)
        self.assertIn("events", entry)
        self.assertIsInstance(entry["events"], list)

    def test_run_level_event_present(self):
        entry = _make_entry(self.repo)
        run_events = [e for e in entry["events"] if e["event_type"] == EVENT_RUN_COMPLETED]
        self.assertEqual(len(run_events), 1)

    def test_run_level_event_source_and_actor(self):
        entry = _make_entry(self.repo)
        run_event = next(e for e in entry["events"] if e["event_type"] == EVENT_RUN_COMPLETED)
        self.assertEqual(run_event["source"], SOURCE_CLAUDE_CODE_IMPORT)
        self.assertEqual(run_event["actor"], "claude_code")

    def test_run_level_event_status_unknown_when_verification_never_attempted(self):
        entry = _make_entry(self.repo)
        run_event = next(e for e in entry["events"] if e["event_type"] == EVENT_RUN_COMPLETED)
        self.assertEqual(run_event["status"], STATUS_UNKNOWN)

    def test_run_level_event_linkage_matches_entry(self):
        entry = _make_entry(self.repo)
        run_event = next(e for e in entry["events"] if e["event_type"] == EVENT_RUN_COMPLETED)
        self.assertEqual(run_event["run_id"], entry["run_id"])
        self.assertEqual(run_event["shard_id"], entry["shard_id"])
        self.assertEqual(run_event["attempt_number"], entry["attempt_number"])

    def test_event_id_is_a_genuine_uuid4_not_a_stable_hash(self):
        entry = _make_entry(self.repo)
        run_event = next(e for e in entry["events"] if e["event_type"] == EVENT_RUN_COMPLETED)
        self.assertRegex(run_event["event_id"], _UUID4_RE)
        self.assertFalse(run_event["event_id"].startswith("evt-"))

    def test_no_file_events_when_git_diff_not_available(self):
        # self.repo is not a git repository, so files_source is "not_available".
        entry = _make_entry(self.repo)
        self.assertEqual(entry["files_source"], "not_available")
        file_events = [e for e in entry["events"] if e["event_type"] == EVENT_FILE_CHANGED]
        self.assertEqual(file_events, [])

    def test_file_events_created_from_files_detail(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/foo.py\nA\tsrc/bar.py\n"
        with patch("subprocess.run", return_value=mock_result):
            entry = _make_entry(self.repo)
        file_events = [e for e in entry["events"] if e["event_type"] == EVENT_FILE_CHANGED]
        self.assertEqual(len(file_events), 2)
        self.assertEqual({e["target"] for e in file_events}, {"src/foo.py", "src/bar.py"})

    def test_file_event_status_stays_unknown(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/foo.py\n"
        with patch("subprocess.run", return_value=mock_result):
            entry = _make_entry(self.repo)
        file_event = next(e for e in entry["events"] if e["event_type"] == EVENT_FILE_CHANGED)
        self.assertEqual(file_event["status"], STATUS_UNKNOWN)

    def test_file_event_evidence_is_git_observed(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/foo.py\n"
        with patch("subprocess.run", return_value=mock_result):
            entry = _make_entry(self.repo)
        file_event = next(e for e in entry["events"] if e["event_type"] == EVENT_FILE_CHANGED)
        self.assertEqual(file_event["evidence"], EVIDENCE_GIT_OBSERVED)

    def test_events_never_expose_openshard_as_actor(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M\tsrc/foo.py\n"
        with patch("subprocess.run", return_value=mock_result):
            entry = _make_entry(self.repo)
        for e in entry["events"]:
            self.assertNotEqual((e.get("actor") or "").lower(), "openshard")

    def test_events_all_json_serializable(self):
        entry = _make_entry(self.repo)
        json.dumps(entry["events"])  # must not raise

    def test_no_raw_content_stored_flag_set(self):
        entry = _make_entry(self.repo)
        for e in entry["events"]:
            self.assertFalse(e["raw_content_stored"])

    def test_secret_in_task_does_not_leak_into_events(self):
        entry = _make_entry(
            self.repo, task="Use sk-ant-api03-SECRETSECRET12345678901234 to authenticate",
        )
        raw = json.dumps(entry["events"])
        self.assertNotIn("sk-ant-api03-SECRETSECRET12345678901234", raw)

    def test_events_from_entry_uses_embedded_events_verbatim(self):
        # events_from_entry must return exactly what was embedded -- same
        # ids, no re-derivation -- once the "events" key is present.
        entry = _make_entry(self.repo)
        embedded_ids = {e["event_id"] for e in entry["events"]}
        derived = events_from_entry(entry)
        self.assertEqual({e.event_id for e in derived}, embedded_ids)

    def test_events_survive_write_and_load_round_trip(self):
        import os
        orig = os.getcwd()
        os.chdir(self.repo)
        try:
            entry = _make_entry(self.repo)
            embedded_ids = {e["event_id"] for e in entry["events"]}
            write_import_entry(entry, self.repo)
            runs = load_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual({e["event_id"] for e in runs[0]["events"]}, embedded_ids)
        finally:
            os.chdir(orig)

    def test_shard_receipt_events_match_embedded_events(self):
        entry = _make_entry(self.repo)
        embedded_ids = {e["event_id"] for e in entry["events"]}
        receipt = build_shard_receipt(entry)
        self.assertEqual({e.event_id for e in receipt.events}, embedded_ids)

    def test_shard_receipt_events_linkage_matches_run_shard_attempt(self):
        entry = _make_entry(self.repo)
        receipt = build_shard_receipt(entry)
        self.assertGreater(len(receipt.events), 0)
        for e in receipt.events:
            self.assertEqual(e.run_id, receipt.run_id)
            self.assertEqual(e.shard_id, receipt.shard_id)
            self.assertEqual(e.attempt_number, receipt.attempt_number)


if __name__ == "__main__":
    unittest.main()
