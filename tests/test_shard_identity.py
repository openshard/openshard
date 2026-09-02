from __future__ import annotations

import unittest

from openshard.history.shard import (
    CAPTURE_FULL,
    CAPTURE_PARTIAL,
    CAPTURE_UNKNOWN,
    ORIGIN_EXTERNAL_OBSERVED,
    ORIGIN_OPENSHARD_ROUTED,
    ORIGIN_UNKNOWN,
    Shard,
    build_shard,
    derive_shard_identity,
)
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)


def _native_entry() -> dict:
    return {
        "task": "add JWT auth",
        "timestamp": "2026-04-13T06:24:08Z",
        "workflow": "native",
        "executor": "native",
        "retry_triggered": False,
    }


def _opencode_entry() -> dict:
    return {
        "task": "refactor db layer",
        "timestamp": "2026-04-13T06:24:08Z",
        "workflow": "opencode",
        "executor": "opencode",
        "adapter": "opencode",
        "retry_triggered": False,
    }


def _routed_but_unlabeled_entry() -> dict:
    """A genuine pipeline-produced entry (has retry_triggered) whose workflow
    has no dedicated agent label (e.g. an old "direct" record)."""
    return {
        "task": "fix typo",
        "timestamp": "2026-04-13T06:24:08Z",
        "retry_triggered": False,
        "duration_seconds": 1.2,
    }


def _truly_ambiguous_entry() -> dict:
    """No positive signal OpenShard produced this record at all."""
    return {
        "task": "unknown provenance",
        "timestamp": "2026-04-13T06:24:08Z",
    }


def _claude_code_import_entry() -> dict:
    return {
        "task": "Claude Code session import",
        "timestamp": "2026-04-13T06:24:08Z",
        "executor": "claude_code_import",
        "import_source": "claude_code",
        "import_method": "openshard_import_v0",
        "verification_attempted": False,
        "verification_passed": None,
    }


def _claude_code_wrap_entry() -> dict:
    return {
        "task": "Claude Code wrap session",
        "timestamp": "2026-04-13T06:24:08Z",
        "executor": "claude_code_wrap",
        "import_source": "claude_code",
        "import_method": "openshard_wrap_v0",
        "verification_attempted": False,
        "wrap_exit_code": 0,
    }


class TestDeriveShardIdentity(unittest.TestCase):
    def test_native_is_openshard_routed_full(self):
        agent, origin, capture = derive_shard_identity(_native_entry())
        self.assertEqual(agent, "OpenShard Native")
        self.assertEqual(origin, ORIGIN_OPENSHARD_ROUTED)
        self.assertEqual(capture, CAPTURE_FULL)

    def test_opencode_is_openshard_routed_full(self):
        agent, origin, capture = derive_shard_identity(_opencode_entry())
        self.assertEqual(agent, "OpenCode")
        self.assertEqual(origin, ORIGIN_OPENSHARD_ROUTED)
        self.assertEqual(capture, CAPTURE_FULL)

    def test_routed_but_unlabeled_workflow_stays_openshard_routed(self):
        agent, origin, capture = derive_shard_identity(_routed_but_unlabeled_entry())
        self.assertEqual(agent, "OpenShard")
        self.assertEqual(origin, ORIGIN_OPENSHARD_ROUTED)
        self.assertEqual(capture, CAPTURE_FULL)

    def test_truly_ambiguous_entry_is_unknown_but_keeps_display_label(self):
        agent, origin, capture = derive_shard_identity(_truly_ambiguous_entry())
        self.assertEqual(agent, "OpenShard")
        self.assertEqual(origin, ORIGIN_UNKNOWN)
        self.assertEqual(capture, CAPTURE_UNKNOWN)

    def test_claude_code_import_is_external_observed_partial(self):
        agent, origin, capture = derive_shard_identity(_claude_code_import_entry())
        self.assertNotIn(agent, ("OpenShard", "OpenShard Native"))
        self.assertEqual(origin, ORIGIN_EXTERNAL_OBSERVED)
        self.assertEqual(capture, CAPTURE_PARTIAL)

    def test_claude_code_wrap_is_external_observed_partial(self):
        agent, origin, capture = derive_shard_identity(_claude_code_wrap_entry())
        self.assertNotIn(agent, ("OpenShard", "OpenShard Native"))
        self.assertEqual(origin, ORIGIN_EXTERNAL_OBSERVED)
        self.assertEqual(capture, CAPTURE_PARTIAL)

    def test_never_raises_on_empty_entry(self):
        agent, origin, capture = derive_shard_identity({})
        self.assertEqual(agent, "OpenShard")
        self.assertEqual(origin, ORIGIN_UNKNOWN)
        self.assertEqual(capture, CAPTURE_UNKNOWN)


