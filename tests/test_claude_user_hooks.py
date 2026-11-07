"""Install once, capture every repository: Claude Code hooks in the user's own settings."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_user_hooks as uh
from openshard.adapters.claude_hooks_install import HOOK_EVENTS
from openshard.cli.main import cli

EXE = "/opt/tools/bin/openshard"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    return path


def _settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _payload(event: str, cwd: Path, **extra: object) -> bytes:
    return json.dumps({"hook_event_name": event, "session_id": "s-1", "cwd": str(cwd), **extra}).encode()


def _no_openshard_on_path(tmp_path: Path) -> dict[str, str]:
    """An environment in which a repository's command hooks cannot find ``openshard``."""
    return {"PATH": str(tmp_path / "empty-bin"), "HOME": str(tmp_path / "nohome")}


# --- install / uninstall ------------------------------------------------------


def test_install_adds_every_capture_event_as_a_command_hook(tmp_path):
    path = tmp_path / "settings.json"
    result = uh.install_user_hooks(settings_path=path, executable=EXE)
    assert result.status == "installed"
    hooks = _settings(path)["hooks"]
    assert sorted(hooks) == sorted(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        (entry,) = [h for g in hooks[event] for h in g["hooks"]]
        assert entry == {"type": "command", "command": f'"{EXE}" hooks claude --scope user',
                         "timeout": entry["timeout"]}
    assert hooks["PostToolUse"][0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell|Read"


def test_install_keeps_the_persons_own_settings_and_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    own = {"model": "opus", "hooks": {"PostToolUse": [{"matcher": "Bash",
                                                        "hooks": [{"type": "command", "command": "echo mine"}]}]}}
    path.write_text(json.dumps(own), encoding="utf-8")
    uh.install_user_hooks(settings_path=path, executable=EXE)
    after = _settings(path)
    assert after["model"] == "opus"
    assert {"type": "command", "command": "echo mine"} in [h for g in after["hooks"]["PostToolUse"] for h in g["hooks"]]
    again = uh.install_user_hooks(settings_path=path, executable=EXE)
    assert again.status == "already_installed"
    assert _settings(path) == after


def test_a_new_executable_path_replaces_the_entry_instead_of_adding_one(tmp_path):
    path = tmp_path / "settings.json"
    uh.install_user_hooks(settings_path=path, executable=EXE)
    result = uh.install_user_hooks(settings_path=path, executable="/new/place/openshard")
    assert result.status == "updated"
    stop = [h for g in _settings(path)["hooks"]["Stop"] for h in g["hooks"]]
    assert stop == [{"type": "command", "command": '"/new/place/openshard" hooks claude --scope user', "timeout": 5}]


def test_uninstall_restores_the_file_exactly(tmp_path):
    path = tmp_path / "settings.json"
    own = {"model": "opus", "hooks": {"PostToolUse": [{"matcher": "Bash",
                                                        "hooks": [{"type": "command", "command": "echo mine"}]}]}}
    path.write_text(json.dumps(own), encoding="utf-8")
    uh.install_user_hooks(settings_path=path, executable=EXE)
    assert uh.uninstall_user_hooks(settings_path=path).status == "removed"
    assert _settings(path) == own
    assert uh.uninstall_user_hooks(settings_path=path).status == "not_installed"


def test_uninstall_of_hooks_into_an_empty_file_leaves_no_hooks_key(tmp_path):
    path = tmp_path / "settings.json"
    uh.install_user_hooks(settings_path=path, executable=EXE)
    uh.uninstall_user_hooks(settings_path=path)
    assert _settings(path) == {}


def test_invalid_settings_are_never_overwritten(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    result = uh.install_user_hooks(settings_path=path, executable=EXE)
    assert result.status == "error"
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_settings_path_honours_claude_config_dir(tmp_path):
    assert uh.user_settings_path({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path / "settings.json"
    assert uh.user_settings_path({}) == Path.home() / ".claude" / "settings.json"


def test_user_hooks_installed_reports_presence(tmp_path):
    path = tmp_path / "settings.json"
    assert uh.user_hooks_installed(path) is False
    uh.install_user_hooks(settings_path=path, executable=EXE)
    assert uh.user_hooks_installed(path) is True


# --- which events are delivered ----------------------------------------------------


def test_an_unset_up_repository_is_delivered(tmp_path):
    repo = _git_repo(tmp_path / "alpha")
    assert uh.user_scope_skip_reason(_payload("SessionStart", repo), {"CLAUDE_PROJECT_DIR": str(repo)}) is None


def test_a_subdirectory_of_a_repository_is_delivered(tmp_path):
    repo = _git_repo(tmp_path / "alpha")
    (repo / "src").mkdir()
    assert uh.user_scope_skip_reason(_payload("Stop", repo / "src"), {}) is None


def test_a_folder_that_is_not_a_repository_is_skipped(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    reason = uh.user_scope_skip_reason(_payload("SessionStart", folder), {"CLAUDE_PROJECT_DIR": str(folder)})
    assert reason == "not inside a git repository"


def test_a_parent_folder_of_several_repositories_is_skipped_even_when_cwd_is_inside_one(tmp_path):
    parent = tmp_path / "code"
    alpha = _git_repo(parent / "alpha")
    _git_repo(parent / "beta")
    reason = uh.user_scope_skip_reason(_payload("PostToolUse", alpha), {"CLAUDE_PROJECT_DIR": str(parent)})
    assert reason == "not inside a git repository"


def test_the_home_directory_is_never_captured(tmp_path, monkeypatch):
    home = _git_repo(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    reason = uh.user_scope_skip_reason(_payload("SessionStart", home), {"CLAUDE_PROJECT_DIR": str(home)})
    assert reason == "the home directory is never captured"


def test_malformed_payload_is_skipped():
    assert uh.user_scope_skip_reason(b"not json", {}) == "empty or malformed payload"
    assert uh.user_scope_skip_reason(b"", {}) == "empty or malformed payload"


def test_a_repository_with_local_http_hooks_captures_through_them(tmp_path):
    repo = _git_repo(tmp_path / "alpha")
    local = repo / ".claude" / "settings.local.json"
    local.parent.mkdir()
    local.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "http", "url": "http://127.0.0.1:47811/hooks/claude"}]}]}}), encoding="utf-8")
    reason = uh.user_scope_skip_reason(_payload("Stop", repo), _no_openshard_on_path(tmp_path))
    assert reason == "this repository has its own Openshard hooks"


