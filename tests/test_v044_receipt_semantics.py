"""v0.4.4 Phase 6 -- Receipt semantic hardening.

The Receipt never claims more than its evidence supports: a finished agent
turn is not "Completed", a recorded risk is shown as recorded, an unkeyed
content hash is "Integrity  Matches", not a signature, and both identities
are shown.
"""

from __future__ import annotations

import re

from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.views import receipt_to_dict


def _hooks_entry(**extra) -> dict:
    entry = {
        "schema_version": "1.1",
        "timestamp": "2026-09-14T10:00:00Z",
        "task": "wire the thing",
        "executor": "claude_code_hooks",
        "shard_id": "shard-20260914-0003",
        "receipt_id": "rcpt_" + "a" * 32,
        "files_created": 0, "files_updated": 0, "files_deleted": 0,
        "capture": {"session_id": "s-1", "task_status": "turn_completed", "hook_events_dropped": 0},
    }
    entry.update(extra)
    return entry


class TestStatusWording:
    def test_turn_completed_is_not_rendered_as_completed(self):
        receipt = build_shard_receipt(_hooks_entry())
        assert receipt.task_completion == "Turn completed (unverified)"
        out = render_compact_shard_receipt(receipt)
        status_line = next(ln for ln in out.splitlines() if ln.strip().startswith("Status"))
        assert "unverified" in status_line
        assert not re.search(r"\bCompleted\b(?!.*unverified)", status_line)

    def test_session_end_without_turn_is_explicit(self):
        receipt = build_shard_receipt(_hooks_entry(capture={"session_id": "s", "task_status": "ended_no_turn"}))
        assert receipt.task_completion == "Session ended (no turn observed)"

    def test_in_progress_unchanged(self):
        receipt = build_shard_receipt(_hooks_entry(capture={"session_id": "s", "task_status": "in_progress"}))
        assert receipt.task_completion == "In progress"


class TestRiskIsShownAsRecorded:
    def test_review_task_missing_risk_stays_not_recorded(self):
        receipt = build_shard_receipt({"task": "review", "timestamp": "2026-09-14T10:00:00Z", "is_review_task": True})
        assert receipt.risk == "Not recorded"

    def test_review_task_low_risk_stays_low(self):
        receipt = build_shard_receipt({
            "task": "review", "timestamp": "2026-09-14T10:00:00Z", "is_review_task": True,
            "form_factor": {"risk_level": "low"},
        })
        assert receipt.risk == "Low"

    def test_recorded_high_risk_still_high(self):
        receipt = build_shard_receipt({"task": "x", "timestamp": "t", "form_factor": {"risk_level": "high"}})
        assert receipt.risk == "High"


class TestIntegrityWording:
    def test_matching_hash_says_matches_never_signed(self):
        entry = _hooks_entry()
        entry["content_hash"] = compute_shard_hash(entry)
        receipt = build_shard_receipt(entry)
        assert receipt.integrity == "Matches (content hash)"
        for out in (render_compact_shard_receipt(receipt), render_full_shard_receipt(receipt)):
            assert "Integrity" in out and "Matches (content hash)" in out
            assert "sign" not in out.lower()  # no "signed" / "signature" claim anywhere

    def test_edited_record_reports_mismatch(self):
        entry = _hooks_entry()
        entry["content_hash"] = compute_shard_hash(entry)
        entry["task"] = "edited after the fact"
        receipt = build_shard_receipt(entry)
        assert receipt.integrity == "Mismatch (content hash)"
        assert "Mismatch (content hash)" in render_compact_shard_receipt(receipt)

    def test_legacy_record_without_hash_is_not_recorded(self):
        receipt = build_shard_receipt({"task": "old", "timestamp": "2026-01-01T00:00:00Z"})
        assert receipt.integrity == "Not recorded"
        assert receipt_to_dict(receipt)["integrity"] == "Not recorded"


class TestIdentityRows:
    def test_receipt_shows_both_identities(self):
        receipt = build_shard_receipt(_hooks_entry())
        out = render_compact_shard_receipt(receipt)
        assert "Receipt ID" in out and "rcpt_" + "a" * 32 in out
        assert "shard-20260914-0003" in out
        full = render_full_shard_receipt(receipt)
        assert "Receipt ID" in full and "Shard ID" in full

    def test_no_owner_or_requester_is_fabricated(self):
        out = render_full_shard_receipt(build_shard_receipt(_hooks_entry()))
        for word in ("Owner", "Requested by", "Executed by", "Approved by"):
            assert word not in out
        d = receipt_to_dict(build_shard_receipt(_hooks_entry()))
        assert not any(k in d for k in ("owner", "requested_by", "executed_by", "approved_by", "user"))


class TestCaptureRows:
    """Depth and completeness are shown as two facts, never folded into one."""

    def _lines(self, entry: dict) -> tuple[str, str]:
        out = render_compact_shard_receipt(build_shard_receipt(entry)).splitlines()
        capture = next(ln for ln in out if ln.strip().startswith("Capture"))
        gaps = next(ln for ln in out if ln.strip().startswith("Gaps"))
        return capture, gaps

    def test_incomplete_capture_names_the_gap_and_keeps_partial_depth(self):
        entry = _hooks_entry()
        entry["capture"]["completeness"] = {
            "status": "incomplete",
            "reasons": [{"kind": "corrupt_queued_event", "count": 1, "detail": "1 queued event could not be decoded"}],
        }
        capture, gaps = self._lines(entry)
        assert "partial" in capture and "did not execute or verify" in capture
        assert "1 queued event could not be decoded" in gaps
        full = render_full_shard_receipt(build_shard_receipt(entry))
        assert "Capture depth  partial" in full
        assert "Completeness   Incomplete" in full
        assert "Known gaps     1 queued event could not be decoded" in full

    def test_healthy_capture_is_partial_depth_and_complete(self):
        entry = _hooks_entry()
        entry["capture"]["completeness"] = {"status": "complete", "reasons": []}
        capture, gaps = self._lines(entry)
        assert "partial" in capture and "did not execute or verify" in capture
        assert "None known" in gaps
        full = render_full_shard_receipt(build_shard_receipt(entry))
        assert "Completeness   Complete" in full and "Known gaps     None known" in full

    def test_legacy_record_completeness_is_unknown_not_complete(self):
        capture, gaps = self._lines(_hooks_entry())  # no stored block, no dropped counter
        assert "partial" in capture
        assert "Unknown" in gaps
        block = build_shard_receipt(_hooks_entry()).capture_completeness
        assert block["depth"] == "partial" and block["status"] == "unknown" and block["derived"] is True

    def test_native_run_is_full_depth_and_complete(self):
        entry = {"task": "native", "timestamp": "2026-09-14T10:00:00Z", "workflow": "native", "executor": "native"}
        receipt = build_shard_receipt(entry)
        assert receipt.capture_completeness["depth"] == "full"
        assert receipt.capture_completeness["status"] == "complete"
        out = render_compact_shard_receipt(receipt)
        assert "Gaps" not in out  # nothing to flag on a native run with no known loss
        d = receipt_to_dict(receipt)["capture_completeness"]
        assert d["depth"] == "full" and d["status"] == "complete"
