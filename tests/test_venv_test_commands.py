"""Test runs through a project virtualenv, and pytest's summary line as the outcome of a piped run.

From a Windows dogfood session: ``.venv\\Scripts\\python -m pytest`` ran 510
passing tests but the Receipt recorded its checks as unknown. A pytest run
whose exit status a pipe hides (``| tail -5``) takes its outcome from its own
summary line; anything else stays unknown.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openshard.adapters.claude_hook_checks import hook_verification, record_checks
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.verification import check_command as cc
from tests.capture_fixtures import _make_repo

SID = "venv-checks-session"


@pytest.mark.parametrize("command, shell", [
    (".venv/bin/python -m pytest", cc.SHELL_POSIX),
    (".venv\\Scripts\\python -m pytest", cc.SHELL_POSIX),
    (".venv\\Scripts\\python.exe -m pytest", cc.SHELL_POSIX),
    (".venv\\Scripts\\python.exe -m pytest", cc.SHELL_POWERSHELL),
    ("& .venv\\Scripts\\python.exe -m pytest", cc.SHELL_POWERSHELL),
    ("venv/Scripts/python -m pytest -q", cc.SHELL_POSIX),
    ("C:\\proj\\.venv\\Scripts\\python.exe -m pytest", cc.SHELL_POWERSHELL),
    ('"C:\\My Proj\\.venv\\Scripts\\python.exe" -m pytest', cc.SHELL_POSIX),
    ('& "C:\\My Proj\\.venv\\Scripts\\python.exe" -m pytest', cc.SHELL_POWERSHELL),
    ('"/home/a b/.venv/bin/python" -m pytest', cc.SHELL_POSIX),
    ("python3.12 -m pytest", cc.SHELL_POSIX),
    ("python3.12.exe -m pytest", cc.SHELL_POWERSHELL),
    ("PYTHON.EXE -m pytest", cc.SHELL_POWERSHELL),
    ("py -m pytest", cc.SHELL_POWERSHELL),
    ("uv run pytest", cc.SHELL_POSIX),
    ("poetry run pytest", cc.SHELL_POSIX),
    ("uv run python -m pytest", cc.SHELL_POSIX),
    (".venv\\Scripts\\pytest.exe -q", cc.SHELL_POWERSHELL),
    (".venv/bin/python -m unittest discover", cc.SHELL_POSIX),
    ("C:\\x\\.venv\\Scripts\\python.exe -m unittest", cc.SHELL_POWERSHELL),
    ("cd /c/proj && .venv/Scripts/python -m pytest -q", cc.SHELL_POSIX),
    ("source .venv/bin/activate && python -m pytest", cc.SHELL_POSIX),
    ("cd C:\\proj; .venv\\Scripts\\python.exe -m pytest", cc.SHELL_POWERSHELL),
])
def test_interpreter_paths_running_tests_are_attributable_test_checks(command, shell):
    checks = cc.classify_check_commands(command, shell=shell)
    assert [c.kind for c in checks] == [cc.KIND_TEST]
    assert checks[0].unattributed is None  # the line's exit status is the test run's
    assert "\\" not in checks[0].segment and "/" not in checks[0].segment.split()[0]


def test_only_the_test_part_of_a_chain_is_the_check():
    checks = cc.classify_check_commands("cd /c/proj && git status && .venv/Scripts/python -m pytest -q; echo done")
    assert [(c.kind, c.segment) for c in checks] == [(cc.KIND_TEST, "python -m pytest -q")]
    assert checks[0].unattributed == cc.UNATTRIBUTED_NOT_LAST  # ``echo`` decides the exit status
    assert checks[0].summary_readable is False  # ``echo`` could print a summary line


@pytest.mark.parametrize("command", [
    ".venv\\Scripts\\python -m pytest tests/ -q 2>&1 | tail -5",
    "cd /c/proj && .venv/Scripts/python -m pytest -q 2>&1 | tail -n 20",
    ".venv/bin/python -m pytest | head -50",
    "pytest -q | tee pytest.log | tail -1",
    "pytest -q 2>&1 | tail -3; cd ..",
])
def test_piped_pytest_runs_may_take_their_summary_line(command):
    (check,) = cc.classify_check_commands(command)
    assert check.unattributed in (cc.UNATTRIBUTED_PIPED, cc.UNATTRIBUTED_NOT_LAST)
    assert check.summary_readable is True


@pytest.mark.parametrize("command, shell", [
    ("pytest | tail -5 old.log", cc.SHELL_POSIX),  # tail reads another file
    ("pytest | tail -c 30", cc.SHELL_POSIX),  # bytes can cut a line to a passing tail
    ("pytest | grep -o 'passed in'", cc.SHELL_POSIX),
    ("pytest | sed s/failed/passed/", cc.SHELL_POSIX),
    ("pytest -q | tail -3; echo '1 passed in 1s'", cc.SHELL_POSIX),
    ("pytest || true", cc.SHELL_POSIX),
    ("pytest & tail -f log", cc.SHELL_POSIX),
    ("if pytest | tail; then :; fi", cc.SHELL_POSIX),
    ("pytest; ruff check . | tail", cc.SHELL_POSIX),
    ("bash -lc 'pytest | tail -3'", cc.SHELL_POSIX),
    ("python -m unittest | tail -5", cc.SHELL_POSIX),
])
def test_lines_where_the_summary_may_not_be_pytests_own_are_not_readable(command, shell):
    assert not any(c.summary_readable for c in cc.classify_check_commands(command, shell=shell))


@pytest.mark.parametrize("output, outcome", [
    ("....\n====== 510 passed in 12.31s ======\n", "passed"),
    ("510 passed, 3 warnings in 1.20s", "passed"),
    ("======= 510 passed in 75.42s (0:01:15) =======", "passed"),
    ("5 skipped in 0.20s", "passed"),
    ("1 failed, 509 passed in 3.00s", "failed"),
    ("= 2 errors in 0.10s =", "failed"),
    ("1 error in 0.50s", "failed"),
    ("no tests ran in 0.01s", "failed"),  # pytest exits 5
    ("3 deselected in 0.10s", "failed"),
    ("!!!!!! KeyboardInterrupt !!!!!!\n==== 2 passed in 1.00s ====", None),
    ("!!! Interrupted: 1 error during collection !!!\n1 error in 0.3s", None),
    ("tests collected", None),
    ("510 tests collected in 0.50s", None),
    ("", None),
    (None, None),
    ({"stdout": "1 passed in 1s"}, None),
])
def test_pytest_summary_outcome(output, outcome):
    assert cc.pytest_summary_outcome(output) == outcome


def test_the_last_summary_line_decides():
    assert cc.pytest_summary_outcome("1 failed in 1s\n...\n3 passed in 2s") == "passed"
    assert cc.pytest_summary_outcome("3 passed in 2s\n1 failed in 1s") == "failed"


def _items(command: str, summary: str | None) -> list[dict]:
    return cc.queued_check_commands(cc.check_command_items(command, lambda seg, k: seg, test_summary=summary))


@pytest.mark.parametrize("summary, command_outcome, expected", [
    ("passed", "passed", "passed"),
    ("failed", "passed", "failed"),
    ("failed", "failed", "failed"),
    ("passed", "failed", "unknown"),  # the pipeline failed: never a guessed pass
    ("passed", "unknown", "unknown"),  # interrupted / background: no result
    ("failed", "unknown", "unknown"),
    (None, "passed", "unknown"),  # no summary line in the output
])
def test_piped_run_outcome_comes_from_its_summary_line(summary, command_outcome, expected):
    (item,) = _items(".venv\\Scripts\\python -m pytest -q 2>&1 | tail -5", summary)
    status, reason = cc.item_outcome(item, command_outcome)
    assert status == expected
    if expected == "unknown" and command_outcome in ("passed", "failed"):
        assert reason == cc.UNATTRIBUTED_PIPED


def test_summary_is_ignored_where_the_line_could_fake_it():
    (item,) = _items("pytest -q | tail -3; echo '1 passed in 1s'", "passed")
    assert "summary_outcome" not in item
    assert cc.item_outcome(item, "passed")[0] == "unknown"
    (item,) = _items("pytest -q", "failed")
    assert "summary_outcome" not in item  # attributable run: its exit status decides
    assert cc.item_outcome(item, "passed")[0] == "passed"


def test_summary_outcome_is_not_upgraded_from_a_forged_queue_line():
    (item,) = cc.queued_check_commands([{"kind": "test", "name": "pytest", "summary_outcome": "green"}])
    assert "summary_outcome" not in item


class _Payload:
    def __init__(self, items, exit_code=None):
        self.check_commands, self.command_exit_code = items, exit_code
        self.command_kind, self.command_key = "test", "k" * 16


def test_a_summary_outcome_records_no_exit_code_of_its_own():
    buf: dict = {}
    record_checks(buf, _Payload(_items("pytest -q | tail -3", "passed"), exit_code=0), "Ran tests", "passed", "t")
    assert buf["checks"][0]["status"] == "passed" and "exit_code" not in buf["checks"][0]
    block = hook_verification(buf, lost=False)
    assert block["status"] == "passed" and block["checks_passed"] == 1
    assert block["source"] == "agent_reported"  # the agent's tool output, never OpenShard's own run


# -- through the Claude hook -----------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "widget")


def _hook(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _verification(repo: Path) -> dict:
    path = repo / ".openshard" / "runs.jsonl"
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return entries[-1]["verification"]


def test_dogfood_windows_venv_runs_are_recorded_with_their_outcome(repo):
    _hook(repo, "UserPromptSubmit", prompt="fix the bug")
    for command in (".venv\\Scripts\\python -m pytest",
                    ".venv/Scripts/python -m pytest tests/test_x.py -q 2>&1 | tail -5",
                    'cd "C:\\My Proj" && .venv/Scripts/python.exe -m pytest -q | tail -n 3'):
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": command},
              tool_response={"stdout": "SECRET-OUTPUT\n510 passed in 41.20s\n", "interrupted": False})
    _hook(repo, "Stop")
    block = _verification(repo)
    assert block["status"] == "passed"
    assert block["checks_attempted"] == 3 and block["checks_passed"] == 3
    assert "outcome_not_observed" not in (block.get("incomplete_reasons") or [])
    assert "SECRET-OUTPUT" not in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")


def test_piped_run_with_a_failing_summary_is_failed(repo):
    _hook(repo, "UserPromptSubmit", prompt="fix the bug")
    _hook(repo, "PostToolUse", tool_name="PowerShell",
          tool_input={"command": "cd C:\\proj; .venv\\Scripts\\python.exe -m pytest -q; git status"},
          tool_response={"stdout": "2 failed, 508 passed in 40.0s\n"})
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "pytest -q | tail -3"},
          tool_response={"stdout": "=== 2 failed, 508 passed in 40.0s ===\n"})
    _hook(repo, "Stop")
    block = _verification(repo)
    # The PowerShell line ends with another native program: its output is not
    # only pytest's, so that run stays unknown; the piped bash run is failed.
    assert block["status"] == "failed" and block["checks_failed"] == 1
    assert [c["status"] for c in block["checks"]] == ["unknown", "failed"]


def test_piped_run_without_output_stays_unknown(repo):
    _hook(repo, "UserPromptSubmit", prompt="fix the bug")
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": ".venv/bin/python -m pytest | tail -5"},
          tool_response={"stdout": "", "interrupted": False})
    _hook(repo, "Stop")
    block = _verification(repo)
    assert block["status"] == "unknown" and block["checks_passed"] == 0
    assert "outcome_not_observed" in block["incomplete_reasons"]
