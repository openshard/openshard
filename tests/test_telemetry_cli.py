"""Telemetry (0.4.2) as wired into the product: CLI group, setup/doctor
notice and consent, command instrumentation, hook-fold and MCP emission,
and -- above all -- that a broken telemetry path never breaks anything.

``tests/conftest.py`` turns telemetry off and forbids background flushing
for the whole suite; these tests switch it back to ``on`` for themselves
and inject a ``RecordingTransport`` so nothing ever leaves the machine.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.cli.main import cli
from openshard.telemetry import client, queue, state, transport

SECRET = "sk-ant-api03-SECRETSECRET12345678901234567890"
SID = "0f1e2d3c-4b5a-4697-8877-665544332211"
FORBIDDEN = ("word_utils", "openshard/openshard", SECRET, "Traceback", "transcript", "someone", "@")


@pytest.fixture
def telemetry_on(monkeypatch):
    monkeypatch.setenv("OPENSHARD_TELEMETRY", "on")  # conftest set it off; "on" is simply "not off"
    rt = transport.RecordingTransport()
    client.configure(transport=rt, repo_config={})
    yield rt
    client.configure(transport=None, repo_config=None)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _drain(rt: transport.RecordingTransport) -> list[dict]:
    client.flush(transport=rt)
    return rt.events


def _types(events: list[dict]) -> list[str]:
    return [e["event_type"] for e in events]


def _assert_clean(events: list[dict]) -> None:
    blob = json.dumps(events)
    for needle in FORBIDDEN:
        assert needle not in blob, needle


# ---------------------------------------------------------------------------
# openshard telemetry status | on | off | reset | sample
# ---------------------------------------------------------------------------


class TestTelemetryCli:
    def test_status_on_off_reset_sample(self, telemetry_on):
        runner = CliRunner()
        out = runner.invoke(cli, ["telemetry", "status"])
        assert out.exit_code == 0 and "Help improve OpenShard: off (not yet asked" in out.output
        assert "(none yet)" in out.output  # no installation id is minted just by asking

        out = runner.invoke(cli, ["telemetry", "on"])
        assert out.exit_code == 0 and "Help improve OpenShard: on (on)" in out.output
        st = state.load_state()
        assert st is not None and st.improve == "on" and st.improve_source == "cli"
        # Turning on is itself an event, and the command was counted.
        sample = runner.invoke(cli, ["telemetry", "sample", "--json"])
        assert sample.exit_code == 0
        queued = json.loads(sample.output)
        assert {e["event_type"] for e in queued} >= {"telemetry.consent_changed", "command.invoked"}
        consent = next(e for e in queued if e["event_type"] == "telemetry.consent_changed")
        assert consent["properties"] == {"improve": "on", "source": "cli"}
        assert all(e["installation_id"] == st.installation_id for e in queued)
        _assert_clean(queued)

        doc = json.loads(runner.invoke(cli, ["telemetry", "status", "--json"]).output)
        assert doc["enabled"] is True and doc["queued"] >= 2 and doc["installation_id"] == st.installation_id
        assert doc["endpoint"].startswith("https://")

        before = st.installation_id
        out = runner.invoke(cli, ["telemetry", "reset"])
        assert out.exit_code == 0 and "New installation id" in out.output
        assert state.load_state().installation_id != before and state.load_state().improve == "on"

        out = runner.invoke(cli, ["telemetry", "off"])
        assert out.exit_code == 0 and "Help improve OpenShard: off" in out.output
        assert state.load_state().improve == "off"
        assert queue.size() == 0  # queued events are discarded, never sent later
        assert runner.invoke(cli, ["telemetry", "sample"]).output.strip() == "No events queued."
        assert client.emit("install.seen", first_run=True) is False

    def test_env_kill_switch_wins_over_consent(self, telemetry_on, monkeypatch):
        state.set_consent("on", source="cli")
        monkeypatch.setenv("OPENSHARD_TELEMETRY", "off")
        out = CliRunner().invoke(cli, ["telemetry", "status"])
        assert "off (disabled by OPENSHARD_TELEMETRY)" in out.output


# ---------------------------------------------------------------------------
# setup / doctor: the notice, and consent only when a person sees it
# ---------------------------------------------------------------------------


class TestSetupNotice:
    def test_human_setup_shows_notice_and_turns_undecided_consent_on(self, telemetry_on, repo):
        assert state.load_state() is None
        with patch("shutil.which", return_value=None):
            out = CliRunner().invoke(cli, ["setup", "--yes", "--repo-path", str(repo)])
        assert "Improve OpenShard: on" in out.output and "privacy-safe" in out.output
        assert "anonymous" not in out.output.lower()
        assert "Never code, prompts" in out.output and "openshard telemetry off" in out.output
        assert "Richer development data: off" in out.output
        st = state.load_state()
        assert st is not None and st.improve == "on" and st.improve_source == "setup" and st.richer == "off"
        events = _drain(telemetry_on)
        assert "setup.completed" in _types(events) and "telemetry.consent_changed" in _types(events)
        setup_ev = next(e for e in events if e["event_type"] == "setup.completed")
        assert setup_ev["properties"]["agents"] == [] and setup_ev["properties"]["result"] == "error"
        invoked = next(e for e in events if e["event_type"] == "command.invoked")
        assert invoked["properties"]["command"] == "setup" and invoked["properties"]["result"] == "error"
        _assert_clean(events)

    def test_agent_setup_turns_basic_telemetry_on_and_returns_the_human_notice(self, telemetry_on, repo):
        """``setup --json`` is agent-driven setup: same default as a person, plus the notice to surface."""
        assert state.load_state() is None
        with patch("shutil.which", return_value=None):
            out = CliRunner().invoke(cli, ["setup", "--yes", "--json", "--repo-path", str(repo)])
        data = json.loads(out.output)
        tel = data["telemetry"]
        assert tel["enabled"] is True and tel["consent"] == "on" and tel["consent_source"] == "setup"
        assert tel["privacy_notice"] == state.PRIVACY_NOTICE
        assert "privacy-safe" in tel["privacy_notice"] and "anonymous" not in tel["privacy_notice"].lower()
        assert "openshard telemetry off" in tel["privacy_notice"]
        assert "Richer development data remains off" in tel["privacy_notice"]
        assert "owner" in tel["agent_instruction"] and "privacy_notice" in tel["agent_instruction"]
        st = state.load_state()
        assert st is not None and st.improve == "on" and st.richer == "off"
        events = _drain(telemetry_on)
        assert "setup.completed" in _types(events) and "telemetry.consent_changed" in _types(events)
        _assert_clean(events)

    def test_agent_status_snapshot_is_read_only_but_carries_the_notice(self, telemetry_on, repo):
        """``setup --agent`` makes no changes, so it never decides; the notice still travels with it."""
        with patch("shutil.which", return_value=None):
            out = CliRunner().invoke(cli, ["setup", "--agent", "--repo-path", str(repo)])
        tel = json.loads(out.output)["telemetry"]
        assert tel["consent"] == "unset" and tel["enabled"] is False
        assert tel["privacy_notice"] == state.PRIVACY_NOTICE and tel["agent_instruction"]
        assert state.load_state() is None or state.load_state().improve == "unset"
        assert _drain(telemetry_on) == []  # nothing is emitted while consent is unset

    @pytest.mark.parametrize("var", ["CI", "GITHUB_ACTIONS", "GITLAB_CI"])
    def test_ci_agent_setup_stays_off_and_records_no_decision(self, telemetry_on, repo, monkeypatch, var):
        monkeypatch.setenv(var, "true")
        with patch("shutil.which", return_value=None):
            out = CliRunner().invoke(cli, ["setup", "--yes", "--json", "--repo-path", str(repo)])
        tel = json.loads(out.output)["telemetry"]
        assert tel["enabled"] is False and tel["reason"] == "disabled in CI" and tel["consent"] == "unset"
        assert tel["privacy_notice"]  # the notice is still returned; it just is not in effect here
        assert state.load_state() is None or state.load_state().improve == "unset"
        assert _drain(telemetry_on) == []

    def test_do_not_track_keeps_setup_off(self, telemetry_on, repo, monkeypatch):
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        runner = CliRunner()
        with patch("shutil.which", return_value=None):
            human = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(repo)])
            data = json.loads(runner.invoke(cli, ["setup", "--yes", "--json", "--repo-path", str(repo)]).output)
        assert "Improve OpenShard: off (disabled by DO_NOT_TRACK)" in human.output
        assert data["telemetry"]["enabled"] is False and data["telemetry"]["reason"] == "disabled by DO_NOT_TRACK"
        assert state.load_state() is None or state.load_state().improve == "unset"
        assert _drain(telemetry_on) == []

    def test_openshard_telemetry_off_env_keeps_setup_off(self, telemetry_on, repo, monkeypatch):
        monkeypatch.setenv("OPENSHARD_TELEMETRY", "off")
        runner = CliRunner()
        with patch("shutil.which", return_value=None):
            human = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(repo)])
            data = json.loads(runner.invoke(cli, ["setup", "--yes", "--json", "--repo-path", str(repo)]).output)
        assert "Improve OpenShard: off (disabled by OPENSHARD_TELEMETRY)" in human.output
        assert data["telemetry"]["enabled"] is False
        assert data["telemetry"]["reason"] == "disabled by OPENSHARD_TELEMETRY"
        assert state.load_state() is None or state.load_state().improve == "unset"
        assert _drain(telemetry_on) == []

    def test_richer_development_data_stays_off_after_setup(self, telemetry_on, repo):
        runner = CliRunner()
        with patch("shutil.which", return_value=None):
            runner.invoke(cli, ["setup", "--yes", "--repo-path", str(repo)])
            runner.invoke(cli, ["setup", "--yes", "--json", "--repo-path", str(repo)])
        st = state.load_state()
        assert st.improve == "on" and st.richer == "off" and st.to_dict()["richer"] == "off"
        # The richer layer's event names are rejected even with basic telemetry on.
        from openshard.telemetry.schema import RESERVED_EVENT_TYPES

        for name in RESERVED_EVENT_TYPES:
            assert client.emit(name, anything=1) is False
        assert "richer dev data: off" in runner.invoke(cli, ["telemetry", "status"]).output
        assert all(e["consent_level"] == "improve" for e in _drain(telemetry_on))

    def test_setup_shows_off_when_decided_off(self, telemetry_on, repo):
        state.set_consent("off", source="cli")
        with patch("shutil.which", return_value=None):
            out = CliRunner().invoke(cli, ["setup", "--yes", "--repo-path", str(repo)])
        assert "Improve OpenShard: off (off" in out.output
        assert state.load_state().improve == "off"  # the notice never overrides a decision

    def test_doctor_reports_telemetry(self, telemetry_on, repo):
        runner = CliRunner()
        with patch("shutil.which", return_value=None):
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
            data = json.loads(runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)]).output)
        assert "telemetry:    off (not yet asked" in human.output
        assert data["telemetry"]["consent"] == "unset"
        state.set_consent("on", source="cli")
        with patch("shutil.which", return_value=None):
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert "telemetry:    on (on)" in human.output


# ---------------------------------------------------------------------------
# Commands are counted, never changed
# ---------------------------------------------------------------------------


class TestCommandInstrumentation:
    def test_last_history_context_stats(self, telemetry_on, tmp_path, monkeypatch):
        state.set_consent("on", source="cli")
        telemetry_on.batches.clear()
        queue.clear()
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        assert runner.invoke(cli, ["last"]).exit_code == 0
        assert runner.invoke(cli, ["history"]).exit_code == 0
        assert runner.invoke(cli, ["context", "add", "caching"]).exit_code == 0
        assert runner.invoke(cli, ["stats"]).exit_code == 0
        events = _drain(telemetry_on)
        invoked = [e["properties"] for e in events if e["event_type"] == "command.invoked"]
        assert [p["command"] for p in invoked] == ["last", "history", "context", "stats"]
        assert all(p["result"] == "ok" and p["error_category"] is None for p in invoked)
        queried = [e["properties"] for e in events if e["event_type"] == "history.queried"]
        assert [(q["command"], q["results"]) for q in queried] == [("history", 0), ("context", 0)]
        assert "caching" not in json.dumps(events)  # the task text is never sent
        _assert_clean(events)

    def test_error_is_a_category_only(self, telemetry_on, tmp_path, monkeypatch):
        state.set_consent("on", source="cli")
        telemetry_on.batches.clear()
        queue.clear()
        monkeypatch.chdir(tmp_path)
        with patch("openshard.cli.main._locate_history", side_effect=PermissionError("/home/someone/.openshard")):
            out = CliRunner().invoke(cli, ["history"])
        assert out.exit_code != 0
        events = _drain(telemetry_on)
        props = next(e["properties"] for e in events if e["event_type"] == "command.invoked")
        assert props["command"] == "history" and props["result"] == "error" and props["error_category"] == "permission"
        _assert_clean(events)


# ---------------------------------------------------------------------------
# Hook folds and MCP tools emit; a broken telemetry path never breaks them
# ---------------------------------------------------------------------------


def _hook(repo: Path, event: str, **fields) -> dict:
    return {"session_id": SID, "cwd": str(repo), "hook_event_name": event,
            "transcript_path": "/home/someone/.claude/transcript.jsonl", **fields}


class TestEmissionFromCapture:
    def test_hook_session_emits_created_then_completed(self, telemetry_on, repo):
        state.set_consent("on", source="cli")
        telemetry_on.batches.clear()
        queue.clear()
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        nested = repo / "evals" / "basic" / "bug_fix" / "fixtures"
        nested.mkdir(parents=True)
        handle_claude_hook(_hook(repo, "SessionStart", source="startup"), env=env)
        handle_claude_hook(_hook(repo, "UserPromptSubmit", prompt=f"Fix word_utils.py; token {SECRET}"), env=env)
        (nested / "word_utils.py").write_text("x = 1\n", encoding="utf-8")
        handle_claude_hook(_hook(repo, "PostToolUse", tool_name="Write",
                                 tool_input={"file_path": str(nested / "word_utils.py")}), env=env)
        handle_claude_hook(_hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}), env=env)
        handle_claude_hook(_hook(repo, "Stop"), env=env)
        handle_claude_hook(_hook(repo, "SessionEnd", reason="prompt_input_exit"), env=env)
        events = _drain(telemetry_on)
        assert _types(events) == ["receipt.created", "receipt.completed"]
        done = events[1]["properties"]
        assert done["agent"] == "claude_code" and done["origin"] == "external_observed"
        assert done["capture_depth"] == "partial" and done["files_changed"] == 1
        assert done["files_source"] == "git_diff" and done["tool_calls"] == 2
        assert done["checks"] == "attempted_unverified" and done["turn_count"] == 1
        assert done["model_family"] == "unknown" and done["cost_usd"] is None
        _assert_clean(events)

    def test_capture_keeps_working_when_telemetry_explodes(self, telemetry_on, repo):
        state.set_consent("on", source="cli")
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        with patch("openshard.telemetry.client.emit", side_effect=RuntimeError("telemetry is on fire")):
            first = handle_claude_hook(_hook(repo, "UserPromptSubmit", prompt="task"), env=env)
            last = handle_claude_hook(_hook(repo, "Stop"), env=env)
        assert first.action == "record_created" and last.action == "record_updated"
        lines = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["task"] == "task"
        with patch("openshard.telemetry.state.load_state", side_effect=RuntimeError("state unreadable")):
            assert client.emit("install.seen", first_run=True) is False
            out = CliRunner().invoke(cli, ["telemetry", "status"])
            assert out.exit_code == 0

    def test_mcp_tool_call_emits_counts_only(self, telemetry_on):
        from openshard.mcp.server import _ToolCall

        state.set_consent("on", source="cli")
        telemetry_on.batches.clear()
        queue.clear()
        with _ToolCall("search_history") as call:
            call.results = 3
        with pytest.raises(KeyError):
            with _ToolCall("get_shard"):
                raise KeyError("shard-x not found in /home/someone/repo")
        events = _drain(telemetry_on)
        props = [e["properties"] for e in events if e["event_type"] == "mcp.tool_called"]
        assert props[0]["tool"] == "search_history" and props[0]["results"] == 3 and props[0]["result"] == "ok"
        assert props[1]["tool"] == "get_shard" and props[1]["result"] == "error"
        _assert_clean(events)

    def test_capture_service_lifecycle_event(self, telemetry_on):
        from openshard.adapters.claude_capture_service import _telemetry_service_event

        state.set_consent("on", source="cli")
        telemetry_on.batches.clear()
        queue.clear()

        class _Recorder:
            stats = {"queued": 15, "replayed": 15, "replay_errors": 0, "last_error": "/home/someone/x"}

        class _Server:
            @staticmethod
            def health_document():
                return {"blocking_ms": {"p50_ms": 9.2, "p95_ms": 66.1, "n": 15}, "pid": 1234}

        _telemetry_service_event(dict(os.environ), "started", _Recorder(), _Server())  # type: ignore[arg-type]
        _telemetry_service_event(dict(os.environ), "idle_exit", _Recorder(), _Server())  # type: ignore[arg-type]
        events = telemetry_on.events or _drain(telemetry_on)
        props = [e["properties"] for e in events if e["event_type"] == "capture.service"]
        assert [p["state"] for p in props] == ["started", "idle_exit"]
        assert props[0] == {"state": "started", "queued": 15, "folded": 15, "replay_errors": 0, "p50_ms": 9, "p95_ms": 66}
        _assert_clean(events)


# ---------------------------------------------------------------------------
# Onboarding: seeing the notice turns an undecided consent on; skipping does not
# ---------------------------------------------------------------------------


class TestOnboardingConsent:
    def test_cli_and_tui_notice_hooks(self, telemetry_on):
        from openshard.cli.ui.onboarding import _record_telemetry_notice_seen
        from openshard.onboarding.choices import LOCAL_FIRST_NOTICE
        from openshard.tui.onboarding_screen import OnboardingScreen

        assert "does not send telemetry" not in LOCAL_FIRST_NOTICE and "anonymous" not in LOCAL_FIRST_NOTICE
        assert "Help improve OpenShard: on" in LOCAL_FIRST_NOTICE and "openshard telemetry off" in LOCAL_FIRST_NOTICE
        assert "Richer development data stays off" in LOCAL_FIRST_NOTICE
        _record_telemetry_notice_seen()
        assert state.load_state().improve == "on" and state.load_state().improve_source == "onboarding"
        state.set_consent("off", source="cli")
        OnboardingScreen._record_telemetry_notice_seen()
        assert state.load_state().improve == "off"  # a decision is never overridden