def _commit_repo_hooks(repo: Path) -> None:
    from openshard.adapters.repo_setup import run_repo_setup

    assert run_repo_setup(repo, ["claude"]).ok


def test_committed_repo_hooks_that_can_run_are_used_instead(tmp_path):
    repo = _git_repo(tmp_path / "alpha")
    _commit_repo_hooks(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "openshard"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path / "nohome"), "CLAUDE_PROJECT_DIR": str(repo)}
    assert uh.user_scope_skip_reason(_payload("Stop", repo), env) == "this repository has its own Openshard hooks"


def test_committed_repo_hooks_that_cannot_find_openshard_do_not_silence_capture(tmp_path):
    """Their shell finds no ``openshard`` and records nothing, so the user-level hook must deliver."""
    repo = _git_repo(tmp_path / "alpha")
    _commit_repo_hooks(repo)
    env = {**_no_openshard_on_path(tmp_path), "CLAUDE_PROJECT_DIR": str(repo)}
    assert uh.user_scope_skip_reason(_payload("Stop", repo), env) is None


def test_the_users_own_level_hook_in_a_project_file_is_not_mistaken_for_repo_hooks(tmp_path):
    repo = _git_repo(tmp_path / "alpha")
    uh.install_user_hooks(settings_path=repo / ".claude" / "settings.json", executable=EXE)
    assert uh.project_has_openshard_hooks(repo, _no_openshard_on_path(tmp_path)) is False


# --- end to end through the real capture path ------------------------------------


def _run_session(start: Path, edited: Path, env: dict[str, str]) -> list[str]:
    outcomes = []
    for event, extra in (
        ("SessionStart", {"source": "startup"}),
        ("UserPromptSubmit", {"prompt": f"task in {start.name}"}),
        ("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(edited)},
                         "tool_response": {"success": True}}),
        ("Stop", {}),
        ("SessionEnd", {"reason": "exit"}),
    ):
        if event == "PostToolUse" and edited.parent.is_dir():
            edited.write_text("changed\n", encoding="utf-8")
        outcomes.append(uh.run_user_scope_hook(io.BytesIO(_payload(event, start, **extra)), env=env))
    return outcomes


def _receipt_tasks(repo: Path) -> list[str]:
    runs = repo / ".openshard" / "runs.jsonl"
    if not runs.exists():
        return []
    return [json.loads(line).get("task") for line in runs.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("name", ["alpha", "beta"])
def test_every_unset_up_repository_gets_its_own_receipt(tmp_path, monkeypatch, name):
    import os

    repo = _git_repo(tmp_path / "code" / name)
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    outcomes = _run_session(repo, repo / "m.py", env)
    assert not any(o.startswith("skipped") for o in outcomes), outcomes
    assert _receipt_tasks(repo) == [f"task in {name}"]


def test_a_skipped_session_writes_nothing_anywhere(tmp_path):
    import os

    parent = tmp_path / "code"
    alpha = _git_repo(parent / "alpha")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(parent)}
    outcomes = _run_session(parent, alpha / "m.py", env)
    assert all(o == "skipped: not inside a git repository" for o in outcomes), outcomes
    assert not (parent / ".openshard").exists()
    assert not (alpha / ".openshard").exists()


# --- CLI ----------------------------------------------------------------------


def test_cli_install_and_uninstall_global(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    runner = CliRunner()
    result = runner.invoke(cli, ["capture", "install", "claude", "--global", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["scope"] == "user" and data["status"] == "installed"
    assert Path(data["settings_path"]) == tmp_path / "claude" / "settings.json"
    assert data["platform_linked"] is False
    result = runner.invoke(cli, ["capture", "uninstall", "claude", "--global", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "removed"


def test_cli_install_global_plain_output_says_what_it_does(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    result = CliRunner().invoke(cli, ["capture", "install", "claude", "--global"])
    assert result.exit_code == 0, result.output
    assert "every git repository" in result.output.lower()
    assert "openshard login" in result.output


@pytest.mark.parametrize("args, message", [
    (["capture", "install", "codex", "--global"], "--global is supported for claude only."),
    (["capture", "install", "claude", "--global", "--learn"], "cannot be combined"),
    (["capture", "uninstall", "cursor", "--global"], "--global is supported for claude only."),
])
def test_cli_global_refuses_unsupported_combinations(tmp_path, monkeypatch, args, message):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert message in result.output
    assert not (tmp_path / "claude" / "settings.json").exists()


def test_cli_hook_with_user_scope_outside_a_repository_records_nothing(tmp_path, monkeypatch):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(folder))
    result = CliRunner().invoke(cli, ["hooks", "claude", "--scope", "user"],
                                input=_payload("SessionStart", folder).decode())
    assert result.exit_code == 0
    assert result.output == ""
    assert not (folder / ".openshard").exists()
