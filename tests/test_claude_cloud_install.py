"""Capture-only setup must work before the hosted agent is installed or launched."""
from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from openshard.adapters.claude_hooks_install import HOOK_EVENTS, SETTINGS_RELPATH, installed_events
from openshard.cli.main import cli
from tests.capture_fixtures import _make_repo


def test_deferred_claude_install_needs_no_cli_and_preserves_settings(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "cloud repo")
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    settings = repo / SETTINGS_RELPATH
    settings.parent.mkdir()
    custom = {"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo own-hook"}]}]}}
    settings.write_text(json.dumps(custom), encoding="utf-8")
    runner = CliRunner()
    command = ["capture", "install", "claude", "--repo-path", str(repo), "--defer-service", "--json"]
    with patch("openshard.adapters.claude_mcp_install.shutil.which", return_value=None), patch(
        "openshard.adapters.claude_setup.ensure_capture_service", side_effect=AssertionError("prelaunch service started")
    ):
        result = runner.invoke(cli, command, catch_exceptions=False)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent"] == "claude_code" and payload["configured"]
        assert not payload["cli_available"]
        assert payload["capture_service"]["state"] == "deferred"
        configured = json.loads(settings.read_text(encoding="utf-8"))
        assert set(HOOK_EVENTS) <= set(installed_events(configured))
        assert configured["permissions"] == custom["permissions"]
        assert custom["hooks"]["Stop"][0] in configured["hooks"]["Stop"]
        assert not (repo / ".mcp.json").exists()
        original = settings.read_bytes()
        result = runner.invoke(cli, command, catch_exceptions=False)
        assert result.exit_code == 0 and json.loads(result.output)["status"] == "already_installed"
        assert settings.read_bytes() == original
        history = repo / ".openshard" / "runs.jsonl"
        history.parent.mkdir(exist_ok=True)
        history.write_bytes(b'{"historical":true}\n')
        result = runner.invoke(cli, ["capture", "uninstall", "claude", "--repo-path", str(repo), "--json"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        removed = json.loads(settings.read_text(encoding="utf-8"))
        assert removed["permissions"] == custom["permissions"]
        assert removed["hooks"]["Stop"] == custom["hooks"]["Stop"]
        assert not installed_events(removed)
        assert history.read_bytes() == b'{"historical":true}\n'


def test_normal_claude_install_uses_existing_service_startup(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "normal repo")
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    with patch("openshard.adapters.claude_setup.ensure_capture_service", return_value={"state": "running", "port": 32123}) as start:
        result = CliRunner().invoke(cli, ["capture", "install", "claude", "--repo-path", str(repo), "--json"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        start.assert_called_once_with()
        assert json.loads(result.output)["capture_service"]["port"] == 32123
