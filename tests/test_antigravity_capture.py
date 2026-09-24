"""Tests for Google Antigravity capture (0.4.7): translator, installer, service path, CLI.

Every test drives the adapter with synthetic Antigravity hook documents in a
throw-away git repository. No real Antigravity is ever run; where the
installer/setup code looks for one, ``shutil.which`` is patched. Documents
follow the camelCase shape Antigravity sends on stdin, which does not name
the event -- the event arrives on the command line (``--event``), exactly as
the installer configures it.
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

from openshard.adapters import antigravity_hooks as ag
from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters.antigravity_hooks_install import (
    HOOK_COMMAND,
    HOOK_EVENTS,
    HOOK_NAME,
    HOOKS_RELPATH,
    build_hook_config,
    install_antigravity_hooks,
    installed_antigravity_events,
    uninstall_antigravity_hooks,
)
from openshard.adapters.capture_agents import profile_for
from openshard.adapters.claude_hooks import handle_hook, reduce_hook_payload
from openshard.cli.main import cli
from openshard.history.capture_completeness import derive_capture_completeness
from openshard.history.event import SOURCE_ANTIGRAVITY_HOOKS, events_from_entry
from openshard.history.query import get_receipt, list_shards
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)

SID = "0b6c1f7e-2a4d-4c8e-9f10-a1b2c3d4e5f6"
SID2 = "0b6c1f7e-2a4d-4c8e-9f10-ffffffffffff"
SECRET = "sk-proj-SECRETSECRET12345678901234567890abcdef"
TRANSCRIPT = "/home/user/.gemini/antigravity/brain/conv/transcript.jsonl"
ARTIFACTS = "/home/user/.gemini/antigravity/brain/conv/artifacts"
FILE_BODY = "def add(a, b):\n    return a + b  # RAW FILE CONTENT"
GEMINI = "gemini-3.1-pro"
CLAUDE = "claude-sonnet-4-6"


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
    return _make_repo(tmp_path / "antigravity repo")


def _doc(repo: Path, sid: str = SID, **fields) -> dict:
    """An Antigravity hook document with the base fields every event carries."""
    base: dict = {
        "conversationId": sid,
        "workspacePaths": [str(repo)],
        "modelName": GEMINI,
        "transcriptPath": TRANSCRIPT,
        "artifactDirectoryPath": ARTIFACTS,
    }
    base.update(fields)
    return base


def _tool(repo: Path, name: str, args: dict, sid: str = SID, **fields) -> dict:
    return _doc(repo, sid, toolCall={"name": name, "args": args}, stepIdx=fields.pop("stepIdx", 3), **fields)


def _run(repo: Path, event: str, doc: dict | None = None):
    return handle_hook(doc if doc is not None else _doc(repo), env={}, agent="antigravity", event_override=event)


def _drive_inline(repo: Path, sid: str = SID) -> None:
    _run(repo, "PreInvocation", _doc(repo, sid, invocationNum=0))
    _run(repo, "PostToolUse", _tool(repo, "view_file", {"AbsolutePath": str(repo / "README.md")}, sid,
                                    error=""))
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(repo, "PostToolUse", _tool(repo, "write_to_file",
                                    {"TargetFile": str(repo / "calc.py"), "CodeContent": FILE_BODY + SECRET},
                                    sid, error=""))
    _run(repo, "PreInvocation", _doc(repo, sid, invocationNum=1))
    _run(repo, "PostToolUse", _tool(repo, "run_command",
                                    {"CommandLine": "python -m pytest -q", "Cwd": str(repo)}, sid, error=""))
    # The user switched models mid-conversation (Antigravity runs several).
    _run(repo, "PreInvocation", _doc(repo, sid, invocationNum=2, modelName=CLAUDE))
    _run(repo, "PostToolUse", _tool(repo, "run_command", {"CommandLine": "git status"}, sid,
                                    modelName=CLAUDE, error=f"exit status 1 {SECRET}"))
    _run(repo, "PostToolUse", _tool(repo, "mcp_github_create_issue", {"title": SECRET}, sid,
                                    modelName=CLAUDE, error=""))
    _run(repo, "Stop", _doc(repo, sid, modelName=CLAUDE, terminationReason="model_stop", fullyIdle=True,
                            error="", executionNum=1))


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
    def test_event_comes_from_the_command_line_and_model_is_preserved(self, repo):
        p = ag.extract_antigravity_payload(_doc(repo, invocationNum=0), event_override="PreInvocation")
        assert p is not None and p.event == "ModelInvocation"
        assert p.agent == "antigravity" and p.model_id == GEMINI and p.provider_id is None
        assert p.session_id == SID and p.cwd == str(repo)
        # No event on the command line: hookEventName is the fallback; with neither, nothing.
        assert ag.extract_antigravity_payload(_doc(repo, hookEventName="Stop")).event == "Stop"
        assert ag.extract_antigravity_payload(_doc(repo)) is None
        # The installed --event wins over whatever the document claims.
        assert ag.extract_antigravity_payload(_doc(repo, hookEventName="Stop"), event_override="PreInvocation").event \
            == "ModelInvocation"

    def test_stop_completion_needs_no_error_and_full_idle(self, repo):
        stop = ag.extract_antigravity_payload(_doc(repo, fullyIdle=True, error=""), event_override="Stop")
        assert stop.event == "Stop"
        assert ag.extract_antigravity_payload(_doc(repo), event_override="Stop").event == "Stop"
        for fields in ({"error": "model overloaded"}, {"fullyIdle": False}, {"fullyIdle": False, "error": "x"}):
            p = ag.extract_antigravity_payload(_doc(repo, **fields), event_override="Stop")
            assert p.event == "SessionIdle", fields

    def test_unsubscribed_or_unknown_events_are_ignored(self, repo):
        for ev in ("PreToolUse", "PostInvocation", "SessionStart", "SessionEnd", "Notification",
                   "BeforeTool", "AfterAgent", "Nope", ""):
            assert ag.extract_antigravity_payload(_doc(repo), event_override=ev or None) is None, ev

    def test_session_id_and_cwd_are_validated(self, repo):
        for bad in ("../../etc/passwd", 42, None, "", "a b"):
            p = ag.extract_antigravity_payload(_doc(repo, conversationId=bad), event_override="Stop")
            assert p.session_id is None, bad
        for bad in ([], "not a list", [42, None], None):
            p = ag.extract_antigravity_payload(_doc(repo, workspacePaths=bad), event_override="Stop")
            assert p.cwd is None, bad
        multi = ag.extract_antigravity_payload(_doc(repo, workspacePaths=["", str(repo), "/other"]),
                                               event_override="Stop")
        assert multi.cwd == str(repo)

    def test_agent_identity_is_never_read_from_the_payload(self, repo):
        doc = _doc(repo, agent="claude_code", executor="native", hookEventName="Stop",
                   agentName="Cursor", modelName=GEMINI)
        p = ag.extract_antigravity_payload(doc)
        assert p.agent == "antigravity"
        handle_hook(_doc(repo, agent="claude_code", invocationNum=0), env={}, agent="antigravity",
                    event_override="PreInvocation")
        entry = _lines(repo)[0]
        assert entry["executor"] == "antigravity_hooks" and entry["capture"]["agent"] == "antigravity"

    def test_tools_classify_and_read_only_the_path_or_command(self, repo):
        def post(name, args, **fields):
            return ag.extract_antigravity_payload(_tool(repo, name, args, **fields), event_override="PostToolUse")

        cmd = post("run_command", {"CommandLine": "npm test", "Cwd": "/x", "SafeToAutoRun": True})
        assert cmd.event == "PostToolUse" and cmd.tool_kind == "command" and cmd.command == "npm test"
        assert cmd.file_path is None and cmd.tool_success is None
        # ACP client tools use snake_case.
        assert post("run_command", {"command_line": "ls"}).command == "ls"

        write = post("write_to_file", {"TargetFile": "a.py", "CodeContent": FILE_BODY}, error="")
        assert write.tool_kind == "file" and write.file_path == "a.py" and write.tool_success is True
        assert write.file_paths == [("a.py", "create")]
        over = post("write_to_file", {"TargetFile": "a.py", "Overwrite": True}, error="")
        assert over.file_paths == [("a.py", "update")]
        for name in ("replace_file_content", "multi_replace_file_content", "client_edit_file"):
            p = post(name, {"TargetFile": "b.py", "target_file": "c.py", "ReplacementChunks": [SECRET]}, error="")
            assert p.tool_kind == "file" and p.file_paths == [("b.py", "update")], name
        assert post("client_create_file", {"target_file": "d.py"}).file_paths == [("d.py", "create")]

        for name, args, path in (
            ("view_file", {"AbsolutePath": "/r/x.py", "StartLine": 1}, "/r/x.py"),
            ("view_file_outline", {"AbsolutePath": "/r/y.py"}, "/r/y.py"),
            ("view_code_item", {"File": "/r/z.py", "NodePaths": ["A.b"]}, "/r/z.py"),
            ("list_dir", {"DirectoryPath": "/r/src"}, "/r/src"),
            ("client_view_file", {"absolute_path": "/r/w.py"}, "/r/w.py"),
        ):
            p = post(name, args)
            assert p.tool_kind == "read" and p.file_path == path and p.command is None, name

        for name in ("grep_search", "codebase_search", "search_web", "read_url_content", "browser_subagent",
                     "mcp_github_create_issue", "SomethingNew"):
            p = post(name, {"CommandLine": "rm -rf /", "TargetFile": "/etc/passwd", "Query": SECRET})
            assert p.tool_kind == "other" and p.command is None and p.file_path is None, name

    def test_error_string_decides_failure_and_success(self, repo):
        def post(name, args, **fields):
            return ag.extract_antigravity_payload(_tool(repo, name, args, **fields), event_override="PostToolUse")

        failed = post("run_command", {"CommandLine": "false"}, error="exit status 1")
        assert failed.event == "PostToolUseFailure" and failed.command == "false"
        failed_write = post("write_to_file", {"TargetFile": "a.py"}, error="permission denied")
        assert failed_write.event == "PostToolUseFailure" and failed_write.tool_success is None
        # A present-and-empty error is the success signal; an absent one proves nothing.
        assert post("write_to_file", {"TargetFile": "a.py"}, error="").tool_success is True
        assert post("write_to_file", {"TargetFile": "a.py"}).tool_success is None
        assert post("write_to_file", {"TargetFile": "a.py"}, error="  ").event == "PostToolUse"

    def test_malformed_tool_shapes_under_report(self, repo):
        for call in ({"name": "run_command", "args": "ls"},
                     {"name": "run_command", "args": {"CommandLine": ["ls"]}},
                     {"name": "write_to_file", "args": {"TargetFile": 7}}):
            p = ag.extract_antigravity_payload(_doc(repo, toolCall=call), event_override="PostToolUse")
            assert p is not None and p.command is None and p.file_path is None, call

    def test_post_tool_use_without_a_tool_name_is_not_a_tool_step(self, repo):
        # CLI builds before 1.1.9 fired PostToolUse on non-tool steps (user input,
        # model responses); a document with no toolCall.name is never a tool record.
        for call in (None, "run_command", [], {"name": 42}, {"args": {"CommandLine": "ls"}}):
            assert ag.extract_antigravity_payload(_doc(repo, toolCall=call), event_override="PostToolUse") is None

    def test_never_reads_transcript_artifacts_contents_or_results(self, repo):
        for event, doc in (
            ("PreInvocation", _doc(repo, invocationNum=0)),
            ("PostToolUse", _tool(repo, "write_to_file", {"TargetFile": str(repo / "a.py"),
                                                          "CodeContent": FILE_BODY + SECRET}, error="")),
            ("PostToolUse", _tool(repo, "run_command", {"CommandLine": f"echo {SECRET}"},
                                  error=f"boom {SECRET}", result=f"out {SECRET}")),
            ("Stop", _doc(repo, fullyIdle=True, error=f"model said {SECRET}", responseText=SECRET)),
        ):
            p = ag.extract_antigravity_payload(doc, event_override=event)
            blob = json.dumps(reduce_hook_payload(p, repo).to_dict())
            assert TRANSCRIPT not in blob and ARTIFACTS not in blob and SECRET not in blob, event
            assert "RAW FILE CONTENT" not in blob


# ---------------------------------------------------------------------------
# Payload -> canonical Events / record / receipt (inline path)
# ---------------------------------------------------------------------------


class TestCanonicalRecord:
    def test_session_becomes_one_antigravity_shard(self, repo):
        _drive_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "antigravity_hooks" and entry["import_source"] == "antigravity"
        cap = entry["capture"]
        assert cap["source"] == "antigravity_hooks" and cap["agent"] == "antigravity"
        assert cap["agent_vendor"] == "Google" and cap["provider"] is None
        assert cap["model_source"] == "antigravity_hook"
        assert cap["session_id"] == SID
        # Antigravity has no session-end hook: never claimed.
        assert cap["session_end_observed"] is False
        assert cap["prompt_count"] == 0 and cap["tool_call_count"] == 5 and cap["tool_failure_count"] == 1
        assert cap["invocation_count"] == 3 and cap["turn_count"] == 1
        assert cap["task_status"] == "turn_completed"
        # No prompt reaches a command hook: the task is an honest placeholder.
        assert entry["task"] == "Google Antigravity session (task not captured)"
        assert cap["task_source"] == "not_captured"
        raw = json.dumps(entry)
        assert SECRET not in raw and TRANSCRIPT not in raw and ARTIFACTS not in raw and "RAW FILE" not in raw
        assert entry["verification_attempted"] is True and entry["verification_passed"] is None
        for key in ("estimated_cost", "cost_provenance", "prompt_tokens", "tokens_provenance"):
            assert key not in entry
        calc = next(f for f in entry["files_detail"] if f["path"] == "calc.py")
        assert calc["attribution"] == "agent_reported"  # Antigravity's empty-error success signal

    def test_each_model_used_is_preserved_individually(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        assert entry["capture"]["models_seen"] == [GEMINI, CLAUDE]
        assert entry["execution_model"] == CLAUDE  # the latest one reported
        model_events = [e for e in entry["events"]
                        if e["event_type"] == "session.activity" and e["metadata"].get("hook") == "ModelInvocation"]
        # One Event per model *change*, not per invocation, each with its own time and index.
        assert [e["metadata"]["model"] for e in model_events] == [GEMINI, CLAUDE]
        assert [e["metadata"]["invocation_index"] for e in model_events] == [1, 3]
        assert all(e["evidence"] == "agent_reported" for e in model_events)
        text = render_full_shard_receipt(build_shard_receipt(entry))
        assert "Observed 1" in text and "Observed 2" in text

    def test_events_carry_antigravity_identity_and_evidence(self, repo):
        _drive_inline(repo)
        entry = _lines(repo)[0]
        events = events_from_entry(entry)
        assert events and all(e.source == SOURCE_ANTIGRAVITY_HOOKS for e in events)
        assert all(e.actor == "antigravity" for e in events)
        types = [e.event_type for e in events]
        assert "session.started" in types and "tool.invoked" in types and "file.changed" in types
        assert "run.completed" not in types  # no session end is ever fabricated
        tools = [e for e in events if e.event_type == "tool.invoked"]
        by_tool = {e.metadata.get("tool"): e for e in tools}
        read = by_tool["view_file"]
        assert read.target == "README.md" and read.metadata.get("access") == "read" and read.status == "unknown"
        write = by_tool["write_to_file"]
        assert write.target == "calc.py" and write.status == "passed" and write.evidence == "agent_reported"
        test_cmd = next(e for e in tools if e.metadata.get("command_kind") == "test")
        assert test_cmd.action.startswith("run_command: python -m pytest") and test_cmd.status == "unknown"
        git_cmd = next(e for e in tools if e.target == "git")
        assert git_cmd.status == "failed"
        assert by_tool["mcp_github_create_issue"].target is None
        assert not [e for e in events if e.event_type.startswith("verification.")]
        started = next(e for e in events if e.event_type == "session.started")
        assert "Google Antigravity session observed" in started.action

    def test_observed_check_is_directly_observed_unknown_with_title_and_synced(self, repo):
        from openshard.sync import envelope

        _drive_inline(repo)
        entry = _lines(repo)[0]
        block = entry["verification"]
        # The hook showed `pytest` was invoked; Antigravity exposes no exit code.
        assert block["status"] == "unknown" and block["source"] == "directly_observed"
        assert block["observation_mode"] == "hook_tool_event"
        assert "outcome_not_observed" in block["incomplete_reasons"]
        assert [c["kind"] for c in block["checks"]] == ["test"]
        # The raw task is kept as recorded; the title is separate display metadata.
        assert entry["task"] and entry["task_title"]
        payload = envelope.receipt_payload(entry, 0)
        assert payload["task_full"] == entry["task"]
        assert payload["verification"] == block and payload["task_title"] == entry["task_title"]
        assert payload["verification_status"] == "unknown"
        assert payload["verification_reason"].endswith("[directly_observed]")

    def test_failed_check_rests_on_the_agents_own_error_signal(self, repo):
        _run(repo, "PreInvocation", _doc(repo, invocationNum=0))
        _run(repo, "PostToolUse", _tool(repo, "run_command", {"CommandLine": "npm test"},
                                        error=f"exit status 1 {SECRET}"))
        _run(repo, "Stop", _doc(repo, fullyIdle=True, error=""))
        entry = _lines(repo)[0]
        assert entry["verification"]["status"] == "failed"
        assert entry["verification"]["source"] == "agent_reported"
        assert SECRET not in json.dumps(entry)

    def test_read_is_never_a_change_or_an_attempted_edit(self, repo):
        _run(repo, "PreInvocation", _doc(repo, invocationNum=0))
        (repo / "README.md").write_text("changed by someone\n", encoding="utf-8")
        _run(repo, "PostToolUse", _tool(repo, "view_file", {"AbsolutePath": str(repo / "README.md")}, error=""))
        _run(repo, "Stop", _doc(repo, fullyIdle=True, error=""))
        entry = _lines(repo)[0]
        readme = next(f for f in entry["files_detail"] if f["path"] == "README.md")
        assert readme["attribution"] == "git_observed" and not readme.get("agent_attempted")

    def test_read_outside_the_repository_is_dropped(self, repo, tmp_path):
        _run(repo, "PreInvocation", _doc(repo))
        _run(repo, "PostToolUse", _tool(repo, "view_file", {"AbsolutePath": str(tmp_path / "secret.txt")}))
        _run(repo, "Stop", _doc(repo))
        ev = next(e for e in _lines(repo)[0]["events"] if e["event_type"] == "tool.invoked")
        assert ev["target"] is None and ev["metadata"]["path_dropped"] == "outside repository"
        assert str(tmp_path) not in json.dumps(_lines(repo)[0])

    def test_receipt_identity(self, repo):
        _drive_inline(repo)
        receipt = build_shard_receipt(_lines(repo)[0])
        assert receipt.agent == "Google Antigravity (external)"
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED
        assert receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.tokens_input is None and receipt.cost_provenance is None
        text = render_compact_shard_receipt(receipt)
        assert "Google Antigravity (external)" in text and "did not execute or verify" in text
        assert "Unknown" not in text.split("\n")[0]
        assert SECRET not in text
        shards = list_shards(repo_path=repo)
        assert len(shards) == 1 and shards[0].agent == "Google Antigravity (external)"
        assert get_receipt(shards[0].shard_id, repo_path=repo).agent == "Google Antigravity (external)"

    def test_invocations_alone_record_a_session_even_without_tool_hooks(self, repo):
        # Some Antigravity builds never fire PostToolUse; git still proves the change.
        _run(repo, "PreInvocation", _doc(repo, invocationNum=0))
        assert _lines(repo), "the first model invocation creates the record"
        (repo / "made.py").write_text("x = 1\n", encoding="utf-8")
        _run(repo, "Stop", _doc(repo, fullyIdle=True, error=""))
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 1 and entry["capture"]["tool_call_count"] == 0
        made = next(f for f in entry["files_detail"] if f["path"] == "made.py")
        assert made["attribution"] == "git_observed"

    def test_stop_without_any_activity_records_nothing(self, repo):
        outcome = _run(repo, "Stop", _doc(repo, fullyIdle=True, error=""))
        assert outcome.action == "buffered" and _lines(repo) == []

    def test_idle_or_errored_stop_is_never_a_completed_turn(self, repo):
        _run(repo, "PreInvocation", _doc(repo))
        _run(repo, "Stop", _doc(repo, fullyIdle=False, error=""))
        _run(repo, "Stop", _doc(repo, fullyIdle=True, error="quota exceeded"))
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["idle_count"] == 2
        assert entry["capture"]["task_status"] == "in_progress"

    def test_missing_optional_data_stays_unknown(self, repo):
        bare = {"conversationId": SID, "workspacePaths": [str(repo)]}
        _run(repo, "PreInvocation", dict(bare))
        _run(repo, "PostToolUse", dict(bare))
        _run(repo, "Stop", dict(bare))
        entry = _lines(repo)[0]
        assert entry["execution_model"] == "unknown" and entry["capture"]["model_source"] == "not_captured"
        assert entry["capture"]["models_seen"] == []
        # A PostToolUse with no toolCall is not a tool step: nothing is invented for it.
        assert not [e for e in entry["events"] if e["event_type"] == "tool.invoked"]
        assert not [e for e in entry["events"]
                    if e["event_type"] == "session.activity" and e["metadata"].get("hook") == "ModelInvocation"]

    def test_malformed_documents_are_ignored_not_errors(self, repo):
        for doc in ({}, {"conversationId": SID}, {"workspacePaths": [str(repo)]},
                    {"conversationId": "../x", "workspacePaths": [str(repo)]}):
            outcome = _run(repo, "PreInvocation", doc)
            assert outcome.action == "ignored", doc
        assert _lines(repo) == []

    def test_hook_config_is_never_a_changed_file_but_other_agents_files_are(self, repo):
        _run(repo, "PreInvocation", _doc(repo))
        install_antigravity_hooks(repo_root=repo)
        (repo / ".agents" / "rules").mkdir(parents=True)
        (repo / ".agents" / "rules" / "style.md").write_text("be terse\n", encoding="utf-8")
        _run(repo, "Stop", _doc(repo))
        paths = {f["path"] for f in _lines(repo)[0]["files_detail"]}
        assert ".agents/hooks.json" not in paths and ".agents/rules/style.md" in paths

    def test_same_session_id_as_another_agent_is_a_separate_shard(self, repo):
        from openshard.adapters.claude_hooks import handle_claude_hook

        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        claude_doc = {"session_id": SID, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "c"}
        handle_claude_hook(claude_doc, env=env)
        handle_claude_hook({**claude_doc, "hook_event_name": "Stop"}, env=env)
        _run(repo, "PreInvocation", _doc(repo))
        _run(repo, "Stop", _doc(repo))
        lines = _lines(repo)
        assert {e["executor"] for e in lines} == {"claude_code_hooks", "antigravity_hooks"}
        assert len({e["shard_id"] for e in lines}) == 2

    def test_idle_sweep_closes_a_session_honestly(self, repo):
        from openshard.adapters.claude_hooks import buffer_path, sweep_stale_buffers

        _run(repo, "PreInvocation", _doc(repo))
        _run(repo, "Stop", _doc(repo))
        assert buffer_path(repo, SID, "antigravity").exists()
        assert sweep_stale_buffers(repo, max_age_seconds=0) == [SID]
        entry = _lines(repo)[0]
        completeness = derive_capture_completeness(entry)
        assert completeness["status"] == "incomplete"
        assert [r["kind"] for r in completeness["reasons"]] == ["session_end_not_observed"]

    def test_export_compatible_record(self, repo):
        # The record is the same coerced shape every exporter/sync path reads.
        from openshard.history.shard_schema import coerce_shard_entry

        _drive_inline(repo)
        entry = _lines(repo)[0]
        assert coerce_shard_entry(json.loads(json.dumps(entry))) == entry
        assert isinstance(entry.get("receipt_id"), str) and entry["receipt_id"].startswith("rcpt_")
        assert profile_for("antigravity").label == "Google Antigravity"


# ---------------------------------------------------------------------------
# Sync / telemetry compatibility
# ---------------------------------------------------------------------------


class TestDownstream:
    def test_sync_envelope_carries_the_agent_and_is_eligible_once_quiet(self, repo):
        from datetime import UTC, datetime, timedelta

        from openshard.sync import envelope

        _drive_inline(repo)
        entry = _lines(repo)[0]
        env = envelope.build_envelope(entry, 0, core_version="0.0.0-test")
        receipt = env["receipt"]
        assert receipt["agent"] == "Google Antigravity (external)"
        blob = json.dumps(env)
        assert SECRET not in blob and TRANSCRIPT not in blob and "RAW FILE" not in blob
        # No session-end hook exists, so an Antigravity session syncs once it goes quiet.
        assert envelope.eligibility(entry).eligible is False
        later = datetime.now(UTC) + timedelta(seconds=envelope.QUIESCENT_SECONDS + 5)
        assert envelope.eligibility(entry, now=later).eligible is True

    def test_telemetry_maps_the_agent_to_its_own_enum(self, repo):
        from openshard.telemetry import events, schema

        _drive_inline(repo)
        props = events.receipt_properties(_lines(repo)[0])
        assert props["agent"] == "antigravity" and props["model_family"] == "claude"
        for key, value in props.items():
            schema.EVENT_TYPES["receipt.created"][key](value)  # raises on a value outside the contract


# ---------------------------------------------------------------------------
# The stdout reply
# ---------------------------------------------------------------------------


class TestReply:
    def test_reply_depends_only_on_the_event_name(self):
        assert client.antigravity_hook_response(b"{}", "Stop") == '{"decision": "stop"}'
        assert client.antigravity_hook_response(b"{}", "PreToolUse") == '{"decision": "allow"}'
        for ev in ("PreInvocation", "PostToolUse", "PostInvocation", "Weird"):
            assert client.antigravity_hook_response(b"{}", ev) == "{}", ev
        assert client.antigravity_hook_response(b'{"hookEventName": "Stop"}') == '{"decision": "stop"}'
        assert client.antigravity_hook_response(b'{"hookEventName": "Stop"}', "PreInvocation") == "{}"
        for raw in (b"not json", b"", b"[1, 2]"):
            assert client.antigravity_hook_response(raw) == "{}"

    def test_reply_is_the_same_whether_or_not_capture_works(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        doc = json.dumps(_doc(repo)).encode()
        label, reply = client.run_antigravity_hook(io.BytesIO(doc), env=dict(os.environ), event_override="Stop",
                                                   spawn=False)
        assert reply == '{"decision": "stop"}' and label == "buffered"
        with patch("openshard.adapters.claude_capture_client._inline_hook", side_effect=RuntimeError("boom")):
            label, reply = client.run_antigravity_hook(io.BytesIO(doc), env=dict(os.environ),
                                                       event_override="Stop", spawn=False)
        assert reply == '{"decision": "stop"}' and label == "error"
        label, reply = client.run_antigravity_hook(io.BytesIO(b"{ nope"), env=dict(os.environ),
                                                   event_override="PostToolUse", spawn=False)
        assert reply == "{}" and label == "ignored"


# ---------------------------------------------------------------------------
# Service path: POST /hooks/antigravity (authenticated, queued, folded behind)
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
                            hook_path=client.ANTIGRAVITY_HOOK_PATH)


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
        steps = [
            ("PreInvocation", _doc(via_http, invocationNum=0)),
            ("PostToolUse", _tool(via_http, "view_file", {"AbsolutePath": str(via_http / "README.md")}, error="")),
            ("PostToolUse", _tool(via_http, "write_to_file",
                                  {"TargetFile": str(via_http / "calc.py"), "CodeContent": FILE_BODY + SECRET},
                                  error="")),
            ("PreInvocation", _doc(via_http, invocationNum=1)),
            ("PostToolUse", _tool(via_http, "run_command",
                                  {"CommandLine": "python -m pytest -q", "Cwd": str(via_http)}, error="")),
            ("PreInvocation", _doc(via_http, invocationNum=2, modelName=CLAUDE)),
            ("PostToolUse", _tool(via_http, "run_command", {"CommandLine": "git status"}, modelName=CLAUDE,
                                  error=f"exit status 1 {SECRET}")),
            ("PostToolUse", _tool(via_http, "mcp_github_create_issue", {"title": SECRET}, modelName=CLAUDE,
                                  error="")),
            ("Stop", _doc(via_http, modelName=CLAUDE, terminationReason="model_stop", fullyIdle=True, error="",
                          executionNum=1)),
        ]
        for i, (event, doc) in enumerate(steps):
            if i == 2:
                (via_http / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            assert _post(service.port, event, doc), event
        _drive_inline(via_inline)
        assert _wait_for(lambda: bool(_lines(via_http)) and _lines(via_http)[0]["capture"]["turn_count"] == 1)
        assert service.server.recorder.wait_idle(20)
        assert _stable(_lines(via_http)[0]) == _stable(_lines(via_inline)[0])
        raw = (via_http / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert SECRET not in raw and TRANSCRIPT not in raw

    def test_queue_line_is_reduced_and_agent_tagged(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, "PostToolUse",
                     _tool(repo, "write_to_file", {"TargetFile": str(repo / "calc.py"),
                                                   "CodeContent": FILE_BODY + SECRET}, error=""))
        queue_file = repo / ".openshard" / "claude_sessions" / f"antigravity.{SID}{svc.QUEUE_SUFFIX}"
        line = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
        assert line["kind"] == "hook" and line["data"]["agent"] == "antigravity"
        assert line["data"]["event"] == "PostToolUse" and line["data"]["file_target"] == "calc.py"
        assert line["data"]["model_id"] == GEMINI and line["data"]["tool_success"] is True
        text = queue_file.read_text(encoding="utf-8")
        assert SECRET not in text and TRANSCRIPT not in text and "RAW FILE" not in text
        service.server.recorder.resume_processing()

    def test_baseline_is_taken_when_the_first_invocation_is_received(self, service, repo):
        # The worker may replay long after the agent's first edits; with no
        # start hook, the first model invocation must still anchor attribution.
        (repo / "dirty.py").write_text("pre-existing\n", encoding="utf-8")
        service.server.recorder.pause_processing()
        assert _post(service.port, "PreInvocation", _doc(repo, invocationNum=0))
        assert _post(service.port, "PreInvocation", _doc(repo, invocationNum=1))
        (repo / "made.py").write_text("x = 1\n", encoding="utf-8")
        assert _post(service.port, "Stop", _doc(repo, fullyIdle=True, error=""))
        service.server.recorder.resume_processing()
        def _first_line_has_one_turn() -> bool:
            # Read once per poll. On Windows the recorder atomically replaces
            # runs.jsonl; two back-to-back reads can straddle that replace and
            # make the second read briefly observe an empty file.
            lines = _lines(repo)
            return bool(lines) and lines[0]["capture"]["turn_count"] == 1

        assert _wait_for(_first_line_has_one_turn)
        entry = _lines(repo)[0]
        made = next(f for f in entry["files_detail"] if f["path"] == "made.py")
        assert made["attribution"] == "git_observed" and not made.get("pre_existing")
        dirty = next(f for f in entry["files_detail"] if f["path"] == "dirty.py")
        assert dirty["attribution"] == "pre_existing"  # excluded from this session's changes
        assert entry["capture"]["invocation_count"] == 2

    def test_unauthenticated_post_is_refused(self, service, repo):
        status, _ = client._request("POST", service.port, client.ANTIGRAVITY_HOOK_PATH + "?event=PreInvocation",
                                    json.dumps(_doc(repo)).encode(), {"Content-Type": "application/json"})
        assert status == 401
        # A capability minted for another agent does not authorise Antigravity events.
        from openshard.adapters.capture_auth import TOKEN_HEADER, repo_capability

        token = client._auth_headers(None, None)[TOKEN_HEADER]
        headers = {"Content-Type": "application/json", TOKEN_HEADER: repo_capability(token, repo, "claude_code")}
        status, _ = client._request("POST", service.port, client.ANTIGRAVITY_HOOK_PATH + "?event=PreInvocation",
                                    json.dumps(_doc(repo)).encode(), headers)
        assert status == 401
        assert _lines(repo) == [] and client.health(service.port)["stats"]["queued"] == 0

    def test_unsupported_event_is_ignored_not_errored(self, service, repo):
        assert _post(service.port, "PreToolUse", _tool(repo, "run_command", {"CommandLine": "ls"}))
        assert _post(service.port, "PostInvocation", _doc(repo))
        assert client.health(service.port)["stats"]["queued"] == 0

    def test_hook_command_forwards_and_never_imports_fold_code(self, service, repo, capture_env):
        code = (
            "import sys, os; from openshard.adapters.claude_capture_client import run_antigravity_hook; "
            "label, reply = run_antigravity_hook(sys.stdin, env=dict(os.environ), event_override='PreInvocation'); "
            "print(label); print(reply); print(sorted(m for m in sys.modules if m.startswith('openshard')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc(repo, invocationNum=0)),
            capture_output=True, text=True, timeout=60, env=capture_env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == "forwarded" and out[1] == "{}"
        assert "openshard.adapters.claude_hooks" not in result.stdout
        assert "openshard.adapters.antigravity_hooks" not in result.stdout
        assert _wait_for(lambda: bool(_lines(repo)))
        assert _lines(repo)[0]["executor"] == "antigravity_hooks"

    def test_no_spawn_falls_back_inline(self, capture_env, repo):
        stream = io.BytesIO(json.dumps(_doc(repo, invocationNum=0)).encode())
        label, reply = client.run_antigravity_hook(stream, env=capture_env, event_override="PreInvocation",
                                                   spawn=False)
        assert label == "record_created" and reply == "{}"
        assert _lines(repo)[0]["executor"] == "antigravity_hooks"

    def test_blocking_path_stays_within_budget(self, service, repo):
        # The per-model-call PreInvocation is the hottest Antigravity hook:
        # its server-side work must stay validate + reduce + fsync, never a fold.
        assert _post(service.port, "PreInvocation", _doc(repo, invocationNum=0))
        p50_budget, p95_budget = (60, 250) if sys.platform == "win32" else (25, 50)
        for attempt in range(1, 4):
            roundtrips: list[float] = []
            for i in range(40):
                event, doc = (("PreInvocation", _doc(repo, invocationNum=i)) if i % 2 else
                              ("PostToolUse", _tool(repo, "run_command", {"CommandLine": f"echo {i}"}, error="")))
                t0 = time.perf_counter()
                assert _post(service.port, event, doc)
                roundtrips.append(time.perf_counter() - t0)
            service.server.recorder.wait_idle(60)
            roundtrips.sort()
            p50_ms = roundtrips[len(roundtrips) // 2] * 1000
            p95_ms = roundtrips[int(round(0.95 * (len(roundtrips) - 1)))] * 1000
            if p50_ms < p50_budget and p95_ms < p95_budget:
                break
            if attempt == 3:
                assert p50_ms < p50_budget and p95_ms < p95_budget, (p50_ms, p95_ms)
        # Invocations are folded behind the caller, at the next turn boundary.
        assert _post(service.port, "Stop", _doc(repo, fullyIdle=True, error=""))
        assert _wait_for(lambda: bool(_lines(repo)) and _lines(repo)[0]["capture"]["turn_count"] == 1)
        assert _lines(repo)[0]["capture"]["invocation_count"] >= 21


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class TestInstaller:
    def test_fresh_install_writes_the_named_hook_and_excludes_file_from_git(self, repo):
        result = install_antigravity_hooks(repo_root=repo)
        assert result.status == "installed", result.message
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert list(data) == [HOOK_NAME] and data[HOOK_NAME] == build_hook_config()
        hook = data[HOOK_NAME]
        assert set(hook) == set(HOOK_EVENTS) == {"PreInvocation", "PostToolUse", "Stop"}
        assert "PreToolUse" not in hook  # a permission gate OpenShard never needs
        assert hook["PostToolUse"] == [{"matcher": ".*", "hooks": [
            {"type": "command", "command": f"{HOOK_COMMAND} --event PostToolUse"}]}]
        for event in ("PreInvocation", "Stop"):
            assert hook[event] == [{"type": "command", "command": f"{HOOK_COMMAND} --event {event}"}]
        assert installed_antigravity_events(data) == list(HOOK_EVENTS)
        check = subprocess.run(["git", "check-ignore", "-q", HOOKS_RELPATH.as_posix()], cwd=repo)
        assert check.returncode == 0

    def test_idempotent(self, repo):
        assert install_antigravity_hooks(repo_root=repo).status == "installed"
        before = (repo / HOOKS_RELPATH).read_bytes()
        again = install_antigravity_hooks(repo_root=repo)
        assert again.status == "already_installed"
        assert all(v == "unchanged" for v in again.events.values())
        assert (repo / HOOKS_RELPATH).read_bytes() == before

    def test_preserves_other_named_hooks_and_updates_a_stale_one_of_ours(self, repo):
        (repo / ".agents").mkdir()
        other = {"PreToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "./guard.sh"}]}]}
        (repo / HOOKS_RELPATH).write_text(json.dumps({
            "guard": other,
            HOOK_NAME: {"Stop": [{"type": "command", "command": HOOK_COMMAND}], "enabled": True},
        }), encoding="utf-8")
        result = install_antigravity_hooks(repo_root=repo)
        assert result.status == "updated"
        assert result.events["Stop"] == "updated" and result.events["PreInvocation"] == "added"
        data = json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))
        assert data["guard"] == other
        assert data[HOOK_NAME]["enabled"] is True
        assert data[HOOK_NAME]["Stop"] == build_hook_config()["Stop"]
        # A pre-existing file is not force-excluded from git.
        exclude = repo / ".git" / "info" / "exclude"
        assert not exclude.exists() or HOOKS_RELPATH.as_posix() not in exclude.read_text(encoding="utf-8")

    def test_refuses_to_clobber(self, repo):
        (repo / ".agents").mkdir()
        (repo / HOOKS_RELPATH).write_text("{ not json", encoding="utf-8")
        assert install_antigravity_hooks(repo_root=repo).status == "error"
        assert (repo / HOOKS_RELPATH).read_text(encoding="utf-8") == "{ not json"
        for bad in ({HOOK_NAME: []}, {HOOK_NAME: {"Stop": [{"type": "command", "command": "./mine.sh"}]}}):
            (repo / HOOKS_RELPATH).write_text(json.dumps(bad), encoding="utf-8")
            result = install_antigravity_hooks(repo_root=repo)
            assert result.status == "error" and "will not modify" in result.message, bad
            assert json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8")) == bad
            assert uninstall_antigravity_hooks(repo_root=repo).status == "not_installed"

    def test_uninstall_removes_only_ours(self, repo):
        (repo / ".agents").mkdir()
        (repo / HOOKS_RELPATH).write_text(json.dumps({"guard": {"Stop": [{"command": "./g.sh"}]}}),
                                          encoding="utf-8")
        install_antigravity_hooks(repo_root=repo)
        result = uninstall_antigravity_hooks(repo_root=repo)
        assert result.status == "removed"
        assert json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8")) == {
            "guard": {"Stop": [{"command": "./g.sh"}]}
        }
        assert uninstall_antigravity_hooks(repo_root=repo).status == "not_installed"
        _drive_inline(repo)
        uninstall_antigravity_hooks(repo_root=repo)
        assert len(_lines(repo)) == 1  # history is never touched


# ---------------------------------------------------------------------------
# CLI: hooks antigravity, capture install/uninstall antigravity, setup, doctor
# ---------------------------------------------------------------------------


def _which(name: str):
    return {"agy": "/usr/local/bin/agy", "openshard": "/usr/local/bin/openshard"}.get(name)


class TestCli:
    def test_hooks_antigravity_command_records_inline_and_replies(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["hooks", "antigravity", "--event", "PreInvocation"],
                               input=json.dumps(_doc(repo, invocationNum=0)))
        assert result.exit_code == 0, result.output
        assert result.output == "{}\n"
        result = runner.invoke(cli, ["hooks", "antigravity", "--event", "Stop", "--no-spawn"],
                               input=json.dumps(_doc(repo, fullyIdle=True, error="")))
        assert result.exit_code == 0 and result.output == '{"decision": "stop"}\n'
        entry = _lines(repo)[0]
        assert entry["executor"] == "antigravity_hooks" and entry["capture"]["turn_count"] == 1
        result = runner.invoke(cli, ["hooks", "antigravity", "--event", "Stop"], input="not json")
        assert result.exit_code == 0 and result.output == '{"decision": "stop"}\n'

    def test_entrypoint_fast_path_handles_hooks_antigravity(self, repo):
        code = (
            "import sys; sys.argv = ['openshard', 'hooks', 'antigravity', '--event', 'Stop', '--no-spawn']; "
            "from openshard.cli.entrypoint import main; main(); "
            "print(sorted(m for m in sys.modules if m.startswith('openshard.cli')))"
        )
        env = {**os.environ, "OPENSHARD_CAPTURE_DISABLE": "1"}
        handle_hook(_doc(repo), env={}, agent="antigravity", event_override="PreInvocation")
        result = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(_doc(repo, fullyIdle=True)),
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout.splitlines()
        assert out[0] == '{"decision": "stop"}'
        assert "openshard.cli.main" not in out[1]
        assert _lines(repo)[0]["capture"]["turn_count"] == 1

    def test_capture_install_and_uninstall_antigravity(self, repo):
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "antigravity", "--repo-path", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "installed" and data["configured"] is True
        assert (repo / HOOKS_RELPATH).exists()
        result = runner.invoke(cli, ["capture", "install", "antigravity", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "already installed" in result.output
        assert "Google Antigravity" in result.output
        result = runner.invoke(cli, ["capture", "uninstall", "antigravity", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "removed" in result.output
        assert HOOK_NAME not in json.loads((repo / HOOKS_RELPATH).read_text(encoding="utf-8"))

    def test_setup_configures_antigravity_without_claude(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["readiness"] == "ready"
        assert data["agents"]["antigravity"]["status"] == "installed"
        assert data["configured_agents"] == ["antigravity"]
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--repo-path", str(repo), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Antigravity:" in result.output and "Use Google Antigravity normally" in result.output

    def test_setup_without_antigravity_points_at_capture_install(self, repo):
        with patch("shutil.which", return_value=None):
            result = CliRunner().invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        data = json.loads(result.output)
        assert any("openshard capture install antigravity" in s for s in data["next_steps"])

    def test_doctor_reports_antigravity_independently(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            before = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            install_antigravity_hooks(repo_root=repo)
            after = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert before.exit_code == 0 and after.exit_code == 0, after.output
        assert json.loads(before.output)["antigravity"]["configured"] is False
        status = json.loads(after.output)["antigravity"]
        assert status["configured"] is True and status["cli_available"] is True
        assert status["events_missing"] == []
        assert "\nGoogle Antigravity\n" in human.output
        with patch("shutil.which", return_value=None):
            absent = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert "openshard capture install antigravity" in absent.output

    def test_setup_agent_snapshot_includes_antigravity(self, repo):
        with patch("shutil.which", side_effect=_which):
            result = CliRunner().invoke(cli, ["setup", "--agent", "--json", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["antigravity"]["cli_available"] is True and data["antigravity"]["configured"] is False
