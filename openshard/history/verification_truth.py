"""One interpretation of a Receipt's verification evidence, shared by every surface.

Before this module each consumer read the flat ``verification_status`` token
on its own: the proof contract, the trust score (through the failure
classifier), the quality summary, the CI policy check, the Home screen and
``last`` each decided separately what "passed" meant. The token carries no
source, so an agent's own ``exitCode: 0`` -- stored honestly as
``source: agent_reported`` -- came out of every one of them as "Verification
passed", while a later ``openshard verify`` that OpenShard ran itself and saw
fail was shown on one line of ``last`` and ignored everywhere else.

This module is the single place that turns the recorded evidence into a
claim, using only the existing vocabulary (``history/verification.py``
sources and statuses, ``verification/post_session.py`` attestations,
``history/shard_hash.py`` integrity). Nothing here scores; it states:

``authority``
    Who vouches for the *current* outcome: ``directly_observed`` (OpenShard
    ran the check and read the exit code, or observed it itself),
    ``git_verified``, ``independently_verified`` (CI or another system),
    ``agent_reported`` (the agent's own claim), or ``none``.
``state``
    The claim OpenShard can make, strongest evidence first:
    ``verified_passed`` / ``verified_failed`` / ``verified_partial`` (an
    observed outcome), ``agent_reported_passed`` / ``agent_reported_failed`` /
    ``agent_reported_partial`` (a claim OpenShard did not check),
    ``attempted_unverified`` (a check was seen running, no outcome),
    ``manual_review``, ``skipped``, ``not_run``, ``not_observed``.
``effective_status``
    The one token every consumer that still thinks in the flat vocabulary
    (``passed`` | ``failed`` | ``partial`` | ``skipped`` | ``manual_review`` |
    ``not_run`` | ``unknown``) must use. An agent-reported *pass* is
    ``unknown`` here: recorded, labelled, but never equivalent to "OpenShard
    verified this passed". An agent-reported *failure* stays ``failed``: a
    claimed failure lowers confidence, it never raises it.

Precedence: the latest ``openshard verify`` attestation for the Receipt
(OpenShard-executed, ``directly_observed``) describes the current evidence
state and wins over what the session recorded; the session's own block
stays visible as the historical claim (``claim_status`` / ``claim_source``).
An attestation that ran nothing does not override anything. The Receipt
bytes are never touched.

``integrity`` is the record's checksum state (``valid`` / ``mismatch`` /
``missing``). The hash is an unkeyed SHA-256 over the stored record: a
match means the bytes are what they were when the hash was written, not
who wrote them.

Pure, never raises, emits only static strings, small ints and enum tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openshard.history.verification import (
    MODE_OPENSHARD_EXECUTED,
    REASON_OUTCOME_NOT_OBSERVED,
    SOURCE_AGENT_REPORTED,
    SOURCE_DIRECTLY_OBSERVED,
    SOURCE_GIT_VERIFIED,
    SOURCE_INDEPENDENTLY_VERIFIED,
    STATUS_FAILED,
    STATUS_NOT_RUN,
    STATUS_PARTIAL,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    parse_verification_block,
)

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt

TRUTH_VERSION = 1

# Authorities reuse the evidence-source vocabulary; ``none`` when nothing vouches.
AUTHORITY_NONE = "none"
OBSERVED_AUTHORITIES: frozenset[str] = frozenset(
    {SOURCE_DIRECTLY_OBSERVED, SOURCE_GIT_VERIFIED, SOURCE_INDEPENDENTLY_VERIFIED}
)

STATE_VERIFIED_PASSED = "verified_passed"
STATE_VERIFIED_FAILED = "verified_failed"
STATE_VERIFIED_PARTIAL = "verified_partial"
STATE_AGENT_REPORTED_PASSED = "agent_reported_passed"
STATE_AGENT_REPORTED_FAILED = "agent_reported_failed"
STATE_AGENT_REPORTED_PARTIAL = "agent_reported_partial"
STATE_ATTEMPTED_UNVERIFIED = "attempted_unverified"
STATE_MANUAL_REVIEW = "manual_review"
STATE_SKIPPED = "skipped"
STATE_NOT_RUN = "not_run"
STATE_NOT_OBSERVED = "not_observed"
STATES: frozenset[str] = frozenset(
    {
        STATE_VERIFIED_PASSED, STATE_VERIFIED_FAILED, STATE_VERIFIED_PARTIAL,
        STATE_AGENT_REPORTED_PASSED, STATE_AGENT_REPORTED_FAILED, STATE_AGENT_REPORTED_PARTIAL,
        STATE_ATTEMPTED_UNVERIFIED, STATE_MANUAL_REVIEW, STATE_SKIPPED, STATE_NOT_RUN, STATE_NOT_OBSERVED,
    }
)
# States whose outcome OpenShard (or an independent system) observed.
OBSERVED_STATES: frozenset[str] = frozenset(
    {STATE_VERIFIED_PASSED, STATE_VERIFIED_FAILED, STATE_VERIFIED_PARTIAL}
)

BASIS_POST_SESSION = "post_session"  # the latest ``openshard verify`` attestation
BASIS_SESSION = "session"  # the Receipt's own verification block
BASIS_NONE = "none"

INTEGRITY_VALID = "valid"
INTEGRITY_MISMATCH = "mismatch"
INTEGRITY_MISSING = "missing"

EFFECTIVE_TOKENS: frozenset[str] = frozenset(
    {"passed", "failed", "partial", "skipped", "manual_review", "not_run", "unknown"}
)

_LEGACY_STATUS_TOKENS: dict[str, str] = {
    "Passed": "passed",
    "Failed": "failed",
    "No checks run": "not_run",
    "Not recorded": "unknown",
}


@dataclass(frozen=True)
class VerificationTruth:
    """What OpenShard can truthfully say about a Receipt's verification right now."""

    state: str
    authority: str
    effective_status: str
    basis: str
    # The session's own recorded evidence (never replaced by a re-run).
    session_status: str | None
    session_source: str | None
    # The agent's claim when it is *not* the authority (kept as history).
    claim_status: str | None
    claim_source: str | None
    # The latest OpenShard re-run, when one exists.
    post_session_status: str | None
    post_session_passed: int | None
    post_session_attempted: int | None
    post_session_artifact_sha: str | None
    checks_passed: int | None
    checks_attempted: int | None
    checks_skipped: int | None
    integrity: str

    @property
    def observed(self) -> bool:
        return self.state in OBSERVED_STATES

    @property
    def integrity_mismatch(self) -> bool:
        return self.integrity == INTEGRITY_MISMATCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": TRUTH_VERSION,
            "state": self.state,
            "authority": self.authority,
            "effective_status": self.effective_status,
            "basis": self.basis,
            "session_status": self.session_status,
            "session_source": self.session_source,
            "claim_status": self.claim_status,
            "claim_source": self.claim_source,
            "post_session_status": self.post_session_status,
            "post_session_artifact_sha": self.post_session_artifact_sha,
            "integrity": self.integrity,
            "label": verification_label(self),
        }


