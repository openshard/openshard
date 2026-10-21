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
import subprocess
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


def test_install_with_learn_adds_the_hosted_prompt_hook_to_workspace_and_checkouts(workspace):
    from click.testing import CliRunner

    from openshard.cli.main import cli

    result = CliRunner().invoke(
        cli, ["capture", "install", "claude", "--defer-service", "--learn", "--workspace", str(workspace), "--json"], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "installed"
    assert payload["learning"]["hook"] == "openshard learn hook claude --hosted"
    assert set(payload["learning"]["installed"].values()) == {"installed"}
    for directory in (workspace, workspace / "core", workspace / "platform"):
        settings = json.loads((directory / ".claude" / "settings.local.json").read_text())
        commands = [h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"] if h.get("type") == "command"]
        assert commands.count("openshard learn hook claude --hosted") == 1
        assert "openshard hooks claude-watchdog" in commands  # capture's own prompt hook is kept beside it
        assert "Stop" in settings["hooks"]


def test_install_refuses_a_directory_inside_a_repository(workspace):
    assert ws.install_workspace(workspace / "core", port=47999)["status"] == "error"


def test_install_accepts_a_workspace_under_a_parent_repository(tmp_path, monkeypatch):
    # A home directory kept as a dotfiles checkout must not block a workspace beneath it.
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "openshard-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    home = _make_repo(tmp_path / "home")
    work = home / "work"
    work.mkdir()
    for name in ("core", "platform"):
        _make_repo(work / name)
    outcome = ws.install_workspace(work, port=47999)
    assert outcome["status"] == "installed"
    assert set(outcome["checkouts"]) == {"core", "platform"}
    assert ws.is_workspace(work.resolve())


def test_install_refuses_a_directory_that_is_itself_a_checkout(workspace):
    checkout = workspace / "core"
    _make_repo(checkout / "inner")  # a child checkout, so only the checkout rule can refuse
    outcome = ws.install_workspace(checkout, port=47999)
    assert outcome["status"] == "error"
    assert outcome["message"] == "The workspace is itself inside a git repository; install per repository instead."
    ws.write_marker(checkout, [])
    assert not ws.is_workspace(checkout.resolve())


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


def test_a_turn_prices_its_usage_where_it_committed_not_where_it_first_ran_a_command(workspace):
    """A turn that ran setup in platform, then committed in core, is priced on core's Receipt."""
    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _Claude(workspace)
        core, platform = (workspace / "core").resolve(), (workspace / "platform").resolve()
        claude.send("SessionStart", source="startup")
        claude.send("UserPromptSubmit", prompt="Fix core")
        claude.reply(input_tokens=100, output_tokens=10)
        claude.send("PostToolUse", cwd=platform, tool_name="Bash", tool_input={"command": "openshard setup -y"},
                    tool_response={"stdout": "ok"})
        claude.reply(input_tokens=200, output_tokens=20)
        claude.send("PostToolUse", cwd=core, tool_name="Bash", tool_input={"command": "git commit -q -m fix"},
                    tool_response={"stdout": ""})
        claude.send("Stop")
        assert _wait_for(lambda: _sealed(core, 1) and _sealed(platform, 1))
    finally:
        service.stop()
    (core_turn,) = _receipts(core)
    (platform_turn,) = _receipts(platform)
    assert core_turn["prompt_tokens"] == 300 and core_turn["completion_tokens"] == 30
    assert core_turn["estimated_cost"] is not None
    assert "tokens_not_recorded_reason" not in core_turn["capture"]
    assert "prompt_tokens" not in platform_turn and platform_turn.get("estimated_cost") is None
    assert platform_turn["capture"]["cost_not_recorded_reason"] == ws.USAGE_ELSEWHERE_REASON


def test_a_local_workspace_session_records_into_the_touched_checkout(workspace, monkeypatch):
    """Without the cloud surface the parent is an ordinary project; its edits still land where they happened."""
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
        assert _wait_for(lambda: len(_receipts(workspace / "core")) == 1)
    finally:
        service.stop()
    (core_receipt,) = _receipts(workspace / "core")
    assert core_receipt["task"] == "local workspace"
    assert {f["path"] for f in core_receipt["files_detail"] if f.get("attribution") != "pre_existing"} == {"a.py"}
    assert core_receipt["capture"]["checkout"] == {"kind": "main_checkout", "name": None}
    assert _receipts(workspace / "platform") == []


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


def test_routing_resolves_path_aliases_like_the_checkouts(workspace, tmp_path):
    from types import SimpleNamespace

    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    core = (workspace / "core").resolve()
    via_alias = SimpleNamespace(cwd=str(alias), file_path=str(alias / "core" / "a.py"), file_paths=[])
    assert ws.checkout_for(workspace.resolve(), via_alias) == core
    relative = SimpleNamespace(cwd=str(alias / "core"), file_path="a.py", file_paths=[])
    assert ws.checkout_for(workspace.resolve(), relative) == core
    outside = SimpleNamespace(cwd=str(alias), file_path=str(tmp_path / "elsewhere.py"), file_paths=[])
    assert ws.checkout_for(workspace.resolve(), outside) is None

def test_install_keeps_capture_state_out_of_every_checkout(workspace):
    """An agent's `git add .` in a checkout must never commit Receipts, session
    queues or locks: each checkout git-ignores `.openshard/` (locally, in
    `.git/info/exclude`), with the shareable `config.yml` re-included."""
    assert ws.install_workspace(workspace, port=47999)["status"] == "installed"
    def ignored(repo: Path, rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode == 0

    for name in ("core", "platform"):
        repo = workspace / name
        assert ignored(repo, ".openshard/runs.jsonl")
        assert ignored(repo, ".openshard/claude_sessions/x.queue.1.jsonl")
        assert not ignored(repo, ".openshard/config.yml")
        assert not (repo / ".gitignore").exists()  # local rule only; nothing of the user's is changed


CLOUD_SESSION = "session_01Uze9QtYmeCTuxJ7dKTKTAP"


class _CloudClaude(_Claude):
    """Claude Code on the web as it really behaves in a workspace session.

    Command hooks (SessionStart) run with ``CLAUDE_PROJECT_DIR`` and the
    launch environment, so they name the workspace and the cloud session. HTTP
    hooks do not: ``$CLAUDE_PROJECT_DIR`` interpolates to an empty string
    there, and they carry no agent environment either.
    """

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.headers = {k: ("" if "$" in v or v == str(workspace) else v) for k, v in self.headers.items()}
        self.headers = {k: v for k, v in self.headers.items() if v}
        self.transcript = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / ws._project_folder_name(workspace) / f"{SID}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()

    def command_start(self, source: str = "startup") -> None:
        """SessionStart, sent as ``openshard hooks claude`` sends it."""
        body = {"session_id": SID, "transcript_path": str(self.transcript), "cwd": str(self.workspace),
                "hook_event_name": "SessionStart", "source": source}
        headers = {"Content-Type": "application/json", **self.headers,
                   "X-OpenShard-Project-Dir": str(self.workspace),
                   "X-OpenShard-Agent-Env": f"provider=anthropic;surface=sdk-cli;cloud_session={CLOUD_SESSION}"}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_http_hooks_without_a_project_dir_make_one_receipt_for_the_task(workspace):
    """The live failure: the workspace parent and the checkout each held a Receipt of one run.

    The checkout had the repository and commit but no tokens; the parent had the
    model and tokens but no repository. A cloud task is one Receipt, in the
    checkout, carrying the commit, the files, the model and the usage once.
    """
    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _CloudClaude(workspace)
        core = (workspace / "core").resolve()

        claude.command_start()
        claude.send("UserPromptSubmit", prompt="Improve the docs")
        claude.reply(input_tokens=100, output_tokens=10)
        _edit(claude, core / "docs.md", "# Docs\n")
        head = _commit(core, "docs.md")
        claude.send("PostToolUse", cwd=core, tool_name="Bash", tool_input={"command": "git commit -am docs"},
                    tool_response={"stdout": "[main] docs"})
        claude.reply(input_tokens=200, output_tokens=20)
        claude.send("Stop")  # an HTTP hook: no project dir, run from the workspace parent
        assert _wait_for(lambda: _sealed(core, 1))
    finally:
        service.stop()

    # Nothing is recorded for the workspace parent: no Receipt, no session buffer.
    assert not (workspace / ".openshard" / "runs.jsonl").exists()
    assert not (workspace / ".openshard" / "claude_sessions").exists()
    assert _receipts(workspace / "platform") == []

    (receipt,) = _receipts(core)
    assert receipt["task"] == "Improve the docs"
    assert receipt["capture"]["checkout"] == {"kind": "main_checkout", "name": None}
    assert {f["path"] for f in receipt["files_detail"] if f.get("attribution") != "pre_existing"} >= {"docs.md"}
    # Repository, commit, model and usage are on the same Receipt, counted once.
    assert receipt["repo_identity"] == "github.com/openshard/core"
    assert receipt["git_end_head"] == head and head in receipt["session_commits"]["shas"]
    assert receipt["execution_model"] == "claude-opus-5-5"
    assert receipt["prompt_tokens"] == 300 and receipt["completion_tokens"] == 30
    assert "tokens_not_recorded_reason" not in receipt["capture"]
    # The command hook's launch environment survived the replay of the held start.
    assert receipt["capture"]["surface"] == "sdk-cli"
    links = {(link["namespace"], link["id"]) for link in receipt["correlation"]["external_ids"]}
    assert ("claude.cloud_session", CLOUD_SESSION) in links
    # The missing SessionEnd is admitted, not papered over.
    assert receipt["capture"]["session_end_observed"] is False
    assert receipt["capture"]["completeness"]["status"] == "incomplete"
    assert {r["kind"] for r in receipt["capture"]["completeness"]["reasons"]} == {"session_end_not_observed"}


def test_a_restarted_service_still_routes_http_hooks_to_the_workspace(workspace):
    """Only the transcript's project folder names the workspace once the service forgot the session."""
    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _CloudClaude(workspace)
        core = (workspace / "core").resolve()
        claude.send("UserPromptSubmit", prompt="Resume after a restart")  # no command start was ever seen
        claude.reply(input_tokens=7, output_tokens=3)
        _edit(claude, core / "a.py", "A = 1\n")
        claude.send("Stop")
        assert _wait_for(lambda: _sealed(core, 1))
    finally:
        service.stop()
    assert not (workspace / ".openshard" / "runs.jsonl").exists()
    (receipt,) = _receipts(core)
    assert receipt["task"] == "Resume after a restart"
    assert receipt["prompt_tokens"] == 7


def test_workspace_from_transcript_only_names_an_opted_in_workspace(workspace, tmp_path):
    ws.install_workspace(workspace, port=47999)
    folder = tmp_path / "claude" / "projects" / ws._project_folder_name(workspace.resolve())
    transcript = str(folder / "s.jsonl")
    assert ws.workspace_from_transcript(transcript, str(workspace)) == workspace.resolve()
    # From inside a checkout: the folder still names the workspace the session started in.
    assert ws.workspace_from_transcript(transcript, str(workspace / "core")) == workspace.resolve()
    # A transcript of another project, or none at all, names nothing.
    other = str(tmp_path / "claude" / "projects" / "-somewhere-else" / "s.jsonl")
    assert ws.workspace_from_transcript(other, str(workspace)) is None
    assert ws.workspace_from_transcript(None, str(workspace)) is None
    assert ws.workspace_from_transcript(transcript, None) is None
    # A folder that matches a directory which never opted in is not a workspace.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ws.workspace_from_transcript(str(folder.parent / ws._project_folder_name(plain) / "s.jsonl"), str(plain)) is None
