"""``openshard setup --repo``: committable repository config for cloud agents (adapters/repo_setup.py)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from openshard.adapters import repo_setup as rs
from openshard.adapters.cursor_hooks_install import install_cursor_hooks
from openshard.cli.main import cli

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None or os.name == "nt", reason="POSIX shell hook commands")

ALL_FILES = (
    ".claude/settings.json",
    ".cursor/hooks.json",
    ".cursor/environment.json",
    ".github/workflows/copilot-setup-steps.yml",
    ".github/hooks/openshard.json",
    ".openhands/setup.sh",
    ".openhands/hooks.json",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _setup(root: Path, *args: str):
    return CliRunner().invoke(cli, ["setup", "--repo", "--repo-path", str(root), *args])


def _status(result: rs.RepoSetupResult) -> dict[str, str]:
    return {c.path: c.status for c in result.changes}


# --- from scratch, re-run, dry run ------------------------------------------------


def test_fresh_repo_writes_every_file_valid_and_credential_free(repo: Path) -> None:
    res = _setup(repo)
    assert res.exit_code == 0, res.output
    for rel in ALL_FILES:
        text = (repo / rel).read_text(encoding="utf-8")
        assert "created" in res.output and rel in res.output
        assert not rs._CREDENTIAL_RE.search(text), rel
        if rel.endswith(".json"):
            json.loads(text)
        elif rel.endswith(".yml"):
            yaml.safe_load(text)
    assert not (repo / ".devcontainer").exists()  # never created
    assert os.access(repo / ".openhands/setup.sh", os.X_OK)

    claude = json.loads((repo / ".claude/settings.json").read_text())
    assert set(claude["hooks"]) == {spec.event for spec in rs.CLAUDE_HOOK_SPECS}  # same events as a local install
    assert set(claude["hooks"]) >= {"SessionStart", "PostModelSwitch", "UserPromptSubmit", "PostToolUse",
                                    "PostToolUseFailure", "Stop", "SessionEnd"}
    workflow = yaml.safe_load((repo / ".github/workflows/copilot-setup-steps.yml").read_text())
    steps = workflow["jobs"]["copilot-setup-steps"]["steps"]
    assert steps[-1]["name"] == rs.COPILOT_STEP_NAME
    assert "env" not in steps[-1]  # an explicit mapping could blank the Agents secrets Copilot exposes
    copilot = json.loads((repo / ".github/hooks/openshard.json").read_text())
    assert copilot["version"] == 1 and set(copilot["hooks"]) == {"sessionEnd", "agentStop"}
    openhands = json.loads((repo / ".openhands/hooks.json").read_text())
    assert openhands["stop"][0]["matcher"] == "*"
    assert "not captured event by event" in res.output  # Copilot/OpenHands limits are stated


def test_rerun_is_idempotent_and_writes_nothing(repo: Path) -> None:
    rs.run_repo_setup(repo, list(rs.FILE_AGENTS))
    before = _snapshot(repo)
    again = rs.run_repo_setup(repo, list(rs.FILE_AGENTS))
    assert set(_status(again).values()) <= {"unchanged", "skipped"}
    assert _snapshot(repo) == before


def test_dry_run_writes_nothing(repo: Path) -> None:
    (repo / ".cursor").mkdir()
    (repo / ".cursor/hooks.json").write_text('{"version": 1, "hooks": {}}\n')
    before = _snapshot(repo)
    res = _setup(repo, "--dry-run")
    assert res.exit_code == 0, res.output
    assert "would create" in res.output and "would update" in res.output
    assert _snapshot(repo) == before


def test_agents_filter_and_snippets(repo: Path) -> None:
    res = _setup(repo, "--agents", "copilot,devin")
    assert res.exit_code == 0, res.output
    assert (repo / ".github/hooks/openshard.json").exists()
    assert not (repo / ".claude").exists() and not (repo / ".cursor").exists()
    assert "devin: add to the environment" in res.output and rs.SETUP_MARKER in res.output
    bad = _setup(repo, "--agents", "nope")
    assert bad.exit_code == 2 and "unknown agent" in bad.output


# --- merging into existing content ------------------------------------------------


def test_claude_merge_keeps_user_hooks_and_replaces_local_openshard_entries(repo: Path) -> None:
    (repo / ".claude").mkdir()
    user_hook = {"type": "command", "command": "./lint.sh"}
    committed_http = {"type": "http", "url": "http://127.0.0.1:47321/hooks/claude",
                      "headers": {"X-OpenShard-Capture-Token": "r2." + "a" * 40}}
    (repo / ".claude/settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"Stop": [{"hooks": [user_hook, committed_http]}]},
    }))
    result = rs.run_repo_setup(repo, ["claude"])
    assert _status(result)[".claude/settings.json"] == "updated"
    data = json.loads((repo / ".claude/settings.json").read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    stop = [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert user_hook in stop
    assert sum(rs.is_repo_hook(h) for h in stop) == 1
    assert "r2." not in (repo / ".claude/settings.json").read_text()  # the committed capability is gone


def test_cursor_merge_and_local_install_do_not_double_deliver(repo: Path) -> None:
    (repo / ".cursor").mkdir()
    (repo / ".cursor/environment.json").write_text(json.dumps({"install": "npm ci", "terminals": []}))
    local = install_cursor_hooks(repo_root=repo)  # the per-user local form
    assert local.status == "installed"
    rs.run_repo_setup(repo, ["cursor"])
    hooks = json.loads((repo / ".cursor/hooks.json").read_text())["hooks"]
    for event, entries in hooks.items():
        assert len(entries) == 1 and rs.is_repo_hook(entries[0]), event
    env = json.loads((repo / ".cursor/environment.json").read_text())
    assert env["terminals"] == [] and env["install"] == f"{rs.SETUP_ONE_LINE}; npm ci"
    # A later local install accepts the committed form instead of adding a second entry.
    assert install_cursor_hooks(repo_root=repo).status == "already_installed"
    assert json.loads((repo / ".cursor/hooks.json").read_text())["hooks"] == hooks


def test_copilot_workflow_merge_appends_step_and_keeps_comments(repo: Path) -> None:
    wf = repo / ".github/workflows/copilot-setup-steps.yml"
    wf.parent.mkdir(parents=True)
    original = (
        "# team setup\n"
        "name: Copilot Setup Steps\n"
        "on: workflow_dispatch\n"
        "jobs:\n"
        "  copilot-setup-steps:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "    - uses: actions/checkout@v4\n"
        "    - name: Node\n"
        "      run: |\n"
        "        npm ci\n"
        "\n"
        "    timeout-minutes: 30\n"
    )
    wf.write_text(original)
    result = rs.run_repo_setup(repo, ["copilot"])
    assert _status(result)[".github/workflows/copilot-setup-steps.yml"] == "updated"
    text = wf.read_text()
    assert text.startswith("# team setup\n")
    job = yaml.safe_load(text)["jobs"]["copilot-setup-steps"]
    assert job["timeout-minutes"] == 30
    assert [s.get("name") for s in job["steps"]] == [None, "Node", rs.COPILOT_STEP_NAME]
    assert job["steps"][1]["run"] == "npm ci\n"
    again = rs.run_repo_setup(repo, ["copilot"])
    assert _status(again)[".github/workflows/copilot-setup-steps.yml"] == "unchanged"


def test_copilot_workflow_without_the_job_is_left_for_manual_edit(repo: Path) -> None:
    wf = repo / ".github/workflows/copilot-setup-steps.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("name: x\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n")
    before = wf.read_bytes()
    result = rs.run_repo_setup(repo, ["copilot"])
    assert _status(result)[".github/workflows/copilot-setup-steps.yml"] == "manual"
    assert wf.read_bytes() == before


def test_openhands_merge_appends_block_once_and_keeps_hooks(repo: Path) -> None:
    (repo / ".openhands").mkdir()
    setup = repo / ".openhands/setup.sh"
    setup.write_text("#!/bin/bash\nset -e\nmake deps")  # no trailing newline
    setup.chmod(0o700)
    (repo / ".openhands/hooks.json").write_text(json.dumps(
        {"pre_tool_use": [{"matcher": "terminal", "hooks": [{"command": "./guard.sh"}]}]}))
    rs.run_repo_setup(repo, ["openhands"])
    text = setup.read_text()
    assert text.startswith("#!/bin/bash\nset -e\nmake deps\n" + rs.BLOCK_BEGIN)
    if os.name != "nt":
        assert (setup.stat().st_mode & 0o777) == 0o700
    hooks = json.loads((repo / ".openhands/hooks.json").read_text())
    assert hooks["pre_tool_use"][0]["hooks"] == [{"command": "./guard.sh"}]
    rs.run_repo_setup(repo, ["openhands"])
    assert setup.read_text().count(rs.BLOCK_BEGIN) == 1


def test_devcontainer_only_when_present(repo: Path) -> None:
    res = rs.run_repo_setup(repo, ["devcontainer"])
    assert [c.status for c in res.changes] == ["skipped"] and not (repo / ".devcontainer").exists()
    (repo / ".devcontainer").mkdir()
    dc = repo / ".devcontainer/devcontainer.json"
    dc.write_text(json.dumps({"image": "python:3.12", "postCreateCommand": "pip install -e ."}))
    rs.run_repo_setup(repo, ["devcontainer"])
    assert json.loads(dc.read_text())["postCreateCommand"] == f"{rs.SETUP_ONE_LINE}; pip install -e ."
    dc.write_text(json.dumps({"postCreateCommand": {"deps": "make"}}))
    rs.run_repo_setup(repo, ["devcontainer"])
    assert json.loads(dc.read_text())["postCreateCommand"] == {"deps": "make", "openshard": rs.SETUP_ONE_LINE}
    for unmergeable in ('{\n  // comment\n  "image": "x"\n}\n', json.dumps({"postCreateCommand": ["make"]})):
        dc.write_text(unmergeable)
        res = rs.run_repo_setup(repo, ["devcontainer"])
        assert res.changes[0].status == "manual" and dc.read_text() == unmergeable


def test_unparseable_files_are_never_clobbered(repo: Path) -> None:
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text("{not json")
    res = _setup(repo, "--agents", "claude")
    assert res.exit_code == 1 and "error" in res.output
    assert (repo / ".claude/settings.json").read_text() == "{not json"


def test_existing_credential_blocks_the_write(repo: Path) -> None:
    (repo / ".github/hooks").mkdir(parents=True)
    leaked = json.dumps({"version": 1, "hooks": {}, "env": {"T": "osr_abcdefgh_" + "x" * 24}})
    (repo / ".github/hooks/openshard.json").write_text(leaked)
    res = rs.run_repo_setup(repo, ["copilot"])
    assert _status(res)[".github/hooks/openshard.json"] == "error"
    assert (repo / ".github/hooks/openshard.json").read_text() == leaked


def test_written_file_that_git_ignores_is_warned(repo: Path) -> None:
    (repo / ".gitignore").write_text(".cursor/\n")
    res = rs.run_repo_setup(repo, ["cursor"])
    assert any(".cursor/hooks.json is git-ignored" in w for w in res.warnings)


# --- hook commands fail open ------------------------------------------------------


def _run(command: str, env: dict[str, str], stdin: str = "{}") -> subprocess.CompletedProcess:
    # Run from HOME, not the developer's checkout: hooks read ${CLAUDE_PROJECT_DIR:-.}/.claude/settings.local.json.
    return subprocess.run([SH, "-c", command], input=stdin, capture_output=True, text=True, env=env, timeout=20,
                          cwd=env["HOME"])


def _env(tmp_path: Path, path: str, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return {"PATH": path, "HOME": str(home), "TMPDIR": str(tmp_path / "tmp"), **extra}


def _stub(bindir: Path, name: str, log: Path) -> None:
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\ncat >> "{log}"\n')
    script.chmod(0o755)


@needs_sh
def test_hooks_fail_open_when_openshard_is_missing(repo: Path, tmp_path: Path) -> None:
    rs.run_repo_setup(repo, list(rs.FILE_AGENTS))
    if shutil.which("openshard", path="/usr/bin:/bin"):
        pytest.skip("openshard is installed system-wide")
    env = _env(tmp_path, "/usr/bin:/bin")
    claude = json.loads((repo / ".claude/settings.json").read_text())["hooks"]
    for event, groups in claude.items():
        out = _run(groups[0]["hooks"][0]["command"], env)
        assert out.returncode == 0 and out.stdout == "", event
        assert "not installed" in out.stderr
    cursor = json.loads((repo / ".cursor/hooks.json").read_text())["hooks"]
    out = _run(cursor["beforeSubmitPrompt"][0]["command"], env)
    assert out.returncode == 0 and json.loads(out.stdout) == {"continue": True}
    out = _run(cursor["stop"][0]["command"], env)
    assert out.returncode == 0 and json.loads(out.stdout) == {}
    copilot = json.loads((repo / ".github/hooks/openshard.json").read_text())["hooks"]
    assert _run(copilot["agentStop"][0]["bash"], env).returncode == 0
    openhands = json.loads((repo / ".openhands/hooks.json").read_text())["stop"][0]["hooks"][0]
    assert _run(openhands["command"], env).stdout == ""


@needs_sh
def test_claude_session_start_bootstraps_in_the_background_only_in_cloud(repo: Path, tmp_path: Path) -> None:
    rs.run_repo_setup(repo, ["claude"])
    command = json.loads((repo / ".claude/settings.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    log = tmp_path / "calls.log"
    _stub(tmp_path / "bin", "pipx", log)  # stands in for the installer; nothing is really installed
    path = f"{tmp_path / 'bin'}:/usr/bin:/bin"
    local = _run(command, _env(tmp_path, path))
    assert local.returncode == 0 and not log.exists()  # a teammate's machine never gets a silent install
    cloud = _run(command, _env(tmp_path, path, CLAUDE_CODE_REMOTE="true"))
    assert cloud.returncode == 0 and cloud.stdout == ""
    assert "installing in the background" in cloud.stderr and "not linked" in cloud.stderr
    for _ in range(50):
        if log.exists():
            break
        time.sleep(0.1)
    assert log.read_text().startswith("pipx install openshard")
    (tmp_path / "tmp2").mkdir()
    opted_out = _run(command, _env(tmp_path, path, CLAUDE_CODE_REMOTE="true", OPENSHARD_REPO_BOOTSTRAP="0",
                                   TMPDIR=str(tmp_path / "tmp2")))
    assert opted_out.returncode == 0 and "installing" not in opted_out.stderr


@needs_sh
def test_hooks_run_openshard_when_installed_and_defer_to_local_hooks(repo: Path, tmp_path: Path) -> None:
    rs.run_repo_setup(repo, ["claude", "cursor"])
    log = tmp_path / "calls.log"
    _stub(tmp_path / "bin", "openshard", log)
    env = _env(tmp_path, f"{tmp_path / 'bin'}:/usr/bin:/bin", CLAUDE_PROJECT_DIR=str(repo))
    command = json.loads((repo / ".claude/settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert _run(command, env, stdin='{"hook_event_name": "Stop"}').returncode == 0
    assert log.read_text() == 'openshard hooks claude\n{"hook_event_name": "Stop"}'
    end = json.loads((repo / ".cursor/hooks.json").read_text())["hooks"]["sessionEnd"][0]["command"]
    _run(end, env, stdin="")
    assert "openshard hooks cursor --no-spawn" in log.read_text()
    # This clone has the per-user local hooks: the committed ones stand aside.
    (repo / ".claude/settings.local.json").write_text('{"hooks": {"Stop": [{"hooks": [{"command": "openshard hooks claude"}]}]}}')
    log.unlink()
    assert _run(command, env).returncode == 0 and not log.exists()


@needs_sh
def test_setup_step_reports_unlinked_capture_and_never_fails(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    _stub(tmp_path / "bin", "openshard", log)
    env = _env(tmp_path, f"{tmp_path / 'bin'}:/usr/bin:/bin")
    out = _run("set -e; " + rs.SETUP_ONE_LINE + "; echo after", env, stdin="")
    assert out.returncode == 0 and out.stdout == "after\n" and "not linked" in out.stderr
    assert not log.exists()
    linked = dict(env, OPENSHARD_REMOTE_CAPTURE_URL="https://api.example/v1/remote-captures/x",
                  OPENSHARD_REMOTE_TOKEN="from-env")
    assert _run(rs.SETUP_ONE_LINE, linked, stdin="").returncode == 0
    assert log.read_text().startswith("openshard remote attach")
