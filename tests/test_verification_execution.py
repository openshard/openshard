from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openshard.cli.main import _run_verification_plan
from openshard.execution.gates import GateEvaluator
from openshard.verification.plan import (
    CommandSafety,
    VerificationCommand,
    VerificationKind,
    VerificationPlan,
    VerificationSource,
)


def _make_cmd(
    argv: list[str],
    safety: CommandSafety,
    kind: VerificationKind = VerificationKind.unknown,
    source: VerificationSource = VerificationSource.detected,
) -> VerificationCommand:
    return VerificationCommand(
        name="tests" if safety == CommandSafety.safe else "verification",
        argv=argv,
        kind=kind,
        source=source,
        safety=safety,
        reason=f"safety={safety.value}",
    )


def _safe_plan(argv: list[str] | None = None) -> VerificationPlan:
    argv = argv or ["python", "-m", "pytest"]
    return VerificationPlan(commands=[
        _make_cmd(argv, CommandSafety.safe, kind=VerificationKind.test),
    ])


def _blocked_plan() -> VerificationPlan:
    return VerificationPlan(commands=[
        _make_cmd(["rm", "-rf", "."], CommandSafety.blocked),
    ])


def _needs_approval_plan() -> VerificationPlan:
    return VerificationPlan(commands=[
        _make_cmd(["make", "test"], CommandSafety.needs_approval),
    ])


def _proc(returncode: int = 0, stdout: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    return p


def _gate(approval_mode: str = "auto") -> GateEvaluator:
    return GateEvaluator(approval_mode=approval_mode, risky_paths=[], cost_threshold=0.10)


# ---------------------------------------------------------------------------
# No-command paths
# ---------------------------------------------------------------------------


class TestNoCommand(unittest.TestCase):

    def test_no_command_returns_zero(self):
        with patch("click.echo") as mock_echo:
            result = _run_verification_plan(VerificationPlan(), Path("/tmp"))
        self.assertEqual(result, 0)
        mock_echo.assert_called_once_with("  [verify] no test command detected")

    def test_no_command_capture_is_silent(self):
        with patch("click.echo") as mock_echo:
            result = _run_verification_plan(VerificationPlan(), Path("/tmp"), capture=True)
        mock_echo.assert_not_called()
        self.assertEqual(result, (0, ""))

    def test_no_command_custom_label(self):
        with patch("click.echo") as mock_echo:
            _run_verification_plan(VerificationPlan(), Path("/tmp"), label="[retry/x]")
        mock_echo.assert_called_once_with("  [retry/x] no test command detected")


# ---------------------------------------------------------------------------
# Blocked paths
# ---------------------------------------------------------------------------


class TestBlockedCommand(unittest.TestCase):

    def test_blocked_returns_one(self):
        with patch("click.echo"), patch("subprocess.run") as mock_run:
            result = _run_verification_plan(_blocked_plan(), Path("/tmp"))
        self.assertEqual(result, 1)
        mock_run.assert_not_called()

    def test_blocked_prints_message(self):
        with patch("click.echo") as mock_echo, patch("subprocess.run"):
            _run_verification_plan(_blocked_plan(), Path("/tmp"))
        msgs = [c.args[0] for c in mock_echo.call_args_list]
        self.assertTrue(any("blocked" in m for m in msgs))

    def test_blocked_label_in_message(self):
        with patch("click.echo") as mock_echo, patch("subprocess.run"):
            _run_verification_plan(_blocked_plan(), Path("/tmp"), label="[retry/opus]")
        msgs = [c.args[0] for c in mock_echo.call_args_list]
        self.assertTrue(any("[retry/opus]" in m and "blocked" in m for m in msgs))

    def test_blocked_capture_returns_one_and_message(self):
        with patch("click.echo") as mock_echo, patch("subprocess.run") as mock_run:
            code, output = _run_verification_plan(_blocked_plan(), Path("/tmp"), capture=True)
        self.assertEqual(code, 1)
        self.assertIn("blocked", output)
        mock_echo.assert_not_called()
        mock_run.assert_not_called()

    def test_blocked_no_subprocess_called(self):
        with patch("subprocess.run") as mock_run, patch("click.echo"):
            _run_verification_plan(_blocked_plan(), Path("/tmp"))
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Safe paths
# ---------------------------------------------------------------------------


class TestSafeCommand(unittest.TestCase):

    def test_safe_executes_argv_directly(self):
        with patch("subprocess.run", return_value=_proc()) as mock_run, patch("click.echo"):
            _run_verification_plan(_safe_plan(["python", "-m", "pytest"]), Path("/tmp"))
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0], ["python", "-m", "pytest"])

    def test_safe_no_shell_true(self):
        with patch("subprocess.run", return_value=_proc()) as mock_run, patch("click.echo"):
            _run_verification_plan(_safe_plan(), Path("/tmp"))
        kwargs = mock_run.call_args.kwargs
        self.assertNotEqual(kwargs.get("shell"), True)

    def test_safe_pass_prints_passed_returns_zero(self):
        with patch("subprocess.run", return_value=_proc(0)), patch("click.echo") as mock_echo:
            result = _run_verification_plan(_safe_plan(), Path("/tmp"))
        self.assertEqual(result, 0)
        msgs = [c.args[0] for c in mock_echo.call_args_list]
        self.assertTrue(any("passed" in m for m in msgs))

    def test_safe_fail_prints_failed_returns_nonzero(self):
        with patch("subprocess.run", return_value=_proc(2)), patch("click.echo") as mock_echo:
            result = _run_verification_plan(_safe_plan(), Path("/tmp"))
        self.assertEqual(result, 2)
        msgs = [c.args[0] for c in mock_echo.call_args_list]
        self.assertTrue(any("failed" in m for m in msgs))

    def test_safe_capture_silent_returns_code_and_output(self):
        with patch("subprocess.run", return_value=_proc(0, "3 passed")), patch("click.echo") as mock_echo:
            result = _run_verification_plan(_safe_plan(), Path("/tmp"), capture=True)
        mock_echo.assert_not_called()
        self.assertEqual(result, (0, "3 passed"))

    def test_safe_custom_label_in_output(self):
        with patch("subprocess.run", return_value=_proc()), patch("click.echo") as mock_echo:
            _run_verification_plan(_safe_plan(["pytest"]), Path("/tmp"), label="[retry/sonnet]")
        msgs = [c.args[0] for c in mock_echo.call_args_list]
        self.assertTrue(any("[retry/sonnet]" in m for m in msgs))

    def test_safe_cwd_passed_to_subprocess(self):
        cwd = Path("/some/workspace")
        with patch("subprocess.run", return_value=_proc()) as mock_run, patch("click.echo"):
            _run_verification_plan(_safe_plan(), cwd)
        self.assertEqual(mock_run.call_args.kwargs["cwd"], cwd)


