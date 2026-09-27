"""Tests for the Shard quality surface in `openshard last`.

Covers the `shard_quality` block in `last --json` and the compact human proof
line, plus a regression guard that `proof last`, `trust last`, and `ci check`
still render their key lines.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.cli.main import cli

_STRONG_ENTRY = {
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

# Same record, but with a blocked raw field so it reads as unsafe.
_UNSAFE_ENTRY = dict(_STRONG_ENTRY, raw_diff="raw diff body that must never appear")

_QUALITY_KEYS = {
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


def _invoke(args: list[str], *, runs: list[dict] | None):
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        log_dir = Path(td) / ".openshard"
        log_dir.mkdir()
        if runs is not None:
            (log_dir / "runs.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in runs), encoding="utf-8"
            )
        with patch("openshard.cli.main.Path.cwd", return_value=Path(td)):
            result = runner.invoke(cli, args)
    return result


class TestLastJsonQuality(unittest.TestCase):
    def test_shard_quality_block_present_and_valid_json(self):
        result = _invoke(["last", "--json"], runs=[_STRONG_ENTRY])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertIn("shard_quality", payload)
        quality = payload["shard_quality"]
        self.assertEqual(set(quality), _QUALITY_KEYS)
        self.assertEqual(quality["status"], "strong")
        self.assertEqual(quality["required_proof"], "present")
        self.assertEqual(quality["verification"], "passed")

    def test_unsafe_run_json_counts(self):
        result = _invoke(["last", "--json"], runs=[_UNSAFE_ENTRY])
        payload = json.loads(result.output)
        quality = payload["shard_quality"]
        self.assertEqual(quality["status"], "unsafe")
        self.assertGreaterEqual(quality["unsafe_findings_count"], 1)
        # No raw blocked value leaks into the JSON.
        self.assertNotIn("raw diff body", result.output)


class TestLastJsonContentHash(unittest.TestCase):
    def test_missing_hash_for_legacy_record(self):
        # Raw entry (no content_hash) → legacy shape reports "missing".
        result = _invoke(["last", "--json"], runs=[_STRONG_ENTRY])
        payload = json.loads(result.output)
        self.assertIsNone(payload["content_hash"])
        self.assertEqual(payload["content_hash_status"], "missing")
        self.assertTrue(payload["computed_content_hash"].startswith("sha256:"))

    def test_valid_when_record_carries_matching_hash(self):
        from openshard.history.shard_hash import compute_shard_hash

        entry = dict(_STRONG_ENTRY)
        entry["content_hash"] = compute_shard_hash(entry)
        result = _invoke(["last", "--json"], runs=[entry])
        payload = json.loads(result.output)
        self.assertEqual(payload["content_hash"], entry["content_hash"])
        self.assertEqual(payload["content_hash_status"], "valid")
        self.assertNotIn("computed_content_hash", payload)


class TestLastHumanProofLine(unittest.TestCase):
    def test_proof_line_shown(self):
        result = _invoke(["last"], runs=[_STRONG_ENTRY])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Proof: strong", result.output)
        # No unsafe findings, so that line is suppressed.
        self.assertNotIn("Unsafe findings:", result.output)

    def test_unsafe_findings_line_shown_only_when_unsafe(self):
        result = _invoke(["last"], runs=[_UNSAFE_ENTRY])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Proof: unsafe", result.output)
        self.assertIn("Unsafe findings:", result.output)
        self.assertNotIn("raw diff body", result.output)


class TestNoRegression(unittest.TestCase):
    def test_proof_last_unchanged_key_lines(self):
        result = _invoke(["proof", "last"], runs=[_STRONG_ENTRY])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertTrue(result.output.startswith("Proof for last run"), msg=result.output)
        self.assertIn("Status:", result.output)

    def test_trust_last_still_runs(self):
        result = _invoke(["trust", "last"], runs=[_STRONG_ENTRY])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("OpenShard Trust Score:", result.output)

    def test_ci_check_still_runs(self):
        # _STRONG_ENTRY verified and was approved, so the verdict is a clean pass.
        result = _invoke(["ci", "check"], runs=[_STRONG_ENTRY])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("OpenShard CI Check: pass", result.output)


if __name__ == "__main__":
    unittest.main()
