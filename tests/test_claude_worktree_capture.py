"""A Claude Code session that works in other checkouts: one Receipt per checkout.

A local session is authorised for the project its hooks were installed in,
but its shell moves: into a linked ``git worktree`` under ``C:\\tmp``, into
another repository's checkout, or into a scratch directory. Before this
change every event was recorded into the project root, so the Receipt
described the project's branch, HEAD and diff while the edits, commits and
test runs had happened elsewhere. Now each event is recorded into the checkout
it touched, with the session's start and the turn's prompt replayed first, so
every checkout's Receipt carries the task and start, observes its own git
facts, and says which checkout it is (``capture.checkout``). Nothing from one
checkout leaks into another's Receipt, and a turn's usage is counted once.

These tests drive a real capture service exactly as Claude Code does: the
``SessionStart`` command hook arrives with the token and ``CLAUDE_PROJECT_DIR``;
every other event is the installed HTTP hook with the project's capability and
an *empty* ``X-OpenShard-Project-Dir`` header (what Claude Code sends).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_workspace as ws
from openshard.adapters.claude_hooks_install import install_claude_hooks
from openshard.history.store import load_history
from tests.capture_fixtures import _git, _make_repo, _Service, _wait_for

SID = "7a7a7a7a-6b6b-4c7c-8d8d-9e9e9e9e9e9e"
SID2 = "8b8b8b8b-6b6b-4c7c-8d8d-9e9e9e9e9e9e"


@pytest.fixture
def repos(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """``platform`` (the project), its linked worktree ``wt``, repository ``core`` and its worktree ``core_wt``."""
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    monkeypatch.delenv("OPENSHARD_CONNECTED_SURFACE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    platform = _make_repo(tmp_path / "platform")
    _git(platform, "remote", "add", "origin", "https://github.com/openshard/platform.git")
    core = _make_repo(tmp_path / "core")
    _git(core, "remote", "add", "origin", "https://github.com/openshard/core.git")
    wt = tmp_path / "tmp" / "plat-wt"
    _git(platform, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    core_wt = tmp_path / "tmp" / "core-wt"
    _git(core, "worktree", "add", "-q", str(core_wt), "-b", "core-wt-branch")
    return {"platform": platform.resolve(), "wt": wt.resolve(), "core": core.resolve(), "core_wt": core_wt.resolve()}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def _branch(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


class _Claude:
    """Sends hook events the way Claude Code does for a session started in *project*."""

    def __init__(self, project: Path, port: int, session_id: str = SID) -> None:
        result = install_claude_hooks(repo_root=project, port=port)
        assert result.status in ("installed", "updated", "already_installed"), result.message
        settings = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        hook = settings["hooks"]["Stop"][0]["hooks"][0]
        self.url = hook["url"]
        # Claude Code interpolates $CLAUDE_PROJECT_DIR in HTTP headers to "" (see test_capture_root_fallback).
        self.headers = {k: ("" if "$" in v else v) for k, v in hook["headers"].items()}
        self.project = project
        self.port = port
        self.sid = session_id
        self.transcript = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-project" / f"{session_id}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()
        self.messages = 0

    def _body(self, event: str, cwd: Path | None, fields: dict) -> bytes:
        body = {"session_id": self.sid, "transcript_path": str(self.transcript), "cwd": str(cwd or self.project),
                "hook_event_name": event, "permission_mode": "acceptEdits", **fields}
        return json.dumps(body).encode()

    def start(self, *, cwd: Path | None = None) -> None:
        """SessionStart is a command hook: token, CLAUDE_PROJECT_DIR, the session's cwd."""
        raw = self._body("SessionStart", cwd, {"source": "startup"})
        assert client.post_hook(self.port, raw, project_dir=str(self.project), env=os.environ)

    def send(self, event: str, *, cwd: Path | None = None, **fields) -> None:
        request = urllib.request.Request(self.url, data=self._body(event, cwd, fields), method="POST",
                                         headers={"Content-Type": "application/json", **self.headers})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200

    def status(self, *, cost: float, cwd: Path | None = None) -> None:
        """The status line is a command hook too: token and CLAUDE_PROJECT_DIR."""
        raw = json.dumps({"session_id": self.sid, "cwd": str(cwd or self.project),
                          "model": {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
                          "cost": {"total_cost_usd": cost},
                          "context_window": {"current_usage": {"input_tokens": 10, "output_tokens": 2}}}).encode()
        assert client.post_status(self.port, raw, project_dir=str(self.project), env=os.environ)

    def reply(self, *, input_tokens: int, output_tokens: int) -> None:
        self.messages += 1
        line = {"type": "assistant", "sessionId": self.sid, "timestamp": _now(), "message": {
            "id": f"msg_{self.messages}", "model": "claude-opus-5-5", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }}
        with self.transcript.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    def edit(self, path: Path, text: str, *, cwd: Path | None = None) -> None:
        path.write_text(text, encoding="utf-8")
        self.send("PostToolUse", cwd=cwd, tool_name="Write", tool_input={"file_path": str(path), "content": "RAW"},
                  tool_response={"filePath": str(path)})

    def run(self, command: str, *, cwd: Path, stdout: str = "") -> None:
        self.send("PostToolUse", cwd=cwd, tool_name="Bash", tool_input={"command": command},
                  tool_response={"stdout": stdout, "stderr": "", "interrupted": False})


def _receipts(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return load_history(path, coerce=False) if path.exists() else []


def _ended(repo: Path, count: int = 1) -> bool:
    receipts = _receipts(repo)
    return len(receipts) == count and all(r["capture"].get("session_end_observed") for r in receipts)


def _changed(receipt: dict) -> set[str]:
    return {f["path"] for f in receipt["files_detail"] if not f.get("pre_existing")}


def _commit(repo: Path, message: str) -> str:
    # Never stage the capture state the running service is writing into the checkout:
    # on Windows git exits 128 when a buffer file is replaced mid-scan (seen in CI).
    _git(repo, "add", "--", ".", ":(exclude).openshard")
    _git(repo, "commit", "-q", "-m", message)
    return _head(repo)


def test_work_in_a_linked_worktree_is_attributed_to_the_worktree(repos):
    platform, wt = repos["platform"], repos["wt"]
    wt_start = _head(wt)
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.status(cost=0.0)  # the status line's first ping, before any work
        claude.send("UserPromptSubmit", prompt="Fix the parser in a worktree")
        claude.reply(input_tokens=100, output_tokens=10)
        claude.edit(platform / "notes.md", "plan\n")
        # The shell moves into the worktree. Looking (git status) does not open it;
        # everything from the first edit on happened there.
        claude.run("git status --short", cwd=wt)
        claude.edit(wt / "parser.py", "PARSED = True\n", cwd=wt)
        claude.run("python -m pytest -q", cwd=wt, stdout="1 passed")
        sha = _commit(wt, "Fixed the parser")
        claude.run("git commit -m 'Fixed the parser'", cwd=wt, stdout=f"[wt-branch {sha[:7]}] Fixed the parser")
        claude.status(cost=0.42, cwd=wt)
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(platform) and _ended(wt))
    finally:
        service.stop()

    (project_receipt,) = _receipts(platform)
    (worktree_receipt,) = _receipts(wt)

    # The worktree's Receipt carries the session, the task and the worktree's own git facts.
    assert worktree_receipt["capture"]["session_id"] == project_receipt["capture"]["session_id"] == SID
    assert worktree_receipt["task"] == project_receipt["task"] == "Fix the parser in a worktree"
    assert worktree_receipt["capture"]["start_source"] == "startup"
    assert worktree_receipt["capture"]["checkout"] == {"kind": "linked_worktree", "name": "plat-wt"}
    assert worktree_receipt["git_branch"] == "wt-branch"
    assert worktree_receipt["git_head_commit_hash"] == wt_start
    assert worktree_receipt["git_end_head"] == sha
    assert sha in worktree_receipt["session_commits"]["shas"]
    assert _changed(worktree_receipt) == {"parser.py"}
    assert worktree_receipt["capture"]["tool_call_count"] == 3  # edit, pytest, commit
    assert worktree_receipt["repo_identity"] == "github.com/openshard/platform"

    # The project's Receipt keeps only what happened in the project.
    assert project_receipt["capture"]["checkout"] == {"kind": "main_checkout", "name": None}
    assert project_receipt["git_branch"] == _branch(platform)
    assert project_receipt["git_end_head"] == _head(platform) != sha
    assert sha not in project_receipt["session_commits"]["shas"]
    assert _changed(project_receipt) == {"notes.md"}
    assert project_receipt["capture"]["tool_call_count"] == 2  # the edit, and git status run before the worktree opened

    # The turn's usage and cost are counted once, on the checkout that owned the turn.
    assert project_receipt["prompt_tokens"] == 100 and project_receipt["completion_tokens"] == 10
    assert project_receipt["estimated_cost"] == pytest.approx(0.42)
    assert "prompt_tokens" not in worktree_receipt
    assert worktree_receipt["capture"]["tokens_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    assert worktree_receipt["capture"]["cost_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    # Both observed the session end.
    for receipt in (project_receipt, worktree_receipt):
        assert receipt["capture"]["session_end_observed"] is True
        assert receipt["executor"] == "claude_code_hooks"


def test_usage_is_counted_once_when_turns_move_between_checkouts(repos):
    """Seen live: a session's four Receipts summed to 129% of its transcript's tokens.

    A checkout that owned a later turn read the transcript from its own
    cursor, through turns the other checkout had already counted. The
    session's first checkout now owns all of its usage and cost.
    """
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.status(cost=0.0)
        claude.send("UserPromptSubmit", prompt="Plan in the project")
        claude.reply(input_tokens=100, output_tokens=10)
        claude.edit(platform / "notes.md", "plan\n")
        claude.status(cost=0.10)
        claude.send("Stop")
        time.sleep(1.1)
        # Turn 2 happens only in the worktree, which the per-turn rule made its owner.
        claude.send("UserPromptSubmit", prompt="Work in the worktree", cwd=wt)
        claude.reply(input_tokens=1000, output_tokens=100)
        claude.edit(wt / "parser.py", "PARSED = True\n", cwd=wt)
        claude.status(cost=0.60, cwd=wt)
        claude.send("Stop", cwd=wt)
        time.sleep(1.1)
        # Turn 3 is back in the project: it must not re-read turn 2's usage.
        claude.send("UserPromptSubmit", prompt="Finish in the project")
        claude.reply(input_tokens=10, output_tokens=1)
        claude.edit(platform / "notes.md", "done\n")
        claude.status(cost=0.65)
        claude.send("Stop")
        claude.send("SessionEnd", reason="other")
        assert _wait_for(lambda: _ended(platform) and _ended(wt))
    finally:
        service.stop()

    (project_receipt,) = _receipts(platform)
    (worktree_receipt,) = _receipts(wt)
    assert project_receipt["prompt_tokens"] == 1110 and project_receipt["completion_tokens"] == 111
    assert project_receipt["estimated_cost"] == pytest.approx(0.65)
    assert "prompt_tokens" not in worktree_receipt and "estimated_cost" not in worktree_receipt
    # Seen in acceptance: the worktree Receipt still names the model that ran (transcript), just not its usage.
    assert worktree_receipt["execution_model"] == "claude-opus-5-5"
    assert worktree_receipt["capture"]["tokens_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    assert worktree_receipt["capture"]["cost_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    assert _changed(worktree_receipt) == {"parser.py"}


def test_a_worktree_of_another_repository_is_recorded_as_that_repository(repos):
    platform, core_wt = repos["platform"], repos["core_wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Change Core from the Platform session")
        claude.reply(input_tokens=50, output_tokens=5)
        claude.edit(core_wt / "core_change.py", "X = 1\n", cwd=core_wt)
        sha = _commit(core_wt, "Changed core")
        claude.run("git commit -m 'Changed core'", cwd=core_wt)
        claude.send("Stop", cwd=core_wt)
        claude.send("SessionEnd", cwd=core_wt, reason="other")
        assert _wait_for(lambda: _ended(platform) and _ended(core_wt))
    finally:
        service.stop()

    (project_receipt,) = _receipts(platform)
    (core_receipt,) = _receipts(core_wt)
    assert core_receipt["repo_identity"] == "github.com/openshard/core"
    assert project_receipt["repo_identity"] == "github.com/openshard/platform"
    assert core_receipt["git_branch"] == "core-wt-branch"
    assert sha in core_receipt["session_commits"]["shas"]
    assert _changed(core_receipt) == {"core_change.py"}
    assert _changed(project_receipt) == set()
    assert sha not in project_receipt["session_commits"]["shas"]
    assert core_receipt["task"] == project_receipt["task"] == "Change Core from the Platform session"
    # Nothing was written into Core's main checkout: the work happened in its worktree.
    assert not (repos["core"] / ".openshard" / "runs.jsonl").exists()


def test_two_worktrees_in_one_turn_share_the_task_and_count_usage_once(repos):
    platform, wt, core_wt = repos["platform"], repos["wt"], repos["core_wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        # The prompt arrives with the shell already in the worktree. A prompt is not
        # work: it is recorded in the project, which owns the session's usage.
        claude.send("UserPromptSubmit", cwd=wt, prompt="Touch both worktrees")
        claude.reply(input_tokens=300, output_tokens=30)
        claude.edit(wt / "one.py", "1\n", cwd=wt)
        claude.edit(core_wt / "two.py", "2\n", cwd=core_wt)
        claude.send("Stop", cwd=core_wt)
        claude.send("SessionEnd", cwd=core_wt, reason="other")
        assert _wait_for(lambda: _ended(wt) and _ended(core_wt))
    finally:
        service.stop()

    (wt_receipt,) = _receipts(wt)
    (core_receipt,) = _receipts(core_wt)
    assert wt_receipt["task"] == core_receipt["task"] == "Touch both worktrees"
    assert _changed(wt_receipt) == {"one.py"} and _changed(core_receipt) == {"two.py"}
    for receipt in (wt_receipt, core_receipt):
        assert "prompt_tokens" not in receipt
        assert receipt["capture"]["tokens_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    # The project saw the session start and the prompt, changed nothing, and counts the usage once.
    (project_receipt,) = _receipts(platform)
    assert _changed(project_receipt) == set()
    assert project_receipt["capture"]["prompt_count"] == 1
    assert project_receipt["prompt_tokens"] == 300


def test_a_service_restart_mid_session_keeps_recording_into_the_worktree(repos):
    platform, wt = repos["platform"], repos["wt"]
    env = {**os.environ}
    service = _Service(env)
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Survive a restart")
        claude.edit(wt / "before.py", "1\n", cwd=wt)
        assert service.server.recorder.wait_idle(20)
    finally:
        service.stop()
    # The replacement service has no memory of the session; the capability is still the project's.
    service = _Service(env, port=service.port)
    try:
        claude.port = service.port
        claude.edit(wt / "after.py", "2\n", cwd=wt)
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(wt))
    finally:
        service.stop()

    (worktree_receipt,) = _receipts(wt)
    assert _changed(worktree_receipt) == {"before.py", "after.py"}
    assert worktree_receipt["task"] == "Survive a restart"
    assert worktree_receipt["capture"]["tool_call_count"] == 2
    assert worktree_receipt["capture"]["session_end_observed"] is True


def test_a_restarted_service_keeps_the_session_usage_on_the_same_receipt(repos):
    platform, wt = repos["platform"], repos["wt"]
    env = {**os.environ}
    service = _Service(env)
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Plan in the project")
        claude.reply(input_tokens=100, output_tokens=10)
        claude.edit(platform / "notes.md", "plan" + chr(10))
        claude.send("Stop")
        assert service.server.recorder.wait_idle(20)
    finally:
        service.stop()
    time.sleep(1.1)
    # The replacement service forgot the session; the next turn starts in the worktree.
    service = _Service(env, port=service.port)
    try:
        claude.port = service.port
        claude.send("UserPromptSubmit", prompt="Continue in the worktree", cwd=wt)
        claude.reply(input_tokens=1000, output_tokens=100)
        claude.edit(wt / "parser.py", "PARSED = True" + chr(10), cwd=wt)
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(platform) and _ended(wt))
    finally:
        service.stop()
    (project_receipt,) = _receipts(platform)
    (worktree_receipt,) = _receipts(wt)
    assert project_receipt["prompt_tokens"] == 1100 and project_receipt["completion_tokens"] == 110
    assert "prompt_tokens" not in worktree_receipt
    assert worktree_receipt["capture"]["tokens_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    # The owner is kept in the service's state, never in a repository, and is gone after the session ends.
    assert not list(platform.rglob("*.usage-owner")) and not list(wt.rglob("*.usage-owner"))


def test_delayed_queue_processing_folds_the_same_receipts(repos):
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        service.server.recorder.pause_processing()
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Queue it all")
        claude.edit(platform / "p.py", "1\n")
        claude.edit(wt / "w.py", "2\n", cwd=wt)
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        # Everything is durably queued, nothing folded yet.
        assert not (wt / ".openshard" / "runs.jsonl").exists()
        assert list((wt / ".openshard" / "claude_sessions").glob("*.queue*.jsonl"))
        service.server.recorder.resume_processing()
        assert _wait_for(lambda: _ended(platform) and _ended(wt))
    finally:
        service.stop()
    (project_receipt,) = _receipts(platform)
    (worktree_receipt,) = _receipts(wt)
    assert _changed(project_receipt) == {"p.py"} and _changed(worktree_receipt) == {"w.py"}
    assert worktree_receipt["task"] == project_receipt["task"] == "Queue it all"


def test_concurrent_sessions_in_the_same_worktree_stay_separate(repos):
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        first = _Claude(platform, service.port, SID)
        second = _Claude(platform, service.port, SID2)
        first.start()
        second.start()
        first.send("UserPromptSubmit", prompt="First agent")
        second.send("UserPromptSubmit", prompt="Second agent")
        first.edit(wt / "first.py", "1\n", cwd=wt)
        second.edit(wt / "second.py", "2\n", cwd=wt)
        first.send("Stop", cwd=wt)
        second.send("Stop", cwd=wt)
        first.send("SessionEnd", cwd=wt, reason="other")
        second.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(wt, 2))
    finally:
        service.stop()
    by_session = {r["capture"]["session_id"]: r for r in _receipts(wt)}
    assert set(by_session) == {SID, SID2}
    assert by_session[SID]["task"] == "First agent" and by_session[SID2]["task"] == "Second agent"
    # Each session reports its own edit; the other's file shows up only as a git-observed, unattributed change.
    assert {f["path"] for f in by_session[SID]["files_detail"] if f.get("attribution") == "agent_reported"} == {"first.py"}
    assert {f["path"] for f in by_session[SID2]["files_detail"] if f.get("attribution") == "agent_reported"} == {"second.py"}


def test_events_outside_every_repository_stay_with_the_project(repos, tmp_path):
    platform = repos["platform"]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Run a scratch script")
        claude.run("python probe.py", cwd=scratch, stdout="ok")
        claude.send("Stop", cwd=scratch)
        claude.send("SessionEnd", cwd=scratch, reason="other")
        assert _wait_for(lambda: _ended(platform))
    finally:
        service.stop()
    (project_receipt,) = _receipts(platform)
    assert project_receipt["capture"]["tool_call_count"] == 1
    assert not (scratch / ".openshard").exists()


def test_a_notification_with_its_cwd_in_a_worktree_does_not_open_it(repos):
    """Seen live: a background-task notification arriving while the shell sat in a checkout opened an empty Receipt there."""
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Plan in the project")
        claude.edit(platform / "notes.md", "plan\n")
        claude.send("Stop")
        claude.send("UserPromptSubmit", cwd=wt, prompt="<task-notification>\n<status>completed</status>\n</task-notification>")
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(platform))
    finally:
        service.stop()
    (project_receipt,) = _receipts(platform)
    assert project_receipt["capture"]["turn_count"] == 2  # the notification's turn closed in the project
    assert not (wt / ".openshard" / "runs.jsonl").exists()


def test_git_log_in_an_unopened_worktree_stays_home(repos):
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Look at the worktree's history")
        claude.run("git log --oneline -5", cwd=wt, stdout="abc123 init")
        claude.run("git status --short", cwd=wt)
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(platform))
    finally:
        service.stop()
    (project_receipt,) = _receipts(platform)
    assert project_receipt["capture"]["tool_call_count"] == 2
    assert not (wt / ".openshard" / "runs.jsonl").exists()


def test_pytest_in_an_unopened_worktree_still_opens_it(repos):
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Run the worktree's tests")
        claude.run("python -m pytest -q", cwd=wt, stdout="1 passed")
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(platform) and _ended(wt))
    finally:
        service.stop()
    (worktree_receipt,) = _receipts(wt)
    (project_receipt,) = _receipts(platform)
    assert worktree_receipt["capture"]["tool_call_count"] == 1
    assert worktree_receipt["task"] == "Run the worktree's tests"
    assert project_receipt["capture"]["tool_call_count"] == 0


