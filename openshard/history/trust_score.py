"""Pure, deterministic Run Trust Score v1 over a Shard receipt.

This module answers one question: *"How trustworthy is this OpenShard run, based
on the proof signals already recorded for it?"* It is a **trust heuristic over
recorded proof signals** — not ML, not adaptive routing, not analytics, and not a
safety guarantee or certification.

It contains no I/O. It takes an already-built ``ShardReceipt`` (and its raw run
entry) plus the *types* of any developer-interaction events for the run, and
reduces them to a single integer ``score`` in ``0..100`` with an explicit list of
named ``penalties``. Scoring starts at 100 and subtracts fixed penalties; there
are no bonus points in v1.

Signal reuse (single source of truth):

* ``classify_failure(entry, receipt)`` (public) yields the failure ``category``
  *and* a ``signals`` dict already carrying verification / policy_denied /
  manual_review_required / secret_scan_findings / feedback_outcome / a sanitised
  error_class. We read those rather than re-import private CI helpers.
* ``score_receipt(receipt)`` (public) yields the completeness percent.
* Two receipt predicates (``_changes_made`` / ``_manual_fix_required``) are
  imported from ``shard_schema`` as the canonical single source of truth.

Double-counting guard: penalties are computed from the *discrete underlying
signals*, never from the failure category (the category is itself a reduction of
those same signals; ``failure_category`` is reported for transparency but is worth
0 points). Overlapping policy/approval signals collapse into a single
strongest-wins penalty; the feedback signals likewise emit at most one penalty.

Safety: output is built only from static reason strings, integer counts, enum
values, and the safe short ``shard_id``. It never contains raw secrets, absolute
paths, raw error text, raw feedback reason text, prompts, diffs, file contents, or
transcripts. The functions here never raise: any unexpected error degrades to a
best-effort score with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openshard.history.completeness import score_receipt
from openshard.history.failures import classify_failure
from openshard.history.shard_schema import (
    shard_changes_made as _changes_made,
)
from openshard.history.shard_schema import (
    shard_manual_fix_required as _manual_fix_required,
)

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt

SCHEMA_VERSION = "1"

# --- Penalty point constants (explicit; no hidden scoring) -------------------
PENALTY_VERIFICATION_FAILED = 35
PENALTY_VERIFICATION_NOT_RUN = 20
PENALTY_POLICY_DENIED = 40
PENALTY_MANUAL_REVIEW = 25
PENALTY_SECRET_SCAN = 40
PENALTY_LOW_COMPLETENESS_HIGH = 20  # completeness < 50
PENALTY_LOW_COMPLETENESS_MID = 10  # completeness < 70
PENALTY_LOW_COMPLETENESS_LOW = 5  # completeness < 85
PENALTY_FEEDBACK_REJECTED = 25
PENALTY_FEEDBACK_PARTIAL = 15
PENALTY_FEEDBACK_RETRIED = 10
PENALTY_EXECUTION_ERROR = 15
PENALTY_UNSAFE_INTERACTION = 25
PENALTY_DIRTY_REPO = 5
PENALTY_NO_TIMELINE = 5

# Completeness band edges (percent).
COMPLETENESS_LOW_EDGE = 50
COMPLETENESS_MID_EDGE = 70
COMPLETENESS_HIGH_EDGE = 85

# Interaction event types that signal an unsafe / out-of-scope action. Mirrors the
# canonical event-type vocabulary in ``history/interactions.py``. Penalised once,
# regardless of how many such events occurred.
UNSAFE_INTERACTION_TYPES = frozenset({"unsafe_command", "wrong_file", "wrong_scope"})

# Feedback outcomes that map to the "rejected" feedback penalty.
_REJECTED_OUTCOMES = frozenset({"rejected", "abandoned"})

# Score band edges. ``_band_for`` walks these high-to-low.
BANDS: tuple[tuple[int, str], ...] = (
    (90, "strong"),
    (70, "good"),
    (50, "caution"),
    (25, "weak"),
    (0, "unsafe"),
)

# Stable, static reason strings keyed by penalty code. Never interpolate run data.
REASONS: dict[str, str] = {
    "verification_failed": "Verification failed.",
    "verification_not_run": "Verification was not run for a changed run.",
    "policy_denied": "A policy or approval gate denied the run.",
    "manual_review_required": "Manual review was required.",
    "secret_scan_finding": "Secret-scan finding(s) were detected (redacted).",
    "low_completeness": "Receipt completeness is low.",
    "feedback_rejected": "Developer rejected or abandoned the output.",
    "feedback_partial": "Developer reported a partial result or a required manual fix.",
    "feedback_retried": "The run was retried after a prior attempt.",
    "execution_error": "An execution error was recorded.",
    "unsafe_interaction": "An unsafe or out-of-scope interaction was recorded.",
    "dirty_repo": "The repository had uncommitted changes during the run.",
    "no_timeline": "No run timeline was recorded for a changed run.",
}


@dataclass
class TrustPenalty:
    """One explicit, named deduction from the trust score."""

    code: str
    points: int
    reason: str


@dataclass
class RunTrustScore:
    """Deterministic trust heuristic for a single run.

    ``status`` is ``"ok"`` here; the ``not_found`` case (no run history) is built
    by the CLI, not this evaluator.
    """

    score: int
    band: str
    status: str
    shard_id: str
    signals: dict = field(default_factory=dict)
    penalties: list[TrustPenalty] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)



def _band_for(score: int) -> str:
    """Map a 0..100 score to its band name."""
    for edge, name in BANDS:
        if score >= edge:
            return name
    return "unsafe"


def evaluate_trust_score(
    entry: dict,
    receipt: ShardReceipt,
    *,
    interaction_event_types: list[str] | None = None,
) -> RunTrustScore:
    """Reduce a run's recorded proof signals to a 0..100 trust score. Never raises.

    ``interaction_event_types`` is the list of sanitised ``event_type`` strings for
    the run (types only — never raw summaries). The caller loads these; this
    function performs no I/O.
    """
    shard_id = (getattr(receipt, "shard_id", "") or "")
    warnings: list[str] = []

    # Pull the discrete signals from the public failure classifier (single source
    # of truth) and the public completeness scorer.
    try:
        classification = classify_failure(entry, receipt)
        sig = classification.signals or {}
        failure_category = classification.category
    except Exception:
        sig = {}
        failure_category = "unknown_failure"
        warnings.append("Some proof signals could not be read from the receipt.")
    if not sig:
        warnings.append("Some proof signals could not be read from the receipt.")

    try:
        completeness = int(score_receipt(receipt).score_percent)
    except Exception:
        completeness = 0
        warnings.append("Receipt completeness could not be scored.")

    verification = sig.get("verification", "unknown")
    policy_denied = bool(sig.get("policy_denied"))
    manual_review = bool(sig.get("manual_review_required"))
    secret_findings = int(sig.get("secret_scan_findings") or 0)
    feedback_outcome = sig.get("feedback_outcome")
    error_class = sig.get("error_class")

    changes = _changes_made(receipt)
    manual_fix = _manual_fix_required(receipt)
    timeline_events = len(getattr(receipt, "run_timeline", None) or [])
    event_types = list(interaction_event_types or [])
    unsafe_interaction = any(t in UNSAFE_INTERACTION_TYPES for t in event_types)

    penalties: list[TrustPenalty] = []

    def _add(code: str, points: int) -> None:
        penalties.append(TrustPenalty(code=code, points=points, reason=REASONS[code]))

    # 1. Verification. skipped is a weak signal like not_run, not a failure.
    # manual_review is handled below as part of the policy / review group.
    if verification == "failed":
        _add("verification_failed", PENALTY_VERIFICATION_FAILED)
    elif verification in {"not_run", "unknown", "skipped", "partial"} and changes:
        _add("verification_not_run", PENALTY_VERIFICATION_NOT_RUN)

    # 2. Policy / approval group — at most one, strongest first (no double count).
    if policy_denied:
        _add("policy_denied", PENALTY_POLICY_DENIED)
    elif manual_review:
        _add("manual_review_required", PENALTY_MANUAL_REVIEW)

    # 3. Secret scan.
    if secret_findings > 0:
        _add("secret_scan_finding", PENALTY_SECRET_SCAN)

    # 4. Completeness (one penalty, static bands).
    if completeness < COMPLETENESS_LOW_EDGE:
        _add("low_completeness", PENALTY_LOW_COMPLETENESS_HIGH)
    elif completeness < COMPLETENESS_MID_EDGE:
        _add("low_completeness", PENALTY_LOW_COMPLETENESS_MID)
    elif completeness < COMPLETENESS_HIGH_EDGE:
        _add("low_completeness", PENALTY_LOW_COMPLETENESS_LOW)

    # 5. Feedback group — at most one, strongest first.
    if feedback_outcome in _REJECTED_OUTCOMES:
        _add("feedback_rejected", PENALTY_FEEDBACK_REJECTED)
    elif feedback_outcome == "partial" or manual_fix:
        _add("feedback_partial", PENALTY_FEEDBACK_PARTIAL)
    elif feedback_outcome == "retried":
        _add("feedback_retried", PENALTY_FEEDBACK_RETRIED)

    # 6. Execution error — only when not already counted as a verification failure.
    if error_class and verification != "failed":
        _add("execution_error", PENALTY_EXECUTION_ERROR)

    # 7. Unsafe / out-of-scope interaction (capped once).
    if unsafe_interaction:
        _add("unsafe_interaction", PENALTY_UNSAFE_INTERACTION)

    # 8. Dirty repo (weak).
    if getattr(receipt, "git_dirty", None) is True:
        _add("dirty_repo", PENALTY_DIRTY_REPO)

    # 9. No timeline for a changed run (weak).
    if timeline_events == 0 and changes:
        _add("no_timeline", PENALTY_NO_TIMELINE)

    score = max(0, 100 - sum(p.points for p in penalties))

    signals = {
        "verification": verification,
        "manual_review_required": manual_review,
        "policy_denied": policy_denied,
        "secret_scan_findings": secret_findings,
        "completeness_score_percent": completeness,
        "failure_category": failure_category,
        "feedback_outcome": feedback_outcome,
        "interaction_events": len(event_types),
        "timeline_events": timeline_events,
    }

    return RunTrustScore(
        score=score,
        band=_band_for(score),
        status="ok",
        shard_id=shard_id,
        signals=signals,
        penalties=penalties,
        warnings=warnings,
    )


def to_payload(ts: RunTrustScore) -> dict:
    """Serialise the body fields for the ``--json`` envelope (no envelope wrapper)."""
    return {
        "score": ts.score,
        "band": ts.band,
        "signals": dict(ts.signals),
        "penalties": [
            {"code": p.code, "points": p.points, "reason": p.reason} for p in ts.penalties
        ],
    }


def format_human(ts: RunTrustScore) -> list[str]:
    """Render the short, careful human output as a list of lines."""
    lines = [
        f"OpenShard Trust Score: {ts.score} / 100 ({ts.band})",
        "",
        "This is a trust heuristic over recorded proof signals, not a safety guarantee.",
        "",
        "Reasons:",
    ]
    for reason in _summary_reasons(ts):
        lines.append(f"  - {reason}")
    if ts.penalties:
        lines.append("")
        lines.append("Penalties:")
        for p in ts.penalties:
            lines.append(f"  - {p.reason} (-{p.points})")
    if ts.warnings:
        lines.append("")
        lines.append("Notes:")
        for w in ts.warnings:
            lines.append(f"  - {w}")
    return lines


def _summary_reasons(ts: RunTrustScore) -> list[str]:
    """Short, static positive/neutral reasons derived from the signals."""
    sig = ts.signals
    reasons: list[str] = []

    verification = sig.get("verification")
    if verification == "passed":
        reasons.append("Verification passed")
    elif verification == "failed":
        reasons.append("Verification failed")
    elif verification == "not_run":
        reasons.append("Verification was not run")
    elif verification == "skipped":
        reasons.append("Verification was skipped")
    elif verification == "partial":
        reasons.append("Verification was only partially observed")
    elif verification == "manual_review":
        reasons.append("Verification needs manual review")
    else:
        reasons.append("Verification status is unknown")

    completeness = sig.get("completeness_score_percent")
    if isinstance(completeness, int):
        reasons.append(f"Receipt completeness is {completeness}%")

    category = sig.get("failure_category")
    if category == "no_failure_detected":
        reasons.append("No failure category detected")
    elif category:
        reasons.append(f"Failure category: {category}")

    return reasons
