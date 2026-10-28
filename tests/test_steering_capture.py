"""Steering capture (docs/steering-capture.md): every agent, real hook path, real git.

Every test drives the shared fold through ``handle_hook`` / ``handle_claude_hook``
in a throw-away git repository with an isolated ``OPENSHARD_HOME`` (conftest),
with fixture hook payloads and session logs shaped like each agent's own.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from openshard.adapters import steering
from openshard.adapters.claude_hooks import handle_claude_hook, handle_hook
from openshard.history import steering as steering_view
from openshard.history.receipt_render import render_compact_shard_receipt
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.store import load_history
from openshard.history.task_title import normalize_title_candidate
from openshard.sync import envelope
from tests.capture_fixtures import _git, _make_repo

SID = "5a6b7c8d-1e2f-4a3b-9c8d-7e6f5a4b3c2d"
SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8")
)
SECRET = "sk-ant-api03-" + "Zq9" * 20  # secret-shaped: must never leave the prompt
FOLLOW_UP_PHRASE = "periwinkle-zeppelin"  # distinctive: only ever in a follow-up prompt
INTERJECTION_PHRASE = "marmalade-quasar"  # distinctive: only ever in a message sent mid-turn


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "widget")
    (root / "README.md").write_text("# widget\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "readme")
    return root


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path: Path, monkeypatch) -> Path:
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return config_dir


def _now_ms(offset: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _entries(repo: Path) -> list[dict]:
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)


def _block(entry: dict) -> dict:
    evidence = steering_view.steering_evidence(entry)
    assert evidence is not None
    return evidence


def _kinds(entry: dict) -> list[str]:
    return [e["kind"] for e in _block(entry)["events"]]


def _one(entry: dict, kind: str) -> dict:
    found = [e for e in _block(entry)["events"] if e["kind"] == kind]
    assert len(found) == 1, (kind, _block(entry)["events"])
    return found[0]


def _wait_next_second() -> None:
    """Hook times are whole seconds: make the next hook land in a later one."""
    time.sleep(1.1)


# ---------------------------------------------------------------------------
# Claude Code: UserPromptSubmit hook + transcript (queue, Esc, rejections) + git
# ---------------------------------------------------------------------------


def _claude_transcript() -> Path:
    directory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "c--work-widget"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{SID}.jsonl"


def _claude(repo: Path, event: str, **fields):
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event,
               "transcript_path": str(_claude_transcript()), "permission_mode": "default", **fields}
    return handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _claude_log(*records: dict) -> None:
    with _claude_transcript().open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps({"sessionId": SID, **record}) + "\n")


def _enqueue(text: str) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": _now_ms(), "content": text}


def _user_text(text: str) -> dict:
    return {"type": "user", "timestamp": _now_ms(), "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _rejection() -> dict:
    body = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file "
            "edit, the new_string was NOT written to the file). STOP what you are doing and wait for the user.")
    return {"type": "user", "timestamp": _now_ms(), "toolUseResult": "User rejected tool use",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": body}]}}


def _claude_session(repo: Path) -> None:
    """Turn 1 writes a.py; between turns a person edits README.md and a.py; turn 2 is steered hard."""
    _claude_transcript().write_text("", encoding="utf-8")
    _claude(repo, "SessionStart", source="startup")
    _claude(repo, "UserPromptSubmit", prompt=f"Build the settings page (key {SECRET})")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _claude(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "a.py")},
            tool_response={"filePath": str(repo / "a.py")})
    _claude(repo, "Stop")
    # A person edits between turns: README.md was clean at the turn's end, a.py was the agent's.
    with (repo / "README.md").open("a", encoding="utf-8") as fh:
        fh.write("one\ntwo\n")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _wait_next_second()
    _claude(repo, "UserPromptSubmit", prompt=(
        f"I don't like the preview, make it exactly like the real component {FOLLOW_UP_PHRASE} {SECRET}"))
    _claude(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": ""})
    _claude_log(
        _enqueue(f"also add tests for the parser {INTERJECTION_PHRASE} {SECRET}"),
        _rejection(),
        _user_text("[Request interrupted by user for tool use]"),
    )
    _wait_next_second()
    _claude_log(_user_text("[Request interrupted by user]"))
    _wait_next_second()  # Esc fires no hook: the next prompt arrives in the same turn
    _claude(repo, "UserPromptSubmit", prompt=f"no, use the existing helper instead {FOLLOW_UP_PHRASE}")
    _claude(repo, "Stop")


class TestClaudeCode:
    def test_turns_carry_initial_prompt_follow_ups_interjection_interrupt_denial_and_manual_edits(self, repo):
        _claude_session(repo)
        first, second = _entries(repo)
        assert _kinds(first) == ["initial_prompt"]
        assert _one(first, "initial_prompt")["after"] == "none"

        kinds = _kinds(second)
        assert sorted(kinds) == sorted(["follow_up", "manual_edit", "interjection", "permission_decision",
                                        "interrupt", "follow_up"])
        correction, redirect = [e for e in _block(second)["events"] if e["kind"] == "follow_up"]
        assert (correction["intent"], correction["intent_confidence"]) == ("correction", "high")
        assert correction["after"] == "turn_end" and correction["think_seconds"] >= 1
        assert correction["source"] == "hook" and correction["turn_index"] == 2
        assert correction["prompt_chars"] > 60
        assert redirect["intent"] == "redirect"
        assert redirect["after"] == "turn_end"  # the Esc in the transcript ended the turn (no hook fired)

        interjection = _one(second, "interjection")
        assert (interjection["intent"], interjection["source"]) == ("new_task", "transcript")
        assert interjection["after"] == "tool_call"
        denial = _one(second, "permission_decision")
        assert (denial["decision"], denial["source"], denial["after"]) == ("deny", "transcript", "permission_prompt")
        interrupt = _one(second, "interrupt")  # the Esc at the permission prompt is the denial, not a 2nd input
        assert (interrupt["source"], interrupt["after"]) == ("transcript", "unknown")

        edit = _one(second, "manual_edit")
        assert (edit["files"], edit["source"], edit["after"]) == (2, "git", "turn_end")
        assert edit["lines_added"] == 2 and edit["lines_removed"] == 0  # README.md, exact
        assert edit["lines_unknown_files"] == 1  # a.py was already the agent's change at the turn's end
        assert sorted(edit["paths"]) == ["README.md", "a.py"]

        facts = _block(second)["autonomy"]
        assert facts["human_inputs"] == 6
        assert facts["interrupts"] == 1
        # No Notification hook fired in this fixture: none asked, none allowed; the transcript's denial counts.
        assert facts["permission_prompts"] == {"asked": 0, "allowed": 0, "denied": 1}
        assert facts["intents"] == {"correction": 1, "redirect": 1, "new_task": 1}
        assert facts["steering_seconds"] > 1  # two pauses: after the Stop, and after the Esc
        assert "permission_mode" not in facts and "ran_on" not in facts  # existing fields, not repeated
        assert _block(second)["not_available"] == {}

    def test_openshard_last_prints_one_plain_steering_line(self, repo):
        _claude_session(repo)
        entries = _entries(repo)
        receipt = build_shard_receipt(entries[-1], index=len(entries) - 1)
        line = next(row for row in render_compact_shard_receipt(receipt).splitlines() if "Steering" in row)
        assert re.search(r"Steering\s+1 correction · 1 redirect · 1 new ask · 1 interrupt · 1 permission denied · "
                         r"2 files changed between turns, not by the agent · you stepped in 6 times", line)
        first = build_shard_receipt(entries[0], index=0)
        assert "no steering observed" in render_compact_shard_receipt(first)

    def test_message_queued_then_delivered_as_next_prompt_is_counted_once(self, repo):
        _claude_transcript().write_text("", encoding="utf-8")
        _claude(repo, "SessionStart", source="startup")
        _claude(repo, "UserPromptSubmit", prompt="build it")
        _claude(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": ""})
        _claude_log(_enqueue("also add docs"))
        _claude(repo, "Stop")
        _wait_next_second()
        _claude_log({"type": "queue-operation", "operation": "dequeue", "timestamp": _now_ms()})
        _claude(repo, "UserPromptSubmit", prompt="also add docs")
        _claude(repo, "Stop")
        first, second = _entries(repo)
        assert _kinds(first) == ["initial_prompt", "interjection"]
        delivered = _one(second, "follow_up")
        assert delivered["from_queue"] is True
        assert _block(second)["autonomy"]["human_inputs"] == 0  # typed (and counted) in turn 1

    def test_slash_commands_in_the_queue_are_not_interjections(self, repo):
        _claude_transcript().write_text("", encoding="utf-8")
        _claude(repo, "SessionStart", source="startup")
        _claude(repo, "UserPromptSubmit", prompt="build it")
        _claude_log(_enqueue("/compact"), _enqueue("<bash-input>ls</bash-input>"))
        _claude(repo, "Stop")
        assert _kinds(_entries(repo)[0]) == ["initial_prompt"]

    def test_no_transcript_means_no_log_signals_and_no_invention(self, repo):
        payload = {"session_id": SID, "cwd": str(repo), "permission_mode": "default"}
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        handle_claude_hook({**payload, "hook_event_name": "UserPromptSubmit", "prompt": "go"}, env=env)
        handle_claude_hook({**payload, "hook_event_name": "Stop"}, env=env)
        _wait_next_second()
        handle_claude_hook({**payload, "hook_event_name": "UserPromptSubmit", "prompt": "lgtm"}, env=env)
        handle_claude_hook({**payload, "hook_event_name": "Stop"}, env=env)
        first, second = _entries(repo)
        assert _kinds(first) == ["initial_prompt"]
        assert _one(second, "follow_up")["intent"] == "approval"

    def test_long_logs_are_read_to_the_end_in_bounded_chunks(self, repo, monkeypatch):
        monkeypatch.setattr(steering, "_LOG_READ_BYTES", 256)  # many chunks, one over-long line
        _claude_transcript().write_text("", encoding="utf-8")
        _claude(repo, "UserPromptSubmit", prompt="build it")
        filler = [{"type": "assistant", "timestamp": _now_ms(), "message": {"content": "x" * 40}}] * 50
        _claude_log(*filler, {"type": "summary", "summary": "y" * 1000}, _user_text("[Request interrupted by user]"))
        _claude(repo, "Stop")
        assert _kinds(_entries(repo)[0]) == ["initial_prompt", "interrupt"]

    def test_session_end_drops_the_turn_end_snapshot(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_TURN_RECEIPTS", "off")
        _claude(repo, "UserPromptSubmit", prompt="build it")
        _claude(repo, "Stop")
        snapshots = repo / ".openshard" / "claude_sessions" / "steering"
        assert len(list(snapshots.glob("*.json"))) == 1
        _claude(repo, "SessionEnd", reason="other")
        assert not snapshots.exists()  # the last session's end removes the folder too

    def test_snapshots_of_sessions_that_never_returned_are_pruned(self, repo):
        directory = repo / ".openshard" / "claude_sessions" / "steering"
        directory.mkdir(parents=True)
        stale = directory / ("0" * 24 + ".json")
        stale.write_text("{}", encoding="utf-8")
        old = time.time() - steering._SNAPSHOT_MAX_AGE_SECONDS - 60
        os.utime(stale, (old, old))
        _claude(repo, "UserPromptSubmit", prompt="build it")
        _claude(repo, "Stop")
        assert not stale.exists() and len(list(directory.glob("*.json"))) == 1

    def test_session_receipts_number_turns_from_the_session(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_TURN_RECEIPTS", "off")
        _claude_transcript().write_text("", encoding="utf-8")
        _claude(repo, "UserPromptSubmit", prompt="build it")
        _claude(repo, "Stop")
        _wait_next_second()
        _claude(repo, "UserPromptSubmit", prompt="why did you pick that library?")
        _claude(repo, "Stop")
        _claude_log(_user_text("[Request interrupted by user]"))  # read at turn 2's fold, belongs to turn 2
        _claude(repo, "Stop")
        (entry,) = _entries(repo)
        assert [(e["kind"], e["turn_index"]) for e in _block(entry)["events"]] == [
            ("initial_prompt", 1), ("follow_up", 2), ("interrupt", 2)]
        assert _one(entry, "follow_up")["intent"] == "clarification"
        assert _block(entry)["autonomy"]["longest_unsupervised_seconds"] is not None


# ---------------------------------------------------------------------------
# Codex: UserPromptSubmit / Interrupt hooks + rollout log
# ---------------------------------------------------------------------------


def _codex_rollout(tmp_path: Path) -> Path:
    path = tmp_path / ".codex" / "sessions" / "2026" / f"rollout-{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"timestamp": _now_ms(-60), "type": "session_meta", "payload": {"id": SID, "cwd": "/w"}}
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return path


def _codex_log(path: Path, *payloads: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for i, payload in enumerate(payloads):  # a few ms apart, as a real rollout's lines are
            fh.write(json.dumps({"timestamp": _now_ms(i * 0.005), "type": "event_msg", "payload": payload}) + "\n")


def _codex(repo: Path, rollout: Path, event: str, **fields):
    doc = {"session_id": SID, "turn_id": "turn_1", "hook_event_name": event, "cwd": str(repo),
           "model": "gpt-5.5", "transcript_path": str(rollout), **fields}
    return handle_hook(doc, env={}, agent="codex")


class TestCodex:
    def test_hooks_and_rollout_give_prompts_interjection_and_one_interrupt(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        rollout = _codex_rollout(tmp_path)
        _codex(repo, rollout, "SessionStart", source="startup")
        _codex(repo, rollout, "UserPromptSubmit", prompt="Add a retry helper")
        _codex_log(rollout, {"type": "task_started", "turn_id": "turn_1"},
                   {"type": "user_message", "message": "Add a retry helper"},
                   {"type": "user_message", "message": f"actually use exponential backoff {INTERJECTION_PHRASE}"},
                   {"type": "turn_aborted", "reason": "interrupted"})
        _codex(repo, rollout, "Interrupt")  # the same Esc, seen by the hook too
        _wait_next_second()
        _codex(repo, rollout, "UserPromptSubmit", prompt="looks good, now add the docs")
        _codex_log(rollout, {"type": "task_started", "turn_id": "turn_2"},
                   {"type": "user_message", "message": "looks good, now add the docs"})
        with rollout.open("a", encoding="utf-8") as fh:  # the person rejects an approval prompt
            fh.write(json.dumps({"timestamp": _now_ms(), "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": "exec command rejected by user"}}) + "\n")
        _codex_log(rollout, {"type": "task_complete", "turn_id": "turn_2"})
        _codex(repo, rollout, "Stop")
        _codex(repo, rollout, "SessionEnd", reason="other")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interjection", "interrupt", "follow_up", "permission_decision"]
        interjection = _one(entry, "interjection")
        assert (interjection["intent"], interjection["source"]) == ("redirect", "transcript")
        follow_up = _one(entry, "follow_up")
        assert (follow_up["intent"], follow_up["after"]) == ("new_task", "turn_end")
        assert follow_up["think_seconds"] >= 1
        block = _block(entry)
        denial = _one(entry, "permission_decision")
        assert (denial["decision"], denial["source"]) == ("deny", "transcript")
        assert block["not_available"] == {}
        # Codex persists no approval and no approval request: those stay unknown, never 0.
        assert block["autonomy"]["permission_prompts"] == {"asked": None, "allowed": None, "denied": 1}

    def test_a_rollout_of_another_session_is_never_read(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        rollout = _codex_rollout(tmp_path)
        rollout.write_text(json.dumps({"timestamp": _now_ms(-60), "type": "session_meta",
                                       "payload": {"id": "someone-else"}}) + "\n", encoding="utf-8")
        _codex(repo, rollout, "UserPromptSubmit", prompt="go")
        _codex_log(rollout, {"type": "task_started"}, {"type": "user_message", "message": "go"},
                   {"type": "user_message", "message": "no"})
        _codex(repo, rollout, "Stop")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt"]
        assert _block(entry)["log_not_read_reason"] == "log_session_mismatch"


# ---------------------------------------------------------------------------
# Cursor, OpenCode, Grok Build, Hermes, Antigravity: their hooks only
# ---------------------------------------------------------------------------


class TestOtherAgents:
    def test_cursor_prompts_and_aborted_stop(self, repo):
        def doc(event: str, **fields) -> dict:
            return {"conversation_id": SID, "generation_id": "g1", "hook_event_name": event, "model": "m",
                    "workspace_roots": [str(repo)], **fields}

        handle_hook(doc("beforeSubmitPrompt", prompt="add a dark mode toggle"), env={}, agent="cursor")
        handle_hook(doc("stop", status="aborted", loop_count=0), env={}, agent="cursor")
        _wait_next_second()
        handle_hook(doc("beforeSubmitPrompt", prompt="that's wrong, keep the light theme default"),
                    env={}, agent="cursor")
        handle_hook(doc("stop", status="completed", loop_count=0), env={}, agent="cursor")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interrupt", "follow_up"]
        assert _one(entry, "interrupt")["source"] == "hook"
        assert (_one(entry, "follow_up")["intent"], _one(entry, "follow_up")["after"]) == ("correction", "turn_end")
        assert _block(entry)["not_available"] == {}  # no transcript here: nothing claimed from it

    def test_opencode_prompts_after_idle(self, repo):
        def run(event: str, **fields):
            doc = {"agent": "opencode", "event": event, "session_id": SID, "directory": str(repo),
                   "worktree": str(repo), **fields}
            return handle_hook(doc, env={}, agent="opencode")

        run("session.created")
        run("chat.message", prompt="add a cache")
        run("session.idle")
        _wait_next_second()
        run("chat.message", prompt="continue")
        run("session.idle")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "follow_up"]
        assert _one(entry, "follow_up")["intent"] == "continue"
        assert _block(entry)["not_available"] == {}

    def test_grok_build_permission_denied_is_not_claimed_as_a_person(self, repo):
        def doc(event: str, **fields) -> dict:
            snake = {"UserPromptSubmit": "user_prompt_submit", "Stop": "stop",
                     "PermissionDenied": "permission_denied"}[event]
            return {"sessionId": SID, "cwd": str(repo), "workspaceRoot": str(repo), "hook_event_name": event,
                    "hookEventName": snake, **fields}

        handle_hook(doc("UserPromptSubmit", prompt="add a cli"), env={}, agent="grok_build")
        handle_hook(doc("PermissionDenied", toolName="run_terminal_command"), env={}, agent="grok_build")
        handle_hook(doc("Stop", reason="end_turn"), env={}, agent="grok_build")
        _wait_next_second()
        handle_hook(doc("UserPromptSubmit", prompt="ok go ahead"), env={}, agent="grok_build")
        handle_hook(doc("Stop", reason="end_turn"), env={}, agent="grok_build")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "follow_up"]
        assert _one(entry, "follow_up")["intent"] == "continue"
        # PermissionDenied alone does not say a person denied: no permission decision is claimed.
        assert _block(entry)["autonomy"]["permission_prompts"]["denied"] == 0

    def test_hermes_person_approvals_and_interrupt(self, repo):
        (repo / ".openshard").mkdir(exist_ok=True)  # Hermes hooks are user-global: a repository opts in
        def doc(event: str, **extra) -> dict:
            return {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SID,
                    "cwd": str(repo), "extra": extra}

        handle_hook(doc("pre_llm_call", user_message="deploy the staging stack", model="m"), env={}, agent="hermes")
        for choice in ("once", "smart_approve", "deny", "always"):
            handle_hook(doc("pre_approval_request", command="terraform apply", surface="cli"), env={}, agent="hermes")
            handle_hook(doc("post_approval_response", command="terraform apply", choice=choice, surface="cli"),
                        env={}, agent="hermes")
        handle_hook(doc("on_session_end", completed=False, interrupted=True), env={}, agent="hermes")
        (entry,) = _entries(repo)
        decisions = [e["decision"] for e in _block(entry)["events"] if e["kind"] == "permission_decision"]
        assert decisions == ["allow", "deny", "always_allow"]  # the smart-mode approval was not a person's
        assert _one(entry, "interrupt")["source"] == "hook"
        assert _block(entry)["autonomy"]["permission_prompts"] == {"asked": 4, "allowed": 2, "denied": 1}

    def test_antigravity_has_no_prompts_but_sees_files_changed_between_turns(self, repo):
        def doc(**fields) -> dict:
            return {"conversationId": SID, "workspacePaths": [str(repo)], "modelName": "gemini-3", **fields}

        handle_hook(doc(), env={}, agent="antigravity", event_override="PreInvocation")
        handle_hook(doc(), env={}, agent="antigravity", event_override="Stop")
        (repo / "README.md").write_text("# widget\nedited by hand\n", encoding="utf-8")
        handle_hook(doc(), env={}, agent="antigravity", event_override="PreInvocation")
        handle_hook(doc(), env={}, agent="antigravity", event_override="Stop")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["manual_edit"]
        assert (_one(entry, "manual_edit")["lines_added"], _one(entry, "manual_edit")["lines_removed"]) == (1, 0)
        by_kind = _block(entry)["autonomy"]["human_inputs_by_kind"]
        # No transcript here, so no prompt was read; the permission decision is what Antigravity never records.
        assert by_kind["initial_prompt"] == 0 and by_kind["manual_edit"] == 1
        assert by_kind["permission_decision"] is None


# ---------------------------------------------------------------------------
# Manual edits: real git
# ---------------------------------------------------------------------------


def _codex_turns(repo: Path, between) -> dict:
    doc = {"session_id": SID, "turn_id": "t", "cwd": str(repo), "model": "gpt-5.5"}
    handle_hook({**doc, "hook_event_name": "UserPromptSubmit", "prompt": "work"}, env={}, agent="codex")
    handle_hook({**doc, "hook_event_name": "Stop"}, env={}, agent="codex")
    between()
    handle_hook({**doc, "hook_event_name": "UserPromptSubmit", "prompt": "more"}, env={}, agent="codex")
    handle_hook({**doc, "hook_event_name": "Stop"}, env={}, agent="codex")
    (entry,) = _entries(repo)
    return entry


class TestManualEdits:
    def test_nothing_changed_between_turns_records_no_edit(self, repo):
        entry = _codex_turns(repo, lambda: None)
        assert "manual_edit" not in _kinds(entry)

    def test_new_untracked_file_counts_its_lines(self, repo):
        entry = _codex_turns(repo, lambda: (repo / "notes.txt").write_text("a\nb\nc", encoding="utf-8"))
        edit = _one(entry, "manual_edit")
        assert (edit["files"], edit["lines_added"], edit["lines_removed"]) == (1, 3, 0)
        assert "lines_unknown_files" not in edit

    def test_a_commit_made_by_hand_between_turns_is_seen(self, repo):
        def commit() -> None:
            (repo / "README.md").write_text("# widget v2\n", encoding="utf-8")
            _git(repo, "commit", "-q", "-am", "by hand")

        edit = _one(_codex_turns(repo, commit), "manual_edit")
        assert (edit["files"], edit["lines_added"], edit["lines_removed"]) == (1, 1, 1)

    def test_deleted_file_counts_removed_lines(self, repo):
        edit = _one(_codex_turns(repo, lambda: (repo / "README.md").unlink()), "manual_edit")
        assert (edit["files"], edit["lines_added"], edit["lines_removed"]) == (1, 0, 1)

    def test_agent_state_directories_are_never_manual_edits(self, repo):
        def touch() -> None:
            (repo / ".codex").mkdir(exist_ok=True)
            (repo / ".codex" / "x.json").write_text("{}", encoding="utf-8")

        assert "manual_edit" not in _kinds(_codex_turns(repo, touch))

    def test_snapshot_file_name_is_windows_safe_for_any_session_id(self, repo):
        buf = {"agent": "claude_code", "session_id": "abc:def/..\\x"}
        path = steering._snapshot_path(buf, repo)
        assert re.fullmatch(r"[0-9a-f]{24}\.json", path.name)
        assert path.parent == repo / ".openshard" / "claude_sessions" / "steering"

    def test_no_git_means_no_manual_edit_claim(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        buf = {"agent": "codex", "session_id": SID, "record": {"run_id": "r"}}
        steering.note_turn_end(buf, plain, "2026-01-01T00:00:00Z", how="stop")
        steering.note_work_resumed(buf, plain, "2026-01-01T00:01:00Z")
        assert buf["steering"]["events"] == []


# ---------------------------------------------------------------------------
# Receipt + sync
# ---------------------------------------------------------------------------


class TestReceiptAndSync:
    def test_envelope_carries_the_block_without_paths_and_validates(self, repo):
        _claude_session(repo)
        entries = _entries(repo)
        doc = envelope.build_envelope(entries[-1], len(entries) - 1, core_version="9.9.9")
        block = doc["receipt"]["steering"]
        assert block["version"] == 1 and block["intent_method"] == "rules-v1"
        assert block["autonomy"]["human_inputs"] == 6
        assert all("paths" not in e for e in block["events"])
        assert "turn_ends" not in block
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))
        assert not errors, [e.message for e in errors[:3]]

    def test_receipts_without_the_block_are_unchanged(self, repo):
        _claude_session(repo)
        entries = _entries(repo)
        old = json.loads(json.dumps(entries[-1]))
        old["capture"].pop("steering")
        doc = envelope.build_envelope(old, 0, core_version="9.9.9")
        assert "steering" not in doc["receipt"]
        receipt = build_shard_receipt(old, index=0)
        assert "Steering" not in render_compact_shard_receipt(receipt)
        assert steering_view.steering_evidence(old) is None

    def test_a_platform_that_predates_the_block_gets_the_receipt_without_it(self):
        class Result:
            kind, code = "rejected", "invalid_payload"
            details = [{"path": "receipt", "message": 'Unrecognized key: "steering"'}]

        assert envelope.rejected_optional_key(Result(), {"steering": {}}) == "steering"
        assert "steering" in envelope.OPTIONAL_RECEIPT_KEYS

    def test_sealed_turn_receipts_verify_with_and_without_the_block(self, repo, monkeypatch):
        from openshard.history import receipt_signing as rs
        from openshard.history.receipt_verify import verify_local

        monkeypatch.delenv(rs.DISABLE_ENV, raising=False)
        _claude_session(repo)
        runs = repo / ".openshard" / "runs.jsonl"
        entries = _entries(repo)
        assert all("steering" in e["capture"] for e in entries)
        for index, entry in enumerate(entries):
            assert verify_local(runs, entry, index).verdict == rs.VERDICT_VERIFIED
        # A Receipt sealed before steering capture existed: no block, still verified.
        legacy_repo = repo.parent / "legacy"
        legacy_repo.mkdir()
        _git(legacy_repo, "init", "-q")
        monkeypatch.setattr(steering, "stamp", lambda entry, buf: None)
        legacy_payload = {"session_id": SID, "cwd": str(legacy_repo)}
        env = {"CLAUDE_PROJECT_DIR": str(legacy_repo)}
        handle_claude_hook({**legacy_payload, "hook_event_name": "UserPromptSubmit", "prompt": "go"}, env=env)
        handle_claude_hook({**legacy_payload, "hook_event_name": "Stop"}, env=env)
        (legacy,) = _entries(legacy_repo)
        assert "steering" not in legacy["capture"]
        assert verify_local(legacy_repo / ".openshard" / "runs.jsonl", legacy, 0).verdict == rs.VERDICT_VERIFIED

    def test_round_trip_through_a_rebuilt_buffer_keeps_the_events(self, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_TURN_RECEIPTS", "off")
        _claude_transcript().write_text("", encoding="utf-8")
        _claude(repo, "UserPromptSubmit", prompt="build it")
        _claude(repo, "Stop")
        for path in (repo / ".openshard" / "claude_sessions").glob("*.json"):
            path.unlink()  # a lost staging buffer: rebuilt from runs.jsonl at the next hook
        _wait_next_second()
        _claude(repo, "UserPromptSubmit", prompt="redo it")
        _claude(repo, "Stop")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "follow_up"]
        assert _one(entry, "follow_up")["intent"] == "correction"


# ---------------------------------------------------------------------------
# Privacy: prompt text never stored, synced or sent
# ---------------------------------------------------------------------------


def _all_text(root: Path) -> str:
    out = []
    for path in root.rglob("*"):
        if path.is_file():
            out.append(path.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(out)


class TestPrivacy:
    def test_prompt_text_never_reaches_runs_receipt_sync_or_telemetry(self, repo, tmp_path, monkeypatch):
        from openshard.telemetry import queue as telemetry_queue
        from openshard.telemetry import state as telemetry_state

        monkeypatch.delenv("OPENSHARD_TELEMETRY", raising=False)
        telemetry_state.set_consent("on", source="cli")
        telemetry_queue.clear()

        _claude_session(repo)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        codex_repo = _make_repo(tmp_path / "codex-widget")
        rollout = _codex_rollout(tmp_path)
        _codex(codex_repo, rollout, "UserPromptSubmit", prompt=f"start {SECRET}")
        _codex_log(rollout, {"type": "task_started"}, {"type": "user_message", "message": "start"},
                   {"type": "user_message", "message": f"{INTERJECTION_PHRASE} {SECRET}"})
        _codex(codex_repo, rollout, "Stop")

        surfaces: dict[str, str] = {}
        for root in (repo, codex_repo):
            entries = _entries(root)
            assert any("steering" in e["capture"] for e in entries)
            surfaces[f"{root.name}/.openshard"] = _all_text(root / ".openshard")
            for i, entry in enumerate(entries):
                receipt = envelope.build_envelope(entry, i, core_version="9.9.9")["receipt"]
                surfaces[f"{root.name}/sync/{i}"] = json.dumps(receipt)
                surfaces[f"{root.name}/receipt/{i}"] = render_compact_shard_receipt(build_shard_receipt(entry, index=i))
                surfaces[f"{root.name}/steering/{i}"] = json.dumps(entry["capture"].get("steering"))
                surfaces[f"{root.name}/runs/{i}"] = json.dumps(entry)
        surfaces["telemetry"] = json.dumps(telemetry_queue.peek(limit=500))
        assert telemetry_queue.peek(limit=500), "telemetry was on, so receipt events were queued"

        for name, text in surfaces.items():
            assert SECRET not in text and SECRET[:24] not in text, name  # scrubbed or never kept, everywhere
            assert INTERJECTION_PHRASE not in text, name  # a mid-turn message is never stored anywhere
            # Only the task's first prompt may name a Receipt: a follow-up's words are nowhere, not even as
            # the next turn's task (runs.jsonl, every .openshard file, the Receipt, sync, telemetry).
            assert FOLLOW_UP_PHRASE not in text, name

        # The first prompt's scrubbed title is still the task's, and names each later turn with its number.
        first, second = _entries(repo)
        title = first["task_title"]
        assert title.startswith("Build settings page") and first["capture"]["task_source"] == "first_user_prompt_excerpt"
        assert second["task"] == second["task_title"] == f"{normalize_title_candidate(title)} · turn 2"
        assert second["task_title"].startswith("Build settings page")
        assert title in surfaces["widget/.openshard"] and title in surfaces["widget/sync/0"]
        synced = envelope.build_envelope(second, 1, core_version="9.9.9")["receipt"]
        assert synced["task_title"] == synced["task_full"] == second["task_title"]
        assert "task_source" not in synced.get("session", {})  # not a contract value yet: unknown, never mislabelled


# ---------------------------------------------------------------------------
# Docs: the capability matrix is the code's
# ---------------------------------------------------------------------------


def test_capability_matrix_in_docs_matches_the_code():
    doc = (Path(__file__).parent.parent / "docs" / "steering-capture.md").read_text(encoding="utf-8")
    labels = {"live_hook": "live hook", "from_log": "from log", "from_git": "from git",
              "not_available": "not available"}
    names = {"claude_code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "opencode": "OpenCode",
             "antigravity": "Google Antigravity", "grok_build": "Grok Build", "hermes": "Hermes Agent"}
    for agent, signals in steering.CAPABILITIES.items():
        row = next((line for line in doc.splitlines() if line.startswith(f"| {names[agent]} |")), None)
        assert row is not None, agent
        cells = [c.strip() for c in row.strip("|").split("|")][1:]
        assert len(cells) == len(steering.SIGNALS)
        for cell, signal in zip(cells, steering.SIGNALS, strict=True):
            assert cell.startswith(labels[signals[signal][0]]), (agent, signal, cell)
