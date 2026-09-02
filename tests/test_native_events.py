"""Tests for OSN (OpenShard Native) canonical Event embedding (Migration 6)."""

from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from openshard.execution.generator import ChangedFile
from openshard.history.event import (
    EVENT_FILE_CHANGED,
    EVENT_RETRY_STARTED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_INVOKED,
    EVENT_VERIFICATION_FAILED,
    EVENT_VERIFICATION_PASSED,
    EVENT_VERIFICATION_SKIPPED,
    EVIDENCE_DIRECTLY_OBSERVED,
    SOURCE_NATIVE_RUN,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    events_for_run,
)
from openshard.history.shard_contract import build_shard_receipt
from openshard.run._pipeline_helpers import _build_native_events
from openshard.run.pipeline import _log_run

# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_event_receipt_wiring.py)
# ---------------------------------------------------------------------------


def _assert_no_unsafe(text: str) -> None:
    for needle in (
        "C:\\", "C:/", "/Users/", "/home/", "/etc/",
        "sk-", "AKIA", "api_key=", "password=", "secret=",
    ):
        assert needle.lower() not in text.lower(), (
            f"unsafe substring {needle!r} leaked in: {text!r}"
        )


def _entry(**extra) -> dict:
    base = {
        "run_id": "2026-06-01T00:00:00Z",
        "timestamp": "2026-06-01T00:00:00Z",
        "shard_id": "shard-20260601-0001",
        "attempt_number": 1,
        "executor": "native",
    }
    base.update(extra)
    return base


def _files() -> list[ChangedFile]:
    return [
        ChangedFile(path="src/foo.py", change_type="update", content="", summary="edited foo"),
        ChangedFile(path="src/bar.py", change_type="create", content="", summary="new bar"),
    ]


def _tool_trace() -> list[dict]:
    return [
        {"tool": "read_file", "ok": True, "approved": True, "output_chars": 120, "error": None},
        {"tool": "run_verification", "ok": False, "approved": True, "output_chars": 0, "error": "exit 1"},
    ]


# ---------------------------------------------------------------------------
# Unit tests: _build_native_events
# ---------------------------------------------------------------------------


