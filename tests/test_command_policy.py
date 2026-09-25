from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openshard.policy.command_execution import (
    APPROVAL_DENIED,
    APPROVAL_ERROR,
    APPROVAL_GRANTED,
    APPROVAL_NOT_REQUIRED,
    APPROVAL_UNAVAILABLE,
    authorize_command,
    evaluate_command,
    run_gated_command,
)
from openshard.verification.executor import run_verification_plan
from openshard.verification.plan import (
    CommandSafety,
    VerificationCommand,
    VerificationKind,
    VerificationPlan,
    VerificationSource,
    classify_command_safety,
)

SAFE = [sys.executable, "-m", "pytest", "-q"]
ASK = ["make", "build"]
DENY = ["git", "push", "origin", "main"]


class Runner:
    def __init__(self, returncode: int = 0, stdout: str = "", raises: Exception | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode, self.stdout, self.raises = returncode, stdout, raises

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raises:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout)


def _plan(argv: list[str], safety: CommandSafety | None = None) -> VerificationPlan:
    s, r = classify_command_safety(argv, VerificationSource.config)
    return VerificationPlan([VerificationCommand(
        "verification", argv, VerificationKind.unknown, VerificationSource.config,
        safety or s, r,
    )])


# --- decision semantics -----------------------------------------------------

def test_allow_command_runs_without_approval(tmp_path: Path):
    r = Runner()
    o = run_gated_command(SAFE, tmp_path, runner=r)
    assert o.decision.decision == "allow"
    assert o.approval_status == APPROVAL_NOT_REQUIRED
    assert o.executed and o.exit_code == 0
    assert r.calls[0][1]["shell"] is False


def test_ask_granted_runs(tmp_path: Path):
    r = Runner()
    o = run_gated_command(ASK, tmp_path, approver=lambda a, d: (True, "interactive_prompt"), runner=r)
    assert o.decision.decision == "ask" and o.decision.approval_required
    assert o.approval_status == APPROVAL_GRANTED
    assert o.approval_source == "interactive_prompt"
    assert o.executed and len(r.calls) == 1


def test_ask_denied_does_not_run(tmp_path: Path):
    r = Runner()
    o = run_gated_command(ASK, tmp_path, approver=lambda a, d: (False, "interactive_prompt"), runner=r)
    assert o.approval_status == APPROVAL_DENIED
    assert not o.executed and r.calls == []


def test_ask_without_approver_fails_closed(tmp_path: Path):
    r = Runner()
    o = run_gated_command(ASK, tmp_path, runner=r)
    assert o.approval_status == APPROVAL_UNAVAILABLE
    assert not o.executed and r.calls == []


def test_approver_exception_fails_closed(tmp_path: Path):
    r = Runner()

    def boom(a, d):
        raise RuntimeError("prompt crashed")

    o = run_gated_command(ASK, tmp_path, approver=boom, runner=r)
    assert o.approval_status == APPROVAL_ERROR
    assert not o.executed and r.calls == []


@pytest.mark.parametrize("truthy", [1, "yes", object()])
def test_only_literal_true_grants(tmp_path: Path, truthy):
    r = Runner()
    o = run_gated_command(ASK, tmp_path, approver=lambda a, d: (truthy, "x"), runner=r)
    assert o.approval_status == APPROVAL_DENIED and r.calls == []


def test_deny_never_consults_approver_and_never_runs(tmp_path: Path):
    asked: list[int] = []
    r = Runner()
    o = run_gated_command(DENY, tmp_path, approver=lambda a, d: asked.append(1) or (True, "x"), runner=r)
    assert o.decision.decision == "deny"
    assert asked == [] and not o.executed and r.calls == []


def test_declared_safe_cannot_loosen_fresh_deny(tmp_path: Path):
    r = Runner()
    o = run_gated_command(DENY, tmp_path, declared_safety=CommandSafety.safe, runner=r)
    assert o.decision.decision == "deny" and r.calls == []


def test_declared_blocked_tightens_safe_command(tmp_path: Path):
    o = authorize_command(SAFE, declared_safety=CommandSafety.blocked)
    assert o.decision.decision == "deny"


@pytest.mark.parametrize("bad", [[], [""], ["a\x00b"], ["pytest", "x\ny"], "pytest -q", [1, 2]])
def test_malformed_argv_denied(bad):
    assert evaluate_command(bad).decision == "deny"  # type: ignore[arg-type]