def _counts(block: dict[str, Any]) -> tuple[int | None, int | None]:
    def _int(v: object) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return _int(block.get("checks_passed")), _int(block.get("checks_attempted"))


def _observed_state(status: str) -> str:
    return {
        STATUS_PASSED: STATE_VERIFIED_PASSED,
        STATUS_FAILED: STATE_VERIFIED_FAILED,
        STATUS_PARTIAL: STATE_VERIFIED_PARTIAL,
    }[status]


def _post_session_block(post: object) -> dict[str, Any] | None:
    """The attestation's verification block when it is an OpenShard-executed outcome."""
    if not isinstance(post, dict):
        return None
    ev = parse_verification_block(post.get("verification"))
    if ev.source != SOURCE_DIRECTLY_OBSERVED or ev.observation_mode != MODE_OPENSHARD_EXECUTED:
        return None
    if ev.status not in (STATUS_PASSED, STATUS_FAILED, STATUS_PARTIAL):
        # A re-run that ran nothing (or completed nothing) states no outcome
        # and overrides nothing.
        return None
    return ev.to_dict()


def interpret_evidence(
    verification: object,
    *,
    post_session_verification: object = None,
    integrity: str = INTEGRITY_MISSING,
    canonical_status: str | None = None,
    legacy_status: str | None = None,
) -> VerificationTruth:
    """Interpret a stored ``verification`` block (plus optional re-run and integrity).

    *canonical_status* is the receipt's flat token when it carries an OSN
    ``manual_review`` / ``skipped`` outcome that the block itself cannot
    express; *legacy_status* is the display status of a hand-built receipt
    with no block at all. Never raises.
    """
    integrity_token = integrity if integrity in (INTEGRITY_VALID, INTEGRITY_MISMATCH) else INTEGRITY_MISSING

    session: dict[str, Any] | None = None
    if isinstance(verification, dict):
        ev = parse_verification_block(verification)
        session = ev.to_dict()
    block: dict[str, Any] = session or {}
    session_status = block.get("status") if session else None
    session_source = block.get("source") if session else None
    session_recorded = bool(session) and block.get("observation_mode") != "none"
    s_passed, s_attempted = _counts(block) if session else (None, None)

    claim_status = claim_source = None
    if session_recorded and session_source == SOURCE_AGENT_REPORTED and session_status in (
        STATUS_PASSED, STATUS_FAILED, STATUS_PARTIAL,
    ):
        claim_status, claim_source = session_status, session_source

    raw_skipped = block.get("checks_skipped")
    s_skipped = raw_skipped if isinstance(raw_skipped, int) and not isinstance(raw_skipped, bool) else None

    def build(
        state: str, authority: str, effective: str, basis: str, *,
        post: dict[str, Any] | None = None, passed: int | None = None, attempted: int | None = None,
    ) -> VerificationTruth:
        p_passed, p_attempted = _counts(post) if post else (None, None)
        sha = post.get("artifact_sha") if post else None
        return VerificationTruth(
            state=state, authority=authority, effective_status=effective, basis=basis,
            session_status=session_status, session_source=session_source,
            claim_status=claim_status, claim_source=claim_source,
            post_session_status=post.get("status") if post else None,
            post_session_passed=p_passed, post_session_attempted=p_attempted,
            post_session_artifact_sha=sha if isinstance(sha, str) else None,
            checks_passed=passed, checks_attempted=attempted, checks_skipped=s_skipped,
            integrity=integrity_token,
        )

    # 1. The latest OpenShard re-run describes the current evidence state.
    post = _post_session_block(post_session_verification)
    if post is not None:
        status = str(post.get("status"))
        p_passed, p_attempted = _counts(post)
        return build(
            _observed_state(status), SOURCE_DIRECTLY_OBSERVED, status, BASIS_POST_SESSION,
            post=post, passed=p_passed, attempted=p_attempted,
        )

    # 2. No block at all: a hand-built receipt. Keep the legacy status mapping.
    if not session_recorded:
        if legacy_status is not None:
            text = legacy_status.strip()
            if text.startswith("Checks:"):
                effective = "failed" if "failed" in text.lower() else "passed"
            else:
                effective = _LEGACY_STATUS_TOKENS.get(text, "unknown")
            if effective in ("passed", "failed"):
                return build(_observed_state(effective), SOURCE_DIRECTLY_OBSERVED, effective, BASIS_SESSION)
            if effective == "not_run":
                return build(STATE_NOT_RUN, AUTHORITY_NONE, "not_run", BASIS_SESSION)
        return build(STATE_NOT_OBSERVED, AUTHORITY_NONE, "unknown", BASIS_NONE)

    observed = session_source in OBSERVED_AUTHORITIES
    if observed and canonical_status in ("manual_review", "skipped"):
        state = STATE_MANUAL_REVIEW if canonical_status == "manual_review" else STATE_SKIPPED
        return build(state, str(session_source), canonical_status, BASIS_SESSION,
                     passed=s_passed, attempted=s_attempted)

    # 3. An observed outcome: OpenShard (or an independent system) saw it.
    if observed and session_status in (STATUS_PASSED, STATUS_FAILED, STATUS_PARTIAL):
        return build(_observed_state(str(session_status)), str(session_source), str(session_status),
                     BASIS_SESSION, passed=s_passed, attempted=s_attempted)

    # 4. The agent's own account. A pass is a claim, never a verified pass;
    #    a failure is kept as failed (it can only lower confidence).
    if session_source == SOURCE_AGENT_REPORTED:
        if session_status == STATUS_PASSED:
            return build(STATE_AGENT_REPORTED_PASSED, SOURCE_AGENT_REPORTED, STATUS_UNKNOWN, BASIS_SESSION,
                         passed=s_passed, attempted=s_attempted)
        if session_status == STATUS_PARTIAL:
            return build(STATE_AGENT_REPORTED_PARTIAL, SOURCE_AGENT_REPORTED, STATUS_UNKNOWN, BASIS_SESSION,
                         passed=s_passed, attempted=s_attempted)
        if session_status == STATUS_FAILED:
            return build(STATE_AGENT_REPORTED_FAILED, SOURCE_AGENT_REPORTED, STATUS_FAILED, BASIS_SESSION,
                         passed=s_passed, attempted=s_attempted)

    # 5. Nothing decisive: not run, attempted without an outcome, or unknown.
    if session_status == STATUS_NOT_RUN:
        return build(STATE_NOT_RUN, str(session_source or AUTHORITY_NONE), "not_run", BASIS_SESSION,
                     passed=s_passed, attempted=s_attempted)
    incomplete = block.get("incomplete_reasons")
    attempted_seen = bool(s_attempted) or bool(block.get("checks")) or (
        isinstance(incomplete, list) and REASON_OUTCOME_NOT_OBSERVED in incomplete
    )
    if attempted_seen:
        return build(STATE_ATTEMPTED_UNVERIFIED, str(session_source or AUTHORITY_NONE), STATUS_UNKNOWN,
                     BASIS_SESSION, passed=s_passed, attempted=s_attempted)
    return build(STATE_NOT_OBSERVED, AUTHORITY_NONE, STATUS_UNKNOWN, BASIS_SESSION)


