"""Tests for Codex capture (PR12): translator, installer, service path, CLI.

Every test drives the adapter with synthetic Codex hook documents in a
throw-away git repository. No real ``codex`` binary is ever run; where the
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
from openshard.adapters import codex_hooks as cx
from openshard.adapters.claude_hooks import handle_hook, reduce_hook_payload
from openshard.adapters.codex_hooks_install import (
    HOOK_COMMAND,
    HOOK_EVENTS,
    HOOKS_RELPATH,
    build_hook_config,
    install_codex_hooks,
    is_openshard_codex_hook,
    uninstall_codex_hooks,
)
from openshard.cli.main import cli
from openshard.history.event import SOURCE_CODEX_HOOKS, events_from_entry
from openshard.history.query import get_receipt, list_shards
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt

SID = "019a4f3c-6c1e-7d2b-9c3e-2f4a5b6c7d8e"
SID2 = "019a4f3c-6c1e-7d2b-9c3e-ffffffffffff"
SECRET = "sk-proj-SECRETSECRET12345678901234567890abcdef"
TRANSCRIPT = "/home/user/.codex/sessions/2026/09/rollout-abc.jsonl"


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
    return _make_repo(tmp_path / "codex repo")


def _doc(event: str, repo: Path, session_id: str = SID, **fields) -> dict:
    """A Codex hook document with the common fields Codex always sends."""
    base: dict = {
        "session_id": session_id,
        "turn_id": "turn_0001",
        "hook_event_name": event,
        "cwd": str(repo),
        "model": "gpt-5-codex",
        "transcript_path": TRANSCRIPT,
        "permission_mode": "default",
    }
    base.update(fields)
    return base


PATCH = (
    "*** Begin Patch\n"
    "*** Add File: calc.py\n"
    "+def add(a, b):\n"
    f"+    return a + b  # {SECRET}\n"
    "*** Update File: README.md\n"
    "@@\n-hello\n+hello world\n"
    "*** Delete File: old.txt\n"
    "*** End Patch\n"
)


def _run(repo: Path, event: str, session_id: str = SID, **fields):
    return handle_hook(_doc(event, repo, session_id, **fields), env={}, agent="codex")


def _drive_inline(repo: Path, session_id: str = SID) -> None:
    _run(repo, "SessionStart", session_id, source="startup")
    _run(repo, "UserPromptSubmit", session_id, prompt=f"Add a calculator module; token {SECRET}")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(repo, "PostToolUse", session_id, tool_name="apply_patch", tool_use_id="call_1",
         tool_input={"patch": PATCH}, tool_response={"output": f"Done {SECRET}"})
    _run(repo, "PostToolUse", session_id, tool_name="Bash", tool_use_id="call_2",
         tool_input={"command": ["bash", "-lc", "python -m pytest -q"]},
         tool_response={"stdout": "3 passed", "exit_code": 0})
    _run(repo, "PostToolUse", session_id, tool_name="mcp__github__create_issue", tool_use_id="call_3",
         tool_input={"title": "x"}, tool_response={"ok": True})
    _run(repo, "Stop", session_id, stop_hook_active=False, last_assistant_message="All done")
    _run(repo, "SessionEnd", session_id)


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
        p = cx.extract_codex_payload(_doc("SessionStart", repo, source="resume"))
        assert p is not None and p.event == "SessionStart" and p.source == "resume"
        assert p.agent == "codex" and p.model_id == "gpt-5-codex" and p.provider_id is None
        assert p.cwd == str(repo) and p.session_id == SID
        assert cx.extract_codex_payload(_doc("Interrupt", repo)).event == "Interrupt"
        assert cx.extract_codex_payload(_doc("SessionEnd", repo)).event == "SessionEnd"
        assert cx.extract_codex_payload(_doc("Stop", repo, stop_hook_active=True)).stop_hook_active is True

    def test_unsubscribed_or_foreign_events_are_ignored(self, repo):
        for ev in ("PreToolUse", "PermissionRequest", "PreCompact", "SubagentStart", "Nope"):
            assert cx.extract_codex_payload(_doc(ev, repo)) is None
        assert cx.extract_codex_payload({"session_id": SID}) is None
        assert cx.extract_codex_payload(_doc("Stop", repo), event_override=None).session_id == SID
        assert cx.extract_codex_payload({"cwd": str(repo)}, event_override="Stop").session_id is None

    def test_apply_patch_reads_headers_only(self):
        files = cx.parse_apply_patch_files(PATCH)
        assert files == [("calc.py", "create"), ("README.md", "update"), ("old.txt", "delete")]
        assert cx.parse_apply_patch_files(42) == []
        assert cx.parse_apply_patch_files("no patch here") == []
        moved = "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n*** End Patch\n"
        assert cx.parse_apply_patch_files(moved) == [("a.py", "update"), ("b.py", "create")]

    def test_apply_patch_documented_and_tolerated_input_keys(self, repo):
        # ``command`` is the documented key; ``patch`` is tolerated (community templates).
        for key in ("command", "patch"):
            p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="apply_patch", tool_input={key: PATCH}))
            assert p.tool_kind == "file"
            assert [f for f, _ in p.file_paths] == ["calc.py", "README.md", "old.txt"]
        # The documented key wins when both are present.
        both = cx.extract_codex_payload(_doc(
            "PostToolUse", repo, tool_name="apply_patch",
            tool_input={"command": PATCH, "patch": "*** Begin Patch\n*** Add File: other.py\n*** End Patch\n"},
        ))
        assert [f for f, _ in both.file_paths] == ["calc.py", "README.md", "old.txt"]
        # Any other key under-reports: a file tool call with no file targets, never invented ones.
        p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="apply_patch", tool_input={"input": PATCH}))
        assert p.tool_kind == "file" and p.file_paths == [] and p.file_path is None

    def test_bash_command_string_or_argv(self, repo):
        p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="Bash", tool_input={"command": "ls -la"}))
        assert p.tool_kind == "command" and p.command == "ls -la"
        p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="bash",
                                          tool_input={"command": ["bash", "-lc", "pytest -q"]}))
        assert p.tool_kind == "command" and p.command == "bash -lc pytest -q"
        # Codex's internal tool names are not its hook-facing names (hook input
        # reports ``Bash`` even for exec_command completions): recorded by name
        # only, never summarized as a command.
        for name in ("shell", "exec_command", "local_shell", "shell_command", "unified_exec"):
            p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name=name, tool_input={"command": "rm -rf /"}))
            assert p.tool_kind == "other" and p.command is None, name

    def test_unconfirmed_shapes_under_report_never_fabricate(self, repo):
        # Edit/Write are matcher aliases only; hook input reports apply_patch. A
        # payload naming them yields a name-only record and no file evidence.
        for name in ("Edit", "Write", "MultiEdit"):
            p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name=name, tool_input={"file_path": "a.py"}))
            assert p.tool_kind == "other" and p.file_path is None and p.file_paths == [], name
        # Codex has no PostToolUseFailure: such a document is not a Codex hook.
        assert cx.extract_codex_payload(_doc("PostToolUseFailure", repo, tool_name="Bash")) is None
        # Only the documented ``reason`` is read on SessionEnd.
        assert cx.extract_codex_payload(_doc("SessionEnd", repo, end_reason="logout")).reason is None
        assert cx.extract_codex_payload(_doc("SessionEnd", repo, reason="other")).reason == "other"
        # Malformed tool_input shapes: nothing is guessed from them.
        for bad in ({"command": 42}, {"command": ["ok", 7]}, {"command": {"patch": PATCH}}, "not a dict", None, []):
            p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="apply_patch", tool_input=bad))
            assert p.tool_kind == "file" and p.file_paths == [] and p.file_path is None, bad
            b = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="Bash", tool_input=bad))
            assert b.tool_kind == "command" and b.command is None, bad
        # Broken patch headers name no file.
        junk = "*** Begin Patch\n*** Add File\n*** Update File:\n--- a/x.py\n+++ b/x.py\n*** End Patch\n"
        assert cx.parse_apply_patch_files(junk) == []
        # A Codex PostToolUse is never a success signal (it also fires for failed commands).
        ok = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="apply_patch", tool_input={"command": PATCH}))
        assert ok.tool_success is None
        assert reduce_hook_payload(ok, repo).tool_success is None

    def test_apply_patch_windows_style_paths(self, repo):
        crlf = "*** Begin Patch\r\n*** Update File: README.md\r\n@@\r\n*** End Patch\r\n"
        assert cx.parse_apply_patch_files(crlf) == [("README.md", "update")]
        inside = str(repo / "pkg" / "a.py")  # native absolute path inside the repository
        outside = "C:\\Users\\someone\\other\\a.py"
        patch_text = (
            f"*** Begin Patch\n*** Add File: {inside}\n*** Update File: {outside}\n"
            "*** Update File: /etc/hosts\n*** Delete File: README.md\n*** End Patch\n"
        )
        p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="apply_patch", tool_input={"command": patch_text}))
        reduced = reduce_hook_payload(p, repo)
        assert [(t["path"], t["change_type"]) for t in reduced.file_targets] == [("pkg/a.py", "create"), ("README.md", "delete")]
        assert reduced.file_dropped is True  # the two paths outside the repository were dropped entirely
        assert "\\" not in json.dumps(reduced.to_dict()) and "someone" not in json.dumps(reduced.to_dict())
        if sys.platform == "win32":
            rel = cx.extract_codex_payload(_doc(
                "PostToolUse", repo, tool_name="apply_patch",
                tool_input={"command": "*** Begin Patch\n*** Update File: src\\app.py\n*** End Patch\n"},
            ))
            assert [t["path"] for t in reduce_hook_payload(rel, repo).file_targets] == ["src/app.py"]

    def test_other_tools_are_name_only(self, repo):
        p = cx.extract_codex_payload(_doc("PostToolUse", repo, tool_name="mcp__fs__read_file",
                                          tool_input={"path": "/etc/passwd"}))
        assert p.tool_kind == "other" and p.tool_name == "mcp__fs__read_file"
        assert p.file_path is None and p.command is None and p.file_paths == []

    def test_reduced_payload_carries_no_raw_text(self, repo):
        p = cx.extract_codex_payload(_doc(
            "PostToolUse", repo, tool_name="apply_patch", tool_input={"patch": PATCH},
            tool_response={"output": SECRET},
        ))
        reduced = reduce_hook_payload(p, repo)
        blob = json.dumps(reduced.to_dict())
        assert SECRET not in blob and TRANSCRIPT not in blob and "def add" not in blob
        assert reduced.agent == "codex" and reduced.model_id == "gpt-5-codex"
        assert [t["path"] for t in reduced.file_targets] == ["calc.py", "README.md", "old.txt"]
        assert reduced.file_target == "calc.py"
        outside = cx.extract_codex_payload(_doc(
            "PostToolUse", repo, tool_name="apply_patch",
            tool_input={"patch": "*** Begin Patch\n*** Update File: /etc/hosts\n*** End Patch\n"},
        ))
        r2 = reduce_hook_payload(outside, repo)
        assert r2.file_targets == [] and r2.file_dropped is True


# ---------------------------------------------------------------------------
# Payload -> canonical Events / record / receipt (inline path)
# ---------------------------------------------------------------------------


class TestCanonicalRecord:
    def test_session_becomes_one_codex_shard(self, repo):
        _drive_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "codex_hooks"
        assert entry["import_source"] == "codex"
        assert entry["execution_model"] == "gpt-5-codex"
        cap = entry["capture"]
        assert cap["source"] == "codex_hooks" and cap["agent"] == "codex"
        assert cap["agent_vendor"] == "OpenAI" and cap["provider"] is None
        assert cap["model_source"] == "codex_hook"
        assert cap["session_id"] == SID and cap["session_end_observed"] is True
        assert cap["prompt_count"] == 1 and cap["tool_call_count"] == 3 and cap["turn_count"] == 1
        assert cap["task_status"] == "turn_completed"
        assert entry["task"].startswith("Add a calculator module")
        assert SECRET not in json.dumps(entry) and TRANSCRIPT not in json.dumps(entry)
        assert "All done" not in json.dumps(entry)
        # Evidence fails closed: nothing Codex does not expose is invented.
        assert entry["verification_attempted"] is False and entry["verification_passed"] is None
        for key in ("estimated_cost", "cost_provenance", "prompt_tokens", "tokens_provenance"):
            assert key not in entry
        assert "duration_seconds" in entry
        paths = {f["path"] for f in entry["files_detail"]}
        assert "calc.py" in paths  # git-observed (untracked file created in the repo)
        assert entry["files_source"] == "git_diff_inferred"

    def test_events_carry_codex_identity_and_evidence(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        events = events_from_entry(entry)
        assert events and all(e.source == SOURCE_CODEX_HOOKS for e in events)
        assert all(e.actor == "codex" for e in events)
        types = [e.event_type for e in events]
        assert "session.started" in types and "run.completed" in types and "tool.invoked" in types
        tools = [e for e in events if e.event_type == "tool.invoked"]
        assert {e.metadata.get("tool") for e in tools} == {"apply_patch", "Bash", "mcp__github__create_issue"}
        patch_ev = next(e for e in tools if e.metadata.get("tool") == "apply_patch")
        assert patch_ev.evidence == "agent_reported" and patch_ev.target == "calc.py"
        assert patch_ev.metadata.get("file_count") == 3
        assert patch_ev.status == "unknown"  # Codex gives no success signal for apply_patch
        assert not [e for e in tools if e.status == "passed"]
        bash_ev = next(e for e in tools if e.metadata.get("tool") == "Bash")
        assert bash_ev.action.startswith("Bash: ") and bash_ev.metadata.get("command_kind") == "test"
        assert bash_ev.status == "unknown"  # a test command is never a verification result
        assert not [e for e in events if e.event_type.startswith("verification.")]
        started = next(e for e in events if e.event_type == "session.started")
        assert "Codex session observed" in started.action and started.evidence == "directly_observed"

    def test_receipt_identity(self, repo):
        _drive_inline(repo)
        receipt = build_shard_receipt(_lines(repo)[0])
        assert receipt.agent == "Codex (external)"
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED
        assert receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.tokens_input is None and receipt.cost_provenance is None
        text = render_compact_shard_receipt(receipt)
        assert "Codex (external)" in text and "did not execute or verify" in text
        assert SECRET not in text
        shards = list_shards(repo_path=repo)
        assert len(shards) == 1 and shards[0].agent == "Codex (external)"
        assert get_receipt(shards[0].shard_id, repo_path=repo).agent == "Codex (external)"

    def test_interrupt_is_activity_not_completion(self, repo):
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="do a thing")
        _run(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "echo hi"})
        outcome = _run(repo, "Interrupt")
        assert outcome.action in ("record_updated", "record_created")
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["task_status"] == "in_progress"
        actions = [e["action"] for e in entry["events"]]
        assert any("interrupted" in a for a in actions)

    def test_apply_patch_is_never_file_success_evidence(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        # git-observed evidence stands on its own; no hook-reported claim joins it.
        assert entry["files_source"] == "git_diff_inferred"
        assert any(f["path"] == "calc.py" for f in entry["files_detail"])
        assert not [f for f in entry["files_detail"] if str(f.get("summary", "")).startswith("reported by")]
        assert all(e["evidence"] == "git_observed" for e in entry["events"] if e["event_type"] == "file.changed")

    def test_no_hook_reported_files_when_git_unavailable(self, tmp_path):
        root = tmp_path / "plain"
        root.mkdir()
        with patch("openshard.adapters.claude_mcp_install.find_repo_root", return_value=None), \
             patch("openshard.adapters.claude_code_import.subprocess.run",
                   side_effect=FileNotFoundError("no git")):
            _run(root, "UserPromptSubmit", prompt="task")
            _run(root, "PostToolUse", tool_name="apply_patch",
                 tool_input={"command": "*** Begin Patch\n*** Add File: made.py\n+x\n*** End Patch\n"})
            _run(root, "Stop")
        entry = _lines(root)[0]
        # Unlike Claude Code (whose PostToolUse attests success), Codex's
        # apply_patch claim never becomes a file record without git evidence.
        assert entry["files_source"] == "not_available" and entry["files_detail"] == []
        assert not [e for e in entry["events"] if e["event_type"] == "file.changed"]
        tool = next(e for e in entry["events"] if e["event_type"] == "tool.invoked")
        assert tool["status"] == "unknown" and tool["target"] == "made.py"

    def test_model_missing_stays_unknown(self, repo):
        doc = _doc("UserPromptSubmit", repo, prompt="x")
        del doc["model"]
        handle_hook(doc, env={}, agent="codex")
        handle_hook({k: v for k, v in _doc("Stop", repo).items() if k != "model"}, env={}, agent="codex")
        entry = _lines(repo)[0]
        assert entry["execution_model"] == "unknown"
        assert entry["capture"]["model_source"] == "not_captured"

    def test_repositories_are_isolated(self, tmp_path):
        a = _make_repo(tmp_path / "a")
        b = _make_repo(tmp_path / "b")
        _drive_inline(a)
        _run(b, "UserPromptSubmit", prompt="other repo")
        _run(b, "Stop")
        assert len(_lines(a)) == 1 and len(_lines(b)) == 1
        assert _lines(b)[0]["task"] == "other repo"
        assert not (tmp_path / ".openshard").exists()

    def test_same_session_id_as_a_claude_session_is_a_separate_shard(self, repo):
        from openshard.adapters.claude_hooks import handle_claude_hook

        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        claude_doc = {"session_id": SID, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "claude"}
        handle_claude_hook(claude_doc, env=env)
        handle_claude_hook({**claude_doc, "hook_event_name": "Stop"}, env=env)
        _run(repo, "UserPromptSubmit", prompt="codex")
        _run(repo, "Stop")
        lines = _lines(repo)
        assert {e["executor"] for e in lines} == {"claude_code_hooks", "codex_hooks"}
        assert len({e["shard_id"] for e in lines}) == 2


# ---------------------------------------------------------------------------
# Service path: POST /hooks/codex
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
    return client.post_hook(port, json.dumps(doc).encode("utf-8"), hook_path=client.CODEX_HOOK_PATH)


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
        assert _post(service.port, _doc("SessionStart", via_http, source="startup"))
        assert _post(service.port, _doc("UserPromptSubmit", via_http, prompt=f"Add a calculator module; token {SECRET}"))
        (via_http / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert _post(service.port, _doc("PostToolUse", via_http, tool_name="apply_patch", tool_use_id="call_1",
                                        tool_input={"patch": PATCH}, tool_response={"output": f"Done {SECRET}"}))
        assert _post(service.port, _doc("PostToolUse", via_http, tool_name="Bash", tool_use_id="call_2",
                                        tool_input={"command": ["bash", "-lc", "python -m pytest -q"]},
                                        tool_response={"stdout": "3 passed", "exit_code": 0}))
        assert _post(service.port, _doc("PostToolUse", via_http, tool_name="mcp__github__create_issue",
                                        tool_use_id="call_3", tool_input={"title": "x"}, tool_response={"ok": True}))
        assert _post(service.port, _doc("Stop", via_http, last_assistant_message="All done"))
        assert _post(service.port, _doc("SessionEnd", via_http))
        _drive_inline(via_inline)
        assert _wait_for(lambda: bool(_lines(via_http)) and _lines(via_http)[0]["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        assert _stable(_lines(via_http)[0]) == _stable(_lines(via_inline)[0])
        assert SECRET not in (via_http / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")

    def test_queue_line_is_reduced_and_agent_tagged(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, _doc("PostToolUse", repo, tool_name="apply_patch",
                                        tool_input={"patch": PATCH}, tool_response={"output": SECRET}))
        # Queue files are agent-scoped so a Claude session with the same id never shares a replay.
        queue_file = repo / ".openshard" / "claude_sessions" / f"codex.{SID}{svc.QUEUE_SUFFIX}"
        assert not (repo / ".openshard" / "claude_sessions" / f"{SID}{svc.QUEUE_SUFFIX}").exists()
        line = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
        assert line["kind"] == "hook" and line["data"]["agent"] == "codex"
        assert line["data"]["model_id"] == "gpt-5-codex" and line["data"]["tool_success"] is None
        text = queue_file.read_text(encoding="utf-8")
        assert SECRET not in text and TRANSCRIPT not in text and "def add" not in text
        service.server.recorder.resume_processing()

    def test_blocking_path_stays_within_budget(self, service, repo):
        assert _post(service.port, _doc("SessionStart", repo, source="startup"))
        assert _post(service.port, _doc("UserPromptSubmit", repo, prompt="warm"))
        roundtrips: list[float] = []
        for i in range(40):
            doc = _doc("PostToolUse", repo, tool_name="Bash", tool_input={"command": f"echo {i}"},
                       tool_response={"stdout": f"{i}\n"})
            t0 = time.perf_counter()
            assert _post(service.port, doc)
            roundtrips.append(time.perf_counter() - t0)
        # The blocking-path timings are complete once the POSTs returned; the
        # background worker's own drain time is not under test here (on a
        # contended box it can be seconds), so it is only given a chance to
        # finish, never asserted on.
        service.server.recorder.wait_idle(60)
        timing = client.health(service.port)["blocking_ms"]
        # Windows loopback/TCP-stack overhead on shared CI runners is
        # substantially higher and noisier than Linux/macOS, so it gets a
        # looser, still-meaningful budget (see test_opencode_capture.py's
        # counterpart for the same reasoning and observed numbers).
        p50_budget, p95_budget = (60, 120) if sys.platform == "win32" else (25, 50)
        assert timing["p50_ms"] < p50_budget, timing
        assert timing["p95_ms"] < p95_budget, timing
        roundtrips.sort()
        assert roundtrips[len(roundtrips) // 2] < 0.05, roundtrips

    def test_unsupported_codex_event_is_ignored_not_errored(self, service, repo):
        status, reply = client._request("POST", service.port, client.CODEX_HOOK_PATH,
                                        json.dumps(_doc("PreToolUse", repo)).encode())
        assert status == 200 and reply == b"{}"
        assert client.health(service.port)["stats"]["queued"] == 0

    def test_hook_command_forwards_and_never_imports_fold_code(self, service, repo, capture_env):
        code = (
            "import sys, os; from openshard.adapters.claude_capture_client import run_hook_via_service; "
            "print(run_hook_via_service(sys.stdin, env=dict(os.environ), agent='codex')); "
            "print(sorted(m for m in sys.modules if m.startswith('openshard')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc("UserPromptSubmit", repo, prompt="via cli")),
            capture_output=True, text=True, timeout=60, env=capture_env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[0] == "forwarded"
        assert "openshard.adapters.claude_hooks" not in result.stdout
        assert "openshard.adapters.codex_hooks" not in result.stdout
        assert _wait_for(lambda: bool(_lines(repo)))
        assert _lines(repo)[0]["executor"] == "codex_hooks"

    def test_no_spawn_falls_back_inline(self, capture_env, repo):
        import io

        stream = io.BytesIO(json.dumps(_doc("UserPromptSubmit", repo, prompt="fallback")).encode())
        label = client.run_hook_via_service(stream, env=capture_env, agent="codex", spawn=False)
        assert label == "record_created"
        assert _lines(repo)[0]["executor"] == "codex_hooks"


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class TestInstaller:
    def test_fresh_install_writes_all_events_and_excludes_file_from_git(self, repo):
        result = install_codex_hooks(repo_root=repo)
        assert result.status == "installed", result.message
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert set(data["hooks"]) == set(HOOK_EVENTS)
        assert data["hooks"] == build_hook_config()
        for event, groups in data["hooks"].items():
            hook = groups[0]["hooks"][0]
            assert hook["type"] == "command" and hook["command"].startswith(HOOK_COMMAND)
            assert hook["timeout"] <= (3 if event in ("SessionEnd", "Interrupt") else 15)
        assert data["hooks"]["PostToolUse"][0]["hooks"][0]["async"] is True
        assert "--no-spawn" in data["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        assert "--no-spawn" not in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert HOOKS_RELPATH.as_posix() in exclude
        check = subprocess.run(["git", "check-ignore", "-q", HOOKS_RELPATH.as_posix()], cwd=repo)
        assert check.returncode == 0

    def test_idempotent(self, repo):
        assert install_codex_hooks(repo_root=repo).status == "installed"
        before = (repo / HOOKS_RELPATH).read_bytes()
        again = install_codex_hooks(repo_root=repo)
        assert again.status == "already_installed"
        assert all(v == "unchanged" for v in again.events.values())
        assert (repo / HOOKS_RELPATH).read_bytes() == before

    def test_preserves_unrelated_hooks_and_keys(self, repo):
        (repo / ".codex").mkdir()
        existing = {
            "description": "team policy",
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 policy.py"}]}],
                "PostToolUse": [{"matcher": "apply_patch", "hooks": [{"type": "command", "command": "fmt.sh"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 99}]}],
            },
            "custom": {"keep": True},
        }
        (repo / HOOKS_RELPATH).write_text(json.dumps(existing), encoding="utf-8")
        result = install_codex_hooks(repo_root=repo)
        assert result.status == "updated"
        assert result.events["Stop"] == "updated" and result.events["SessionStart"] == "added"
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert data["description"] == "team policy" and data["custom"] == {"keep": True}
        assert data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
        # The user's PostToolUse group (different matcher) is untouched; ours is a separate group.
        assert data["hooks"]["PostToolUse"][0] == existing["hooks"]["PostToolUse"][0]
        assert any(is_openshard_codex_hook(h) for g in data["hooks"]["PostToolUse"] for h in g["hooks"])
        assert data["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 5
        # A pre-existing file is not force-excluded from git.
        assert not (repo / ".git" / "info" / "exclude").exists() or \
            HOOKS_RELPATH.as_posix() not in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    def test_malformed_config_is_left_alone(self, repo):
        (repo / ".codex").mkdir()
        (repo / HOOKS_RELPATH).write_text("{ not json", encoding="utf-8")
        result = install_codex_hooks(repo_root=repo)
        assert result.status == "error" and "not valid JSON" in result.message
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == "{ not json"
        (repo / HOOKS_RELPATH).write_text(json.dumps({"hooks": []}), encoding="utf-8")
        result = install_codex_hooks(repo_root=repo)
        assert result.status == "error" and "unexpected hooks layout" in result.message
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == json.dumps({"hooks": []})
        # Nothing of ours can be in an unparseable layout; uninstall leaves it alone too.
        assert uninstall_codex_hooks(repo_root=repo).status == "not_installed"
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == json.dumps({"hooks": []})

    def test_uninstall_removes_only_ours(self, repo):
        (repo / ".codex").mkdir()
        (repo / HOOKS_RELPATH).write_text(json.dumps({
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "python3 policy.py"}]}]},
        }), encoding="utf-8")
        install_codex_hooks(repo_root=repo)
        result = uninstall_codex_hooks(repo_root=repo)
        assert result.status == "removed"
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert data["hooks"]["PreToolUse"] == [{"hooks": [{"type": "command", "command": "python3 policy.py"}]}]
        assert all(not groups for ev, groups in data["hooks"].items() if ev != "PreToolUse")
        assert uninstall_codex_hooks(repo_root=repo).status == "not_installed"
        _drive_inline(repo)
        uninstall_codex_hooks(repo_root=repo)
        assert len(_lines(repo)) == 1  # history is never touched


# ---------------------------------------------------------------------------
# CLI: hooks codex, capture install/uninstall codex, setup, doctor
# ---------------------------------------------------------------------------


def _which(name: str):
    return {"codex": "/usr/local/bin/codex", "openshard": "/usr/local/bin/openshard"}.get(name)


class TestCli:
    def test_hooks_codex_command_records_inline(self, repo):
        runner = CliRunner()
        doc = json.dumps(_doc("UserPromptSubmit", repo, prompt="cli prompt"))
        result = runner.invoke(cli, ["hooks", "codex"], input=doc)
        assert result.exit_code == 0, result.output
        assert result.output == ""  # hook stdout is a Codex decision channel: stay silent
        result = runner.invoke(cli, ["hooks", "codex", "--no-spawn"], input=json.dumps(_doc("Stop", repo)))
        assert result.exit_code == 0
        assert _lines(repo)[0]["executor"] == "codex_hooks"
        assert _lines(repo)[0]["capture"]["turn_count"] == 1

    def test_entrypoint_fast_path_parses_codex_argv(self):
        from openshard.cli.entrypoint import _parse_hooks_codex_argv

        assert _parse_hooks_codex_argv([]) == (None, True)
        assert _parse_hooks_codex_argv(["--no-spawn"]) == (None, False)
        assert _parse_hooks_codex_argv(["--event", "Stop", "--no-spawn"]) == ("Stop", False)
        assert _parse_hooks_codex_argv(["--event=Stop"]) == ("Stop", True)
        assert _parse_hooks_codex_argv(["--help"]) is None
        assert _parse_hooks_codex_argv(["--event"]) is None

    def test_capture_install_and_uninstall_codex(self, repo):
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "codex", "--repo-path", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "installed" and data["configured"] is True
        assert any("review" in s for s in data["next_steps"])
        assert (repo / HOOKS_RELPATH).exists()
        result = runner.invoke(cli, ["capture", "install", "codex", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "already installed" in result.output
        result = runner.invoke(cli, ["capture", "uninstall", "codex", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "removed" in result.output
        assert json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))["hooks"]["Stop"] == []

    def test_setup_configures_codex_without_claude(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Codex alone is a complete integration: a missing Claude Code CLI is
        # reported as a skipped agent, never as an OpenShard readiness failure.
        assert data["readiness"] == "ready"
        assert any("Claude Code CLI not found" in s for s in data["next_steps"])
        assert data["agents"]["codex"]["status"] == "installed"
        assert data["agents"]["opencode"]["status"] == "skipped"
        assert data["configured_agents"] == ["codex"]
        assert data["mcp"]["status"] == "skipped"
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--repo-path", str(repo), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Codex:" in result.output and "Use Codex normally" in result.output

    def test_setup_without_any_agent_is_not_ready(self, repo):
        runner = CliRunner()
        with patch("shutil.which", return_value=None):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["readiness"] == "not_ready"
        assert any("Codex" in s for s in data["next_steps"])

    def test_doctor_reports_codex_independently(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            before = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            install_codex_hooks(repo_root=repo)
            after = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert before.exit_code == 0 and after.exit_code == 0, after.output
        assert json.loads(before.output)["codex"]["configured"] is False
        codex = json.loads(after.output)["codex"]
        assert codex["configured"] is True and codex["cli_available"] is True
        assert codex["events_missing"] == []
        assert json.loads(after.output)["claude_code"]["claude_cli_available"] is False
        assert "Codex" in human.output and "OpenCode" in human.output
        assert "Ready" in human.output and "use Codex normally" in human.output
        assert human.output.index("Claude Code\n") < human.output.index("\nCodex\n")

    def test_setup_agent_snapshot_includes_codex(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--agent", "--json", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["codex"]["cli_available"] is True and data["codex"]["configured"] is False
        assert data["opencode"]["cli_available"] is False
        assert not (repo / HOOKS_RELPATH).exists()  # --agent is read-only
