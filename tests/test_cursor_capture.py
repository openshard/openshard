"""Tests for Cursor capture (0.4.2): translator, installer, service path, CLI.

Every test drives the adapter with synthetic Cursor hook documents in a
throw-away git repository. No real Cursor is ever run; where the
installer/setup code looks for one, ``shutil.which`` is patched.
"""

from __future__ import annotations

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
from openshard.adapters import cursor_hooks as cu
from openshard.adapters.claude_hooks import handle_hook, reduce_hook_payload
from openshard.adapters.cursor_hooks_install import (
    HOOK_COMMAND,
    HOOK_EVENTS,
    HOOKS_RELPATH,
    build_hook_config,
    install_cursor_hooks,
    is_openshard_cursor_hook,
    uninstall_cursor_hooks,
)
from openshard.cli.main import cli
from openshard.history.event import SOURCE_CURSOR_HOOKS, events_from_entry
from openshard.history.query import get_receipt, list_shards
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt

SID = "6f1a2b3c-4d5e-4f60-8a71-b2c3d4e5f607"
SID2 = "6f1a2b3c-4d5e-4f60-8a71-ffffffffffff"
SECRET = "sk-proj-SECRETSECRET12345678901234567890abcdef"
TRANSCRIPT = "/home/user/.cursor/transcripts/2026/09/conversation-abc.jsonl"
EMAIL = "developer@example.com"
EDIT_TEXT = "def add(a, b):\n    return a + b  # RAW EDIT CONTENT"


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
    return _make_repo(tmp_path / "cursor repo")


def _doc(event: str, repo: Path, sid: str = SID, **fields) -> dict:
    """A Cursor hook document with the base fields Cursor always sends.

    *sid* is the conversation id; a ``session_id=`` kwarg is the separate
    payload field sessionStart/sessionEnd carry, so it stays in *fields*.
    """
    base: dict = {
        "conversation_id": sid,
        "generation_id": "gen_0001",
        "hook_event_name": event,
        "model": "claude-4-sonnet",
        "cursor_version": "1.7.0",
        "workspace_roots": [str(repo)],
        "user_email": EMAIL,
        "transcript_path": TRANSCRIPT,
    }
    base.update(fields)
    return base


def _run(repo: Path, event: str, sid: str = SID, **fields):
    return handle_hook(_doc(event, repo, sid, **fields), env={}, agent="cursor")


