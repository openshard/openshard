"""The Claude plugin: Openshard capture for every Claude session, including cloud chats."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_plugin as cp
from openshard.adapters.claude_hooks_install import HOOK_EVENTS, TOOL_MATCHER
from openshard.cli.main import cli

CONN = cp.Connection(endpoint="https://api.openshard.dev", organisation_id="01ae53cd-d297-4be1-b30a-73f5dce72ff9",
                     token="osc_" + "a" * 32)

needs_sh = pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX sh")


def _unzip(path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(path) as archive:
        archive.extractall(dest)
    return dest


def test_every_capture_event_runs_the_capture_script():
    hooks = json.loads(cp.plugin_files(version="1.0")["hooks/hooks.json"])["hooks"]
    assert sorted(hooks) == sorted(HOOK_EVENTS)
    assert hooks["PostToolUse"][0]["matcher"] == TOOL_MATCHER
    for groups in hooks.values():
        (hook,) = groups[0]["hooks"]
        assert hook["type"] == "command"
        assert hook["command"] == 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/capture.sh"'


def test_without_a_connection_no_credential_is_written_and_the_readme_says_so():
    files = cp.plugin_files(version="1.0")
    assert cp.CONNECTION_FILE not in files
    assert "Receipts stay on the machine that made them" in files["README.md"]
    assert json.loads(files[".claude-plugin/plugin.json"])["name"] == "openshard"


def test_a_connection_is_written_and_its_exposure_is_stated():
    files = cp.plugin_files(version="1.0", connection=CONN)
    env = files[cp.CONNECTION_FILE]
    assert "OPENSHARD_CONNECTED_TOKEN=osc_" in env and "OPENSHARD_CONNECTED_ORG_ID=01ae53cd-d297-4be1-b30a-73f5dce72ff9" in env
    assert "The agent can read this file" in env
    assert "The agent in each session can read that file" in files["README.md"]


@pytest.mark.parametrize("field, value", [
    ("token", "osk_short"), ("token", "osc_aaaaaaaa; rm -rf ~"), ("token", "Bearer x"),
    ("endpoint", "http://api.openshard.dev"), ("endpoint", "https://x.dev/$(id)"),
    ("organisation_id", "org 1"), ("organisation_id", "org`id`"), ("organisation_id", "org-1"),
])
def test_unsafe_connection_values_are_refused(field, value):
    conn = cp.Connection(**{**CONN.__dict__, field: value})
    with pytest.raises(cp.PluginError):
        cp.plugin_files(version="1.0", connection=conn)


@pytest.mark.parametrize("spec", ['openshard"; rm -rf ~', "openshard $(id)", "open shard", ""])
def test_unsafe_install_specs_are_refused(spec):
    with pytest.raises(cp.PluginError):
        cp.plugin_files(version="1.0", install_spec=spec)


def test_the_archive_has_private_credentials_and_an_executable_script(tmp_path):
    out = cp.build_plugin(tmp_path / "openshard.plugin", version="1.0", connection=CONN)
    with zipfile.ZipFile(out) as archive:
        modes = {i.filename: stat.S_IMODE(i.external_attr >> 16) for i in archive.infolist()}
    assert modes["hooks/capture.sh"] == 0o755
    assert modes[cp.CONNECTION_FILE] == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_cli_writes_the_plugin_from_the_linked_platform(tmp_path, monkeypatch):
    from openshard.sync import config as sync_config

    link = sync_config.PlatformLink(endpoint="https://api.openshard.dev", organisation_id="11111111-2222-4333-8444-555555555555",
                                    api_key="osk_" + "b" * 32, linked_at=None, source="file")
    monkeypatch.setattr(sync_config, "load_link", lambda *a, **k: link)
    token = tmp_path / "token"
    token.write_text("osc_" + "c" * 32 + "\n", encoding="utf-8")
    out = tmp_path / "openshard.plugin"
    result = CliRunner().invoke(cli, ["plugin", "claude", "-o", str(out), "--token-file", str(token), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["connected"] is True
    env = zipfile.ZipFile(out).read(cp.CONNECTION_FILE).decode()
    assert "OPENSHARD_CONNECTED_ORG_ID=11111111-2222-4333-8444-555555555555" in env and "osc_" + "c" * 32 in env
    assert "osk_" not in env  # the broader Platform key never goes into the plugin


def test_cli_refuses_a_bad_credential(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("not-a-token", encoding="utf-8")
    result = CliRunner().invoke(cli, ["plugin", "claude", "-o", str(tmp_path / "p.plugin"),
                                      "--token-file", str(token), "--org", "11111111-2222-4333-8444-555555555555",
                                      "--endpoint", "https://e.dev"])
    assert result.exit_code != 0 and "credential is not valid" in result.output
    assert not (tmp_path / "p.plugin").exists()


# --- the capture script itself -------------------------------------------------------


@pytest.fixture
def machine(tmp_path):
    """A fresh machine: a plugin, an empty home, and no openshard on PATH."""
    plugin = _unzip(cp.build_plugin(tmp_path / "p.plugin", version="1.0", install_spec="/nonexistent/openshard"),
                    tmp_path / "plugin")
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(plugin),
           "OPENSHARD_HOME": str(home / ".openshard")}
    # An install already in progress, so these tests never start one.
    (home / ".openshard" / "plugin-install.lock").mkdir(parents=True)
    return plugin, home, bin_dir, env


def _hook(plugin: Path, env: dict[str, str], payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(plugin / "hooks" / "capture.sh")], input=payload, env=env,
                          capture_output=True, text=True, timeout=30)


def _fake_openshard(bin_dir: Path, log: Path) -> None:
    script = bin_dir / "openshard"
    script.write_text(f'#!/bin/sh\n[ "$1" = "--version" ] && exit 0\ncat >> "{log}"\necho >> "{log}"\n',
                      encoding="utf-8")
    script.chmod(0o755)


@needs_sh
def test_events_before_install_are_kept_then_delivered_in_order(machine, tmp_path):
    plugin, home, bin_dir, env = machine
    for i in range(3):
        result = _hook(plugin, env, json.dumps({"n": i}))
        assert result.returncode == 0 and result.stdout == ""
    spool = home / ".openshard" / "plugin-spool"
    assert len(list(spool.iterdir())) == 3
    log = tmp_path / "delivered.log"
    _fake_openshard(bin_dir, log)  # the install finished
    assert _hook(plugin, env, json.dumps({"n": 3})).returncode == 0
    delivered = [json.loads(line)["n"] for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert delivered == [0, 1, 2, 3]
    assert not spool.exists()


@needs_sh
def test_the_spool_is_bounded(machine):
    plugin, home, _bin, env = machine
    spool = home / ".openshard" / "plugin-spool"
    spool.mkdir()
    for i in range(cp.MAX_SPOOLED - 2):
        (spool / f"0-{i:06d}-1").write_text("{}", encoding="utf-8")
    for i in range(5):
        _hook(plugin, env, json.dumps({"n": i}))
    assert len(list(spool.iterdir())) == cp.MAX_SPOOLED


@needs_sh
def test_the_plugin_stands_aside_where_user_level_hooks_are_installed(machine, tmp_path):
    plugin, home, bin_dir, env = machine
    log = tmp_path / "delivered.log"
    _fake_openshard(bin_dir, log)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": '"/x/openshard" hooks claude --scope user'}]}]}}), encoding="utf-8")
    assert _hook(plugin, env, '{"n": 1}').returncode == 0
    assert not log.exists()


@needs_sh
def test_the_connection_reaches_openshard(machine, tmp_path):
    plugin, _home, bin_dir, env = machine
    (plugin / cp.CONNECTION_FILE).write_text(cp.plugin_files(version="1", connection=CONN)[cp.CONNECTION_FILE],
                                             encoding="utf-8")
    seen = tmp_path / "env.log"
    script = bin_dir / "openshard"
    script.write_text(f'#!/bin/sh\n[ "$1" = "--version" ] && exit 0\ncat >/dev/null\n'
                      f'env | grep OPENSHARD_CONNECTED_ | sort > "{seen}"\n', encoding="utf-8")
    script.chmod(0o755)
    assert _hook(plugin, env, "{}").returncode == 0
    lines = seen.read_text(encoding="utf-8").splitlines()
    assert "OPENSHARD_CONNECTED_ENDPOINT=https://api.openshard.dev" in lines
    assert "OPENSHARD_CONNECTED_SURFACE=claude-chat" in lines

