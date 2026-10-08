"""Claude Code's permission mode is captured as observed evidence, never as OpenShard control.

Every Claude Code hook payload carries ``permission_mode`` (default |
acceptEdits | plan | bypassPermissions | dontAsk): the regime the agent ran
under, by its own report. The Receipt records it under
``runtime_configuration`` as ``agent_reported`` and the compact Receipt
shows it on a ``Permissions`` row that says OpenShard did not enforce it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import (
    PERMISSION_MODES,
    ReducedHookPayload,
    extract_hook_payload,
    handle_claude_hook,
    reduce_hook_payload,
)
from openshard.cli.main import cli
from openshard.history.receipt_evidence import runtime_configuration_block
from openshard.history.shard_contract import build_shard_receipt, permission_mode_label
from openshard.sync.envelope import receipt_payload
from tests.capture_fixtures import _lines
from tests.test_task_context_capture import SID1, _claude_docs


def _drive(repo: Path, docs: list[dict]) -> dict:
    for doc in docs:
        outcome = handle_claude_hook(doc, env={"CLAUDE_PROJECT_DIR": str(repo)})
        assert outcome.action != "error", outcome.detail
    return _lines(repo)[0]


class TestCapture:
    def test_mode_is_recorded_as_the_agents_own_report(self, repo: Path):
        docs = _claude_docs(repo, SID1)
        for doc in docs:
            doc["permission_mode"] = "acceptEdits"
        entry = _drive(repo, docs)
        capture = entry["capture"]
        assert capture["permission_mode"] == "acceptEdits"
        assert capture["permission_mode_source"] == "claude_hook"
        assert capture["permission_modes_seen"] == ["acceptEdits"]
        block = runtime_configuration_block(entry)
        assert block == {
            "permission_mode": "acceptEdits", "permission_modes_seen": ["acceptEdits"],
            "source": "claude_hook", "evidence": "agent_reported",
        }
        # Hosted projection: the Platform's strict contract has no permission
        # keys yet and requires effort, so a mode-only block is not sent.
        assert "runtime_configuration" not in receipt_payload(entry, 1)
        # No enforced-permission evidence is invented for an external agent.
        assert "permission_evidence" not in entry

    def test_a_mode_change_keeps_every_mode_in_order(self, repo: Path):
        docs = _claude_docs(repo, SID1)
        docs[0]["permission_mode"] = "default"
        docs[1]["permission_mode"] = "default"
        docs[2]["permission_mode"] = "acceptEdits"
        docs[3]["permission_mode"] = "acceptEdits"
        entry = _drive(repo, docs)
        assert entry["capture"]["permission_mode"] == "acceptEdits"
        assert entry["capture"]["permission_modes_seen"] == ["default", "acceptEdits"]

    @pytest.mark.parametrize("mode", ["yolo", "", 42, None, {"mode": "acceptEdits"}, "acceptedits"])
    def test_an_undocumented_value_is_never_recorded(self, repo: Path, mode):
        docs = _claude_docs(repo, SID1)
        for doc in docs:
            doc["permission_mode"] = mode
        entry = _drive(repo, docs)
        assert "permission_mode" not in entry["capture"]
        assert runtime_configuration_block(entry) is None
        assert "runtime_configuration" not in receipt_payload(entry, 1)

    def test_a_payload_without_the_field_records_nothing(self, repo: Path):
        docs = _claude_docs(repo, SID1)
        for doc in docs:
            doc.pop("permission_mode", None)
        entry = _drive(repo, docs)
        assert "permission_mode" not in entry["capture"]

    def test_queue_round_trip_preserves_the_mode(self, repo: Path):
        doc = {"hook_event_name": "Stop", "session_id": SID1, "cwd": str(repo), "permission_mode": "plan"}
        payload = extract_hook_payload(doc)
        assert payload is not None and payload.permission_mode == "plan"
        reduced = reduce_hook_payload(payload, repo)
        assert reduced is not None
        restored = ReducedHookPayload.from_dict(reduced.to_dict())
        assert restored is not None and restored.permission_mode == "plan"
        assert extract_hook_payload({**doc, "permission_mode": "nope"}).permission_mode is None

    def test_documented_modes_are_the_closed_set(self):
        assert PERMISSION_MODES == {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"}


class TestReceipt:
    def test_the_receipt_names_the_mode_and_that_openshard_did_not_enforce_it(self, repo: Path):
        docs = _claude_docs(repo, SID1)
        docs[0]["permission_mode"] = "default"
        for doc in docs[1:]:
            doc["permission_mode"] = "bypassPermissions"
        _drive(repo, docs)
        from unittest.mock import patch

        with patch.object(Path, "cwd", return_value=repo):
            result = CliRunner().invoke(cli, ["last"])
        assert result.exit_code == 0, result.output
        assert (
            "Permissions default → bypassPermissions (Claude Code's own permission mode, agent-reported; "
            "not enforced by OpenShard)"
        ) in result.output or (
            "Permissions default -> bypassPermissions (Claude Code's own permission mode, agent-reported; "
            "not enforced by OpenShard)"
        ) in result.output

    def test_no_row_without_the_evidence(self, repo: Path):
        docs = _claude_docs(repo, SID1)
        for doc in docs:
            doc.pop("permission_mode", None)
        entry = _drive(repo, docs)
        receipt = build_shard_receipt(entry, index=0)
        assert permission_mode_label(receipt) is None
        assert "Permissions" not in str(build_shard_receipt(entry, index=0).recorded_evidence)