def _drive_inline(repo: Path, sid: str = SID) -> None:
    _run(repo, "sessionStart", sid, session_id=sid, is_background_agent=False, composer_mode="agent")
    _run(repo, "beforeSubmitPrompt", sid, prompt=f"Add a calculator module; token {SECRET}",
         attachments=[{"type": "file", "file_path": str(repo / "README.md")}])
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(repo, "postToolUse", sid, tool_name="Write", tool_use_id="call_1", cwd=str(repo),
         tool_input={"path": str(repo / "calc.py"), "content": EDIT_TEXT}, tool_output=json.dumps({"ok": True}),
         duration=12)
    _run(repo, "afterFileEdit", sid, file_path=str(repo / "calc.py"),
         edits=[{"old_string": "", "new_string": EDIT_TEXT + SECRET}])
    _run(repo, "postToolUse", sid, tool_name="Shell", tool_use_id="call_2", cwd=str(repo),
         tool_input={"command": "python -m pytest -q", "working_directory": str(repo)},
         tool_output=json.dumps({"stdout": f"3 passed {SECRET}"}), duration=800)
    _run(repo, "postToolUse", sid, tool_name="MCP:github_create_issue", tool_use_id="call_3",
         cwd=str(repo), tool_input={"title": "x"}, tool_output=json.dumps({"ok": True}), duration=5)
    _run(repo, "stop", sid, status="completed", loop_count=0)
    _run(repo, "sessionEnd", sid, session_id=sid, reason="completed", duration_ms=9000,
         is_background_agent=False, final_status="completed")


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
    def test_lifecycle_events_map_and_model_is_preserved(self, repo):
        p = cu.extract_cursor_payload(_doc("sessionStart", repo, session_id=SID, is_background_agent=False))
        assert p is not None and p.event == "SessionStart" and p.source == "startup"
        assert p.agent == "cursor" and p.model_id == "claude-4-sonnet" and p.provider_id is None
        assert p.cwd == str(repo) and p.session_id == SID  # cwd falls back to workspace_roots[0]
        bg = cu.extract_cursor_payload(_doc("sessionStart", repo, is_background_agent=True))
        assert bg.source == "background"
        end = cu.extract_cursor_payload(_doc("sessionEnd", repo, reason="window_close", error_message="RAW ERROR"))
        assert end.event == "SessionEnd" and end.reason == "window_close"
        # model_id (optional, more canonical) wins over the display model name.
        m = cu.extract_cursor_payload(_doc("stop", repo, status="completed", model_id="anthropic/claude-4-sonnet"))
        assert m.model_id == "anthropic/claude-4-sonnet"

    def test_stop_status_decides_completion(self, repo):
        assert cu.extract_cursor_payload(_doc("stop", repo, status="completed")).event == "Stop"
        assert cu.extract_cursor_payload(_doc("stop", repo, status="aborted")).event == "Interrupt"
        # An errored, missing or unknown status is never a completed turn.
        for status in ("error", "weird", None, 3):
            doc = _doc("stop", repo)
            if status is not None:
                doc["status"] = status
            assert cu.extract_cursor_payload(doc).event == "SessionIdle", status

    def test_unsubscribed_or_foreign_events_are_ignored(self, repo):
        for ev in ("afterShellExecution", "beforeReadFile", "afterAgentResponse", "afterAgentThought",
                   "preToolUse", "beforeShellExecution", "beforeMCPExecution", "afterMCPExecution",
                   "subagentStart", "subagentStop", "preCompact", "workspaceOpen", "beforeTabFileRead",
                   "SessionStart", "PostToolUse", "Nope"):
            assert cu.extract_cursor_payload(_doc(ev, repo)) is None, ev
        assert cu.extract_cursor_payload({"conversation_id": SID}) is None
        # event_override is only a fallback for a payload with no hook_event_name.
        assert cu.extract_cursor_payload(_doc("stop", repo, status="completed"), event_override="sessionEnd").event == "Stop"
        no_name = {k: v for k, v in _doc("stop", repo, status="completed").items() if k != "hook_event_name"}
        assert cu.extract_cursor_payload(no_name) is None
        assert cu.extract_cursor_payload(no_name, event_override="stop").event == "Stop"

    def test_session_id_and_cwd_resolution(self, repo):
        # conversation_id is the session; session_id (sessionStart/End) is the fallback.
        p = cu.extract_cursor_payload(_doc("sessionStart", repo, conversation_id="../../etc", session_id=SID2))
        assert p.session_id == SID2
        p = cu.extract_cursor_payload(_doc("sessionStart", repo, conversation_id=42))
        assert p.session_id is None
        # A tool event's own cwd wins over workspace_roots.
        sub = repo / "pkg"
        sub.mkdir()
        p = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name="Shell", cwd=str(sub),
                                           tool_input={"command": "ls"}))
        assert p.cwd == str(sub)
        p = cu.extract_cursor_payload(_doc("stop", repo, status="completed", workspace_roots=[]))
        assert p.cwd is None
        p = cu.extract_cursor_payload(_doc("stop", repo, status="completed", workspace_roots="not a list"))
        assert p.cwd is None

    def test_tools_classify_and_read_only_path_or_command(self, repo):
        p = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name="Shell",
                                           tool_input={"command": "ls -la", "working_directory": "/x"},
                                           tool_output="RAW OUTPUT"))
        assert p.tool_kind == "command" and p.command == "ls -la" and p.file_path is None
        assert p.tool_success is None
        for name in ("Write", "write", "Delete"):
            p = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name=name, tool_input={"file_path": "a.py"}))
            assert p.tool_kind == "file" and p.file_path == "a.py" and p.command is None, name
        # The Write/Delete path key is not documented: ``path`` is tolerated, anything else under-reports.
        p = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name="Write", tool_input={"path": "b.py"}))
        assert p.file_path == "b.py"
        p = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name="Write", tool_input={"target": "c.py"}))
        assert p.tool_kind == "file" and p.file_path is None
        for name in ("Read", "Grep", "Task", "MCP:github_create_issue", "SomethingNew"):
            p = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name=name,
                                               tool_input={"command": "rm -rf /", "file_path": "/etc/passwd"}))
            assert p.tool_kind == "other" and p.command is None and p.file_path is None, name
        # Failures map to the failure event with the same tool handling.
        f = cu.extract_cursor_payload(_doc("postToolUseFailure", repo, tool_name="Shell",
                                           tool_input={"command": "pytest"}, error_message="RAW ERROR",
                                           failure_type="timeout", is_interrupt=False))
        assert f.event == "PostToolUseFailure" and f.tool_kind == "command" and f.command == "pytest"
        # Malformed tool_input shapes: nothing is guessed.
        for bad in ({"command": 42}, {"command": ["ls"]}, "not a dict", None, []):
            s = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name="Shell", tool_input=bad))
            assert s.tool_kind == "command" and s.command is None, bad
            w = cu.extract_cursor_payload(_doc("postToolUse", repo, tool_name="Write", tool_input=bad))
            assert w.tool_kind == "file" and w.file_path is None, bad

    def test_after_file_edit_reads_the_path_only(self, repo):
        p = cu.extract_cursor_payload(_doc("afterFileEdit", repo, file_path=str(repo / "calc.py"),
                                           edits=[{"old_string": "x", "new_string": EDIT_TEXT + SECRET}]))
        assert p.event == "FileEdited" and p.file_path == str(repo / "calc.py")
        reduced = reduce_hook_payload(p, repo)
        assert reduced.file_target == "calc.py"
        blob = json.dumps(reduced.to_dict())
        assert SECRET not in blob and "RAW EDIT" not in blob

    def test_prompt_feeds_only_the_task_excerpt(self, repo):
        p = cu.extract_cursor_payload(_doc("beforeSubmitPrompt", repo, prompt=f"fix it {SECRET}",
                                           attachments=[{"type": "rule", "file_path": "/home/u/.cursor/rules/x.mdc"}]))
        assert p.event == "UserPromptSubmit"
        reduced = reduce_hook_payload(p, repo)
        assert reduced.task_excerpt is not None and reduced.task_excerpt.startswith("fix it")
        blob = json.dumps(reduced.to_dict())
        assert SECRET not in blob and ".cursor/rules" not in blob

    def test_never_reads_email_transcript_or_output(self, repo):
        for ev, extra in (
            ("sessionStart", {}),
            ("beforeSubmitPrompt", {"prompt": "hi"}),
            ("postToolUse", {"tool_name": "Shell", "tool_input": {"command": "ls"}, "tool_output": f"out {SECRET}"}),
            ("afterFileEdit", {"file_path": str(repo / "a.py"), "edits": [{"old_string": "", "new_string": SECRET}]}),
            ("stop", {"status": "completed"}),
            ("sessionEnd", {"reason": "error", "error_message": f"boom {SECRET}"}),
        ):
            p = cu.extract_cursor_payload(_doc(ev, repo, **extra))
            assert not hasattr(p, "user_email") and not hasattr(p, "transcript_path")
            blob = json.dumps(reduce_hook_payload(p, repo).to_dict())
            assert EMAIL not in blob and TRANSCRIPT not in blob and SECRET not in blob, ev


