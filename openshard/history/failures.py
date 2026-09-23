"""Pure, deterministic failure taxonomy over Shard run receipts.

This module contains no I/O. It takes already-built ``ShardReceipt`` objects (and
their raw run entries) and classifies each run into a single, stable failure
category using explicit, evidence-backed rules. It is not ML, not analytics, and
holds no confidence percentages or weights — only a category, a coarse
confidence (``high`` | ``medium`` | ``low``), and short plain-English reasons.

Categories whose suggested names had no supporting receipt field (``tool_error``,
``model_error``, ``context_error``, ``timeout``, ``cost_guardrail``) are
deliberately collapsed into ``execution_error`` rather than guessed. The
normalized error class is preserved — sanitised — in ``signals.error_class`` so
detail is not lost without inventing precision.

Safety: output never contains raw secret values, absolute paths, raw
``error_message`` text, or developer feedback reason text. It emits only static
category names, integer counts, the receipt ``shard_id`` (a safe short id), and a
sanitised, length-capped ``error_class`` token.

The functions here never raise: any unexpected per-run error degrades to
``unknown_failure``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openshard.ci.policy_check import (
    _manual_review_required,
    _secret_scan_findings,
    _verification_status,
)
from openshard.history.shard_schema import (
    shard_changes_made as _changes_made,
)
from openshard.history.shard_schema import (
    shard_manual_fix_required as _manual_fix_required,
)

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt


# Ordered category set. Classification walks this list top-down; first match wins.
# ``no_failure_detected`` is the terminal, not-a-failure outcome.
CATEGORIES: list[str] = [
    "policy_denied",
    "verification_failed",
    "secret_scan_finding",
    "manual_review_required",
    "execution_error",
    "user_rejected",
    "partial_success",
    "verification_not_run",
    "unknown_failure",
    "no_failure_detected",
]

# Coarse confidence per category. No percentages, no weights.
CATEGORY_CONFIDENCE: dict[str, str] = {
    "policy_denied": "high",
    "verification_failed": "high",
    "secret_scan_finding": "high",
    "manual_review_required": "high",
    "execution_error": "medium",
    "user_rejected": "high",
    "partial_success": "medium",
    "verification_not_run": "low",
    "unknown_failure": "low",
    "no_failure_detected": "high",
}

# Plain-English remediation guidance, emitted only for categories actually seen.
RECOMMENDATIONS: dict[str, str] = {
    "policy_denied": "policy or approval gate denied the run; review the policy/approval decision.",
    "verification_failed": "verification failed; inspect the failing checks before shipping.",
    "secret_scan_finding": "secret-scan finding(s) present (redacted); rotate and remove the secret.",
    "manual_review_required": "manual review was flagged; resolve the blocker before relying on the run.",
    "execution_error": "an execution error was recorded; inspect the run for the error class shown.",
    "user_rejected": "developer rejected/abandoned the output; revisit the task framing.",
    "partial_success": "partial result or manual fix required; finish the remaining work.",
    "verification_not_run": "changes shipped without verification; run and record checks.",
    "unknown_failure": "a retry/failure signal was present but the cause is unclear; inspect manually.",
}

# Feedback outcomes (see CLI feedback command) that map to failure categories.
_REJECTED_OUTCOMES = {"rejected", "abandoned"}

# Output caps to keep reports short.
MAX_RECOMMENDATIONS = 5
MAX_RECENT_FAILURES = 10

# Sanitisation caps for the untrusted error_class token.
_ERROR_CLASS_MAX_LEN = 60


def _safe_error_class(value: object) -> str | None:
    """Sanitise an untrusted ``error_class`` value for safe emission.

    ``error_class`` is intended to be a normalized token, but we treat it as
    untrusted run metadata. Returns a short safe token (e.g. ``VerificationError``)
    or ``None`` if missing, non-string, path-like, or secret-like.
    """
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None
    # Reject path-like values outright (absolute paths or any separator).
    if "/" in token or "\\" in token:
        return None
    # Reject obvious secret-like values: long opaque runs with no spaces.
    if len(token) > _ERROR_CLASS_MAX_LEN:
        return None
    # A normalized error class is a short identifier-ish token. Anything with
    # whitespace beyond a couple of words, or non-identifier punctuation, is
    # likely free text or a payload — scrub it to None rather than emit.
    if any(ch.isspace() for ch in token):
        return None
    if not all(ch.isalnum() or ch in {".", "_", "-"} for ch in token):
        return None
    return token


def _policy_denied(receipt: ShardReceipt) -> bool:
    """True when an approval was required-but-denied, or a policy decision denied."""
    if receipt.approval_required and not receipt.approval_granted:
        return True
    return any(
        isinstance(pd, dict) and pd.get("decision") == "deny"
        for pd in receipt.policy_decisions
    )


def _feedback_outcome(receipt: ShardReceipt) -> str | None:
    """Extract the developer feedback outcome string, if any."""
    df = receipt.developer_feedback
    if not isinstance(df, dict):
        return None
    outcome = df.get("outcome")
    return outcome if isinstance(outcome, str) and outcome else None



@dataclass
class FailureClassification:
    """Per-run failure classification result."""

    shard_id: str
    category: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)


@dataclass
class FailureReport:
    """Aggregate failure taxonomy across a set of runs."""

    runs_checked: int
    category_counts: dict[str, int]
    top_categories: list[dict]
    recommendations: list[str]
    failures: list[FailureClassification] = field(default_factory=list)


def classify_failure(entry: dict, receipt: ShardReceipt) -> FailureClassification:
    """Classify a single run entry/receipt into one failure category. Never raises."""
    shard_id = receipt.shard_id or ""
    try:
        verification = _verification_status(receipt)
        policy_denied = _policy_denied(receipt)
        manual_review = _manual_review_required(entry, receipt)
        secret_findings = _secret_scan_findings(receipt)
        feedback_outcome = _feedback_outcome(receipt)
        manual_fix = _manual_fix_required(receipt)
        error_class = _safe_error_class(receipt.error_class)

        signals = {
            "verification": verification,
            "policy_denied": policy_denied,
            "manual_review_required": manual_review,
            "secret_scan_findings": secret_findings,
            "feedback_outcome": feedback_outcome,
            "error_class": error_class,
        }

        # Priority order — first match wins.
        if policy_denied:
            category, reasons = "policy_denied", ["Policy or approval gate denied the run."]
        elif verification == "failed":
            category, reasons = "verification_failed", ["Verification failed."]
        elif secret_findings > 0:
            category = "secret_scan_finding"
            reasons = [f"{secret_findings} secret-scan finding(s) present (redacted)."]
        elif manual_review:
            category, reasons = "manual_review_required", ["Manual review was flagged."]
        elif error_class is not None:
            category, reasons = "execution_error", [f"Execution error recorded ({error_class})."]
        elif feedback_outcome in _REJECTED_OUTCOMES:
            category = "user_rejected"
            reasons = [f"Developer feedback outcome: {feedback_outcome}."]
        elif feedback_outcome == "partial" or manual_fix:
            category = "partial_success"
            reasons = (
                ["Manual fix required after the run."]
                if manual_fix and feedback_outcome != "partial"
                else ["Developer feedback outcome: partial."]
            )
        elif verification in {"not_run", "unknown", "skipped", "partial"} and _changes_made(receipt):
            category = "verification_not_run"
            reasons = ["Changes were made but verification did not produce a result."]
        elif feedback_outcome == "retried":
            category = "unknown_failure"
            reasons = ["Run was retried, implying a prior failure of unclear cause."]
        else:
            category, reasons = "no_failure_detected", []

        return FailureClassification(
            shard_id=shard_id,
            category=category,
            confidence=CATEGORY_CONFIDENCE.get(category, "low"),
            reasons=reasons,
            signals=signals,
        )
    except Exception:
        return FailureClassification(
            shard_id=shard_id,
            category="unknown_failure",
            confidence="low",
            reasons=["Classification could not be completed from the receipt."],
            signals={},
        )


def evaluate_failures(
    pairs: list[tuple[dict, ShardReceipt]],
) -> FailureReport:
    """Aggregate failure taxonomy over (entry, receipt) pairs. Never raises.

    Empty input yields a zeroed report. ``no_failure_detected`` runs are counted
    in ``runs_checked`` and ``category_counts`` but excluded from ``failures``.
    """
    classifications = [classify_failure(entry, receipt) for entry, receipt in pairs]
    runs_checked = len(classifications)

    # Counts over every category that can appear (stable keys, zero-filled).
    category_counts: dict[str, int] = {name: 0 for name in CATEGORIES}
    for c in classifications:
        category_counts[c.category] = category_counts.get(c.category, 0) + 1

    # Top failure categories (exclude the not-a-failure terminal), most common first.
    top_categories = [
        {"category": name, "count": count}
        for name, count in sorted(
            (
                (n, cnt)
                for n, cnt in category_counts.items()
                if n != "no_failure_detected" and cnt > 0
            ),
            key=lambda x: (-x[1], CATEGORIES.index(x[0])),
        )
    ]

    recommendations = [
        RECOMMENDATIONS[entry["category"]]
        for entry in top_categories[:MAX_RECOMMENDATIONS]
        if entry["category"] in RECOMMENDATIONS
    ]

    # Failures, most-recent-first (pairs arrive oldest-first), capped.
    failures = [c for c in reversed(classifications) if c.category != "no_failure_detected"]

    return FailureReport(
        runs_checked=runs_checked,
        category_counts=category_counts,
        top_categories=top_categories,
        recommendations=recommendations,
        failures=failures[:MAX_RECENT_FAILURES],
    )
