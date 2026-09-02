"""Tests for wiring canonical Events (Migration 3) into ShardReceipt / CLI JSON."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.history.event import EVENT_UNKNOWN, SOURCE_POLICY_DECISIONS
from openshard.history.shard_contract import build_shard_receipt

# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_provenance.py)
# ---------------------------------------------------------------------------


def _assert_no_unsafe(text: str) -> None:
    for needle in (
        "C:\\", "C:/", "/Users/", "/home/", "/etc/",
        "sk-", "AKIA", "api_key=", "password=", "secret=",
    ):
        assert needle.lower() not in text.lower(), (
            f"unsafe substring {needle!r} leaked in: {text!r}"
        )


def _check(name: str = "terraform_fmt", status: str = "passed", summary: str = "formatting ok"):
    return {"name": name, "status": status, "summary": summary}


def _timeline_event(
    event: str = "repo_scanned",
    label: str | None = "Repository scanned",
    kind: str = "scan",
    status: str = "completed",
) -> dict:
    ev: dict = {"event": event, "kind": kind, "status": status}
    if label is not None:
        ev["label"] = label
    return ev


def _policy_decision(
    decision_id: str | None = "550e8400-e29b-41d4-a716-446655440000",
    action: str = "write",
    decision: str = "allow",
    reason: str = "Approved by path policy",
    source: str = "path_policy",
) -> dict:
    return {
        "decision_id": decision_id,
        "action": action,
        "decision": decision,
        "reason": reason,
        "source": source,
    }


_BASE_ENTRY = {
    "schema_version": "1.1",
    "task": "Add a helper function",
    "timestamp": "2026-06-01T00:00:00Z",
    "execution_model": "claude-sonnet-4-6",
    "verification_attempted": True,
    "verification_passed": True,
}


def _write_log(td: str, entries: list[dict]) -> Path:
    log_dir = Path(td) / ".openshard"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "runs.jsonl"
    log_file.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return log_file


def _invoke_last_json(entries: list[dict]):
    from openshard.cli.main import cli
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        _write_log(td, entries)
        with patch("openshard.cli.main.Path.cwd", return_value=Path(td)):
            result = runner.invoke(cli, ["last", "--json"])
    return result


# ---------------------------------------------------------------------------
# TestEventsWiredIntoShardReceipt
# ---------------------------------------------------------------------------


class TestEventsWiredIntoShardReceipt(unittest.TestCase):
    def test_events_field_exists_on_receipt(self):
        receipt = build_shard_receipt({})
        self.assertTrue(hasattr(receipt, "events"))

    def test_events_defaults_to_empty_list_for_old_entry(self):
        receipt = build_shard_receipt({"task": "test", "timestamp": "2026-01-01T00:00:00Z"})
        self.assertEqual(receipt.events, [])

    def test_timeline_review_and_policy_entries_produce_events(self):
        entry = {
            "shard_id": "shard-20260101-0001",
            "run_timeline": [_timeline_event()],
            "review_checks": [_check()],
            "policy_decisions": [_policy_decision()],
        }
        receipt = build_shard_receipt(entry)
        self.assertGreater(len(receipt.events), 0)

    def test_never_raises_on_garbage_entry(self):
        entry = {
            "run_timeline": "totally-invalid",
            "review_checks": {"not": "a list"},
            "policy_decisions": 12345,
        }
        receipt = build_shard_receipt(entry)
        self.assertIsInstance(receipt.events, list)
        self.assertEqual(receipt.events, [])


# ---------------------------------------------------------------------------
# TestEventsInLastJson (CLI integration)
# ---------------------------------------------------------------------------


class TestEventsInLastJson(unittest.TestCase):
    def test_events_present_in_last_json_output(self):
        result = _invoke_last_json([_BASE_ENTRY])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertIn("events", data["run"])
        self.assertIsInstance(data["run"]["events"], list)

    def test_events_empty_for_old_entry(self):
        result = _invoke_last_json([_BASE_ENTRY])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data["run"]["events"], [])

    def test_events_populated_for_rich_entry(self):
        entry = {
            **_BASE_ENTRY,
            "shard_id": "shard-20260601-0001",
            "review_checks": [_check()],
        }
        result = _invoke_last_json([entry])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertGreater(len(data["run"]["events"]), 0)
        evt = data["run"]["events"][0]
        for key in (
            "event_id", "event_type", "source", "actor", "action",
            "status", "evidence", "raw_content_stored", "schema_version",
        ):
            self.assertIn(key, evt, f"key {key!r} missing from event record")
        self.assertFalse(evt["raw_content_stored"])

    def test_no_unsafe_values_in_events_json(self):
        entry = {
            **_BASE_ENTRY,
            "shard_id": "shard-20260601-0001",
            "review_checks": [_check()],
            "run_timeline": [_timeline_event()],
            "policy_decisions": [_policy_decision()],
        }
        result = _invoke_last_json([entry])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        _assert_no_unsafe(json.dumps(data["run"]["events"]))

    def test_events_absent_from_human_output(self):
        from openshard.cli.main import cli
        runner = CliRunner()
        entry = {
            **_BASE_ENTRY,
            "shard_id": "shard-20260601-0001",
            "review_checks": [_check()],
        }
        with tempfile.TemporaryDirectory() as td:
            _write_log(td, [entry])
            with patch("openshard.cli.main.Path.cwd", return_value=Path(td)):
                result = runner.invoke(cli, ["last"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("evt-", result.output)
        self.assertNotIn("event_id", result.output)

    def test_last_json_remains_valid_json(self):
        result = _invoke_last_json([_BASE_ENTRY])
        self.assertEqual(result.exit_code, 0)
        json.loads(result.output)  # must not raise


# ---------------------------------------------------------------------------
# TestEventOrderingIsDeterministic
#
# NOTE: deliberately does not assert on the specific internal source-priority
# sequence used by events_from_entry -- only that identical evidence always
# projects to the identical event order. The exact concatenation order is an
# implementation detail of the Migration 3 seam, not a contract this PR fixes.
# ---------------------------------------------------------------------------


class TestEventOrderingIsDeterministic(unittest.TestCase):
    def _rich_entry(self) -> dict:
        return {
            "shard_id": "shard-20260601-0002",
            "run_timeline": [_timeline_event("repo_scanned"), _timeline_event("tests_run", kind="check", status="completed")],
            "review_checks": [_check(), _check(name="ruff", status="failed", summary="lint errors")],
            "policy_decisions": [_policy_decision()],
        }

    def test_repeated_build_yields_identical_event_order(self):
        entry = self._rich_entry()
        receipt_a = build_shard_receipt(entry)
        receipt_b = build_shard_receipt(entry)
        ids_a = [e.event_id for e in receipt_a.events]
        ids_b = [e.event_id for e in receipt_b.events]
        self.assertEqual(ids_a, ids_b)
        self.assertGreater(len(ids_a), 0)

    def test_repeated_json_export_yields_identical_event_order(self):
        entry = {**_BASE_ENTRY, **self._rich_entry()}
        result_a = _invoke_last_json([entry])
        result_b = _invoke_last_json([entry])
        self.assertEqual(result_a.exit_code, 0)
        self.assertEqual(result_b.exit_code, 0)
        ids_a = [e["event_id"] for e in json.loads(result_a.output)["run"]["events"]]
        ids_b = [e["event_id"] for e in json.loads(result_b.output)["run"]["events"]]
        self.assertEqual(ids_a, ids_b)


# ---------------------------------------------------------------------------
# TestEventHonestyPreservedThroughReceipt
# ---------------------------------------------------------------------------


class TestEventHonestyPreservedThroughReceipt(unittest.TestCase):
    def test_policy_decision_event_has_no_actor(self):
        entry = {
            "shard_id": "shard-20260601-0003",
            "policy_decisions": [_policy_decision()],
        }
        receipt = build_shard_receipt(entry)
        policy_events = [e for e in receipt.events if e.source == SOURCE_POLICY_DECISIONS]
        self.assertGreater(len(policy_events), 0)
        for e in policy_events:
            self.assertIsNone(e.actor)

    def test_unknown_event_type_coerced_not_fabricated(self):
        entry = {
            "shard_id": "shard-20260601-0004",
            "run_timeline": [_timeline_event(event="mystery", kind="mystery", status="mystery")],
        }
        receipt = build_shard_receipt(entry)
        self.assertGreater(len(receipt.events), 0)
        self.assertTrue(any(e.event_type == EVENT_UNKNOWN for e in receipt.events))

    def test_legacy_entry_without_events_still_builds_receipt(self):
        entry = {"task": "legacy task", "timestamp": "2025-01-01T00:00:00Z"}
        receipt = build_shard_receipt(entry)
        self.assertEqual(receipt.events, [])
        self.assertEqual(receipt.provenance, [])
        self.assertEqual(receipt.task_full, "legacy task")


if __name__ == "__main__":
    unittest.main()
