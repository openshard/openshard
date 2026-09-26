"""A sandboxed run's record names the real repository, not the throwaway worktree it ran in."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openshard.execution.generator import ChangedFile
from openshard.native.sandbox import create_run_sandbox
from openshard.run.pipeline import _log_run


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "a.py").write_text("x = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _log(cwd: Path, workspace: Path | None, extra_metadata: dict | None) -> dict:
    """Run the real _log_run with *cwd* as the current directory and return the stored record."""
    gen = MagicMock()
    gen.model = "m"
    gen.fixer_model = "f"
    original = os.getcwd()
    os.chdir(cwd)
    try:
        (cwd / ".openshard").mkdir(exist_ok=True)
        _log_run(
            start=time.time(), task="t", generator=gen, retry_triggered=False,
            files=[ChangedFile(path="a.py", change_type="update", content="", summary="")],
            verification_attempted=False, verification_passed=None, workspace=workspace,
            extra_metadata=extra_metadata, run_index=1,
        )
        return json.loads((cwd / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    finally:
        os.chdir(original)


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "myrepo")


class TestSandboxedRuns:
    def test_a_real_worktree_sandbox_reports_the_source_repository(self, repo):
        original = os.getcwd()
        os.chdir(repo)
        try:
            workspace, meta = create_run_sandbox(repo, "2026-09-26T13:30:25.646877Z")
        finally:
            os.chdir(original)
        try:
            assert meta.sandbox_type == "worktree" and workspace.name == "wt"
            entry = _log(repo, workspace, {"sandbox": asdict(meta)})
            assert entry["repo_name"] == "myrepo"  # was "wt"
            assert entry["git_branch"] == "main"  # was the temporary osn/run-... branch
            assert entry["git_head_commit_hash"]
            assert entry["workspace_path"] == str(workspace)  # the sandbox path is still recorded as the workspace
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(workspace)], cwd=str(repo), capture_output=True)

    def test_a_temp_dir_sandbox_outside_git_still_names_the_source(self, tmp_path):
        plain = tmp_path / "plainproj"
        plain.mkdir()
        ws = tmp_path / "tmpabc123"
        ws.mkdir()
        entry = _log(plain, ws, {"sandbox": {"sandbox_enabled": True, "sandbox_type": "temp"}})
        assert entry["repo_name"] == "plainproj"

    def test_the_branch_is_the_repositorys_own_branch(self, tmp_path):
        repo = _init_repo(tmp_path / "proj", branch="develop")
        ws = tmp_path / "wt"
        ws.mkdir()
        assert _log(repo, ws, {"sandbox": {"sandbox_enabled": True, "sandbox_type": "worktree"}})["git_branch"] == "develop"


class TestUnchangedBehaviour:
    def test_without_a_sandbox_the_workspace_is_still_what_is_described(self, tmp_path):
        cwd = _init_repo(tmp_path / "cwdrepo")
        other = _init_repo(tmp_path / "otherproj", branch="feature")
        entry = _log(cwd, other, None)
        assert entry["repo_name"] == "otherproj" and entry["git_branch"] == "feature"

    def test_a_disabled_sandbox_flag_does_not_redirect(self, tmp_path):
        cwd = _init_repo(tmp_path / "cwdrepo")
        other = _init_repo(tmp_path / "otherproj")
        entry = _log(cwd, other, {"sandbox": {"sandbox_enabled": False}})
        assert entry["repo_name"] == "otherproj"

    def test_no_workspace_uses_the_current_directory_as_before(self, repo):
        assert _log(repo, None, None)["repo_name"] == "myrepo"

    def test_a_malformed_sandbox_block_is_ignored(self, tmp_path):
        cwd = _init_repo(tmp_path / "cwdrepo")
        other = _init_repo(tmp_path / "otherproj")
        assert _log(cwd, other, {"sandbox": "yes"})["repo_name"] == "otherproj"
