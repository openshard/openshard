from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.interactions import (
    DeveloperInteractionEvent,
    _dict_to_event,
    _event_to_dict,
    interaction_events_for_run,
    load_interaction_events,
    log_interaction_event,
    sanitize_event,
)

# A fake AWS-style key and absolute paths used to prove no raw content leaks.
_FAKE_SECRET = "AKIAABCDEFGHIJKLMNOP"
_ABS_POSIX = "/home/user/secret.env"
_ABS_WIN = "C:\\Users\\Michael\\secret.env"


def _write_raw_jsonl(records: list[dict]) -> Path:
    """Write raw (un-sanitised) JSONL lines, simulating legacy on-disk entries."""
    path = Path(".openshard") / "interactions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _make_event(**kwargs) -> DeveloperInteractionEvent:
    defaults: dict = dict(
        run_id="2025-01-01T00:00:00Z",
        event_type="feedback_accepted",
        summary="test event",
    )
    defaults.update(kwargs)
    return DeveloperInteractionEvent(**defaults)


def _write_interactions(events: list[DeveloperInteractionEvent]) -> Path:
    path = Path(".openshard") / "interactions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(_event_to_dict(evt)) + "\n")
    return path


def _make_run_entry(**kwargs) -> dict:
    base = {
        "task": "test task",
        "timestamp": "2025-01-01T00:00:00Z",
        "execution_model": "claude-sonnet-4-6",
        "duration_seconds": 1.0,
        "files_created": 0,
        "files_updated": 0,
        "files_deleted": 0,
        "retry_triggered": False,
        "verification_attempted": False,
        "verification_passed": None,
        "workspace_path": None,
        "summary": "",
    }
    base.update(kwargs)
    return base


def _write_runs(entries: list[dict]) -> Path:
    path = Path(".openshard") / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


class TestEventSerialization(unittest.TestCase):

    def test_event_serialization(self):
        evt = DeveloperInteractionEvent(
            run_id="run-123",
            event_type="feedback_rejected",
            summary="rejected bad output",
            related_stage="execution",
            related_file_paths=["src/foo.py"],
            correction_reason="wrong-file",
            severity="warning",
            accepted=False,
            metadata={"rating": "bad"},
        )
        d = _event_to_dict(evt)
        self.assertEqual(d["run_id"], "run-123")
        self.assertEqual(d["event_type"], "feedback_rejected")
        self.assertEqual(d["summary"], "rejected bad output")
        self.assertEqual(d["related_stage"], "execution")
        self.assertEqual(d["related_file_paths"], ["src/foo.py"])
        self.assertEqual(d["correction_reason"], "wrong-file")
        self.assertEqual(d["severity"], "warning")
        self.assertIs(d["accepted"], False)
        self.assertEqual(d["metadata"], {"rating": "bad"})
        self.assertIs(d["raw_content_stored"], False)

        roundtrip = _dict_to_event(d)
        self.assertEqual(roundtrip.run_id, "run-123")
        self.assertEqual(roundtrip.event_type, "feedback_rejected")
        self.assertEqual(roundtrip.correction_reason, "wrong-file")
        self.assertIs(roundtrip.accepted, False)
        self.assertIs(roundtrip.raw_content_stored, False)

    def test_all_required_keys_present(self):
        d = _event_to_dict(_make_event())
        for key in (
            "schema_version", "event_id", "run_id", "timestamp", "actor",
            "event_type", "summary", "related_stage", "related_file_paths",
            "correction_reason", "severity", "accepted", "metadata",
            "raw_content_stored",
        ):
            self.assertIn(key, d, f"missing key: {key}")