def interpret_receipt(receipt: ShardReceipt) -> VerificationTruth:
    """The interpretation for a built ``ShardReceipt``. Never raises."""
    try:
        canonical = (getattr(receipt, "verification_status", "") or "").strip() or None
        return interpret_evidence(
            getattr(receipt, "verification", None),
            post_session_verification=getattr(receipt, "post_session_verification", None),
            integrity=getattr(receipt, "integrity_status", INTEGRITY_MISSING) or INTEGRITY_MISSING,
            canonical_status=canonical,
            legacy_status=getattr(receipt, "status", None),
        )
    except Exception:
        return interpret_evidence(None)


# ---------------------------------------------------------------------------
# Human wording -- the only place these sentences live
# ---------------------------------------------------------------------------


def _ratio(passed: int | None, attempted: int | None) -> str:
    if attempted:
        return f"{passed or 0}/{attempted} passed"
    return ""


def verification_label(truth: VerificationTruth) -> str:
    """One receipt-row sentence: the claim, who vouches for it, and any older claim."""
    if truth.basis == BASIS_POST_SESSION:
        ratio = _ratio(truth.post_session_passed, truth.post_session_attempted)
        word = {STATE_VERIFIED_PASSED: "Passed", STATE_VERIFIED_FAILED: "Failed",
                STATE_VERIFIED_PARTIAL: "Partial"}[truth.state]
        text = f"{word} (OpenShard re-ran the check(s)"
        if ratio:
            text += f": {ratio}"
        text += f" @ {truth.post_session_artifact_sha[:12]})" if truth.post_session_artifact_sha else (
            "; not bound to a commit)")
        if truth.claim_status:
            text += f"; the agent had reported {truth.claim_status}"
        return text
    ratio = _ratio(truth.checks_passed, truth.checks_attempted)
    if truth.state == STATE_VERIFIED_PASSED:
        who = {SOURCE_INDEPENDENTLY_VERIFIED: "independently verified", SOURCE_GIT_VERIFIED: "git-verified"}.get(
            truth.authority, "OpenShard ran the check(s)")
        return f"Passed ({who}" + (f": {ratio})" if ratio else ")")
    if truth.state == STATE_VERIFIED_FAILED:
        who = {SOURCE_INDEPENDENTLY_VERIFIED: "independently verified", SOURCE_GIT_VERIFIED: "git-verified"}.get(
            truth.authority, "OpenShard ran the check(s)")
        return f"Failed ({who}" + (f": {ratio})" if ratio else ")")
    if truth.state == STATE_VERIFIED_PARTIAL:
        return "Partial (OpenShard observed some outcomes" + (f": {ratio})" if ratio else ")")
    if truth.state == STATE_AGENT_REPORTED_PASSED:
        return "Not verified by OpenShard (agent reported " + (ratio or "passed") + ")"
    if truth.state == STATE_AGENT_REPORTED_PARTIAL:
        return "Not verified by OpenShard (agent reported " + (ratio or "a partial result") + ")"
    if truth.state == STATE_AGENT_REPORTED_FAILED:
        return "Failed per the agent's own report (OpenShard did not run it)"
    if truth.state == STATE_ATTEMPTED_UNVERIFIED:
        return "Not verified (check command seen; outcome not observed)"
    if truth.state == STATE_MANUAL_REVIEW:
        return "Manual review required"
    if truth.state == STATE_SKIPPED:
        return "Skipped"
    if truth.state == STATE_NOT_RUN:
        if truth.checks_skipped:
            return "No checks ran (all skipped)"
        return "Not run (no check observed)"
    return "Not recorded"


