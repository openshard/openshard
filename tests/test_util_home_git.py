"""``openshard.util.home`` and ``openshard.util.git``: the shared helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from openshard.adapters import capture_auth
from openshard.adapters import claude_capture_client as client
from openshard.util.git import run_git
from openshard.util.home import openshard_home


class TestOpenshardHome:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("OPENSHARD_HOME", "/tmp/custom-home ")
        assert openshard_home() == "/tmp/custom-home"

    def test_blank_override_falls_back_to_user_home(self, monkeypatch):
        monkeypatch.setenv("OPENSHARD_HOME", "   ")
        assert openshard_home() == os.path.join(os.path.expanduser("~"), ".openshard")

    def test_explicit_env_mapping_is_honoured(self):
        assert openshard_home({"OPENSHARD_HOME": "/elsewhere"}) == "/elsewhere"
        assert openshard_home({}) == os.path.join(os.path.expanduser("~"), ".openshard")

    def test_capture_paths_share_the_resolver(self):
        env = {"OPENSHARD_HOME": "/one/home"}
        assert client.capture_home(env) == "/one/home"
        assert capture_auth.token_path(env) == os.path.join("/one/home", capture_auth.TOKEN_FILENAME)
        assert client.state_path(env) == os.path.join("/one/home", client.STATE_FILENAME)

    def test_caches_live_under_openshard_home(self, tmp_path: Path):
        # Both cache paths are resolved once at import through the shared
        # resolver, so check them in a fresh interpreter with the override set.
        import sys

        code = (
            "from openshard.providers.cache import CACHE_PATH;"
            "from openshard.models.openrouter_fetcher import _DEFAULT_CACHE_PATH;"
            "print(CACHE_PATH);print(_DEFAULT_CACHE_PATH)"
        )
        env = dict(os.environ, OPENSHARD_HOME=str(tmp_path))
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert out == [str(tmp_path / "model_cache.json"), str(tmp_path / "openrouter-models.json")]


class TestRunGit:
    def test_stdout_on_success(self, tmp_path: Path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        out = run_git(tmp_path, ["rev-parse", "--is-inside-work-tree"])
        assert out is not None and out.strip() == "true"

    def test_none_on_nonzero_exit(self, tmp_path: Path):
        assert run_git(tmp_path, ["rev-parse", "--is-inside-work-tree"]) is None

    def test_none_when_git_raises(self, tmp_path: Path):
        with patch("openshard.util.git.subprocess.run", side_effect=OSError("no git")):
            assert run_git(tmp_path, ["status"]) is None
        with patch(
            "openshard.util.git.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1),
        ):
            assert run_git(tmp_path, ["status"], timeout=1) is None

    def test_stdin_is_forwarded(self, tmp_path: Path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
        out = run_git(tmp_path, ["hash-object", "--stdin-paths"], stdin="a.txt\n")
        assert out is not None and len(out.strip()) == 40
