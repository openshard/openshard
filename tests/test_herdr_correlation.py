"""Herdr attribution: launch context, delegation parsing and parent-side edges.

Herdr is never contacted: ``subprocess.run`` is replaced by a fake.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from openshard.adapters.claude_hooks import ReducedHookPayload, handle_hook
from openshard.history.correlation import CONTEXT_ENV, launch_correlation, workflow_timeline
from openshard.history.receipt_evidence import session_key_for
from openshard.util import herdr

PANE_ENV = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w5:p2", "HERDR_WORKSPACE_ID": "w5"}


def _info(agent="claude", session="abc-123", terminal="term_65dd"):
    return json.dumps({"id": "cli:agent:get", "result": {"agent": {
        "agent": agent, "agent_session": {"kind": "id", "value": session},
        "terminal_id": terminal, "cwd": "C:/secret/cwd", "pane_id": "w5:p2"}, "type": "agent_info"}}).encode()


@pytest.fixture
def fake_herdr(monkeypatch, tmp_path):
    exe = tmp_path / "herdr.exe"
    exe.write_text("")
    calls: list[list[str]] = []
    state: dict = {"out": _info(), "code": 0, "exc": None, "calls": calls,
                   "env": {**PANE_ENV, "HERDR_BIN_PATH": str(exe)}}

    def run(argv, **kw):
        calls.append(argv)
        if state["exc"]:
            raise state["exc"]
        assert kw["timeout"] <= 2.0
        return subprocess.CompletedProcess(argv, state["code"], stdout=state["out"], stderr=b"")

    monkeypatch.setattr(herdr.subprocess, "run", run)
    return state


def test_no_herdr_env_changes_nothing(fake_herdr):
    assert launch_correlation({}) is None
    assert launch_correlation({"HERDR_PANE_ID": "w5:p2"}) is None
    assert fake_herdr["calls"] == []


def test_pane_context(fake_herdr):
    assert launch_correlation(fake_herdr["env"]) == {
        "evidence": "declared", "source": "herdr",
        "external_ids": [{"namespace": "herdr.terminal", "id": "term_65dd"},
                         {"namespace": "herdr.pane", "id": "w5/p2"}]}
    launch_correlation(fake_herdr["env"])
    assert len(fake_herdr["calls"]) == 1  # cached per pane


@pytest.mark.parametrize("bad", [{"code": 1}, {"out": b"{nope"}, {"out": b"[]"},
                                 {"exc": subprocess.TimeoutExpired("h", 2)}, {"exc": OSError("gone")}])
def test_failure_skips_terminal_only(fake_herdr, bad):
    fake_herdr.update(bad)
    block = launch_correlation(fake_herdr["env"])
    assert block["external_ids"] == [{"namespace": "herdr.pane", "id": "w5/p2"}]
    assert "C:/secret" not in json.dumps(block)


def test_explicit_context_wins_and_caps(fake_herdr):
    explicit = {"source": "github", "external_ids": [{"namespace": "ci", "id": "1"}]}
    env = {**fake_herdr["env"], CONTEXT_ENV: json.dumps(explicit)}
    block = launch_correlation(env)
    assert block["source"] == "github"
    assert block["external_ids"][0] == {"namespace": "ci", "id": "1"} and len(block["external_ids"]) == 3
    full = [{"namespace": "ci", "id": str(i)} for i in range(16)]
    env[CONTEXT_ENV] = json.dumps({"external_ids": full})
    assert launch_correlation(env)["external_ids"] == full


@pytest.mark.parametrize("command,targets", [
    ('herdr agent prompt w5:p3 "do the thing"', ["w5:p3"]),
    # Seen live: Claude delegated through Herdr's own exported binary variable.
    ('"$HERDR_BIN_PATH" agent prompt w5:p8 "do x" --wait', ["w5:p8"]),
    ("${HERDR_BIN_PATH} agent prompt w5:p8 hi", ["w5:p8"]),
    ("& $env:HERDR_BIN_PATH agent send-keys w5:p8 enter", ["w5:p8"]),
    ("$HERDR_BIN_PATH_OTHER agent prompt w5:p8 hi", []),
    ('"C:\\Users\\Me\\.herdr\\bin\\herdr.exe" agent prompt w5:p3 hi', ["w5:p3"]),
    ('& "C:\\Program Files\\herdr\\herdr.exe" agent send-keys w5:p3 esc', ["w5:p3"]),
    ("HERDR_X=1 herdr agent prompt --wait --timeout 5 w1:p1 'a b'", ["w1:p1"]),
    ("herdr agent start helper --kind claude --pane w2:p4", ["w2:p4"]),
    ("herdr agent start helper --kind claude --pane=w2:p4 -- --flag", ["w2:p4"]),
    ("cd x && herdr agent prompt w1:p1 a; herdr agent prompt w1:p2 b", ["w1:p1", "w1:p2"]),
    ('bash -c "herdr agent prompt w1:p9 hi"', ["w1:p9"]),
    ("herdr agent get w5:p3", []),
    ("herdr agent list", []),
    ("herdr agent read w5:p3", []),
    ("herdr agent wait w5:p3", []),
    ("herdr workspace create", []),
    ('echo "herdr agent prompt w5:p3 x"', []),
    ("herdr agent prompt", []),
    ("herdr agent prompt -- -rf", []),
    ("notherdr agent prompt w5:p3 hi", []),
    ("herdr agent start helper --kind claude", []),
    (None, []),
])
def test_delegation_parsing(command, targets):
    assert herdr.delegation_targets(command) == targets


def test_delegated_sessions_use_child_session_key(fake_herdr, monkeypatch):
    monkeypatch.setenv("HERDR_BIN_PATH", fake_herdr["env"]["HERDR_BIN_PATH"])
    keys = herdr.delegated_sessions("herdr agent prompt w5:p3 hi")
    assert keys == [session_key_for("claude_code", "abc-123")]
    assert "abc-123" not in json.dumps(keys)
    both = "herdr agent prompt w5:p3 hi && herdr agent send-keys w5:p3 esc"
    assert herdr.delegated_sessions(both) == keys
    fake_herdr["out"] = _info(agent="gemini")
    assert herdr.delegated_sessions("herdr agent prompt w5:p4 hi") == []
    fake_herdr["out"] = b"garbage"
    assert herdr.delegated_sessions("herdr agent prompt w5:p5 hi") == []
    assert herdr.delegated_sessions("herdr agent get w5:p5") == []


def test_reduced_payload_roundtrip_drops_bad_ids():
    good = "ses_" + "a" * 32
    reduced = ReducedHookPayload.from_dict({
        "event": "PostToolUse", "session_id": "s1", "herdr_delegated": [good, good, "bad id", 5]})
    assert reduced is not None and reduced.herdr_delegated == [good]


def test_delegation_recorded_on_parent_receipt(fake_herdr, monkeypatch, tmp_path):
    from openshard.history.store import load_history

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-q", str(repo)])
    monkeypatch.setenv("HERDR_BIN_PATH", fake_herdr["env"]["HERDR_BIN_PATH"])
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    sid = "11111111-2222-3333-4444-555555555555"
    corr = launch_correlation(fake_herdr["env"])

    def fire(event, **extra):
        return handle_hook({"hook_event_name": event, "session_id": sid, "cwd": str(repo), **extra},
                           env=env, correlation=corr)

    fire("SessionStart", source="startup")
    fire("PostToolUse", tool_name="Bash", tool_input={"command": 'herdr agent prompt w5:p3 "go"'},
         tool_response={"interrupted": False})
    fire("Stop")
    fire("SessionEnd", reason="other")
    entries = load_history(repo / ".openshard" / "runs.jsonl")
    ids = [(i["namespace"], i["id"]) for e in entries for i in e.get("correlation", {}).get("external_ids", [])]
    assert ("herdr.delegated_session", session_key_for("claude_code", "abc-123")) in ids
    assert ("herdr.terminal", "term_65dd") in ids
    assert "abc-123" not in json.dumps(entries)


def test_timeline_herdr_terminal_filter():
    task = "task_018f4d2a-1c3e-7000-8b1a-0242ac120002"
    corr = {"external_ids": [{"namespace": "herdr.terminal", "id": "term_1"}]}
    entries = [{"task_id": task, "run_id": "a", "correlation": corr}, {"task_id": task, "run_id": "b"}]
    assert [r["run_id"] for r in workflow_timeline(entries, task_id=task, herdr_terminal="term_1")] == ["a"]
    assert len(workflow_timeline(entries, task_id=task)) == 2
