"""A re-run that could not set the project up is ``unknown`` with a reason, never ``failed``.

OpenShard re-runs a session's checks on the exact commit, often in an isolated
copy whose JavaScript dependencies were installed offline with install scripts
disabled. A suite that cannot start there says nothing about the change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openshard.history.verification import build_verification
from openshard.history.verification_truth import (
    BASIS_POST_SESSION,
    STATE_AGENT_REPORTED_PASSED,
    interpret_evidence,
    verification_label,
)
from openshard.verification import post_session as ps
from openshard.verification.setup_failure import reports_test_results, rerun_setup_failure

ELECTRON = (
    "> tether@1.0.0 test\n> vitest run\n\n"
    "Error: Electron failed to install correctly, please delete node_modules/electron and try installing again\n"
    "    at getElectronPath (node_modules/electron/index.js:17:11)\n"
)
ELECTRON_IN_VITEST_REPORT = (
    " FAIL  src/main.test.ts [ src/main.test.ts ]\n"
    "Error: Electron failed to install correctly, please delete node_modules/electron and try installing again\n"
    " Test Files  1 failed (1)\n      Tests  no tests\n"
)
NATIVE = "Error: Could not locate the bindings file. Tried:\n → node_modules/better-sqlite3/build/better_sqlite3.node\n"
NATIVE_PATH = "Error: Cannot find module '../build/Release/keytar.node'\nRequire stack:\n- index.js\n"
NODE_VERSION = ("Error: The module 'x.node'\nwas compiled against a different Node.js version using\n"
                "NODE_MODULE_VERSION 119. This version of Node.js requires\nNODE_MODULE_VERSION 115.\n")
MISSING_PACKAGE = (
    "node:internal/modules/cjs/loader:1228\n  throw err;\n  ^\n\n"
    "Error: Cannot find module 'vitest/config'\nRequire stack:\n- vitest.config.ts\n"
    "  code: 'MODULE_NOT_FOUND',\n"
)
MISSING_ESM_PACKAGE = "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@electron/remote' imported from main.js\n"
SH_NOT_FOUND = "> tether@1.0.0 test\n> vitest run\n\nsh: 1: vitest: not found\n"
NPM_MISSING_SCRIPT = 'npm error Missing script: "test"\nnpm error\nnpm error To see a list of scripts, run:\n'
NPM_ENOENT = "npm ERR! code ENOENT\nnpm ERR! syscall open\nnpm ERR! enoent Could not read package.json\n"
SPAWN_ENOENT = "Error: spawn /tmp/x/node_modules/.bin/electron ENOENT\n    at ChildProcess._handle.onexit\n"
PY_NO_MODULE = "/usr/bin/python3.12: No module named pytest\n"

VITEST_FAILED = (
    " FAIL  src/sum.test.ts > adds\nAssertionError: expected 3 to be 4\n\n"
    " Test Files  1 failed (1)\n      Tests  1 failed | 4 passed (5)\n"
)
JEST_FAILED_ON_MISSING_MODULE = (
    " FAIL  src/app.test.js\n  ● Test suite failed to run\n\n"
    "    Cannot find module 'left-pad' from 'src/app.js'\n\n"
    "Test Suites: 1 failed, 1 total\nTests:       0 total\n"
)
PYTEST_FAILED_ON_IMPORT = (
    "============================= test session starts ==============================\n"
    "E   ModuleNotFoundError: No module named 'pkg.gone'\n"
    "=========================== 1 error in 0.10s ===========================\n"
)


class TestClassification:
    @pytest.mark.parametrize("output, needle", [
        (ELECTRON, "Electron"),
        (ELECTRON_IN_VITEST_REPORT, "Electron"),  # an incomplete install wins even inside a report
        (NATIVE, "native module"),
        (NATIVE_PATH, "native module"),
        (NODE_VERSION, "different Node.js"),
        (MISSING_PACKAGE, "vitest is not installed"),
        (MISSING_ESM_PACKAGE, "@electron/remote is not installed"),
        (SH_NOT_FOUND, "command"),
        (NPM_MISSING_SCRIPT, "package script"),
        (NPM_ENOENT, "ENOENT"),
        (SPAWN_ENOENT, "program the tests start"),
        (PY_NO_MODULE, "Python module"),
    ])
    def test_setup_signatures_are_could_not_run(self, output, needle):
        reason = rerun_setup_failure(1, output)
        assert reason is not None and needle in reason
        assert "/tmp" not in reason and "node_modules" not in reason  # path-free

    def test_command_not_found_exit_codes(self):
        assert rerun_setup_failure(127, "") is not None
        assert rerun_setup_failure(126, None) is not None

    def test_a_runner_killed_before_reporting_is_could_not_run(self):
        assert "signal 11" in (rerun_setup_failure(-11, "Segmentation fault\n") or "")
        assert rerun_setup_failure(-11, VITEST_FAILED) is None  # it had reported results

    @pytest.mark.parametrize("output", [
        VITEST_FAILED, JEST_FAILED_ON_MISSING_MODULE, PYTEST_FAILED_ON_IMPORT,
        "  3 passing (20ms)\n  1 failing\n\nError: Cannot find module 'chalk'\n",
        "# tests 4\n# pass 3\n# fail 1\nsh: 1: helper: not found\n",
    ])
    def test_once_results_are_reported_a_failure_stays_failed(self, output):
        assert reports_test_results(output)
        assert rerun_setup_failure(1, output) is None

    @pytest.mark.parametrize("output", [
        "",
        None,
        "npm ERR! Lifecycle script `test` failed with error:\nnpm ERR! code 1\n",  # npm's normal failure banner
        "src/a.ts(1,20): error TS2307: Cannot find module 'x' or its corresponding type declarations.\n",
        "Error: Cannot find module './util'\nRequire stack:\n- src/index.js\n",  # the change's own file
        "Error: Cannot find module '/repo/dist/main.js'\n",
        "Found 3 errors.\n",
    ])
    def test_no_signature_stays_failed(self, output):
        assert rerun_setup_failure(1, output) is None

    def test_a_pass_or_missing_exit_code_is_never_a_setup_failure(self):
        assert rerun_setup_failure(0, ELECTRON) is None
        assert rerun_setup_failure(None, ELECTRON) is None

    def test_colour_codes_do_not_hide_a_report(self):
        coloured = "\x1b[31m FAIL \x1b[39m src/a.test.ts\n\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m (1)\n"
        assert rerun_setup_failure(1, coloured + "Error: Cannot find module 'x'\n") is None


class _Proc:
    def __init__(self, code: int, stdout: str | None = None):
        self.returncode = code
        self.stdout = stdout


def _check(name: str) -> ps.PlannedCheck:
    return ps.PlannedCheck(name=name, argv=[name], kind="test", origin="change", safety="safe", reason="")


def _runner(outcomes: dict[str, _Proc]):
    return lambda argv, **_kw: outcomes[argv[0]]


CLEAN = ps.TreeState(head="2650366" + "a" * 33, dirty=False, tracked_dirty=False)
AGENT_PASSED = build_verification(
    source="agent_reported", observation_mode="hook_tool_event",
    checks=[{"name": "Bash: npm run test", "kind": "test", "status": "passed"}],
)


def _attest(results):
    return ps.build_attestation({"receipt_id": "rcp_1"}, results, before=CLEAN, after=CLEAN,
                                started_at="2026-10-17T10:00:00Z", completed_at="2026-10-17T10:00:09Z",
                                commit=CLEAN.head)


class TestRerunRecording:
    def test_electron_setup_failure_is_unknown_with_the_reason(self, tmp_path):
        results = ps.run_checks([_check("npm")], tmp_path, runner=_runner({"npm": _Proc(1, ELECTRON)}),
                                stream=False)
        (r,) = results
        assert (r.status, r.exit_code, r.setup_failed) == ("unknown", 1, True)
        assert r.note.startswith("OpenShard couldn't set up the project to re-run the check: Electron")
        att = _attest(results)
        block = att["verification"]
        assert block["status"] == "unknown" and block["checks_failed"] == 0
        assert "verifier_setup_failed" in block["incomplete_reasons"] and block["complete"] is False
        assert block["reason"].startswith("OpenShard couldn't set up the project to re-run the check")
        assert att["checks"][0]["note"].startswith("OpenShard couldn't set up")
        assert ps.display_line(ps.summarize_attestation(att)).startswith("Could not run (project setup failed)")

    def test_tests_that_ran_and_failed_are_still_failed(self, tmp_path):
        results = ps.run_checks([_check("npm")], tmp_path, runner=_runner({"npm": _Proc(1, VITEST_FAILED)}),
                                stream=False)
        assert [(r.status, r.setup_failed) for r in results] == [("failed", False)]
        att = _attest(results)
        assert att["verification"]["status"] == "failed"
        assert "verifier_setup_failed" not in att["verification"]["incomplete_reasons"]
        assert ps.display_line(ps.summarize_attestation(att)).startswith("0/1 passed")

    def test_a_runner_without_output_keeps_exit_code_semantics(self, tmp_path):
        results = ps.run_checks([_check("bad")], tmp_path, runner=_runner({"bad": _Proc(1)}), stream=False)
        assert results[0].status == "failed"

    def test_one_setup_failure_beside_a_pass_is_partial_not_failed(self, tmp_path):
        results = ps.run_checks([_check("ok"), _check("npm")], tmp_path, stream=False,
                                runner=_runner({"ok": _Proc(0), "npm": _Proc(1, MISSING_PACKAGE)}))
        block = _attest(results)["verification"]
        assert block["status"] == "partial" and block["checks_failed"] == 0
        assert block["reason"].startswith("OpenShard couldn't set up")


class TestReceiptVerdict:
    def _truth(self, results):
        summary = ps.latest_for_entry({"receipt_id": "rcp_1"}, [_attest(results)])
        return interpret_evidence(AGENT_PASSED, post_session_verification=summary)

    def test_a_setup_failure_never_reads_failed_and_never_upgrades_the_agent_claim(self, tmp_path):
        results = ps.run_checks([_check("npm")], tmp_path, runner=_runner({"npm": _Proc(1, ELECTRON)}),
                                stream=False)
        truth = self._truth(results)
        assert truth.state == STATE_AGENT_REPORTED_PASSED and truth.basis != BASIS_POST_SESSION
        label = verification_label(truth)
        assert label.startswith("Not verified by OpenShard") and "Failed" not in label

    def test_a_real_failure_still_overrides_the_agent_claim(self, tmp_path):
        results = ps.run_checks([_check("npm")], tmp_path, runner=_runner({"npm": _Proc(1, VITEST_FAILED)}),
                                stream=False)
        assert verification_label(self._truth(results)).startswith("Failed (OpenShard re-ran the check(s)")


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "check.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


class TestRealProcess:
    def test_output_is_read_without_a_custom_runner(self, tmp_path):
        argv = _script(tmp_path, "import sys\nsys.stderr.write(" + repr(ELECTRON) + ")\nsys.exit(1)\n")
        check = ps.PlannedCheck(name="t", argv=argv, kind="test", origin="change", safety="safe", reason="")
        (r,) = ps.run_checks([check], tmp_path, stream=False)
        assert (r.status, r.exit_code, r.setup_failed) == ("unknown", 1, True)

    def test_a_real_failing_suite_is_failed_and_echoed(self, tmp_path, capfd):
        argv = _script(tmp_path, "print(" + repr(VITEST_FAILED) + ")\nraise SystemExit(1)\n")
        check = ps.PlannedCheck(name="t", argv=argv, kind="test", origin="change", safety="safe", reason="")
        (r,) = ps.run_checks([check], tmp_path, stream=True)
        assert r.status == "failed"
        assert "Test Files  1 failed" in capfd.readouterr().out

    def test_a_long_output_keeps_its_head_and_tail(self, tmp_path):
        body = ("import sys\nsys.stdout.write('sh: 1: vitest: not found\\n')\n"
                "sys.stdout.write('x' * 200000 + '\\n')\nsys.stdout.write('npm error code 127\\n')\nsys.exit(1)\n")
        _proc, output = ps.run_check_process(_script(tmp_path, body), cwd=tmp_path, timeout=60, echo=False)
        assert output.startswith("sh: 1: vitest: not found") and output.rstrip().endswith("code 127")
        assert len(output) <= ps.OUTPUT_HEAD_BYTES + ps.OUTPUT_TAIL_BYTES


def test_an_offline_install_that_failed_is_could_not_run_with_its_reason():
    from openshard.verification import commit_run as cr

    check = _check("npm")
    results = cr._not_run([check], [check], "npm could not prepare dependencies offline (exit 1)")
    assert [(r.status, r.setup_failed) for r in results] == [("unknown", True)]
    block = _attest(results)["verification"]
    assert block["status"] == "unknown" and "verifier_setup_failed" in block["incomplete_reasons"]
    assert block["reason"] == ("OpenShard couldn't set up the project to re-run the check: "
                               "npm could not prepare dependencies offline (exit 1).")


def test_the_cli_detail_names_the_reason_and_the_exit_code():
    setup = ps.CheckRun(_check("npm"), "unknown", exit_code=1, note="OpenShard couldn't set up x", setup_failed=True)
    assert ps.result_detail(setup) == "OpenShard couldn't set up x; exit 1"
    assert ps.result_detail(ps.CheckRun(_check("npm"), "failed", exit_code=1)) == "exit 1"
