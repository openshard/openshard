"""One machine-readable verdict for a commit or a task (``openshard verdict``).

Nothing runs here. The verdict reads the evidence already recorded and the
one interpretation every other surface uses (``history/verification_truth``):

* ``verified`` (exit 0) -- an outcome OpenShard or CI observed passed
  (``verified_passed``).
* ``failed`` (exit 1) -- the current outcome is a failure. ``authority`` says
  who reported it; an agent's own report of a failure counts as a failure.
* ``unknown`` (exit 2) -- nothing has checked it yet, the outcome is partial
  or not observed, or only the agent says it passed.
* ``error`` (exit 3) -- the question could not be answered (bad id, no
  history).

``--commit SHA`` uses only re-runs and CI verdicts bound to exactly that
commit (``verifications.jsonl``). ``--task ID`` uses the task's latest
Receipt, with its later evidence; earlier Receipts are listed, never used.
"""

from __future__ import annotations

from typing import Any

from openshard.history.task_identity import is_task_id, stored_task_id
from openshard.history.verification_truth import (
    INTEGRITY_MISMATCH,
    STATE_VERIFIED_PASSED,
    VerificationTruth,
    interpret_evidence,
    interpret_receipt,
)
from openshard.verification.post_session import (
    evidence_for_entry,
    latest_for_entry,
    summarize_attestation,
)

SCHEMA = "openshard.verdict"
SCHEMA_VERSION = 1

VERDICT_VERIFIED = "verified"
VERDICT_FAILED = "failed"
VERDICT_UNKNOWN = "unknown"
VERDICT_ERROR = "error"
EXIT_CODES = {VERDICT_VERIFIED: 0, VERDICT_FAILED: 1, VERDICT_UNKNOWN: 2, VERDICT_ERROR: 3}

MATCH_COMMIT = "commit"  # evidence bound to exactly the commit asked about
MATCH_TREE = "tree"  # evidence bound to another commit with the same tree

REASON_NOT_CHECKED = "not_checked"  # no evidence names this subject
REASON_NO_RECEIPT = "no_receipt"  # no Receipt carries this task id
REASON_INTEGRITY_MISMATCH = "integrity_mismatch"  # the Receipt changed after its hash was written


def _bound_sha(item: dict) -> str | None:
    block = item.get("verification")
    sha = block.get("artifact_sha") if isinstance(block, dict) else None
    return sha.lower() if isinstance(sha, str) else None


def _evidence_row(summary: dict, receipt_id: object) -> dict[str, Any]:
    block = summary.get("verification") or {}
    return {
        "attestation_id": summary.get("attestation_id"),
        "receipt_id": receipt_id if isinstance(receipt_id, str) else None,
        "kind": "ci" if summary.get("kind") == "ci_verification" else "rerun",
        "status": block.get("status"),
        "source": block.get("source"),
        "artifact_sha": block.get("artifact_sha"),
        "created_at": summary.get("created_at"),
    }


def _from_truth(truth: VerificationTruth, *, reason: str | None = None) -> dict[str, Any]:
    if reason is None and truth.integrity == INTEGRITY_MISMATCH:
        reason = REASON_INTEGRITY_MISMATCH
    if reason is not None:
        verdict = VERDICT_UNKNOWN
    elif truth.state == STATE_VERIFIED_PASSED:
        verdict = VERDICT_VERIFIED
    elif truth.effective_status == "failed":
        verdict = VERDICT_FAILED
    else:
        verdict = VERDICT_UNKNOWN
    return {
        "verdict": verdict,
        "exit_code": EXIT_CODES[verdict],
        "reason": reason or truth.state,
        "state": truth.state,
        "authority": truth.authority,
        "basis": truth.basis,
        "artifact_sha": truth.artifact_sha,
    }


def _document(subject: dict[str, Any], result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "subject": subject, **result, **extra}


def error(kind: str, ident: str, message: str) -> dict[str, Any]:
    """The ``error`` verdict: the question could not be answered."""
    return _document({"kind": kind, "id": ident}, {
        "verdict": VERDICT_ERROR, "exit_code": EXIT_CODES[VERDICT_ERROR], "reason": "error", "state": None,
        "authority": None, "basis": None, "artifact_sha": None,
    }, message=message, evidence=[], receipts=[])


def commit_evidence(sha: str, attestations: list[dict], *, tree_oid: str | None = None
                    ) -> tuple[list[dict], str | None]:
    """The attestations a verdict on *sha* uses, and how they match (``commit``, ``tree`` or None)."""
    items = [item for item in attestations if isinstance(item, dict) and _bound_sha(item)]
    matched = [item for item in items if _bound_sha(item) == sha]
    if matched:
        return matched, MATCH_COMMIT
    if tree_oid:
        matched = [item for item in items if item.get("tree_oid") == tree_oid]
        if matched:
            return matched, MATCH_TREE
    return [], None


def commit_verdict(sha: str, attestations: list[dict], *, tree_oid: str | None = None) -> dict[str, Any]:
    """The verdict for *sha* (a full lowercase commit id) from *attestations*.

    Evidence bound to exactly *sha* is used when there is any (``match:
    commit``). Otherwise, with *tree_oid* (the tree of *sha*), evidence bound
    to another commit with that same tree is used (``match: tree``): the same
    content under a different commit id, e.g. after a squash merge.
    """
    matched, match = commit_evidence(sha, attestations, tree_oid=tree_oid)
    summaries = [summarize_attestation(item) for item in matched]
    truth = interpret_evidence(None, post_session_verification={"evidence": summaries})
    result = _from_truth(truth, reason=None if summaries else REASON_NOT_CHECKED)
    rows = [{**_evidence_row(s, item.get("receipt_id")), "tree_oid": item.get("tree_oid"), "match": match}
            for s, item in zip(summaries, matched, strict=True)]
    subject = {"kind": "commit", "id": sha, "tree_oid": tree_oid}
    return _document(subject, result, match=match, evidence=rows, receipts=[])


def task_verdict(task_id: str, entries: list[dict], attestations: list[dict], *, build_receipt) -> dict[str, Any]:
    """The verdict for *task_id*: its latest Receipt's, with that Receipt's later evidence.

    *build_receipt(entry, index, post)* returns the ``ShardReceipt`` for one record.
    """
    if not is_task_id(task_id):
        return error("task", task_id, "Not a task id: expected 'task_' followed by a canonical UUIDv7.")
    indexed = [(i, e) for i, e in enumerate(entries) if stored_task_id(e) == task_id]
    subject = {"kind": "task", "id": task_id}
    if not indexed:
        truth = interpret_evidence(None)
        return _document(subject, _from_truth(truth, reason=REASON_NO_RECEIPT), evidence=[], receipts=[])
    receipts = []
    result: dict[str, Any] = {}
    for index, entry in indexed:
        post = latest_for_entry(entry, attestations)
        result = _from_truth(interpret_receipt(build_receipt(entry, index, post)))
        receipts.append({"receipt_id": entry.get("receipt_id"), "verdict": result["verdict"],
                         "state": result["state"], "authority": result["authority"]})
    latest = indexed[-1][1]
    rows = [_evidence_row(s, latest.get("receipt_id")) for s in evidence_for_entry(latest, attestations)]
    return _document(subject, result, receipt_id=latest.get("receipt_id"), evidence=rows, receipts=receipts)


__all__ = [
    "EXIT_CODES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "commit_evidence",
    "commit_verdict",
    "error",
    "task_verdict",
]
