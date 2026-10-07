"""The compact OSN Receipt's COST section names what its first row covers.

The first row is the run's recorded cost minus the retries', so when the
planner or verifier made calls it holds their spend too. Labelling it with
the executor's model credited that model with everyone's cost. With more
than one active role the row now reads 'Attempt 1 · all roles (...)'; with
only the executor it keeps the model name. ROLES carries the per-role split.
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'


class FakeProvider(BaseProvider):
    """Replies in order; every call reports a cost so the Receipt has a COST section."""

    def __init__(self, replies):
        self.replies = list(replies)

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        return ChatResponse(self.replies.pop(0), model, UsageStats(100, 20, 120, 0.01, cost_source="provider_reported"))


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "out.txt").write_text("bad")
    return repo


def _turn(*actions):
    return json.dumps({"actions": list(actions), "note": "n"})


def _write():
    return {"kind": "write_file", "path": "out.txt", "content": "ok", "intent": "w"}


def _finish():
    return {"kind": "finish", "intent": "done"}


def _run(monkeypatch, repo, fake, *args):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    r = CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "exec/m", "--verify-cmd", CHECK,
                                 "--no-learning", "--json", *args])
    assert r.exit_code == 0, r.output
    entry = [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]
    return render_compact_shard_receipt(build_shard_receipt(entry))


def _cost_section(text: str) -> str:
    assert "COST" in text, text
    return text.split("COST", 1)[1].split("PROOF", 1)[0]


def test_executor_only_keeps_the_model_name(tmp_path, monkeypatch):
    text = _run(monkeypatch, _repo(tmp_path), FakeProvider([_turn(_write(), _finish())]), "--roles", "executor")
    cost = _cost_section(text)
    assert "all roles" not in cost and "$0.0100" in cost


def test_planner_executor_and_verifier_label_the_row_all_roles(tmp_path, monkeypatch):
    plan = json.dumps({"plan": {"summary": "write it", "files": ["out.txt"], "steps": ["write"], "verification": ["ok"],
                                "simple": False}, "actions": [{"kind": "finish"}]})
    review = json.dumps({"verdict": "pass", "summary": "fine", "concerns": []})
    fake = FakeProvider([plan, _turn(_write(), _finish()), review])
    text = _run(monkeypatch, _repo(tmp_path), fake, "--roles", "full", "--planner-model", "plan/m",
                "--verifier-model", "review/m")
    cost = _cost_section(text)
    row = next(line for line in cost.splitlines() if "Attempt 1" in line)
    assert row.strip().startswith("Attempt 1 · all roles") and row.rstrip().endswith("$0.0300")  # one aligned row
    assert "ROLES" in text and "Planner" in text and "Verifier" in text