class TestBuildShard(unittest.TestCase):
    def test_returns_shard_with_identity_fields(self):
        shard = build_shard(
            _claude_code_import_entry(),
            shard_id="shard-20260413-0001",
            created_at="2026-04-13T06:24:08Z",
            task_short="Claude Code session import",
            task_full="Claude Code session import",
        )
        self.assertIsInstance(shard, Shard)
        self.assertEqual(shard.shard_id, "shard-20260413-0001")
        self.assertEqual(shard.origin, ORIGIN_EXTERNAL_OBSERVED)
        self.assertEqual(shard.capture_depth, CAPTURE_PARTIAL)


class TestBuildShardReceiptHonestIdentity(unittest.TestCase):
    def test_claude_code_import_receipt_is_not_labelled_openshard(self):
        receipt = build_shard_receipt(_claude_code_import_entry())
        self.assertIsNotNone(receipt.shard)
        self.assertNotEqual(receipt.agent, "OpenShard")
        self.assertNotEqual(receipt.agent, "OpenShard Native")
        self.assertEqual(receipt.shard.origin, ORIGIN_EXTERNAL_OBSERVED)
        self.assertEqual(receipt.shard.capture_depth, CAPTURE_PARTIAL)
        self.assertEqual(receipt.agent, receipt.shard.agent)

    def test_claude_code_wrap_receipt_is_not_labelled_openshard(self):
        receipt = build_shard_receipt(_claude_code_wrap_entry())
        self.assertIsNotNone(receipt.shard)
        self.assertNotEqual(receipt.agent, "OpenShard")
        self.assertEqual(receipt.shard.origin, ORIGIN_EXTERNAL_OBSERVED)
        self.assertEqual(receipt.shard.capture_depth, CAPTURE_PARTIAL)

    def test_native_receipt_still_openshard_native(self):
        receipt = build_shard_receipt(_native_entry())
        self.assertEqual(receipt.agent, "OpenShard Native")
        self.assertEqual(receipt.shard.origin, ORIGIN_OPENSHARD_ROUTED)
        self.assertEqual(receipt.shard.capture_depth, CAPTURE_FULL)

    def test_opencode_receipt_still_opencode(self):
        receipt = build_shard_receipt(_opencode_entry())
        self.assertEqual(receipt.agent, "OpenCode")
        self.assertEqual(receipt.shard.origin, ORIGIN_OPENSHARD_ROUTED)
        self.assertEqual(receipt.shard.capture_depth, CAPTURE_FULL)


class TestRendererShowsHonestCapture(unittest.TestCase):
    def test_compact_receipt_shows_capture_line_for_external_observed(self):
        receipt = build_shard_receipt(_claude_code_import_entry())
        rendered = render_compact_shard_receipt(receipt)
        self.assertIn("Capture", rendered)
        self.assertIn("partial", rendered)
        self.assertIn("OpenShard did not execute or verify this run", rendered)

    def test_full_receipt_shows_capture_line_for_external_observed(self):
        receipt = build_shard_receipt(_claude_code_wrap_entry())
        rendered = render_full_shard_receipt(receipt)
        self.assertIn("Capture", rendered)
        self.assertIn("partial", rendered)

    def test_native_receipt_has_no_capture_line(self):
        receipt = build_shard_receipt(_native_entry())
        rendered = render_compact_shard_receipt(receipt)
        self.assertNotIn("Capture", rendered)


if __name__ == "__main__":
    unittest.main()
