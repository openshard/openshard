"""The files the planner named as likely to change are shown to the executor on its first turn.

The planner already read them; the executor then spent its own turns
rediscovering them. Existing, small files from the plan (at most four, each
within the context-file size cap, not already supplied) now go into the
first turn's prompt exactly as --context-file does, and the Receipt records
which ones under osn_loop.plan_context_files.
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.osn.agent_loop import TurnState
from openshard.osn.model_provider import (
    MAX_CONTEXT_FILE_BYTES,
    MAX_PLAN_CONTEXT_FILES,
    IterativeModelProvider,
    build_turn_prompt,
    plan_context_files,
)
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.prompts.append(prompt)
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


def test_plan_files_are_filtered_to_existing_small_unsupplied_ones_and_capped(tmp_path):
    for i in range(6):
        (tmp_path / f"f{i}.py").write_text(f"# {i}\n")
    (tmp_path / "big.py").write_bytes(b"#" * (MAX_CONTEXT_FILE_BYTES + 1))
    plan = {"files": ["f0.py", "missing.py", "big.py", "f1.py", "../etc/passwd", "f2.py", "f0.py", "f3.py", "f4.py", "f5.py"]}
    chosen = plan_context_files(plan, tmp_path, already=["f1.py"])
    assert chosen == ["f0.py", "f2.py", "f3.py", "f4.py"]
    assert len(chosen) == MAX_PLAN_CONTEXT_FILES
    assert plan_context_files(None, tmp_path, []) == [] and plan_context_files({"files": []}, tmp_path, []) == []


def test_set_plan_shows_the_files_on_turn_one_and_names_them_later(tmp_path):
    (tmp_path / "slug.py").write_text("def slugify(x):\n    return x\n")
    provider = IterativeModelProvider(FakeProvider([]), ["m"], tmp_path)
    provider.set_plan({"summary": "s", "files": ["slug.py"], "steps": ["edit slug.py"]})
    assert provider.plan_context_files == ["slug.py"]

    def state(turn: int) -> TurnState:
        return TurnState(task="t", attempt=1, turn=turn, max_turns=12, repo_files=["slug.py"], observations=[],
                         changed_files=[], blocked_paths=[], previous_failure=None, verifications_left=2,
                         writes_applied=0)

    first = build_turn_prompt(state(1), tmp_path, [*provider.context_files, *provider.plan_context_files],
                              plan=provider.plan)
    assert '<untrusted file="slug.py">\ndef slugify(x):' in first
    second = build_turn_prompt(state(2), tmp_path, [*provider.context_files, *provider.plan_context_files],
                               plan=provider.plan)
    assert "<untrusted file=" not in second and "Files shown to you on turn 1" in second and "slug.py" in second


def test_the_executor_receives_the_plan_files_and_the_receipt_records_them(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "out.txt").write_text("bad")
    (repo / "notes.md").write_text("the note content\n")
    plan = json.dumps({"plan": {"summary": "fix out", "files": ["out.txt", "notes.md"], "steps": ["write"],
                                "verification": ["ok"], "simple": False}, "actions": [{"kind": "finish"}]})
    executor = json.dumps({"actions": [{"kind": "write_file", "path": "out.txt", "content": "ok", "intent": "w"},
                                       {"kind": "finish", "intent": "done"}], "note": "n"})
    fake = FakeProvider([plan, executor])
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    r = CliRunner().invoke(cli, ["osn", "run", "fix out", "--model", "exec/m", "--verify-cmd", CHECK, "--no-learning",
                                 "--roles", "full", "--planner-model", "plan/m", "--verifier-model", "exec/m", "--json"])
    assert r.exit_code == 0, r.output
    executor_prompt = fake.prompts[1]
    assert '<untrusted file="notes.md">\nthe note content' in executor_prompt and '<untrusted file="out.txt">' in executor_prompt
    entry = [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]
    assert entry["osn_loop"]["plan_context_files"] == ["out.txt", "notes.md"]
