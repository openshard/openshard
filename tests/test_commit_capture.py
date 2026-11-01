"""A Receipt for one commit, rebuilt from the committing agent's own transcript.

For sessions that ran without Openshard's hooks: the commit's Receipt must
carry the agent, model, owner, prompts, tool calls, tokens and cost for the
work since the session's previous captured commit, and the commit, its files
and line counts as git saw them. Each transcript message belongs to one
commit, unknown usage stays unknown, and nothing is claimed about
verification.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import commit_capture as cc
from openshard.history.receipt_evidence import commit_value
from openshard.history.store import load_history
from tests.capture_fixtures import _make_repo

SID = "7f0c2b1e-0c55-4a1d-9a37-5b1f0d2e9c11"


def _assistant(at: str, cwd: str, blocks: list[dict], *, msg_id: str, stop: str, tokens: int = 10) -> dict:
    return {"type": "assistant", "timestamp": at, "cwd": cwd, "sessionId": SID, "uuid": f"a-{msg_id}-{at}",
            "isSidechain": False,
            "message": {"role": "assistant", "id": msg_id, "model": "claude-opus-5-5", "stop_reason": stop,
                        "content": blocks,
                        "usage": {"input_tokens": tokens, "output_tokens": tokens * 2, "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 0}}}


def _user(at: str, cwd: str, content) -> dict:
    return {"type": "user", "timestamp": at, "cwd": cwd, "sessionId": SID, "uuid": f"u-{at}", "isSidechain": False,
            "message": {"role": "user", "content": content}}


def _tool(tool_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _result(tool_id: str, *, is_error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "output", "is_error": is_error}


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def _commit(repo: Path, name: str, body: str, at: str, message: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    env = {"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at, "PATH": __import__("os").environ["PATH"],
           "HOME": str(repo.parent), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", message],
                   cwd=repo, check=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    repo = _make_repo(tmp_path / "repo")
    cwd = str(repo)
    records = [
        _user("2026-11-01T10:00:00Z", cwd, "Add the parser"),
        _assistant("2026-11-01T10:00:05Z", cwd, [_tool("t1", "Write", file_path=f"{cwd}/parser.py", content="x")],
                   msg_id="m1", stop="tool_use"),
        _user("2026-11-01T10:00:06Z", cwd, [_result("t1")]),
        _assistant("2026-11-01T10:00:10Z", cwd, [_tool("t2", "Bash", command="python -m pytest -q")],
                   msg_id="m2", stop="tool_use"),
        _user("2026-11-01T10:00:20Z", cwd, [_result("t2", is_error=True)]),
        _assistant("2026-11-01T10:00:30Z", cwd, [{"type": "text", "text": "done"}], msg_id="m3", stop="end_turn"),
        # after the first commit (10:01:00)
        _user("2026-11-01T10:05:00Z", cwd, "Now the docs"),
        _assistant("2026-11-01T10:05:05Z", cwd, [_tool("t3", "Edit", file_path=f"{cwd}/README.md", old_string="a",
                                                         new_string="b")], msg_id="m4", stop="tool_use", tokens=40),
        _user("2026-11-01T10:05:06Z", cwd, [_result("t3")]),
        _assistant("2026-11-01T10:05:10Z", cwd, [{"type": "text", "text": "ok"}], msg_id="m5", stop="end_turn", tokens=40),
    ]
    transcript = tmp_path / "claude" / "projects" / "p" / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return repo, transcript


def test_commit_receipt_carries_the_agents_work_and_gits_view_of_the_commit(setup) -> None:
    repo, transcript = setup
    sha = _commit(repo, "parser.py", "a = 1\nb = 2\n", "2026-11-01T10:01:00Z", "Add the parser")
    report = cc.capture_commit(repo, transcript=transcript, write=True)
    assert report.status == "written"
    assert report.receipt_id == cc.receipt_id_for_commit(SID, sha)
    entry = report.entry
    assert entry["task"] == "Add the parser" and entry["task_title"]
    assert entry["execution_model"] == "claude-opus-5-5"
    capture = entry["capture"]
    assert capture["prompt_count"] == 1
    assert capture["tool_call_count"] == 2 and capture["tool_failure_count"] == 1
    # usage of m1..m3 only: 3 messages x 10 input
    assert entry["prompt_tokens"] == 30 and entry["completion_tokens"] == 60
    assert isinstance(entry["estimated_cost"], float) and entry["cost_is_estimate"] is True
    # git, not the agent, says what the commit is
    assert entry["git_end_head"] == sha and commit_value(entry) == sha
    assert [f["path"] for f in entry["files_detail"]] == ["parser.py"]
    assert entry["files_detail"][0]["attribution"] == "git_observed"
    assert (entry["lines_added"], entry["lines_removed"]) == (2, 0)
    assert entry["duration_seconds"] == 60.0  # first record 10:00:00 to the commit at 10:01:00
    assert entry["verification"]["status"] != "passed"  # verification is the CI's to give, never claimed here
    stored = [e for e in load_history(repo / ".openshard" / "runs.jsonl", coerce=False) if e.get("receipt_id") == report.receipt_id]
    assert len(stored) == 1


def test_each_message_belongs_to_one_commit(setup) -> None:
    repo, transcript = setup
    _commit(repo, "parser.py", "a = 1\n", "2026-11-01T10:01:00Z", "Add the parser")
    first = cc.capture_commit(repo, transcript=transcript, write=True)
    second_sha = _commit(repo, "README.md", "docs\n", "2026-11-01T10:06:00Z", "Write the docs")
    second = cc.capture_commit(repo, transcript=transcript, write=True)
    assert second.window["since"] == first.window["until"] == "2026-11-01T10:01:00Z"
    entry = second.entry
    assert entry["git_end_head"] == second_sha
    assert entry["capture"]["prompt_count"] == 1 and entry["capture"]["tool_call_count"] == 1
    assert entry["prompt_tokens"] == 80  # m4 + m5 only
    assert entry["task"] == "Write the docs"


def test_capturing_the_same_commit_twice_writes_once(setup) -> None:
    repo, transcript = setup
    _commit(repo, "parser.py", "a = 1\n", "2026-11-01T10:01:00Z", "Add the parser")
    assert cc.capture_commit(repo, transcript=transcript, write=True).status == "written"
    assert cc.capture_commit(repo, transcript=transcript, write=True).status == "already_recorded"


def test_no_usage_in_the_window_stays_unknown_never_zero(setup, tmp_path: Path) -> None:
    repo, transcript = setup
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    for rec in lines:
        if rec["type"] == "assistant":
            rec["message"].pop("usage")
    transcript.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    _commit(repo, "parser.py", "a = 1\n", "2026-11-01T10:01:00Z", "Add the parser")
    entry = cc.capture_commit(repo, transcript=transcript).entry
    assert entry.get("prompt_tokens") is None and entry.get("estimated_cost") is None
    assert entry["capture"]["tokens_not_recorded_reason"]


def test_an_empty_window_is_refused(setup) -> None:
    repo, transcript = setup
    _commit(repo, "parser.py", "a = 1\n", "2026-11-01T09:00:00Z", "Before the session")
    with pytest.raises(cc.CaptureUnavailable):
        cc.capture_commit(repo, transcript=transcript)


def test_cli_dry_run_says_what_it_would_record(setup) -> None:
    from openshard.cli.main import cli

    repo, transcript = setup
    _commit(repo, "parser.py", "a = 1\n", "2026-11-01T10:01:00Z", "Add the parser")
    out = CliRunner().invoke(cli, ["import", "claude-commit", "--transcript", str(transcript), "--repo", str(repo)])
    assert out.exit_code == 0, out.output
    assert "Would write Receipt rcpt_" in out.output and "tool calls: 2 (1 failed)" in out.output
    assert not (repo / ".openshard" / "runs.jsonl").exists() or "rcpt_" not in (repo / ".openshard" / "runs.jsonl").read_text()


def test_a_prompt_in_the_starting_second_is_inside_the_window(setup) -> None:
    repo, transcript = setup
    _commit(repo, "parser.py", "a = 1\n", "2026-11-01T10:01:00Z", "Add the parser")
    entry = cc.capture_commit(repo, transcript=transcript, since="2026-11-01T09:59:59.500Z").entry
    assert entry["capture"]["prompt_count"] == 1


def test_after_commit_records_the_commit_that_landed_on_the_old_head(setup) -> None:
    repo, transcript = setup
    old = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "parser.py", "a = 1\n", "2026-11-01T10:01:00Z", "Add the parser")
    import os
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    # the transcript is found by session id under ~/.claude/projects: point the roots at the fixture
    import openshard.adapters.commit_usage_trailers as cut
    original = cut._transcript_roots
    cut._transcript_roots = lambda: [transcript.parent.parent]
    try:
        rid = cc.run_after_commit(repo, old, SID, wait=1, sync=False)
    finally:
        cut._transcript_roots = original
    assert rid == cc.receipt_id_for_commit(SID, sha)
    assert rid in (repo / ".openshard" / "runs.jsonl").read_text()


def test_after_commit_records_nothing_when_the_commit_never_landed(setup) -> None:
    repo, _ = setup
    old = _git(repo, "rev-parse", "HEAD")
    assert cc.run_after_commit(repo, old, SID, wait=0.6, sync=False) is None
    assert not (repo / ".openshard" / "runs.jsonl").exists()


def test_the_commit_hook_marks_and_schedules_only_unobserved_sessions_with_a_transcript(setup, monkeypatch) -> None:
    import openshard.adapters.commit_usage_trailers as cut
    from openshard.adapters import agent_commit_trailer as act

    repo, transcript = setup
    monkeypatch.setattr(cut, "_transcript_roots", lambda: [transcript.parent.parent])
    started: list = []
    monkeypatch.setattr(act, "_spawn", lambda argv, kwargs: started.append(argv))
    msg = repo / "MSG"
    msg.write_text("Add the parser\n", encoding="utf-8")
    env = {"CLAUDE_CODE_SESSION_ID": SID, "PATH": __import__("os").environ["PATH"]}
    assert act.schedule_commit_capture(msg, repo, env) is True
    assert "Openshard-Agent: Claude Code" in msg.read_text()
    assert started and started[0][-4:-2] == ["claude-commit-capture", str(repo)] and started[0][-1] == SID
    # opted out, or no transcript for the session: nothing
    assert act.schedule_commit_capture(msg, repo, {**env, "OPENSHARD_COMMIT_CAPTURE": "off"}) is False
    assert act.schedule_commit_capture(msg, repo, {**env, "CLAUDE_CODE_SESSION_ID": "deadbeef-0000"}) is False


def test_a_scheduled_capture_adds_no_usage_trailers_to_the_message(setup, monkeypatch) -> None:
    import openshard.adapters.commit_usage_trailers as cut
    from openshard.adapters import agent_commit_trailer as act

    repo, transcript = setup
    monkeypatch.setattr(cut, "_transcript_roots", lambda: [transcript.parent.parent])
    monkeypatch.setattr(act, "_spawn", lambda argv, kwargs: None)
    msg = repo / "MSG"
    msg.write_text("Add the parser\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    act.run_git_hook([str(msg)], {"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_CODE_REMOTE": "true",
                                  "PATH": __import__("os").environ["PATH"]})
    text = msg.read_text()
    assert "Openshard-Agent: Claude Code" in text and "Openshard-Tokens" not in text
