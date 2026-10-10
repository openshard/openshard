"""Multi-repository Claude Code cloud sessions: the whole capture flow.

A cloud session with several repositories starts in their parent directory, so
only workspace-level hooks fire. These tests install the hooks with the real
installer, send exactly what Claude Code sends (the installed URL and headers)
to a real capture service, and deliver the folded Receipts through the
connected-capture collector. They check that every Receipt lands in the
checkout the work touched, keeps the task, and that a turn's usage is counted
once.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openshard.adapters import claude_hooks as ch
from openshard.adapters import claude_workspace as ws
from openshard.connected import config as cconfig
from openshard.history.store import load_history
from openshard.remote import collector
from openshard.remote import config as rconfig
from openshard.sync import config as sconfig
from tests.capture_fixtures import _git, _make_repo, _Service, _wait_for
from tests.test_connected_capture import ENDPOINT, ORG, FakeConnectedPlatform

SID = "5a5a5a5a-6b6b-4c7c-8d8d-9e9e9e9e9e9e"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "user"
    root.mkdir()
    for name in ("core", "platform"):
        repo = _make_repo(root / name)
        _git(repo, "remote", "add", "origin", f"https://github.com/openshard/{name}.git")
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENSHARD_REMOTE_NO_SPAWN", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    for name in (rconfig.URL_ENV, rconfig.TOKEN_ENV, sconfig.ENDPOINT_ENV, sconfig.ORG_ENV, sconfig.API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(cconfig.ENDPOINT_ENV, ENDPOINT)
    monkeypatch.setenv(cconfig.ORG_ENV, ORG)
    monkeypatch.setenv(cconfig.TOKEN_ENV, cconfig.PROXY_INJECTED_TOKEN)
    monkeypatch.setenv(cconfig.SURFACE_ENV, "claude-code-web")
    return root


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Claude:
    """Sends hook events the way Claude Code does, from the installed workspace settings."""

    def __init__(self, workspace: Path) -> None:
        settings = json.loads((workspace / ".claude" / "settings.local.json").read_text())
        hook = settings["hooks"]["Stop"][0]["hooks"][0]
        self.url = hook["url"]
        self.headers = {k: v.replace("$CLAUDE_PROJECT_DIR", str(workspace)) for k, v in hook["headers"].items()
                        if "$" not in v or "$CLAUDE_PROJECT_DIR" in v}
        self.workspace = workspace
        self.transcript = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-home-user" / f"{SID}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()
        self.messages = 0

    def send(self, event: str, *, cwd: Path | None = None, **fields) -> None:
        body = {"session_id": SID, "transcript_path": str(self.transcript), "cwd": str(cwd or self.workspace),
                "hook_event_name": event, "permission_mode": "acceptEdits", **fields}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **self.headers})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200

    def reply(self, *, input_tokens: int, output_tokens: int) -> None:
        """The model's answer as Claude Code appends it to the session transcript."""
        self.messages += 1
        line = {"type": "assistant", "sessionId": SID, "timestamp": _now(), "message": {
            "id": f"msg_{self.messages}", "model": "claude-opus-5-5", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }}
        with self.transcript.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")


