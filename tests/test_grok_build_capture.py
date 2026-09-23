"""Tests for Grok Build capture (unreleased): translator, installer, service path, CLI.

Every test drives the adapter with synthetic Grok Build hook documents in a
throw-away git repository. No real Grok Build is ever run; where the
installer/setup code looks for one, ``shutil.which`` is patched. Documents
follow the camelCase vocabulary the Grok Build hooks reference documents
(``hookEventName``, ``sessionId``, ``cwd``, ``workspaceRoot``, ``toolName``,
``toolInput``); fields the reference does not document (``prompt``,
``source``, ``reason``) are exercised only as tolerated, optional extras.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import grok_build_hooks as gb
from openshard.adapters.capture_agents import profile_for
from openshard.adapters.claude_hooks import handle_claude_hook, handle_hook, reduce_hook_payload
from openshard.adapters.grok_build_hooks_install import (
    HOOK_COMMAND,
    HOOK_EVENTS,
    HOOKS_RELPATH,
    build_hook_config,
    install_grok_build_hooks,
    installed_grok_build_events,
    uninstall_grok_build_hooks,
)
from openshard.cli.main import cli
from openshard.history.event import SOURCE_GROK_BUILD_HOOKS, events_from_entry
from openshard.history.query import get_receipt, list_shards
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
)

SID = "0b6c1f7e-2a4d-4c8e-9f10-a1b2c3d4e5f6"
SECRET = "sk-proj-SECRETSECRET12345678901234567890abcdef"
FILE_BODY = "def add(a, b):\n    return a + b  # RAW FILE CONTENT"
LABEL = "Grok Build (external)"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "grok repo")


def _doc(repo: Path, event: str | None = None, sid: str = SID, **fields) -> dict:
    """A Grok Build hook document with the base fields every event carries."""
    base: dict = {"sessionId": sid, "cwd": str(repo), "workspaceRoot": str(repo)}
    if event:
        base["hookEventName"] = event
    base.update(fields)
    return base


def _tool(repo: Path, event: str, name: str, tool_input: dict, sid: str = SID, **fields) -> dict:
    return _doc(repo, event, sid, toolName=name, toolInput=tool_input, **fields)


def _run(repo: Path, event: str, doc: dict | None = None):
    return handle_hook(doc if doc is not None else _doc(repo, event), env={}, agent="grok_build",
                       event_override=event)


def _drive_inline(repo: Path, sid: str = SID) -> None:
    _run(repo, "SessionStart", _doc(repo, "SessionStart", sid, source="startup"))
    _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", sid,
                                        prompt=f"add a calc module. key={SECRET}"))
    _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "read", {"filePath": str(repo / "README.md")}, sid))
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "write",
                                    {"filePath": str(repo / "calc.py"), "content": FILE_BODY + SECRET}, sid))
    _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "run_terminal_command",
                                    {"command": "python -m pytest -q"}, sid))
    _run(repo, "PostToolUseFailure", _tool(repo, "PostToolUseFailure", "run_terminal_command",
                                           {"command": "git push"}, sid, error=f"denied {SECRET}"))
    _run(repo, "PermissionDenied", _tool(repo, "PermissionDenied", "run_terminal_command",
                                         {"command": f"rm -rf / {SECRET}"}, sid))
    _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "mcp__github__create_issue", {"title": SECRET}, sid))
    _run(repo, "Stop", _doc(repo, "Stop", sid, reason="end_turn"))
    _run(repo, "SessionEnd", _doc(repo, "SessionEnd", sid, reason="user_exit"))


def _lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


class TestTranslator:
    def test_event_mapping_and_identity(self, repo):
        expected = {
            "SessionStart": "SessionStart", "UserPromptSubmit": "UserPromptSubmit",
            "PostToolUse": "PostToolUse", "PostToolUseFailure": "PostToolUseFailure",
            "PermissionDenied": "PermissionDenied", "Stop": "Stop", "StopFailure": "SessionIdle",
            "SessionEnd": "SessionEnd",
        }
        assert gb.GROK_BUILD_EVENT_MAP == expected
        for grok_event, neutral in expected.items():
            p = gb.extract_grok_build_payload(_doc(repo), event_override=grok_event)
            assert p.event == neutral and p.agent == "grok_build", grok_event
            assert p.session_id == SID and p.cwd == str(repo)
            assert p.model_id is None and p.provider_id is None and p.tool_success is None

    def test_event_comes_from_the_command_line_then_the_document(self, repo):
        assert gb.extract_grok_build_payload(_doc(repo, "Stop")).event == "Stop"
        assert gb.extract_grok_build_payload(_doc(repo)) is None
        # The installed --event wins over whatever the document claims.
        assert gb.extract_grok_build_payload(_doc(repo, "Stop"), event_override="SessionStart").event \
            == "SessionStart"

    def test_unsubscribed_or_unknown_events_are_ignored(self, repo):
        for ev in ("PreToolUse", "SubagentStart", "SubagentStop", "Notification", "PreCompact", "PostCompact",
                   "TaskCreated", "InstructionsLoaded", "session_start", "Nope"):
            assert gb.extract_grok_build_payload(_doc(repo, ev)) is None, ev

    def test_claude_vocabulary_is_never_read_as_grok(self, repo):
        claude_doc = {"session_id": SID, "cwd": str(repo), "hook_event_name": "PostToolUse",
                      "tool_name": "Bash", "tool_input": {"command": "ls"}}
        assert gb.extract_grok_build_payload(claude_doc) is None
        p = gb.extract_grok_build_payload(claude_doc, event_override="PostToolUse")
        assert p.session_id is None and p.tool_name is None and p.command is None  # nothing borrowed

    def test_session_id_is_validated(self, repo):
        for bad in ("../../etc/passwd", 42, None, "", "a b"):
            p = gb.extract_grok_build_payload(_doc(repo, sid=bad), event_override="Stop")
            assert p.session_id is None, bad

    def test_agent_identity_is_never_read_from_the_payload(self, repo):
        doc = _doc(repo, "Stop", agent="claude_code", executor="native", agentName="Cursor", model="gpt-x")
        p = gb.extract_grok_build_payload(doc)
        assert p.agent == "grok_build" and p.model_id is None
        handle_hook(_doc(repo, "UserPromptSubmit", agent="claude_code", prompt="task"), env={},
                    agent="grok_build", event_override="UserPromptSubmit")
        entry = _lines(repo)[0]
        assert entry["executor"] == "grok_build_hooks" and entry["capture"]["agent"] == "grok_build"

    def test_tools_classify_and_read_only_the_path_or_command(self, repo):
        def post(name, tool_input, event="PostToolUse"):
            return gb.extract_grok_build_payload(_tool(repo, event, name, tool_input), event_override=event)

        for name in ("run_terminal_command", "Bash", "bash"):
            cmd = post(name, {"command": "npm test", "cwd": "/x", "timeout": 5})
            assert cmd.tool_kind == "command" and cmd.command == "npm test" and cmd.file_path is None, name
        write = post("write", {"filePath": "a.py", "content": FILE_BODY})
        assert write.tool_kind == "file" and write.file_paths == [("a.py", "create")] and write.tool_success is None
        for name in ("edit", "Edit", "multiedit"):
            p = post(name, {"file_path": "b.py", "path": "c.py", "old_string": SECRET})
            assert p.tool_kind == "file" and p.file_paths == [("b.py", "update")], name
        read = post("read", {"path": "/r/x.py", "offset": 3})
        assert read.tool_kind == "read" and read.file_path == "/r/x.py" and read.command is None
        for name in ("grep", "web_fetch", "web_search", "mcp__github__create_issue", "SomethingNew"):
            p = post(name, {"command": "rm -rf /", "filePath": "/etc/passwd", "query": SECRET})
            assert p.tool_kind == "other" and p.command is None and p.file_path is None, name
        failed = post("run_terminal_command", {"command": "false"}, event="PostToolUseFailure")
        assert failed.event == "PostToolUseFailure" and failed.command == "false"

    def test_permission_denied_names_only_the_tool(self, repo):
        p = gb.extract_grok_build_payload(
            _tool(repo, "PermissionDenied", "run_terminal_command", {"command": f"curl {SECRET}"}),
            event_override="PermissionDenied",
        )
        assert p.event == "PermissionDenied" and p.tool_name == "run_terminal_command"
        assert p.command is None and p.file_path is None and p.tool_kind is None
        blob = json.dumps(reduce_hook_payload(p, repo).to_dict())
        assert SECRET not in blob

    def test_malformed_shapes_under_report(self, repo):
        for tool_input in (None, "ls", [], {"command": ["ls"]}, {"command": 7}, {"filePath": 7}):
            for name in ("run_terminal_command", "write", "read"):
                p = gb.extract_grok_build_payload(
                    _doc(repo, "PostToolUse", toolName=name, toolInput=tool_input), event_override="PostToolUse")
                assert p is not None and p.command is None and p.file_path is None, (name, tool_input)
        p = gb.extract_grok_build_payload(_doc(repo, "PostToolUse", toolName=42), event_override="PostToolUse")
        assert p.tool_name is None

    def test_prompt_is_tolerated_only_when_it_is_a_string(self, repo):
        assert gb.extract_grok_build_payload(_doc(repo, prompt="do x"), event_override="UserPromptSubmit").prompt \
            == "do x"
        for bad in (None, 5, ["x"], {"a": 1}, ""):
            p = gb.extract_grok_build_payload(_doc(repo, prompt=bad), event_override="UserPromptSubmit")
            assert p.prompt is None, bad
        # A prompt on any other event is never read.
        assert gb.extract_grok_build_payload(_doc(repo, prompt="x"), event_override="Stop").prompt is None

    def test_never_reads_results_transcripts_or_contents(self, repo):
        for event, doc in (
            ("UserPromptSubmit", _doc(repo, prompt="hi", transcriptPath=f"/x/{SECRET}")),
            ("PostToolUse", _tool(repo, "PostToolUse", "write", {"filePath": str(repo / "a.py"),
                                                                 "content": FILE_BODY + SECRET},
                                  toolResponse=f"out {SECRET}", toolOutput=SECRET)),
            ("PostToolUseFailure", _tool(repo, "PostToolUseFailure", "run_terminal_command",
                                         {"command": "ls"}, error=f"boom {SECRET}")),
            ("Stop", _doc(repo, "Stop", lastAssistantMessage=SECRET)),
        ):
            p = gb.extract_grok_build_payload(doc, event_override=event)
            blob = json.dumps(reduce_hook_payload(p, repo).to_dict())
            assert SECRET not in blob and "RAW FILE CONTENT" not in blob, event


# ---------------------------------------------------------------------------
# Payload -> canonical Events / record / receipt (inline path)
# ---------------------------------------------------------------------------


class TestCanonicalRecord:
    def test_session_becomes_one_grok_build_shard(self, repo):
        _drive_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "grok_build_hooks" and entry["import_source"] == "grok_build"
        cap = entry["capture"]
        assert cap["source"] == "grok_build_hooks" and cap["agent"] == "grok_build"
        assert cap["agent_vendor"] == "xAI" and cap["provider"] is None
        assert cap["session_id"] == SID
        assert cap["session_end_observed"] is True and cap["session_end_reason"] == "user_exit"
        assert cap["prompt_count"] == 1 and cap["tool_call_count"] == 5 and cap["tool_failure_count"] == 1
        assert cap["turn_count"] == 1 and cap["permission_denied_count"] == 1
        assert cap["task_status"] == "turn_completed"
        assert entry["task"].startswith("add a calc module") and SECRET not in entry["task"]
        # Nothing in Grok's documented hook payload names a model, usage or cost.
        assert entry["execution_model"] == "unknown" and cap["model_source"] == "not_captured"
        assert cap["models_seen"] == []
        for key in ("estimated_cost", "cost_provenance", "prompt_tokens", "tokens_provenance"):
            assert key not in entry
        raw = json.dumps(entry)
        assert SECRET not in raw and "RAW FILE" not in raw

    def test_task_is_a_placeholder_when_no_prompt_is_delivered(self, repo):
        _run(repo, "SessionStart")
        _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "run_terminal_command", {"command": "ls"}))
        _run(repo, "Stop")
        entry = _lines(repo)[0]
        assert entry["task"] == "Grok Build session (task not captured)"
        assert entry["capture"]["task_source"] == "not_captured"

    def test_events_carry_grok_identity_and_evidence(self, repo):
        _drive_inline(repo)
        events = events_from_entry(_lines(repo)[0])
        assert events and all(e.source == SOURCE_GROK_BUILD_HOOKS for e in events)
        assert all(e.actor == "grok_build" for e in events)
        types = [e.event_type for e in events]
        for wanted in ("session.started", "tool.invoked", "file.changed", "approval.denied", "run.completed"):
            assert wanted in types, wanted
        tools = {e.metadata.get("tool"): e for e in events if e.event_type == "tool.invoked"}
        read = tools["read"]
        assert read.target == "README.md" and read.metadata.get("access") == "read" and read.status == "unknown"
        # Grok does not document PostToolUse as success-only: a write is never "passed".
        write = tools["write"]
        assert write.target == "calc.py" and write.status == "unknown" and write.evidence == "agent_reported"
        assert tools["mcp__github__create_issue"].target is None
        failed = next(e for e in events if e.event_type == "tool.invoked" and e.status == "failed")
        assert failed.target == "git" and failed.metadata["hook"] == "PostToolUseFailure"
        assert not [e for e in events if e.event_type.startswith("verification.")]
        started = next(e for e in events if e.event_type == "session.started")
        assert "Grok Build session observed" in started.action

    def test_permission_denied_is_reported_not_decided(self, repo):
        _drive_inline(repo)
        events = events_from_entry(_lines(repo)[0])
        denied = [e for e in events if e.event_type == "approval.denied"]
        assert len(denied) == 1
        d = denied[0]
        assert d.action == "permission denied: run_terminal_command" and d.status == "failed"
        assert d.evidence == "agent_reported" and d.target is None
        assert "rm -rf" not in json.dumps(_lines(repo)[0])  # the denied command text is never kept
        # OpenShard did not make a policy decision, so the receipt claims no approval fact.
        receipt = build_shard_receipt(_lines(repo)[0])
        assert receipt.approval == "Not recorded" and receipt.approval_required is False

    def test_permission_denied_alone_neither_opens_a_record_nor_counts_as_work(self, repo):
        outcome = _run(repo, "PermissionDenied", _tool(repo, "PermissionDenied", "run_terminal_command", {}))
        assert outcome.action == "buffered" and _lines(repo) == []
        _run(repo, "Stop")
        _run(repo, "SessionEnd")
        assert _lines(repo) == []

    def test_written_file_is_git_evidence_not_hook_evidence(self, repo):
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="add calc"))
        (repo / "calc.py").write_text("x = 1\n", encoding="utf-8")
        _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "write", {"filePath": str(repo / "calc.py")}))
        _run(repo, "Stop")
        entry = _lines(repo)[0]
        calc = next(f for f in entry["files_detail"] if f["path"] == "calc.py")
        assert calc["attribution"] == "git_observed"  # never upgraded to agent_reported

    def test_command_check_is_observed_but_never_passed(self, repo):
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="test it"))
        _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "run_terminal_command", {"command": "npm test"}))
        _run(repo, "Stop")
        entry = _lines(repo)[0]
        assert entry["verification_attempted"] is True and entry["verification_passed"] is None
        block = entry["verification"]
        assert block["status"] == "unknown" and block["source"] == "directly_observed"
        assert "outcome_not_observed" in block["incomplete_reasons"]

    def test_failed_check_rests_on_the_agents_own_failure_event(self, repo):
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="test it"))
        _run(repo, "PostToolUseFailure", _tool(repo, "PostToolUseFailure", "run_terminal_command",
                                               {"command": "npm test"}, error=SECRET))
        _run(repo, "Stop")
        entry = _lines(repo)[0]
        assert entry["verification"]["status"] == "failed"
        assert entry["verification"]["source"] == "agent_reported"
        assert SECRET not in json.dumps(entry)

    def test_read_outside_the_repository_is_dropped(self, repo, tmp_path):
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="x"))
        _run(repo, "PostToolUse", _tool(repo, "PostToolUse", "read", {"path": str(tmp_path / "secret.txt")}))
        _run(repo, "Stop")
        ev = next(e for e in _lines(repo)[0]["events"] if e["event_type"] == "tool.invoked")
        assert ev["target"] is None and ev["metadata"]["path_dropped"] == "outside repository"
        assert str(tmp_path) not in json.dumps(_lines(repo)[0])

    def test_stop_failure_is_never_a_completed_turn(self, repo):
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="x"))
        _run(repo, "StopFailure", _doc(repo, "StopFailure", error="rate limited"))
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["idle_count"] == 1
        assert entry["capture"]["task_status"] == "in_progress"
        _run(repo, "Stop")
        assert _lines(repo)[0]["capture"]["turn_count"] == 1

    def test_session_end_is_observed_and_finalises(self, repo):
        _drive_inline(repo)
        events = events_from_entry(_lines(repo)[0])
        done = next(e for e in events if e.event_type == "run.completed")
        assert "Grok Build session ended (reason=user_exit)" in done.action

    def test_receipt_identity(self, repo):
        _drive_inline(repo)
        receipt = build_shard_receipt(_lines(repo)[0])
        assert receipt.agent == LABEL
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED
        assert receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.tokens_input is None and receipt.cost_provenance is None
        text = render_compact_shard_receipt(receipt)
        assert LABEL in text and "did not execute or verify" in text and SECRET not in text
        shards = list_shards(repo_path=repo)
        assert len(shards) == 1 and shards[0].agent == LABEL
        assert get_receipt(shards[0].shard_id, repo_path=repo).agent == LABEL

    def test_hook_config_is_never_a_changed_file_but_other_grok_files_are(self, repo):
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="x"))
        install_grok_build_hooks(repo_root=repo)
        (repo / ".grok" / "skills").mkdir(parents=True)
        (repo / ".grok" / "skills" / "style.md").write_text("be terse\n", encoding="utf-8")
        _run(repo, "Stop")
        paths = {f["path"] for f in _lines(repo)[0]["files_detail"]}
        assert ".grok/hooks/openshard.json" not in paths and ".grok/skills/style.md" in paths

    def test_same_session_id_as_another_agent_is_a_separate_shard(self, repo):
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        claude_doc = {"session_id": SID, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "c"}
        handle_claude_hook(claude_doc, env=env)
        handle_claude_hook({**claude_doc, "hook_event_name": "Stop"}, env=env)
        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="g"))
        _run(repo, "Stop")
        lines = _lines(repo)
        assert {e["executor"] for e in lines} == {"claude_code_hooks", "grok_build_hooks"}
        assert len({e["shard_id"] for e in lines}) == 2

    def test_grok_documents_are_not_recorded_by_the_claude_receiver(self, repo):
        # Grok also loads Claude compat hooks: a Grok-vocabulary document that
        # reaches the *Claude* translator must never become a Claude Code record.
        outcome = handle_claude_hook(_doc(repo, "UserPromptSubmit", prompt="x"), env={"CLAUDE_PROJECT_DIR": str(repo)})
        assert outcome.action == "ignored" and _lines(repo) == []

    def test_malformed_documents_are_ignored_not_errors(self, repo):
        for doc in ({}, {"sessionId": SID}, {"cwd": str(repo)}, {"sessionId": "../x", "cwd": str(repo)}):
            outcome = _run(repo, "UserPromptSubmit", doc)
            assert outcome.action == "ignored", doc
        assert _lines(repo) == []

    def test_export_compatible_record(self, repo):
        from openshard.history.shard_schema import coerce_shard_entry

        _drive_inline(repo)
        entry = _lines(repo)[0]
        assert coerce_shard_entry(json.loads(json.dumps(entry))) == entry
        assert isinstance(entry.get("receipt_id"), str) and entry["receipt_id"].startswith("rcpt_")
        assert profile_for("grok_build").label == "Grok Build"

    def test_permission_count_survives_a_rebuilt_buffer(self, repo):
        from openshard.adapters.claude_hooks import buffer_path

        _run(repo, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="x"))
        _run(repo, "PermissionDenied", _tool(repo, "PermissionDenied", "write", {}))
        _run(repo, "Stop")
        buffer_path(repo, SID, "grok_build").unlink()  # force a rebuild from runs.jsonl
        _run(repo, "PermissionDenied", _tool(repo, "PermissionDenied", "write", {}))
        _run(repo, "Stop")
        assert _lines(repo)[0]["capture"]["permission_denied_count"] == 2


# ---------------------------------------------------------------------------
# Sync / telemetry compatibility
# ---------------------------------------------------------------------------


class TestDownstream:
    def test_sync_envelope_carries_the_agent(self, repo):
        from openshard.sync import envelope

        _drive_inline(repo)
        env = envelope.build_envelope(_lines(repo)[0], 0, core_version="0.0.0-test")
        assert env["receipt"]["agent"] == LABEL
        blob = json.dumps(env)
        assert SECRET not in blob and "RAW FILE" not in blob
        assert envelope.eligibility(_lines(repo)[0]).eligible is True  # SessionEnd was observed

    def test_telemetry_maps_the_agent_to_its_own_enum(self, repo):
        from openshard.telemetry import events, schema

        _drive_inline(repo)
        props = events.receipt_properties(_lines(repo)[0])
        assert props["agent"] == "grok_build"
        for key, value in props.items():
            schema.EVENT_TYPES["receipt.created"][key](value)  # raises on a value outside the contract


# ---------------------------------------------------------------------------
# The stdout reply
# ---------------------------------------------------------------------------


class TestReply:
    def test_reply_is_always_the_empty_object_never_a_decision(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        for event in ("SessionStart", "Stop", "PostToolUse", "PreToolUse", "Weird"):
            label, reply = client.run_grok_build_hook(
                io.BytesIO(json.dumps(_doc(repo, event)).encode()), env=dict(os.environ),
                event_override=event, spawn=False)
            assert reply == "{}", event
        _label, reply = client.run_grok_build_hook(io.BytesIO(b"{ nope"), env=dict(os.environ), spawn=False)
        assert reply == "{}"
        with patch("openshard.adapters.claude_capture_client._inline_hook", side_effect=RuntimeError("boom")):
            label, reply = client.run_grok_build_hook(
                io.BytesIO(json.dumps(_doc(repo)).encode()), env=dict(os.environ), event_override="Stop",
                spawn=False)
        assert reply == "{}" and label == "error"


# ---------------------------------------------------------------------------
# Service path: POST /hooks/grok-build (authenticated, queued, folded behind)
# ---------------------------------------------------------------------------


class _Service:
    def __init__(self, env: dict) -> None:
        self.env = env
        self.ready = threading.Event()
        self.box: list = []
        self.thread = threading.Thread(
            target=svc.serve, kwargs={"port": 0, "idle_timeout": 0.0, "env": env,
                                      "ready": self.ready, "server_box": self.box}, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    @property
    def server(self):
        return self.box[0]

    @property
    def port(self) -> int:
        return self.server.port

    def stop(self) -> None:
        if self.box:
            self.box[0].begin_shutdown("test")
        self.thread.join(60)


@pytest.fixture
def capture_env(monkeypatch) -> dict:
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    return dict(os.environ)


@pytest.fixture
def service(capture_env):
    running = _Service(capture_env)
    yield running
    running.stop()


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _post(port: int, event: str, doc: dict) -> bool:
    return client.post_hook(port, json.dumps(doc).encode("utf-8"), event_override=event,
                            hook_path=client.GROK_BUILD_HOOK_PATH)


def _stable(entry: dict) -> dict:
    keys = ("event_type", "action", "status", "evidence", "target", "actor", "source")
    volatile = {"started_at", "last_activity_at", "first_prompt_at", "last_turn_completed_at",
                "last_status_ping_at", "last_idle_at", "applied_event_ids"}
    return {
        "task": entry["task"], "executor": entry["executor"], "execution_model": entry["execution_model"],
        "files_detail": entry["files_detail"], "summary": entry["summary"],
        "capture": {k: v for k, v in entry["capture"].items() if k not in volatile},
        "events": [{k: e.get(k) for k in keys} for e in entry["events"]],
    }


class TestServicePath:
    def test_http_session_matches_inline_record(self, service, tmp_path):
        via_http = _make_repo(tmp_path / "http")
        via_inline = _make_repo(tmp_path / "inline")

        def steps(r: Path):
            return [
                ("SessionStart", _doc(r, "SessionStart", source="startup")),
                ("UserPromptSubmit", _doc(r, "UserPromptSubmit", prompt=f"add calc {SECRET}")),
                ("PostToolUse", _tool(r, "PostToolUse", "write", {"filePath": str(r / "calc.py"),
                                                                 "content": FILE_BODY + SECRET})),
                ("PostToolUse", _tool(r, "PostToolUse", "run_terminal_command", {"command": "pytest -q"})),
                ("PostToolUseFailure", _tool(r, "PostToolUseFailure", "run_terminal_command",
                                             {"command": "git push"})),
                ("PermissionDenied", _tool(r, "PermissionDenied", "run_terminal_command", {"command": "rm x"})),
                ("Stop", _doc(r, "Stop")),
                ("SessionEnd", _doc(r, "SessionEnd", reason="user_exit")),
            ]

        for i, (event, doc) in enumerate(steps(via_http)):
            if i == 2:
                (via_http / "calc.py").write_text("x = 1\n", encoding="utf-8")
            assert _post(service.port, event, doc), event
        for i, (event, doc) in enumerate(steps(via_inline)):
            if i == 2:
                (via_inline / "calc.py").write_text("x = 1\n", encoding="utf-8")
            _run(via_inline, event, doc)
        assert _wait_for(lambda: bool(_lines(via_http)) and _lines(via_http)[0]["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        assert _stable(_lines(via_http)[0]) == _stable(_lines(via_inline)[0])
        raw = (via_http / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert SECRET not in raw and "RAW FILE" not in raw

    def test_queue_line_is_reduced_and_agent_tagged(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, "PostToolUse",
                     _tool(repo, "PostToolUse", "write", {"filePath": str(repo / "calc.py"),
                                                          "content": FILE_BODY + SECRET}))
        queue_file = repo / ".openshard" / "claude_sessions" / f"grok_build.{SID}{svc.QUEUE_SUFFIX}"
        line = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
        assert line["kind"] == "hook" and line["data"]["agent"] == "grok_build"
        assert line["data"]["event"] == "PostToolUse" and line["data"]["file_target"] == "calc.py"
        assert line["data"]["model_id"] is None and line["data"]["tool_success"] is None
        text = queue_file.read_text(encoding="utf-8")
        assert SECRET not in text and "RAW FILE" not in text
        service.server.recorder.resume_processing()

    def test_session_start_anchors_the_baseline_when_received(self, service, repo):
        (repo / "dirty.py").write_text("pre-existing\n", encoding="utf-8")
        service.server.recorder.pause_processing()
        assert _post(service.port, "SessionStart", _doc(repo, "SessionStart"))
        assert _post(service.port, "UserPromptSubmit", _doc(repo, "UserPromptSubmit", prompt="x"))
        (repo / "made.py").write_text("x = 1\n", encoding="utf-8")
        assert _post(service.port, "Stop", _doc(repo, "Stop"))
        service.server.recorder.resume_processing()
        assert _wait_for(lambda: bool(_lines(repo)) and _lines(repo)[0]["capture"]["turn_count"] == 1)
        entry = _lines(repo)[0]
        made = next(f for f in entry["files_detail"] if f["path"] == "made.py")
        assert made["attribution"] == "git_observed"
        dirty = next(f for f in entry["files_detail"] if f["path"] == "dirty.py")
        assert dirty["attribution"] == "pre_existing"

    def test_unauthenticated_post_is_refused(self, service, repo):
        path = client.GROK_BUILD_HOOK_PATH + "?event=UserPromptSubmit"
        body = json.dumps(_doc(repo, prompt="x")).encode()
        status, _ = client._request("POST", service.port, path, body, {"Content-Type": "application/json"})
        assert status == 401
        # A capability minted for another agent does not authorise Grok Build events.
        from openshard.adapters.capture_auth import TOKEN_HEADER, repo_capability

        token = client._auth_headers(None, None)[TOKEN_HEADER]
        for other in ("claude_code", "antigravity"):
            headers = {"Content-Type": "application/json", TOKEN_HEADER: repo_capability(token, repo, other)}
            status, _ = client._request("POST", service.port, path, body, headers)
            assert status == 401, other
        assert _lines(repo) == [] and client.health(service.port)["stats"]["queued"] == 0

    def test_unsubscribed_event_is_ignored_not_errored(self, service, repo):
        assert _post(service.port, "PreToolUse", _tool(repo, "PreToolUse", "run_terminal_command", {"command": "ls"}))
        assert _post(service.port, "SubagentStart", _doc(repo, "SubagentStart"))
        assert client.health(service.port)["stats"]["queued"] == 0

    def test_hook_command_forwards_and_never_imports_fold_code(self, service, repo, capture_env):
        code = (
            "import sys, os; from openshard.adapters.claude_capture_client import run_grok_build_hook; "
            "label, reply = run_grok_build_hook(sys.stdin, env=dict(os.environ), event_override='UserPromptSubmit'); "
            "print(label); print(reply); print(sorted(m for m in sys.modules if m.startswith('openshard')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc(repo, prompt="do it")),
            capture_output=True, text=True, timeout=60, env=capture_env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == "forwarded" and out[1] == "{}"
        assert "openshard.adapters.claude_hooks" not in result.stdout
        assert "openshard.adapters.grok_build_hooks" not in result.stdout
        assert _wait_for(lambda: bool(_lines(repo)))
        assert _lines(repo)[0]["executor"] == "grok_build_hooks"

    def test_no_spawn_falls_back_inline(self, capture_env, repo):
        stream = io.BytesIO(json.dumps(_doc(repo, prompt="do it")).encode())
        label, reply = client.run_grok_build_hook(stream, env=capture_env, event_override="UserPromptSubmit",
                                                  spawn=False)
        assert label == "record_created" and reply == "{}"
        assert _lines(repo)[0]["executor"] == "grok_build_hooks"


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class TestInstaller:
    def test_fresh_install_writes_our_own_file_and_excludes_it_from_git(self, repo):
        result = install_grok_build_hooks(repo_root=repo)
        assert result.status == "installed", result.message
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert HOOKS_RELPATH.as_posix() == ".grok/hooks/openshard.json"
        assert data == {"hooks": build_hook_config()}
        hooks = data["hooks"]
        assert set(hooks) == set(HOOK_EVENTS) == set(gb.GROK_BUILD_HOOK_EVENTS)
        assert "PreToolUse" not in hooks  # the one event that can deny a tool
        for event, groups in hooks.items():
            (group,) = groups
            assert "matcher" not in group  # omitted = every tool
            (handler,) = group["hooks"]
            assert handler["type"] == "command" and handler["timeout"] >= 5
            assert handler["command"].startswith(f"{HOOK_COMMAND} --event {event}")
        assert hooks["SessionEnd"][0]["hooks"][0]["command"].endswith("--no-spawn")
        assert "--no-spawn" not in hooks["SessionStart"][0]["hooks"][0]["command"]
        assert installed_grok_build_events(data) == list(HOOK_EVENTS)
        check = subprocess.run(["git", "check-ignore", "-q", HOOKS_RELPATH.as_posix()], cwd=repo)
        assert check.returncode == 0

    def test_no_http_handler_and_no_secret_in_the_file(self, repo):
        install_grok_build_hooks(repo_root=repo)
        text = (repo / HOOKS_RELPATH).read_text(encoding="utf-8")
        assert '"http"' not in text and "url" not in text and "token" not in text.lower()

    def test_idempotent(self, repo):
        assert install_grok_build_hooks(repo_root=repo).status == "installed"
        before = (repo / HOOKS_RELPATH).read_bytes()
        again = install_grok_build_hooks(repo_root=repo)
        assert again.status == "already_installed"
        assert all(v == "unchanged" for v in again.events.values())
        assert (repo / HOOKS_RELPATH).read_bytes() == before

    def test_other_files_in_the_hooks_dir_are_never_touched(self, repo):
        (repo / ".grok" / "hooks").mkdir(parents=True)
        mine = repo / ".grok" / "hooks" / "guard.json"
        guard = {"hooks": {"PreToolUse": [{"matcher": "bash", "hooks": [{"type": "command", "command": "./g.sh"}]}]}}
        mine.write_text(json.dumps(guard), encoding="utf-8")
        install_grok_build_hooks(repo_root=repo)
        assert json.loads(mine.read_text(encoding="utf-8")) == guard
        uninstall_grok_build_hooks(repo_root=repo)
        assert json.loads(mine.read_text(encoding="utf-8")) == guard

    def test_foreign_entries_inside_our_file_survive_and_stale_ones_of_ours_update(self, repo):
        (repo / ".grok" / "hooks").mkdir(parents=True)
        foreign = {"matcher": "bash", "hooks": [{"type": "command", "command": "./mine.sh"}]}
        (repo / HOOKS_RELPATH).write_text(json.dumps({"hooks": {
            "PostToolUse": [foreign],
            "Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND}]}],
        }}), encoding="utf-8")
        result = install_grok_build_hooks(repo_root=repo)
        assert result.status == "updated" and result.events["Stop"] == "updated"
        hooks = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))["hooks"]
        assert foreign in hooks["PostToolUse"] and len(hooks["PostToolUse"]) == 2
        uninstall_grok_build_hooks(repo_root=repo)
        assert json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8")) == {
            "hooks": {"PostToolUse": [foreign]}}

    def test_refuses_to_clobber_an_unparseable_file(self, repo):
        (repo / ".grok" / "hooks").mkdir(parents=True)
        (repo / HOOKS_RELPATH).write_text("{ not json", encoding="utf-8")
        assert install_grok_build_hooks(repo_root=repo).status == "error"
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == "{ not json"

    def test_uninstall_removes_only_ours_and_never_history(self, repo):
        install_grok_build_hooks(repo_root=repo)
        _drive_inline(repo)
        result = uninstall_grok_build_hooks(repo_root=repo)
        assert result.status == "removed" and not (repo / HOOKS_RELPATH).exists()  # our own file, now empty
        assert uninstall_grok_build_hooks(repo_root=repo).status == "not_installed"
        assert len(_lines(repo)) == 1  # history is never touched


# ---------------------------------------------------------------------------
# CLI: hooks grok-build, capture install/uninstall grok-build, setup, doctor
# ---------------------------------------------------------------------------


def _which(name: str):
    return {"grok": "/usr/local/bin/grok", "openshard": "/usr/local/bin/openshard"}.get(name)


class TestCli:
    def test_hooks_grok_build_command_records_inline_and_replies_empty(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["hooks", "grok-build", "--event", "UserPromptSubmit"],
                               input=json.dumps(_doc(repo, prompt="do it")))
        assert result.exit_code == 0 and result.output == "{}\n", result.output
        result = runner.invoke(cli, ["hooks", "grok-build", "--event", "Stop", "--no-spawn"],
                               input=json.dumps(_doc(repo, "Stop")))
        assert result.exit_code == 0 and result.output == "{}\n"
        entry = _lines(repo)[0]
        assert entry["executor"] == "grok_build_hooks" and entry["capture"]["turn_count"] == 1
        result = runner.invoke(cli, ["hooks", "grok-build", "--event", "Stop"], input="not json")
        assert result.exit_code == 0 and result.output == "{}\n"

    def test_entrypoint_fast_path_handles_hooks_grok_build(self, repo):
        code = (
            "import sys; sys.argv = ['openshard', 'hooks', 'grok-build', '--event', 'Stop', '--no-spawn']; "
            "from openshard.cli.entrypoint import main; main(); "
            "print(sorted(m for m in sys.modules if m.startswith('openshard.cli')))"
        )
        env = {**os.environ, "OPENSHARD_CAPTURE_DISABLE": "1"}
        handle_hook(_doc(repo, "UserPromptSubmit", prompt="x"), env={}, agent="grok_build",
                    event_override="UserPromptSubmit")
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc(repo, "Stop")),
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == "{}" and "openshard.cli.main" not in out[1]
        assert _lines(repo)[0]["capture"]["turn_count"] == 1

    def test_capture_install_and_uninstall_grok_build(self, repo):
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "grok-build", "--repo-path", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["agent"] == "grok_build" and data["status"] == "installed" and data["configured"] is True
        assert any("/hooks-trust" in s and "--trust" in s for s in data["next_steps"])
        assert (repo / HOOKS_RELPATH).exists()
        result = runner.invoke(cli, ["capture", "install", "grok-build", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "already installed" in result.output and "Grok Build" in result.output
        assert "/hooks-trust" in result.output
        result = runner.invoke(cli, ["capture", "uninstall", "grok-build", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "removed" in result.output

    def test_setup_configures_grok_build_without_claude(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["readiness"] == "ready"
        assert data["agents"]["grok_build"]["status"] == "installed"
        assert data["configured_agents"] == ["grok_build"]
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--repo-path", str(repo), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Grok Build:" in result.output and "Use Grok Build normally" in result.output

    def test_setup_without_grok_points_at_capture_install(self, repo):
        with patch("shutil.which", return_value=None):
            result = CliRunner().invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        data = json.loads(result.output)
        assert any("openshard capture install grok-build" in s for s in data["next_steps"])

    def test_doctor_reports_grok_build_independently_with_trust_and_verification(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            before = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            install_grok_build_hooks(repo_root=repo)
            after = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
            _drive_inline(repo)
            proven = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            proven_human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert before.exit_code == 0 and after.exit_code == 0, after.output
        assert json.loads(before.output)["grok_build"]["configured"] is False
        status = json.loads(after.output)["grok_build"]
        assert status["configured"] is True and status["cli_available"] is True
        assert status["events_missing"] == [] and status["capture_observed"] is None
        # Installed but never proven: doctor says so and names the trust step.
        assert "\nGrok Build\n" in human.output
        assert "no Grok Build session captured yet" in human.output and "/hooks-trust" in human.output
        assert "Configured but unverified: Grok Build" in human.output
        assert json.loads(proven.output)["grok_build"]["capture_observed"] is True
        assert "Capture verified" in proven_human.output and "no Grok Build session captured" not in proven_human.output
        with patch("shutil.which", return_value=None):
            absent = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert "openshard capture install grok-build" in absent.output

    def test_doctor_reports_a_partial_install(self, repo):
        install_grok_build_hooks(repo_root=repo)
        path = repo / HOOKS_RELPATH
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["hooks"]["PermissionDenied"]
        path.write_text(json.dumps(data), encoding="utf-8")
        with patch("shutil.which", side_effect=_which):
            result = CliRunner().invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
        status = json.loads(result.output)["grok_build"]
        assert status["configured"] is False and status["events_missing"] == ["PermissionDenied"]

    def test_setup_agent_snapshot_includes_grok_build(self, repo):
        with patch("shutil.which", side_effect=_which):
            result = CliRunner().invoke(cli, ["setup", "--agent", "--json", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["grok_build"]["cli_available"] is True and data["grok_build"]["configured"] is False