@pytest.mark.parametrize("command", [
    "git status --short", "git log --oneline -5", "git --no-pager diff HEAD~1", "git show HEAD",
    "git rev-parse HEAD", "git branch --show-current", "git remote -v", "openshard history --limit 5",
    "gh pr view 12", "gh pr checks", "ls -la", "pwd", "cat README.md", "tail -n 20 log.txt",
    "Get-Content README.md", "Get-ChildItem -Recurse",
])
def test_read_only_allowlist_accepts(command):
    from openshard.adapters.claude_capture_routing import is_read_only_command
    assert is_read_only_command(command)


@pytest.mark.parametrize("command", [
    "", "git commit -m x", "git branch feature", "git remote add o url", "git diff --output=out.patch",
    "git log | head", "git status && git commit -m x", "cat a > b", "ls; rm -rf x", "git log $(echo x)",
    "gh pr create", "gh pr merge 1", "python -m pytest", "Set-Content a b", "GIT_PAGER=cat git log",
    "openshard sync now", "git -C ../other log", "cd /tmp/wt && git log", "echo `rm x`",
])
def test_read_only_allowlist_rejects(command):
    from openshard.adapters.claude_capture_routing import is_read_only_command
    assert not is_read_only_command(command)


def test_a_command_that_changes_directory_is_attributed_where_it_ran(repos):
    """``cd <worktree> && git commit`` from the project's shell acts in the worktree: its commit belongs there."""
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", prompt="Commit in the worktree without leaving the project")
        (wt / "fix.py").write_text("FIXED = True\n", encoding="utf-8")
        sha = _commit(wt, "Fixed it in the worktree")
        posix_wt = "/" + str(wt).replace(":", "", 1).replace("\\", "/") if os.name == "nt" else str(wt)
        claude.run(f"cd {posix_wt} && git add -A && git commit -m 'Fixed it in the worktree'", cwd=platform,
                   stdout=f"[wt-branch {sha[:7]}] Fixed it in the worktree")
        claude.send("Stop")
        claude.send("SessionEnd", reason="other")
        assert _wait_for(lambda: _ended(wt) and _ended(platform))
    finally:
        service.stop()
    (worktree_receipt,) = _receipts(wt)
    (project_receipt,) = _receipts(platform)
    assert worktree_receipt["capture"]["tool_call_count"] == 1
    assert sha in worktree_receipt["session_commits"]["shas"]
    assert project_receipt["capture"]["tool_call_count"] == 0
    assert sha not in project_receipt["session_commits"]["shas"]


def test_the_worktree_receipt_is_not_written_into_the_project(repos):
    """The original defect: the worktree edit used to land in the project's Receipt with the project's git facts."""
    platform, wt = repos["platform"], repos["wt"]
    service = _Service({**os.environ})
    try:
        claude = _Claude(platform, service.port)
        claude.start()
        claude.send("UserPromptSubmit", cwd=wt, prompt="Only in the worktree")
        claude.edit(wt / "only.py", "1\n", cwd=wt)
        claude.send("Stop", cwd=wt)
        claude.send("SessionEnd", cwd=wt, reason="other")
        assert _wait_for(lambda: _ended(wt))
        time.sleep(0.2)
    finally:
        service.stop()
    (worktree_receipt,) = _receipts(wt)
    assert _changed(worktree_receipt) == {"only.py"} and worktree_receipt["git_branch"] == "wt-branch"
    for receipt in _receipts(platform):
        assert "only.py" not in {f["path"] for f in receipt["files_detail"]}
        assert receipt["capture"]["tool_call_count"] == 0
