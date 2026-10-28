"""Steering from every agent's own local log and Core's own integrations (docs/steering-capture.md).

Each test drives the real hook path in a throw-away git repository with an
isolated home, with fixture logs shaped like each agent's own (the formats and
their sources are in ``adapters/steering_logs.py``). The last class checks that
no text from any newly read log, and no follow-up prompt's words, is stored,
synced or sent.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from openshard.adapters import steering_logs
from openshard.adapters.claude_hooks import extract_hook_payload, handle_hook
from openshard.adapters.claude_hooks_install import HOOK_SPECS
from openshard.adapters.opencode_plugin_install import render_plugin_source
from openshard.history.receipt_render import render_compact_shard_receipt
from openshard.history.shard_contract import build_shard_receipt
from openshard.sync import envelope
from tests.capture_fixtures import _git, _make_repo
from tests.test_steering_capture import (
    SECRET,
    SID,
    _all_text,
    _block,
    _claude,
    _claude_log,
    _claude_transcript,
    _entries,
    _kinds,
    _now_ms,
    _one,
    _rejection,
    _wait_next_second,
)

PHRASES = {  # distinctive words that only ever appear in a message read from an agent's own log
    "cursor": "tangerine-oscilloscope",
    "cursor_db": "bubble-quokka-lighthouse",
    "antigravity": "saffron-gyroscope",
    "grok": "cobalt-hummingbird",
    "hermes": "juniper-trebuchet",
    "opencode": "obsidian-carousel",
}


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


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    for name in ("XDG_CONFIG_HOME", "GROK_HOME", "HERMES_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    return root


def _jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Cursor: agent transcript (turn_ended rows) + state.vscdb userDecision
# ---------------------------------------------------------------------------


def _cursor_transcript(home: Path) -> Path:
    return home / ".cursor" / "projects" / "work-widget" / "agent-transcripts" / f"{SID}.jsonl"


def _cursor_user(text: str) -> dict:
    return {"role": "user", "message": {"content": [
        {"type": "text", "text": f"<timestamp>Monday</timestamp>\n<user_query>{text}</user_query>"}]}}


def _cursor_db(home: Path, rows: list[tuple[str, dict]]) -> Path:
    path = steering_logs.cursor_state_db()
    assert str(path).startswith(str(home))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        db.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [(k, json.dumps(v)) for k, v in rows])
    return path


def _cursor_session(repo: Path, home: Path) -> None:
    transcript = _cursor_transcript(home)

    def hook(event: str, **fields):
        doc = {"conversation_id": SID, "generation_id": "g1", "hook_event_name": event, "model": "m",
               "workspace_roots": [str(repo)], "transcript_path": str(transcript), **fields}
        return handle_hook(doc, env={}, agent="cursor")

    _jsonl(transcript, _cursor_user("add a dark mode toggle"))
    hook("beforeSubmitPrompt", prompt="add a dark mode toggle")
    _wait_next_second()
    # Sent while the generation was still running: no turn_ended row and no stop hook before it.
    _jsonl(transcript, {"role": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
           _cursor_user(f"also persist it in local storage {PHRASES['cursor']}"))
    hook("beforeSubmitPrompt", prompt=f"also persist it in local storage {PHRASES['cursor']}")
    _jsonl(transcript, {"type": "turn_ended", "status": "success"})
    _cursor_db(home, [
        (f"bubbleId:{SID}:b1", {"type": 2, "createdAt": _now_ms(), "text": PHRASES["cursor_db"],
                                "toolFormerData": {"name": "run_terminal_cmd", "userDecision": "accepted"}}),
        (f"bubbleId:{SID}:b2", {"type": 2, "createdAt": int(time.time() * 1000) + 5,
                                "toolFormerData": {"name": "edit_file", "userDecision": "rejected"}}),
        (f"bubbleId:{SID}:b3", {"type": 2, "toolFormerData": {"userDecision": "accepted"}}),  # no time: skipped
        ("bubbleId:another-conversation:b1", {"type": 2, "createdAt": _now_ms(),
                                              "toolFormerData": {"userDecision": "rejected"}}),
    ])
    hook("stop", status="completed", loop_count=0)
    _wait_next_second()
    _jsonl(transcript, _cursor_user("that's wrong, keep the light theme default"))
    hook("beforeSubmitPrompt", prompt="that's wrong, keep the light theme default")
    _jsonl(transcript, {"type": "turn_ended", "status": "success"})
    hook("stop", status="completed", loop_count=0)


class TestCursor:
    def test_transcript_marks_a_prompt_sent_mid_generation_and_the_store_gives_decisions(self, repo, home):
        _cursor_session(repo, home)
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interjection", "permission_decision", "permission_decision",
                                 "follow_up"]
        interjection = _one(entry, "interjection")
        assert (interjection["intent"], interjection["source"]) == ("new_task", "transcript")
        follow_up = _one(entry, "follow_up")
        assert (follow_up["intent"], follow_up["after"]) == ("correction", "turn_end")
        decisions = [(e["decision"], e["source"]) for e in _block(entry)["events"] if e["kind"] == "permission_decision"]
        assert decisions == [("allow", "transcript"), ("deny", "transcript")]
        block = _block(entry)
        assert block["not_available"] == {}
        assert block["log_formats"] == [steering_logs.FORMAT_CURSOR, steering_logs.FORMAT_CURSOR_STATE]
        assert block["autonomy"]["permission_prompts"] == {"asked": None, "allowed": 1, "denied": 1}
        assert block["autonomy"]["human_inputs_by_kind"]["interjection"] == 1

    def test_a_transcript_outside_cursor_or_of_another_conversation_is_never_read(self, home):
        other = home / ".cursor" / "projects" / "w" / "agent-transcripts" / "someone-else.jsonl"
        assert steering_logs.valid_log_path("cursor", str(other), SID) is None
        assert steering_logs.valid_log_path("cursor", str(home / f"{SID}.jsonl"), SID) is None
        assert steering_logs.valid_log_path("cursor", str(_cursor_transcript(home)), SID) is not None

    def test_an_unreadable_store_or_unknown_lines_never_break_capture(self, repo, home):
        path = steering_logs.cursor_state_db()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a database")
        transcript = _cursor_transcript(home)
        _jsonl(transcript, {"weird": True}, {"role": "user", "message": "no query tag"})
        transcript.open("a", encoding="utf-8").write("{broken json\n")
        doc = {"conversation_id": SID, "hook_event_name": "beforeSubmitPrompt", "prompt": "go",
               "workspace_roots": [str(repo)], "transcript_path": str(transcript)}
        handle_hook(doc, env={}, agent="cursor")
        handle_hook({**doc, "hook_event_name": "stop", "status": "completed"}, env={}, agent="cursor")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt"]


# ---------------------------------------------------------------------------
# Antigravity: transcript USER_INPUT steps + Stop terminationReason
# ---------------------------------------------------------------------------


def _antigravity_transcript(home: Path) -> Path:
    return home / ".gemini" / "antigravity" / "brain" / SID / ".system_generated" / "logs" / "transcript.jsonl"


def _step(index: int, text: str, kind: str = "USER_INPUT", source: str = "USER_EXPLICIT") -> dict:
    content = f"<USER_REQUEST>{text}</USER_REQUEST>\n<ADDITIONAL_METADATA>cursor at a.py</ADDITIONAL_METADATA>"
    return {"step_index": index, "source": source, "type": kind, "status": "DONE", "created_at": _now_ms(),
            "content": content if kind == "USER_INPUT" else text}


def _antigravity_session(repo: Path, home: Path) -> None:
    transcript = _antigravity_transcript(home)

    def hook(event: str, **fields):
        doc = {"conversationId": SID, "workspacePaths": [str(repo)], "modelName": "gemini-3",
               "transcriptPath": str(transcript), **fields}
        return handle_hook(doc, env={}, agent="antigravity", event_override=event)

    first = _step(0, "add a dark mode toggle")
    _jsonl(transcript, first)
    hook("PreInvocation")
    _wait_next_second()
    _jsonl(transcript, _step(1, "planning", "PLANNER_RESPONSE", "MODEL"),
           _step(2, f"actually use css variables {PHRASES['antigravity']}"), first)  # a repeated step is read once
    _wait_next_second()
    hook("Stop", terminationReason="NO_TOOL_CALL", fullyIdle=True)
    _wait_next_second()
    _jsonl(transcript, _step(3, "that's wrong, revert the toggle"))
    hook("PreInvocation")
    _wait_next_second()
    hook("Stop", terminationReason="USER_CANCELED", fullyIdle=True)


class TestAntigravity:
    def test_prompts_follow_ups_and_mid_turn_messages_from_the_transcript_and_a_cancel_from_stop(self, repo, home):
        _antigravity_session(repo, home)
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interjection", "follow_up", "interrupt"]
        assert all(e["source"] == "transcript" for e in _block(entry)["events"][:3])
        assert _one(entry, "interjection")["intent"] == "redirect"
        follow_up = _one(entry, "follow_up")
        assert (follow_up["intent"], follow_up["after"]) == ("correction", "turn_end")
        assert follow_up["think_seconds"] >= 0
        assert _one(entry, "interrupt")["source"] == "hook"
        block = _block(entry)
        assert block["log_formats"] == [steering_logs.FORMAT_ANTIGRAVITY]
        assert set(block["not_available"]) == {"permission_decision"}
        assert entry["capture"]["turn_count"] == 1  # the cancelled turn is not a completed one

    def test_other_termination_reasons_are_never_a_person_s_stop(self, repo):
        from openshard.adapters.antigravity_hooks import extract_antigravity_payload

        for reason, event in (("USER_CANCELED", "Interrupt"), ("USER_CANCELLED", "Interrupt"),
                              ("MAX_INVOCATIONS", "Stop"), ("ERROR", "Stop"), (None, "Stop")):
            doc = {"conversationId": SID, "workspacePaths": [str(repo)], "terminationReason": reason}
            assert extract_antigravity_payload(doc, event_override="Stop").event == event, reason


# ---------------------------------------------------------------------------
# Grok Build: StopCancelled reason / cancelledBy + updates.jsonl
# ---------------------------------------------------------------------------


def _grok_dir(home: Path) -> Path:
    return home / ".grok" / "sessions" / "%2Fwork%2Fwidget" / SID


def _update(kind: str, **fields) -> dict:
    return {"timestamp": _now_ms(), "method": "session/update",
            "params": {"sessionId": SID, "update": {"sessionUpdate": kind, **fields}}}


def _grok_session(repo: Path, home: Path) -> None:
    updates = _grok_dir(home) / "updates.jsonl"

    def hook(event: str, **fields):
        snake = {"UserPromptSubmit": "user_prompt_submit", "Stop": "stop", "StopCancelled": "stop_cancelled",
                 "PermissionDenied": "permission_denied"}[event]
        doc = {"sessionId": SID, "cwd": str(repo), "workspaceRoot": str(repo), "hook_event_name": event,
               "hookEventName": snake, "transcriptPath": str(updates), **fields}
        return handle_hook(doc, env={}, agent="grok_build")

    _jsonl(updates, _update("user_message_chunk", content={"type": "text", "text": "add a cli"}))
    hook("UserPromptSubmit", prompt="add a cli")
    _wait_next_second()
    _jsonl(updates, _update("agent_message_chunk", content={"type": "text", "text": "on it"}),
           _update("tool_call", toolCallId="t1", title="run"),
           _update("user_message_chunk", content={"type": "text", "text": f"also add a --verbose flag {PHRASES['grok']}"}),
           _update("user_message_chunk", content={"type": "text", "text": " (continued)"}),
           _update("_x.ai/permission_decision", toolCallId="t2", outcome={"outcome": "selected", "optionKind": "allow_once"}),
           _update("agent_message_chunk", content={"type": "text", "text": "done"}),
           _update("turn_completed", stopReason="end_turn"))
    hook("Stop", reason="end_turn")
    _wait_next_second()
    hook("UserPromptSubmit", prompt="run the full test suite")
    hook("PermissionDenied", toolName="run_terminal_command")
    hook("StopCancelled", reason="permission_rejected", cancelledBy="user", cancelTrigger="mouse")
    _wait_next_second()
    hook("UserPromptSubmit", prompt="ok go ahead")
    hook("StopCancelled", reason="user_interrupt", cancelledBy="user", cancelTrigger="ctrl_c")
    _wait_next_second()
    hook("UserPromptSubmit", prompt="continue")
    hook("StopCancelled", reason="max_turns", cancelledBy="runtime")


class TestGrokBuild:
    def test_person_declines_and_interrupts_from_stop_cancelled_and_mid_turn_messages_from_updates(self, repo, home):
        _grok_session(repo, home)
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interjection", "permission_decision", "follow_up",
                                 "permission_decision", "follow_up", "interrupt", "follow_up"]
        assert _one(entry, "interjection")["intent"] == "new_task"
        decisions = [(e["decision"], e["source"]) for e in _block(entry)["events"] if e["kind"] == "permission_decision"]
        assert decisions == [("allow", "transcript"), ("deny", "hook")]
        assert _one(entry, "interrupt")["source"] == "hook"  # max_turns is the runtime's: no second interrupt
        block = _block(entry)
        assert block["not_available"] == {} and block["log_formats"] == [steering_logs.FORMAT_GROK]
        assert block["autonomy"]["permission_prompts"] == {"asked": None, "allowed": None, "denied": 1}

    def test_a_runtime_cancel_or_a_declined_prompt_without_who_is_never_a_person(self, repo):
        from openshard.adapters.grok_build_hooks import extract_grok_build_payload

        def payload(**fields):
            return extract_grok_build_payload({"sessionId": SID, "hook_event_name": "StopCancelled", **fields})

        assert payload(reason="user_interrupt", cancelledBy="user").event == "Interrupt"
        assert payload(reason="user_interrupt").event == "SessionIdle"  # who cancelled is not said
        assert payload(reason="max_turns", cancelledBy="runtime").attrs["cancelled_by"] == "runtime"
        assert payload(reason="permission_rejected").attrs == {}


# ---------------------------------------------------------------------------
# Hermes: state.db steer / queued rows
# ---------------------------------------------------------------------------


def _hermes_db(tmp_path: Path, monkeypatch) -> Path:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    path = hermes_home / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (32)")
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                   "tool_calls TEXT, timestamp REAL NOT NULL, display_kind TEXT, display_metadata TEXT, "
                   "active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, extra_unknown TEXT)")
    return path


def _hermes_row(path: Path, role: str, content: str, *, kind: str | None = None, meta: dict | None = None,
                active: int = 1, session: str = SID) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO messages (session_id, role, content, timestamp, display_kind, display_metadata, "
                   "active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (session, role, content, time.time(), kind, json.dumps(meta) if meta else None, active))


def _hermes_session(repo: Path, db: Path) -> None:
    (repo / ".openshard").mkdir(exist_ok=True)

    def hook(event: str, **extra):
        doc = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SID,
               "cwd": str(repo), "extra": extra}
        return handle_hook(doc, env={}, agent="hermes")

    _hermes_row(db, "user", "deploy the staging stack")
    hook("pre_llm_call", user_message="deploy the staging stack", model="m")
    _wait_next_second()
    _hermes_row(db, "assistant", "running terraform")
    _hermes_row(db, "user", "[OUT-OF-BAND USER MESSAGE — sent while you were working]\n"
                            f"actually use the blue cluster {PHRASES['hermes']}\n[/OUT-OF-BAND USER MESSAGE]",
                kind="steer")
    _hermes_row(db, "user", "also run the smoke tests", meta={"_queued_prompt": True}, active=0)
    _hermes_row(db, "user", f"another session's steer {PHRASES['hermes']}", kind="steer", session="other")
    hook("on_session_end", completed=True, interrupted=False)
    _wait_next_second()
    _hermes_row(db, "user", "also run the smoke tests")  # the queued message, delivered as the next turn
    hook("pre_llm_call", user_message="also run the smoke tests", model="m")
    hook("on_session_end", completed=True, interrupted=False)


class TestHermes:
    def test_steered_and_queued_messages_from_state_db_are_interjections_counted_once(self, repo, tmp_path, monkeypatch):
        db = _hermes_db(tmp_path, monkeypatch)
        _hermes_session(repo, db)
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interjection", "interjection", "follow_up"]
        steer, queued = [e for e in _block(entry)["events"] if e["kind"] == "interjection"]
        assert (steer["intent"], steer["source"]) == ("redirect", "transcript")
        assert queued["intent"] == "new_task"
        assert _one(entry, "follow_up")["from_queue"] is True
        block = _block(entry)
        assert block["autonomy"]["human_inputs"] == 3  # the delivered queue message was counted where it was typed
        assert block["log_formats"] == ["hermes-state-db/v32"] and block["not_available"] == {}

    def test_a_database_of_another_shape_is_not_read(self, repo, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hh"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        with sqlite3.connect(hermes_home / "state.db") as db:
            db.execute("CREATE TABLE messages (id INTEGER, body TEXT)")
        (repo / ".openshard").mkdir(exist_ok=True)
        doc = {"hook_event_name": "pre_llm_call", "session_id": SID, "cwd": str(repo), "extra": {"user_message": "go"}}
        handle_hook(doc, env={}, agent="hermes")
        handle_hook({**doc, "hook_event_name": "on_session_end", "extra": {"completed": True}}, env={}, agent="hermes")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt"] and "log_formats" not in _block(entry)


# ---------------------------------------------------------------------------
# OpenCode: plugin v6 (abort, permission replies, busy)
# ---------------------------------------------------------------------------


def _opencode(repo: Path, event: str, **fields):
    doc = {"agent": "opencode", "event": event, "session_id": SID, "directory": str(repo), "worktree": str(repo),
           **fields}
    return handle_hook(doc, env={}, agent="opencode")


class TestOpenCode:
    def test_busy_messages_permission_replies_and_aborts(self, repo):
        _opencode(repo, "session.created")
        _opencode(repo, "chat.message", prompt="add a cache", while_busy=False)
        _wait_next_second()
        _opencode(repo, "chat.message", prompt=f"also cache misses {PHRASES['opencode']}", while_busy=True)
        _opencode(repo, "permission.asked", permission="bash")
        _opencode(repo, "permission.replied", reply="once")
        _opencode(repo, "permission.asked", permission="edit")
        _opencode(repo, "permission.replied", reply="reject")
        _opencode(repo, "permission.replied", reply="maybe")  # not one of OpenCode's answers: ignored
        _opencode(repo, "session.aborted")
        _opencode(repo, "session.idle")
        _wait_next_second()
        _opencode(repo, "chat.message", prompt="continue")
        _opencode(repo, "session.idle")
        (entry,) = _entries(repo)
        assert _kinds(entry) == ["initial_prompt", "interjection", "permission_decision", "permission_decision",
                                 "interrupt", "follow_up"]
        assert _one(entry, "interjection")["source"] == "hook"
        block = _block(entry)
        assert [e["decision"] for e in block["events"] if e["kind"] == "permission_decision"] == ["allow", "deny"]
        assert block["autonomy"]["permission_prompts"] == {"asked": 2, "allowed": 1, "denied": 1}
        assert block["not_available"] == {}
        assert _one(entry, "follow_up")["after"] == "turn_end"

    def test_plugin_forwards_from_the_passive_bus_and_never_answers_a_prompt(self):
        source = render_plugin_source(4096)
        for needle in ('"session.status"', "MessageAbortedError", '"permission.replied"', '"permission.asked"',
                       "while_busy"):
            assert needle in source
        assert '"permission.ask"' not in source and "output.status" not in source


# ---------------------------------------------------------------------------
# Claude Code: non-blocking Notification -> approvals
# ---------------------------------------------------------------------------


class TestClaudeCodeApprovals:
    def test_notification_hook_is_installed_and_cannot_decide(self):
        (spec,) = [s for s in HOOK_SPECS if s.event == "Notification"]
        assert spec.matcher == "permission_prompt"
        assert extract_hook_payload({"hook_event_name": "Notification", "session_id": SID,
                                     "notification_type": "idle_prompt", "message": "waiting"}) is None

    def test_a_prompt_then_the_tool_running_is_an_allow_and_a_rejection_a_deny(self, repo):
        _claude_transcript().write_text("", encoding="utf-8")
        _claude(repo, "SessionStart", source="startup")
        _claude(repo, "UserPromptSubmit", prompt="tidy the repo")
        _claude(repo, "Notification", notification_type="permission_prompt",
                message="Claude needs your permission to use Bash")
        _claude(repo, "PostToolUse", tool_name="Read", tool_input={"file_path": str(repo / "README.md")},
                tool_response={})  # another tool: not the answer
        _claude(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": ""})
        _claude(repo, "Notification", notification_type="permission_prompt",
                message="Claude needs your permission to use Write")
        _claude_log(_rejection())
        _claude(repo, "Stop")
        (entry,) = _entries(repo)
        decisions = [(e["decision"], e["source"]) for e in _block(entry)["events"] if e["kind"] == "permission_decision"]
        assert decisions == [("allow", "hook"), ("deny", "transcript")]
        assert _block(entry)["autonomy"]["permission_prompts"] == {"asked": 2, "allowed": 1, "denied": 1}
        assert _block(entry)["not_available"] == {}

    def test_a_prompt_left_open_at_the_turn_end_is_never_an_allow(self, repo):
        _claude(repo, "UserPromptSubmit", prompt="tidy the repo")
        _claude(repo, "Notification", notification_type="permission_prompt",
                message="Claude needs your permission to use Bash")
        _claude(repo, "Stop")
        _wait_next_second()
        _claude(repo, "UserPromptSubmit", prompt="ok")
        _claude(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": ""})
        _claude(repo, "Stop")
        assert all("permission_decision" not in _kinds(e) for e in _entries(repo))


# ---------------------------------------------------------------------------
# Only the task's first prompt names a Receipt (founder decision, option B)
# ---------------------------------------------------------------------------


class TestFollowUpsNeverNameATask:
    def test_a_resumed_session_is_the_task_s_title_and_turn_with_no_prompt_text(self, repo):
        doc = {"session_id": SID, "turn_id": "t", "cwd": str(repo), "model": "gpt-5.5"}

        def hook(event: str, **fields):
            return handle_hook({**doc, "hook_event_name": event, **fields}, env={}, agent="codex")

        hook("UserPromptSubmit", prompt="Revamp the website hero")
        hook("Stop")
        hook("SessionEnd", reason="other")
        _wait_next_second()
        hook("SessionStart", source="resume")
        hook("UserPromptSubmit", prompt=f"make the hero taller {PHRASES['grok']}")
        hook("UserPromptSubmit", prompt="and darker")
        hook("Stop")
        hook("SessionEnd", reason="other")
        _wait_next_second()
        hook("UserPromptSubmit", prompt="one more")
        hook("Stop")
        first, second, third = _entries(repo)
        assert first["task_title"] == "Revamp website hero"
        assert (second["task"], second["capture"]["task_source"]) == ("Revamp website hero · turn 2",
                                                                      "task_title_and_turn")
        assert third["task_title"] == "Revamp website hero · turn 4"  # turn 2's Receipt held two prompts
        assert PHRASES["grok"] not in _all_text(repo / ".openshard")
        receipt = render_compact_shard_receipt(build_shard_receipt(second, index=1))
        assert "Revamp website hero · turn 2" in receipt

    def test_a_later_prompt_never_names_a_session_receipt_whose_first_prompt_had_no_text(self, repo):
        doc = {"conversation_id": SID, "workspace_roots": [str(repo)]}
        handle_hook({**doc, "hook_event_name": "beforeSubmitPrompt", "prompt": "   "}, env={}, agent="cursor")
        handle_hook({**doc, "hook_event_name": "beforeSubmitPrompt", "prompt": f"fix {PHRASES['cursor']}"},
                    env={}, agent="cursor")
        handle_hook({**doc, "hook_event_name": "stop", "status": "completed"}, env={}, agent="cursor")
        (entry,) = _entries(repo)
        assert entry["capture"]["task_source"] == "not_captured"
        assert PHRASES["cursor"] not in json.dumps(entry)

    def test_a_task_that_ended_starts_a_new_one_named_by_its_first_prompt(self, repo):
        _claude(repo, "UserPromptSubmit", prompt="Revamp the website hero")
        _claude(repo, "Stop")
        _git(repo, "checkout", "-q", "-b", "other-work")  # the task ended at a branch change
        _wait_next_second()
        _claude(repo, "UserPromptSubmit", prompt="Write the release notes")
        _claude(repo, "Stop")
        first, second = _entries(repo)
        assert second["capture"]["previous_task_ended"] == "branch_changed"
        assert (second["task"], second["capture"]["task_source"]) == ("Write the release notes",
                                                                      "first_user_prompt_excerpt")
        assert first["task_id"] != second["task_id"]


# ---------------------------------------------------------------------------
# Privacy: every newly read log, and a follow-up's words appear nowhere
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_text_from_every_newly_read_log_never_reaches_runs_receipt_sync_or_telemetry(
            self, tmp_path, home, monkeypatch):
        from openshard.telemetry import queue as telemetry_queue
        from openshard.telemetry import state as telemetry_state

        monkeypatch.delenv("OPENSHARD_TELEMETRY", raising=False)
        telemetry_state.set_consent("on", source="cli")
        telemetry_queue.clear()
        repos = {name: _make_repo(tmp_path / name) for name in ("cursor", "antigravity", "grok", "hermes", "opencode")}
        _cursor_session(repos["cursor"], home)
        _antigravity_session(repos["antigravity"], home)
        _grok_session(repos["grok"], home)
        _hermes_session(repos["hermes"], _hermes_db(tmp_path, monkeypatch))
        _opencode(repos["opencode"], "chat.message", prompt=f"start {SECRET}")
        _opencode(repos["opencode"], "chat.message", prompt=f"{PHRASES['opencode']} {SECRET}", while_busy=True)
        _opencode(repos["opencode"], "session.idle")

        surfaces: dict[str, str] = {"telemetry": json.dumps(telemetry_queue.peek(limit=500))}
        for name, root in repos.items():
            entries = _entries(root)
            assert entries and all("steering" in e["capture"] for e in entries), name
            surfaces[f"{name}/.openshard"] = _all_text(root / ".openshard")
            for i, entry in enumerate(entries):
                surfaces[f"{name}/sync/{i}"] = json.dumps(envelope.build_envelope(entry, i, core_version="9.9.9"))
                surfaces[f"{name}/receipt/{i}"] = render_compact_shard_receipt(build_shard_receipt(entry, index=i))
        for surface, text in surfaces.items():
            assert SECRET not in text and SECRET[:24] not in text, surface
            for phrase in PHRASES.values():
                assert phrase not in text, (surface, phrase)
