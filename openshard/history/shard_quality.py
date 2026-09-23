"""Shard Quality Summary v1 - a compact, derived view of proof quality.

This module does not introduce a new score or judgement. It is a pure
projection over signals that already exist:

- the Shard Proof Contract v1 (``build_shard_proof_contract``), which decides
  ``overall_status``, ``missing_required_sections``, ``weak_recommended_sections``
  and ``unsafe_findings``, and
- the verification enum already derived from a Shard receipt
  (``verification_status_from_receipt``).

The result answers, at a glance, "is the latest run's proof record good enough?"
without making the reader open ``proof last``.

All output is JSON-serializable and made of safe tokens only: fixed enums,
integer counts, a boolean, and a templated plain-English sentence. No raw
prompts, diffs, file contents, secrets, paths, command output, or model output
are ever emitted. The function never raises.
"""

from __future__ import annotations

from openshard.history.proof_contract import (
    OVERALL_UNKNOWN,
    build_shard_proof_contract,
)
from openshard.history.proof_signals import verification_status_from_receipt
from openshard.history.shard_contract import build_shard_receipt

SUMMARY_VERSION = "1"

# Plain-English phrasing for the verification enum. Kept here so the summary
# sentence never leaks raw status tokens or section names.
_VERIFICATION_PHRASE = {
    "passed": "verification passed",
    "failed": "verification failed",
    "skipped": "verification skipped",
    "manual_review": "verification needs manual review",
    "not_run": "verification not run",
    "partial": "verification partially observed",
    "unknown": "verification status unknown",
}


def _fallback() -> dict:
    """Safe result for non-dict input or an unexpected internal failure."""
    return {
        "summary_version": SUMMARY_VERSION,
        "status": OVERALL_UNKNOWN,
        "required_proof": "incomplete",
        "recommended_gaps_count": 0,
        "unsafe_findings_count": 0,
        "verification": "unknown",
        "raw_output_stored": False,
        "summary": "Quality summary unavailable",
    }


def _summary_sentence(
    required_proof: str,
    recommended_gaps: int,
    unsafe_count: int,
    verification: str,
) -> str:
    """Build a plain-English summary from safe tokens only."""
    clauses: list[str] = []

    # Unsafe findings lead when present; they are the most important signal.
    if unsafe_count > 0:
        noun = "unsafe finding" if unsafe_count == 1 else "unsafe findings"
        clauses.append(f"{unsafe_count} {noun}")

    if required_proof == "present":
        clauses.append("Required proof present")
    else:
        clauses.append("Required proof incomplete")

    if unsafe_count == 0 and recommended_gaps == 0:
        clauses.append("no unsafe findings")
    elif recommended_gaps > 0:
        noun = "recommended gap" if recommended_gaps == 1 else "recommended gaps"
        clauses.append(f"{recommended_gaps} {noun}")

    clauses.append(_VERIFICATION_PHRASE.get(verification, "verification status unknown"))

    # Capitalise the first clause; keep the rest as written.
    sentence = "; ".join(clauses)
    return sentence[:1].upper() + sentence[1:] if sentence else "Quality summary unavailable"


def build_shard_quality_summary(entry: dict, receipt: object | None = None) -> dict:
    """Derive a compact quality summary for a single run entry. Never raises.

    Reuses the Shard Proof Contract v1 and the existing verification signal; it
    adds no new scoring algorithm. ``receipt`` may be passed in to avoid
    rebuilding one the caller already has; if omitted, a receipt is built
    internally for the verification fields.

    A non-dict ``entry`` (or any unexpected internal failure) yields a safe
    fallback. An empty dict is a valid record and flows through the normal proof
    contract, which reports it as weak or partial evidence.
    """
    if not isinstance(entry, dict):
        return _fallback()

    try:
        contract = build_shard_proof_contract(entry)

        missing_required = contract.get("missing_required_sections") or []
        weak_recommended = contract.get("weak_recommended_sections") or []
        unsafe_findings = contract.get("unsafe_findings") or []
        status = contract.get("overall_status") or OVERALL_UNKNOWN

        required_proof = "present" if not missing_required else "incomplete"
        recommended_gaps = len(weak_recommended)
        unsafe_count = len(unsafe_findings)

        if receipt is None:
            receipt = build_shard_receipt(entry, index=None)
        verification = verification_status_from_receipt(receipt)  # type: ignore[arg-type]  # receipt is ShardReceipt after build or assignment above
        raw_output_stored = bool(getattr(receipt, "verification_raw_output_stored", False))

        return {
            "summary_version": SUMMARY_VERSION,
            "status": status,
            "required_proof": required_proof,
            "recommended_gaps_count": recommended_gaps,
            "unsafe_findings_count": unsafe_count,
            "verification": verification,
            "raw_output_stored": raw_output_stored,
            "summary": _summary_sentence(
                required_proof, recommended_gaps, unsafe_count, verification
            ),
        }
    except Exception:
        return _fallback()