# ---------------------------------------------------------------------------
# Payload -> canonical Events / record / receipt (inline path)
# ---------------------------------------------------------------------------


class TestCanonicalRecord:
    def test_session_becomes_one_cursor_shard(self, repo):
        _drive_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "cursor_hooks"
        assert entry["import_source"] == "cursor"
        assert entry["execution_model"] == "claude-4-sonnet"
        cap = entry["capture"]
        assert cap["source"] == "cursor_hooks" and cap["agent"] == "cursor"
        assert cap["agent_vendor"] == "Anysphere" and cap["provider"] is None
        assert cap["model_source"] == "cursor_hook"
        assert cap["session_id"] == SID and cap["session_end_observed"] is True
        assert cap["session_end_reason"] == "completed" and cap["start_source"] == "startup"
        # Write, Shell and the MCP tool are tool calls; afterFileEdit is a file signal, not a call.
        assert cap["prompt_count"] == 1 and cap["tool_call_count"] == 3 and cap["turn_count"] == 1
        assert cap["task_status"] == "turn_completed"
        assert entry["task"].startswith("Add a calculator module")
        raw = json.dumps(entry)
        assert SECRET not in raw and TRANSCRIPT not in raw and EMAIL not in raw and "RAW EDIT" not in raw
        # A test command was observed: honestly "attempted", never a fabricated outcome.
        assert entry["verification_attempted"] is True and entry["verification_passed"] is None
        for key in ("estimated_cost", "cost_provenance", "prompt_tokens", "tokens_provenance"):
            assert key not in entry
        assert "duration_seconds" in entry
        assert "calc.py" in {f["path"] for f in entry["files_detail"]}  # git-observed
        assert entry["files_source"] == "git_diff_inferred"

    def test_events_carry_cursor_identity_and_evidence(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        events = events_from_entry(entry)
        assert events and all(e.source == SOURCE_CURSOR_HOOKS for e in events)
        assert all(e.actor == "cursor" for e in events)
        types = [e.event_type for e in events]
        assert "session.started" in types and "run.completed" in types and "tool.invoked" in types
        tools = [e for e in events if e.event_type == "tool.invoked"]
        assert {e.metadata.get("tool") for e in tools} == {"Write", "Shell", "MCP:github_create_issue"}
        write_ev = next(e for e in tools if e.metadata.get("tool") == "Write")
        assert write_ev.evidence == "agent_reported" and write_ev.target == "calc.py"
        assert write_ev.status == "unknown"  # Cursor gives no success signal for postToolUse
        assert not [e for e in tools if e.status == "passed"]
        shell_ev = next(e for e in tools if e.metadata.get("tool") == "Shell")
        assert shell_ev.action.startswith("Shell: ") and shell_ev.metadata.get("command_kind") == "test"
        assert shell_ev.status == "unknown"
        assert not [e for e in events if e.event_type.startswith("verification.")]
        started = next(e for e in events if e.event_type == "session.started")
        assert "Cursor session observed" in started.action and started.evidence == "directly_observed"

    def test_receipt_identity(self, repo):
        _drive_inline(repo)
        receipt = build_shard_receipt(_lines(repo)[0])
        assert receipt.agent == "Cursor (external)"
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED
        assert receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.tokens_input is None and receipt.cost_provenance is None
        text = render_compact_shard_receipt(receipt)
        assert "Cursor (external)" in text and "did not execute or verify" in text
        assert SECRET not in text
        shards = list_shards(repo_path=repo)
        assert len(shards) == 1 and shards[0].agent == "Cursor (external)"
        assert get_receipt(shards[0].shard_id, repo_path=repo).agent == "Cursor (external)"

    def test_aborted_stop_is_activity_and_errored_stop_is_idle(self, repo):
        _run(repo, "sessionStart", session_id=SID)
        _run(repo, "beforeSubmitPrompt", prompt="do a thing")
        _run(repo, "postToolUse", tool_name="Shell", tool_input={"command": "echo hi"})
        outcome = _run(repo, "stop", status="aborted")
        assert outcome.action in ("record_updated", "record_created")
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["task_status"] == "in_progress"
        assert any("interrupted" in e["action"] for e in entry["events"])
        _run(repo, "stop", status="error")
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["idle_count"] == 1
        assert any("turn completion not confirmed" in e["action"] for e in entry["events"])

    def test_after_file_edit_is_the_hook_reported_file_signal(self, tmp_path):
        root = tmp_path / "plain"
        root.mkdir()
        with patch("openshard.adapters.claude_mcp_install.find_repo_root", return_value=None), \
             patch("openshard.adapters.claude_code_import.subprocess.run",
                   side_effect=FileNotFoundError("no git")):
            _run(root, "beforeSubmitPrompt", prompt="task")
            # A Write claim alone is never file evidence (no success signal)...
            _run(root, "postToolUse", tool_name="Write", tool_input={"path": str(root / "made.py")})
            _run(root, "stop", status="completed")
            entry = _lines(root)[0]
            assert entry["files_source"] == "not_available" and entry["files_detail"] == []
            # ...afterFileEdit (fired after the edit was applied) is.
            _run(root, "afterFileEdit", file_path=str(root / "made.py"), edits=[{"old_string": "", "new_string": "x"}])
            _run(root, "stop", status="completed")
        entry = _lines(root)[0]
        assert entry["files_source"] == "cursor_hook_reported"
        assert entry["files_detail"] == [
            {"path": "made.py", "change_type": "update", "summary": "reported by Cursor hook"}
        ]
        fe = [e for e in entry["events"] if e["event_type"] == "file.changed"]
        assert fe and fe[0]["evidence"] == "agent_reported" and fe[0]["metadata"]["evidence_source"] == "cursor_hook"

    def test_background_agent_without_session_start_is_captured(self, repo):
        # Cloud/background agents never fire sessionStart/sessionEnd.
        _run(repo, "beforeSubmitPrompt", prompt="background task")
        _run(repo, "postToolUse", tool_name="Shell", tool_input={"command": "make build"})
        _run(repo, "stop", status="completed")
        entry = _lines(repo)[0]
        assert entry["executor"] == "cursor_hooks" and entry["capture"]["turn_count"] == 1
        assert entry["capture"]["session_end_observed"] is False
        started = next(e for e in entry["events"] if e["event_type"] == "session.started")
        assert "first hook: UserPromptSubmit" in started["action"]

    def test_model_missing_stays_unknown(self, repo):
        doc = _doc("beforeSubmitPrompt", repo, prompt="x")
        del doc["model"]
        handle_hook(doc, env={}, agent="cursor")
        handle_hook({k: v for k, v in _doc("stop", repo, status="completed").items() if k != "model"},
                    env={}, agent="cursor")
        entry = _lines(repo)[0]
        assert entry["execution_model"] == "unknown"
        assert entry["capture"]["model_source"] == "not_captured"

    def test_cursor_local_state_is_never_a_changed_file(self, repo):
        _run(repo, "sessionStart", session_id=SID)
        _run(repo, "beforeSubmitPrompt", prompt="task")
        (repo / ".cursor").mkdir()
        (repo / ".cursor" / "hooks.json").write_text("{}", encoding="utf-8")
        _run(repo, "stop", status="completed")
        assert _lines(repo)[0]["files_detail"] == []

    def test_same_session_id_as_a_claude_session_is_a_separate_shard(self, repo):
        from openshard.adapters.claude_hooks import handle_claude_hook

        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        claude_doc = {"session_id": SID, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "claude"}
        handle_claude_hook(claude_doc, env=env)
        handle_claude_hook({**claude_doc, "hook_event_name": "Stop"}, env=env)
        _run(repo, "beforeSubmitPrompt", prompt="cursor")
        _run(repo, "stop", status="completed")
        lines = _lines(repo)
        assert {e["executor"] for e in lines} == {"claude_code_hooks", "cursor_hooks"}
        assert len({e["shard_id"] for e in lines}) == 2


# ---------------------------------------------------------------------------
# The stdout decision reply
# ---------------------------------------------------------------------------


class TestReply:
    def test_reply_depends_only_on_the_event_name(self):
        assert client.cursor_hook_response(b'{"hook_event_name": "beforeSubmitPrompt"}') == '{"continue": true}'
        for ev in ("sessionStart", "postToolUse", "afterFileEdit", "stop", "sessionEnd", "afterShellExecution"):
            assert client.cursor_hook_response(json.dumps({"hook_event_name": ev}).encode()) == "{}", ev
        assert client.cursor_hook_response(b"not json") == "{}"
        assert client.cursor_hook_response(b"") == "{}"
        assert client.cursor_hook_response(b"[1, 2]") == "{}"
        assert client.cursor_hook_response(b"{}", "beforeSubmitPrompt") == '{"continue": true}'
        assert client.cursor_hook_response(b'{"hook_event_name": "beforeSubmitPrompt"}', "stop") == "{}"

    def test_reply_is_the_same_whether_or_not_capture_works(self, repo, monkeypatch):
        import io

        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        doc = json.dumps(_doc("beforeSubmitPrompt", repo, prompt="p")).encode()
        label, reply = client.run_cursor_hook(io.BytesIO(doc), env=dict(os.environ), spawn=False)
        assert reply == '{"continue": true}' and label == "record_created"
        with patch("openshard.adapters.claude_capture_client._inline_hook", side_effect=RuntimeError("boom")):
            label, reply = client.run_cursor_hook(io.BytesIO(doc), env=dict(os.environ), spawn=False)
        assert reply == '{"continue": true}' and label == "error"
        label, reply = client.run_cursor_hook(io.BytesIO(b""), env=dict(os.environ), spawn=False)
        assert reply == "{}" and label == "ignored"


# ---------------------------------------------------------------------------
# Service path: POST /hooks/cursor
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


def _post(port: int, doc: dict) -> bool:
    return client.post_hook(port, json.dumps(doc).encode("utf-8"), hook_path=client.CURSOR_HOOK_PATH)


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
        assert _post(service.port, _doc("sessionStart", via_http, session_id=SID, is_background_agent=False))
        assert _post(service.port, _doc("beforeSubmitPrompt", via_http, prompt=f"Add a calculator module; token {SECRET}",
                                        attachments=[{"type": "file", "file_path": str(via_http / "README.md")}]))
        (via_http / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert _post(service.port, _doc("postToolUse", via_http, tool_name="Write", tool_use_id="call_1",
                                        cwd=str(via_http), tool_input={"path": str(via_http / "calc.py"), "content": EDIT_TEXT},
                                        tool_output=json.dumps({"ok": True}), duration=12))
        assert _post(service.port, _doc("afterFileEdit", via_http, file_path=str(via_http / "calc.py"),
                                        edits=[{"old_string": "", "new_string": EDIT_TEXT + SECRET}]))
        assert _post(service.port, _doc("postToolUse", via_http, tool_name="Shell", tool_use_id="call_2",
                                        cwd=str(via_http), tool_input={"command": "python -m pytest -q", "working_directory": str(via_http)},
                                        tool_output=json.dumps({"stdout": f"3 passed {SECRET}"}), duration=800))
        assert _post(service.port, _doc("postToolUse", via_http, tool_name="MCP:github_create_issue", tool_use_id="call_3",
                                        cwd=str(via_http), tool_input={"title": "x"}, tool_output=json.dumps({"ok": True}), duration=5))
        assert _post(service.port, _doc("stop", via_http, status="completed", loop_count=0))
        assert _post(service.port, _doc("sessionEnd", via_http, session_id=SID, reason="completed", duration_ms=9000,
                                        is_background_agent=False, final_status="completed"))
        _drive_inline(via_inline)
        assert _wait_for(lambda: bool(_lines(via_http)) and _lines(via_http)[0]["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        assert _stable(_lines(via_http)[0]) == _stable(_lines(via_inline)[0])
        raw = (via_http / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert SECRET not in raw and EMAIL not in raw and TRANSCRIPT not in raw

    def test_queue_line_is_reduced_and_agent_tagged(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, _doc("afterFileEdit", repo, file_path=str(repo / "calc.py"),
                                        edits=[{"old_string": "", "new_string": EDIT_TEXT + SECRET}]))
        queue_file = repo / ".openshard" / "claude_sessions" / f"cursor.{SID}{svc.QUEUE_SUFFIX}"
        assert not (repo / ".openshard" / "claude_sessions" / f"{SID}{svc.QUEUE_SUFFIX}").exists()
        line = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
        assert line["kind"] == "hook" and line["data"]["agent"] == "cursor"
        assert line["data"]["event"] == "FileEdited" and line["data"]["file_target"] == "calc.py"
        assert line["data"]["model_id"] == "claude-4-sonnet" and line["data"]["tool_success"] is None
        text = queue_file.read_text(encoding="utf-8")
        assert SECRET not in text and EMAIL not in text and TRANSCRIPT not in text and "RAW EDIT" not in text
        service.server.recorder.resume_processing()

    def test_unsupported_cursor_event_is_ignored_not_errored(self, service, repo):
        status, reply = client._request("POST", service.port, client.CURSOR_HOOK_PATH,
                                        json.dumps(_doc("afterShellExecution", repo, command="ls", output="x")).encode(),
                                        client._auth_headers(None, None))
        assert status == 200 and reply == b"{}"
        assert client.health(service.port)["stats"]["queued"] == 0

    def test_hook_command_forwards_and_never_imports_fold_code(self, service, repo, capture_env):
        code = (
            "import sys, os; from openshard.adapters.claude_capture_client import run_cursor_hook; "
            "label, reply = run_cursor_hook(sys.stdin, env=dict(os.environ)); print(label); print(reply); "
            "print(sorted(m for m in sys.modules if m.startswith('openshard')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc("beforeSubmitPrompt", repo, prompt="via cli")),
            capture_output=True, text=True, timeout=60, env=capture_env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == "forwarded" and out[1] == '{"continue": true}'
        assert "openshard.adapters.claude_hooks" not in result.stdout
        assert "openshard.adapters.cursor_hooks" not in result.stdout
        assert _wait_for(lambda: bool(_lines(repo)))
        assert _lines(repo)[0]["executor"] == "cursor_hooks"

    def test_no_spawn_falls_back_inline(self, capture_env, repo):
        import io

        stream = io.BytesIO(json.dumps(_doc("beforeSubmitPrompt", repo, prompt="fallback")).encode())
        label, reply = client.run_cursor_hook(stream, env=capture_env, spawn=False)
        assert label == "record_created" and reply == '{"continue": true}'
        assert _lines(repo)[0]["executor"] == "cursor_hooks"


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class TestInstaller:
    def test_fresh_install_writes_all_events_and_excludes_file_from_git(self, repo):
        result = install_cursor_hooks(repo_root=repo)
        assert result.status == "installed", result.message
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert set(data["hooks"]) == set(HOOK_EVENTS)
        assert data["hooks"] == build_hook_config()
        for event, entries in data["hooks"].items():
            assert len(entries) == 1
            hook = entries[0]
            assert hook["type"] == "command" and hook["command"].startswith(HOOK_COMMAND)
            assert hook["failClosed"] is False
            assert hook["timeout"] <= (3 if event == "sessionEnd" else 15)
            assert "matcher" not in hook and "hooks" not in hook  # flat layout, not matcher groups
        assert "--no-spawn" in data["hooks"]["sessionEnd"][0]["command"]
        assert "--no-spawn" not in data["hooks"]["sessionStart"][0]["command"]
        exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert HOOKS_RELPATH.as_posix() in exclude
        check = subprocess.run(["git", "check-ignore", "-q", HOOKS_RELPATH.as_posix()], cwd=repo)
        assert check.returncode == 0

    def test_idempotent(self, repo):
        assert install_cursor_hooks(repo_root=repo).status == "installed"
        before = (repo / HOOKS_RELPATH).read_bytes()
        again = install_cursor_hooks(repo_root=repo)
        assert again.status == "already_installed"
        assert all(v == "unchanged" for v in again.events.values())
        assert (repo / HOOKS_RELPATH).read_bytes() == before

    def test_preserves_unrelated_hooks_keys_and_version(self, repo):
        (repo / ".cursor").mkdir()
        existing = {
            "version": 2,
            "hooks": {
                "beforeShellExecution": [{"command": "./guard.sh", "timeout": 10, "failClosed": True}],
                "postToolUse": [{"command": "./audit.sh", "matcher": "Shell"}],
                "stop": [{"command": HOOK_COMMAND, "timeout": 99}],
            },
            "custom": {"keep": True},
        }
        (repo / HOOKS_RELPATH).write_text(json.dumps(existing), encoding="utf-8")
        result = install_cursor_hooks(repo_root=repo)
        assert result.status == "updated"
        assert result.events["stop"] == "updated" and result.events["sessionStart"] == "added"
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert data["version"] == 2 and data["custom"] == {"keep": True}
        assert data["hooks"]["beforeShellExecution"] == existing["hooks"]["beforeShellExecution"]
        assert data["hooks"]["postToolUse"][0] == existing["hooks"]["postToolUse"][0]
        assert any(is_openshard_cursor_hook(h) for h in data["hooks"]["postToolUse"])
        assert [h for h in data["hooks"]["stop"] if is_openshard_cursor_hook(h)][0]["timeout"] == 5
        # A pre-existing file is not force-excluded from git.
        assert not (repo / ".git" / "info" / "exclude").exists() or \
            HOOKS_RELPATH.as_posix() not in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    def test_malformed_config_is_left_alone(self, repo):
        (repo / ".cursor").mkdir()
        (repo / HOOKS_RELPATH).write_text("{ not json", encoding="utf-8")
        result = install_cursor_hooks(repo_root=repo)
        assert result.status == "error" and "not valid JSON" in result.message
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == "{ not json"
        (repo / HOOKS_RELPATH).write_text(json.dumps({"hooks": []}), encoding="utf-8")
        result = install_cursor_hooks(repo_root=repo)
        assert result.status == "error" and "unexpected hooks layout" in result.message
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == json.dumps({"hooks": []})
        (repo / HOOKS_RELPATH).write_text(json.dumps({"hooks": {"stop": {"command": "x"}}}), encoding="utf-8")
        assert install_cursor_hooks(repo_root=repo).status == "error"
        assert uninstall_cursor_hooks(repo_root=repo).status == "not_installed"

    def test_uninstall_removes_only_ours(self, repo):
        (repo / ".cursor").mkdir()
        (repo / HOOKS_RELPATH).write_text(json.dumps({
            "version": 1,
            "hooks": {"beforeShellExecution": [{"command": "./guard.sh"}]},
        }), encoding="utf-8")
        install_cursor_hooks(repo_root=repo)
        result = uninstall_cursor_hooks(repo_root=repo)
        assert result.status == "removed"
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert data["hooks"]["beforeShellExecution"] == [{"command": "./guard.sh"}]
        assert all(not entries for ev, entries in data["hooks"].items() if ev != "beforeShellExecution")
        assert uninstall_cursor_hooks(repo_root=repo).status == "not_installed"
        _drive_inline(repo)
        uninstall_cursor_hooks(repo_root=repo)
        assert len(_lines(repo)) == 1  # history is never touched


# ---------------------------------------------------------------------------
# CLI: hooks cursor, capture install/uninstall cursor, setup, doctor
# ---------------------------------------------------------------------------


def _which(name: str):
    return {"cursor": "/usr/local/bin/cursor", "openshard": "/usr/local/bin/openshard"}.get(name)


def _which_agent_only(name: str):
    return {"cursor-agent": "/usr/local/bin/cursor-agent"}.get(name)


class TestCli:
    def test_hooks_cursor_command_records_inline_and_replies(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        runner = CliRunner()
        doc = json.dumps(_doc("beforeSubmitPrompt", repo, prompt="cli prompt"))
        result = runner.invoke(cli, ["hooks", "cursor"], input=doc)
        assert result.exit_code == 0, result.output
        assert result.output == '{"continue": true}\n'  # the decision reply, nothing else
        result = runner.invoke(cli, ["hooks", "cursor", "--no-spawn"], input=json.dumps(_doc("stop", repo, status="completed")))
        assert result.exit_code == 0 and result.output == "{}\n"
        assert _lines(repo)[0]["executor"] == "cursor_hooks"
        assert _lines(repo)[0]["capture"]["turn_count"] == 1

    def test_entrypoint_fast_path_handles_hooks_cursor(self, repo):
        code = (
            "import sys; sys.argv = ['openshard', 'hooks', 'cursor', '--no-spawn']; "
            "from openshard.cli.entrypoint import main; main(); "
            "print(sorted(m for m in sys.modules if m.startswith('openshard.cli')))"
        )
        env = {**os.environ, "OPENSHARD_CAPTURE_DISABLE": "1"}
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc("beforeSubmitPrompt", repo, prompt="fast")),
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == '{"continue": true}'
        assert "openshard.cli.main" not in out[1]  # the full CLI was never imported
        assert _lines(repo)[0]["executor"] == "cursor_hooks"

    def test_capture_install_and_uninstall_cursor(self, repo):
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "cursor", "--repo-path", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "installed" and data["configured"] is True
        assert any("no restart" in s for s in data["next_steps"])
        assert (repo / HOOKS_RELPATH).exists()
        result = runner.invoke(cli, ["capture", "install", "cursor", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "already installed" in result.output
        assert "Restart" not in result.output
        result = runner.invoke(cli, ["capture", "uninstall", "cursor", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "removed" in result.output
        assert json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))["hooks"]["stop"] == []

    def test_setup_configures_cursor_without_claude(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["readiness"] == "ready"
        assert data["agents"]["cursor"]["status"] == "installed"
        assert data["agents"]["codex"]["status"] == "skipped"
        assert data["configured_agents"] == ["cursor"]
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--repo-path", str(repo), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Cursor:" in result.output and "Use Cursor normally" in result.output

    def test_cursor_agent_cli_also_counts_as_detected(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which_agent_only):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["agents"]["cursor"]["status"] == "installed"

    def test_setup_without_cursor_points_at_capture_install(self, repo):
        runner = CliRunner()
        with patch("shutil.which", return_value=None):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["readiness"] == "not_ready"
        assert any("openshard capture install cursor" in s for s in data["next_steps"])

    def test_doctor_reports_cursor_independently(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            before = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            install_cursor_hooks(repo_root=repo)
            after = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert before.exit_code == 0 and after.exit_code == 0, after.output
        assert json.loads(before.output)["cursor"]["configured"] is False
        cursor = json.loads(after.output)["cursor"]
        assert cursor["configured"] is True and cursor["cli_available"] is True
        assert cursor["events_missing"] == []
        assert "\nCursor\n" in human.output and "Ready -- use Cursor normally" in human.output
        with patch("shutil.which", return_value=None):
            absent = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert "openshard capture install cursor" in absent.output

    def test_setup_agent_snapshot_includes_cursor(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--agent", "--json", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["cursor"]["cli_available"] is True and data["cursor"]["configured"] is False
        assert data["cursor"]["config_path"] == ".cursor/hooks.json"
        assert not (repo / HOOKS_RELPATH).exists()  # --agent is read-only
