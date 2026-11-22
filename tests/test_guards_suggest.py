"""``openshard guard suggest``: find a person's corrections of an agent's lines in git history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from openshard.guards.suggest import _agent_of, find_corrections


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    import os

    full = {**os.environ, **(env or {})}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=full).stdout.strip()


def _commit(repo: Path, rel: str, text: str, message: str, when: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    stamp = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    _git(repo, "commit", "-q", "-m", message, env=stamp)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


AGENT = "Add proration\n\nOpenshard-Agent: Claude Code"


def test_a_person_fixing_agent_lines_is_a_correction(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "billing.py", "def total(x):\n    return x\n", "start", "2026-11-01T10:00:00Z")
    bad = _commit(repo, "billing.py", "def total(x):\n    return round(x)\n", AGENT, "2026-11-02T10:00:00Z")
    good = _commit(repo, "billing.py", "def total(x):\n    return int(x)\n", "Round down, not to nearest",
                   "2026-11-03T10:00:00Z")
    [c] = find_corrections(repo)
    assert (c.bad, c.good, c.agent, c.evidence) == (bad, good, "Claude Code", "observed")
    assert c.files == ["billing.py"] and c.lines_changed == 1
    assert 0.9 < c.days_after < 1.1


def test_changing_a_persons_own_lines_is_not_a_correction(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "a.py", "x = 1\n", "start", "2026-11-01T10:00:00Z")
    _commit(repo, "b.py", "y = 1\n", AGENT, "2026-11-02T10:00:00Z")
    _commit(repo, "a.py", "x = 2\n", "tweak", "2026-11-03T10:00:00Z")
    assert find_corrections(repo) == []


def test_an_agent_changing_agent_lines_is_not_a_correction(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "a.py", "x = 1\n", "start", "2026-11-01T10:00:00Z")
    _commit(repo, "a.py", "x = 2\n", AGENT, "2026-11-02T10:00:00Z")
    _commit(repo, "a.py", "x = 3\n", "Agent again\n\nOpenshard-Agent: Codex", "2026-11-02T11:00:00Z")
    assert find_corrections(repo) == []


def test_old_agent_lines_outside_the_window_are_not_a_correction(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "a.py", "x = 1\n", "start", "2026-10-01T10:00:00Z")
    _commit(repo, "a.py", "x = 2\n", AGENT, "2026-10-02T10:00:00Z")
    _commit(repo, "a.py", "x = 3\n", "much later", "2026-11-20T10:00:00Z")
    assert find_corrections(repo) == []
    assert len(find_corrections(repo, window_days=60)) == 1


def test_agent_marks_are_read_from_trailers_only():
    assert _agent_of("Openshard-Agent: Cursor") == ("Cursor", "observed")
    assert _agent_of("Claude-Session: https://claude.ai/code/session_x") == ("Claude Code (cloud)", "declared")
    assert _agent_of("Co-Authored-By: Claude <noreply@anthropic.com>") == ("Claude", "declared")
    assert _agent_of("Co-Authored-By: Sam <sam@example.com>") == (None, None)
    assert _agent_of("") == (None, None)


def test_cli_lists_corrections(tmp_path, monkeypatch):
    from openshard.cli.main import cli

    repo = _repo(tmp_path)
    _commit(repo, "billing.py", "v = 1\n", "start", "2026-11-01T10:00:00Z")
    _commit(repo, "billing.py", "v = 2\n", AGENT, "2026-11-02T10:00:00Z")
    good = _commit(repo, "billing.py", "v = 3\n", "Fix the agent's value", "2026-11-03T10:00:00Z")
    monkeypatch.chdir(repo)
    out = CliRunner().invoke(cli, ["guard", "suggest", "--json"])
    assert out.exit_code == 0, out.output
    assert [c["good"] for c in json.loads(out.output)["corrections"]] == [good]
    text = CliRunner().invoke(cli, ["guard", "suggest"]).output
    assert "Fix the agent's value" in text and "guard qualify" in text
