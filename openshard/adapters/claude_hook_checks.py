"""Hook-observed check commands and the ``verification`` block a hook session records.

Each check a captured command ran is remembered with the outcome that is
provably its own (verification/check_command.py decides when the command's
exit status is the check's). The block is built from those checks:
``directly_observed`` invocations whose outcomes are only ever
``agent_reported``. Moved out of adapters/claude_hooks.py unchanged apart
from per-check attribution.
"""

from __future__ import annotations

from typing import Any

from openshard.verification.check_command import item_outcome, suite_supersedes
from openshard.verification.gaming_signals import note_masked_check

MAX_BUFFERED_CHECKS = 20


def _stored_count(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


MAX_CHECK_KEYS = 100  # distinct check commands whose latest outcome is tracked


def record_checks(buf: dict, payload: Any, action: str, outcome: str, at: str) -> None:
    """Record each check one command ran, with the outcome that is provably its own.

    A queue line from before ``check_commands`` existed records the whole
    command as one check with the command's outcome, as it always did.
    """
    items = payload.check_commands
    if items is None:
        _record_check(buf, action, payload.command_kind or "other", status=outcome, at=at,
                      exit_code=payload.command_exit_code, key=payload.command_key)
        return
    for index, item in enumerate(items):
        status, reason = item_outcome(item, outcome)
        key = payload.command_key if len(items) == 1 or not payload.command_key else f"{payload.command_key[:14]}{index:02d}"
        _record_check(buf, item["name"], item["kind"], status=status, at=at,
                      exit_code=payload.command_exit_code if status != "unknown" else None,
                      key=key, unattributed=reason, suite=item.get("suite"))
        note_masked_check(buf, item["name"], item.get("exit_masked"))  # a test line that cannot fail


def _record_check(
    buf: dict, name: str, kind: str, *, status: str, at: str, exit_code: int | None = None,
    key: str | None = None, unattributed: str | None = None, suite: str | None = None,
) -> None:
    """Remember one hook-observed check command with the outcome its hook reported
    (``unknown`` when the hook reported none).

    Every run counts (``checks_total``, ``checks_passed_runs`` /
    ``checks_failed_runs``), and the *latest* outcome per command
    (``check_latest``, keyed by ``command_key``) is what the verification
    status is computed from -- a check that failed and then passed on a
    re-run is passed; the earlier failed run stays listed and counted.

    A passed *full-suite* run also supersedes earlier failed *narrow* runs of
    the same suite (``check_command.suite_supersedes`` has the rule): their
    slots leave ``check_latest``, so the status follows the later full run,
    while their runs stay in ``checks`` and ``checks_failed_runs``. A narrow
    run that fails again afterwards gets a new slot and fails the session.
    """
    buf["checks_total"] = int(buf.get("checks_total") or 0) + 1
    if status in ("passed", "failed"):
        counter = f"checks_{status}_runs"
        buf[counter] = int(buf.get(counter) or 0) + 1
    latest = buf.get("check_latest")
    if not isinstance(latest, dict):
        latest = buf["check_latest"] = {}
    suites = buf.get("check_suites")
    if not isinstance(suites, dict):
        suites = buf["check_suites"] = {}
    slot = key or f"name:{name}"
    if status == "passed" and suite:
        for other, other_suite in list(suites.items()):
            if other != slot and latest.get(other) == "failed" and suite_supersedes(suite, other_suite):
                latest.pop(other, None)
                suites.pop(other, None)
    if slot in latest or len(latest) < MAX_CHECK_KEYS:
        latest.pop(slot, None)  # re-insert: most recently run last
        latest[slot] = status
        if suite:
            suites[slot] = suite
        else:
            suites.pop(slot, None)
    else:
        buf["check_latest_overflow"] = True
    checks = buf.get("checks")
    if not isinstance(checks, list):
        checks = buf["checks"] = []
    if len(checks) < MAX_BUFFERED_CHECKS:
        item: dict[str, Any] = {"name": name, "kind": kind, "status": status, "at": at}
        if exit_code is not None:
            item["exit_code"] = exit_code
        if unattributed:
            # Kept locally; the synced block carries it as an incomplete reason.
            item["outcome_unattributed"] = unattributed
        checks.append(item)


def hook_verification(buf: dict, *, lost: bool) -> dict:
    """The record's ``verification`` block (history/verification.py) for a hook session.

    OpenShard receives the hook events itself, so a check-shaped command's
    invocation is ``directly_observed`` -- but its exit code never is. So an
    observed check is ``unknown`` with ``outcome_not_observed`` (never
    ``passed``); a tool failure the agent's hook reported is ``failed`` and
    ``agent_reported`` (the agent's own signal); and "no check command seen"
    is ``not_run`` only while no capture evidence is known lost -- otherwise
    ``unknown``. See history/verification.py ``hook_verification_source``.
    """
    from openshard.history.verification import (
        MODE_HOOK_TOOL_EVENT,
        REASON_CAPTURE_LOSS,
        REASON_CHECKS_TRUNCATED,
        REASON_OUTCOME_NOT_OBSERVED,
        SOURCE_DIRECTLY_OBSERVED,
        STATUS_FAILED,
        STATUS_PARTIAL,
        STATUS_PASSED,
        STATUS_UNKNOWN,
        build_verification,
        hook_verification_source,
    )

    checks = [c for c in (buf.get("checks") or []) if isinstance(c, dict)]
    total = max(int(buf.get("checks_total") or 0), len(checks))
    incomplete: list[str] = [REASON_CAPTURE_LOSS] if lost else []
    if total == 0 and bool(buf.get("check_command_seen")):
        # Older buffer (rebuilt from a pre-v1 record): a check was seen but
        # its details were never kept.
        return build_verification(
            source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT, status=STATUS_UNKNOWN,
            reason="Check command(s) observed through agent hooks; outcome not observed.",
            incomplete_reasons=[REASON_OUTCOME_NOT_OBSERVED, REASON_CAPTURE_LOSS],
        )
    if total == 0:
        if lost:
            return build_verification(
                source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT, status=STATUS_UNKNOWN,
                reason="No check command observed, but some capture events were lost.",
                incomplete_reasons=incomplete,
            )
        return build_verification(
            source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT, checks_attempted=0,
            reason="No check command observed in the agent's tool events.",
        )
    stamps = [c["at"] for c in checks if isinstance(c.get("at"), str)]
    truncated = total > len(checks)
    if truncated:
        incomplete.append(REASON_CHECKS_TRUNCATED)
    latest = buf.get("check_latest") if isinstance(buf.get("check_latest"), dict) else None
    superseded = 0
    if latest:
        # The status follows each command's *latest* reported outcome, so a
        # red-then-green loop ends green. ``checks_attempted`` counts every
        # run; ``checks_passed`` / ``checks_failed`` count commands by their
        # latest outcome (a passed status with a failed count is inconsistent
        # by contract). Earlier failed runs stay in ``checks`` and the reason.
        outcomes = list(latest.values())
        latest_failed = outcomes.count("failed")
        latest_passed = outcomes.count("passed")
        passed, failed = latest_passed, latest_failed
        unknown = len(outcomes) - latest_failed - latest_passed + (1 if buf.get("check_latest_overflow") else 0)
        superseded = _stored_count(buf.get("checks_failed_runs")) if not latest_failed else 0
    else:
        # A buffer rebuilt from an older record: outcomes per run only.
        failed = sum(1 for c in checks if c.get("status") == "failed")
        passed = sum(1 for c in checks if c.get("status") == "passed")
        latest_failed, latest_passed = failed, passed
        # Checks past the buffer bound have no recorded outcome, so they count as unknown.
        unknown = total - passed - failed
    if latest_failed:
        status = STATUS_FAILED
    elif latest_passed and not unknown:
        status = STATUS_PASSED
    elif latest_passed:
        status = STATUS_PARTIAL
    else:
        status = STATUS_UNKNOWN
    if unknown:
        incomplete.append(REASON_OUTCOME_NOT_OBSERVED)
    unattributed = sorted({c["outcome_unattributed"] for c in checks if isinstance(c.get("outcome_unattributed"), str)})
    incomplete.extend(unattributed)
    reason = _reason(latest_failed, latest_passed, unknown, superseded, bool(unattributed))
    return build_verification(
        source=hook_verification_source(status, outcome_reported=bool(passed or failed)),
        observation_mode=MODE_HOOK_TOOL_EVENT,
        checks=[
            {"name": c.get("name"), "kind": c.get("kind"), "status": c.get("status"), "exit_code": c.get("exit_code")}
            for c in checks
        ],
        status=status,
        checks_attempted=total,
        checks_passed=passed,
        checks_failed=failed,
        checks_skipped=0,
        started_at=min(stamps) if stamps else None,
        exit_code=_single_exit_code(checks) if total == 1 else None,
        reason=reason,
        incomplete_reasons=incomplete,
    )


def _single_exit_code(checks: list[dict]) -> int | None:
    code = checks[0].get("exit_code") if checks else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None



def _reason(latest_failed: int, latest_passed: int, unknown: int, superseded: int, unattributed: bool) -> str:
    """The block's plain-language reason for its status."""
    if latest_failed:
        reason = "The agent's hook reported a check command as failed; OpenShard did not run it."
    elif superseded and latest_passed and not unknown:
        reason = (f"The agent's hook reported {superseded} failed check run(s) that passed on a later re-run "
                  "or a later full-suite run of the same tests; OpenShard did not run them.")
    elif latest_passed and not unknown:
        reason = "The agent's hook reported the check command outcome(s); OpenShard did not run them."
    elif latest_passed:
        reason = "The agent's hook reported some check outcomes; the rest were not observed."
    else:
        reason = "Check command(s) observed through agent hooks; outcome not observed."
    if unattributed:
        reason += (" Some reported outcomes belonged to another program in the same command line,"
                   " so those checks are unknown.")
    return reason
