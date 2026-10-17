"""Agent-reported hook checks are judged by their last run.

A failed check that is re-run and passes ends passed; so does a failed
*narrow* run (one test file, a ``-k`` filter) of a test runner that a later
*full-suite* run of the same runner, in the same directory, passes. The
earlier failure always stays in the evidence history (``checks`` and
``capture.check_runs``). A failure nothing later re-ran stays a failure.
See ``verification/check_command.py`` ``suite_supersedes`` for the rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.receipt_render import render_compact_shard_receipt, render_full_shard_receipt
from openshard.history.shard_contract import build_shard_receipt, checks_label, verified_label
from openshard.history.verification_truth import replaced_failures_phrase
from openshard.verification.check_command import classify_check_commands, suite_supersedes
from tests.capture_fixtures import _make_repo

SID = "11111111-2222-4333-8444-666666666666"
NARROW = "npx vitest run tests/deduplicator-edge-cases.test.ts"


def _hook(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _ran(repo: Path, command: str, *, ok: bool) -> None:
    if ok:
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": command})
    else:
        _hook(repo, "PostToolUseFailure", tool_name="Bash", tool_input={"command": command},
              error="Exit code 1\n1 passed, 1 failed")


def _session(repo: Path, *runs: tuple[str, bool]) -> dict:
    _hook(repo, "UserPromptSubmit", prompt="fix the deduplicator")
    for command, ok in runs:
        _ran(repo, command, ok=ok)
    _hook(repo, "Stop")
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()][-1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "widget")


class TestLastRunDecides:
    def test_same_command_fail_then_pass_is_passed_and_keeps_both_runs(self, repo):
        entry = _session(repo, (NARROW, False), (NARROW, True))
        block = entry["verification"]
        assert block["status"] == "passed" and block["source"] == "agent_reported"
        assert [c["status"] for c in block["checks"]] == ["failed", "passed"]
        assert entry["capture"]["check_runs"] == {"passed": 1, "failed": 1}

    def test_narrow_vitest_failure_superseded_by_later_full_npm_test_pass(self, repo):
        entry = _session(repo, (NARROW, False), ("npm run test", True))
        block = entry["verification"]
        assert block["status"] == "passed" and block["source"] == "agent_reported"
        assert block["checks_failed"] == 0
        # The failed narrow run stays in the evidence history, in order.
        assert [(c["name"], c["status"]) for c in block["checks"]] == [
            (f"Bash: {NARROW}", "failed"), ("Bash: npm run test", "passed"),
        ]
        assert entry["capture"]["check_runs"] == {"passed": 1, "failed": 1}
        assert "1 failed check run(s)" in block["reason"]
        assert "later full-suite run" in block["reason"]
        receipt = build_shard_receipt(entry)
        assert receipt.verification["status"] == "passed"
        label = checks_label(receipt)
        assert "failed" not in label and label.endswith("(agent-reported)")

    def test_narrow_pytest_failure_superseded_by_full_pytest_pass(self, repo):
        entry = _session(repo, ("pytest tests/test_x.py -k dedupe", False), ("python -m pytest -q", True))
        assert entry["verification"]["status"] == "passed"

    def test_fail_with_no_later_run_stays_failed(self, repo):
        entry = _session(repo, (NARROW, False))
        block = entry["verification"]
        assert block["status"] == "failed" and block["checks_failed"] == 1

    def test_pass_then_fail_is_failed(self, repo):
        entry = _session(repo, ("npm run test", True), (NARROW, False))
        block = entry["verification"]
        assert block["status"] == "failed" and block["checks_failed"] == 1

    def test_failure_after_the_full_run_is_not_hidden(self, repo):
        entry = _session(repo, (NARROW, False), ("npm run test", True), (NARROW, False))
        assert entry["verification"]["status"] == "failed"


class TestConservativeSupersede:
    @pytest.mark.parametrize("later", [
        "npx vitest run tests/other.test.ts",  # another narrow run proves nothing about this file
        "npm run test -- tests/deduplicator-edge-cases.test.ts",  # a narrowed script run
        "python -m pytest -q",  # a different runner
        "npx jest",  # a different JS runner
        "npm run test:unit",  # a different script
        "cd packages/web && npm run test",  # possibly another package
        "npm --prefix packages/web test",
        "npm run test | tail -5",  # pass not attributable to the tests
    ])
    def test_later_pass_that_is_not_a_full_run_of_the_same_suite_keeps_failed(self, repo, later):
        entry = _session(repo, (NARROW, False), (later, True))
        assert entry["verification"]["status"] == "failed"

    def test_a_failed_full_run_is_never_superseded_by_a_narrower_spelling(self, repo):
        entry = _session(repo, ("npm run test", False), ("npx vitest run", True))
        assert entry["verification"]["status"] == "failed"

    def test_another_directory_does_not_supersede(self, repo, tmp_path):
        _hook(repo, "UserPromptSubmit", prompt="task")
        _ran(repo, NARROW, ok=False)
        handle_claude_hook(
            {"session_id": SID, "cwd": str(repo / "packages"), "hook_event_name": "PostToolUse",
             "tool_name": "Bash", "tool_input": {"command": "npm run test"}},
            env={"CLAUDE_PROJECT_DIR": str(repo)},
        )
        _hook(repo, "Stop")
        path = repo / ".openshard" / "runs.jsonl"
        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["verification"]["status"] == "failed"

    def test_no_command_text_or_cwd_is_persisted(self, repo):
        entry = _session(repo, (NARROW, False), ("npm run test", True))
        capture = json.dumps(entry["capture"])
        assert str(repo) not in capture and "vitest" not in capture


class TestSuiteClassification:
    @pytest.mark.parametrize("command,suite", [
        ("npx vitest run tests/a.test.ts", "vitest:narrow"),
        ("npx vitest run", "vitest:full"),
        ("vitest --run", "vitest:full"),
        ("pnpm exec vitest run -t dedupe", "vitest:narrow"),
        ("npm run test", "pm_test:full"),
        ("npm test", "pm_test:full"),
        ("yarn test --silent", "pm_test:full"),
        ("npm run test -- a.test.ts", "pm_test:narrow"),
        ("pytest", "pytest:full"),
        ("uv run pytest -q -x", "pytest:full"),
        ("python -m pytest tests/test_a.py", "pytest:narrow"),
        ("pytest -n auto", "pytest:narrow"),  # unknown argument: never assumed to be full
        ("bun test", None),  # bun's own runner, not the package script
        ("npm run test:unit", None),
        ("ruff check .", None),
        ("cd web && npm test", None),
    ])
    def test_suite_of(self, command, suite):
        checks = classify_check_commands(command)
        assert checks and checks[-1].suite == suite

    def test_supersede_rule(self):
        assert suite_supersedes("a" * 16 + ":pm_test:full", "a" * 16 + ":vitest:narrow")
        assert suite_supersedes("a" * 16 + ":vitest:full", "a" * 16 + ":vitest:narrow")
        assert suite_supersedes("a" * 16 + ":pm_test:full", "a" * 16 + ":pm_test:narrow")
        assert not suite_supersedes("a" * 16 + ":vitest:full", "a" * 16 + ":pm_test:narrow")
        assert not suite_supersedes("a" * 16 + ":pm_test:full", "a" * 16 + ":pytest:narrow")
        assert not suite_supersedes("a" * 16 + ":pm_test:full", "b" * 16 + ":vitest:narrow")
        assert not suite_supersedes("a" * 16 + ":vitest:narrow", "a" * 16 + ":vitest:narrow")
        assert not suite_supersedes("a" * 16 + ":vitest:full", "a" * 16 + ":vitest:full")
        assert not suite_supersedes(None, "a" * 16 + ":vitest:narrow")


REPLACED = "passed · 1 earlier failure replaced by a later run"


class TestReplacedFailureWording:
    """A pass that replaced an earlier failure reads as a pass, never as ``1/2 passed``."""

    @pytest.mark.parametrize("later", [NARROW, "npm run test"])
    def test_checks_and_verified_rows_name_the_replaced_failure(self, repo, later):
        entry = _session(repo, (NARROW, False), (later, True))
        receipt = build_shard_receipt(entry)
        assert checks_label(receipt) == f"{REPLACED} (agent-reported)"
        assert verified_label(receipt) == f"Not verified by OpenShard (agent reported {REPLACED})"
        compact = render_compact_shard_receipt(receipt)
        assert f"{REPLACED} (agent-reported)" in compact and "1/2 passed" not in compact
        assert "1/2 passed" not in render_full_shard_receipt(receipt)

    def test_display_only_evidence_history_and_synced_fields_unchanged(self, repo):
        entry = _session(repo, (NARROW, False), ("npm run test", True))
        receipt = build_shard_receipt(entry)
        checks_label(receipt)
        assert receipt.checks_display == "1/2 passed"  # synced string, unchanged
        # The evidence history still lists the failed run, unchanged.
        assert [(c["name"], c["status"]) for c in receipt.verification["checks"]] == [
            (f"Bash: {NARROW}", "failed"), ("Bash: npm run test", "passed"),
        ]
        assert entry["capture"]["check_runs"] == {"passed": 1, "failed": 1}

    def test_two_replaced_failures_are_plural(self, repo):
        entry = _session(repo, (NARROW, False), (NARROW, False), ("npm run test", True))
        label = checks_label(build_shard_receipt(entry))
        assert label == "passed · 2 earlier failures replaced by later runs (agent-reported)"

    def test_failure_still_standing_keeps_counts(self, repo):
        entry = _session(repo, ("npm run test", True), (NARROW, False))
        label = checks_label(build_shard_receipt(entry))
        assert "replaced" not in label and "failed" in label

    def test_clean_pass_keeps_ratio(self, repo):
        entry = _session(repo, ("npm run test", True))
        assert checks_label(build_shard_receipt(entry)) == "1/1 passed (agent-reported)"

    def test_truncated_run_list_is_a_lower_bound(self):
        block = {"source": "agent_reported", "status": "passed", "checks_failed": 0,
                 "checks": [{"status": "failed"}, {"status": "passed"}],
                 "incomplete_reasons": ["checks_truncated"]}
        assert replaced_failures_phrase(block) == "passed · at least 1 earlier failure replaced by a later run"

    def test_observed_or_malformed_blocks_get_no_phrase(self):
        observed = {"source": "directly_observed", "status": "passed", "checks_failed": 0,
                    "checks": [{"status": "failed"}]}
        assert replaced_failures_phrase(observed) == ""
        assert replaced_failures_phrase(None) == ""
        assert replaced_failures_phrase({"source": "agent_reported", "status": "passed", "checks": "x"}) == ""