def test_batch_script_with_cmd_metachar_denied():
    assert evaluate_command(["tool.cmd", "a&calc"]).decision == "deny"


# --- executed != verified ---------------------------------------------------

def test_successful_execution_does_not_imply_verification(tmp_path: Path):
    o = run_gated_command(SAFE, tmp_path, runner=Runner(returncode=0))
    assert o.executed and o.exit_code == 0
    assert o.verification == "not_run"
    assert o.to_record()["verification"] == "not_run"


def test_execution_failure_is_recorded_not_hidden(tmp_path: Path):
    o = run_gated_command(SAFE, tmp_path, runner=Runner(raises=FileNotFoundError("x")))
    assert not o.executed and o.error
    o = run_gated_command(SAFE, tmp_path, runner=Runner(raises=subprocess.TimeoutExpired("x", 1)))
    assert o.executed and o.error == "timeout" and o.exit_code is None


def test_record_has_no_raw_argv_or_paths(tmp_path: Path):
    secret = str(tmp_path / "s3cr3t" / "tool")
    o = run_gated_command([secret, "--token=abc"], tmp_path, runner=Runner())
    text = repr(o.to_record())
    assert "s3cr3t" not in text and "argv" not in o.to_record()


# --- hardened classification (strengthening only) ---------------------------

@pytest.mark.parametrize("argv", [
    ["git", "-C", "..", "push"],
    ["git", "-c", "core.x=y", "push"],
    ["git", "--no-pager", "clean", "-fd"],
    ["git", "-C", ".", "reset", "--hard"],
    ["curl.exe", "http://x"],
    ["RM.EXE", "-rf", "x"],
    ["terraform.exe", "apply"],
])
def test_blocked_forms_that_used_to_slip_past(argv):
    assert classify_command_safety(argv, VerificationSource.config)[0] == CommandSafety.blocked


@pytest.mark.parametrize("argv", [
    ["rg", "--pre", "sh", "x"],
    ["rg", "--pre=sh", "x"],
    ["git", "diff", "--output=out.txt"],
    ["git", "diff", "--ext-diff"],
])
def test_safe_tools_with_exec_flags_need_approval(argv):
    assert classify_command_safety(argv, VerificationSource.config)[0] == CommandSafety.needs_approval


@pytest.mark.parametrize("argv,expected", [
    (["git", "status"], CommandSafety.safe),
    (["git", "diff"], CommandSafety.safe),
    ([sys.executable, "-m", "pytest"], CommandSafety.safe),
    (["rg", "foo"], CommandSafety.safe),
    (["git", "push"], CommandSafety.blocked),
    (["terraform", "apply"], CommandSafety.blocked),
    (["kubectl", "delete", "x"], CommandSafety.blocked),
    (["git", "reset", "--hard"], CommandSafety.blocked),
    (["make"], CommandSafety.needs_approval),
    (["unknown-tool"], CommandSafety.needs_approval),
])
def test_existing_classifications_unchanged(argv, expected):
    assert classify_command_safety(argv, VerificationSource.config)[0] == expected


# --- run_verification_plan integration --------------------------------------

def _patch_run(monkeypatch, runner):
    monkeypatch.setattr("openshard.policy.command_execution.subprocess.run", runner)


def test_plan_blocked_never_runs_even_with_pre_approval(monkeypatch, tmp_path: Path):
    r = Runner()
    _patch_run(monkeypatch, r)
    code, out = run_verification_plan(_plan(DENY), tmp_path, capture=True, pre_approved_by="anything")
    assert code == 1 and "blocked" in out and r.calls == []


def test_plan_needs_approval_without_gate_fails_closed(monkeypatch, tmp_path: Path):
    r = Runner()
    _patch_run(monkeypatch, r)
    sink: list = []
    code, out = run_verification_plan(_plan(ASK), tmp_path, capture=True, outcome_sink=sink)
    assert code == 1 and r.calls == []
    assert sink[0].approval_status == APPROVAL_UNAVAILABLE and not sink[0].executed


def test_plan_stale_safe_label_is_not_trusted(monkeypatch, tmp_path: Path):
    r = Runner()
    _patch_run(monkeypatch, r)
    code, _ = run_verification_plan(_plan(ASK, CommandSafety.safe), tmp_path, capture=True)
    assert code == 1 and r.calls == []


