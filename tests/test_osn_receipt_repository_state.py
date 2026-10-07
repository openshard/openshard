"""The OSN Receipt says where the changed files were when it was written: the isolated copy, or the repository.

"Files modified 2" on a run that never touched the repository read as if the
repository changed. The run entry now records whether the verified bytes were
promoted when the Receipt was written; the local projection keeps counts
only, and the compact Receipt says so under the files row. Older Receipts
without the record say nothing rather than guess.
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.receipt_evidence import agent_loop_block, repository_block
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
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
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "out.txt").write_text("bad")
    return repo


def _turn(content="ok"):
    return json.dumps({"actions": [{"kind": "write_file", "path": "out.txt", "content": content, "intent": "w"},
                                   {"kind": "finish", "intent": "done"}], "note": "n"})


def _run(monkeypatch, repo, *args):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", FakeProvider([_turn()])))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd", CHECK,
                                    "--roles", "executor", "--no-learning", *args])


def _last_entry(repo):
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]


def test_an_unpromoted_run_says_its_files_stayed_in_the_isolated_copy(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _run(monkeypatch, repo)
    assert r.exit_code == 0, r.output
    entry = _last_entry(repo)
    assert entry["osn_loop"]["repository"] == {"applied": False, "files_applied": [], "files_skipped": [], "how": None}
    text = render_compact_shard_receipt(build_shard_receipt(entry))
    assert "Files modified  1" in text
    assert "↳ in an isolated copy · not applied when this Receipt was written" in text
    assert (repo / "out.txt").read_text() == "bad"


def test_a_promoted_run_says_its_files_reached_the_repository(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _run(monkeypatch, repo, "--promote", "--json")
    assert r.exit_code == 0, r.output
    entry = _last_entry(repo)
    assert entry["osn_loop"]["repository"] == {"applied": True, "files_applied": ["out.txt"], "files_skipped": [],
                                               "how": "promote"}
    text = render_compact_shard_receipt(build_shard_receipt(entry))
    assert "↳ applied to the repository" in text and "not applied" not in text


def test_the_local_projection_keeps_counts_only_and_older_receipts_say_nothing():
    assert repository_block({"applied": True, "files_applied": ["a.py", "b.py"], "files_skipped": ["c.py"],
                             "how": "promote"}) == {"applied": True, "files_applied": 2, "files_skipped": 1,
                                                    "how": "promote"}
    assert repository_block(None) is None and repository_block({"files_applied": []}) is None
    old_entry = {"osn_loop": {"mode": "turns", "turns_total": 1, "attempts": []}}
    block = agent_loop_block(old_entry)
    assert block is not None and block["repository"] is None