# ---------------------------------------------------------------------------
# needs_approval paths
# ---------------------------------------------------------------------------


class TestNeedsApprovalCommand(unittest.TestCase):

    def test_auto_gate_runs_without_prompt(self):
        with (
            patch("subprocess.run", return_value=_proc()),
            patch("click.echo"),
            patch("openshard.verification.executor.confirm_or_abort") as mock_confirm,
        ):
            result = _run_verification_plan(_needs_approval_plan(), Path("/tmp"), gate=_gate("auto"))
        mock_confirm.assert_not_called()
        self.assertEqual(result, 0)

    def test_ask_gate_calls_confirm_or_abort(self):
        with (
            patch("subprocess.run", return_value=_proc()),
            patch("click.echo"),
            patch("openshard.verification.executor.confirm_or_abort") as mock_confirm,
        ):
            _run_verification_plan(_needs_approval_plan(), Path("/tmp"), gate=_gate("ask"))
        mock_confirm.assert_called_once()

    def test_smart_gate_with_unknown_command_calls_confirm(self):
        with (
            patch("subprocess.run", return_value=_proc()),
            patch("click.echo"),
            patch("openshard.verification.executor.confirm_or_abort") as mock_confirm,
        ):
            _run_verification_plan(_needs_approval_plan(), Path("/tmp"), gate=_gate("smart"))
        mock_confirm.assert_called_once()

    def test_no_gate_fails_closed_without_prompt(self):
        # Command policy v1: ask with no approver available must not execute.
        with (
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("click.echo"),
            patch("openshard.verification.executor.confirm_or_abort") as mock_confirm,
        ):
            result = _run_verification_plan(_needs_approval_plan(), Path("/tmp"), gate=None)
        mock_confirm.assert_not_called()
        mock_run.assert_not_called()
        self.assertEqual(result, 1)

    def test_needs_approval_argv_passed_to_subprocess(self):
        with (
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("click.echo"),
            patch("openshard.verification.executor.confirm_or_abort"),
        ):
            _run_verification_plan(_needs_approval_plan(), Path("/tmp"), gate=_gate("auto"))
        self.assertEqual(mock_run.call_args.args[0], ["make", "test"])


class TestCommandNotFound(unittest.TestCase):

    def test_not_found_capture_returns_exit_1(self):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("not found")),
            patch("click.echo"),
        ):
            code, output = _run_verification_plan(_safe_plan(), Path("/tmp"), capture=True)
        self.assertEqual(code, 1)
        self.assertIn("not found", output)

    def test_not_found_stream_returns_exit_1(self):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("not found")),
            patch("click.echo") as mock_echo,
        ):
            result = _run_verification_plan(_safe_plan(), Path("/tmp"), capture=False)
        self.assertEqual(result, 1)
        msgs = [str(a) for call in mock_echo.call_args_list for a in call.args]
        assert any("not found" in m for m in msgs)

    def test_not_found_message_does_not_expose_raw_path(self):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("click.echo"),
        ):
            _, output = _run_verification_plan(
                _safe_plan(["/usr/local/bin/pytest"]), Path("/tmp"), capture=True,
                pre_approved_by="test",
            )
        self.assertNotIn("/usr/local/bin/pytest", output)
        self.assertIn("not found", output)


if __name__ == "__main__":
    unittest.main()
