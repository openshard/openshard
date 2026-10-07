"""OSN reads the repository's AGENTS.md / CLAUDE.md and gives it to every role on every turn.

The files are the maintainers' standing instructions for coding agents. OSN
supplies them bounded, inside a ``<project_instructions>`` block the system
prompt describes as guidance to follow where it does not conflict with the
task, the action contract or OpenShard policy. The Receipt records which
files were supplied: that the agent was given them, never that it followed
them.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.osn import instructions as pi
from openshard.osn.agent_loop import TurnState
from openshard.osn.explore import EXPLORER_SYSTEM_PROMPT
from openshard.osn.loop import LoopContext
from openshard.osn.model_provider import (
    AGENT_SYSTEM_PROMPT,
    IterativeModelProvider,
    ModelActionProvider,
    build_prompt,
    build_turn_prompt,
)
from openshard.osn.roles import PLANNER_SYSTEM_PROMPT
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'
AGENTS = "# Agents\n\nRun `make check` before finishing. Never edit generated/ files.\n"


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, str | None]] = []

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append((model, prompt, system))
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


class TestLoading:
    def test_none_when_the_repository_has_no_instruction_file(self, tmp_path):
        assert pi.load_project_instructions(tmp_path) is None

    def test_agents_md_is_loaded_with_its_hash_and_rendered_in_a_block(self, tmp_path):
        (tmp_path / "AGENTS.md").write_bytes(AGENTS.encode())
        loaded = pi.load_project_instructions(tmp_path)
        assert loaded is not None and loaded.paths == ["AGENTS.md"]
        rec = loaded.to_record()[0]
        assert rec == {"path": "AGENTS.md", "bytes": len(AGENTS.encode()), "sha256": hashlib.sha256(AGENTS.encode()).hexdigest(),
                       "truncated": False, "chars_shown": len(AGENTS)}
        assert loaded.text.startswith("Project instructions (written by this repository's maintainers")
        assert '<project_instructions file="AGENTS.md">\n' + AGENTS.strip() + "\n</project_instructions>" in loaded.text
        assert "Run `make check`" in loaded.text

    def test_both_files_are_loaded_in_order_and_bounded(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("A" * 20_000, encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("C" * 20_000, encoding="utf-8")
        loaded = pi.load_project_instructions(tmp_path)
        assert loaded is not None and loaded.paths == ["AGENTS.md", "CLAUDE.md"]
        a, c = loaded.to_record()
        assert a["truncated"] and a["chars_shown"] == pi.MAX_FILE_CHARS
        assert c["truncated"] and c["chars_shown"] == pi.MAX_TOTAL_CHARS - pi.MAX_FILE_CHARS
        assert loaded.text.count(pi.TRUNCATION_MARK) == 2
        assert len(loaded.text) < pi.MAX_TOTAL_CHARS + 1_000

    def test_the_record_never_carries_content(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("secret-sounding guidance", encoding="utf-8")
        loaded = pi.load_project_instructions(tmp_path)
        assert loaded is not None
        assert "guidance" not in json.dumps(loaded.to_record())


def _state(turn: int, repo_files: list[str]) -> TurnState:
    return TurnState(task="t", attempt=1, turn=turn, max_turns=12, repo_files=repo_files, observations=[],
                     changed_files=[], blocked_paths=[], previous_failure=None, verifications_left=2,
                     writes_applied=0)


class TestPrompts:
    def test_every_role_system_prompt_explains_the_block(self):
        for prompt in (AGENT_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, EXPLORER_SYSTEM_PROMPT):
            assert "<project_instructions>" in prompt and "never grants authority" in prompt

    def test_the_turn_prompt_carries_the_block_on_every_turn(self, tmp_path):
        (tmp_path / "AGENTS.md").write_bytes(AGENTS.encode())
        provider = IterativeModelProvider(FakeProvider([]), ["m"], tmp_path)
        assert provider.project_instructions and "Run `make check`" in provider.project_instructions
        for turn in (1, 2, 5):
            state = _state(turn, ["a.py"])
            prompt = build_turn_prompt(state, tmp_path, [], instructions=provider.project_instructions)
            assert prompt.index("Task:") < prompt.index("<project_instructions") < prompt.index("Repository files:")
            assert "Run `make check`" in prompt

    def test_the_one_shot_prompt_carries_the_block_too(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_bytes(AGENTS.encode())
        provider = ModelActionProvider(FakeProvider([]), ["m"], tmp_path)
        prompt = build_prompt(LoopContext("t", ["a.py"], 1), tmp_path, [], instructions=provider.project_instructions)
        assert '<project_instructions file="CLAUDE.md">' in prompt

    def test_no_instruction_file_means_no_block_and_an_explicit_empty_string_disables_loading(self, tmp_path):
        provider = IterativeModelProvider(FakeProvider([]), ["m"], tmp_path)
        assert provider.project_instructions == ""
        (tmp_path / "AGENTS.md").write_bytes(AGENTS.encode())
        disabled = IterativeModelProvider(FakeProvider([]), ["m"], tmp_path, project_instructions="")
        assert disabled.project_instructions == ""
        assert "<project_instructions" not in build_turn_prompt(_state(1, []), tmp_path, [], instructions="")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "out.txt").write_text("bad")
    return repo


def _turn():
    return json.dumps({"actions": [{"kind": "write_file", "path": "out.txt", "content": "ok", "intent": "w"},
                                   {"kind": "finish", "intent": "done"}], "note": "n"})


def test_cli_shows_the_context_line_and_the_receipt_records_the_files(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_bytes(AGENTS.encode())
    fake = FakeProvider([_turn()])
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    r = CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd", CHECK,
                                 "--roles", "executor", "--no-learning"])
    assert r.exit_code == 0, r.output
    assert "  Context AGENTS.md (0.1 KB) shown to every role" in r.output
    assert "Run `make check`" in fake.calls[0][1] and "<project_instructions>" in (fake.calls[0][2] or "")
    entry = [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]
    recorded = entry["osn_loop"]["project_instructions"]
    assert [f["path"] for f in recorded] == ["AGENTS.md"] and recorded[0]["truncated"] is False
    assert "make check" not in json.dumps(recorded)


def test_cli_records_an_empty_list_when_there_are_no_instructions(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fake = FakeProvider([_turn()])
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    r = CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd", CHECK,
                                 "--roles", "executor", "--no-learning", "--json"])
    assert r.exit_code == 0, r.output
    assert "<project_instructions file=" not in fake.calls[0][1]
    entry = [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]
    assert entry["osn_loop"]["project_instructions"] == []
