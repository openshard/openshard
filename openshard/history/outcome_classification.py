"""Outcome classification: what happened, why, and what it may be used for.

Three questions are kept apart on purpose:

* **outcome**: what OpenShard directly observed (``verification_failed``).
* **cause**: who or what is responsible (``model``, ``harness``, ``unknown``...),
  plus ``cause_basis``: ``observed`` only when a recorded fact establishes it,
  ``unproven`` otherwise. A failed check is an observation; "the model caused
  it" is not, so ``verification_failed`` defaults to cause ``unknown``. A bare
  non-zero exit proves the command failed, not that the code was bad; only a
  passing verifier preflight lets a failure be attributed to the model.
* **routing_use**: what the outcome may be used for. ``coding`` and ``format``
  are the only model-quality evidence; ``provider``, ``harness`` and ``policy``
  are operational signals; ``withheld`` is everything unproven. Unknown is a
  valid result and is withheld. A stored claim of model evidence is downgraded
  to ``withheld`` unless its own evidence supports it.

One classification describes one *attempt* (``attempt``, ``model``): a failed
attempt by model A and a successful retry by model B stay separate evidence, and
the final task verification is not used to score whichever model ran last.

Agent claims are metadata, not evidence: a pass counts only when its source is
observed by OpenShard or an independent system.

The classification is written by whoever observes the attempt (``classify``)
into an optional ``outcome_classification`` block. Old records lack it and are
never re-derived or rewritten (``read_classification``, ``routing_use_for_entry``).
``classify_entry`` is for writers classifying a fresh entry, not for back-filling
history.

Evidence is a few bounded, static facts (exit codes, enum tokens, counts). No
prompts, model replies, command output, messages, paths or secrets are stored.
Pure; never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from openshard.history.verification import (
    REASON_CHECK_NOT_COMPLETED,
    STATUS_FAILED,
    STATUS_PASSED,
    derive_verification,
)

OUTCOME_CLASSIFICATION_VERSION = 1

# --- outcomes: what was observed -------------------------------------------
VERIFICATION_PASSED = "verification_passed"
VERIFICATION_FAILED = "verification_failed"
VERIFICATION_INFRA_ERROR = "verification_infra_error"  # the verifier could not run
MODEL_OUTPUT_INVALID = "model_output_invalid"
MODEL_NO_RESPONSE = "model_no_response"
PROVIDER_ERROR = "provider_error"
RATE_LIMITED = "rate_limited"
TIMED_OUT = "timed_out"
HARNESS_ERROR = "harness_error"  # OpenShard or its environment failed
POLICY_BLOCKED = "policy_blocked"
APPROVAL_DENIED = "approval_denied"
APPROVAL_UNAVAILABLE = "approval_unavailable"
TASK_INVALID = "task_invalid"
EXECUTION_FAILED = "execution_failed"
UNKNOWN_FAILURE = "unknown_failure"
COMPLETED_UNVERIFIED = "completed_unverified"  # finished, nothing observed either way
UNCLASSIFIED = "unclassified"  # no classification recorded (old records)

OUTCOMES: frozenset[str] = frozenset(
    {
        VERIFICATION_PASSED, VERIFICATION_FAILED, VERIFICATION_INFRA_ERROR,
        MODEL_OUTPUT_INVALID, MODEL_NO_RESPONSE, PROVIDER_ERROR, RATE_LIMITED,
        TIMED_OUT, HARNESS_ERROR, POLICY_BLOCKED, APPROVAL_DENIED,
        APPROVAL_UNAVAILABLE, TASK_INVALID, EXECUTION_FAILED, UNKNOWN_FAILURE,
        COMPLETED_UNVERIFIED, UNCLASSIFIED,
    }
)

# --- causes ------------------------------------------------------------------
CAUSE_MODEL = "model"
CAUSE_PROVIDER = "provider"
CAUSE_HARNESS = "harness"  # OpenShard, its environment, or its configuration
CAUSE_POLICY = "policy"
CAUSE_USER = "user"
CAUSE_TASK = "task"
CAUSE_UNKNOWN = "unknown"
CAUSES: frozenset[str] = frozenset(
    {CAUSE_MODEL, CAUSE_PROVIDER, CAUSE_HARNESS, CAUSE_POLICY, CAUSE_USER, CAUSE_TASK, CAUSE_UNKNOWN}
)

BASIS_OBSERVED = "observed"
BASIS_UNPROVEN = "unproven"
BASES: frozenset[str] = frozenset({BASIS_OBSERVED, BASIS_UNPROVEN})

# --- routing use ---------------------------------------------------------------
USE_CODING = "coding"  # model-quality evidence: the solution
USE_FORMAT = "format"  # model-quality evidence: the reply shape
USE_PROVIDER = "provider"  # provider reliability, not coding quality
USE_HARNESS = "harness"  # OpenShard/environment
USE_POLICY = "policy"  # policy or approval
USE_WITHHELD = "withheld"  # unproven: must not feed any model statistic
ROUTING_USES: frozenset[str] = frozenset(
    {USE_CODING, USE_FORMAT, USE_PROVIDER, USE_HARNESS, USE_POLICY, USE_WITHHELD}
)
MODEL_QUALITY_USES: frozenset[str] = frozenset({USE_CODING, USE_FORMAT})

# --- static vocabularies a writer can report ---------------------------------
ERROR_KINDS: frozenset[str] = frozenset(
    {
        "rate_limit", "timeout", "provider", "network", "auth", "malformed_reply",
        "no_reply", "harness", "secret_scan",
    }
)
APPROVAL_STATES: frozenset[str] = frozenset({"granted", "denied", "unavailable"})
PREFLIGHT_STATES: frozenset[str] = frozenset({"passed", "failed"})
_VERIFICATION_STATUSES: frozenset[str] = frozenset({"passed", "failed", "partial", "not_run", "unknown"})
# Sources that are observation. Anything else (agent claims, unknown values) fails closed.
_OBSERVED_SOURCES: frozenset[str] = frozenset(
    {"directly_observed", "git_verified", "independently_verified"}
)
_KNOWN_SOURCES: frozenset[str] = _OBSERVED_SOURCES | {"agent_reported"}

# Shell convention: 127 = command not found, 126 = found but not executable.
# Either means the check tooling never ran, whatever the model wrote.
_TOOLING_EXIT_CODES: frozenset[int] = frozenset({126, 127})

_MAX_EXIT_CODES = 8
_MAX_RETRIES = 99
# provider/model style ids only: segments of word chars joined by / : @, so no
# absolute paths, drive letters or whitespace.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(?:[/:@][A-Za-z0-9][A-Za-z0-9._+-]*)*$")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass(frozen=True)
class ObservedFacts:
    """What a writer directly observed. Every field defaults to 'not observed'."""

    verification_status: str | None = None  # passed | failed | ...
    verification_source: str | None = None  # see history.verification.SOURCES
    check_exit_codes: tuple[int, ...] = ()
    check_not_completed: bool = False  # a check timed out or could not start
    # Did the intended verifier demonstrably run in this environment (executable
    # found, right interpreter, pytest importable...) independent of the model's
    # change? Without it a non-zero exit cannot be blamed on the model.
    verifier_preflight: str | None = None  # passed | failed
    error_kind: str | None = None
    http_status: int | None = None  # provider errors only
    policy_denied: bool = False
    approval: str | None = None  # granted | denied | unavailable
    task_invalid: bool = False
    execution_failed: bool = False
    run_failed: bool = False  # a failure was signalled but nothing says why
    retry_count: int | None = None


@dataclass(frozen=True)
class OutcomeClassification:
    outcome: str = UNCLASSIFIED
    cause: str = CAUSE_UNKNOWN
    cause_basis: str = BASIS_UNPROVEN
    evidence: dict[str, Any] = field(default_factory=dict)
    attempt: int | None = None  # 1-based; which attempt this describes
    model: str | None = None  # the model that ran that attempt, when known

    @property
    def routing_use(self) -> str:
        """What this outcome may be used for; ``withheld`` unless evidence supports more."""
        return routing_use_for(self.outcome, self.cause, self.cause_basis, self.evidence)

    @property
    def routing_eligible(self) -> bool:
        """May this outcome influence model-quality statistics?"""
        return self.routing_use in MODEL_QUALITY_USES

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": OUTCOME_CLASSIFICATION_VERSION,
            "outcome": self.outcome,
            "cause": self.cause,
            "cause_basis": self.cause_basis,
            "routing_use": self.routing_use,
            "evidence": dict(self.evidence),
        }
        if self.attempt is not None:
            d["attempt"] = self.attempt
        if self.model is not None:
            d["model"] = self.model
        return d


UNCLASSIFIED_RESULT = OutcomeClassification()

# (outcome, cause) -> use. Any other pairing, including unknown cause, is withheld.
_OPERATIONAL_USE: dict[tuple[str, str], str] = {
    (PROVIDER_ERROR, CAUSE_PROVIDER): USE_PROVIDER,
    (PROVIDER_ERROR, CAUSE_HARNESS): USE_HARNESS,  # bad/missing credentials are configuration
    (RATE_LIMITED, CAUSE_PROVIDER): USE_PROVIDER,
    (HARNESS_ERROR, CAUSE_HARNESS): USE_HARNESS,
    (VERIFICATION_INFRA_ERROR, CAUSE_HARNESS): USE_HARNESS,
    (APPROVAL_UNAVAILABLE, CAUSE_HARNESS): USE_HARNESS,
    (POLICY_BLOCKED, CAUSE_POLICY): USE_POLICY,
}


def routing_use_for(
    outcome: str, cause: str, cause_basis: str, evidence: dict[str, Any] | None = None
) -> str:
    """Deterministic use of an outcome. Model-quality uses need an observed model
    cause *and* evidence that supports it, so a forged or stale block cannot
    promote an outcome; anything unproven is withheld."""
    ev = evidence or {}
    if cause_basis != BASIS_OBSERVED:
        return USE_WITHHELD
    if cause == CAUSE_MODEL:
        if outcome == VERIFICATION_PASSED:
            supported = (
                ev.get("verification_status") == "passed"
                and ev.get("verification_source") in _OBSERVED_SOURCES
            )
            return USE_CODING if supported else USE_WITHHELD
        if outcome == VERIFICATION_FAILED:
            supported = (
                ev.get("verification_status") == "failed"
                and ev.get("verification_source") in _OBSERVED_SOURCES
                and ev.get("verifier_preflight") == "passed"
                and not ev.get("check_not_completed")
                and not any(c in _TOOLING_EXIT_CODES for c in ev.get("check_exit_codes", ()))
            )
            return USE_CODING if supported else USE_WITHHELD
        if outcome == MODEL_OUTPUT_INVALID:
            return USE_FORMAT if ev.get("error_kind") == "malformed_reply" else USE_WITHHELD
        return USE_WITHHELD
    return _OPERATIONAL_USE.get((outcome, cause), USE_WITHHELD)


def should_retry_with_another_model(c: OutcomeClassification) -> bool:
    """A retry/escalation only makes sense after an accountable model failure: a
    verification failure attributed to the model, or malformed model output.
    Infrastructure, provider, policy, approval and unknown causes never do."""
    return c.routing_use in MODEL_QUALITY_USES and c.outcome in {
        VERIFICATION_FAILED,
        MODEL_OUTPUT_INVALID,
    }


def _evidence(f: ObservedFacts) -> dict[str, Any]:
    ev: dict[str, Any] = {}
    if f.verification_status in _VERIFICATION_STATUSES:
        ev["verification_status"] = f.verification_status
    if f.verification_source in _KNOWN_SOURCES:
        ev["verification_source"] = f.verification_source
    codes = [c for c in f.check_exit_codes if _is_int(c)]
    if codes:
        ev["check_exit_codes"] = codes[:_MAX_EXIT_CODES]
    if f.check_not_completed:
        ev["check_not_completed"] = True
    if f.verifier_preflight in PREFLIGHT_STATES:
        ev["verifier_preflight"] = f.verifier_preflight
    if f.error_kind in ERROR_KINDS:
        ev["error_kind"] = f.error_kind
    if _is_int(f.http_status) and 100 <= f.http_status <= 599:  # type: ignore[operator]
        ev["http_status"] = f.http_status
    if f.policy_denied:
        ev["policy_decision"] = "deny"
    if f.approval in APPROVAL_STATES:
        ev["approval"] = f.approval
    if f.task_invalid:
        ev["task_invalid"] = True
    if f.execution_failed:
        ev["execution_state"] = "failed"
    if _is_int(f.retry_count) and 0 <= f.retry_count <= _MAX_RETRIES:  # type: ignore[operator]
        ev["retry_count"] = f.retry_count
    return ev


def _safe_model(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 80 or not _MODEL_ID_RE.match(value):
        return None
    try:
        from openshard.security.secret_scan import scrub_text_for_secrets

        scrubbed, _ = scrub_text_for_secrets(value, source_label="<outcome>")
    except Exception:
        return None
    return value if scrubbed == value else None  # secret-like ids are dropped


def _safe_attempt(value: object) -> int | None:
    if _is_int(value) and 1 <= value <= _MAX_RETRIES + 1:  # type: ignore[operator]
        return value  # type: ignore[return-value]
    return None


# Error kind -> (outcome, cause, basis). Only a malformed reply is model evidence.
_ERROR_KIND_MAP: dict[str, tuple[str, str, str]] = {
    "rate_limit": (RATE_LIMITED, CAUSE_PROVIDER, BASIS_OBSERVED),
    "provider": (PROVIDER_ERROR, CAUSE_PROVIDER, BASIS_OBSERVED),
    "network": (PROVIDER_ERROR, CAUSE_PROVIDER, BASIS_OBSERVED),
    "auth": (PROVIDER_ERROR, CAUSE_HARNESS, BASIS_OBSERVED),  # bad/missing key is configuration
    # A timeout could be a slow provider or an oversized task; we cannot tell.
    "timeout": (TIMED_OUT, CAUSE_UNKNOWN, BASIS_UNPROVEN),
    "no_reply": (MODEL_NO_RESPONSE, CAUSE_PROVIDER, BASIS_UNPROVEN),
    "malformed_reply": (MODEL_OUTPUT_INVALID, CAUSE_MODEL, BASIS_OBSERVED),
    "harness": (HARNESS_ERROR, CAUSE_HARNESS, BASIS_OBSERVED),
    "secret_scan": (POLICY_BLOCKED, CAUSE_POLICY, BASIS_OBSERVED),  # fail-closed pre-send scan
}


def classify(
    facts: ObservedFacts, *, attempt: int | None = None, model: str | None = None
) -> OutcomeClassification:
    """Deterministic classification of one attempt. Earlier rules win: blocks and
    infrastructure are ruled out before a verification result can be read as
    model behaviour."""
    ev = _evidence(facts)

    def done(outcome: str, cause: str, basis: str) -> OutcomeClassification:
        return OutcomeClassification(
            outcome, cause, basis, ev, _safe_attempt(attempt), _safe_model(model)
        )

    if facts.policy_denied:
        return done(POLICY_BLOCKED, CAUSE_POLICY, BASIS_OBSERVED)
    if facts.approval == "denied":
        return done(APPROVAL_DENIED, CAUSE_USER, BASIS_OBSERVED)
    if facts.approval == "unavailable":
        return done(APPROVAL_UNAVAILABLE, CAUSE_HARNESS, BASIS_OBSERVED)
    if facts.task_invalid:
        return done(TASK_INVALID, CAUSE_TASK, BASIS_OBSERVED)
    if facts.error_kind in _ERROR_KIND_MAP:
        return done(*_ERROR_KIND_MAP[facts.error_kind])

    status = facts.verification_status
    observed = facts.verification_source in _OBSERVED_SOURCES  # unknown sources fail closed
    tooling_missing = any(c in _TOOLING_EXIT_CODES for c in facts.check_exit_codes)
    if (
        facts.check_not_completed
        or facts.verifier_preflight == "failed"
        or (status == STATUS_FAILED and tooling_missing)
    ):
        return done(VERIFICATION_INFRA_ERROR, CAUSE_HARNESS, BASIS_OBSERVED)
    if status == STATUS_FAILED and observed:
        if facts.verifier_preflight == "passed":
            return done(VERIFICATION_FAILED, CAUSE_MODEL, BASIS_OBSERVED)
        # Observed failure, unproven cause: a bare non-zero exit does not show
        # the verifier could run, so the environment may be at fault.
        return done(VERIFICATION_FAILED, CAUSE_UNKNOWN, BASIS_UNPROVEN)
    if facts.execution_failed:
        return done(EXECUTION_FAILED, CAUSE_UNKNOWN, BASIS_UNPROVEN)
    if facts.run_failed or status == STATUS_FAILED:
        # Includes a failure only the agent claimed: metadata, not evidence.
        return done(UNKNOWN_FAILURE, CAUSE_UNKNOWN, BASIS_UNPROVEN)
    if status == STATUS_PASSED and observed:
        return done(VERIFICATION_PASSED, CAUSE_MODEL, BASIS_OBSERVED)
    return done(COMPLETED_UNVERIFIED, CAUSE_UNKNOWN, BASIS_UNPROVEN)


# ---------------------------------------------------------------------------
# Reading and building from stored records
# ---------------------------------------------------------------------------

_ERROR_CLASS_KINDS: dict[str, str] = {
    "providerratelimiterror": "rate_limit",
    "providerautherror": "auth",
    "providererror": "provider",
    "presendsecretscanerror": "secret_scan",
    "locktimeouterror": "harness",
    "connectionerror": "network",
    "connecterror": "network",
    "timeouterror": "timeout",
    "readtimeout": "timeout",
    "connecttimeout": "timeout",
}


def _dict(v: object) -> dict:
    return v if isinstance(v, dict) else {}


def facts_from_entry(entry: object) -> ObservedFacts:
    """Extract observed facts from an existing run entry. Never raises.

    Unrecognised error classes become ``execution_failed`` (cause unknown), never
    a model failure. An ``approval_receipt`` with ``granted`` false is recorded
    as ``denied``; ``approval_unavailable`` and ``verifier_preflight`` need an
    explicit writer signal, so an entry alone never attributes a failure to the
    model.
    """
    e = _dict(entry)
    try:
        ev = derive_verification(e)
    except Exception:
        ev = None
    codes: list[int] = []
    not_completed = False
    if ev is not None:
        for c in ev.checks:
            if _is_int(c.exit_code) and c.status == "failed":
                codes.append(c.exit_code)  # type: ignore[arg-type]
        if not codes and ev.status == STATUS_FAILED and _is_int(ev.exit_code):
            codes.append(ev.exit_code)  # type: ignore[arg-type]
        not_completed = REASON_CHECK_NOT_COMPLETED in ev.incomplete_reasons

    raw_class = e.get("error_class")
    error_kind = None
    execution_failed = False
    if isinstance(raw_class, str) and raw_class.strip():
        error_kind = _ERROR_CLASS_KINDS.get(raw_class.strip().lower())
        execution_failed = error_kind is None

    approval = None
    receipt = _dict(e.get("approval_receipt"))
    if receipt:
        approval = "granted" if receipt.get("granted") else "denied"
    decisions = e.get("policy_decisions")
    denied = isinstance(decisions, list) and any(
        isinstance(pd, dict) and pd.get("decision") == "deny" for pd in decisions
    )
    retry = e.get("retry_triggered")
    return ObservedFacts(
        verification_status=ev.status if ev is not None and ev.recorded else None,
        verification_source=ev.source if ev is not None else None,
        check_exit_codes=tuple(codes),
        check_not_completed=not_completed,
        error_kind=error_kind,
        policy_denied=denied,
        approval=approval,
        execution_failed=execution_failed,
        retry_count=(1 if retry is True else 0 if retry is False else None),
    )


def classify_entry(
    entry: object, *, attempt: int | None = None, model: str | None = None
) -> OutcomeClassification:
    """Classify a fresh run entry at write time. Not for back-filling history."""
    return classify(facts_from_entry(entry), attempt=attempt, model=model)


def _clean_evidence(raw: object) -> dict[str, Any]:
    """Re-validate stored evidence through the same bounded vocabulary."""
    r = _dict(raw)
    codes = r.get("check_exit_codes")
    return _evidence(
        ObservedFacts(
            verification_status=r.get("verification_status"),
            verification_source=r.get("verification_source"),
            check_exit_codes=tuple(codes) if isinstance(codes, list) else (),
            check_not_completed=r.get("check_not_completed") is True,
            verifier_preflight=r.get("verifier_preflight"),
            error_kind=r.get("error_kind"),
            http_status=r.get("http_status"),
            policy_denied=r.get("policy_decision") == "deny",
            approval=r.get("approval"),
            task_invalid=r.get("task_invalid") is True,
            execution_failed=r.get("execution_state") == "failed",
            retry_count=r.get("retry_count"),
        )
    )


def parse_classification(raw: object) -> OutcomeClassification:
    """Parse a stored block. Anything malformed or of another version reads as
    unclassified. The stored ``routing_use`` is ignored and recomputed from the
    outcome, cause and evidence, so a stale or edited block cannot promote an
    outcome into model statistics."""
    if not isinstance(raw, dict) or raw.get("version") != OUTCOME_CLASSIFICATION_VERSION:
        return UNCLASSIFIED_RESULT
    outcome, cause, basis = raw.get("outcome"), raw.get("cause"), raw.get("cause_basis")
    if outcome not in OUTCOMES or cause not in CAUSES or basis not in BASES:
        return UNCLASSIFIED_RESULT
    if outcome == UNCLASSIFIED:
        return UNCLASSIFIED_RESULT
    return OutcomeClassification(
        outcome,
        cause,
        basis,
        _clean_evidence(raw.get("evidence")),
        _safe_attempt(raw.get("attempt")),
        _safe_model(raw.get("model")),
    )


def read_classification(entry: object) -> OutcomeClassification:
    """The recorded classification for a run entry; unclassified when absent.

    Never derives one for old records and never mutates *entry*.
    """
    return parse_classification(_dict(entry).get("outcome_classification"))


def routing_use_for_entry(entry: object) -> str:
    """Routing use for any run entry, old or new. Never writes anything.

    A recorded classification decides. Without one, only an already-recorded
    observed verification PASS stays usable as coding evidence; everything else,
    notably an old failure that cannot be told apart from an infrastructure
    failure, is withheld rather than blamed on either side.
    """
    c = read_classification(entry)
    if c.outcome != UNCLASSIFIED:
        return c.routing_use
    try:
        ev = derive_verification(_dict(entry))
    except Exception:
        return USE_WITHHELD
    if ev.recorded and ev.status == STATUS_PASSED and ev.source in _OBSERVED_SOURCES:
        return USE_CODING
    return USE_WITHHELD