def verification_phrase(truth: VerificationTruth) -> str:
    """Lower-case clause for summary sentences (quality summary, trust reasons)."""
    if truth.basis == BASIS_POST_SESSION:
        word = {STATE_VERIFIED_PASSED: "passed", STATE_VERIFIED_FAILED: "failed",
                STATE_VERIFIED_PARTIAL: "partially passed"}[truth.state]
        text = f"verification {word} on OpenShard re-run"
        if truth.claim_status:
            text += f" (agent had reported {truth.claim_status})"
        return text
    return {
        STATE_VERIFIED_PASSED: "verification passed (OpenShard-observed)",
        STATE_VERIFIED_FAILED: "verification failed (OpenShard-observed)",
        STATE_VERIFIED_PARTIAL: "verification partially observed",
        STATE_AGENT_REPORTED_PASSED: "verification not independently observed (agent reported a pass)",
        STATE_AGENT_REPORTED_PARTIAL: "verification not independently observed (agent reported a partial result)",
        STATE_AGENT_REPORTED_FAILED: "verification failed per the agent's report (not run by OpenShard)",
        STATE_ATTEMPTED_UNVERIFIED: "verification attempted, outcome not observed",
        STATE_MANUAL_REVIEW: "verification needs manual review",
        STATE_SKIPPED: "verification skipped",
        STATE_NOT_RUN: "verification not run",
    }.get(truth.state, "verification status unknown")


