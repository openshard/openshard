"""Tests for Hermes Agent capture (0.4.7): translator, installer, service path, CLI.

Every test drives the adapter with synthetic Hermes shell-hook documents in a
throw-away git repository. No real Hermes is ever run, and ``HERMES_HOME`` is
pinned to a temporary directory so the user's real ``~/.hermes`` is never read
or written. Documents follow the shape Hermes' shell-hook runner writes to a
hook's stdin: ``{hook_event_name, tool_name, tool_input, session_id, cwd,
profile, extra}`` with every event-specific kwarg under ``extra``.
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
import yaml
from click.testing import CliRunner

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import hermes_hooks as hm
from openshard.adapters.agent_setup import detect_hermes_integration
from openshard.adapters.capture_agents import profile_for
from openshard.adapters.claude_hooks import (
    HookPayload,
    StatusPayload,
    extract_agent_payload,
    handle_hook,
    reduce_hook_payload,
    resolve_repo_root,
)
from openshard.adapters.hermes_hooks_install import (
    BACKUP_SUFFIX,
    BLOCK_BEGIN,
    HOOK_COMMAND,
    HOOK_EVENTS,
    allowlist_path,
    approved_hermes_events,
    config_path,
    hermes_home,
    install_hermes_hooks,
    installed_hermes_events,
    load_hermes_config,
    uninstall_hermes_hooks,
)
from openshard.cli.main import cli
from openshard.history.event import SOURCE_HERMES_HOOKS, events_from_entry
from openshard.history.query import get_receipt, list_shards
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)

SID = "20260923_101500_a1b2c3"
SID2 = "20260923_111500_ffffff"
CHILD_SID = "20260923_101530_c0ffee"
SECRET = "sk-proj-SECRETSECRET12345678901234567890abcdef"
FILE_BODY = "def add(a, b):\n    return a + b  # RAW FILE CONTENT"
HISTORY = [{"role": "user", "content": "RAW TRANSCRIPT TEXT " + SECRET}]
MODEL = "claude-sonnet-4-6"
PROVIDER = "anthropic"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(root: Path, *, opted_in: bool = True) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    if opted_in:
        (root / ".openshard").mkdir()
    return root


@pytest.fixture(autouse=True)
def hermes_env(tmp_path, monkeypatch) -> Path:
    """Pin the Hermes home so no test can touch the real ~/.hermes."""
    home = tmp_path / "hermes home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "hermes repo")


def _doc(repo: Path, event: str, sid: str | None = SID, *, tool: str | None = None,
         args: dict | None = None, **extra) -> dict:
    """A Hermes shell-hook stdin document."""
    return {
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": args,
        "session_id": sid if sid is not None else "",
        "cwd": str(repo),
        "profile": "default",
        "extra": extra,
    }


def _tool(repo: Path, name: str, args: dict, sid: str = SID, **extra) -> dict:
    extra.setdefault("status", "ok")
    extra.setdefault("result", json.dumps({"output": "RAW TOOL RESULT " + SECRET}))
    extra.setdefault("task_id", sid)
    return _doc(repo, "post_tool_call", sid, tool=name, args=args, **extra)


def _run(doc: dict):
    return handle_hook(doc, env={}, agent="hermes")


def _usage(repo: Path, n: int, sid: str = SID, **usage) -> dict:
    return _doc(
        repo, "post_api_request", sid, model=MODEL, provider=PROVIDER, api_request_id=f"turn1:api:{n}",
        api_call_count=n, usage=usage or {"input_tokens": 100 * n, "output_tokens": 10 * n,
                                          "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7},
        response={"content": "RAW RESPONSE " + SECRET},
    )


def _write_calc(repo: Path) -> None:
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def _session_steps(repo: Path, sid: str = SID) -> list[tuple[dict, object]]:
    """A whole Hermes session as ``(document, action-to-run-before-sending-it)`` steps."""
    def step(doc, before=None):
        return doc, before

    return [
        step(_doc(repo, "on_session_start", sid, model=MODEL, platform="cli")),
        step(_doc(repo, "pre_llm_call", sid, user_message=f"Add a calc module (key {SECRET})", model=MODEL,
                  conversation_history=HISTORY, is_first_turn=True, platform="cli")),
        step(_usage(repo, 1, sid)),
        step(_tool(repo, "read_file", {"path": "README.md"}, sid, duration_ms=4, tool_call_id="tc-1",
                   turn_id="turn1")),
        step(_tool(repo, "write_file", {"path": str(repo / "calc.py"), "content": FILE_BODY + SECRET}, sid,
                   duration_ms=12, tool_call_id="tc-2", turn_id="turn1"), lambda: _write_calc(repo)),
        step(_tool(repo, "terminal", {"command": "python -m pytest -q"}, sid, duration_ms=850,
                   tool_call_id="tc-3", turn_id="turn1")),
        step(_tool(repo, "terminal", {"command": f"git status {SECRET}"}, sid, status="error",
                   error_type="tool_error", error_message=f"exit 1 {SECRET}")),
        step(_tool(repo, "web_search", {"query": SECRET}, sid)),
        step(_doc(repo, "subagent_start", sid, parent_session_id=sid, parent_turn_id="turn1",
                  child_session_id=CHILD_SID, child_subagent_id="sa-1", child_role="leaf",
                  child_goal=f"RAW CHILD GOAL {SECRET}")),
        step(_doc(repo, "subagent_stop", sid, parent_session_id=sid, child_role="leaf", child_status="completed",
                  child_summary=f"RAW CHILD SUMMARY {SECRET}", duration_ms=4200,
                  tool_call_history=[{"tool_name": "terminal"}, {"tool_name": "read_file"}])),
        step(_doc(repo, "pre_approval_request", sid, command=f"rm -rf build {SECRET}",
                  description="recursive delete", pattern_key="rm_rf", pattern_keys=["rm_rf"], session_key="sk",
                  surface="cli", turn_id="turn1", tool_call_id="tc-5")),
        step(_doc(repo, "post_approval_response", sid, command=f"rm -rf build {SECRET}",
                  description="recursive delete", pattern_key="rm_rf", pattern_keys=["rm_rf"], session_key="sk",
                  surface="cli", turn_id="turn1", tool_call_id="tc-5", choice="once")),
        step(_usage(repo, 2, sid)),
        step(_doc(repo, "on_session_end", sid, completed=True, interrupted=False, failed=False, model=MODEL,
                  platform="cli", turn_id="turn1")),
        step(_doc(repo, "on_session_finalize", sid, platform="cli", reason="cli_exit")),
    ]


def _drive_inline(repo: Path, sid: str = SID) -> None:
    for doc, before in _session_steps(repo, sid):
        if before:
            before()  # type: ignore[operator]
        _run(doc)


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
    def test_session_and_prompt_events(self, repo):
        start = hm.extract_hermes_payload(_doc(repo, "on_session_start", model=MODEL))
        assert isinstance(start, HookPayload)
        assert (start.event, start.session_id, start.source, start.agent) == ("SessionStart", SID, "startup", "hermes")
        assert start.model_id == MODEL and start.cwd == str(repo)
        prompt = hm.extract_hermes_payload(_doc(repo, "pre_llm_call", user_message="Fix the bug", model=MODEL,
                                                conversation_history=HISTORY))
        assert isinstance(prompt, HookPayload)
        assert prompt.event == "UserPromptSubmit" and prompt.prompt == "Fix the bug"
        multimodal = hm.extract_hermes_payload(_doc(repo, "pre_llm_call", user_message=[
            {"type": "text", "text": "Describe"}, {"type": "image_url", "image_url": {"url": "data:x"}},
            {"type": "text", "text": "this"}]))
        assert isinstance(multimodal, HookPayload) and multimodal.prompt == "Describe\nthis"
        assert not hasattr(prompt, "conversation_history")

    def test_turn_end_is_completed_interrupted_or_neutral_never_a_session_end(self, repo):
        def event(**extra):
            payload = hm.extract_hermes_payload(_doc(repo, "on_session_end", **extra))
            assert isinstance(payload, HookPayload)
            return payload.event
        assert event(completed=True, interrupted=False) == "Stop"
        assert event(completed=False, interrupted=True) == "Interrupt"
        assert event(completed=True, interrupted=True) == "Interrupt"
        assert event(completed=False, interrupted=False, failed=True) == "SessionIdle"
        assert event() == "SessionIdle"  # reduced exit-path payload: nothing is confirmed
        end = hm.extract_hermes_payload(_doc(repo, "on_session_finalize", reason="cli_exit"))
        assert isinstance(end, HookPayload) and end.event == "SessionEnd" and end.reason == "cli_exit"

    def test_tool_status_is_the_only_success_signal(self, repo):
        def tool(status: str | None, name: str = "write_file"):
            extra = {} if status is None else {"status": status}
            payload = hm.extract_hermes_payload(_doc(repo, "post_tool_call", tool=name,
                                                     args={"path": "a.py", "content": "x"}, **extra))
            assert isinstance(payload, HookPayload)
            return payload
        ok = tool("ok")
        assert ok.event == "PostToolUse" and ok.tool_success is True and ok.file_paths == [("a.py", "update")]
        for failed in ("error", "blocked"):
            payload = tool(failed)
            assert payload.event == "PostToolUseFailure" and payload.tool_success is None
        for unknown in (None, "weird"):
            payload = tool(unknown)
            assert payload.event == "PostToolUse" and payload.tool_success is None
        # ``ok`` on a non-file tool attests nothing about files.
        assert tool("ok", "web_search").tool_success is None

    def test_tool_classification_and_arguments_read(self, repo):
        def payload(name, args, **extra):
            result = hm.extract_hermes_payload(_doc(repo, "post_tool_call", tool=name, args=args, **extra))
            assert isinstance(result, HookPayload)
            return result
        cmd = payload("terminal", {"command": "pytest -q", "background": False, "workdir": "/x"})
        assert cmd.tool_kind == "command" and cmd.command == "pytest -q" and cmd.file_path is None
        read = payload("read_file", {"path": "README.md", "offset": 1})
        assert read.tool_kind == "read" and read.file_path == "README.md"
        replace = payload("patch", {"mode": "replace", "path": "a.py", "old_string": "SECRETOLD",
                                    "new_string": "SECRETNEW"})
        assert replace.tool_kind == "file" and replace.file_paths == [("a.py", "update")]
        v4a = payload("patch", {"mode": "patch", "patch": (
            "*** Begin Patch\n*** Update File: src/a.py\n@@ ctx @@\n-old SECRETHUNK\n+new\n"
            "*** Add File: src/b.py\n+content\n*** Delete File: old.py\n"
            "*** Move File: c.py -> d.py\n*** End Patch")})
        assert v4a.file_paths == [("src/a.py", "update"), ("src/b.py", "create"), ("old.py", "delete"),
                                  ("c.py", "delete"), ("d.py", "create")]
        for name in ("search_files", "execute_code", "delegate_task", "browser_click", "mcp_github_issue"):
            other = payload(name, {"query": "SECRETQ", "code": "SECRETCODE"})
            assert other.tool_kind == "other" and other.command is None and other.file_paths == []
        assert payload("terminal", None).command is None  # malformed shapes under-report, never raise
        assert payload("write_file", {"content": "x"}).file_paths == []

    def test_correlation_and_duration_attrs(self, repo):
        payload = hm.extract_hermes_payload(_tool(repo, "terminal", {"command": "ls"}, duration_ms=1234.9,
                                                  tool_call_id="tc-9", turn_id="turn7", api_request_id="x"))
        assert isinstance(payload, HookPayload)
        assert payload.attrs == {"duration_ms": 1234, "tool_status": "ok", "tool_call_id": "tc-9",
                                 "turn_id": "turn7"}
        bad = hm.extract_hermes_payload(_tool(repo, "terminal", {"command": "ls"}, duration_ms=True,
                                              tool_call_id=["a"], status="ok"))
        assert isinstance(bad, HookPayload) and bad.attrs == {"tool_status": "ok"}

    def test_usage_observation(self, repo):
        payload = hm.extract_hermes_payload(_usage(repo, 3))
        assert isinstance(payload, StatusPayload)
        assert payload.agent == "hermes" and payload.session_id == SID
        assert (payload.model_id, payload.provider_id) == (MODEL, PROVIDER)
        assert (payload.tokens_input, payload.tokens_output) == (300, 30)
        assert (payload.tokens_cache_creation, payload.tokens_cache_read) == (5, 7)
        assert payload.cost_total_usd is None  # Hermes reports no cost
        assert payload.usage_key == "turn1:api:3"
        # No usage dict / no request id: model and provider only, never invented tokens.
        no_usage = hm.extract_hermes_payload(_doc(repo, "post_api_request", model=MODEL, provider=PROVIDER,
                                                  api_request_id="t:api:1", api_call_count=1))
        assert isinstance(no_usage, StatusPayload)
        assert no_usage.tokens_input is None and no_usage.usage_key is None and no_usage.model_id == MODEL
        no_id = hm.extract_hermes_payload(_doc(repo, "post_api_request", model=MODEL,
                                               usage={"input_tokens": 9, "output_tokens": 1}))
        assert isinstance(no_id, StatusPayload) and no_id.usage_key is None and no_id.tokens_input is None
        long_id = hm.extract_hermes_payload(_doc(repo, "post_api_request", api_request_id="t" * 200,
                                                 api_call_count=1, usage={"input_tokens": 1}))
        assert isinstance(long_id, StatusPayload) and long_id.usage_key is not None
        assert len(long_id.usage_key) <= 80

    def test_subagent_and_approval_attrs(self, repo):
        start = hm.extract_hermes_payload(_doc(
            repo, "subagent_start", parent_session_id=SID, parent_subagent_id="sa-0", child_session_id=CHILD_SID,
            child_subagent_id="sa-1", child_role="orchestrator", child_goal="RAW GOAL"))
        assert isinstance(start, HookPayload) and start.event == "SubagentStart"
        assert start.attrs == {"child_role": "orchestrator", "child_subagent_id": "sa-1",
                               "child_session_id": CHILD_SID, "parent_subagent_id": "sa-0"}
        assert "RAW GOAL" not in json.dumps(start.attrs)
        stop = hm.extract_hermes_payload(_doc(
            repo, "subagent_stop", child_status="failed", duration_ms=10, child_summary="RAW SUMMARY",
            tool_call_history=[{"tool_name": "a"}, {"tool_name": "b"}, {"tool_name": "c"}]))
        assert isinstance(stop, HookPayload) and stop.event == "SubagentStop"
        assert stop.attrs == {"child_status": "failed", "duration_ms": 10, "tool_calls": 3}
        request = hm.extract_hermes_payload(_doc(repo, "pre_approval_request", command="rm -rf x", surface="gateway",
                                                 pattern_key="rm_rf", choice="ignored"))
        assert isinstance(request, HookPayload) and request.event == "ApprovalRequest"
        assert request.command == "rm -rf x" and "choice" not in request.attrs
        decision = hm.extract_hermes_payload(_doc(repo, "post_approval_response", command="x", choice="smart_deny",
                                                  decided_by="aux_llm", surface="smart"))
        assert isinstance(decision, HookPayload) and decision.event == "ApprovalDecision"
        assert decision.attrs["choice"] == "smart_deny" and decision.attrs["decided_by"] == "aux_llm"

    def test_unsubscribed_or_unknown_events_are_ignored(self, repo):
        for event in ("pre_tool_call", "post_llm_call", "on_stream_delta", "pre_verify", "on_session_reset",
                      "transform_tool_result", "pre_api_request", "nonsense"):
            assert hm.extract_hermes_payload(_doc(repo, event, tool="terminal", args={"command": "ls"})) is None
        assert hm.extract_hermes_payload({"tool_name": "terminal"}) is None
        assert "pre_tool_call" not in HOOK_EVENTS  # observation only: never a control hook

    def test_event_name_fallback_and_agent_is_fixed_by_the_receiver(self, repo):
        doc = _doc(repo, "on_session_start")
        del doc["hook_event_name"]
        assert hm.extract_hermes_payload(doc) is None
        fallback = hm.extract_hermes_payload(doc, event_override="on_session_start")
        assert isinstance(fallback, HookPayload) and fallback.event == "SessionStart"
        doc = _doc(repo, "on_session_start", agent="claude_code")
        doc["agent"] = "claude_code"
        assert extract_agent_payload(doc, agent="hermes").agent == "hermes"  # type: ignore[union-attr]

    def test_invalid_session_id_and_malformed_documents(self, repo):
        payload = hm.extract_hermes_payload(_doc(repo, "on_session_start", "bad id/../x"))
        assert isinstance(payload, HookPayload) and payload.session_id is None
        assert reduce_hook_payload(payload, repo) is None
        empty = hm.extract_hermes_payload(_doc(repo, "on_session_finalize", None))
        assert isinstance(empty, HookPayload) and empty.session_id is None
        odd = hm.extract_hermes_payload({"hook_event_name": "post_tool_call", "tool_name": 5, "tool_input": [],
                                         "extra": "x", "session_id": SID})
        assert isinstance(odd, HookPayload) and odd.tool_name is None and odd.event == "PostToolUse"

    def test_profile(self):
        profile = profile_for("hermes")
        assert profile.key == "hermes" and profile.executor == "hermes_hooks" and profile.vendor == "Nous Research"
        assert profile.opt_in_repo is True
        assert profile_for("codex").opt_in_repo is False


# ---------------------------------------------------------------------------
# Canonical record (shared fold, driven inline)
# ---------------------------------------------------------------------------


class TestCanonicalRecord:
    def test_session_becomes_one_hermes_shard(self, repo):
        _drive_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "hermes_hooks" and entry["import_source"] == "hermes"
        cap = entry["capture"]
        assert cap["source"] == "hermes_hooks" and cap["agent"] == "hermes" and cap["agent_vendor"] == "Nous Research"
        assert cap["session_id"] == SID
        assert cap["provider"] == PROVIDER and cap["model_source"] == "hermes_hook"
        assert entry["execution_model"] == f"{PROVIDER}/{MODEL}"
        assert cap["prompt_count"] == 1 and cap["turn_count"] == 1
        assert cap["tool_call_count"] == 5 and cap["tool_failure_count"] == 1
        assert cap["task_status"] == "turn_completed"
        assert cap["session_end_observed"] is True and cap["session_end_reason"] == "cli_exit"
        assert cap["task_source"] == "first_user_prompt_excerpt"
        assert entry["task"].startswith("Add a calc module")
        raw = json.dumps(entry)
        for leaked in (SECRET, "RAW TRANSCRIPT", "RAW TOOL RESULT", "RAW FILE", "RAW CHILD", "RAW RESPONSE"):
            assert leaked not in raw, leaked
        assert entry["verification_attempted"] is True and entry["verification_passed"] is None
        calc = next(f for f in entry["files_detail"] if f["path"] == "calc.py")
        assert calc["change_type"] == "create"  # git says created; the write_file report agrees on the path

    def test_tokens_are_hermes_reported_and_cost_is_never_invented(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        assert entry["prompt_tokens"] == 100 + 200 and entry["completion_tokens"] == 10 + 20
        assert entry["total_tokens"] == 330
        assert entry["cache_creation_tokens"] == 10 and entry["cache_read_tokens"] == 14
        assert entry["tokens_provenance"] == "agent_reported"
        for key in ("estimated_cost", "cost_provenance"):
            assert key not in entry
        assert entry["capture"]["cost_total_usd"] is None
        assert len(entry["capture"]["usage_by_key"]) == 2

    def test_a_re_reported_request_does_not_double_count(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Do it", model=MODEL))
        for _ in range(3):
            _run(_usage(repo, 1))
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        entry = _lines(repo)[0]
        assert entry["prompt_tokens"] == 100 and entry["completion_tokens"] == 10

    def test_no_usage_reported_means_no_tokens_recorded(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Do it", model=MODEL))
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        entry = _lines(repo)[0]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "tokens_provenance", "estimated_cost"):
            assert key not in entry
        assert entry["execution_model"] == MODEL and entry["capture"]["provider"] is None

    def test_tool_events_carry_status_and_evidence(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        events = events_from_entry(entry)
        assert events and all(e.source == SOURCE_HERMES_HOOKS and e.actor == "hermes" for e in events)
        tools = {(e.metadata.get("tool"), e.action): e for e in events if e.event_type == "tool.invoked"}
        read = next(e for e in events if e.metadata.get("tool") == "read_file")
        assert read.target == "README.md" and read.metadata.get("access") == "read" and read.status == "unknown"
        assert read.metadata["duration_ms"] == 4 and read.metadata["tool_call_id"] == "tc-1"
        assert read.metadata["turn_id"] == "turn1" and read.metadata["tool_status"] == "ok"
        write = next(e for e in events if e.metadata.get("tool") == "write_file")
        assert write.target == "calc.py" and write.status == "passed" and write.evidence == "agent_reported"
        assert write.metadata["duration_ms"] == 12
        test_cmd = next(e for e in events if e.metadata.get("command_kind") == "test")
        assert test_cmd.action.startswith("terminal: python -m pytest") and test_cmd.status == "unknown"
        assert test_cmd.metadata["duration_ms"] == 850
        git_cmd = next(e for e in events if e.target == "git")
        assert git_cmd.status == "failed" and git_cmd.metadata["tool_status"] == "error"
        assert next(e for e in events if e.metadata.get("tool") == "web_search").target is None
        assert len(tools) == 5
        types = [e.event_type for e in events]
        assert "session.started" in types and "file.changed" in types and "run.completed" in types
        assert not [e for e in events if e.event_type.startswith("verification.")]

    def test_blocked_and_errored_file_tools_are_never_evidence_of_a_change(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Edit", model=MODEL))
        for status in ("blocked", "error"):
            _run(_tool(repo, "write_file", {"path": "nope.py", "content": "x"}, status=status))
        _run(_tool(repo, "write_file", {"path": "unknown.py", "content": "x"}, status="weird"))
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        entry = _lines(repo)[0]
        assert entry["files_detail"] == []
        writes = [e for e in entry["events"] if e["metadata"].get("tool") == "write_file"]
        assert [e["status"] for e in writes] == ["failed", "failed", "unknown"]
        assert writes[0]["metadata"]["tool_status"] == "blocked"
        assert entry["capture"]["tool_failure_count"] == 2

    def test_failed_check_is_agent_reported_and_success_is_not_promoted(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Test", model=MODEL))
        _run(_tool(repo, "terminal", {"command": "ruff check ."}))
        _run(_tool(repo, "terminal", {"command": "pytest -q"}, status="error"))
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        verification = _lines(repo)[0]["verification"]
        assert verification["source"] == "agent_reported" and verification["status"] == "failed"
        assert verification["checks_attempted"] == 2 and verification["checks_failed"] == 1
        assert verification["checks_passed"] == 0  # hooks never promote "ok" to an OpenShard-verified pass

    def test_subagent_and_approval_facts(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        assert entry["capture"]["subagents"] == {"started": 1, "stopped": 1, "failed": 0}
        assert entry["capture"]["approvals"] == {"requested": 1, "granted": 1, "denied": 0, "unanswered": 0}
        events = events_from_entry(entry)
        sub_start = next(e for e in events if e.action.startswith("subagent started"))
        assert sub_start.action == "subagent started (role=leaf)" and sub_start.evidence == "agent_reported"
        assert sub_start.metadata["child_session_id"] == CHILD_SID and sub_start.metadata["child_subagent_id"] == "sa-1"
        sub_stop = next(e for e in events if e.action.startswith("subagent stopped"))
        assert sub_stop.metadata["child_status"] == "completed" and sub_stop.metadata["tool_calls"] == 2
        assert sub_stop.metadata["duration_ms"] == 4200 and sub_stop.status == "unknown"
        requested = next(e for e in events if e.event_type == "approval.requested")
        granted = next(e for e in events if e.event_type == "approval.granted")
        assert requested.action.startswith("approval requested: command: rm -rf build")
        assert requested.metadata["pattern_key"] == "rm_rf" and requested.target == "rm"
        assert granted.status == "passed" and granted.metadata["choice"] == "once"
        assert granted.metadata["surface"] == "cli" and granted.evidence == "agent_reported"

    def test_approval_outcomes_are_never_guessed(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Go", model=MODEL))
        for choice in ("deny", "smart_deny", "timeout", "cancelled", "notify_failed", "mystery", "always"):
            _run(_doc(repo, "post_approval_response", command="rm x", choice=choice, surface="cli"))
        _run(_doc(repo, "subagent_stop", child_status="error"))
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        entry = _lines(repo)[0]
        assert entry["capture"]["approvals"] == {"requested": 0, "granted": 1, "denied": 2, "unanswered": 4}
        assert entry["capture"]["subagents"] == {"started": 0, "stopped": 1, "failed": 1}
        by_choice = {e["metadata"]["choice"]: e for e in entry["events"] if "choice" in e["metadata"]}
        assert by_choice["deny"]["event_type"] == "approval.denied" and by_choice["deny"]["status"] == "failed"
        assert by_choice["smart_deny"]["event_type"] == "approval.denied"
        assert by_choice["always"]["event_type"] == "approval.granted"
        for choice in ("timeout", "cancelled", "notify_failed", "mystery"):
            assert by_choice[choice]["event_type"] == "session.activity" and by_choice[choice]["status"] == "unknown"

    def test_no_subagents_or_approvals_leave_no_counts(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Go", model=MODEL))
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        cap = _lines(repo)[0]["capture"]
        assert "subagents" not in cap and "approvals" not in cap

    def test_counts_survive_a_late_hook_after_session_end(self, repo):
        _drive_inline(repo)
        _run(_doc(repo, "subagent_stop", child_status="completed"))  # arrives after finalize
        cap = _lines(repo)[0]["capture"]
        assert cap["subagents"]["stopped"] == 2 and cap["approvals"]["granted"] == 1
        assert len(_lines(repo)) == 1

    def test_interrupt_and_idle_are_not_completed_turns(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        _run(_doc(repo, "pre_llm_call", user_message="Long task", model=MODEL))
        _run(_tool(repo, "terminal", {"command": "sleep 1"}))
        _run(_doc(repo, "on_session_end", completed=False, interrupted=True))
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["task_status"] == "in_progress"
        assert any(e["action"] == "assistant turn interrupted by user" for e in entry["events"])
        _run(_doc(repo, "on_session_end", completed=False, interrupted=False, failed=True))
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["idle_count"] == 1
        _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        assert _lines(repo)[0]["capture"]["turn_count"] == 1

    def test_multiple_turns_are_one_session(self, repo):
        _run(_doc(repo, "on_session_start", model=MODEL))
        for i in range(3):
            _run(_doc(repo, "pre_llm_call", user_message=f"Turn {i}", model=MODEL, is_first_turn=i == 0))
            _run(_doc(repo, "on_session_end", completed=True, interrupted=False))
        entry = _lines(repo)[0]
        assert entry["capture"]["prompt_count"] == 3 and entry["capture"]["turn_count"] == 3
        assert entry["task"] == "Turn 0" and entry["capture"]["session_end_observed"] is False
        assert len(_lines(repo)) == 1

    def test_child_session_is_its_own_shard_linked_from_the_parent(self, repo):
        _drive_inline(repo)
        _run(_doc(repo, "on_session_start", CHILD_SID, model=MODEL))
        _run(_doc(repo, "pre_llm_call", CHILD_SID, user_message="Child goal", model=MODEL))
        _run(_doc(repo, "on_session_end", CHILD_SID, completed=True, interrupted=False))
        lines = _lines(repo)
        assert {ln["capture"]["session_id"] for ln in lines} == {SID, CHILD_SID}
        parent = next(ln for ln in lines if ln["capture"]["session_id"] == SID)
        link = next(e for e in parent["events"] if e["action"].startswith("subagent started"))
        assert link["metadata"]["child_session_id"] == CHILD_SID

    def test_receipt_reads_as_an_external_hermes_capture(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        receipt = build_shard_receipt(entry)
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED
        assert receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.agent == "Hermes Agent (external)"
        full = render_full_shard_receipt(receipt)
        assert "Hermes Agent" in full
        compact = render_compact_shard_receipt(receipt)
        assert "Hermes Agent" in compact
        shards = list_shards(repo_path=repo)
        assert len(shards) == 1
        assert get_receipt(shards[0].shard_id, repo_path=repo).agent == "Hermes Agent (external)"

    def test_queue_round_trip_keeps_attrs_bounded(self, repo):
        payload = hm.extract_hermes_payload(_tool(repo, "terminal", {"command": "ls"}, duration_ms=5,
                                                  tool_call_id="x" * 500))
        assert isinstance(payload, HookPayload)
        reduced = reduce_hook_payload(payload, repo)
        assert reduced is not None
        from openshard.adapters.claude_hooks import ReducedHookPayload

        again = ReducedHookPayload.from_dict(json.loads(json.dumps(reduced.to_dict())))
        assert again is not None and again.attrs["tool_call_id"] == "x" * 80 and again.attrs["duration_ms"] == 5
        hostile = ReducedHookPayload.from_dict({**reduced.to_dict(), "attrs": {
            "Bad Key": 1, "ok_key": {"nested": "dict"}, "long": "y" * 500, **{f"k{i}": i for i in range(40)}}})
        assert hostile is not None and len(hostile.attrs) <= 12
        assert "Bad Key" not in hostile.attrs and "ok_key" not in hostile.attrs
        assert hostile.attrs["long"] == "y" * 80


class TestOptInRepositories:
    def test_repository_without_openshard_dir_is_not_captured(self, tmp_path):
        plain = _make_repo(tmp_path / "plain", opted_in=False)
        _drive_inline(plain)
        assert not (plain / ".openshard").exists()
        payload = hm.extract_hermes_payload(_doc(plain, "on_session_start"))
        assert isinstance(payload, HookPayload) and resolve_repo_root(payload, {}) is None

    def test_non_repository_directory_is_never_captured(self, tmp_path):
        folder = tmp_path / "just a folder"
        folder.mkdir()
        (folder / ".openshard").mkdir()  # even a stray marker does not make a non-repo a repository
        _drive_inline(folder)
        assert not (folder / ".openshard" / "runs.jsonl").exists()

    def test_opted_in_repository_is_captured_from_a_subdirectory(self, repo):
        sub = repo / "pkg" / "inner"
        sub.mkdir(parents=True)
        payload = hm.extract_hermes_payload(_doc(sub, "on_session_start"))
        assert isinstance(payload, HookPayload) and resolve_repo_root(payload, {}) == repo.resolve()

    def test_other_agents_keep_capturing_anywhere(self, tmp_path):
        plain = _make_repo(tmp_path / "plain", opted_in=False)
        payload = HookPayload(event="SessionStart", session_id="s1", cwd=str(plain), agent="codex")
        assert resolve_repo_root(payload, {}) == plain.resolve()


# ---------------------------------------------------------------------------
# Service path: POST /hooks/hermes (authenticated, queued, folded behind)
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
    return client.post_hook(port, json.dumps(doc).encode("utf-8"), hook_path=client.HERMES_HOOK_PATH)


def _stable(entry: dict) -> dict:
    keys = ("event_type", "action", "status", "evidence", "target", "actor", "source")
    volatile = {"started_at", "last_activity_at", "first_prompt_at", "last_turn_completed_at",
                "last_status_ping_at", "last_idle_at", "applied_event_ids"}
    return {
        "task": entry["task"], "executor": entry["executor"], "execution_model": entry["execution_model"],
        "files_detail": entry["files_detail"], "summary": entry["summary"],
        "tokens": [entry.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")],
        "capture": {k: v for k, v in entry["capture"].items() if k not in volatile},
        "events": [{k: e.get(k) for k in keys} for e in entry["events"]],
    }


class TestServicePath:
    def test_http_session_matches_inline_record(self, service, tmp_path):
        via_http = _make_repo(tmp_path / "http")
        via_inline = _make_repo(tmp_path / "inline")
        for i, (doc, before) in enumerate(_session_steps(via_http)):
            if before:
                before()  # type: ignore[operator]
            assert _post(service.port, doc), i
        _drive_inline(via_inline)
        assert _wait_for(lambda: bool(_lines(via_http)) and _lines(via_http)[0]["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        assert _stable(_lines(via_http)[0]) == _stable(_lines(via_inline)[0])
        raw = (via_http / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert SECRET not in raw and "RAW TRANSCRIPT" not in raw

    def test_queue_line_is_reduced_and_agent_tagged(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, _tool(repo, "write_file", {"path": str(repo / "calc.py"),
                                                              "content": FILE_BODY + SECRET}, duration_ms=9))
        queue_file = repo / ".openshard" / "claude_sessions" / f"hermes.{SID}{svc.QUEUE_SUFFIX}"
        line = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
        assert line["kind"] == "hook" and line["data"]["agent"] == "hermes"
        assert line["data"]["event"] == "PostToolUse" and line["data"]["file_target"] == "calc.py"
        assert line["data"]["tool_success"] is True and line["data"]["attrs"]["duration_ms"] == 9
        text = queue_file.read_text(encoding="utf-8")
        assert SECRET not in text and "RAW FILE" not in text and "RAW TOOL RESULT" not in text
        service.server.recorder.resume_processing()

    def test_usage_is_queued_as_a_status_line_for_the_agent(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, _doc(repo, "on_session_start", model=MODEL))
        assert _post(service.port, _usage(repo, 1))
        queue_file = repo / ".openshard" / "claude_sessions" / f"hermes.{SID}{svc.QUEUE_SUFFIX}"
        lines = [json.loads(ln) for ln in queue_file.read_text(encoding="utf-8").splitlines()]
        status = next(ln for ln in lines if ln["kind"] == "status")
        assert status["data"]["agent"] == "hermes" and status["data"]["usage_key"] == "turn1:api:1"
        assert status["data"]["tokens_input"] == 100 and status["data"]["cost_total_usd"] is None
        assert "RAW RESPONSE" not in queue_file.read_text(encoding="utf-8")
        service.server.recorder.resume_processing()

    def test_unauthenticated_post_is_refused(self, service, repo):
        status, _ = client._request("POST", service.port, client.HERMES_HOOK_PATH,
                                    json.dumps(_doc(repo, "on_session_start")).encode(),
                                    {"Content-Type": "application/json"})
        assert status == 401
        from openshard.adapters.capture_auth import TOKEN_HEADER, repo_capability

        token = client._auth_headers(None, None)[TOKEN_HEADER]
        headers = {"Content-Type": "application/json", TOKEN_HEADER: repo_capability(token, repo, "claude_code")}
        status, _ = client._request("POST", service.port, client.HERMES_HOOK_PATH,
                                    json.dumps(_doc(repo, "on_session_start")).encode(), headers)
        assert status == 401
        assert _lines(repo) == [] and client.health(service.port)["stats"]["queued"] == 0

    def test_unsupported_event_is_ignored_not_errored(self, service, repo):
        assert _post(service.port, _doc(repo, "pre_tool_call", tool="terminal", args={"command": "ls"}))
        assert _post(service.port, _doc(repo, "post_llm_call", assistant_response="RAW"))
        assert client.health(service.port)["stats"]["queued"] == 0

    def test_repository_that_has_not_opted_in_records_nothing(self, service, tmp_path):
        plain = _make_repo(tmp_path / "plain", opted_in=False)
        assert _post(service.port, _doc(plain, "on_session_start", model=MODEL))
        assert _post(service.port, _tool(plain, "terminal", {"command": "ls"}))
        assert service.server.recorder.wait_idle(20)
        assert not (plain / ".openshard").exists()

    def test_hook_command_forwards_and_never_imports_fold_code(self, service, repo, capture_env):
        code = (
            "import sys, os; from openshard.adapters.claude_capture_client import run_hermes_hook; "
            "label, reply = run_hermes_hook(sys.stdin, env=dict(os.environ)); "
            "print(label); print(reply); print(sorted(m for m in sys.modules if m.startswith('openshard')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc(repo, "on_session_start", model=MODEL)),
            capture_output=True, text=True, timeout=60, env=capture_env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == "forwarded" and out[1] == "{}"
        assert "openshard.adapters.claude_hooks" not in result.stdout
        assert "openshard.adapters.hermes_hooks" not in result.stdout
        assert _wait_for(lambda: bool(_lines(repo)) or client.health(service.port)["stats"]["queued"] >= 1)

    def test_no_spawn_falls_back_inline(self, capture_env, repo):
        stream = io.BytesIO(json.dumps(_doc(repo, "pre_llm_call", user_message="Go", model=MODEL)).encode())
        label, reply = client.run_hermes_hook(stream, env=capture_env, spawn=False)
        assert label == "record_created" and reply == "{}"
        assert _lines(repo)[0]["executor"] == "hermes_hooks"

    def test_reply_is_the_empty_object_whatever_happens(self, capture_env, tmp_path):
        for raw in (b"", b"not json", b"[]", json.dumps({"hook_event_name": "pre_tool_call"}).encode()):
            _label, reply = client.run_hermes_hook(io.BytesIO(raw), env=capture_env, spawn=False)
            assert reply == "{}"

    def test_blocking_path_stays_within_budget(self, service, repo):
        assert _post(service.port, _doc(repo, "on_session_start", model=MODEL))
        # GitHub's Windows runners can exhibit large scheduler/loopback latency
        # spikes under the full 9k-test suite. Keep this as a regression guard,
        # but use a CI-realistic Windows budget; Linux remains the tighter signal.
        p50_budget, p95_budget = (500, 1200) if sys.platform == "win32" else (25, 50)
        for attempt in range(1, 4):
            roundtrips: list[float] = []
            for i in range(40):
                doc = _usage(repo, i) if i % 2 else _tool(repo, "terminal", {"command": f"echo {i}"})
                t0 = time.perf_counter()
                assert _post(service.port, doc)
                roundtrips.append(time.perf_counter() - t0)
            service.server.recorder.wait_idle(60)
            roundtrips.sort()
            p50_ms = roundtrips[len(roundtrips) // 2] * 1000
            p95_ms = roundtrips[int(round(0.95 * (len(roundtrips) - 1)))] * 1000
            if p50_ms < p50_budget and p95_ms < p95_budget:
                break
            if attempt == 3:
                assert p50_ms < p50_budget and p95_ms < p95_budget, (p50_ms, p95_ms)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

CONFIG_WITH_COMMENTS = (
    "# my hermes config\n"
    "model:\n"
    "  default: claude-sonnet-4-6  # keep this comment\n"
    "terminal:\n"
    "  backend: local\n"
)


class TestInstaller:
    def test_home_resolution(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
        assert hermes_home() == tmp_path / "custom"
        monkeypatch.delenv("HERMES_HOME")
        if sys.platform == "win32":
            monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
            assert hermes_home() == tmp_path / "local" / "hermes"
        else:
            assert hermes_home() == Path.home() / ".hermes"
        assert hermes_home({"HERMES_HOME": "  "}) != Path("")

    def test_fresh_install_appends_a_managed_block_and_records_consent(self, hermes_env):
        hermes_env.mkdir()
        cfg = config_path(hermes_env)
        cfg.write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")
        result = install_hermes_hooks()
        assert result.status == "installed", result.message
        text = cfg.read_text(encoding="utf-8")
        assert text.startswith(CONFIG_WITH_COMMENTS)  # every existing byte and comment survives
        assert BLOCK_BEGIN in text and not (hermes_env / f"config.yaml{BACKUP_SUFFIX}").exists()
        data = yaml.safe_load(text)
        assert data["model"] == {"default": "claude-sonnet-4-6"} and data["terminal"] == {"backend": "local"}
        assert set(data["hooks"]) == set(HOOK_EVENTS)
        for event in HOOK_EVENTS:
            assert data["hooks"][event] == [{"command": HOOK_COMMAND, "timeout": 15}]
        assert "pre_tool_call" not in data["hooks"] and "matcher" not in json.dumps(data["hooks"])
        assert "fail_closed" not in json.dumps(data["hooks"])
        assert installed_hermes_events(data) == list(HOOK_EVENTS)
        allow = json.loads(allowlist_path(hermes_env).read_text(encoding="utf-8"))
        assert {a["event"] for a in allow["approvals"]} == set(HOOK_EVENTS)
        assert all(a["command"] == HOOK_COMMAND and a["approved_at"].endswith("Z") for a in allow["approvals"])
        assert all("script_mtime_at_approval" in a for a in allow["approvals"])
        assert approved_hermes_events(allow) == list(HOOK_EVENTS)

    def test_install_creates_the_home_when_hermes_has_never_run(self, hermes_env):
        assert not hermes_env.exists()
        assert install_hermes_hooks().status == "installed"
        assert installed_hermes_events(load_hermes_config(hermes_env)[0]) == list(HOOK_EVENTS)

    def test_idempotent(self, hermes_env):
        assert install_hermes_hooks().status == "installed"
        cfg_before = config_path(hermes_env).read_bytes()
        allow_before = allowlist_path(hermes_env).read_bytes()
        again = install_hermes_hooks()
        assert again.status == "already_installed"
        assert all(v == "unchanged" for v in again.events.values())
        assert config_path(hermes_env).read_bytes() == cfg_before
        assert allowlist_path(hermes_env).read_bytes() == allow_before

    def test_merges_into_an_existing_hooks_section_and_backs_up_once(self, hermes_env):
        hermes_env.mkdir()
        original = (
            "# comment that a re-serialisation cannot keep\n"
            "hooks:\n"
            "  post_tool_call:\n"
            "    - matcher: 'write_file|patch'\n"
            "      command: ~/.hermes/agent-hooks/auto-format.sh\n"
            "  pre_tool_call:\n"
            "    - command: ~/.hermes/agent-hooks/guard.sh\n"
            "      fail_closed: true\n"
            "  outbound:\n"
            "    - url: https://ci.example.com/hooks\n"
            "      events: [on_session_end]\n"
            "hooks_auto_accept: false\n"
            "model:\n"
            "  default: x\n"
        )
        config_path(hermes_env).write_text(original, encoding="utf-8")
        result = install_hermes_hooks()
        assert result.status == "installed" and any("not preserved" in w for w in result.warnings)
        backup = hermes_env / f"config.yaml{BACKUP_SUFFIX}"
        assert backup.read_text(encoding="utf-8") == original
        data = yaml.safe_load(config_path(hermes_env).read_text(encoding="utf-8"))
        post = data["hooks"]["post_tool_call"]
        assert post[0] == {"matcher": "write_file|patch", "command": "~/.hermes/agent-hooks/auto-format.sh"}
        assert post[1] == {"command": HOOK_COMMAND, "timeout": 15}
        assert data["hooks"]["pre_tool_call"] == [
            {"command": "~/.hermes/agent-hooks/guard.sh", "fail_closed": True}]  # untouched: never ours
        assert data["hooks"]["outbound"] == [{"url": "https://ci.example.com/hooks", "events": ["on_session_end"]}]
        assert data["hooks_auto_accept"] is False and data["model"] == {"default": "x"}
        # A second re-serialisation must not overwrite the first backup.
        config_path(hermes_env).write_text(yaml.safe_dump({**data, "extra": 1}), encoding="utf-8")
        uninstall_hermes_hooks()
        assert backup.read_text(encoding="utf-8") == original

    def test_a_stale_entry_of_ours_is_updated_not_duplicated(self, hermes_env):
        hermes_env.mkdir()
        config_path(hermes_env).write_text(yaml.safe_dump({"hooks": {
            "on_session_start": [{"command": f"{HOOK_COMMAND} --old", "timeout": 5}, {"command": "mine.sh"}],
        }}), encoding="utf-8")
        result = install_hermes_hooks()
        assert result.status == "updated" and result.events["on_session_start"] == "updated"
        assert result.events["post_tool_call"] == "added"
        data = yaml.safe_load(config_path(hermes_env).read_text(encoding="utf-8"))
        assert data["hooks"]["on_session_start"] == [{"command": "mine.sh"}, {"command": HOOK_COMMAND, "timeout": 15}]

    def test_existing_allowlist_entries_are_preserved(self, hermes_env):
        hermes_env.mkdir()
        other = {"event": "post_llm_call", "command": "/opt/mine.py", "approved_at": "2026-01-01T00:00:00Z",
                 "script_mtime_at_approval": "2026-01-01T00:00:00Z"}
        allowlist_path(hermes_env).write_text(json.dumps({"approvals": [other], "note": "keep"}), encoding="utf-8")
        install_hermes_hooks()
        allow = json.loads(allowlist_path(hermes_env).read_text(encoding="utf-8"))
        assert other in allow["approvals"] and allow["note"] == "keep"
        assert len(allow["approvals"]) == 1 + len(HOOK_EVENTS)
        first = {a["event"]: a["approved_at"] for a in allow["approvals"] if a["command"] == HOOK_COMMAND}
        install_hermes_hooks()
        again = json.loads(allowlist_path(hermes_env).read_text(encoding="utf-8"))
        assert {a["event"]: a["approved_at"] for a in again["approvals"] if a["command"] == HOOK_COMMAND} == first

    def test_refuses_to_clobber(self, hermes_env):
        hermes_env.mkdir()
        cfg = config_path(hermes_env)
        for bad_text in ("model: [unclosed", "- just\n- a list\n"):
            cfg.write_text(bad_text, encoding="utf-8")
            result = install_hermes_hooks()
            assert result.status == "error" and "will not modify" in result.message, bad_text
            assert cfg.read_text(encoding="utf-8") == bad_text
            assert uninstall_hermes_hooks().status == "error"
        for bad in ({"hooks": []}, {"hooks": {"post_tool_call": "x"}}, {"hooks": "no"}):
            cfg.write_text(yaml.safe_dump(bad), encoding="utf-8")
            result = install_hermes_hooks()
            assert result.status == "error" and "will not modify" in result.message, bad
            assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == bad
        cfg.write_text("model: x\n", encoding="utf-8")
        allowlist_path(hermes_env).write_text("{ not json", encoding="utf-8")
        assert install_hermes_hooks().status == "error"
        assert cfg.read_text(encoding="utf-8") == "model: x\n"
        assert allowlist_path(hermes_env).read_text(encoding="utf-8") == "{ not json"

    def test_uninstall_removes_the_managed_block_exactly(self, hermes_env):
        hermes_env.mkdir()
        config_path(hermes_env).write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")
        install_hermes_hooks()
        result = uninstall_hermes_hooks()
        assert result.status == "removed"
        assert config_path(hermes_env).read_text(encoding="utf-8") == CONFIG_WITH_COMMENTS
        assert json.loads(allowlist_path(hermes_env).read_text(encoding="utf-8")) == {"approvals": []}
        assert uninstall_hermes_hooks().status == "not_installed"

    def test_uninstall_removes_only_ours_from_a_merged_config(self, hermes_env):
        hermes_env.mkdir()
        foreign = {"hooks": {"post_tool_call": [{"command": "fmt.sh"}], "outbound": [{"url": "https://x"}]},
                   "model": "m"}
        config_path(hermes_env).write_text(yaml.safe_dump(foreign), encoding="utf-8")
        other = {"event": "post_tool_call", "command": "fmt.sh", "approved_at": "t", "script_mtime_at_approval": None}
        allowlist_path(hermes_env).write_text(json.dumps({"approvals": [other]}), encoding="utf-8")
        install_hermes_hooks()
        result = uninstall_hermes_hooks()
        assert result.status == "removed"
        assert yaml.safe_load(config_path(hermes_env).read_text(encoding="utf-8")) == foreign
        assert json.loads(allowlist_path(hermes_env).read_text(encoding="utf-8")) == {"approvals": [other]}

    def test_uninstall_never_touches_history(self, repo, hermes_env):
        _drive_inline(repo)
        install_hermes_hooks()
        uninstall_hermes_hooks()
        assert len(_lines(repo)) == 1

    def test_installed_config_is_what_hermes_would_parse(self, hermes_env):
        """Cross-check against the runtime rules documented for shell hooks."""
        install_hermes_hooks()
        data = yaml.safe_load(config_path(hermes_env).read_text(encoding="utf-8"))
        for event, entries in data["hooks"].items():
            assert isinstance(entries, list) and len(entries) == 1
            entry = entries[0]
            assert set(entry) == {"command", "timeout"} and isinstance(entry["timeout"], int)
            assert 1 <= entry["timeout"] <= 300  # Hermes clamps a shell hook to 300 s
            assert entry["command"].split() == ["openshard", "hooks", "hermes"]  # no shell syntax needed
        assert set(data["hooks"]) <= set(hm.HERMES_HOOK_EVENTS)


# ---------------------------------------------------------------------------
# CLI: hooks hermes, capture install/uninstall hermes, setup, doctor
# ---------------------------------------------------------------------------


def _which(name: str):
    return {"hermes": "/usr/local/bin/hermes", "openshard": "/usr/local/bin/openshard"}.get(name)


class TestCli:
    def test_hooks_hermes_command_records_inline_and_replies(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["hooks", "hermes"], input=json.dumps(_doc(repo, "on_session_start", model=MODEL)))
        assert result.exit_code == 0, result.output
        assert result.output == "{}\n"
        result = runner.invoke(cli, ["hooks", "hermes", "--no-spawn"], input=json.dumps(
            _doc(repo, "pre_llm_call", user_message="Go", model=MODEL)))
        assert result.exit_code == 0 and result.output == "{}\n"
        result = runner.invoke(cli, ["hooks", "hermes"], input=json.dumps(
            _doc(repo, "on_session_end", completed=True, interrupted=False)))
        assert result.exit_code == 0 and result.output == "{}\n"
        entry = _lines(repo)[0]
        assert entry["executor"] == "hermes_hooks" and entry["capture"]["turn_count"] == 1
        result = runner.invoke(cli, ["hooks", "hermes"], input="not json")
        assert result.exit_code == 0 and result.output == "{}\n"

    def test_entrypoint_fast_path_handles_hooks_hermes(self, repo):
        code = (
            "import sys; sys.argv = ['openshard', 'hooks', 'hermes', '--no-spawn']; "
            "from openshard.cli.entrypoint import main; main(); "
            "print(sorted(m for m in sys.modules if m.startswith('openshard.cli')))"
        )
        env = {**os.environ, "OPENSHARD_CAPTURE_DISABLE": "1"}
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc(repo, "pre_llm_call", user_message="Go", model=MODEL)),
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == "{}" and "openshard.cli.main" not in out[1]
        assert _lines(repo)[0]["executor"] == "hermes_hooks"

    def test_capture_install_and_uninstall_hermes(self, repo, hermes_env):
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "hermes", "--repo-path", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "installed" and data["configured"] is True
        assert installed_hermes_events(load_hermes_config(hermes_env)[0]) == list(HOOK_EVENTS)
        assert (repo / ".openshard").is_dir()
        result = runner.invoke(cli, ["capture", "install", "hermes", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "already installed" in result.output
        assert "Hermes Agent" in result.output and "new Hermes Agent session" in result.output
        result = runner.invoke(cli, ["capture", "uninstall", "hermes", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "removed" in result.output
        assert installed_hermes_events(load_hermes_config(hermes_env)[0]) == []
        assert (repo / ".openshard").is_dir()  # local history and the opt-in marker are never deleted

    def test_capture_install_hermes_marks_a_new_repository_as_opted_in(self, tmp_path):
        fresh = _make_repo(tmp_path / "fresh", opted_in=False)
        result = CliRunner().invoke(cli, ["capture", "install", "hermes", "--repo-path", str(fresh)])
        assert result.exit_code == 0, result.output
        assert (fresh / ".openshard").is_dir()
        _drive_inline(fresh)
        assert len(_lines(fresh)) == 1

    def test_hermes_install_and_uninstall_work_outside_a_repository(self, tmp_path, hermes_env, monkeypatch):
        elsewhere = tmp_path / "not a repo"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "hermes", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == "installed"
        assert not (elsewhere / ".openshard").exists()
        assert runner.invoke(cli, ["capture", "uninstall", "hermes"]).exit_code == 0

    def test_setup_detects_hermes_but_does_not_edit_its_global_config(self, repo, hermes_env):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        assert data["agents"]["hermes"]["status"] == "skipped_optin"
        assert "hermes" not in data["configured_agents"]
        assert any("openshard capture install hermes" in s for s in data["next_steps"])
        assert not hermes_env.exists()  # nothing under the Hermes home was written
        with patch("shutil.which", side_effect=_which):
            human = runner.invoke(cli, ["setup", "--repo-path", str(repo), "--yes"])
        assert "Hermes:" in human.output and "openshard capture install hermes" in human.output

    def test_setup_without_hermes_points_at_capture_install(self, repo):
        with patch("shutil.which", return_value=None):
            result = CliRunner().invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        data = json.loads(result.output)
        assert any("openshard capture install hermes" in s for s in data["next_steps"])

    def test_doctor_walks_through_every_readiness_state(self, repo, hermes_env):
        runner = CliRunner()

        def status():
            return detect_hermes_integration(repo)
        with patch("shutil.which", side_effect=_which):
            assert (status().state, status().configured) == ("absent", False)
            result = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            assert json.loads(result.output)["hermes"]["configured"] is False
            install_hermes_hooks()
            ready = status()
            assert ready.state == "openshard" and ready.configured and ready.events_missing == []
            assert ready.cli_available is True and ready.config_relpath == str(config_path(hermes_env))
            after = json.loads(runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)]).output)
            assert after["hermes"]["configured"] is True and after["hermes"]["events_installed"] == list(HOOK_EVENTS)
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
            assert "\nHermes Agent\n" in human.output and "Auto-capture hooks" in human.output

            # Hermes has not approved the hooks: it would skip them, so this is not "ready".
            allowlist_path(hermes_env).unlink()
            unapproved = status()
            assert unapproved.state == "partial" and not unapproved.configured
            assert "not approved" in unapproved.detail
            install_hermes_hooks()

            # Only some events present.
            data = yaml.safe_load(config_path(hermes_env).read_text(encoding="utf-8"))
            del data["hooks"]["subagent_stop"]
            config_path(hermes_env).write_text(yaml.safe_dump(data), encoding="utf-8")
            partial = status()
            assert partial.state == "partial" and partial.events_missing == ["subagent_stop"]
            install_hermes_hooks()

            # A repository that has not opted in is not captured.
            (repo / ".openshard").rmdir()
            not_opted = status()
            assert not_opted.state == "partial" and "no .openshard/" in not_opted.detail
            (repo / ".openshard").mkdir()

            # Hermes' safe mode skips every shell hook.
            with patch.dict(os.environ, {"HERMES_SAFE_MODE": "1"}):
                assert status().state == "partial" and "HERMES_SAFE_MODE" in status().detail

            config_path(hermes_env).write_text("model: [unclosed", encoding="utf-8")
            broken = status()
            assert broken.state == "error" and broken.config_error
        with patch("shutil.which", return_value=None):
            absent = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert "openshard capture install hermes" in absent.output

    def test_setup_agent_snapshot_includes_hermes(self, repo):
        with patch("shutil.which", side_effect=_which):
            result = CliRunner().invoke(cli, ["setup", "--agent", "--json", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["hermes"]["cli_available"] is True and data["hermes"]["configured"] is False


class TestTelemetryAndIdentity:
    def test_hermes_is_a_known_telemetry_agent_but_sends_nothing_new(self):
        from openshard.telemetry.events import _EXECUTOR_AGENTS
        from openshard.telemetry.schema import AGENTS, EVENT_TYPES

        assert _EXECUTOR_AGENTS["hermes_hooks"] == "hermes" and "hermes" in AGENTS
        validator = EVENT_TYPES["setup.completed"]["agents"]
        assert validator(["claude_code", "hermes"]) is not None
        with pytest.raises(ValueError):
            validator(["hermes", "not_an_agent"])

    def test_shard_identity_is_external_observed_partial(self, repo):
        from openshard.history.shard import derive_shard_identity

        _drive_inline(repo)
        agent, origin, depth = derive_shard_identity(_lines(repo)[0])
        assert agent == "Hermes Agent (external)"
        assert origin == ORIGIN_EXTERNAL_OBSERVED and depth == CAPTURE_PARTIAL