def _receipts(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return load_history(path, coerce=False) if path.exists() else []


def _sealed(repo: Path, count: int) -> bool:
    receipts = _receipts(repo)
    return len(receipts) == count and all(r["capture"].get("task_status") == "turn_completed" for r in receipts)


def _edit(claude: _Claude, path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    claude.send("PostToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "RAW"},
                tool_response={"filePath": str(path)})


def test_install_writes_workspace_and_checkout_hooks(workspace):
    outcome = ws.install_workspace(workspace, port=47999)
    assert outcome["status"] == "installed"
    assert set(outcome["checkouts"]) == {"core", "platform"}
    assert ws.is_workspace(workspace.resolve())
    for directory in (workspace, workspace / "core", workspace / "platform"):
        settings = json.loads((directory / ".claude" / "settings.local.json").read_text())
        assert "SessionStart" in settings["hooks"] and "Stop" in settings["hooks"]
    assert not (workspace / "core" / ".openshard" / "workspace.json").exists()


def test_install_refuses_a_directory_inside_a_repository(workspace):
    assert ws.install_workspace(workspace / "core", port=47999)["status"] == "error"


def test_usage_reason_constants_agree():
    assert ch.USAGE_ELSEWHERE_REASON == ws.USAGE_ELSEWHERE_REASON


def test_multi_repository_cloud_session_produces_receipts_in_the_touched_checkouts(workspace):
    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _Claude(workspace)
        core, platform = (workspace / "core").resolve(), (workspace / "platform").resolve()

        # Turn 1 touches both checkouts: core first, so core owns its usage.
        claude.send("SessionStart", source="startup")
        claude.send("UserPromptSubmit", prompt="Change both repositories")
        claude.reply(input_tokens=100, output_tokens=10)
        _edit(claude, core / "a.py", "A = 1\n")
        claude.reply(input_tokens=200, output_tokens=20)
        claude.send("PostToolUse", cwd=platform, tool_name="Bash", tool_input={"command": "python -m pytest -q"},
                    tool_response={"stdout": "1 passed"})
        _edit(claude, platform / "b.py", "B = 1\n")
        claude.send("Stop")
        # The service folds asynchronously; a real next turn starts well after this one sealed.
        assert _wait_for(lambda: _sealed(core, 1) and _sealed(platform, 1))
        # A read-only question touches nothing: it is not attributed to a repository.
        # (Segment starts have one-second precision; real turns are further apart.)
        time.sleep(1.1)
        claude.send("UserPromptSubmit", prompt="What changed?")
        claude.reply(input_tokens=5, output_tokens=5)
        claude.send("Stop")
        # Turn 3 touches only platform, which now owns that turn's usage.
        time.sleep(1.1)
        claude.send("UserPromptSubmit", prompt="Follow up in platform")
        claude.reply(input_tokens=1000, output_tokens=100)
        _edit(claude, platform / "c.py", "C = 1\n")
        claude.send("Stop")

        assert _wait_for(lambda: _sealed(core, 1) and _sealed(platform, 2))
    finally:
        service.stop()

    assert not (workspace / ".openshard" / "runs.jsonl").exists()
    (core_turn,) = _receipts(core)
    platform_turn, platform_followup = _receipts(platform)

    # The task and the touched files land in the right checkout.
    assert core_turn["task"] == platform_turn["task"] == "Change both repositories"
    assert platform_followup["task"] == "Follow up in platform"
    def changed(receipt: dict) -> set[str]:
        return {f["path"] for f in receipt["files_detail"] if f.get("attribution") != "pre_existing"}

    assert changed(core_turn) == {"a.py"}
    assert changed(platform_turn) == {"b.py"}
    assert changed(platform_followup) == {"c.py"}

    # Turn 1's usage is counted once, on core; platform's copy records why it has none.
    assert core_turn["prompt_tokens"] == 300 and core_turn["completion_tokens"] == 30
    assert "prompt_tokens" not in platform_turn
    assert platform_turn["capture"]["tokens_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    assert platform_turn["capture"]["cost_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON
    # Turn 3 counts only its own message, not turn 1's or the read-only turn's.
    assert platform_followup["prompt_tokens"] == 1000 and platform_followup["completion_tokens"] == 100

    # Every Receipt is an observed Claude Code run that admits the missing SessionEnd.
    for receipt in (core_turn, platform_turn, platform_followup):
        assert receipt["executor"] == "claude_code_hooks"
        assert receipt["capture"]["session_end_observed"] is False

    # The connected collector delivers each one under the same hosted session.
    # The service already tried the (unreachable) test endpoint and backed off; retry after it.
    fake = FakeConnectedPlatform()
    report = collector.flush(client=fake, now=time.time() + 3600)
    assert report.receipts["created"] == 3
    identities = sorted(r["receipt"]["repo_identity"] for r in fake.receipts)
    assert identities == ["github.com/openshard/core", "github.com/openshard/platform", "github.com/openshard/platform"]


def test_workspace_routing_needs_the_cloud_surface(workspace, monkeypatch):
    monkeypatch.delenv(cconfig.SURFACE_ENV)
    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _Claude(workspace)
        claude.send("SessionStart", source="startup")
        claude.send("UserPromptSubmit", prompt="local workspace")
        _edit(claude, workspace / "core" / "a.py", "A = 1\n")
        claude.send("Stop")
        claude.send("SessionEnd", reason="other")
        time.sleep(0.5)
    finally:
        service.stop()
    # Without hosted turn sealing nothing is routed into the checkouts.
    assert _receipts(workspace / "core") == []


def test_status_line_usage_goes_only_to_the_turn_owner(workspace):
    from openshard.adapters import claude_capture_client as client

    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _Claude(workspace)
        core = (workspace / "core").resolve()
        status_url = claude.url.rsplit("/hooks/", 1)[0] + client.STATUS_PATH

        def ping(cost: float) -> None:
            body = {"session_id": SID, "cwd": str(workspace), "model": {"id": "claude-opus-5-5"},
                    "cost": {"total_cost_usd": cost}}
            request = urllib.request.Request(status_url, data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json", **claude.headers})
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200

        claude.send("SessionStart", source="startup")
        claude.send("UserPromptSubmit", prompt="Change core")
        ping(0.10)  # before any checkout is touched: not attributed anywhere
        _edit(claude, core / "a.py", "A = 1\n")
        ping(0.10)
        ping(0.35)
        claude.send("Stop")
        assert _wait_for(lambda: _sealed(core, 1))
    finally:
        service.stop()
    (receipt,) = _receipts(core)
    assert receipt["estimated_cost"] == pytest.approx(0.25)
    assert not (workspace / ".openshard" / "claude_sessions").exists()
    assert _receipts(workspace / "platform") == []
