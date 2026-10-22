"""The committed SessionStart hook installs Openshard in any recognised cloud sandbox, never on a teammate's machine."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from openshard.adapters import repo_setup as rs

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
CLOUDS = [
    ({"CLAUDE_CODE_REMOTE": "true"}, "Claude Code on the web"),
    ({"REPLICAS_WORKSPACE_ID": "5d0cde5e-3464-48f3-b2fd-58d0019eac25"}, "Replicas"),
    ({"E2B_SANDBOX": "true"}, "E2B"),
    ({"CODESPACES": "true"}, "GitHub Codespaces"),
    ({"GITPOD_WORKSPACE_ID": "openshard-abc123"}, "Gitpod"),
]
NOT_CLOUD = [{}, {"CLAUDE_CODE_REMOTE": "false"}, {"CLAUDE_CODE_REMOTE": "TRUE"}, {"E2B_SANDBOX": "0"}, {"CODESPACES": ""},
             {"REPLICAS_WORKSPACE_ID": ""}]


@pytest.mark.parametrize(("env", "label"), CLOUDS)
def test_each_sandbox_is_recognised_by_its_own_marker(env, label):
    assert rs.cloud_sandbox(env) == label


@pytest.mark.parametrize("env", NOT_CLOUD)
def test_a_machine_without_a_marker_is_not_a_sandbox(env):
    assert rs.cloud_sandbox(env) is None


def _session_start(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    rs.run_repo_setup(repo, ["claude"])
    return json.loads((repo / ".claude/settings.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]


def _run(tmp_path: Path, command: str, name: str, extra: dict[str, str]) -> tuple[subprocess.CompletedProcess, Path]:
    bindir, log, tmp = tmp_path / f"bin-{name}", tmp_path / f"{name}.log", tmp_path / f"tmp-{name}"
    bindir.mkdir()
    tmp.mkdir()
    stub = bindir / "pipx"  # stands in for the installer; nothing is really installed
    stub.write_text(f'#!/bin/sh\necho "pipx $*" >> "{log}"\n')
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp), **extra}
    return subprocess.run([SH, "-c", command], input="{}", capture_output=True, text=True, env=env, timeout=20), log


def _installed(log: Path) -> bool:
    for _ in range(50):
        if log.exists():
            return log.read_text().startswith("pipx install openshard")
        time.sleep(0.1)
    return False


@needs_sh
def test_every_recognised_sandbox_installs_in_the_background(tmp_path):
    command = _session_start(tmp_path)
    for i, (env, _label) in enumerate(CLOUDS):
        out, log = _run(tmp_path, command, f"cloud{i}", env)
        assert out.returncode == 0 and out.stdout == "", env
        assert "installing in the background" in out.stderr, env
        assert _installed(log), env


@needs_sh
def test_no_marker_or_an_opt_out_never_installs(tmp_path):
    command = _session_start(tmp_path)
    for i, env in enumerate([*NOT_CLOUD, {"REPLICAS_WORKSPACE_ID": "w", "OPENSHARD_REPO_BOOTSTRAP": "0"}]):
        out, log = _run(tmp_path, command, f"local{i}", env)
        assert out.returncode == 0 and "installing" not in out.stderr, env
        time.sleep(0.2)
        assert not log.exists(), env
