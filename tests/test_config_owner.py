"""``identity.owner``: the explicit run owner, its CLI and where captures stamp it.

The owner is only ever what a person configured (``openshard config
set-owner``) -- never git ``user.name``, the OS account or organisation
metadata. Every test points ``OPENSHARD_HOME`` (the user-global config's
home) at a temp directory and runs in a throw-away working directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.config.settings import recorded_owner, user_config_path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("OPENSHARD_HOME", str(home))
    monkeypatch.delenv("OPENSHARD_CONFIG", raising=False)
    return home


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True, capture_output=True)
    monkeypatch.chdir(work)
    return work


def _invoke(*args: str):
    return CliRunner().invoke(cli, ["config", "set-owner", *args])


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestSetOwnerCommand:
    def test_writes_the_user_global_config_preserving_other_keys(self, home: Path, workdir: Path):
        user_config_path().parent.mkdir(parents=True)
        user_config_path().write_text("workflow: auto\nidentity:\n  team: core\n", encoding="utf-8")
        result = _invoke("Ada Lovelace")
        assert result.exit_code == 0, result.output
        assert user_config_path() == home / "config.yml"
        data = _yaml(home / "config.yml")
        assert data == {"workflow": "auto", "identity": {"team": "core", "owner": "Ada Lovelace"}}
        assert recorded_owner(workdir) == "Ada Lovelace"
        assert not (workdir / ".openshard").exists()  # the repository config is untouched

    def test_repo_flag_writes_the_repository_config_and_wins(self, home: Path, workdir: Path):
        assert _invoke("Global Person").exit_code == 0
        result = _invoke("--repo", "Repo Person")
        assert result.exit_code == 0, result.output
        assert _yaml(workdir / ".openshard" / "config.yml") == {"identity": {"owner": "Repo Person"}}
        assert recorded_owner(workdir) == "Repo Person"

    def test_repo_flag_never_shadows_a_root_config_yml(self, home: Path, workdir: Path):
        # A new .openshard/config.yml would replace ./config.yml for every setting: refused.
        (workdir / "config.yml").write_text("executor: direct\n", encoding="utf-8")
        result = _invoke("--repo", "Repo Person")
        assert result.exit_code != 0 and "config.yml" in result.output
        assert _yaml(workdir / "config.yml") == {"executor": "direct"}
        assert not (workdir / ".openshard").exists()

    def test_repo_flag_outside_a_repository_is_refused(self, home: Path, tmp_path: Path, monkeypatch):
        import openshard.adapters.claude_mcp_install as mcp_install

        monkeypatch.setattr(mcp_install, "find_repo_root", lambda start: None)
        result = _invoke("--repo", "Repo Person")
        assert result.exit_code != 0 and "repository" in result.output

    def test_rewriting_an_existing_file_warns_that_comments_are_lost(self, home: Path, workdir: Path):
        assert _invoke("Ada").exit_code == 0
        result = _invoke("Grace")
        assert result.exit_code == 0 and "comments" in result.output

    def test_clear_removes_only_the_owner(self, home: Path, workdir: Path):
        assert _invoke("Ada").exit_code == 0
        result = _invoke("--clear")
        assert result.exit_code == 0, result.output
        assert "identity" not in (_yaml(home / "config.yml") or {})
        assert recorded_owner(workdir) is None

    @pytest.mark.parametrize("args", [(), ("Ada", "--clear")])
    def test_owner_or_clear_is_required_exactly_once(self, home: Path, workdir: Path, args):
        result = _invoke(*args)
        assert result.exit_code != 0
        assert not (home / "config.yml").exists()

    @pytest.mark.parametrize("bad", ["/home/ada/secret", "sk-ant-api03-SECRETSECRET12345678901234567890"])
    def test_unsafe_values_are_refused(self, home: Path, workdir: Path, bad: str):
        assert _invoke(bad).exit_code != 0
        assert not (home / "config.yml").exists()


class TestOwnerIsStampedOnCaptures:
    def _repo(self, workdir: Path) -> Path:
        for args in (["init", "-q"], ["config", "user.name", "Git Name"], ["config", "user.email", "g@example.com"]):
            subprocess.run(["git", *args], cwd=workdir, check=True, capture_output=True)
        (workdir / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=workdir, check=True, capture_output=True)
        return workdir

    def _session(self, repo: Path) -> dict:
        from openshard.adapters.claude_hooks import handle_claude_hook

        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        sid = "0f1e2d3c-4b5a-4697-8877-665544332211"
        for event, extra in (("UserPromptSubmit", {"prompt": "task"}), ("Stop", {})):
            handle_claude_hook({"session_id": sid, "cwd": str(repo), "hook_event_name": event, **extra}, env=env)
        lines = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        return json.loads(lines[0])

    def test_hook_capture_carries_the_configured_owner_into_the_projection(self, home: Path, workdir: Path):
        from openshard.history.shard_contract import build_shard_receipt
        from openshard.history.views import receipt_to_dict

        assert _invoke("Ada Lovelace").exit_code == 0
        entry = self._session(self._repo(workdir))
        assert entry["owner"] == "Ada Lovelace"
        assert receipt_to_dict(build_shard_receipt(entry), extended=True)["owner"] == "Ada Lovelace"

    def test_no_owner_is_inferred_from_git_user_name(self, home: Path, workdir: Path):
        entry = self._session(self._repo(workdir))
        assert "owner" not in entry