class TestBuildNativeEventsUnit(unittest.TestCase):
    def test_covers_run_started_files_tools_verification_and_completion(self):
        entry = _entry(tool_trace=_tool_trace())
        events = _build_native_events(entry, _files(), True, True, False)
        types = [e["event_type"] for e in events]
        self.assertIn(EVENT_RUN_STARTED, types)
        self.assertEqual(types.count(EVENT_TOOL_INVOKED), 2)
        self.assertEqual(types.count(EVENT_FILE_CHANGED), 2)
        self.assertIn(EVENT_VERIFICATION_PASSED, types)
        self.assertIn(EVENT_RUN_COMPLETED, types)
        self.assertNotIn(EVENT_RUN_FAILED, types)

    def test_evidence_is_directly_observed_throughout(self):
        entry = _entry(tool_trace=_tool_trace())
        events = _build_native_events(entry, _files(), True, False, True)
        self.assertTrue(events, "expected at least one event")
        for e in events:
            self.assertEqual(e["evidence"], EVIDENCE_DIRECTLY_OBSERVED)

    def test_tool_invoked_status_reflects_ok_flag(self):
        entry = _entry(tool_trace=_tool_trace())
        events = _build_native_events(entry, [], False, None, False)
        tool_events = [e for e in events if e["event_type"] == EVENT_TOOL_INVOKED]
        self.assertEqual(len(tool_events), 2)
        self.assertEqual(tool_events[0]["status"], STATUS_PASSED)
        self.assertEqual(tool_events[1]["status"], STATUS_FAILED)

    def test_tool_invoked_target_stays_none(self):
        entry = _entry(tool_trace=_tool_trace())
        events = _build_native_events(entry, [], False, None, False)
        for e in events:
            if e["event_type"] == EVENT_TOOL_INVOKED:
                self.assertIsNone(e["target"])

    def test_file_changed_status_is_unknown_not_pass_fail(self):
        entry = _entry()
        events = _build_native_events(entry, _files(), False, None, False)
        file_events = [e for e in events if e["event_type"] == EVENT_FILE_CHANGED]
        self.assertEqual(len(file_events), 2)
        for e in file_events:
            self.assertEqual(e["status"], STATUS_UNKNOWN)
        self.assertEqual({e["target"] for e in file_events}, {"src/foo.py", "src/bar.py"})

    def test_verification_failed_maps_to_verification_failed_and_run_failed(self):
        entry = _entry()
        events = _build_native_events(entry, [], True, False, False)
        types = [e["event_type"] for e in events]
        self.assertIn(EVENT_VERIFICATION_FAILED, types)
        self.assertIn(EVENT_RUN_FAILED, types)
        self.assertNotIn(EVENT_RUN_COMPLETED, types)

    def test_verification_not_attempted_maps_to_skipped(self):
        entry = _entry()
        events = _build_native_events(entry, [], False, None, False)
        types = [e["event_type"] for e in events]
        self.assertIn(EVENT_VERIFICATION_SKIPPED, types)

    def test_retry_triggered_adds_retry_started_event(self):
        entry = _entry()
        events = _build_native_events(entry, [], False, None, True)
        types = [e["event_type"] for e in events]
        self.assertIn(EVENT_RETRY_STARTED, types)

    def test_no_retry_event_when_not_triggered(self):
        entry = _entry()
        events = _build_native_events(entry, [], False, None, False)
        types = [e["event_type"] for e in events]
        self.assertNotIn(EVENT_RETRY_STARTED, types)

    def test_linkage_fields_match_entry(self):
        entry = _entry()
        events = _build_native_events(entry, _files(), True, True, False)
        for e in events:
            self.assertEqual(e["run_id"], entry["run_id"])
            self.assertEqual(e["shard_id"], entry["shard_id"])
            self.assertEqual(e["attempt_number"], entry["attempt_number"])

    def test_actor_is_never_invented(self):
        entry = _entry()
        events = _build_native_events(entry, _files(), True, True, False)
        for e in events:
            self.assertIsNone(e["actor"])

    def test_source_is_native_run(self):
        entry = _entry()
        events = _build_native_events(entry, _files(), True, True, False)
        for e in events:
            self.assertEqual(e["source"], SOURCE_NATIVE_RUN)

    def test_event_ids_are_unique_across_calls(self):
        entry = _entry()
        events_a = _build_native_events(entry, _files(), True, True, False)
        events_b = _build_native_events(entry, _files(), True, True, False)
        ids_a = {e["event_id"] for e in events_a}
        ids_b = {e["event_id"] for e in events_b}
        self.assertEqual(len(ids_a), len(events_a))
        self.assertTrue(ids_a.isdisjoint(ids_b))

    def test_malformed_input_never_raises(self):
        events = _build_native_events({}, [], True, True, False)
        self.assertIsInstance(events, list)
        events = _build_native_events(_entry(tool_trace="not-a-list"), [], False, None, False)
        self.assertIsInstance(events, list)
        events = _build_native_events(_entry(tool_trace=[1, "x", None, {}]), [], False, None, False)
        self.assertIsInstance(events, list)

    def test_no_unsafe_values_leak(self):
        entry = _entry(
            tool_trace=[{"tool": "read_file", "ok": True, "approved": True,
                         "output_chars": 5, "error": None}],
        )
        files = [ChangedFile(path="C:\\Users\\me\\secret.py", change_type="update",
                              content="", summary="")]
        events = _build_native_events(entry, files, True, True, False)
        _assert_no_unsafe(json.dumps(events))


# ---------------------------------------------------------------------------
# Integration: _log_run wiring
# ---------------------------------------------------------------------------


class _FakeFile:
    def __init__(self, sink: list[str]):
        self._sink = sink

    def write(self, s: str) -> int:
        self._sink.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _run_log_run(*, effective_executor, extra_metadata, captured: list[str]):
    def _fake_path_open(self, mode="r", encoding=None, **kw):
        if mode == "a":
            return _FakeFile(captured)
        import builtins
        return builtins.open(str(self), mode, encoding=encoding, **kw)

    gen_mock = MagicMock()
    gen_mock.model = "mock-model"
    gen_mock.fixer_model = "mock-fixer"

    with patch("pathlib.Path.open", _fake_path_open), \
         patch("pathlib.Path.mkdir"), \
         patch("openshard.history.jsonl_store.os.fsync", lambda fd: None):
        _log_run(
            start=time.time(),
            task="implement a helper",
            generator=gen_mock,
            retry_triggered=False,
            files=[ChangedFile(path="src/x.py", change_type="update", content="", summary="")],
            verification_attempted=True,
            verification_passed=True,
            workspace=None,
            run_index=1,
            effective_executor=effective_executor,
            extra_metadata=extra_metadata,
        )


