"""Task edges: a declared parent task (``OPENSHARD_PARENT_TASK_ID``) and agent-reported subagents."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks_install import HOOK_SPECS, TRANSPORT_HTTP
from openshard.cli.main import cli
from openshard.history import task_edges
from openshard.history.correlation import correlation_block, launch_correlation
from openshard.history.task_identity import PARENT_TASK_ID_ENV, launch_parent_task_id
from openshard.sync.envelope import build_envelope
from tests.capture_fixtures import _lines, _make_repo

PARENT = "task_018f4d2a-1c3e-7000-8b1a-0242ac120002"
SID = "51515151-5151-4151-8151-515151515151"
SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8")
)
MALFORMED = ["", "task_nope", PARENT.upper(), f" {PARENT}", f"{PARENT}\n", "task_018f4d2a-1c3e-4000-8b1a-0242ac120002"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "factory")


def _session(repo: Path, env: dict, *, subagents: bool = True) -> dict:
    base = {"session_id": SID, "cwd": str(repo)}
    docs = [{**base, "hook_event_name": "SessionStart", "source": "startup"},
            {**base, "hook_event_name": "UserPromptSubmit", "prompt": "split the work"}]
    if subagents:
        docs += [
            {**base, "hook_event_name": "SubagentStart", "agent_id": "agent-a1", "agent_type": "Explore"},
            {**base, "hook_event_name": "SubagentStart", "agent_id": "agent-b2"},
            {**base, "hook_event_name": "SubagentStop", "agent_id": "agent-a1", "agent_type": "Explore"},
            {**base, "hook_event_name": "SubagentStart", "agent_id": "/abs/path/not-an-id"},
        ]
    docs += [{**base, "hook_event_name": "Stop"},
             {**base, "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}]
    for doc in docs:
        ch.handle_claude_hook(doc, env={"CLAUDE_PROJECT_DIR": str(repo), **env})
    [entry] = [e for e in _lines(repo) if (e.get("capture") or {}).get("session_id") == SID]
    return entry


class TestDeclaredParent:
    def test_valid_declaration_is_returned_exactly(self):
        assert launch_parent_task_id({PARENT_TASK_ID_ENV: PARENT}) == PARENT

    @pytest.mark.parametrize("value", MALFORMED)
    def test_malformed_is_no_declaration_and_never_repaired(self, value):
        assert launch_parent_task_id({PARENT_TASK_ID_ENV: value}) is None
        assert launch_correlation({PARENT_TASK_ID_ENV: value}) is None
        assert correlation_block({"parent_task_id": value}) is None

    def test_unset_is_no_declaration(self):
        assert launch_parent_task_id({}) is None and launch_correlation({}) is None

    def test_it_joins_the_declared_correlation_block(self):
        context = json.dumps({"trigger": "github.issue"})
        assert launch_correlation({PARENT_TASK_ID_ENV: PARENT, "OPENSHARD_CORRELATION_CONTEXT": context}) == {
            "evidence": "declared", "trigger": "github.issue", "parent_task_id": PARENT}


class TestCapture:
    def test_parent_and_children_land_on_the_receipt_with_their_evidence(self, repo):
        entry = _session(repo, {PARENT_TASK_ID_ENV: PARENT})
        assert entry["correlation"] == {"evidence": "declared", "parent_task_id": PARENT}
        assert entry["capture"]["subagent_edges"] == [
            {"agent_id": "agent-a1", "agent_type": "Explore", "started": True, "stopped": True,
             "evidence": "agent_reported"},
            {"agent_id": "agent-b2", "agent_type": None, "started": True, "stopped": False,
             "evidence": "agent_reported"},
        ]
        # The counts are unchanged: every reported start/stop still counts, edges or not.
        assert entry["capture"]["subagents"] == {"started": 3, "stopped": 1, "failed": 0}
        assert "task_id" not in entry or entry["task_id"] != PARENT  # the parent is never this Receipt's task

    def test_no_declaration_and_no_subagents_keep_the_record_shape(self, repo):
        entry = _session(repo, {}, subagents=False)
        assert "correlation" not in entry
        assert "subagent_edges" not in entry["capture"] and "subagents" not in entry["capture"]
        assert task_edges.task_edges_block(entry) is None

    def test_a_rebuilt_buffer_keeps_the_edges(self, repo):
        entry = _session(repo, {})
        counts = task_edges.restore_subagents(entry["capture"])
        assert [e["agent_id"] for e in counts["edges"]] == ["agent-a1", "agent-b2"]
        assert (counts["started"], counts["stopped"]) == (3, 1)

    def test_edges_are_capped(self):
        counts: dict = {}
        for i in range(task_edges.MAX_SUBAGENT_EDGES + 5):
            task_edges.note_subagent(counts, {"child_subagent_id": f"agent-{i}"}, started=True)
        assert len(counts["edges"]) == task_edges.MAX_SUBAGENT_EDGES

    def test_only_subagent_events_carry_subagent_attrs(self):
        doc = {"agent_id": "agent-a1", "agent_type": "Explore"}
        assert task_edges.claude_subagent_attrs("Stop", doc) == {}
        assert task_edges.claude_subagent_attrs("SubagentStop", doc) == {
            "child_subagent_id": "agent-a1", "child_role": "Explore"}
        assert task_edges.claude_subagent_attrs("SubagentStart", {"agent_id": "x" * 81}) == {}

    def test_claude_installs_the_subagent_hooks_over_http(self):
        specs = {s.event: s for s in HOOK_SPECS}
        assert specs["SubagentStart"].transport == specs["SubagentStop"].transport == TRANSPORT_HTTP


class TestSurfaces:
    def test_last_json_and_full_show_the_edges_with_their_labels(self, repo, monkeypatch):
        _session(repo, {PARENT_TASK_ID_ENV: PARENT})
        monkeypatch.chdir(repo)
        doc = json.loads(CliRunner().invoke(cli, ["last", "--json"], catch_exceptions=False).output)
        assert doc["task_edges"]["parent_task_id"] == PARENT
        assert doc["task_edges"]["parent_evidence"] == "declared"
        assert {c["evidence"] for c in doc["task_edges"]["children"]} == {"agent_reported"}
        text = CliRunner().invoke(cli, ["last", "--full"], catch_exceptions=False).output
        assert f"Parent task {PARENT} (declared)" in " ".join(text.split())
        assert "agent-a1 Explore · stopped (agent-reported)" in text
        assert "verified" not in "\n".join(ln for ln in text.splitlines() if "Subagent" in ln)

    def test_sync_keeps_the_parent_local_and_validates_strictly(self, repo):
        entry = _session(repo, {PARENT_TASK_ID_ENV: PARENT, "OPENSHARD_CORRELATION_CONTEXT": '{"trigger": "ci"}'})
        doc = build_envelope(entry, 0, core_version="9.9.9")
        assert doc["receipt"]["correlation"] == {"evidence": "declared", "trigger": "ci"}
        assert "subagent_edges" not in json.dumps(doc)
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))
        assert not errors, [e.message for e in errors[:3]]

    def test_a_parent_alone_sends_no_correlation_block(self, repo):
        entry = _session(repo, {PARENT_TASK_ID_ENV: PARENT})
        assert "correlation" not in build_envelope(entry, 0, core_version="9.9.9")["receipt"]
