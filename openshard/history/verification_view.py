"""The human-facing verification view of a Receipt: four separate facts, plus history.

An externally captured session used to read as one blended grey state.
``verification_truth`` already decides what OpenShard can claim; this module
only lays that claim out so four questions are never answered by one word:

``work``
    Did the agent's session finish? (turn/session state; says nothing about
    correctness)
``verification``
    What did the checks say, in counts: passed / failed / unknown.
``evidence``
    Who vouches for that outcome, and for which commit: independent CI,
    OpenShard itself, or only the agent.
``capture``
    How complete OpenShard's record of the session is. A partial capture
    does not grey out a verification OpenShard observed itself, and a
    complete capture does not upgrade an agent's claim.

``original`` keeps the session's own record when later evidence describes
the current state, and ``history`` lists every piece of evidence oldest
first, so a stronger later result never reads as if it had been known at
the time.

Pure projection over ``interpret_receipt``: never raises, reads no files,
emits only static words, counts, check names the capture already scrubbed,
and commit SHAs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openshard.history.capture_completeness import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_INCOMPLETE,
    REASON_SESSION_END_NOT_OBSERVED,
    gaps_display,
)
from openshard.history.verification import (
    SOURCE_AGENT_REPORTED,
    SOURCE_GIT_VERIFIED,
    SOURCE_INDEPENDENTLY_VERIFIED,
)
from openshard.history.verification_truth import (
    BASIS_CI,
    BASIS_POST_SESSION,
    BASIS_SESSION,
    STATE_AGENT_REPORTED_FAILED,
    STATE_AGENT_REPORTED_PARTIAL,
    STATE_AGENT_REPORTED_PASSED,
    STATE_ATTEMPTED_UNVERIFIED,
    STATE_MANUAL_REVIEW,
    STATE_NOT_RUN,
    STATE_SKIPPED,
    STATE_VERIFIED_FAILED,
    STATE_VERIFIED_PARTIAL,
    STATE_VERIFIED_PASSED,
    VerificationTruth,
    counts_phrase,
    interpret_receipt,
)

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt

VIEW_VERSION = 1

WORK_COMPLETED = "completed"
WORK_TURN_COMPLETED = "turn_completed"
WORK_IN_PROGRESS = "in_progress"
WORK_ENDED_NO_TURN = "ended_no_turn"
WORK_ENDED_WITHOUT_FINAL_EVENT = "ended_without_final_event"

_WORK_LABELS: dict[str, str] = {
    WORK_COMPLETED: "Completed",
    WORK_TURN_COMPLETED: "Turn completed (session still open)",
    WORK_IN_PROGRESS: "In progress",
    WORK_ENDED_NO_TURN: "Session ended (no turn observed)",
    WORK_ENDED_WITHOUT_FINAL_EVENT: "Session ended without final event",
}

# Upper case is reserved for an outcome OpenShard or an independent system
# observed; an agent's own account stays in ordinary case.
_HEADLINES: dict[str, str] = {
    STATE_VERIFIED_PASSED: "PASSED",
    STATE_VERIFIED_FAILED: "FAILED",
    STATE_VERIFIED_PARTIAL: "PARTIAL",
    STATE_AGENT_REPORTED_PASSED: "Passed",
    STATE_AGENT_REPORTED_FAILED: "Failed",
    STATE_AGENT_REPORTED_PARTIAL: "Partial",
    STATE_ATTEMPTED_UNVERIFIED: "Unknown (check command seen; outcome not observed)",
    STATE_MANUAL_REVIEW: "Manual review required",
    STATE_SKIPPED: "Skipped",
    STATE_NOT_RUN: "Not run",
}

_HISTORY_WHO: dict[str, str] = {"session": "Session", "rerun": "OpenShard re-run", "ci": "Independent CI"}
_SOURCE_WORDS: dict[str, str] = {
    SOURCE_AGENT_REPORTED: "agent reported",
    "directly_observed": "OpenShard observed",
    SOURCE_GIT_VERIFIED: "git verified",
    SOURCE_INDEPENDENTLY_VERIFIED: "independently verified",
}


def _session(receipt: ShardReceipt) -> dict[str, Any] | None:
    evidence = getattr(receipt, "recorded_evidence", None)
    session = evidence.get("session") if isinstance(evidence, dict) else None
    return session if isinstance(session, dict) else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _work(session: dict[str, Any] | None, completeness: dict[str, Any]) -> dict[str, Any] | None:
    if session is None:
        return None
    reasons = completeness.get("reasons")
    swept = any(
        isinstance(r, dict) and r.get("kind") == REASON_SESSION_END_NOT_OBSERVED
        for r in (reasons if isinstance(reasons, list) else [])
    )
    turns = _count(session.get("turn_count")) or 0
    if session.get("ended") is True:
        state = WORK_COMPLETED if turns else WORK_ENDED_NO_TURN
    elif swept:
        # The session went quiet and was closed by the stale sweep: no end
        # event was seen, and none is invented.
        state = WORK_ENDED_WITHOUT_FINAL_EVENT
    else:
        state = WORK_TURN_COMPLETED if turns else WORK_IN_PROGRESS
    return {"state": state, "label": _WORK_LABELS[state]}


def _evidence(truth: VerificationTruth) -> dict[str, Any]:
    sha = truth.artifact_sha
    if truth.basis == BASIS_CI:
        label = "Independent CI"
    elif truth.basis == BASIS_POST_SESSION:
        label = "OpenShard verified"
    elif truth.authority == SOURCE_INDEPENDENTLY_VERIFIED:
        label = "Independently verified"
    elif truth.authority == SOURCE_GIT_VERIFIED:
        label = "Git verified"
    elif truth.authority == SOURCE_AGENT_REPORTED:
        label = "Agent reported (not verified by OpenShard)"
    elif truth.observed:
        label = "OpenShard verified"
    else:
        label = "None"
    bound: bool | None = bool(sha) if truth.basis in (BASIS_CI, BASIS_POST_SESSION) else None
    return {"source": truth.authority, "basis": truth.basis, "label": label, "artifact_sha": sha, "bound": bound}


def _capture(completeness: dict[str, Any]) -> dict[str, Any]:
    """Capture depth and completeness as one row that agrees with the receipt's ``Capture`` / ``Gaps`` rows.

    ``Partial`` when OpenShard only observed the run through an agent's
    hooks (depth) or when evidence is known lost (completeness); ``Complete``
    only for a run OpenShard executed itself with nothing known missing.
    """
    status = str(completeness.get("status") or "unknown")
    depth = str(completeness.get("depth") or "unknown")
    gaps: str | None
    if status == COMPLETENESS_INCOMPLETE:
        label, gaps = "Partial", gaps_display(completeness)
    elif depth == "partial":
        label = "Partial"
        gaps = "observed through agent hooks; no known gaps" if status == COMPLETENESS_COMPLETE else None
    elif depth == "full" and status == COMPLETENESS_COMPLETE:
        label, gaps = "Complete", None
    else:
        label, gaps = "Unknown", None
    return {"status": status, "depth": depth, "label": label, "gaps": gaps}


def build_verification_view(receipt: ShardReceipt) -> dict[str, Any]:
    """Work / verification / evidence / capture for *receipt*, with the evidence history."""
    truth = interpret_receipt(receipt)
    raw_completeness = getattr(receipt, "capture_completeness", None)
    completeness: dict[str, Any] = raw_completeness if isinstance(raw_completeness, dict) else {}
    session = _session(receipt)

    headline = _HEADLINES.get(truth.state, "Not recorded")
    if truth.state == STATE_VERIFIED_PASSED and truth.authority == SOURCE_INDEPENDENTLY_VERIFIED:
        headline = "VERIFIED"
    attempted, passed, failed = truth.checks_attempted, truth.checks_passed, truth.checks_failed
    unknown = max(attempted - (passed or 0) - (failed or 0), 0) if attempted else None

    original: dict[str, Any] | None = None
    if truth.basis != BASIS_SESSION and truth.history and truth.history[0].get("kind") == "session":
        first = truth.history[0]
        original = {
            "status": first.get("status"),
            "source": first.get("source"),
            "counts_label": counts_phrase(
                first.get("checks_passed"), first.get("checks_failed"), first.get("checks_attempted"),
            ),
        }

    activity: dict[str, Any] | None = None
    if session is not None and _count(session.get("tool_call_count")) is not None:
        activity = {
            "tool_calls": _count(session.get("tool_call_count")),
            "tool_failures": _count(session.get("tool_failure_count")),
        }

    return {
        "version": VIEW_VERSION,
        "work": _work(session, completeness),
        "verification": {
            "state": truth.state,
            "headline": headline,
            # True only for a pass OpenShard or an independent system observed.
            "confirmed_pass": truth.state == STATE_VERIFIED_PASSED,
            "checks_passed": passed,
            "checks_failed": failed,
            "checks_unknown": unknown,
            "checks_attempted": attempted,
            "counts_label": counts_phrase(passed, failed, attempted),
            "failed_checks": list(truth.failed_checks),
        },
        "evidence": _evidence(truth),
        "capture": _capture(completeness),
        "activity": activity,
        "original": original,
        "history": [dict(item) for item in truth.history],
    }


def _history_line(item: dict[str, Any]) -> str:
    who = _HISTORY_WHO.get(str(item.get("kind")), "Evidence")
    if item.get("kind") == "session":
        who = f"Session ({_SOURCE_WORDS.get(str(item.get('source')), 'not observed')})"
    text = f"{who}: {str(item.get('status') or 'unknown').replace('_', ' ')}"
    counts = counts_phrase(item.get("checks_passed"), item.get("checks_failed"), item.get("checks_attempted"))
    if counts:
        text += f", {counts}"
    sha = item.get("artifact_sha")
    if isinstance(sha, str) and sha:
        text += f" @ {sha[:12]}"
    at = item.get("at")
    if isinstance(at, str) and at:
        text = f"{at[:16].replace('T', ' ')}Z  {text}"
    return text


def render_verification_view(view: dict[str, Any], *, indent: str = "  ", width: int = 14) -> list[str]:
    """The view as aligned text rows. Activity failures are listed apart from check failures."""

    def row(label: str, value: str) -> str:
        return f"{indent}{label:<{width}}{value}"

    lines = [f"{indent}VERIFICATION"]
    work = view.get("work")
    if isinstance(work, dict):
        lines.append(row("Work", str(work["label"])))
    verification = view["verification"]
    counts = verification.get("counts_label")
    lines.append(row("Verification", verification["headline"] + (f" ({counts})" if counts else "")))
    evidence = view["evidence"]
    text = str(evidence["label"])
    if evidence.get("artifact_sha"):
        text += f", commit {evidence['artifact_sha'][:12]}"
    elif evidence.get("bound") is False:
        text += " (not bound to a commit)"
    lines.append(row("Evidence", text))
    capture = view["capture"]
    lines.append(row("Capture", capture["label"] + (f" ({capture['gaps']})" if capture.get("gaps") else "")))
    activity = view.get("activity")
    if isinstance(activity, dict):
        failures = activity.get("tool_failures")
        lines.append(row(
            "Tool activity",
            f"{activity['tool_calls']} call(s)"
            + (f", {failures} tool failure(s)" if failures is not None else "")
            + " (not verification)",
        ))
    failed = verification.get("failed_checks") or []
    if failed:
        lines.append(f"{indent}Failed checks")
        lines.extend(f"{indent}  {name}" for name in failed)
        hidden = (verification.get("checks_failed") or 0) - len(failed)
        if hidden > 0:
            lines.append(f"{indent}  +{hidden} more")
    original = view.get("original")
    if isinstance(original, dict):
        text = str(original.get("status") or "unknown").replace("_", " ").capitalize()
        detail = [_SOURCE_WORDS.get(str(original.get("source")), "")]
        if original.get("counts_label"):
            detail.append(str(original["counts_label"]))
        detail = [d for d in detail if d]
        lines.append(row("Original", text + (f" ({': '.join(detail)})" if detail else "")))
    history = view.get("history") or []
    if len(history) > 1:
        lines.append(f"{indent}Evidence history (oldest first; nothing here was rewritten)")
        lines.extend(f"{indent}  {_history_line(item)}" for item in history)
    return lines


__all__ = [
    "VIEW_VERSION",
    "WORK_COMPLETED",
    "WORK_ENDED_NO_TURN",
    "WORK_ENDED_WITHOUT_FINAL_EVENT",
    "WORK_IN_PROGRESS",
    "WORK_TURN_COMPLETED",
    "build_verification_view",
    "render_verification_view",
]