def test_plan_explicit_pre_approval_runs_and_is_recorded(monkeypatch, tmp_path: Path):
    r = Runner(stdout="ok")
    _patch_run(monkeypatch, r)
    sink: list = []
    code, out = run_verification_plan(
        _plan(ASK), tmp_path, capture=True, pre_approved_by="eval_task_config", outcome_sink=sink,
    )
    assert (code, out) == (0, "ok")
    assert sink[0].approval_source == "eval_task_config" and sink[0].executed
    assert sink[0].verification == "not_run"


def test_plan_gate_not_required_runs_gate_required_prompts(monkeypatch, tmp_path: Path):
    r = Runner()
    _patch_run(monkeypatch, r)
    auto = SimpleNamespace(check_shell_command=lambda c: SimpleNamespace(required=False, reason=""))
    assert run_verification_plan(_plan(ASK), tmp_path, gate=auto, capture=True)[0] == 0

    asked = SimpleNamespace(check_shell_command=lambda c: SimpleNamespace(required=True, reason="r"))
    monkeypatch.setattr("openshard.verification.executor.confirm_or_abort", lambda reason: (_ for _ in ()).throw(SystemExit(0)))
    r.calls.clear()
    with pytest.raises(SystemExit):
        run_verification_plan(_plan(ASK), tmp_path, gate=asked, capture=True)
    assert r.calls == []


def test_plan_gate_error_fails_closed(monkeypatch, tmp_path: Path):
    r = Runner()
    _patch_run(monkeypatch, r)

    def boom(c):
        raise RuntimeError("gate broke")

    gate = SimpleNamespace(check_shell_command=boom)
    code, _ = run_verification_plan(_plan(ASK), tmp_path, gate=gate, capture=True)
    assert code == 1 and r.calls == []


def test_plan_safe_runs_and_old_shape_preserved(monkeypatch, tmp_path: Path):
    r = Runner(returncode=3, stdout="fail")
    _patch_run(monkeypatch, r)
    assert run_verification_plan(_plan(SAFE), tmp_path, capture=True) == (3, "fail")
    assert run_verification_plan(VerificationPlan(), tmp_path) == 0


def test_old_records_without_command_fields_still_load():
    from openshard.policy.decision import PolicyDecision

    old = {"decision_id": "x", "action": "shell", "resource": None, "decision": "allow"}
    assert PolicyDecision(**old).approval_granted is None


# --- review follow-ups ------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["git", "--config-env", "core.x=HOME", "push", "origin"],
    ["terraform", "-chdir=x", "apply"],
    ["kubectl", "-n", "p", "delete", "pod", "x"],
    ["npm", "--prefix", ".", "publish"],
])
def test_blocked_subcommand_after_global_options(argv):
    assert classify_command_safety(argv, VerificationSource.config)[0] == CommandSafety.blocked


@pytest.mark.parametrize("argv", [
    ["go", "test", "-exec", "calc"],
    ["go", "test", "-toolexec=calc"],
    ["cargo", "test", "--config", "x"],
    ["pytest", "-p", "evil"],
    ["pytest", "-pevil"],
    [sys.executable, "-m", "pytest", "--basetemp=out"],
    ["git", "diff", "--no-index", "a", "b"],
])
def test_test_runners_with_exec_flags_need_approval(argv):
    assert classify_command_safety(argv, VerificationSource.config)[0] == CommandSafety.needs_approval


def test_pytest_plugin_disable_stays_safe():
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    assert classify_command_safety(argv, VerificationSource.config)[0] == CommandSafety.safe


def test_reason_is_deterministic_when_declared_equals_fresh():
    reasons = {evaluate_command(["rm", "x"], CommandSafety.blocked).reason for _ in range(20)}
    assert reasons == {"blocked executable: 'rm'"}


@pytest.mark.parametrize("exe", ["x.bat.", "x.BAT", "x.cmd "])
def test_batch_extension_variants_denied(exe):
    assert evaluate_command([exe, "&calc"]).decision == "deny"


def test_native_tool_reports_refusal_as_not_attempted(monkeypatch, tmp_path: Path):
    from openshard.native.tools import _exec_run_verification

    plan = _plan(ASK)
    plan.commands[0].safety = CommandSafety.safe  # stale label; fresh check asks
    monkeypatch.setattr("openshard.analysis.repo.analyze_repo", lambda p: None)
    monkeypatch.setattr("openshard.verification.plan.build_verification_plan", lambda c, f: plan)
    r = _exec_run_verification(tmp_path, approved=False)
    assert r.ok is False and r.metadata["attempted"] is False
