"""vitest's closing summary as the outcome of a piped vitest run.

From dogfood Receipts: ``npx vitest run 2>&1 | tail -8`` ran the suite, but
the pipe hides vitest's exit status, so the check was recorded unknown. As
with pytest (tests/test_venv_test_commands.py), a direct vitest run whose
exit status only a line filter hides takes its outcome from its own summary
block. A pass is read only when that block ends the output; anything else
stays unknown.

The outputs below are real vitest 5.0.1 output (``| tail -8``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.verification import check_command as cc
from tests.capture_fixtures import _make_repo

PASS = """ RUN  v5.0.1 /home/claude/platform/apps/web


 Test Files  1 passed (1)
      Tests  1 passed | 1 skipped (2)
   Start at  01:38:58
   Duration  220ms (transform 54%, import 27%, worker 10%, tests 7%)
"""
FAIL = """

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 1 passed | 1 skipped (3)
   Start at  01:39:00
   Duration  468ms (transform 42%, import 29%, tests 16%, worker 11%)
"""
# Every test passed, but an unhandled error still makes vitest exit 1.
UNHANDLED_ERROR = """⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯


 Test Files  1 passed (1)
      Tests  2 passed (2)
     Errors  1 error
   Start at  01:39:09
   Duration  258ms (transform 44%, tests 28%, import 22%, worker 6%)
"""
NO_TESTS = """ RUN  v5.0.1 /home/claude/platform/apps/web

No test files found, exiting with code 1

filter: test/zz_nonexistent
"""
COLOR = ("\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
         "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
         "\x1b[2m   Start at \x1b[22m 01:39:02\n"
         "\x1b[2m   Duration \x1b[22m 233ms\x1b[2m (transform 68%)\x1b[22m\n")
# A coverage threshold can fail the run after a passing summary.
COVERAGE_AFTER = PASS + " % Coverage report from v8\nERROR: Coverage for lines (50%) does not meet global threshold (80%)\n"


@pytest.mark.parametrize("output, outcome", [
    (PASS, "passed"),
    (COLOR, "passed"),
    (FAIL, "failed"),
    (UNHANDLED_ERROR, "failed"),
    (FAIL + " % Coverage report from v8\n", "failed"),  # a failure is a failure whatever follows
    (NO_TESTS, None),  # exit status depends on --passWithNoTests: unknown
    (COVERAGE_AFTER, None),  # something printed after the summary: never a guessed pass
    (" Test Files  2 skipped (2)\n      Tests  3 skipped (3)\n   Duration  1s\n", None),
    (" Test Files  1 passed (1)\n      Tests  1 passed (1)\n", None),  # cut off before Duration
    ("Cancelling test run. Press CTRL+c again to exit forcefully.\n" + PASS, None),
    ("", None),
    (None, None),
])
def test_vitest_summary_outcome(output, outcome):
    assert cc.vitest_summary_outcome(output) == outcome


@pytest.mark.parametrize("command", [
    "npx vitest run 2>&1 | tail -8",
    "npx -y vitest run | tail -8",
    "vitest run | tail -5",
    "pnpm vitest run test/a.test.ts | tail -20",
    "corepack pnpm vitest run 2>&1 | tail -8",
    "pnpm exec vitest run | head -50",
    "npx vitest run 2>&1 | tail -8; cd ..",
])
def test_direct_piped_vitest_runs_read_their_summary(command):
    (check,) = cc.classify_check_commands(command)
    assert check.summary_readable is True and check.summary_runner == "vitest"


@pytest.mark.parametrize("command", [
    "pnpm test 2>&1 | tail -8",  # a script: may run several suites, each with a summary
    "pnpm -r vitest run | tail",  # several packages
    "pnpm --filter web vitest run | tail",
    "npx vitest run | grep -E 'FAIL|Tests'",  # grep can drop the failing lines
    "npx vitest run | tail -8; echo ' Test Files  1 passed (1)'",
    "npx vitest run || true",
    "npx vitest run",  # attributable: its exit status decides, not its summary
])
def test_other_vitest_lines_do_not_read_a_summary(command):
    assert cc.runner_summary_outcome(command, PASS) is None


def test_each_runner_is_read_in_its_own_format():
    assert cc.runner_summary_outcome("npx vitest run | tail -8", "=== 3 passed in 1.0s ===") is None
    assert cc.runner_summary_outcome("pytest -q | tail -3", PASS) is None
    assert cc.runner_summary_outcome("pytest -q | tail -3", "3 passed in 1.0s") == "passed"


# -- through the Claude hook -----------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "widget")


def _hook(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": "vitest-summary-session", "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _verification(repo: Path) -> dict:
    lines = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()][-1]["verification"]


def test_a_piped_passing_vitest_run_is_recorded_passed_as_agent_reported(repo):
    _hook(repo, "UserPromptSubmit", prompt="fix the chart")
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "npx vitest run 2>&1 | tail -8"},
          tool_response={"stdout": "SECRET-OUTPUT\n" + PASS, "interrupted": False})
    _hook(repo, "Stop")
    block = _verification(repo)
    assert (block["status"], block["checks_passed"]) == ("passed", 1)
    assert block["source"] == "agent_reported"  # the agent's tool output, never OpenShard's own run
    assert "outcome_not_observed" not in (block.get("incomplete_reasons") or [])
    assert "SECRET-OUTPUT" not in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")


def test_a_piped_vitest_run_with_an_unhandled_error_is_failed(repo):
    _hook(repo, "UserPromptSubmit", prompt="fix the chart")
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "npx vitest run 2>&1 | tail -8"},
          tool_response={"stdout": UNHANDLED_ERROR, "interrupted": False})
    _hook(repo, "Stop")
    block = _verification(repo)
    assert (block["status"], block["checks_failed"]) == ("failed", 1)


def test_pnpm_test_piped_stays_unknown(repo):
    _hook(repo, "UserPromptSubmit", prompt="fix the chart")
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "corepack pnpm test 2>&1 | tail -8"},
          tool_response={"stdout": PASS, "interrupted": False})
    _hook(repo, "Stop")
    block = _verification(repo)
    assert block["status"] == "unknown" and "outcome_not_observed" in block["incomplete_reasons"]
