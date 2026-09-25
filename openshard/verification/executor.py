from __future__ import annotations

import subprocess  # noqa: F401  (kept: tests and callers patch executor.subprocess)
from pathlib import Path

import click

from openshard.policy.command_execution import (
    APPROVAL_DENIED,
    CommandOutcome,
    authorize_command,
    execute_authorized,
)
from openshard.policy.decision import PolicyDecision
from openshard.verification.plan import VerificationPlan

_DEFAULT_TIMEOUT: float = 120.0


def confirm_or_abort(reason: str) -> None:
    click.echo(f"\n[gate] {reason}")
    if not click.confirm("Proceed?", default=False):
        click.echo("Aborted!")
        raise SystemExit(0)


def _gate_approver(gate, pre_approved_by: str | None):
    """Adapt the legacy approval gate / an explicit pre-approval to a CommandApprover.

    Returns None (no approver -> ask fails closed) when neither exists.
    """
    if gate is not None:
        def _approve(argv: list[str], decision: PolicyDecision) -> tuple[bool, str]:
            gd = gate.check_shell_command(" ".join(argv))
            if not gd.required:
                return True, "approval_mode_policy"
            confirm_or_abort(gd.reason)  # SystemExit on decline: nothing runs
            return True, "interactive_prompt"
        return _approve
    if pre_approved_by:
        return lambda argv, decision: (True, pre_approved_by)
    return None


def run_verification_plan(
    plan: VerificationPlan,
    cwd: Path,
    gate=None,
    label: str = "[verify]",
    capture: bool = False,
    detail: str = "default",
    timeout: float = _DEFAULT_TIMEOUT,
    pre_approved_by: str | None = None,
    outcome_sink: list[CommandOutcome] | None = None,
) -> int | tuple[int, str]:
    """Execute the first VerificationCommand from *plan* through the command policy gate.

    - No commands  -> returns 0; announces nothing-to-run (matches old behaviour).
    - deny (blocked / malformed) -> never runs, returns 1; approval cannot override.
    - ask (needs_approval) -> runs only if *gate* approves or the caller names an
      explicit *pre_approved_by* source. Neither present: fails closed, returns 1.
    - allow (safe) -> executes argv directly (never shell=True).

    outcome_sink, if given, receives the CommandOutcome (proposal, decision,
    approval status, whether it executed). Executing is not verifying.

    capture=False: streams output live, returns int exit code.
    capture=True: captures silently, returns (exit_code, output).

    timeout: seconds before the subprocess is killed (default 120). Callers that
    depend on captured output still receive a string on timeout; raw stdout is
    never stored in run history or NativeStepEvent metadata.
    """
    if not plan.has_commands:
        if not capture:
            click.echo(f"  {label} no test command detected")
        return (0, "") if capture else 0

    cmd = plan.commands[0]

    def _fail(msg: str) -> int | tuple[int, str]:
        if not capture:
            click.echo(msg)
        return (1, msg) if capture else 1

    # Authorize first so the "running" line is only shown for commands that will run.
    outcome = authorize_command(
        cmd.argv, _gate_approver(gate, pre_approved_by), declared_safety=cmd.safety
    )
    if outcome_sink is not None:
        outcome_sink.append(outcome)
    if not outcome.permitted:
        if outcome.decision.decision == "deny":
            return _fail(f"  {label} blocked: {outcome.decision.reason}")
        if outcome.approval_status == APPROVAL_DENIED:
            return _fail(f"  {label} not approved: {outcome.decision.reason}")
        return _fail(f"  {label} needs approval, none available; not run: {outcome.decision.reason}")

    if not capture:
        click.echo(f"  {label} running: {' '.join(cmd.argv)}")

    execute_authorized(outcome, cwd, capture=capture, timeout=timeout)
    if outcome.error == "timeout":
        return _fail(f"  {label} timed out after {timeout}s")
    if not outcome.executed:
        return _fail(f"  {label} not found" if outcome.error else f"  {label} not run")

    code = outcome.exit_code if outcome.exit_code is not None else 1
    if capture:
        return code, outcome.output
    if code == 0:
        click.echo(f"  {label} passed")
    else:
        click.echo(f"  {label} failed (exit code {code})")
    return code
