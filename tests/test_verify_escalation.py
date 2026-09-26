"""Verification failures and model escalation: environment failures stop escalation,
real failures still escalate, and every escalation is accumulated."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.execution.generator import ChangedFile
from openshard.history.outcome_classification import (
    CAUSE_HARNESS,
    VERIFICATION_INFRA_ERROR,
    read_classification,
    should_retry_with_another_model,
)
from openshard.history.run_cost import run_total_from_usage
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.verification import derive_verification
from openshard.native.context import NativeSandboxMeta
from openshard.providers.base import UsageStats
from openshard.run.pipeline import ESCALATION_CHAIN
from openshard.verification.setup_failure import (
    KIND_COMMAND_NOT_FOUND,
    KIND_MISSING_MODULE,
    detect_setup_failure,
    setup_failure_metadata,
)
from tests.test_iterative_retry_loop import (
    _DEFAULT_CONFIG,
    _PYTHON_REPO_WITH_TEST_CMD,
    _make_manager_mock,
    _make_native_mock,
)

WIN_MISSING_PYTEST = r"C:\Users\Michael\AppData\Local\Programs\Python\Python311\python.exe: No module named pytest"
POSIX_MISSING_PYTEST = "/usr/bin/python3.12: No module named pytest"


class TestDetector:
    @pytest.mark.parametrize("output", [WIN_MISSING_PYTEST, POSIX_MISSING_PYTEST, "python: No module named pytest"])
    def test_interpreter_missing_module(self, output):
        assert detect_setup_failure(1, output) == KIND_MISSING_MODULE

    @pytest.mark.parametrize("output", [
        "bash: pytest: command not found",
        "sh: 1: pytest: not found",
        "bash: line 1: ruff: command not found",
        "'pytest' is not recognized as an internal or external command,\r\noperable program or batch file.",
        "  [verify] not found",
    ])
    def test_command_not_found(self, output):
        assert detect_setup_failure(1, output) == KIND_COMMAND_NOT_FOUND

    @pytest.mark.parametrize("code", [126, 127])
    def test_shell_tooling_exit_codes_need_no_output(self, code):
        assert detect_setup_failure(code, "") == KIND_COMMAND_NOT_FOUND

    @pytest.mark.parametrize("output", [
        "FAILED tests/test_calc.py::test_subtract - assert 1 == 2",
        "E   ModuleNotFoundError: No module named 'calc_helpers'",  # a real failure of the change under test
        "tests/test_x.py:3: in <module>\n    import foo\nE   ImportError: cannot import name 'foo'",
        "1 failed, 2 passed in 0.12s",
        "",
        None,
    ])
    def test_a_real_test_failure_is_never_an_environment_problem(self, output):
        assert detect_setup_failure(1, output) is None

    def test_success_is_never_a_setup_failure(self):
        assert detect_setup_failure(0, WIN_MISSING_PYTEST) is None

    def test_only_the_interpreters_own_line_counts(self):
        # Mentioning the phrase mid-line, e.g. inside a printed traceback of the code under test.
        assert detect_setup_failure(1, "print('python: No module named pytest')") is None
        assert detect_setup_failure(1, "AssertionError: expected 'python: No module named x'") is None


class TestRecordedMetadata:
    def _meta(self):
        return setup_failure_metadata(KIND_MISSING_MODULE, 1, model="deepseek/deepseek-v4-pro")

    def test_verification_is_unknown_and_incomplete_never_failed_or_passed(self):
        v = self._meta()["verification"]
        assert v["status"] == "unknown" and v["source"] == "directly_observed"
        assert v["complete"] is False and "verifier_setup_failed" in v["incomplete_reasons"]
        assert v["exit_code"] == 1 and "module is missing" in v["reason"]

    def test_the_outcome_is_attributed_to_the_environment_not_the_model(self):
        c = read_classification({"outcome_classification": self._meta()["outcome_classification"]})
        assert c.outcome == VERIFICATION_INFRA_ERROR and c.cause == CAUSE_HARNESS
        assert c.model == "deepseek/deepseek-v4-pro" and c.routing_eligible is False
        assert should_retry_with_another_model(c) is False

    def test_no_command_output_is_recorded(self):
        text = repr(self._meta())
        assert "No module named" not in text and "Python311" not in text and "Users" not in text

    def test_the_receipt_reads_it_as_unknown_not_failed(self):
        entry = {"executor": "native", "timestamp": "2026-09-26T13:30:42Z", "shard_id": "shard-20260926-0001",
                 "receipt_id": "rcpt_" + "c1" * 16, "verification_attempted": True, "verification_passed": None,
                 **self._meta()}
        assert derive_verification(entry).status == "unknown"
        assert build_shard_receipt(entry, index=0).verification["status"] == "unknown"


def _usage(cost, tokens=100):
    return UsageStats(prompt_tokens=tokens - 10, completion_tokens=10, total_tokens=tokens, estimated_cost=cost)


def _run_verify(verify_results, generate_results=None):
    """Invoke `openshard run --workflow native --write --verify` with mocked infrastructure.

    verify_results: one (exit_code, output) per call of the verification runner, in order.
    Returns (cli result, generator mock, the mocked _log_run).
    """
    native = _make_native_mock()
    if generate_results is not None:
        native.generate.side_effect = generate_results
    calls = iter(verify_results)

    def fake_verify(*args, **kwargs):
        try:
            code, out = next(calls)
        except StopIteration:
            code, out = 0, ""
        return (code, out) if kwargs.get("capture", False) else code

    with tempfile.TemporaryDirectory() as td:
        meta = NativeSandboxMeta(sandbox_enabled=False, sandbox_type="none")
        with (
            patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native),
            patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()),
            patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG),
            patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO_WITH_TEST_CMD),
            patch("openshard.run.pipeline._run_verification_plan", side_effect=fake_verify),
            patch("openshard.run.pipeline._write_files"),
            patch("openshard.native.sandbox.create_run_sandbox", return_value=(Path(td), meta)),
            patch("openshard.run.pipeline._log_run") as log_run,
        ):
            result = CliRunner().invoke(cli, ["run", "--workflow", "native", "--write", "--verify", "fix the bug"])
    return result, native, log_run


def _log_kwargs(log_run) -> dict:
    """The arguments _log_run received, with the leading positionals named."""
    args = log_run.call_args.args
    named = dict(zip(("start", "task", "generator", "retry_triggered", "files"), args))
    return {**named, **log_run.call_args.kwargs}


def _gen(cost, files=None):
    return MagicMock(usage=_usage(cost), files=files or [ChangedFile("a.py", "update", "", "")], summary="done", notes=[])


class TestPipelineStopsOnSetupFailure:
    def test_a_missing_tool_makes_no_further_model_call_and_is_recorded_honestly(self):
        result, native, log_run = _run_verify(
            [(1, ""), (1, WIN_MISSING_PYTEST)], generate_results=[_gen(0.001)],
        )
        assert native.generate.call_count == 1  # the first generation only: no escalation
        assert "not escalating to another model" in result.output
        kwargs = _log_kwargs(log_run)
        assert kwargs["retry_triggered"] is False
        assert kwargs["verification_passed"] is None  # not a recorded failure
        extra = kwargs["extra_metadata"]
        assert extra["verification"]["status"] == "unknown"
        assert extra["outcome_classification"]["outcome"] == VERIFICATION_INFRA_ERROR
        assert kwargs["retry_usage"] is None  # nothing was spent on retries
        assert result.exit_code != 0  # the run still did not verify

    def test_a_real_test_failure_still_escalates(self):
        result, native, log_run = _run_verify(
            [(1, ""), (1, "FAILED tests/test_calc.py::test_subtract - assert 1 == 2"), (0, "")],
            generate_results=[_gen(0.001), _gen(0.002)],
        )
        assert native.generate.call_count == 2
        assert "not escalating" not in result.output
        assert _log_kwargs(log_run)["retry_triggered"] is True
        assert result.exit_code == 0

    def test_a_missing_module_inside_the_code_under_test_is_a_real_failure(self):
        result, native, _ = _run_verify(
            [(1, ""), (1, "E   ModuleNotFoundError: No module named 'helpers'"), (0, "")],
            generate_results=[_gen(0.001), _gen(0.002)],
        )
        assert native.generate.call_count == 2 and "not escalating" not in result.output


class TestPipelineAccumulatesEscalations:
    def test_every_escalation_is_recorded_not_just_the_last(self):
        failing = (1, "FAILED tests/test_calc.py::test_subtract - assert 1 == 2")
        result, native, log_run = _run_verify(
            [(1, ""), failing, failing, failing, (0, "")],
            generate_results=[_gen(0.001), _gen(0.002), _gen(0.0146)],
        )
        assert native.generate.call_count == 1 + len(ESCALATION_CHAIN)
        retry_usage = log_run.call_args.kwargs["retry_usage"]
        assert [a["model"] for a in retry_usage.attempts] == list(ESCALATION_CHAIN)
        assert retry_usage.estimated_cost == pytest.approx(0.0166)  # was only the last attempt's 0.0146
        total, complete = run_total_from_usage(_usage(0.001), retry_usage, True)
        assert complete is True and total == pytest.approx(0.0176)
