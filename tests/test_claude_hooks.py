"""Tests for openshard.adapters.claude_hooks (Demo v1 PR5: Claude Code auto capture).

Every test drives the adapter with synthetic Claude Code hook payloads in a
throw-away git repository. The developer's real Claude Code configuration,
``CLAUDE_PROJECT_DIR`` and history are never read: the environment is
always passed explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks import (
    EXECUTOR,
    HookOutcome,
    buffer_path,
    build_hook_entry,
    extract_hook_payload,
    extract_status_payload,
    handle_claude_hook,
    handle_claude_status,
    parse_hook_payload,
    resolve_repo_root,
    run_hook_from_stream,
    run_status_from_stream,
    sanitize_task_excerpt,
    summarize_command,
    sweep_stale_buffers,
)
from openshard.history.event import (
    EVENT_FILE_CHANGED,
    EVENT_RUN_COMPLETED,
    EVENT_SESSION_ACTIVITY,
    EVENT_SESSION_STARTED,
    EVENT_TOOL_INVOKED,
    EVENT_VERIFICATION_FAILED,
    EVENT_VERIFICATION_PASSED,
    EVIDENCE_AGENT_REPORTED,
    EVIDENCE_DIRECTLY_OBSERVED,
    EVIDENCE_GIT_OBSERVED,
    SOURCE_CLAUDE_CODE_HOOKS,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    events_from_entry,
)
from openshard.history.query import (
    get_receipt,
    get_shard,
    list_shards,
    relevant_context,
    search_history,
)
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"
SID2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECRET = "sk-ant-api03-SECRETSECRET12345678901234567890"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository with one commit, in a directory containing a space."""
    root = tmp_path / "my repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _payload(event: str, repo: Path, session_id: str = SID, **fields) -> dict:
    base = {
        "session_id": session_id,
        "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": event,
    }
    base.update(fields)
    return base


def _run(repo: Path, event: str, session_id: str = SID, **fields) -> HookOutcome:
    return handle_claude_hook(_payload(event, repo, session_id, **fields), env={"CLAUDE_PROJECT_DIR": str(repo)})


def _status_payload(
    repo: Path,
    session_id: str = SID,
    *,
    model_id: str | None = "claude-sonnet-5",
    model_display: str | None = "Claude Sonnet 5",
    cost_total: float | None = 0.0,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    cache_read: int | None = None,
    cache_creation: int | None = None,
) -> dict:
    data: dict = {"session_id": session_id, "cwd": str(repo)}
    if model_id is not None:
        data["model"] = {"id": model_id, "display_name": model_display}
    if cost_total is not None:
        data["cost"] = {"total_cost_usd": cost_total}
    if tokens_input is not None or tokens_output is not None:
        usage = {"input_tokens": tokens_input, "output_tokens": tokens_output}
        if cache_read is not None:
            usage["cache_read_input_tokens"] = cache_read
        if cache_creation is not None:
            usage["cache_creation_input_tokens"] = cache_creation
        data["context_window"] = {"current_usage": usage}
    return data


def _status(repo: Path, session_id: str = SID, **kwargs) -> str:
    return handle_claude_status(_status_payload(repo, session_id, **kwargs), env={"CLAUDE_PROJECT_DIR": str(repo)})


def _runs_lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _raw(repo: Path) -> str:
    path = repo / ".openshard" / "runs.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _session(repo: Path, *, with_tools: bool = True, end: bool = True) -> dict:
    """Drive a typical session: start, prompt, edits, bash, stop, end."""
    _run(repo, "SessionStart", source="startup")
    _run(repo, "UserPromptSubmit", prompt="Add a calculator module with unit tests")
    if with_tools:
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (repo / "README.md").write_text("hello\ncalc\n", encoding="utf-8")
        _run(repo, "PostToolUse", tool_name="Write",
             tool_input={"file_path": str(repo / "calc.py"), "content": "RAW FILE CONTENT"},
             tool_response={"filePath": str(repo / "calc.py"), "content": "RAW FILE CONTENT"})
        _run(repo, "PostToolUse", tool_name="Edit",
             tool_input={"file_path": str(repo / "README.md"), "old_string": "a", "new_string": "b"})
        _run(repo, "PostToolUse", tool_name="Bash",
             tool_input={"command": "python -m pytest -q", "description": "run tests"},
             tool_response={"stdout": "3 passed RAW STDOUT", "stderr": ""})
    _run(repo, "Stop", last_assistant_message="All done. RAW ASSISTANT TEXT", stop_hook_active=False)
    if end:
        _run(repo, "SessionEnd", reason="prompt_input_exit")
    lines = _runs_lines(repo)
    assert len(lines) == 1
    return lines[0]


def _events(entry: dict, event_type: str) -> list[dict]:
    return [e for e in entry["events"] if e["event_type"] == event_type]


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_empty_and_malformed_payloads_return_none(self):
        assert parse_hook_payload(None) is None
        assert parse_hook_payload(b"") is None
        assert parse_hook_payload("   \n") is None
        assert parse_hook_payload("{not json") is None
        assert parse_hook_payload(b"\xff\xfe{") is None

    def test_non_object_json_returns_none(self):
        assert parse_hook_payload("[1, 2]") is None
        assert parse_hook_payload('"string"') is None

    def test_bytes_and_str_both_parse(self):
        assert parse_hook_payload(b'{"a": 1}') == {"a": 1}
        assert parse_hook_payload('{"a": 1}') == {"a": 1}

    def test_unknown_fields_are_ignored(self, repo: Path):
        data = _payload("PostToolUse", repo, tool_name="Edit",
                        tool_input={"file_path": "x.py", "surprise": {"deep": [1]}},
                        brand_new_field={"nested": True}, tool_response={"big": "blob"})
        p = extract_hook_payload(data)
        assert p is not None
        assert p.tool_name == "Edit"
        assert p.file_path == "x.py"
        assert not hasattr(p, "brand_new_field")
        assert not hasattr(p, "tool_response")
        assert not hasattr(p, "transcript_path")

    def test_unsupported_event_is_rejected(self, repo: Path):
        assert extract_hook_payload(_payload("PreCompact", repo)) is None
        assert extract_hook_payload({"session_id": SID}) is None

    def test_event_override_used_only_when_payload_lacks_event(self, repo: Path):
        data = _payload("Stop", repo)
        del data["hook_event_name"]
        assert extract_hook_payload(data) is None
        p = extract_hook_payload(data, event_override="Stop")
        assert p is not None and p.event == "Stop"
        p2 = extract_hook_payload(_payload("SessionEnd", repo), event_override="Stop")
        assert p2 is not None and p2.event == "SessionEnd"

    def test_invalid_session_id_is_dropped(self, repo: Path):
        bad = extract_hook_payload(_payload("Stop", repo, session_id="../../etc/passwd"))
        assert bad is not None and bad.session_id is None
        missing = extract_hook_payload(_payload("Stop", repo, session_id=123))
        assert missing is not None and missing.session_id is None

    def test_alternate_prompt_and_reason_field_names(self, repo: Path):
        p = extract_hook_payload(_payload("UserPromptSubmit", repo, user_message="hello"))
        assert p is not None and p.prompt == "hello"
        p2 = extract_hook_payload(_payload("SessionEnd", repo, end_reason="other"))
        assert p2 is not None and p2.reason == "other"


