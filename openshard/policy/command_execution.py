"""Enforcing policy gate for OpenShard-controlled command execution (v1).

Flow per command: proposal -> policy evaluation -> allow / ask / deny ->
approval (ask only) -> execution -> recorded outcome. Allowed != executed and
executed != verified: this module never claims verification, whatever the
exit code. Commands are argv lists and are never run through a shell.

Classification is reused from ``openshard.verification.plan`` (safe /
needs_approval / blocked) and mapped onto the shared PolicyDecision
primitives; there is no second rule table.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from openshard.policy.decision import (
    PolicyDecision,
    make_allow,
    make_ask,
    make_deny,
)
from openshard.verification.plan import (
    CommandSafety,
    VerificationSource,
    classify_command_safety,
)

ACTION_COMMAND_EXEC = "command_exec"
SOURCE = "command_policy"

# approver(argv, decision) -> (granted, approval_source)
CommandApprover = Callable[[list[str], PolicyDecision], tuple[bool, str]]

APPROVAL_NOT_REQUIRED = "not_required"
APPROVAL_GRANTED = "granted"
APPROVAL_DENIED = "denied"
APPROVAL_UNAVAILABLE = "unavailable"  # ask, but nobody could be asked
APPROVAL_ERROR = "error"  # approver raised; treated as not granted

_SAFETY_TO_DECISION = {
    CommandSafety.safe: "allow",
    CommandSafety.needs_approval: "ask",
    CommandSafety.blocked: "deny",
}

_RANK = {"allow": 0, "ask": 1, "deny": 2}

# cmd.exe re-parses arguments of .bat/.cmd targets, so these are injection vectors.
_CMD_EXE_SUFFIXES = (".bat", ".cmd")
_CMD_EXE_META = frozenset('&|<>^%"!')


def command_label(argv: list[str], limit: int = 200) -> str:
    """Path-free, secret-light display form: basenames only, length-capped."""
    def _base(tok: str) -> str:
        if "/" not in tok and "\\" not in tok:
            return tok
        name = min((PureWindowsPath(tok).name, PurePosixPath(tok).name), key=len)
        return name or tok

    return " ".join(_base(t) for t in argv).replace("\n", " ").replace("\r", " ")[:limit]


def _shape_problem(argv: object) -> str | None:
    if not isinstance(argv, list) or not argv:
        return "argv must be a non-empty list"
    if not all(isinstance(t, str) for t in argv):
        return "argv tokens must be strings"
    if not argv[0].strip():
        return "empty executable"
    for tok in argv:
        if "\x00" in tok or "\n" in tok or "\r" in tok:
            return "control character in argv token"
    if argv[0].lower().rstrip(" .").endswith(_CMD_EXE_SUFFIXES) and any(
        c in _CMD_EXE_META for tok in argv[1:] for c in tok
    ):
        return "cmd.exe metacharacter in argument to batch script"
    return None


def evaluate_command(
    argv: list[str],
    declared_safety: CommandSafety | None = None,
) -> PolicyDecision:
    """Evaluate a proposed command. Pure; no I/O.

    *declared_safety* is a classification made earlier (e.g. stored on a
    VerificationCommand). The stricter of it and a fresh classification wins,
    so a stale or forged "safe" label cannot loosen enforcement.
    """
    problem = _shape_problem(argv)
    if problem is not None:
        return make_deny(
            ACTION_COMMAND_EXEC, None, f"malformed command: {problem}",
            source=SOURCE, severity="high",
        )

    safety, reason = classify_command_safety(argv, VerificationSource.config)
    fresh = _decision_for(safety, reason, argv)
    if declared_safety is None:
        return fresh
    declared = _decision_for(declared_safety, f"declared {declared_safety.value}", argv)
    # Stricter wins; on a tie the fresh decision (deterministic reason) is kept.
    return declared if _RANK[declared.decision] > _RANK[fresh.decision] else fresh


def _decision_for(safety: CommandSafety, reason: str, argv: list[str]) -> PolicyDecision:
    label = command_label(argv)
    kind = _SAFETY_TO_DECISION[safety]
    if kind == "allow":
        return make_allow(ACTION_COMMAND_EXEC, label, reason, source=SOURCE)
    if kind == "ask":
        return make_ask(ACTION_COMMAND_EXEC, label, reason, source=SOURCE)
    return make_deny(ACTION_COMMAND_EXEC, label, reason, source=SOURCE, severity="high")


@dataclass
class CommandOutcome:
    """What was proposed, decided, approved and actually done.

    Raw argv is kept in memory only; ``to_record`` never serializes it.
    """

    argv: list[str] = field(repr=False)
    decision: PolicyDecision
    approval_status: str = APPROVAL_NOT_REQUIRED
    approval_source: str | None = None
    executed: bool = False
    exit_code: int | None = None
    error: str | None = None  # start failure / timeout; nothing beyond a short tag
    output: str = field(default="", repr=False)
    # Executing a command is not verification; that is a separate step.
    verification: str = "not_run"

    @property
    def permitted(self) -> bool:
        return self.decision.decision == "allow" or self.approval_status == APPROVAL_GRANTED

    def to_record(self) -> dict[str, Any]:
        d = self.decision
        return {
            "action": d.action,
            "command": d.resource,
            "decision": d.decision,
            "reason": d.reason,
            "source": d.source,
            "approval_required": d.approval_required,
            "approval_status": self.approval_status,
            "approval_source": self.approval_source,
            "executed": self.executed,
            "exit_code": self.exit_code,
            "error": self.error,
            "verification": self.verification,
        }


def authorize_command(
    argv: list[str],
    approver: CommandApprover | None = None,
    declared_safety: CommandSafety | None = None,
) -> CommandOutcome:
    """Decide whether *argv* may run. Never executes anything.

    Deny is final: an approver is not consulted. Ask without an approver, with
    a refusing approver, or with one that raises, does not proceed.
    """
    decision = evaluate_command(argv, declared_safety)
    outcome = CommandOutcome(argv=list(argv) if isinstance(argv, list) else [], decision=decision)
    if decision.decision == "allow":
        return outcome
    if decision.decision != "ask":  # deny, or anything unrecognised: fail closed
        outcome.approval_status = APPROVAL_NOT_REQUIRED
        return outcome
    if approver is None:
        outcome.approval_status = APPROVAL_UNAVAILABLE
        return outcome
    try:
        granted, source = approver(list(argv), decision)
    except Exception:
        outcome.approval_status = APPROVAL_ERROR
        outcome.approval_source = "approver_error"
        return outcome
    outcome.approval_source = source
    outcome.approval_status = APPROVAL_GRANTED if granted is True else APPROVAL_DENIED
    decision.approval_granted = granted is True
    return outcome


def execute_authorized(
    outcome: CommandOutcome,
    cwd: Path,
    *,
    capture: bool = False,
    timeout: float | None = None,
    runner: Callable[..., Any] | None = None,
) -> CommandOutcome:
    """Run an already-authorized outcome. Refuses (does nothing) if not permitted."""
    if not outcome.permitted or outcome.executed:
        return outcome
    run = runner or subprocess.run
    kwargs: dict[str, Any] = {"cwd": cwd, "timeout": timeout, "shell": False}
    if capture:
        kwargs.update(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
    try:
        proc = run(list(outcome.argv), **kwargs)
    except subprocess.TimeoutExpired:
        outcome.executed = True  # process was started, then killed
        outcome.error = "timeout"
        return outcome
    except OSError as exc:
        outcome.error = f"could not start ({type(exc).__name__})"
        return outcome
    outcome.executed = True
    code = getattr(proc, "returncode", None)
    outcome.exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
    stdout = getattr(proc, "stdout", None)
    outcome.output = stdout if isinstance(stdout, str) else ""
    return outcome


def run_gated_command(
    argv: list[str],
    cwd: Path,
    *,
    approver: CommandApprover | None = None,
    declared_safety: CommandSafety | None = None,
    capture: bool = False,
    timeout: float | None = None,
    runner: Callable[..., Any] | None = None,
) -> CommandOutcome:
    """Authorize then execute *argv* (no shell). Executes only if permitted."""
    outcome = authorize_command(argv, approver, declared_safety)
    return execute_authorized(outcome, cwd, capture=capture, timeout=timeout, runner=runner)
