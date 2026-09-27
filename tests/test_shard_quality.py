"""Tests for openshard.history.shard_quality - Shard Quality Summary v1.

Covers strong, usable, weak/partial, unsafe, old record, malformed input,
empty dict (a valid record, not the fallback), receipt reuse, JSON
serializability, and the no-em-dash rule.
"""

from __future__ import annotations

import json
import unittest

from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_quality import (
    SUMMARY_VERSION,
    build_shard_quality_summary,
)

_EXPECTED_KEYS = {
    "summary_version",
    "status",
    "required_proof",
    "recommended_gaps_count",
    "unsafe_findings_count",
    "verification",
    "verification_state",
    "verification_authority",
    "integrity",
    "raw_output_stored",
    "summary",
}


def _strong_entry(**kwargs) -> dict:
    """An entry that should populate every required and recommended section."""
    entry = {
        "schema_version": "1.2",
        "shard_id": "shard-20260604-0001",
        "timestamp": "2026-06-04T10:00:00Z",
        "task": "implement a feature",
        "workflow": "native",
        "execution_profile": "native_deep",
        "execution_model": "openrouter/some-model",
        "duration_seconds": 12.5,
        "estimated_cost": 0.0123,
        "files_created": 1,
        "files_updated": 1,
        "files_deleted": 0,
        "files_detail": [{"path": "a.py", "change_type": "create", "summary": ""}],
        "verification_attempted": True,
        "verification_passed": True,
        "review_checks": [
            {"name": "ruff", "status": "passed"},
            {"name": "pytest", "status": "passed"},
        ],
        "context_files_injected_count": 4,
        "context_utilisation_ratio": 0.5,
        "policy_decisions": [{"decision_id": "p1", "decision": "allow"}],
        "approval_receipt": {"granted": True, "reason": "ok"},
        "run_timeline": [
            {"event": "run_started", "label": "Run started", "kind": "run", "status": "completed"},
        ],
        "evidence_capsules": [
            {"capsule_id": "c1", "kind": "inspection", "summary": "read a.py"},
        ],
        "git_base_branch": "main",
        "git_head_commit_hash": "abc1234",
        "git_dirty": False,
        "summary": "Implemented the feature and verified it.",
        "developer_feedback": {"rating": "good", "action": "accepted"},
    }
    entry.update(kwargs)
    return entry


class TestShape(unittest.TestCase):
    def test_all_keys_present(self) -> None:
        summary = build_shard_quality_summary(_strong_entry())
        self.assertEqual(set(summary), _EXPECTED_KEYS)

    def test_summary_version_constant(self) -> None:
        self.assertEqual(SUMMARY_VERSION, "1")
        self.assertEqual(build_shard_quality_summary(_strong_entry())["summary_version"], "1")

    def test_json_serializable(self) -> None:
        # Must round-trip as valid JSON with no surprises.
        for entry in (_strong_entry(), {"task": "x"}, {}, None):
            json.dumps(build_shard_quality_summary(entry))


class TestStrong(unittest.TestCase):
    def test_strong_run(self) -> None:
        summary = build_shard_quality_summary(_strong_entry())
        self.assertEqual(summary["status"], "strong")
        self.assertEqual(summary["required_proof"], "present")
        self.assertEqual(summary["recommended_gaps_count"], 0)
        self.assertEqual(summary["unsafe_findings_count"], 0)
        self.assertEqual(summary["verification"], "passed")
        self.assertIn("Required proof present", summary["summary"])
        self.assertIn("verification passed", summary["summary"])


class TestUsable(unittest.TestCase):
    def test_recommended_gaps_make_it_usable(self) -> None:
        # Required sections intact, but timeline and provenance are absent, so
        # they surface as recommended gaps.
        entry = _strong_entry()
        for key in ("run_timeline", "evidence_capsules", "git_base_branch",
                    "git_head_commit_hash", "git_dirty"):
            entry.pop(key, None)
        summary = build_shard_quality_summary(entry)
        self.assertEqual(summary["status"], "usable")
        self.assertEqual(summary["required_proof"], "present")
        self.assertGreater(summary["recommended_gaps_count"], 0)
        self.assertEqual(summary["unsafe_findings_count"], 0)
        self.assertIn("recommended gap", summary["summary"])


class TestWeakPartial(unittest.TestCase):
    def test_minimal_entry_required_incomplete(self) -> None:
        summary = build_shard_quality_summary({"task": "do a thing"})
        self.assertIn(summary["status"], {"partial", "weak"})
        self.assertEqual(summary["required_proof"], "incomplete")
        self.assertIn("Required proof incomplete", summary["summary"])


class TestUnsafe(unittest.TestCase):
    def test_blocked_field_makes_it_unsafe(self) -> None:
        entry = _strong_entry(raw_diff="raw diff body that must never appear")
        summary = build_shard_quality_summary(entry)
        self.assertEqual(summary["status"], "unsafe")
        self.assertGreaterEqual(summary["unsafe_findings_count"], 1)
        # The unsafe clause leads the sentence.
        self.assertTrue(summary["summary"].startswith("1 unsafe finding"), summary["summary"])
        # The raw value must never leak into the safe summary.
        self.assertNotIn("raw diff body", json.dumps(summary))


class TestOldRecord(unittest.TestCase):
    def test_old_record_no_crash_unknown_verification(self) -> None:
        entry = {"task": "x", "timestamp": "2020-01-01T00:00:00Z"}
        summary = build_shard_quality_summary(entry)
        self.assertEqual(summary["verification"], "unknown")
        self.assertFalse(summary["raw_output_stored"])
        self.assertIn(summary["status"], {"partial", "weak"})


class TestMalformedAndEmpty(unittest.TestCase):
    def test_non_dict_is_fallback(self) -> None:
        for bad in (None, [], "a string", 42, 3.14, ("a", "b")):
            summary = build_shard_quality_summary(bad)
            self.assertEqual(summary["status"], "unknown")
            self.assertEqual(summary["required_proof"], "incomplete")
            self.assertEqual(summary["verification"], "unknown")
            self.assertEqual(summary["summary"], "Quality summary unavailable")

    def test_empty_dict_is_not_fallback(self) -> None:
        # An empty dict is a valid record: it flows through the proof contract
        # and reads as weak/partial evidence, never the catastrophic fallback.
        summary = build_shard_quality_summary({})
        self.assertIn(summary["status"], {"partial", "weak"})
        self.assertEqual(summary["required_proof"], "incomplete")
        self.assertNotEqual(summary["summary"], "Quality summary unavailable")


class TestReceiptReuse(unittest.TestCase):
    def test_prebuilt_receipt_matches_internal(self) -> None:
        entry = _strong_entry()
        receipt = build_shard_receipt(entry, index=None)
        self.assertEqual(
            build_shard_quality_summary(entry, receipt),
            build_shard_quality_summary(entry),
        )


class TestNoEmDash(unittest.TestCase):
    def test_summary_has_no_em_dash(self) -> None:
        em_dash = chr(0x2014)  # em dash, kept out of the source as a literal
        for entry in (_strong_entry(), {"task": "x"}, {}, None,
                      _strong_entry(raw_diff="leak")):
            self.assertNotIn(em_dash, build_shard_quality_summary(entry)["summary"])


if __name__ == "__main__":
    unittest.main()
