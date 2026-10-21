"""A check never inherits the live agent session, workspace or Platform connection.

Openshard's automatic re-check is started by its capture service, which Claude
Code launched inside the agent's workspace (``CLAUDE_PROJECT_DIR``) and session.
Seen live: the re-check ran Openshard's own test suite with that environment,
and its hook tests recorded fake Codex, Cursor and Antigravity Receipts into the
real workspace. A check is someone else's code; it gets none of that.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from openshard.verification import post_session as ps

LIVE = {
    "CLAUDE_PROJECT_DIR": "/home/user",
    "CLAUDE_CODE_SESSION_ID": "2f5cbe60-3d40-5c3e-b2c9-dcb6038c28ad",
    "CLAUDE_CODE_REMOTE_SESSION_ID": "session_live",
    "OPENSHARD_TASK_ID": "task_live",
    "OPENSHARD_CAPTURE_PORT": "47811",
    "OPENSHARD_CONNECTED_ENDPOINT": "https://api.openshard.dev",
    "OPENSHARD_CONNECTED_TOKEN": "proxy-injected",
    "OPENSHARD_PLATFORM_API_KEY": "osk_live",
    "OPENSHARD_REMOTE_TOKEN": "live",
    "OPENSHARD_GATE_API_KEY": "live",
}


def test_check_environment_drops_the_live_session_and_platform_connection(monkeypatch):
    for name, value in LIVE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PROJECT_SETTING", "kept")
    monkeypatch.delenv("OPENSHARD_NO_AUTO_VERIFY", raising=False)
    env = ps.check_environment({"PYTHONPATH": "/copy"})
    assert not set(LIVE) & set(env)
    assert env["PROJECT_SETTING"] == "kept"
    assert env["PYTHONPATH"] == "/copy"
    assert env["OPENSHARD_NO_AUTO_VERIFY"] == "1"  # a check never starts another re-check


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                       cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return path


# What the leaking tests did: run an Openshard hook with the inherited environment.
HOOK_SCRIPT = """
import os
from openshard.adapters.claude_hooks import handle_claude_hook
handle_claude_hook({"session_id": "0b6c1f7e-2a4d-4c8e-9f10-a1b2c3d4e5f6", "hook_event_name": "UserPromptSubmit",
                    "prompt": "fake", "permission_mode": "default"}, env=dict(os.environ))
"""


def test_a_check_that_runs_openshard_hooks_records_nothing_into_the_live_workspace(tmp_path, monkeypatch):
    workspace = _git_repo(tmp_path / "live-workspace")
    repo = _git_repo(tmp_path / "checked-repo")
    for name, value in LIVE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(workspace))
    check = ps.PlannedCheck(name="hook", argv=[sys.executable, "-c", HOOK_SCRIPT], kind="test",
                            origin="contract", safety="safe", reason="")
    (result,) = ps.run_checks([check], repo, approve=True, stream=False)
    assert result.status == ps.CHECK_PASSED, result.note
    assert not (workspace / ".openshard").exists()