class TestAppendOnlyLogging(unittest.TestCase):

    def test_append_only_logging(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            log_interaction_event(_make_event(event_type="feedback_accepted"))
            log_interaction_event(_make_event(event_type="feedback_rejected"))
            log_interaction_event(_make_event(event_type="edited"))

            path = Path(".openshard") / "interactions.jsonl"
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 3)
            types = [json.loads(ln)["event_type"] for ln in lines]
            self.assertEqual(types, ["feedback_accepted", "feedback_rejected", "edited"])

    def test_append_does_not_overwrite(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            log_interaction_event(_make_event(event_type="accepted"))
            log_interaction_event(_make_event(event_type="rejected"))
            events = load_interaction_events()
            self.assertEqual(events[0].event_type, "accepted")
            self.assertEqual(events[1].event_type, "rejected")


class TestLoadInteractionEvents(unittest.TestCase):

    def test_load_interaction_events(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            evt_a = _make_event(run_id="run-A", event_type="feedback_accepted", summary="A")
            evt_b = _make_event(run_id="run-B", event_type="feedback_rejected", summary="B")
            _write_interactions([evt_a, evt_b])

            loaded = load_interaction_events()
            self.assertEqual(len(loaded), 2)
            self.assertIsInstance(loaded[0], DeveloperInteractionEvent)
            self.assertEqual(loaded[0].run_id, "run-A")
            self.assertEqual(loaded[0].event_type, "feedback_accepted")
            self.assertEqual(loaded[1].run_id, "run-B")
            self.assertEqual(loaded[1].event_type, "feedback_rejected")

    def test_load_skips_malformed_lines(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            path = Path(".openshard") / "interactions.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(_event_to_dict(_make_event(event_type="accepted"))) + "\n")
                fh.write("not valid json\n")
                fh.write("\n")
                fh.write(json.dumps(_event_to_dict(_make_event(event_type="rejected"))) + "\n")

            loaded = load_interaction_events()
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].event_type, "accepted")
            self.assertEqual(loaded[1].event_type, "rejected")


class TestFilterByRunId(unittest.TestCase):

    def test_filter_by_run_id(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([
                _make_event(run_id="run-A", event_type="feedback_accepted"),
                _make_event(run_id="run-A", event_type="feedback_edited"),
                _make_event(run_id="run-B", event_type="feedback_rejected"),
            ])
            a_events = interaction_events_for_run("run-A")
            self.assertEqual(len(a_events), 2)
            b_events = interaction_events_for_run("run-B")
            self.assertEqual(len(b_events), 1)
            c_events = interaction_events_for_run("run-C")
            self.assertEqual(c_events, [])


class TestMissingFileReturnsEmptyList(unittest.TestCase):

    def test_missing_file_returns_empty_list(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = load_interaction_events()
            self.assertEqual(result, [])

    def test_missing_file_for_run_returns_empty_list(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = interaction_events_for_run("run-X")
            self.assertEqual(result, [])


class TestRedactedExport(unittest.TestCase):

    def test_redacted_export(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([
                _make_event(
                    run_id="run-1",
                    event_type="feedback_rejected",
                    summary="sensitive details here",
                    metadata={"rating": "bad", "note": "private note"},
                )
            ])
            result = runner.invoke(cli, ["export-interactions", "--redacted"])
            self.assertEqual(result.exit_code, 0)
            row = json.loads(result.output.strip())
            self.assertEqual(row["summary"], "[redacted]")
            self.assertEqual(row["metadata"], {})
            self.assertEqual(row["event_type"], "feedback_rejected")
            self.assertEqual(row["run_id"], "run-1")
            self.assertIn("timestamp", row)
            self.assertIs(row["raw_content_stored"], False)

    def test_unredacted_export_preserves_fields(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([
                _make_event(
                    summary="original summary",
                    metadata={"rating": "good"},
                )
            ])
            result = runner.invoke(cli, ["export-interactions"])
            self.assertEqual(result.exit_code, 0)
            row = json.loads(result.output.strip())
            self.assertEqual(row["summary"], "original summary")
            self.assertEqual(row["metadata"], {"rating": "good"})

    def test_redacted_export_to_file(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([_make_event(summary="private")])
            result = runner.invoke(cli, ["export-interactions", "--redacted", "--output", "out.jsonl"])
            self.assertEqual(result.exit_code, 0)
            row = json.loads(Path("out.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(row["summary"], "[redacted]")


class TestFeedbackCorrectionIntegration(unittest.TestCase):

    def test_feedback_rejected_logs_interaction_event(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_run_entry(timestamp="2025-06-01T12:00:00Z")])
            result = runner.invoke(
                cli,
                ["feedback", "reject", "--reason", "wrong file targeted"],
            )
            self.assertEqual(result.exit_code, 0)
            events = load_interaction_events()
            self.assertEqual(len(events), 1)
            evt = events[0]
            self.assertEqual(evt.event_type, "feedback_rejected")
            self.assertIs(evt.accepted, False)
            self.assertEqual(evt.run_id, "2025-06-01T12:00:00Z")
            self.assertEqual(evt.correction_reason, "wrong file targeted")
            self.assertIs(evt.raw_content_stored, False)

    def test_feedback_accepted_logs_accepted_true(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_run_entry(timestamp="2025-06-01T12:00:00Z")])
            result = runner.invoke(cli, ["feedback", "accept"])
            self.assertEqual(result.exit_code, 0)
            events = load_interaction_events()
            self.assertEqual(len(events), 1)
            self.assertIs(events[0].accepted, True)
            self.assertEqual(events[0].event_type, "feedback_accepted")

    def test_feedback_retry_logs_event(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_run_entry()])
            result = runner.invoke(cli, ["feedback", "retry"])
            self.assertEqual(result.exit_code, 0)
            events = load_interaction_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "feedback_retried")

    def test_feedback_accept_logs_accepted_true(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_runs([_make_run_entry()])
            result = runner.invoke(cli, ["feedback", "accept"])
            self.assertEqual(result.exit_code, 0)
            events = load_interaction_events()
            self.assertIs(events[0].accepted, True)
            self.assertEqual(events[0].event_type, "feedback_accepted")


class TestRawContentStoredRemainsFalse(unittest.TestCase):

    def test_post_init_enforces_false(self):
        evt = DeveloperInteractionEvent(raw_content_stored=True)
        self.assertIs(evt.raw_content_stored, False)

    def test_stored_json_has_false(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            evt = DeveloperInteractionEvent(raw_content_stored=True)
            log_interaction_event(evt)
            path = Path(".openshard") / "interactions.jsonl"
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertIs(stored["raw_content_stored"], False)

    def test_loaded_event_has_false(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            path = Path(".openshard") / "interactions.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            record = _event_to_dict(_make_event())
            record["raw_content_stored"] = True
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            loaded = load_interaction_events()
            self.assertEqual(len(loaded), 1)
            self.assertIs(loaded[0].raw_content_stored, False)

    def test_dict_to_event_always_false(self):
        d = _event_to_dict(_make_event())
        d["raw_content_stored"] = True
        evt = _dict_to_event(d)
        self.assertIs(evt.raw_content_stored, False)


class TestInteractionsCLI(unittest.TestCase):

    def test_interactions_no_events(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["interactions"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("No interaction events", result.output)

    def test_interactions_shows_recent(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([
                _make_event(event_type="feedback_accepted", summary="first"),
                _make_event(event_type="feedback_rejected", summary="second"),
            ])
            result = runner.invoke(cli, ["interactions", "--last", "1"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("feedback_rejected", result.output)
            self.assertNotIn("feedback_accepted", result.output)

    def test_export_interactions_no_events(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["export-interactions"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("No interaction events", result.output)

    def test_export_interactions_jsonl_output(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([
                _make_event(event_type="feedback_accepted"),
                _make_event(event_type="feedback_rejected"),
            ])
            result = runner.invoke(cli, ["export-interactions"])
            self.assertEqual(result.exit_code, 0)
            lines = [ln for ln in result.output.strip().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            for ln in lines:
                row = json.loads(ln)
                self.assertIn("event_type", row)
                self.assertIs(row["raw_content_stored"], False)


class TestSanitisation(unittest.TestCase):
    """sanitize_event caps + scrubs fields and validates enums."""

    def test_summary_secret_dropped(self):
        evt = _make_event(summary=f"leaked key {_FAKE_SECRET} here")
        self.assertEqual(sanitize_event(evt).summary, "")

    def test_summary_absolute_path_dropped(self):
        evt = _make_event(summary=_ABS_POSIX)
        self.assertEqual(sanitize_event(evt).summary, "")

    def test_summary_capped(self):
        evt = _make_event(summary="x" * 500)
        self.assertLessEqual(len(sanitize_event(evt).summary), 120)

    def test_correction_reason_sanitised(self):
        evt = _make_event(correction_reason=_ABS_WIN)
        self.assertIsNone(sanitize_event(evt).correction_reason)
        clean = _make_event(correction_reason="wrong-file")
        self.assertEqual(sanitize_event(clean).correction_reason, "wrong-file")

    def test_related_file_paths_relative_only(self):
        evt = _make_event(related_file_paths=["src/ok.py", _ABS_POSIX, _ABS_WIN])
        self.assertEqual(sanitize_event(evt).related_file_paths, ["src/ok.py"])

    def test_related_file_paths_keep_long_nested_relative_paths(self):
        # A repo-relative path with several nested directories must not be
        # dropped by the generic "long opaque key-like run" secret heuristic.
        deep = "evals/basic/bug_fix/fixtures/word_utils.py"
        evt = _make_event(related_file_paths=[deep, "src/ok.py"])
        self.assertEqual(sanitize_event(evt).related_file_paths, [deep, "src/ok.py"])

    def test_metadata_scalar_only(self):
        evt = _make_event(
            metadata={
                "rating": "bad",
                "count": 3,
                "ok": True,
                "nested": {"a": 1},
                "items": [1, 2, 3],
                "secret": _FAKE_SECRET,
                "path": _ABS_POSIX,
            }
        )
        meta = sanitize_event(evt).metadata
        self.assertEqual(meta, {"rating": "bad", "count": 3, "ok": True})

    def test_invalid_event_type_fallback(self):
        self.assertEqual(sanitize_event(_make_event(event_type="bogus")).event_type, "unclear_output")

    def test_canonical_event_type_preserved(self):
        self.assertEqual(sanitize_event(_make_event(event_type="wrong_file")).event_type, "wrong_file")

    def test_legacy_event_type_preserved(self):
        for et in ("feedback_accepted", "feedback_rejected", "feedback_abandoned"):
            self.assertEqual(sanitize_event(_make_event(event_type=et)).event_type, et)

    def test_invalid_severity_fallback(self):
        self.assertEqual(sanitize_event(_make_event(severity="warning")).severity, "info")

    def test_valid_severity_preserved(self):
        self.assertEqual(sanitize_event(_make_event(severity="high")).severity, "high")

    def test_raw_content_stored_always_false(self):
        self.assertIs(sanitize_event(_make_event()).raw_content_stored, False)


class TestSanitiseOnWrite(unittest.TestCase):

    def test_new_writes_sanitised_at_log_time(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            log_interaction_event(
                _make_event(
                    summary=f"{_ABS_POSIX} {_FAKE_SECRET}",
                    correction_reason=_ABS_WIN,
                    related_file_paths=[_ABS_POSIX, "src/ok.py"],
                    metadata={"secret": _FAKE_SECRET, "rating": "bad"},
                )
            )
            raw = (Path(".openshard") / "interactions.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(_FAKE_SECRET, raw)
            self.assertNotIn(_ABS_POSIX, raw)
            self.assertNotIn("C:\\Users", raw)
            stored = json.loads(raw.strip())
            self.assertEqual(stored["summary"], "")
            self.assertIsNone(stored["correction_reason"])
            self.assertEqual(stored["related_file_paths"], ["src/ok.py"])
            self.assertEqual(stored["metadata"], {"rating": "bad"})
            self.assertIs(stored["raw_content_stored"], False)


class TestLegacyEntryNoLeak(unittest.TestCase):
    """Unsafe entries already on disk must not leak through display or export."""

    def _raw_unsafe_record(self) -> dict:
        return {
            "schema_version": 1,
            "event_id": "legacy-1",
            "run_id": "run-legacy",
            "timestamp": "2025-01-01T00:00:00Z",
            "actor": "developer",
            "event_type": "feedback_rejected",
            "summary": f"{_ABS_POSIX} contains {_FAKE_SECRET}",
            "related_stage": "execution",
            "related_file_paths": ["/etc/passwd", "src/ok.py"],
            "correction_reason": _ABS_WIN,
            "severity": "warning",
            "accepted": False,
            "metadata": {"secret": _FAKE_SECRET, "path": _ABS_POSIX, "rating": "bad"},
            "raw_content_stored": True,
        }

    def test_legacy_unsafe_not_leaked_in_interactions_last(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_raw_jsonl([self._raw_unsafe_record()])
            result = runner.invoke(cli, ["interactions", "--last", "5"])
            self.assertEqual(result.exit_code, 0)
            self.assertNotIn(_FAKE_SECRET, result.output)
            self.assertNotIn(_ABS_POSIX, result.output)
            self.assertNotIn("/etc/passwd", result.output)
            self.assertNotIn("C:\\Users", result.output)

    def test_legacy_unsafe_not_leaked_in_export(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_raw_jsonl([self._raw_unsafe_record()])
            result = runner.invoke(cli, ["export-interactions"])
            self.assertEqual(result.exit_code, 0)
            self.assertNotIn(_FAKE_SECRET, result.output)
            self.assertNotIn(_ABS_POSIX, result.output)
            self.assertNotIn("/etc/passwd", result.output)
            self.assertNotIn("C:\\Users", result.output)
            row = json.loads(result.output.strip())
            self.assertEqual(row["summary"], "")
            self.assertIsNone(row["correction_reason"])
            self.assertEqual(row["related_file_paths"], ["src/ok.py"])
            self.assertEqual(row["metadata"], {"rating": "bad"})

    def test_redacted_clears_all_four_fields(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_interactions([
                _make_event(
                    summary="clean summary",
                    correction_reason="wrong-file",
                    related_file_paths=["src/ok.py"],
                    metadata={"rating": "good"},
                )
            ])
            result = runner.invoke(cli, ["export-interactions", "--redacted"])
            self.assertEqual(result.exit_code, 0)
            row = json.loads(result.output.strip())
            self.assertEqual(row["summary"], "[redacted]")
            self.assertIsNone(row["correction_reason"])
            self.assertEqual(row["related_file_paths"], [])
            self.assertEqual(row["metadata"], {})


if __name__ == "__main__":
    unittest.main()
