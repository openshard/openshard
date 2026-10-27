"""Verifying uncommitted work on a private copy (verification/snapshot_run.py).

A session that ends with changed files and no commit gets, by default, an
OpenShard re-run of the checks its change calls for -- on an isolated copy
of a snapshot of the checkout, never the checkout itself. The result is
recorded as an OpenShard re-run that is *not* bound to a commit.

These tests pin: the snapshot leaves the user's index, branch, refs and files
untouched; pass and fail are read from real exit codes; the result never
binds to a commit; an already-verified snapshot is not re-run; and the
capture trigger starts it only for sessions that changed files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.cli.main import cli
from openshard.verification import auto
from openshard.verification import change_checks as cc
from openshard.verification import snapshot_run as sr

SID = "77777777-6666-4777-8888-999999999999"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _claude(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A captured session that changed pkg/widget.py and did not commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for rel, text in {
        "pyproject.toml": "[project]\nname='p'\n",
        "pkg/__init__.py": "",
        "pkg/widget.py": "def f():\n    return 1\n",
        "tests/__init__.py": "",
        "tests/test_widget.py": "from pkg.widget import f\n\ndef test_f():\n    assert f() == 2\n",
        ".gitignore": ".openshard/\n__pycache__/\n.pytest_cache/\n",
    }.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    monkeypatch.setattr(cc, "python_executable", lambda _root: sys.executable)
    _claude(root, "UserPromptSubmit", prompt="make f return 2")
    _write(root, "pkg/widget.py", "def f():\n    return 2\n")
    _claude(root, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(root / "pkg/widget.py")})
    _claude(root, "Stop")
    _claude(root, "SessionEnd", reason="exit")
    entry = [json.loads(ln) for ln in (root / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    return root, entry


def _verify(*args: str) -> dict:
    result = CliRunner().invoke(cli, ["verify", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _state(root: Path) -> tuple[str, str, str, str]:
    return (_git(root, "rev-parse", "HEAD"), _git(root, "status", "--porcelain"),
            _git(root, "for-each-ref"), _git(root, "diff", "--cached", "--name-only"))


class TestSnapshot:
    def test_the_snapshot_holds_tracked_and_untracked_work_and_touches_nothing(self, session):
        root, _entry = session
        _write(root, "notes/new.txt", "an untracked file\n")
        _git(root, "add", "pkg/__init__.py")  # something staged: the user's index must survive as it is
        before = _state(root)
        snap = sr.take_snapshot(root)
        assert snap is not None and snap.base == before[0] and snap.tree != snap.base_tree
        listed = _git(root, "ls-tree", "-r", "--name-only", snap.commit).splitlines()
        assert "notes/new.txt" in listed and "pkg/widget.py" in listed
        assert ".openshard/runs.jsonl" not in listed  # .gitignore is respected
        assert "return 2" in _git(root, "show", f"{snap.commit}:pkg/widget.py")
        assert _state(root) == before  # no ref, no index change, no file change

    def test_nothing_uncommitted_means_nothing_to_snapshot_run(self, session):
        root, entry = session
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "work")
        run = sr.verify_snapshot(root, root / ".openshard", entry, config={}, timeout=120, stream=False)
        assert run.status == sr.STATUS_NO_CHANGE

    def test_no_head_means_no_snapshot(self, tmp_path):
        root = tmp_path / "empty"
        root.mkdir()
        _git(root, "init", "-q")
        assert sr.take_snapshot(root) is None


class TestVerifySnapshot:
    def test_passing_changes_are_re_run_on_a_copy_and_never_bound_to_a_commit(self, session):
        root, entry = session
        before = _state(root)
        out = _verify("--receipt", entry["receipt_id"], "--snapshot")
        v = out["verification"]
        assert (v["status"], v["source"], v["observation_mode"]) == ("passed", "directly_observed", "openshard_executed")
        assert v["artifact_sha"] is None and "artifact_not_bound" in v["incomplete_reasons"]
        assert "private copy of the uncommitted changes" in v["reason"]
        assert out["checkout"]["mode"] == "isolated_worktree"
        assert out["snapshot"]["base"] == before[0] and len(out["snapshot"]["tree"]) >= 40
        assert _state(root) == before
        assert len(_git(root, "worktree", "list").splitlines()) == 1  # the copy is removed

    def test_failing_changes_are_recorded_failed(self, session):
        root, entry = session
        _write(root, "pkg/widget.py", "def f():\n    return 5\n")
        v = _verify("--receipt", entry["receipt_id"], "--snapshot")["verification"]
        assert (v["status"], v["checks_failed"], v["artifact_sha"]) == ("failed", 1, None)

    def test_the_same_changes_are_not_re_run_but_new_ones_are(self, session):
        root, entry = session
        assert _verify("--receipt", entry["receipt_id"], "--snapshot", "--auto")["status"] == "ok"
        assert _verify("--receipt", entry["receipt_id"], "--snapshot", "--auto")["status"] == "already_verified"
        _write(root, "pkg/extra.py", "X = 1\n")
        assert _verify("--receipt", entry["receipt_id"], "--snapshot", "--auto")["status"] == "ok"

    def test_snapshot_cannot_be_mixed_with_other_modes(self, session):
        _root, entry = session
        result = CliRunner().invoke(cli, ["verify", "--receipt", entry["receipt_id"], "--snapshot", "--commit", "HEAD"])
        assert result.exit_code != 0 and "--snapshot cannot be combined" in result.output


class TestTrigger:
    @pytest.fixture(autouse=True)
    def _spawns(self, monkeypatch):
        monkeypatch.delenv(auto.ENV_DISABLE, raising=False)
        self.spawned: list[list[str]] = []
        monkeypatch.setattr(auto, "_spawn_detached", lambda argv, cwd: self.spawned.append(argv) or True)

    def _finish(self, root: Path, prompt: str, change: bool) -> dict:
        _claude(root, "UserPromptSubmit", prompt=prompt)
        if change:
            _write(root, "pkg/widget.py", "def f():\n    return 2\n")
            _claude(root, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(root / "pkg/widget.py")})
        _claude(root, "Stop")
        _claude(root, "SessionEnd", reason="exit")
        return [json.loads(ln) for ln in (root / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init", "-q")
        _write(root, "pkg/widget.py", "def f():\n    return 1\n")
        _write(root, ".gitignore", ".openshard/\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        monkeypatch.chdir(root)
        return root

    def test_a_session_that_changed_files_gets_a_snapshot_run_by_default(self, repo):
        import time

        from openshard.verification import settle

        entry = self._finish(repo, "change f", change=True)
        auto.schedule_for_receipt(repo, entry, finalized=True, turn_ended=False)
        settle.run_due(repo, now=time.time() + 601)  # a per-turn Receipt runs once its Task settles
        runs = [a for a in self.spawned if "--snapshot" in a]
        assert runs and all("--auto" in a and "--approve" not in a for a in runs)

    def test_a_session_that_changed_nothing_gets_no_run(self, repo):
        import time

        from openshard.verification import settle

        entry = self._finish(repo, "explain f", change=False)
        assert auto.schedule_for_receipt(repo, entry, finalized=True, turn_ended=False) is False
        assert settle.run_due(repo, now=time.time() + 601) == 0
        assert not [a for a in self.spawned if "--snapshot" in a]

    def test_off_in_config_starts_nothing(self, repo):
        (repo / ".openshard").mkdir(exist_ok=True)
        (repo / ".openshard" / "config.yml").write_text("post_session_verify: off\n", encoding="utf-8")
        import time

        from openshard.verification import settle

        entry = self._finish(repo, "change f", change=True)
        auto.schedule_for_receipt(repo, entry, finalized=True, turn_ended=False)
        settle.run_due(repo, now=time.time() + 601)
        assert not [a for a in self.spawned if "--snapshot" in a]
