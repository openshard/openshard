"""Pure decision for the Openshard merge gate (the root ``action.yml``).

No I/O. Takes the ``openshard verdict --commit`` document for a pull
request's head commit (docs/verdict.md), the ``strict`` flag and an optional
summary of the Receipt the evidence names, and returns pass / warn / fail
plus the markdown for the job summary.

Decision (docs/merge-gate.md):

* ``verified`` -> ``pass``.
* ``failed``   -> ``fail``: verification failed for this commit.
* ``unknown``  -> ``warn`` (exit 0): no evidence, or only the agent says it
  passed. Could be a human-written PR, so it never blocks by default.
  ``strict`` turns it into ``fail``.
* ``error``, or a document this module cannot read -> ``fail``. A gate that
  could not answer never passes silently.

Wording rules: unknown stays unknown (never zero), and a Receipt is called
tamper-evident only when the Platform witness acknowledged its capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

WITNESS_YES = "yes"
WITNESS_NO = "no"
WITNESS_UNKNOWN = "unknown"

UNKNOWN = "unknown"
_MAX_CELL = 120
_MAX_FAILED_NAMES = 5

_AUTHORITY_WORDS = {
    "directly_observed": "an Openshard re-run",
    "independently_verified": "independent CI",
    "git_verified": "git",
    "agent_reported": "the agent's own report",
}

_UNKNOWN_REASONS = {
    "not_checked": "No verification evidence for this commit was available to this job.",
    "no_receipt": "No Receipt was found for this work.",
    "integrity_mismatch": "The Receipt changed after its hash was written, so its evidence is not used.",
    "agent_reported_passed": "Only the agent says the checks passed; nothing independent observed them.",
    "agent_reported_partial": "Only the agent reported a partial result; nothing independent observed it.",
    "attempted_unverified": "Checks were attempted but their outcome was not observed.",
    "verified_partial": "Only some checks completed.",
    "not_run": "No checks were run.",
    "skipped": "Verification was skipped.",
    "manual_review": "Verification needs manual review.",
    "not_observed": "Verification was not observed.",
    "untrusted_history": ("Openshard history files are committed to the repository, so they came with this "
                          "change and are not used as evidence."),
}


@dataclass(frozen=True)
class GateReceipt:
    """What the job summary says about the Receipt behind the evidence. None means unknown."""

    receipt_id: str | None = None
    agent: str | None = None
    model: str | None = None
    cost: str | None = None
    witness: str = WITNESS_UNKNOWN
    witness_detail: str | None = None
    merge_check: str | None = None  # informational only, never a gate criterion


@dataclass(frozen=True)
class GateResult:
    status: str
    exit_code: int
    verdict: str
    headline: str
    reasons: list[str] = field(default_factory=list)
    summary_markdown: str = ""


def _cell(value: object) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return UNKNOWN
    text = " ".join(str(value).split()).replace("|", "\\|").replace("`", "'")
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 1] + "…"


def _short(sha: object) -> str:
    return sha[:12] if isinstance(sha, str) and sha else UNKNOWN


def _checks_text(checks: str | None, failed: tuple[str, ...]) -> str:
    text = checks or UNKNOWN
    if failed:
        names = ", ".join(failed[:_MAX_FAILED_NAMES]) + (" …" if len(failed) > _MAX_FAILED_NAMES else "")
        text += f" (failed: {names})"
    return text


def _witness_text(receipt: GateReceipt | None) -> str:
    if receipt is None:
        return UNKNOWN
    if receipt.witness == WITNESS_YES:
        detail = f", {receipt.witness_detail}" if receipt.witness_detail else ""
        return f"yes{detail}"
    if receipt.witness == WITNESS_NO:
        return "no"
    return UNKNOWN


def evaluate_gate(
    doc: object,
    *,
    strict: bool = False,
    receipt: GateReceipt | None = None,
    checks: str | None = None,
    failed_checks: tuple[str, ...] = (),
    evidence_source: str | None = None,
) -> GateResult:
    """Reduce one verdict document to the gate outcome. Never raises."""
    data = doc if isinstance(doc, dict) else {}
    verdict = data.get("verdict")
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    raw_subject = data.get("subject")
    sha = raw_subject.get("id") if isinstance(raw_subject, dict) else None
    authority = _AUTHORITY_WORDS.get(str(data.get("authority")), None)
    reasons: list[str] = []

    if verdict == "verified":
        status = STATUS_PASS
        headline = f"Verified: the recorded evidence says {authority or 'a verifier'} observed the checks pass."
    elif verdict == "failed":
        status = STATUS_FAIL
        who = f" (reported by {authority})" if authority else ""
        headline = f"Verification failed for this commit{who}."  # a failure stands whoever reported it
        reasons.append(headline)
    elif verdict == "unknown":
        status = STATUS_FAIL if strict else STATUS_WARN
        detail = _UNKNOWN_REASONS.get(reason or "", "Verification is unknown for this commit.")
        if strict:
            headline = f"Unknown, and strict mode requires verified evidence. {detail}"
            reasons.append(headline)
        else:
            headline = f"Unknown. {detail} This may be a human-written change; this check does not fail on unknown."
    else:
        status = STATUS_FAIL
        message = data.get("message") if isinstance(data.get("message"), str) else None
        headline = "The gate could not answer, so it fails rather than pass silently" + (
            f": {message}" if message else "."
        )
        reasons.append(headline)

    match_note = ""
    if data.get("match") == "tree":
        match_note = (
            f"Evidence is bound to commit {_short(data.get('artifact_sha'))}, which has the same content "
            "(same tree) as this commit."
        )

    title = {STATUS_PASS: "passed", STATUS_WARN: "warning: unknown", STATUS_FAIL: "failed"}[status]
    shown = verdict if verdict in ("verified", "failed", "unknown", "error") else "error"
    rows = [
        ("Result", shown + (f" ({reason})" if reason and reason != shown else "")),
        ("Agent", receipt.agent if receipt else None),
        ("Model", receipt.model if receipt else None),
        ("Checks", _checks_text(checks, failed_checks)),
        ("Cost", receipt.cost if receipt else None),
        ("Platform-witnessed", _witness_text(receipt)),
    ]
    if receipt and receipt.receipt_id:
        rows.append(("Receipt", receipt.receipt_id))
    if receipt and receipt.merge_check:
        rows.append(("Merge check (at capture, informational)", receipt.merge_check))

    lines = [
        f"### Openshard gate: {title}",
        "",
        f"Commit `{_short(sha)}`. {headline}",
    ]
    if match_note:
        lines += ["", match_note]
    if evidence_source:
        lines += ["", f"Evidence source: {evidence_source}"]
    lines += ["", "| | |", "| --- | --- |"]
    lines += [f"| {name} | {_cell(value)} |" for name, value in rows]
    if receipt and receipt.witness == WITNESS_YES:
        lines += ["", "The Platform acknowledged this Receipt's capture, so a later local rewrite of it is "
                      "detectable. Being witnessed is not being verified."]
    lines += ["", "Unknown means not recorded or not available to this job, never zero."]

    return GateResult(
        status=status,
        exit_code=1 if status == STATUS_FAIL else 0,
        verdict=shown,
        headline=headline,
        reasons=reasons,
        summary_markdown="\n".join(lines) + "\n",
    )


__all__ = [
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_WARN",
    "WITNESS_NO",
    "WITNESS_UNKNOWN",
    "WITNESS_YES",
    "GateReceipt",
    "GateResult",
    "evaluate_gate",
]
