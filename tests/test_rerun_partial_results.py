"""A re-run whose tests all passed before its setup failed keeps those counts as partial evidence.

The tether case: ``vitest run && electron tests/verify-electron-primitives.cjs``
re-run in an isolated copy reports 69/69 tests passing, then Electron's binary
is missing and the script exits 1. The check stays ``unknown`` with
``setup_failed`` (part of it never ran), but the runner's summary is kept as
counts. A summary with any failure, or no summary, changes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openshard.history.verification import build_verification
from openshard.history.verification_truth import (
    BASIS_POST_SESSION,
    STATE_AGENT_REPORTED_PASSED,
    interpret_evidence,
    verification_label,
)
from openshard.sync.evidence import evidence_items
from openshard.verification import post_session as ps
from openshard.verification.setup_failure import ReportedTests, reported_test_results

ELECTRON_ERROR = (
    "Error: Electron failed to install correctly, please delete node_modules/electron and try installing again\n"
    "    at getElectronPath (node_modules/electron/index.js:17:11)\n"
)
TETHER = (
    "> tether@1.0.0 test\n> vitest run && electron tests/verify-electron-primitives.cjs\n\n"
    " RUN  v2.1.9 C:/Users/me/tether\n\n"
    " \u2713 src/a.test.ts (12 tests) 20ms\n"
    " Test Files  11 passed (11)\n"
    "      Tests  69 passed (69)\n"
    "   Start at  10:00:00\n"
    "   Duration  3.21s\n\n"
    + ELECTRON_ERROR
)
JEST = "Test Suites: 3 passed, 3 total\nTests:       1 skipped, 12 passed, 13 total\nTime: 2.1 s\n" + ELECTRON_ERROR
PYTEST = "============================= test session starts ==============================\n" \
    "============================== 40 passed in 1.23s ==============================\n" + ELECTRON_ERROR
MOCHA = "  12 passing (30ms)\n\n" + ELECTRON_ERROR
NODE_TEST = "# tests 4\n# suites 0\n# pass 4\n# fail 0\n# cancelled 0\n# skipped 0\n" + ELECTRON_ERROR
VITEST_WITH_FAILURE = " Test Files  1 failed | 10 passed (11)\n      Tests  1 failed | 68 passed (69)\n" + ELECTRON_ERROR
NO_SUMMARY = "> tether@1.0.0 test\n> vitest run\n\n" + ELECTRON_ERROR

CLEAN = ps.TreeState(head="2650366" + "a" * 33, dirty=False, tracked_dirty=False)
AGENT_PASSED = build_verification(
    source="agent_reported", observation_mode="hook_tool_event",
    checks=[{"name": "Bash: npm run test", "kind": "test", "status": "passed"}],
)


class _Proc:
    def __init__(self, code: int, stdout: str | None = None):
        self.returncode = code
        self.stdout = stdout


def _rerun(output: str, code: int = 1) -> list[ps.CheckRun]:
    check = ps.PlannedCheck(name="npm", argv=["npm"], kind="test", origin="change", safety="safe", reason="")
    seen: list[list[str]] = []

    def runner(argv, **_kw):
        seen.append(list(argv))
        return _Proc(code, output)

    results = ps.run_checks([check], Path(__file__).parent, runner=runner, stream=False)
    assert seen == [["npm"]]  # the command is run once, as planned, never split on &&
    return results


def _attest(results):
    return ps.build_attestation({"receipt_id": "rcp_1"}, results, before=CLEAN, after=CLEAN,
                                started_at="2026-10-17T10:00:00Z", completed_at="2026-10-17T10:00:09Z",
                                commit=CLEAN.head)


class TestParsing:
    @pytest.mark.parametrize("output, expected", [
        (TETHER, ReportedTests("vitest", 69, 0, 0)),
        (JEST, ReportedTests("jest", 12, 0, 1)),
        (PYTEST, ReportedTests("pytest", 40, 0, 0)),
        ("40 passed, 2 skipped in 0.50s\n", ReportedTests("pytest", 40, 0, 2)),
        (MOCHA, ReportedTests("mocha", 12, 0, 0)),
        (NODE_TEST, ReportedTests("node --test", 4, 0, 0)),
        ("\u2139 tests 4\n\u2139 pass 4\n\u2139 fail 0\n", ReportedTests("node --test", 4, 0, 0)),
    ])
    def test_an_all_passing_summary_is_read(self, output, expected):
        assert reported_test_results(output) == expected

    @pytest.mark.parametrize("output", [
        VITEST_WITH_FAILURE,
        " Test Files  1 failed | 10 passed (11)\n      Tests  69 passed (69)\n",  # a file failed to load
        "      Tests  69 passed (69)\n     Errors  1 error\n",  # an unhandled error
        "Test Suites: 1 failed, 2 passed, 3 total\nTests:       12 passed, 12 total\n",
        "== 1 failed, 2 passed in 0.1s ==\n",
        "== 2 passed, 1 error in 0.1s ==\n",
        "  3 passing (20ms)\n  1 failing\n",
        "# pass 3\n# fail 0\n# cancelled 1\n",
        NO_SUMMARY,
        "",
        None,
        "      Tests  0 passed (0)\n",  # nothing passed
        " Test Files  1 passed (1)\n      Tests  5 passed (5)\n      Tests  6 passed (6)\n",  # two summaries
    ])
    def test_no_partial_result_without_one_clean_summary(self, output):
        assert reported_test_results(output) is None


class TestTetherRerun:
    def test_the_passing_tests_are_kept_and_the_check_stays_unknown(self):
        (r,) = _rerun(TETHER)
        assert (r.status, r.exit_code, r.setup_failed) == ("unknown", 1, True)
        assert r.reported == ReportedTests("vitest", 69, 0, 0)
        expected = ("OpenShard re-ran the tests: 69 / 69 passed (vitest); then Electron's binary is not "
                    "installed (its install script did not run), so the full check couldn't run")
        assert r.note == expected
        att = _attest([r])
        block = att["verification"]
        assert block["status"] == "unknown" and block["complete"] is False
        assert block["checks_passed"] == 0 and block["checks_failed"] == 0  # never counted as a passed check
        assert "verifier_setup_failed" in block["incomplete_reasons"]
        assert block["reason"] == expected + "."
        row = att["checks"][0]
        assert (row["reported_runner"], row["reported_passed"], row["reported_failed"], row["reported_skipped"]) \
            == ("vitest", 69, 0, 0)
        assert row["note"] == expected
        assert att["raw_output_stored"] is False and "Test Files" not in str(att)  # counts only, no output

    def test_the_cli_line_says_partial(self):
        summary = ps.summarize_attestation(_attest(_rerun(TETHER)))
        assert summary["reported_tests"] == {"runner": "vitest", "passed": 69, "failed": 0, "skipped": 0}
        assert ps.display_line(summary) == (
            "partial — 69/69 tests passed (vitest); Electron step couldn't run @ 2650366aaaaa (OpenShard re-run)"
        )
        assert ps.result_detail(_rerun(TETHER)[0]).endswith("so the full check couldn't run; exit 1")

    def test_the_verdict_is_never_verified_and_never_failed(self):
        summary = ps.latest_for_entry({"receipt_id": "rcp_1"}, [_attest(_rerun(TETHER))])
        truth = interpret_evidence(AGENT_PASSED, post_session_verification=summary)
        assert truth.state == STATE_AGENT_REPORTED_PASSED and truth.basis != BASIS_POST_SESSION
        label = verification_label(truth)
        assert label.startswith("Not verified by OpenShard") and "Failed" not in label

    def test_the_platform_evidence_carries_the_unknown_block_not_the_counts(self):
        post = ps.latest_for_entry({"receipt_id": "rcp_1"}, [_attest(_rerun(TETHER))])
        (item,) = evidence_items(post)
        assert set(item) == {"attestation_id", "created_at", "kind", "verification"}
        assert item["verification"]["status"] == "unknown"
        assert item["verification"]["reason"].startswith("OpenShard re-ran the tests: 69 / 69 passed (vitest)")


@pytest.mark.parametrize("output, runner, text", [
    (JEST, "jest", "12 / 12 passed, 1 skipped (jest)"),
    (PYTEST, "pytest", "40 / 40 passed (pytest)"),
    (MOCHA, "mocha", "12 / 12 passed (mocha)"),
    (NODE_TEST, "node --test", "4 / 4 passed (node --test)"),
])
def test_other_runners_are_kept_the_same_way(output, runner, text):
    (r,) = _rerun(output)
    assert r.status == "unknown" and r.setup_failed and r.reported is not None and r.reported.runner == runner
    assert r.note.startswith(f"OpenShard re-ran the tests: {text}; then Electron")
    assert ps.display_line(ps.summarize_attestation(_attest([r]))).startswith("partial — ")


def test_a_summary_with_failures_keeps_the_could_not_run_behaviour():
    (r,) = _rerun(VITEST_WITH_FAILURE)
    assert (r.status, r.setup_failed, r.reported) == ("unknown", True, None)
    assert r.note.startswith("OpenShard couldn't set up the project to re-run the check: Electron")
    att = _attest([r])
    assert not any(key.startswith("reported_") for key in att["checks"][0])
    summary = ps.summarize_attestation(att)
    assert "reported_tests" not in summary
    assert ps.display_line(summary).startswith("Could not run (project setup failed)")


def test_no_summary_keeps_the_could_not_run_behaviour():
    (r,) = _rerun(NO_SUMMARY)
    assert (r.status, r.setup_failed, r.reported) == ("unknown", True, None)
    assert ps.display_line(ps.summarize_attestation(_attest([r]))).startswith("Could not run (project setup failed)")


def test_a_real_failure_is_unchanged():
    (r,) = _rerun(" FAIL  src/a.test.ts\n Test Files  1 failed (1)\n      Tests  1 failed | 4 passed (5)\n")
    assert (r.status, r.setup_failed, r.reported) == ("failed", False, None)


def test_a_forged_count_in_the_file_is_not_surfaced():
    att = _attest(_rerun(TETHER))
    att["checks"][0]["reported_failed"] = 2
    assert "reported_tests" not in ps.summarize_attestation(att)
    att["checks"][0].update(reported_failed=0, reported_runner="<script>")
    assert "reported_tests" not in ps.summarize_attestation(att)
