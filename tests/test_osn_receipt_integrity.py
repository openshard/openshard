"""An OSN Receipt's content hash is frozen over the record as written, after every field the run adds.

The run entry is built (and stamped) before the CLI adds what only the run
knows at the end: the verification command's source, the project
instructions supplied, whether the files reached the repository, a resume,
the commit it created. A hash stamped before those fields reads as
"Checksum mismatch (record edited after it was written)" on a record nobody
edited. The hash must be stamped last.
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.shard_hash import verify_shard_hash
from openshard.osn import checkpoint as ckpt
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'


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


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "osn@example.com")
    _git(repo, "config", "user.name", "OSN Test")
    (repo / "out.txt").write_text("bad")
    (repo / "AGENTS.md").write_bytes(b"# Agents\n\nKeep it small.\n")
    (repo / ".gitignore").write_text(".openshard/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _turn(content="ok"):
    return json.dumps({"actions": [{"kind": "write_file", "path": "out.txt", "content": content, "intent": "w"},
                                   {"kind": "finish", "intent": "done"}], "note": "n"})


def _entries(repo):
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()]


def _invoke(monkeypatch, repo, fake, *args):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd", CHECK,
                                    "--roles", "executor", "--no-learning", *args])


def test_a_plain_run_and_a_promoted_committed_run_carry_a_valid_hash(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    assert _invoke(monkeypatch, repo, FakeProvider([_turn()])).exit_code == 0
    assert _invoke(monkeypatch, repo, FakeProvider([_turn()]), "--promote", "--commit", "--json").exit_code == 0
    plain, committed = _entries(repo)
    for entry in (plain, committed):
        assert entry["content_hash"].startswith("sha256:")
        assert verify_shard_hash(entry)["status"] == "valid", entry.get("receipt_id")
    # The fields added at the end of the run are inside the hashed record.
    assert plain["osn_loop"]["repository"]["applied"] is False
    assert plain["osn_loop"]["verification_command"]["source"] == "user"
    assert plain["osn_loop"]["project_instructions"][0]["path"] == "AGENTS.md"
    assert committed["osn_commit"]["sha"] and committed["osn_loop"]["repository"]["applied"] is True


def test_a_resumed_run_carries_a_valid_hash(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    first = FakeProvider([_turn("wrong"), KeyboardInterrupt()])
    r = _invoke(monkeypatch, repo, first, "--escalate-model", "b/m")
    assert r.exit_code != 0 and "Resume with:" in r.output
    cp = ckpt.list_checkpoints(repo)[0]
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", FakeProvider([_turn()])))
    r2 = CliRunner().invoke(cli, ["osn", "resume", cp.run_id, "--json"])
    assert r2.exit_code == 0, r2.output
    entry = _entries(repo)[-1]
    assert entry["osn_loop"]["resumed"]["from_run_id"] == cp.run_id
    assert verify_shard_hash(entry)["status"] == "valid"
