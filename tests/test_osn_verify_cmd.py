"""`osn run` without `--verify-cmd`: the check comes from the repository's contract or detection, or the run refuses.

OSN never reports work verified without a check it ran itself, so a run with
no known verification command does not start. The Receipt and the checkpoint
name where the command came from (user / config / detected), and a detected
or configured command that the safety classifier would not run silently is
refused unless the user passes it explicitly.
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.osn import checkpoint as ckpt
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK_OK = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'
CONTRACT = "python -m pytest -q tests"  # a contract command must be one the safety classifier calls safe
TEST_FILE = "def test_out():\n    assert open('out.txt').read() == 'ok'\n"


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        content = self.replies.pop(0)
        if isinstance(content, BaseException):
            raise content
        return ChatResponse(content, model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


def _repo(tmp_path, *, with_tests=False):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "out.txt").write_text("bad")
    if with_tests:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_out.py").write_text(TEST_FILE)
    return repo


def _config(repo, command: str):
    (repo / ".openshard").mkdir(exist_ok=True)
    (repo / ".openshard" / "config.yml").write_text(f"verification_commands:\n  - {json.dumps(command)}\n")


def _writes(content="ok"):
    return json.dumps({"actions": [{"kind": "write_file", "path": "out.txt", "content": content, "intent": "w"},
                                   {"kind": "finish", "intent": "done"}]})


def _invoke(monkeypatch, repo, *args):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", FakeProvider([_writes()])))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--roles", "executor",
                                    "--no-learning", *args])


def _last_entry(repo):
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]


def test_no_known_verification_command_refuses_to_start(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _invoke(monkeypatch, repo)
    assert r.exit_code != 0
    assert "No verification command is known for this repository" in r.output
    assert "--verify-cmd" in r.output and "verification_commands" in r.output
    assert not (repo / ".openshard" / "runs.jsonl").exists() and not ckpt.list_checkpoints(repo)


def test_the_repository_contract_is_used_and_named(tmp_path, monkeypatch):
    repo = _repo(tmp_path, with_tests=True)
    _config(repo, CONTRACT)
    r = _invoke(monkeypatch, repo)
    assert r.exit_code == 0, r.output
    assert f"Verify  {CONTRACT} (the repository's verification contract" in r.output
    assert "OSN loop: verified" in r.output
    entry = _last_entry(repo)
    assert entry["osn_loop"]["verification_command"] == {"label": CONTRACT, "source": "config"}
    assert ckpt.list_checkpoints(repo)[0].args["verify_source"] == "config"


def test_a_detected_test_command_is_used_when_there_is_no_contract(tmp_path, monkeypatch):
    repo = _repo(tmp_path, with_tests=True)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    r = _invoke(monkeypatch, repo)
    assert r.exit_code == 0, r.output
    assert "Verify  python -m pytest (detected from the repository; pass --verify-cmd to override)" in r.output
    assert "OSN loop: verified" in r.output
    entry = _last_entry(repo)
    assert entry["osn_loop"]["verification_command"] == {"label": "python -m pytest", "source": "detected"}
    assert entry["verification"]["status"] == "passed"


def test_an_explicit_flag_wins_over_the_contract(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _config(repo, "definitely-not-a-binary-xyz")
    r = _invoke(monkeypatch, repo, "--verify-cmd", CHECK_OK)
    assert r.exit_code == 0, r.output
    assert "(given with --verify-cmd)" in r.output and "OSN loop: verified" in r.output
    assert _last_entry(repo)["osn_loop"]["verification_command"]["source"] == "user"


def test_a_contract_command_the_classifier_would_not_run_silently_is_refused(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _config(repo, "pytest -q && rm -rf build")
    r = _invoke(monkeypatch, repo)
    assert r.exit_code != 0
    assert "is not run on your behalf (blocked" in r.output and "--verify-cmd" in r.output
    assert not ckpt.list_checkpoints(repo)
    # `python -c ...` is arbitrary code: fine when the user types it, never run silently from config.
    _config(repo, CHECK_OK)
    r2 = _invoke(monkeypatch, repo)
    assert r2.exit_code != 0 and "is not run on your behalf" in r2.output
    r3 = _invoke(monkeypatch, repo, "--verify-cmd", CHECK_OK)
    assert r3.exit_code == 0, r3.output
    assert "(given with --verify-cmd)" in r3.output


def test_a_resumed_run_keeps_the_original_command_and_its_source(tmp_path, monkeypatch):
    repo = _repo(tmp_path, with_tests=True)
    _config(repo, CONTRACT)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    first = FakeProvider([_writes("wrong"), KeyboardInterrupt()])
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", first))
    r = CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "a/m", "--escalate-model", "b/m",
                                 "--roles", "executor", "--no-learning"])
    assert r.exit_code != 0 and "Resume with:" in r.output, r.output
    cp = ckpt.list_checkpoints(repo)[0]
    assert cp.args["verify_source"] == "config"
    # The contract changes meanwhile: the resumed run still uses its own command.
    _config(repo, "something-else")
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", FakeProvider([_writes()])))
    r2 = CliRunner().invoke(cli, ["osn", "resume", cp.run_id, "--json"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["status"] == "verified"
    assert _last_entry(repo)["osn_loop"]["verification_command"]["source"] == "config"