# ---------------------------------------------------------------------------
# Repository resolution
# ---------------------------------------------------------------------------


class TestRepoResolution:
    def test_project_dir_env_wins_over_cwd(self, repo: Path, tmp_path: Path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        p = extract_hook_payload(_payload("Stop", repo, cwd=str(other)))
        assert p is not None
        assert resolve_repo_root(p, {"CLAUDE_PROJECT_DIR": str(repo)}) == repo.resolve()

    def test_cwd_subdirectory_resolves_to_git_root(self, repo: Path):
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        p = extract_hook_payload(_payload("Stop", repo, cwd=str(sub)))
        assert p is not None
        assert resolve_repo_root(p, {}) == repo.resolve()

    def test_unresolvable_directory_is_ignored(self, repo: Path):
        p = extract_hook_payload(_payload("Stop", repo, cwd=str(repo / "does-not-exist")))
        assert p is not None
        assert resolve_repo_root(p, {"CLAUDE_PROJECT_DIR": str(repo / "nope")}) is None
        outcome = handle_claude_hook(
            _payload("Stop", repo, cwd=str(repo / "does-not-exist")), env={},
        )
        assert outcome.action == "ignored"
        assert not (repo / ".openshard").exists()


# ---------------------------------------------------------------------------
# Session lifecycle -> canonical Events -> runs.jsonl
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_session_start_alone_writes_no_record(self, repo: Path):
        out = _run(repo, "SessionStart", source="startup")
        assert out.action == "buffered"
        assert out.shard_id is None
        assert _runs_lines(repo) == []
        assert buffer_path(repo.resolve(), SID).exists()

    def test_first_prompt_creates_new_shard_attempt_one(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        out = _run(repo, "UserPromptSubmit", prompt="Fix the login bug")
        assert out.action == "record_created"
        lines = _runs_lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["shard_id"].startswith("shard-")
        assert entry["attempt_number"] == 1
        assert entry["run_id"].endswith(SID[:8])
        assert entry["executor"] == EXECUTOR
        assert entry["import_source"] == "claude_code"
        assert entry["import_method"] == "openshard_claude_hooks_v0"
        assert entry["task"] == "Fix the login bug"
        assert entry["capture"]["session_id"] == SID
        assert entry["capture"]["status"] == "in_progress"
        assert entry["capture"]["session_end_observed"] is False

    def test_shard_id_is_not_the_claude_session_id(self, repo: Path):
        entry = _session(repo)
        assert entry["shard_id"] != SID
        assert SID not in entry["shard_id"]
        assert entry["capture"]["session_id"] == SID

    def test_full_session_yields_one_record_with_expected_events(self, repo: Path):
        entry = _session(repo)
        assert "events" in entry
        types = [e["event_type"] for e in entry["events"]]
        assert types.count(EVENT_SESSION_STARTED) == 1
        assert types.count(EVENT_RUN_COMPLETED) == 1
        assert len(_events(entry, EVENT_TOOL_INVOKED)) == 3
        assert len(_events(entry, EVENT_FILE_CHANGED)) == 2
        assert all(e["source"] == SOURCE_CLAUDE_CODE_HOOKS for e in entry["events"])
        assert all(e["actor"] == "claude_code" for e in entry["events"])
        assert all(e["raw_content_stored"] is False for e in entry["events"])

    def test_events_are_linked_to_run_shard_attempt(self, repo: Path):
        entry = _session(repo)
        for e in entry["events"]:
            assert e["run_id"] == entry["run_id"]
            assert e["shard_id"] == entry["shard_id"]
            assert e["attempt_number"] == 1
            assert e["occurred_at"]

    def test_session_end_finalizes_and_removes_buffer(self, repo: Path):
        entry = _session(repo)
        assert entry["capture"]["status"] == "ended"
        assert entry["capture"]["session_end_observed"] is True
        assert entry["capture"]["session_end_reason"] == "prompt_input_exit"
        assert not buffer_path(repo.resolve(), SID).exists()
        done = _events(entry, EVENT_RUN_COMPLETED)[0]
        assert done["status"] == STATUS_UNKNOWN  # no verification -> never "passed"
        assert done["evidence"] == EVIDENCE_DIRECTLY_OBSERVED

    def test_counts_and_summary_are_derived_not_model_text(self, repo: Path):
        entry = _session(repo)
        cap = entry["capture"]
        assert cap["prompt_count"] == 1
        assert cap["tool_call_count"] == 3
        assert cap["turn_count"] == 1
        assert cap["task_source"] == "first_user_prompt_excerpt"
        assert "3 tool call(s)" in entry["summary"]
        assert "RAW ASSISTANT TEXT" not in entry["summary"]

    def test_verification_never_fabricated(self, repo: Path):
        entry = _session(repo)
        assert entry["verification_attempted"] is False
        assert entry["verification_passed"] is None
        assert _events(entry, EVENT_VERIFICATION_PASSED) == []
        assert _events(entry, EVENT_VERIFICATION_FAILED) == []
        for blocked in ("estimated_cost", "prompt_tokens", "completion_tokens", "total_tokens"):
            assert blocked not in entry

    def test_compaction_start_is_ignored(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="task")
        before = _runs_lines(repo)[0]
        out = _run(repo, "SessionStart", source="compact")
        assert out.action == "buffered"
        assert "ignored" in out.detail
        assert _runs_lines(repo)[0]["events"] == before["events"]

    def test_resume_before_end_adds_activity_event_and_stays_buffered(self, repo: Path):
        _session(repo, end=False)
        out = _run(repo, "SessionStart", source="resume")
        assert out.action == "buffered"
        _run(repo, "Stop")
        lines = _runs_lines(repo)
        assert len(lines) == 1
        acts = [e["action"] for e in _events(lines[0], EVENT_SESSION_ACTIVITY)]
        assert "Claude Code session resumed" in acts
        assert lines[0]["capture"]["session_end_observed"] is False

    def test_resume_after_end_snapshots_same_record_and_keeps_ended_state(self, repo: Path):
        _session(repo)
        out = _run(repo, "SessionStart", source="resume")
        # The session already ended: the rebuilt buffer is folded and dropped again.
        assert out.action == "record_updated"
        assert not buffer_path(repo.resolve(), SID).exists()
        lines = _runs_lines(repo)
        assert len(lines) == 1
        acts = [e["action"] for e in _events(lines[0], EVENT_SESSION_ACTIVITY)]
        assert "Claude Code session resumed" in acts
        # Ended state is preserved, not reset, by a post-end resume.
        assert lines[0]["capture"]["session_end_observed"] is True

    def test_session_end_without_any_work_records_nothing(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        out = _run(repo, "SessionEnd", reason="other")
        assert out.action == "ignored"
        assert _runs_lines(repo) == []
        assert not buffer_path(repo.resolve(), SID).exists()

    def test_stop_without_work_records_nothing(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        _run(repo, "Stop")
        assert _runs_lines(repo) == []

    def test_two_sessions_two_shards(self, repo: Path):
        _session(repo)
        _run(repo, "UserPromptSubmit", session_id=SID2, prompt="second task")
        _run(repo, "SessionEnd", session_id=SID2, reason="clear")
        lines = _runs_lines(repo)
        assert len(lines) == 2
        assert lines[0]["shard_id"] != lines[1]["shard_id"]
        assert lines[0]["capture"]["session_id"] == SID
        assert lines[1]["capture"]["session_id"] == SID2

    def test_stop_hook_active_still_snapshots_without_error(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        out = _run(repo, "Stop", stop_hook_active=True)
        assert out.action == "record_updated"


# ---------------------------------------------------------------------------
# Evidence levels
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_lifecycle_events_are_directly_observed(self, repo: Path):
        entry = _session(repo)
        for t in (EVENT_SESSION_STARTED, EVENT_SESSION_ACTIVITY, EVENT_RUN_COMPLETED):
            for e in _events(entry, t):
                assert e["evidence"] == EVIDENCE_DIRECTLY_OBSERVED, t

    def test_tool_events_are_agent_reported(self, repo: Path):
        entry = _session(repo)
        for e in _events(entry, EVENT_TOOL_INVOKED):
            assert e["evidence"] == EVIDENCE_AGENT_REPORTED

    def test_git_file_events_are_git_observed(self, repo: Path):
        entry = _session(repo)
        files = _events(entry, EVENT_FILE_CHANGED)
        assert {e["target"] for e in files} == {"calc.py", "README.md"}
        for e in files:
            assert e["evidence"] == EVIDENCE_GIT_OBSERVED
            assert e["status"] == STATUS_UNKNOWN
        assert entry["files_source"] == "git_diff_inferred"

    def test_edit_tool_passed_bash_unknown(self, repo: Path):
        entry = _session(repo)
        tools = _events(entry, EVENT_TOOL_INVOKED)
        by_target = {e["target"]: e for e in tools}
        assert by_target["calc.py"]["status"] == STATUS_PASSED
        assert by_target["README.md"]["status"] == STATUS_PASSED
        bash = next(e for e in tools if e["action"].startswith("Bash:"))
        assert bash["status"] == STATUS_UNKNOWN  # OpenShard did not observe the exit code
        assert bash["metadata"]["command_kind"] == "test"
        assert bash["target"] == "python"

    def test_tool_failure_is_failed_without_error_text(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _run(repo, "PostToolUseFailure", tool_name="Bash",
             tool_input={"command": "npm test"}, error="Command timed out RAW ERROR", error_code=None)
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        ev = _events(entry, EVENT_TOOL_INVOKED)[0]
        assert ev["status"] == STATUS_FAILED
        assert "RAW ERROR" not in _raw(repo)
        assert entry["capture"]["tool_failure_count"] == 1

    def test_never_independently_verified(self, repo: Path):
        entry = _session(repo)
        assert all(e["evidence"] != "independently_verified" for e in entry["events"])

    def test_shard_identity_is_external_partial(self, repo: Path):
        entry = _session(repo)
        receipt = build_shard_receipt(entry)
        assert receipt.agent == "Claude Code (external)"
        assert receipt.shard is not None
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED
        assert receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.status == "No checks run"


# ---------------------------------------------------------------------------
# Files and paths
# ---------------------------------------------------------------------------


class TestFiles:
    def test_new_untracked_file_recorded_as_create(self, repo: Path):
        entry = _session(repo)
        detail = {f["path"]: f["change_type"] for f in entry["files_detail"]}
        assert detail == {"calc.py": "create", "README.md": "update"}
        assert entry["files_created"] == 1 and entry["files_updated"] == 1

    def test_commit_during_session_still_seen_via_start_head(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="task")
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "new.py")
        _git(repo, "commit", "-q", "-m", "work")
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert [f["path"] for f in entry["files_detail"]] == ["new.py"]

    def test_openshard_store_never_counts_as_changed_file(self, repo: Path):
        # A repository that does not ignore .openshard/ must not see the
        # history store (or Claude's local settings) as the task's work.
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        _run(repo, "UserPromptSubmit", prompt="task")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "tracks local state")
        (repo / ".openshard" / "note.txt").write_text("x", encoding="utf-8")
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert entry["files_detail"] == []

    def test_paths_are_repo_relative_posix(self, repo: Path):
        sub = repo / "pkg" / "mod"
        sub.mkdir(parents=True)
        (sub / "a.py").write_text("", encoding="utf-8")
        _run(repo, "UserPromptSubmit", prompt="task")
        _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(sub / "a.py")})
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        tool = _events(entry, EVENT_TOOL_INVOKED)[0]
        assert tool["target"] == "pkg/mod/a.py"
        assert "\\" not in tool["target"]

    def test_outside_repo_path_is_dropped_entirely(self, repo: Path, tmp_path: Path):
        outside = tmp_path / "secret-notes.txt"
        outside.write_text("x", encoding="utf-8")
        _run(repo, "UserPromptSubmit", prompt="task")
        _run(repo, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(outside)})
        _run(repo, "Stop")
        raw = _raw(repo)
        assert "secret-notes" not in raw
        entry = _runs_lines(repo)[0]
        tool = _events(entry, EVENT_TOOL_INVOKED)[0]
        assert tool["target"] is None
        assert tool["metadata"].get("path_dropped") == "outside repository"

    def test_foreign_style_absolute_paths_are_dropped(self, repo: Path):
        assert ch._to_repo_relative("C:\\Users\\someone\\proj\\a.py", repo) is None
        assert ch._to_repo_relative("/home/someone/proj/a.py", repo) is None
        assert ch._to_repo_relative("", repo) is None
        assert ch._to_repo_relative(None, repo) is None

    def test_relative_path_is_anchored_at_repo_root(self, repo: Path):
        assert ch._to_repo_relative("src/x.py", repo) == "src/x.py"

    def test_hook_reported_files_used_when_git_unavailable(self, tmp_path: Path):
        root = tmp_path / "plain"
        root.mkdir()
        env = {"CLAUDE_PROJECT_DIR": str(root)}
        # A temp dir can sit under a real git checkout (a developer's home
        # directory, say); pin "no enclosing repository" so the write can
        # never escape *root*, and make every git call fail.
        with patch("openshard.adapters.claude_mcp_install.find_repo_root", return_value=None), \
             patch("openshard.adapters.claude_code_import.subprocess.run",
                   side_effect=FileNotFoundError("no git")):
            handle_claude_hook(_payload("UserPromptSubmit", root, prompt="task"), env=env)
            handle_claude_hook(_payload("PostToolUse", root, tool_name="Write",
                                        tool_input={"file_path": str(root / "made.py")}), env=env)
            handle_claude_hook(_payload("Stop", root), env=env)
        entry = _runs_lines(root)[0]
        assert entry["files_source"] == "claude_hook_reported"
        assert entry["files_detail"] == [
            {"path": "made.py", "change_type": "create", "summary": "reported by Claude Code hook"}
        ]
        fe = _events(entry, EVENT_FILE_CHANGED)[0]
        assert fe["evidence"] == EVIDENCE_AGENT_REPORTED

    def test_no_absolute_paths_anywhere_in_record(self, repo: Path):
        _session(repo)
        raw = _raw(repo)
        assert str(repo) not in raw
        assert str(repo.resolve()) not in raw
        assert repo.resolve().as_posix() not in raw
        assert "transcript.jsonl" not in raw


# ---------------------------------------------------------------------------
# Privacy / secrets
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_secret_in_prompt_is_scrubbed(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt=f"Use {SECRET} to call the API and fix auth")
        raw = _raw(repo)
        assert SECRET not in raw
        assert "fix auth" in _runs_lines(repo)[0]["task"]

    def test_prompt_excerpt_is_bounded_and_only_first_prompt_kept(self, repo: Path):
        long_prompt = "refactor " * 200 + "UNIQUE_TAIL_MARKER"
        _run(repo, "UserPromptSubmit", prompt=long_prompt)
        _run(repo, "UserPromptSubmit", prompt="SECOND_PROMPT_MARKER please continue")
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert len(entry["task"]) <= 300
        raw = _raw(repo)
        assert "UNIQUE_TAIL_MARKER" not in raw
        assert "SECOND_PROMPT_MARKER" not in raw
        assert entry["capture"]["prompt_count"] == 2

    def test_no_transcript_file_content_or_assistant_text_stored(self, repo: Path):
        _session(repo)
        raw = _raw(repo)
        for needle in ("RAW FILE CONTENT", "RAW STDOUT", "RAW ASSISTANT TEXT", "transcript"):
            assert needle not in raw, needle
        entry = _runs_lines(repo)[0]
        for blocked in ("raw_prompt", "prompt_text", "transcript", "raw_transcript", "model_output",
                        "raw_file_content", "file_content", "raw_diff"):
            assert blocked not in entry

    def test_secret_in_bash_command_is_redacted(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _run(repo, "PostToolUse", tool_name="Bash",
             tool_input={"command": f'curl -H "Authorization: Bearer {SECRET}" https://api.example.com'})
        _run(repo, "Stop")
        raw = _raw(repo)
        assert SECRET not in raw
        assert "Bearer" not in raw

    def test_summarize_command_shapes(self):
        action, target, kind = summarize_command("python -m pytest tests/ -q")
        assert action == "Bash: python -m pytest tests/ -q" and target == "python" and kind == "test"
        _, _, lint = summarize_command("ruff check .")
        assert lint == "lint"
        _, _, other = summarize_command("ls -la")
        assert other == "other"
        assert summarize_command(None) == ("Bash command", None, "other")
        long_action, _, _ = summarize_command("echo " + "x" * 500)
        assert len(long_action) <= len("Bash: ") + 100

    def test_no_environment_variables_stored(self, repo: Path):
        env = {"CLAUDE_PROJECT_DIR": str(repo), "MY_API_KEY": "topsecretvalue123", "HOME": "/home/x"}
        handle_claude_hook(_payload("UserPromptSubmit", repo, prompt="task"), env=env)
        raw = _raw(repo)
        assert "topsecretvalue123" not in raw
        assert "MY_API_KEY" not in raw

    def test_task_excerpt_helper(self):
        assert sanitize_task_excerpt(None) is None
        assert sanitize_task_excerpt("   ") is None
        assert sanitize_task_excerpt("a\x00b\n\tc") == "a b c"
        assert sanitize_task_excerpt("Fix\nthe   bug") == "Fix the bug"


# ---------------------------------------------------------------------------
# Interrupted / duplicated / concurrent-ish hook delivery
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_interrupted_session_keeps_last_snapshot_honestly(self, repo: Path):
        entry = _session(repo, end=False)
        assert entry["capture"]["session_end_observed"] is False
        assert entry["capture"]["status"] == "in_progress"
        assert _events(entry, EVENT_RUN_COMPLETED) == []
        assert len(_events(entry, EVENT_TOOL_INVOKED)) == 3  # folded at Stop
        assert buffer_path(repo.resolve(), SID).exists()

    def test_tool_hooks_snapshot_periodically_without_stop(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")  # snapshots at record creation
        (repo / "a.py").write_text("", encoding="utf-8")
        # Within the interval a tool hook only stages...
        out1 = _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "a.py")})
        assert out1.action == "buffered"
        assert _events(_runs_lines(repo)[0], EVENT_TOOL_INVOKED) == []
        # ...once the interval has elapsed since the last snapshot, it folds.
        path = buffer_path(repo.resolve(), SID)
        buf = json.loads(path.read_text(encoding="utf-8"))
        buf["last_fold_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(buf), encoding="utf-8")
        out2 = _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "a.py")})
        assert out2.action == "record_updated"
        entry = _runs_lines(repo)[0]
        assert len(_events(entry, EVENT_TOOL_INVOKED)) == 2
        assert [f["path"] for f in entry["files_detail"]] == ["a.py"]
        # And the very next one stages again.
        out3 = _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "a.py")})
        assert out3.action == "buffered"

    def test_repeated_session_end_does_not_duplicate_record(self, repo: Path):
        _session(repo)
        _run(repo, "SessionEnd", reason="prompt_input_exit")
        _run(repo, "SessionEnd", reason="prompt_input_exit")
        assert len(_runs_lines(repo)) == 1

    def test_late_stop_after_session_end_rebuilds_from_record(self, repo: Path):
        entry = _session(repo)
        before = len(entry["events"])
        out = _run(repo, "Stop")  # a background Stop finishing after SessionEnd
        assert out.action == "record_updated"
        assert not buffer_path(repo.resolve(), SID).exists()  # not left behind
        lines = _runs_lines(repo)
        assert len(lines) == 1
        after = lines[0]
        assert len(after["events"]) == before + 1
        assert len(_events(after, EVENT_TOOL_INVOKED)) == 3
        assert after["task"] == entry["task"]
        assert after["shard_id"] == entry["shard_id"]
        assert after["capture"]["session_end_observed"] is True
        git_ids_before = {e["event_id"] for e in _events(entry, EVENT_FILE_CHANGED)}
        git_ids_after = {e["event_id"] for e in _events(after, EVENT_FILE_CHANGED)}
        assert git_ids_before == git_ids_after  # stable across folds

    def test_duplicate_tool_payload_counts_twice_but_one_record(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        payload = _payload("PostToolUse", repo, tool_name="Bash", tool_input={"command": "ls"})
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        handle_claude_hook(payload, env=env)
        handle_claude_hook(payload, env=env)
        _run(repo, "Stop")
        lines = _runs_lines(repo)
        assert len(lines) == 1
        assert lines[0]["capture"]["tool_call_count"] == 2

    def test_event_buffer_is_bounded(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        for _ in range(ch._MAX_BUFFERED_EVENTS + 25):
            _run(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        # Staged hook events are capped; git file events (max 20) are added on top.
        assert len(entry["events"]) <= ch._MAX_BUFFERED_EVENTS + 20
        assert entry["capture"]["hook_events_dropped"] > 0
        assert entry["capture"]["tool_call_count"] == ch._MAX_BUFFERED_EVENTS + 25

    def test_corrupt_buffer_is_replaced_not_fatal(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        buffer_path(repo.resolve(), SID).write_text("{not json", encoding="utf-8")
        out = _run(repo, "UserPromptSubmit", prompt="task")
        assert out.action == "record_created"

    def test_malformed_runs_line_is_preserved(self, repo: Path):
        store = repo / ".openshard"
        store.mkdir()
        (store / "runs.jsonl").write_text('{"legacy": true}\nnot json at all\n', encoding="utf-8")
        _run(repo, "UserPromptSubmit", prompt="task")
        _run(repo, "Stop")
        _run(repo, "SessionEnd", reason="other")
        raw = _raw(repo).splitlines()
        assert raw[0] == '{"legacy": true}'
        assert raw[1] == "not json at all"
        assert len(raw) == 3
        assert json.loads(raw[2])["capture"]["session_id"] == SID

    def test_stale_buffer_swept_on_next_session_start(self, repo: Path):
        _session(repo, end=False)
        path = buffer_path(repo.resolve(), SID)
        buf = json.loads(path.read_text(encoding="utf-8"))
        buf["last_activity_at"] = "2000-01-01T00:00:00Z"
        # Add an unflushed tool event so the sweep visibly recovers evidence.
        buf["events"].append(dict(buf["events"][-1], event_id="recovered-evt", event_type="tool.invoked"))
        path.write_text(json.dumps(buf), encoding="utf-8")
        _run(repo, "SessionStart", session_id=SID2, source="startup")
        assert not path.exists()
        lines = _runs_lines(repo)
        assert len(lines) == 1
        assert lines[0]["capture"]["session_end_observed"] is False
        assert any(e["event_id"] == "recovered-evt" for e in lines[0]["events"])

    def test_sweep_leaves_fresh_and_ended_buffers_alone(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        assert sweep_stale_buffers(repo.resolve()) == []
        assert buffer_path(repo.resolve(), SID).exists()

    def test_handler_never_raises(self, repo: Path):
        with patch.object(ch, "_load_or_create_buffer", side_effect=RuntimeError("boom")):
            out = _run(repo, "Stop")
        assert out.action == "error"
        assert "boom" not in out.detail  # only the exception class name

    def test_stream_runner_handles_empty_and_bad_input(self, repo: Path):
        import io
        assert run_hook_from_stream(io.BytesIO(b""), env={}).action == "ignored"
        assert run_hook_from_stream(io.StringIO("nope"), env={}).action == "ignored"

        class Broken:
            def read(self):
                raise OSError("closed")

        assert run_hook_from_stream(Broken(), env={}).action == "ignored"


# ---------------------------------------------------------------------------
# Visible through the existing history / receipt / retrieval layer
# ---------------------------------------------------------------------------


class TestHistoryIntegration:
    def test_visible_via_list_and_get(self, repo: Path):
        entry = _session(repo)
        shards = list_shards(repo_path=repo)
        assert [s.shard_id for s in shards] == [entry["shard_id"]]
        shard = get_shard(entry["shard_id"], repo_path=repo)
        assert shard.agent == "Claude Code (external)"
        assert shard.task_short.startswith("Add a calculator")

    def test_receipt_is_the_normal_receipt(self, repo: Path):
        entry = _session(repo)
        receipt = get_receipt(entry["shard_id"], repo_path=repo)
        assert receipt.run_id == entry["run_id"]
        assert receipt.attempt_number == 1
        assert set(receipt.files_touched) == {"calc.py", "README.md"}
        assert {e.event_id for e in receipt.events} == {e["event_id"] for e in entry["events"]}
        assert receipt.checks_display == "Not run"
        assert receipt.cost_display == "Not recorded"
        by_run = get_receipt(run_id=entry["run_id"], repo_path=repo)
        assert by_run.shard_id == entry["shard_id"]

    def test_events_from_entry_uses_embedded_events_verbatim(self, repo: Path):
        entry = _session(repo)
        derived = events_from_entry(entry)
        assert {e.event_id for e in derived} == {e["event_id"] for e in entry["events"]}

    def test_search_history_finds_it(self, repo: Path):
        entry = _session(repo)
        hits = search_history("calculator", repo_path=repo)
        assert [h.shard.shard_id for h in hits] == [entry["shard_id"]]

    def test_relevant_context_finds_it_for_related_task(self, repo: Path):
        entry = _session(repo)
        ctx = relevant_context("add subtraction to the calculator module", repo_path=repo)
        assert [m.shard.shard_id for m in ctx.matches] == [entry["shard_id"]]
        assert "calc.py" in ctx.context_text
        assert "RAW" not in ctx.context_text
        unrelated = relevant_context("rotate kubernetes certificates", repo_path=repo)
        assert unrelated.matches == []

    def test_load_runs_coercion_keeps_capture_block(self, repo: Path):
        from openshard.history.metrics import load_runs

        _session(repo)
        runs = load_runs(repo)
        assert runs[0]["capture"]["session_id"] == SID
        assert runs[0]["content_hash"].startswith("sha256:")

    def test_build_hook_entry_direct_is_coerced(self, repo: Path):
        buf = ch._new_buffer(SID, repo, "SessionStart")
        buf["task"] = "x"
        entry = build_hook_entry(buf, repo)
        assert entry["content_hash"].startswith("sha256:")
        assert entry["schema_version"] == "1.2"


class TestCwdIndependence:
    def test_does_not_depend_on_process_cwd(self, repo: Path, tmp_path: Path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        orig = os.getcwd()
        os.chdir(elsewhere)
        try:
            _session(repo)
        finally:
            os.chdir(orig)
        assert (repo / ".openshard" / "runs.jsonl").exists()
        assert not (elsewhere / ".openshard").exists()


# ---------------------------------------------------------------------------
# PR6: task completion independent of SessionEnd
# ---------------------------------------------------------------------------


class TestTaskCompletion:
    def test_stop_makes_task_completed_without_session_end(self, repo: Path):
        entry = _session(repo, end=False)
        assert entry["capture"]["session_end_observed"] is False
        assert entry["capture"]["task_status"] == "turn_completed"
        receipt = build_shard_receipt(entry)
        assert receipt.task_completion == "Completed"
        out = render_compact_shard_receipt(receipt)
        assert "Completed" in out

    def test_in_progress_before_any_stop(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        entry = _runs_lines(repo)[0]
        assert entry["capture"]["task_status"] == "in_progress"
        receipt = build_shard_receipt(entry)
        assert receipt.task_completion == "In progress"

    def test_session_end_metadata_arrives_later_without_changing_task_status(self, repo: Path):
        entry = _session(repo, end=False)
        assert entry["capture"]["task_status"] == "turn_completed"
        _run(repo, "SessionEnd", reason="other")
        after = _runs_lines(repo)[0]
        assert after["capture"]["task_status"] == "turn_completed"
        assert after["capture"]["session_end_observed"] is True

    def test_repeated_stop_is_idempotent_on_task_status(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _run(repo, "Stop")
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert entry["capture"]["task_status"] == "turn_completed"
        assert entry["capture"]["turn_count"] == 2

    def test_checks_still_not_run_when_turn_completed(self, repo: Path):
        entry = _session(repo, end=False)
        receipt = build_shard_receipt(entry)
        assert receipt.checks_display == "Not run"
        assert receipt.task_completion == "Completed"  # completion != verification


# ---------------------------------------------------------------------------
# PR6: task-boundary duration (first prompt -> most recent Stop)
# ---------------------------------------------------------------------------


class TestDuration:
    def test_duration_absent_before_any_stop(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        entry = _runs_lines(repo)[0]
        assert "duration_seconds" not in entry

    def test_duration_present_and_non_negative_after_stop(self, repo: Path):
        entry = _session(repo, end=False)
        assert entry["duration_seconds"] is not None
        assert entry["duration_seconds"] >= 0
        receipt = build_shard_receipt(entry)
        assert receipt.duration_seconds is not None
        out = render_compact_shard_receipt(receipt)
        assert "Duration" in out


# ---------------------------------------------------------------------------
# PR6: status-line payload parsing
# ---------------------------------------------------------------------------


class TestStatusPayloadParsing:
    def test_extracts_model_cost_tokens(self):
        data = {
            "session_id": SID,
            "cwd": "/repo",
            "model": {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
            "cost": {"total_cost_usd": 0.42},
            "context_window": {
                "current_usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 3,
                }
            },
        }
        p = extract_status_payload(data)
        assert p is not None
        assert p.model_id == "claude-sonnet-5"
        assert p.cost_total_usd == 0.42
        assert p.tokens_input == 100
        assert p.tokens_output == 20
        assert p.tokens_cache_creation == 5
        assert p.tokens_cache_read == 3

    def test_missing_or_invalid_session_id_returns_none(self):
        assert extract_status_payload({"cwd": "/repo"}) is None
        assert extract_status_payload({"session_id": 123}) is None
        assert extract_status_payload({"session_id": "../../etc/passwd"}) is None

    def test_malformed_nested_objects_are_ignored_not_fatal(self):
        p = extract_status_payload({
            "session_id": SID, "model": "not-a-dict", "cost": "nope",
            "context_window": {"current_usage": "nope"},
        })
        assert p is not None
        assert p.model_id is None
        assert p.cost_total_usd is None
        assert p.tokens_input is None

    def test_bool_values_never_treated_as_numbers(self):
        p = extract_status_payload({"session_id": SID, "cost": {"total_cost_usd": True}})
        assert p is not None
        assert p.cost_total_usd is None

    def test_status_line_text_prefers_folder_and_model(self):
        text = ch._status_line_text({"cwd": "/home/dev/myrepo", "model": {"display_name": "Claude Sonnet 5"}})
        assert text == "myrepo · Claude Sonnet 5"

    def test_status_line_text_handles_windows_path(self):
        text = ch._status_line_text({"cwd": "C:\\Users\\dev\\myrepo", "model": {"display_name": "Claude Sonnet 5"}})
        assert text == "myrepo · Claude Sonnet 5"

    def test_status_line_text_never_raises_on_garbage(self):
        assert ch._status_line_text({}) == ""
        assert ch._status_line_text({"cwd": 123, "model": []}) == ""


# ---------------------------------------------------------------------------
# PR6: model / token / cost capture via the status line
# ---------------------------------------------------------------------------


class TestModelTokenCostCapture:
    def test_no_status_line_model_stays_unknown(self, repo: Path):
        entry = _session(repo)
        assert entry["execution_model"] == "unknown"
        receipt = build_shard_receipt(entry)
        assert receipt.model_display == "Unknown"
        assert "prompt_tokens" not in entry
        assert "estimated_cost" not in entry

    def test_model_captured_from_status_line_and_fed_into_next_fold(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _status(repo)
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert entry["execution_model"] == "claude-sonnet-5"
        assert entry["capture"]["model_source"] == "status_line"
        receipt = build_shard_receipt(entry)
        assert receipt.model_display == "Claude Sonnet 5"

    def test_model_switch_is_not_flattened_to_one_model(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _status(repo, model_id="claude-sonnet-5")
        _run(repo, "Stop")
        _status(repo, model_id="claude-opus-5")
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert entry["capture"]["models_seen"] == ["claude-sonnet-5", "claude-opus-5"]
        assert entry["execution_model"] == "claude-opus-5"
        receipt = build_shard_receipt(entry)
        assert len(receipt.model_stages) == 2
        out = render_compact_shard_receipt(receipt)
        assert "Models" in out
        assert "→" in out

    def test_token_usage_captured_with_provenance(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _status(repo, tokens_input=14000, tokens_output=2000, cache_read=500)
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert entry["prompt_tokens"] == 14000
        assert entry["completion_tokens"] == 2000
        assert entry["total_tokens"] == 16000
        assert entry["cache_read_tokens"] == 500
        assert entry["tokens_provenance"] == "provider_reported"
        receipt = build_shard_receipt(entry)
        out = render_compact_shard_receipt(receipt)
        assert "Tokens" in out
        assert "14k input" in out
        assert "2k output" in out

    def test_cost_is_delta_from_baseline_not_raw_cumulative_total(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        _status(repo, cost_total=0.10)  # baseline observed before any real work
        _run(repo, "UserPromptSubmit", prompt="task")
        _status(repo, cost_total=0.37)
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        assert entry["estimated_cost"] == pytest.approx(0.27)
        assert entry["cost_provenance"] == "provider_reported"

    def test_cost_display_is_clearly_labelled_estimate(self, repo: Path):
        _run(repo, "SessionStart", source="startup")
        _status(repo, cost_total=0.0)  # baseline before any work
        _run(repo, "UserPromptSubmit", prompt="task")
        _status(repo, cost_total=0.18)
        _run(repo, "Stop")
        entry = _runs_lines(repo)[0]
        receipt = build_shard_receipt(entry)
        assert receipt.cost_display == "$0.18 est."

    def test_no_status_line_cost_stays_not_recorded(self, repo: Path):
        entry = _session(repo)
        receipt = build_shard_receipt(entry)
        assert receipt.cost_display == "Not recorded"

    def test_status_ping_never_leaks_absolute_repo_path(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        _status(repo)
        _run(repo, "Stop")
        raw = _raw(repo)
        assert str(repo) not in raw
        assert str(repo.resolve()) not in raw

    def test_status_handler_never_raises_and_still_returns_text(self, repo: Path):
        with patch.object(ch, "_load_buffer_light", side_effect=RuntimeError("boom")):
            text = _status(repo)
        assert isinstance(text, str)

    def test_run_status_from_stream_handles_bad_input(self):
        import io

        assert run_status_from_stream(io.BytesIO(b""), env={}) == ""
        assert run_status_from_stream(io.StringIO("nope"), env={}) == ""

        class Broken:
            def read(self):
                raise OSError("closed")

        assert run_status_from_stream(Broken(), env={}) == ""

    def test_status_line_ignored_for_unresolvable_repo(self, tmp_path: Path):
        outcome = handle_claude_status(
            _status_payload(tmp_path / "does-not-exist"), env={"CLAUDE_PROJECT_DIR": str(tmp_path / "nope")},
        )
        assert isinstance(outcome, str)
        assert not (tmp_path / ".openshard").exists()


# ---------------------------------------------------------------------------
# PR6: richer receipt presentation (files, tool activity, evidence)
# ---------------------------------------------------------------------------


class TestRicherReceiptPresentation:
    def test_files_block_shows_change_type_letters(self, repo: Path):
        entry = _session(repo)
        receipt = build_shard_receipt(entry)
        out = render_compact_shard_receipt(receipt)
        assert "A calc.py" in out
        assert "M README.md" in out

    def test_tool_activity_rendered(self, repo: Path):
        entry = _session(repo)
        receipt = build_shard_receipt(entry)
        out = render_compact_shard_receipt(receipt)
        assert "Activity" in out
        assert "Write × 1" in out
        assert "Edit × 1" in out
        assert "Bash × 1" in out

    def test_evidence_summary_lists_distinct_kinds(self, repo: Path):
        entry = _session(repo)
        receipt = build_shard_receipt(entry)
        out = render_compact_shard_receipt(receipt)
        assert "Evidence" in out
        assert "Directly observed" in out
        assert "Agent reported" in out
        assert "Git observed" in out
        assert "Independently verified" not in out  # never fabricated

    def test_no_activity_or_files_block_for_empty_receipt(self):
        from openshard.history.shard_contract import ShardReceipt

        receipt = ShardReceipt(
            shard_id="shard-x", created_at="", task_short="t", task_full="t", agent="a",
            strategy="Not recorded", model_display="Unknown", risk="-", sandbox="-",
            files_changed=0, checks_display="Not run", approval="-", cost_display="Not recorded",
            result="-", status="-", duration_seconds=None,
        )
        out = render_compact_shard_receipt(receipt)
        assert "Files" not in out
        assert "Activity" not in out
        assert "Evidence" not in out


# ---------------------------------------------------------------------------
# PR6: backward compatibility with pre-PR6 records
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_old_claude_hook_record_without_new_fields_loads_safely(self):
        legacy_entry = {
            "schema_version": "1.1",
            "timestamp": "2026-01-01T00:00:00Z",
            "task": "legacy task",
            "execution_model": "unknown",
            "executor": EXECUTOR,
            "import_source": "claude_code",
            "capture": {
                "source": "claude_code_hooks",
                "session_id": SID,
                "status": "ended",
                "session_end_observed": True,
                "prompt_count": 1,
                "turn_count": 1,
                "tool_call_count": 0,
            },
            "files_detail": [],
            "run_id": "r1",
            "shard_id": "shard-20260101-0001",
            "attempt_number": 1,
        }
        receipt = build_shard_receipt(legacy_entry)
        assert receipt.task_completion is None
        assert receipt.tokens_input is None
        assert receipt.tokens_output is None
        assert receipt.cost_display == "Not recorded"
        assert receipt.model_display == "Unknown"
        out = render_compact_shard_receipt(receipt)
        assert "RECEIPT" in out
        full = render_full_shard_receipt(receipt)
        assert "SHARD" in full


# ---------------------------------------------------------------------------
# PR7: capture performance and latency hardening
# ---------------------------------------------------------------------------


class TestStatusLineFastPath:
    """Requirement 7: the status line must never touch git or fold to runs.jsonl."""

    def test_status_line_never_folds(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")  # first prompt already folds once
        before = _raw(repo)
        with patch.object(ch, "_fold", wraps=ch._fold) as fold_spy:
            for i in range(5):
                _status(repo, tokens_input=1000 + i * 100, tokens_output=50 + i)
            assert fold_spy.call_count == 0
        # Status pings never touch runs.jsonl at all -- byte-identical to before.
        assert _raw(repo) == before

    def test_status_line_never_spawns_a_subprocess_in_steady_state(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        with patch("subprocess.run") as run_spy:
            _status(repo, tokens_input=1234, cost_total=0.05)
        run_spy.assert_not_called()

    def test_first_ever_status_ping_creates_no_buffer_and_spawns_no_git(self, repo: Path):
        """A status ping arriving before any lifecycle hook must not create
        a buffer via git-collecting `_new_buffer` -- see `_load_buffer_light`."""
        with patch("subprocess.run") as run_spy:
            text = _status(repo)
        run_spy.assert_not_called()
        assert isinstance(text, str)
        assert not (repo / ".openshard" / "runs.jsonl").exists()
        assert not buffer_path(repo.resolve(), SID).exists()

    def test_status_line_steady_state_stays_well_under_budget(self, repo: Path):
        """Requirement 2: a coarse, generously-bounded regression tripwire.

        Not a claim about absolute performance (see docs/capture-performance.md
        for real numbers) -- just wide enough (a few x the ~25-35ms typically
        observed for this in-process call on the dev machine) to catch a
        regression back to doing real fold work here (previously ~150-700ms)
        without being flaky on a loaded CI runner.
        """
        _run(repo, "UserPromptSubmit", prompt="task")
        samples = []
        for i in range(10):
            t0 = time.monotonic()
            _status(repo, tokens_input=1000 + i, cost_total=0.01 * i)
            samples.append(time.monotonic() - t0)
        samples.sort()
        median = samples[len(samples) // 2]
        assert median < 0.15, f"status-line steady-state median {median * 1000:.1f}ms exceeded budget"

    def test_repeated_status_pings_do_not_grow_runs_jsonl_until_real_fold(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")  # first prompt already folds once
        before = _raw(repo)
        for i in range(10):
            _status(repo, tokens_input=1000 + i, cost_total=0.01 * i)
        assert _raw(repo) == before  # status pings alone never rewrite runs.jsonl
        _run(repo, "Stop")
        assert len(_runs_lines(repo)) == 1
        entry = _runs_lines(repo)[0]
        assert entry["prompt_tokens"] == 1009  # last-observed value still reaches the fold


class TestRepoIdentityCaching:
    """Requirement 3/6: `git config --get remote.origin.url` is not re-run per fold."""

    def test_repo_identity_git_call_happens_at_most_once_per_session(self, repo: Path):
        from openshard.history import repo_identity as ri

        with patch.object(ri, "_origin_remote_url", wraps=ri._origin_remote_url) as spy:
            _run(repo, "UserPromptSubmit", prompt="task")
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "a.py")})
            path = buffer_path(repo.resolve(), SID)
            buf = json.loads(path.read_text(encoding="utf-8"))
            buf["last_fold_at"] = "2000-01-01T00:00:00Z"
            path.write_text(json.dumps(buf), encoding="utf-8")
            _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "a.py")})
            _run(repo, "Stop")
        # Two real folds happened (the throttled tool snapshot and Stop),
        # but the git-identity subprocess only ran once for the session.
        assert spy.call_count == 1


class TestBoundedLockWaitsAndFailOpen:
    """Requirements 4 and 5: bounded lock waits, and Claude Code keeps running."""

    def test_hook_fails_open_when_lock_acquisition_times_out(self, repo: Path):
        from openshard.history.jsonl_store import LockTimeoutError

        with patch("openshard.history.jsonl_store.history_file_lock", side_effect=LockTimeoutError("stuck")):
            out = _run(repo, "UserPromptSubmit", prompt="task")
        assert out.action == "error"
        # Never blocked: the patched lock raised immediately, so this whole
        # call returns fast rather than hanging Claude Code.

    def test_status_line_fails_open_when_lock_acquisition_times_out(self, repo: Path):
        from openshard.history.jsonl_store import LockTimeoutError

        with patch("openshard.history.jsonl_store.history_file_lock", side_effect=LockTimeoutError("stuck")):
            text = _status(repo)
        assert isinstance(text, str)  # status line still renders something

    def test_hook_does_not_hang_on_a_genuinely_held_lock(self, repo: Path):
        """End-to-end: a real contended lock still returns within a bounded time."""
        _run(repo, "UserPromptSubmit", prompt="task")
        path = buffer_path(repo.resolve(), SID)
        release = threading.Event()
        acquired = threading.Event()

        def hold():
            from openshard.history.jsonl_store import _file_lock, _lock_path_for
            with _file_lock(_lock_path_for(path)):
                acquired.set()
                release.wait(timeout=10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        try:
            assert acquired.wait(timeout=5)
            with patch.object(ch, "_LOCK_TIMEOUT_SECONDS", 0.3):
                t0 = time.monotonic()
                out = _run(repo, "Stop")
                elapsed = time.monotonic() - t0
        finally:
            release.set()
            holder.join(timeout=5)
        assert out.action == "error"
        assert elapsed < 5.0  # bounded, not hung


class TestConcurrentHookActivity:
    """Requirement 11: concurrent/contended hook activity must not corrupt history."""

    def test_concurrent_tool_hooks_same_session_no_corruption_no_loss(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        n = 25

        def fire(i: int) -> None:
            handle_claude_hook(
                _payload("PostToolUse", repo, SID, tool_name="Bash", tool_input={"command": f"echo {i}"}),
                env={"CLAUDE_PROJECT_DIR": str(repo)},
            )

        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(fire, range(n)))
        _run(repo, "Stop")

        raw = _raw(repo).splitlines()
        assert len(raw) == 1  # exactly one record, no torn/duplicate lines
        entry = json.loads(raw[0])
        assert entry["capture"]["tool_call_count"] == n  # every call counted, none lost

    def test_rapid_repeated_stop_does_not_explode_record_count(self, repo: Path):
        _run(repo, "UserPromptSubmit", prompt="task")
        for _ in range(15):
            _run(repo, "Stop")
        assert len(_runs_lines(repo)) == 1
        assert _runs_lines(repo)[0]["capture"]["turn_count"] == 15
