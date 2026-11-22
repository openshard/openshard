"""``openshard guard qualify``: a guard must fail on the mistake, pass on the fix, and be stable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openshard.guards import qualify as gq
from openshard.guards.model import load_guards, parse_guard

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs real pytest subprocesses in worktrees")

GUARD = """\
# Written after we corrected an agent that made add() return floats.
id: add-is-int
version: 1
status: proposed
kind: test
title: add() returns an integer for integer inputs
scope:
  paths: ["calc.py"]
check:
  command: "python -m pytest tests/guards/test_add_is_int.py -q -p no:cacheprovider"
"""

GUARD_TEST = "from calc import add\n\n\ndef test_int():\n    assert isinstance(add(2, 3), int)\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path, monkeypatch):
    """good0 (int) -> bad (agent makes it float) -> good (person corrects it); guard written afterwards."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, ".gitignore", ".openshard/*\n!.openshard/guards/\n__pycache__/\n")
    _write(repo, "pyproject.toml", "[tool.pytest.ini_options]\npythonpath = [\".\"]\n")
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    _commit(repo, "start")
    _write(repo, "calc.py", "def add(a, b):\n    return float(a + b)\n")
    bad = _commit(repo, "agent: return float")
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    good = _commit(repo, "person: correct it")
    # The guard and its test exist only in the working tree, as when they are first written.
    _write(repo, "tests/guards/test_add_is_int.py", GUARD_TEST)
    _write(repo, ".openshard/guards/add-is-int.yml", GUARD)
    monkeypatch.chdir(repo)
    return repo, bad, good


def _guard(repo: Path):
    return load_guards(repo).by_id()["add-is-int"]


def test_a_guard_that_catches_the_mistake_is_qualified(history):
    repo, bad, good = history
    q = gq.qualify_guard(repo, _guard(repo), bad, good, runs=2)
    assert q.verdict == gq.QUALIFIED, q.to_dict()
    assert [(r.where, r.status) for r in q.runs] == [
        ("bad", "failed"), ("good", "passed"), ("stable-1", "passed"), ("stable-2", "passed")]
    assert q.stable_runs == 2
    record = q.to_record(at="2026-11-22T00:00:00Z")
    assert (record["failed_on"], record["passed_on"]) == (bad, good)


def test_a_guard_that_does_not_catch_the_mistake_is_not(history):
    repo, bad, good = history
    _write(repo, "tests/guards/test_add_is_int.py",
           "from calc import add\n\n\ndef test_int():\n    assert add(2, 3) == 5\n")
    q = gq.qualify_guard(repo, _guard(repo), bad, good, runs=1)
    assert q.verdict == gq.PASSES_ON_BAD
    assert [r.where for r in q.runs] == ["bad"]


def test_a_guard_that_fails_on_the_fix_is_not(history):
    repo, bad, good = history
    _write(repo, "tests/guards/test_add_is_int.py",
           "from calc import add\n\n\ndef test_int():\n    assert add(2, 3) == 6\n")
    q = gq.qualify_guard(repo, _guard(repo), bad, good, runs=1)
    assert q.verdict == gq.FAILS_ON_GOOD


def test_failing_only_because_the_code_is_missing_does_not_count(history):
    repo, bad, good = history
    _write(repo, "tests/guards/test_add_is_int.py",
           "from calc import add_exact\n\n\ndef test_int():\n    assert add_exact(2, 3) == 5\n")
    q = gq.qualify_guard(repo, _guard(repo), bad, good, runs=1)
    assert q.verdict == gq.CANNOT_LOAD_ON_BAD
    assert q.runs[0].exit_code == 2


def test_a_flaky_guard_is_caught_on_the_working_tree(history):
    repo, bad, good = history
    calls = {"n": 0}
    codes = [1, 0, 0, 1]  # bad fails, good passes, stable-1 passes, stable-2 fails

    def runner(argv, **_kw):
        code = codes[calls["n"]]
        calls["n"] += 1
        return SimpleNamespace(returncode=code, stdout="")

    q = gq.qualify_guard(repo, _guard(repo), bad, good, runs=3, runner=runner)
    assert q.verdict == gq.FLAKY
    assert "stability run 2" in q.reason


def test_a_command_needing_approval_does_not_run_without_it(history):
    repo, bad, good = history
    guard = parse_guard(GUARD.replace("python -m pytest tests/guards/test_add_is_int.py -q -p no:cacheprovider",
                                      "python check.py"), ".openshard/guards/add-is-int.yml")
    q = gq.qualify_guard(repo, guard, bad, good)
    assert q.verdict == gq.COULD_NOT_RUN
    assert "approval" in q.reason


def test_writing_the_result_replaces_the_old_block_and_keeps_comments(tmp_path):
    path = tmp_path / "g.yml"
    path.write_text(GUARD + "qualification:\n  verdict: flaky\n  stable_runs: 0\n")
    gq.write_qualification(path, {"verdict": "qualified", "stable_runs": 3}, activate=True)
    text = path.read_text()
    assert text.startswith("# Written after we corrected")
    assert "status: active" in text and "status: proposed" not in text
    assert text.count("qualification:") == 1 and "verdict: qualified" in text and "flaky" not in text
    parsed = parse_guard(text, ".openshard/guards/add-is-int.yml")
    assert parsed.qualification["verdict"] == "qualified"


def test_cli_writes_the_result_and_activates(history):
    from openshard.cli.main import cli

    repo, bad, good = history
    result = CliRunner().invoke(cli, ["guard", "qualify", "add-is-int", "--bad", bad, "--good", good,
                                      "--runs", "1", "--activate", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["verdict"] == "qualified"
    guard = _guard(repo)
    assert guard.status == "active"
    assert guard.qualification["failed_on"] == bad
    assert guard.qualification["method"] == "openshard guard qualify v1"


def test_cli_without_activate_leaves_the_status(history):
    from openshard.cli.main import cli

    repo, bad, good = history
    result = CliRunner().invoke(cli, ["guard", "qualify", "add-is-int", "--bad", bad, "--good", good, "--runs", "1"])
    assert result.exit_code == 0, result.output
    assert "Set 'status: active'" in result.output
    assert _guard(repo).status == "proposed"


def test_cli_exit_code_when_not_qualified(history):
    from openshard.cli.main import cli

    repo, bad, good = history
    result = CliRunner().invoke(cli, ["guard", "qualify", "add-is-int", "--bad", good, "--good", good, "--runs", "1"])
    assert result.exit_code == 1, result.output
    assert "passes_on_bad" in result.output
