"""Claude Code hook health (``openshard doctor``) and the installed hook configuration hash.

Claude Code's hooks fail open, so a configured hook that is switched off,
points at a missing command or at a port nothing listens on captures
nothing and says nothing. These tests pin what doctor reports for each case
and that the hash is stable across everything that does not change what
runs.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_hook_health as health
from openshard.adapters.claude_hooks_install import build_hook_config


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict:
    """Isolated user config dir, an empty managed dir and a repo with OpenShard's hooks installed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "repo": repo,
        "user": tmp_path / "claude-config",
        "managed": tmp_path / "managed",
        "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config")},
    }


def _install(env: dict, *, port: int = 8765, capability: str | None = "cap-token") -> None:
    _write(env["repo"] / ".claude" / "settings.local.json", {"hooks": build_hook_config(port, capability)})


def _check(env: dict, *, which=lambda name: f"/usr/bin/{name}", port_open=lambda port: True) -> dict:
    return health.check_claude_hooks(
        env["repo"], env=env["env"], managed_dirs=[env["managed"]], which=which, port_open=port_open,
    )


def _codes(result: dict) -> list[str]:
    return [i["code"] for i in result["issues"]]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_healthy_install_has_no_issues(env):
    _install(env)
    result = _check(env)
    assert result["installed"] is True and result["ok"] is True and result["blocked"] is None
    assert result["issues"] == []
    assert result["events"]["PostToolUse"] == ["local"]
    assert health.doctor_rows(result) == [("Hook health", True, "see the hooks line above")]


def test_missing_hooks_are_left_to_the_existing_hooks_line(env):
    result = _check(env)
    assert result["installed"] is False and result["issues"] == []
    assert health.doctor_rows(result) == []


def test_command_not_on_path_blocks(env):
    _install(env)
    result = _check(env, which=lambda name: None)
    assert "command_not_found" in _codes(result)
    assert result["blocked"].startswith("`openshard` is not on this shell's PATH")
    assert "pipx install openshard" in result["blocked"]


def test_script_path_that_does_not_exist_or_is_not_executable(tmp_path):
    missing = health._command_issue(f"{tmp_path}/nope.sh --flag", lambda n: None)
    assert missing and missing["code"] == "command_not_found" and "does not exist" in missing["detail"]
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if os.name != "nt":
        bad = health._command_issue(str(script), lambda n: None)
        assert bad and bad["code"] == "command_not_executable"
    script.chmod(stat.S_IRWXU)
    assert health._command_issue(str(script), lambda n: None) is None


def test_managed_hooks_only_blocks_project_and_local_hooks(env):
    _install(env)
    _write(env["managed"] / "managed-settings.json", {"allowManagedHooksOnly": True})
    result = _check(env)
    assert _codes(result) == ["managed_hooks_only"]
    assert "allowManagedHooksOnly" in result["blocked"] and "local settings do not run" in result["blocked"]


def test_managed_hooks_only_is_fine_when_the_hooks_are_managed(env):
    _write(env["managed"] / "managed-settings.json",
           {"allowManagedHooksOnly": True, "hooks": build_hook_config(8765, None)})
    result = _check(env)
    assert result["installed"] is True and "managed_hooks_only" not in _codes(result)


def test_managed_drop_in_directory_is_read(env):
    _install(env)
    _write(env["managed"] / "managed-settings.d" / "10-policy.json", {"allowManagedHooksOnly": True})
    assert "managed_hooks_only" in _codes(_check(env))


def test_disable_all_hooks_in_user_settings_blocks(env):
    _install(env)
    _write(env["user"] / "settings.json", {"disableAllHooks": True})
    result = _check(env)
    assert _codes(result) == ["hooks_disabled"]
    assert "remove \"disableAllHooks\"" in result["blocked"]


def test_unreachable_capture_service_and_bad_timeouts_are_warnings(env):
    config = build_hook_config(8765, None)
    config["Stop"][0]["hooks"][0]["timeout"] = 1
    config["SessionStart"][0]["hooks"][0]["timeout"] = 300
    _write(env["repo"] / ".claude" / "settings.local.json", {"hooks": config})
    result = _check(env, port_open=lambda port: False)
    assert set(_codes(result)) == {"capture_service_unreachable", "timeout_too_short", "timeout_too_long"}
    assert result["blocked"] is None
    rows = health.doctor_rows(result)
    assert len(rows) == 1 and rows[0][0] == "Hook health" and rows[0][1] is False
    assert "nothing listens on 127.0.0.1:8765" in rows[0][2] and "(fix: " in rows[0][2]


def test_unreadable_user_settings_is_a_warning(env):
    _install(env)
    (env["user"]).mkdir(parents=True)
    (env["user"] / "settings.json").write_text("{not json", encoding="utf-8")
    result = _check(env)
    assert _codes(result) == ["settings_unreadable"]


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------


def _hash(env: dict) -> str:
    value = health.hook_config_hash(env["repo"], env=env["env"], managed_dirs=[env["managed"]])
    assert isinstance(value, str) and value.startswith("sha256:")
    return value


def test_hash_is_stable_across_formatting_order_and_credential(env):
    _install(env, capability="first-token")
    first = _hash(env)
    assert _hash(env) == first
    config = build_hook_config(8765, "rotated-token")
    reordered = dict(reversed(list(config.items())))
    (env["repo"] / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": []}, "hooks": reordered}), encoding="utf-8",
    )
    assert _hash(env) == first


def test_hash_ignores_the_repository_path(env, tmp_path):
    _install(env)
    other = dict(env, repo=tmp_path / "elsewhere")
    other["repo"].mkdir()
    _install(other)
    assert _hash(env) == _hash(other)


def test_hash_changes_when_what_runs_changes(env):
    _install(env)
    base = _hash(env)
    _install(env, port=9999)
    moved = _hash(env)
    assert moved != base
    _write(env["managed"] / "managed-settings.json", {"allowManagedHooksOnly": True})
    assert _hash(env) != moved


def test_hash_ignores_unrelated_hooks(env):
    _install(env)
    base = _hash(env)
    _write(env["user"] / "settings.json",
           {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "~/bin/lint.sh"}]}]}})
    assert _hash(env) == base


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_blocked_hooks_and_json(env, monkeypatch, tmp_path):
    import subprocess

    from openshard.cli.main import cli

    subprocess.run(["git", "init", "-q"], cwd=env["repo"], check=True)
    _install(env)
    _write(env["managed"] / "managed-settings.json", {"allowManagedHooksOnly": True})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env["user"]))
    monkeypatch.setattr(health, "_managed_dirs", lambda: [env["managed"]])
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(env["repo"])])
    assert result.exit_code == 0, result.output
    state = json.loads(result.output)
    hook_health = state["claude_code"]["hook_health"]
    assert hook_health["installed"] is True and "allowManagedHooksOnly" in hook_health["blocked"]
    assert "cap-token" not in result.output  # the credential never reaches doctor output
    human = runner.invoke(cli, ["doctor", "--repo-path", str(env["repo"])])
    assert human.exit_code == 0, human.output
    assert "✗ Auto-capture hooks (managed settings set allowManagedHooksOnly" in human.output
    assert "Ready -- use Claude Code normally" not in human.output