class TestLogRunEmbedsNativeEvents(unittest.TestCase):
    def test_native_run_gets_embedded_events(self):
        captured: list[str] = []
        _run_log_run(
            effective_executor="native",
            extra_metadata={"executor": "native", "tool_trace": _tool_trace()},
            captured=captured,
        )
        self.assertTrue(captured)
        entry = json.loads(captured[0].strip())
        self.assertIn("events", entry)
        self.assertTrue(entry["events"])
        for e in entry["events"]:
            self.assertEqual(e["run_id"], entry["run_id"])
            self.assertEqual(e["shard_id"], entry["shard_id"])
            self.assertEqual(e["attempt_number"], entry["attempt_number"])

    def test_native_run_without_tool_trace_still_gets_core_events(self):
        """Dry-run / verify-failure call sites may not merge tool_trace; core
        facts (run-started/file-changed/verification/run-completed) must
        still be embedded honestly, just without tool.invoked coverage."""
        captured: list[str] = []
        _run_log_run(
            effective_executor="native",
            extra_metadata={"executor": "native"},
            captured=captured,
        )
        entry = json.loads(captured[0].strip())
        types = [e["event_type"] for e in entry["events"]]
        self.assertIn(EVENT_RUN_STARTED, types)
        self.assertNotIn(EVENT_TOOL_INVOKED, types)


class TestNonNativeRunsUnaffected(unittest.TestCase):
    def test_no_executor_means_no_events_key(self):
        captured: list[str] = []
        _run_log_run(effective_executor=None, extra_metadata=None, captured=captured)
        entry = json.loads(captured[0].strip())
        self.assertNotIn("events", entry)

    def test_opencode_executor_means_no_events_key(self):
        captured: list[str] = []
        _run_log_run(
            effective_executor="opencode",
            extra_metadata={"tier_dispatch_receipt": {}},
            captured=captured,
        )
        entry = json.loads(captured[0].strip())
        self.assertNotIn("events", entry)


# ---------------------------------------------------------------------------
# No duplicates: events_for_run must not double-count an embedded-events entry
# ---------------------------------------------------------------------------


class TestNoDuplicateEventsForModernRun(unittest.TestCase):
    def test_events_for_run_returns_embedded_only(self):
        entry = _entry(tool_trace=_tool_trace())
        embedded = _build_native_events(entry, _files(), True, True, False)
        entry["events"] = embedded
        events = events_for_run(entry["run_id"], entry=entry)
        self.assertEqual({e.event_id for e in events}, {e["event_id"] for e in embedded})
        self.assertEqual(len(events), len(embedded))


# ---------------------------------------------------------------------------
# Legacy native entries (no "events" key) still project via Migration 3
# ---------------------------------------------------------------------------


class TestLegacyNativeEntryStillProjects(unittest.TestCase):
    def test_legacy_native_entry_without_events_key_builds_receipt(self):
        entry = {
            "executor": "native",
            "workflow": "native",
            "task": "legacy native task",
            "timestamp": "2025-01-01T00:00:00Z",
            "run_id": "2025-01-01T00:00:00Z",
            "shard_id": "shard-20250101-0001",
        }
        self.assertNotIn("events", entry)
        receipt = build_shard_receipt(entry)
        # No timeline/checkpoints/steps fixtures on disk -> no events, but
        # this must not raise and must not fabricate any.
        self.assertEqual(receipt.events, [])


# ---------------------------------------------------------------------------
# Receipt / CLI see the exact embedded native events, unchanged
# ---------------------------------------------------------------------------


class TestReceiptSeesEmbeddedNativeEvents(unittest.TestCase):
    def test_receipt_events_match_embedded_exactly(self):
        entry = _entry(tool_trace=_tool_trace())
        embedded = _build_native_events(entry, _files(), True, True, False)
        entry["events"] = embedded
        receipt = build_shard_receipt(entry)
        self.assertEqual({e.event_id for e in receipt.events}, {e["event_id"] for e in embedded})
        self.assertEqual(len(receipt.events), len(embedded))

    def test_receipt_events_linkage_matches_receipt(self):
        entry = _entry(tool_trace=_tool_trace())
        entry["events"] = _build_native_events(entry, _files(), True, True, False)
        receipt = build_shard_receipt(entry)
        for e in receipt.events:
            self.assertEqual(e.run_id, receipt.run_id)
            self.assertEqual(e.shard_id, receipt.shard_id)
            self.assertEqual(e.attempt_number, receipt.attempt_number)


if __name__ == "__main__":
    unittest.main()