def integrity_label(integrity: str) -> str:
    """Receipt wording for the checksum state; never implies authorship or a signature."""
    if integrity == INTEGRITY_VALID:
        return "Checksum matches"
    if integrity == INTEGRITY_MISMATCH:
        return "Checksum mismatch (record edited after it was written)"
    return "Not recorded"


INTEGRITY_NOTE = (
    "The checksum is an unkeyed content hash: it detects edits to the stored record; "
    "it does not prove who wrote it."
)
INTEGRITY_MISMATCH_NOTICE = (
    "The stored record no longer matches its checksum; treat every signal on it as unverified."
)


__all__ = [
    "AUTHORITY_NONE",
    "BASIS_NONE",
    "BASIS_POST_SESSION",
    "BASIS_SESSION",
    "EFFECTIVE_TOKENS",
    "INTEGRITY_MISMATCH",
    "INTEGRITY_MISMATCH_NOTICE",
    "INTEGRITY_MISSING",
    "INTEGRITY_NOTE",
    "INTEGRITY_VALID",
    "OBSERVED_AUTHORITIES",
    "OBSERVED_STATES",
    "STATES",
    "STATE_AGENT_REPORTED_FAILED",
    "STATE_AGENT_REPORTED_PARTIAL",
    "STATE_AGENT_REPORTED_PASSED",
    "STATE_ATTEMPTED_UNVERIFIED",
    "STATE_MANUAL_REVIEW",
    "STATE_NOT_OBSERVED",
    "STATE_NOT_RUN",
    "STATE_SKIPPED",
    "STATE_VERIFIED_FAILED",
    "STATE_VERIFIED_PARTIAL",
    "STATE_VERIFIED_PASSED",
    "VerificationTruth",
    "integrity_label",
    "interpret_evidence",
    "interpret_receipt",
    "verification_label",
    "verification_phrase",
]
