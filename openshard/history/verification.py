"""Structured verification evidence for a Receipt (``verification`` block, v1).

Before this module a record's verification lived in two booleans
(``verification_attempted`` / ``verification_passed``) plus, for native OSN
runs only, an ``osn_verification_contract``. ``build_shard_receipt`` turned
the booleans into display strings (``"No checks run"``, ``"Checks
attempted, result not verified"``) and filled the machine
``verification_status`` token from the OSN contract alone. Every
hook-captured, imported or wrapped Receipt therefore crossed the
``history --json`` / sync boundary with ``verification_status = null``,
which the hosted dashboard can only show as "No verification recorded" --
even when a check command was observed. Imports and wraps also stored
``verification_attempted: False`` although they observe nothing, so their
receipts claimed "No checks run".

This module replaces that flattening with one explicit evidence object:

``status``
    ``passed`` | ``failed`` | ``partial`` | ``not_run`` | ``unknown``.
    ``unknown`` covers "checks were attempted but no outcome was observed"
    and "this capture path cannot see checks"; it is never collapsed into
    ``not_run``. ``partial`` means some checks have an observed outcome
    and others do not (and none failed).
``source``
    Who vouches for the outcome, weakest to strongest:
    ``agent_reported`` (a claim made by the agent that OpenShard did not
    independently observe, e.g. a stated result or the agent's own "tool
    failed" signal), ``directly_observed`` (OpenShard itself saw the evidence:
    it ran the check and read its exit code, or a hook event it received
    shows the check command was invoked -- then with status ``unknown`` and
    ``outcome_not_observed`` when no outcome was seen), ``git_verified`` (bound to a commit git confirmed),
    ``independently_verified`` (a CI or other system independent of the
    agent and of OpenShard). ``None`` when nobody observed anything.
``observation_mode``
    How the evidence was obtained (see ``OBSERVATION_MODES``).
``complete`` / ``incomplete_reasons``
    Whether evidence is known to be missing or was dropped as malformed.
``derived``
    ``True`` when the block was computed at read time from an older record
    that has no stored ``verification`` block. Stored records are never
    rewritten: the derivation is recomputed on every read.

Pure, never raises, stores no command output, no argv beyond a short
scrubbed check name, and no environment values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

VERIFICATION_BLOCK_VERSION = 1

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
STATUS_NOT_RUN = "not_run"
STATUS_UNKNOWN = "unknown"
STATUSES: frozenset[str] = frozenset(
    {STATUS_PASSED, STATUS_FAILED, STATUS_PARTIAL, STATUS_NOT_RUN, STATUS_UNKNOWN}
)

CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_SKIPPED = "skipped"
CHECK_UNKNOWN = "unknown"
CHECK_STATUSES: frozenset[str] = frozenset({CHECK_PASSED, CHECK_FAILED, CHECK_SKIPPED, CHECK_UNKNOWN})

SOURCE_AGENT_REPORTED = "agent_reported"
SOURCE_DIRECTLY_OBSERVED = "directly_observed"
SOURCE_GIT_VERIFIED = "git_verified"
SOURCE_INDEPENDENTLY_VERIFIED = "independently_verified"
# Weakest first. Consumers may compare positions; nothing here scores them.
SOURCES: tuple[str, ...] = (
    SOURCE_AGENT_REPORTED,
    SOURCE_DIRECTLY_OBSERVED,
    SOURCE_GIT_VERIFIED,
    SOURCE_INDEPENDENTLY_VERIFIED,
)

MODE_OPENSHARD_EXECUTED = "openshard_executed"  # OpenShard ran it and read the exit code
MODE_HOOK_TOOL_EVENT = "hook_tool_event"  # a received agent hook event showed a check command was invoked
MODE_AGENT_CLAIM = "agent_claim"  # the agent stated a result ("42 tests passed")
MODE_CI_REPORT = "ci_report"  # an independent CI system reported the result
MODE_NOT_OBSERVABLE = "not_observable"  # this capture path cannot see checks (import/wrap)
MODE_LEGACY_BOOLEAN = "legacy_boolean"  # only the pre-v1 booleans were stored
MODE_NONE = "none"  # nothing about verification was recorded
# Historical Ingestion v1: read later from the agent's own history. Any outcome
# found there is ``agent_reported``; never ``directly_observed``.
MODE_IMPORTED_TRANSCRIPT = "imported_transcript"
OBSERVATION_MODES: frozenset[str] = frozenset(
    {
        MODE_OPENSHARD_EXECUTED,
        MODE_HOOK_TOOL_EVENT,
        MODE_AGENT_CLAIM,
        MODE_CI_REPORT,
        MODE_NOT_OBSERVABLE,
        MODE_LEGACY_BOOLEAN,
        MODE_NONE,
        MODE_IMPORTED_TRANSCRIPT,
    }
)

CHECK_KINDS: frozenset[str] = frozenset({"test", "lint", "typecheck", "build", "review", "other"})

# Incomplete-evidence reasons (static vocabulary).
REASON_MALFORMED_BLOCK = "malformed_verification_block"
REASON_MALFORMED_CHECK = "malformed_check_dropped"
REASON_INVALID_STATUS = "invalid_status"
REASON_INCONSISTENT = "status_inconsistent_with_counts"
REASON_OUTCOME_NOT_OBSERVED = "outcome_not_observed"
REASON_CAPTURE_LOSS = "capture_events_lost"
REASON_CHECKS_TRUNCATED = "checks_truncated"
# Verification v2 (``openshard verify``): the working tree was not a clean
# commit, so the result is not bound to an artifact SHA; a check was not run
# to completion (timed out / could not start), so it has no exit code.
REASON_ARTIFACT_NOT_BOUND = "artifact_not_bound"
REASON_CHECK_NOT_COMPLETED = "check_not_completed"

MAX_CHECKS = 20
MAX_NAME = 120
MAX_REASON = 300
MAX_REASONS = 8

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?$")

# Executors whose capture path cannot observe checks at all.
_NOT_OBSERVABLE_EXECUTORS: frozenset[str] = frozenset({"claude_code_import", "claude_code_wrap"})


@dataclass
class VerificationCheck:
    name: str
    status: str = CHECK_UNKNOWN
    kind: str = "other"
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "status": self.status, "exit_code": self.exit_code}


@dataclass
class VerificationEvidence:
    status: str = STATUS_UNKNOWN
    source: str | None = None
    observation_mode: str = MODE_NONE
    checks_attempted: int | None = None
    checks_passed: int | None = None
    checks_failed: int | None = None
    checks_skipped: int | None = None
    checks: list[VerificationCheck] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    artifact_sha: str | None = None
    reason: str | None = None
    complete: bool = True
    incomplete_reasons: list[str] = field(default_factory=list)
    derived: bool = False

    @property
    def recorded(self) -> bool:
        """False only when nothing at all about verification was recorded."""
        return self.observation_mode != MODE_NONE

    def mark_incomplete(self, reason: str) -> None:
        self.complete = False
        if reason not in self.incomplete_reasons and len(self.incomplete_reasons) < MAX_REASONS:
            self.incomplete_reasons.append(reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERIFICATION_BLOCK_VERSION,
            "status": self.status,
            "source": self.source,
            "observation_mode": self.observation_mode,
            "checks_attempted": self.checks_attempted,
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "checks_skipped": self.checks_skipped,
            "checks": [c.to_dict() for c in self.checks[:MAX_CHECKS]],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "artifact_sha": self.artifact_sha,
            "reason": self.reason,
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons[:MAX_REASONS]),
            "derived": self.derived,
        }


# ---------------------------------------------------------------------------
# Small safe coercions
# ---------------------------------------------------------------------------


def _safe_text(value: object, cap: int) -> str | None:
    """Secret-scrubbed, whitespace-collapsed, bounded text; None when empty or unsafe."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        from openshard.safety.sanitize import sanitize_text
        from openshard.security.secret_scan import scrub_text_for_secrets

        scrubbed, _ = scrub_text_for_secrets(value[:2_000], source_label="<verification>")
        return sanitize_text(" ".join(scrubbed.split()), cap) or None
    except Exception:
        return None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _stamp(value: object) -> str | None:
    return value if isinstance(value, str) and _STAMP_RE.match(value) else None


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def aggregate_status(checks: list[VerificationCheck]) -> str:
    """Overall status from per-check outcomes. A failure is never hidden."""
    if not checks:
        return STATUS_NOT_RUN
    statuses = [c.status for c in checks]
    if CHECK_FAILED in statuses:
        return STATUS_FAILED
    ran = [s for s in statuses if s != CHECK_SKIPPED]
    if not ran:
        return STATUS_NOT_RUN
    if all(s == CHECK_PASSED for s in ran):
        return STATUS_PASSED
    if any(s == CHECK_PASSED for s in ran):
        return STATUS_PARTIAL
    return STATUS_UNKNOWN


def _fill_counts(ev: VerificationEvidence) -> None:
    ev.checks_attempted = sum(1 for c in ev.checks if c.status != CHECK_SKIPPED)
    ev.checks_passed = sum(1 for c in ev.checks if c.status == CHECK_PASSED)
    ev.checks_failed = sum(1 for c in ev.checks if c.status == CHECK_FAILED)
    ev.checks_skipped = sum(1 for c in ev.checks if c.status == CHECK_SKIPPED)


def _parse_check(raw: object) -> VerificationCheck | None:
    if not isinstance(raw, dict):
        return None
    name = _safe_text(raw.get("name"), MAX_NAME)
    status = raw.get("status")
    if name is None or status not in CHECK_STATUSES:
        return None
    kind = raw.get("kind") if raw.get("kind") in CHECK_KINDS else "other"
    return VerificationCheck(name=name, status=str(status), kind=str(kind), exit_code=_int_or_none(raw.get("exit_code")))


# ---------------------------------------------------------------------------
# Builders -- for integrations that have real evidence to record
# ---------------------------------------------------------------------------


def build_verification(
    *,
    source: str | None,
    observation_mode: str,
    checks: list[dict] | None = None,
    status: str | None = None,
    checks_attempted: int | None = None,
    checks_passed: int | None = None,
    checks_failed: int | None = None,
    checks_skipped: int | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    duration_seconds: float | None = None,
    exit_code: int | None = None,
    artifact_sha: str | None = None,
    reason: str | None = None,
    incomplete_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Build a stored ``verification`` block from what an integration actually observed.

    Only the arguments given are recorded; nothing is filled in. ``status``
    is computed from *checks* when omitted. The result goes through the same
    validation as a stored block, so a builder can never write something the
    reader would reject.
    """
    raw: dict[str, Any] = {
        "version": VERIFICATION_BLOCK_VERSION,
        "source": source,
        "observation_mode": observation_mode,
        "checks": list(checks or []),
        "checks_attempted": checks_attempted,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "checks_skipped": checks_skipped,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "exit_code": exit_code,
        "artifact_sha": artifact_sha,
        "reason": reason,
        "incomplete_reasons": list(incomplete_reasons or []),
    }
    if status is not None:
        raw["status"] = status
    else:
        parsed = [c for c in (_parse_check(c) for c in raw["checks"]) if c is not None]
        if parsed:
            raw["status"] = aggregate_status(parsed)
        elif checks_attempted:
            raw["status"] = STATUS_UNKNOWN
        else:
            raw["status"] = STATUS_NOT_RUN
    ev = parse_verification_block(raw)
    ev.derived = False
    return ev.to_dict()


def not_observable_verification() -> dict[str, Any]:
    """The block for a capture path that cannot see checks (``import`` / ``wrap``)."""
    return build_verification(
        source=None,
        observation_mode=MODE_NOT_OBSERVABLE,
        status=STATUS_UNKNOWN,
        reason="This capture path does not observe checks; verification was not recorded.",
    )


# ---------------------------------------------------------------------------
# Reading a stored block
# ---------------------------------------------------------------------------


def parse_verification_block(raw: object) -> VerificationEvidence:
    """Validate a stored/received ``verification`` block. Never raises.

    Malformed evidence is never silently dropped: anything unusable makes the
    result ``unknown`` or marks it incomplete with a reason. Counts that
    contradict the declared status turn the status into ``unknown`` rather
    than guessing which of the two is right.
    """
    if not isinstance(raw, dict):
        ev = VerificationEvidence(status=STATUS_UNKNOWN, observation_mode=MODE_LEGACY_BOOLEAN)
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)
        ev.reason = "Verification evidence was present but unreadable."
        return ev
    try:
        return _parse_dict(raw)
    except Exception:
        ev = VerificationEvidence(status=STATUS_UNKNOWN, observation_mode=MODE_LEGACY_BOOLEAN)
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)
        ev.reason = "Verification evidence was present but unreadable."
        return ev


def _parse_dict(raw: dict) -> VerificationEvidence:
    ev = VerificationEvidence()
    mode = raw.get("observation_mode")
    ev.observation_mode = mode if mode in OBSERVATION_MODES else MODE_LEGACY_BOOLEAN
    if mode not in OBSERVATION_MODES:
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)
    source = raw.get("source")
    if source in SOURCES:
        ev.source = source
    elif source is not None:
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)

    raw_checks = raw.get("checks")
    if raw_checks is not None and not isinstance(raw_checks, list):
        ev.mark_incomplete(REASON_MALFORMED_CHECK)
        raw_checks = []
    for item in (raw_checks or [])[: MAX_CHECKS * 2]:
        check = _parse_check(item)
        if check is None:
            ev.mark_incomplete(REASON_MALFORMED_CHECK)
            continue
        if len(ev.checks) >= MAX_CHECKS:
            ev.mark_incomplete(REASON_CHECKS_TRUNCATED)
            break
        ev.checks.append(check)

    ev.checks_attempted = _count(raw.get("checks_attempted"))
    ev.checks_passed = _count(raw.get("checks_passed"))
    ev.checks_failed = _count(raw.get("checks_failed"))
    ev.checks_skipped = _count(raw.get("checks_skipped"))
    if ev.checks and all(v is None for v in (ev.checks_attempted, ev.checks_passed, ev.checks_failed)):
        _fill_counts(ev)

    ev.started_at = _stamp(raw.get("started_at"))
    ev.completed_at = _stamp(raw.get("completed_at"))
    ev.duration_seconds = _duration(raw.get("duration_seconds"))
    ev.exit_code = _int_or_none(raw.get("exit_code"))
    sha = raw.get("artifact_sha")
    ev.artifact_sha = sha.lower() if isinstance(sha, str) and _SHA_RE.match(sha.lower()) else None
    if sha is not None and ev.artifact_sha is None:
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)
    ev.reason = _safe_text(raw.get("reason"), MAX_REASON)
    for r in raw.get("incomplete_reasons") or []:
        if isinstance(r, str) and re.fullmatch(r"[a-z_]{1,64}", r):
            ev.mark_incomplete(r)
    if raw.get("complete") is False:
        ev.complete = False
    ev.derived = bool(raw.get("derived"))

    status = raw.get("status")
    if status not in STATUSES:
        ev.status = STATUS_UNKNOWN
        ev.mark_incomplete(REASON_INVALID_STATUS)
    else:
        ev.status = str(status)
    if not _consistent(ev):
        ev.status = STATUS_UNKNOWN
        ev.mark_incomplete(REASON_INCONSISTENT)
    return ev


def _consistent(ev: VerificationEvidence) -> bool:
    failed = ev.checks_failed or 0
    attempted = ev.checks_attempted
    if ev.status == STATUS_PASSED and failed > 0:
        return False
    if ev.status == STATUS_NOT_RUN and (attempted or 0) > 0:
        return False
    if ev.status == STATUS_FAILED and ev.checks and failed == 0 and not any(
        c.status == CHECK_FAILED for c in ev.checks
    ):
        return False
    if attempted is not None:
        known = (ev.checks_passed or 0) + failed
        if known > attempted:
            return False
    return True


# ---------------------------------------------------------------------------
# Deriving from a record (stored block, or older records' fields)
# ---------------------------------------------------------------------------


def derive_verification(entry: dict) -> VerificationEvidence:
    """The verification evidence for a ``runs.jsonl`` record. Never raises.

    A stored ``verification`` block is authoritative. Otherwise the evidence
    is derived from the fields older writers stored, in order of how
    specific they are, and ``derived`` is set. The record itself is never
    modified.
    """
    if not isinstance(entry, dict):
        ev = VerificationEvidence()
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)
        return ev
    if "verification" in entry and entry.get("verification") is not None:
        return parse_verification_block(entry.get("verification"))
    try:
        ev = _derive_legacy(entry)
    except Exception:
        ev = VerificationEvidence(observation_mode=MODE_LEGACY_BOOLEAN)
        ev.mark_incomplete(REASON_MALFORMED_BLOCK)
    ev.derived = True
    return ev


def _plan_checks(entry: dict, status: str) -> list[VerificationCheck]:
    plan = entry.get("verification_plan")
    if not isinstance(plan, list):
        return []
    checks: list[VerificationCheck] = []
    for cmd in plan[:MAX_CHECKS]:
        if not isinstance(cmd, dict):
            continue
        name = _safe_text(cmd.get("name"), MAX_NAME)
        if not name:
            continue
        kind = cmd.get("kind") if cmd.get("kind") in CHECK_KINDS else "other"
        checks.append(VerificationCheck(name=name, status=status, kind=str(kind)))
    return checks


def _derive_legacy(entry: dict) -> VerificationEvidence:
    osn = entry.get("osn_verification_contract")
    if isinstance(osn, dict) and osn.get("enabled"):
        return _from_osn(entry, osn)

    review = entry.get("review_checks")
    if isinstance(review, list) and review:
        return _from_review_checks(review)

    executor = entry.get("executor")
    capture = entry.get("capture") if isinstance(entry.get("capture"), dict) else None

    attempted = entry.get("verification_attempted")
    passed = entry.get("verification_passed")
    if attempted is None:
        raw_fr = entry.get("final_report")
        fr: dict = raw_fr if isinstance(raw_fr, dict) else {}
        attempted = fr.get("verification_attempted")
        if passed is None:
            passed = fr.get("verification_passed")

    if executor in _NOT_OBSERVABLE_EXECUTORS:
        # import/wrap stored ``verification_attempted: False`` meaning "not
        # recorded", never "no checks ran": they cannot see inside the agent.
        return VerificationEvidence(
            status=STATUS_UNKNOWN,
            observation_mode=MODE_NOT_OBSERVABLE,
            reason="This capture path does not observe checks; verification was not recorded.",
        )

    if capture is not None and isinstance(capture.get("source"), str):
        return _from_hook_record(entry, capture, bool(attempted))

    if attempted is None:
        return VerificationEvidence(status=STATUS_UNKNOWN, observation_mode=MODE_NONE)

    # Native / pipeline records: OpenShard itself ran (or decided not to
    # run) the verification plan and read its exit status.
    if not attempted:
        return VerificationEvidence(
            status=STATUS_NOT_RUN,
            source=SOURCE_DIRECTLY_OBSERVED,
            observation_mode=MODE_OPENSHARD_EXECUTED,
            checks_attempted=0,
            reason="No verification command was run.",
        )
    if passed is True or passed is False:
        check_status = CHECK_PASSED if passed else CHECK_FAILED
        ev = VerificationEvidence(
            status=STATUS_PASSED if passed else STATUS_FAILED,
            source=SOURCE_DIRECTLY_OBSERVED,
            observation_mode=MODE_OPENSHARD_EXECUTED,
            checks=_plan_checks(entry, check_status),
        )
        if ev.checks:
            _fill_counts(ev)
        return ev
    ev = VerificationEvidence(
        status=STATUS_UNKNOWN,
        source=SOURCE_DIRECTLY_OBSERVED,
        observation_mode=MODE_OPENSHARD_EXECUTED,
        checks=_plan_checks(entry, CHECK_UNKNOWN),
        reason="Verification was attempted but its outcome was not recorded.",
    )
    ev.mark_incomplete(REASON_OUTCOME_NOT_OBSERVED)
    if ev.checks:
        _fill_counts(ev)
    return ev


def _from_osn(entry: dict, osn: dict) -> VerificationEvidence:
    raw_status = str(osn.get("status") or "").strip()
    manual = bool(osn.get("manual_review_required"))
    reason = _safe_text(osn.get("skipped_reason") or osn.get("summary"), MAX_REASON)
    rc = _int_or_none(osn.get("returncode"))
    ev = VerificationEvidence(
        source=SOURCE_DIRECTLY_OBSERVED,
        observation_mode=MODE_OPENSHARD_EXECUTED,
        exit_code=rc,
        duration_seconds=_duration(osn.get("duration_seconds")),
        reason=reason,
    )
    if raw_status == "passed":
        ev.status = STATUS_PASSED
        ev.checks = _plan_checks(entry, CHECK_PASSED)
    elif raw_status == "failed":
        ev.status = STATUS_FAILED
        ev.checks = _plan_checks(entry, CHECK_FAILED)
    elif manual:
        ev.status = STATUS_UNKNOWN
        ev.reason = reason or "Manual review required."
    elif raw_status in ("skipped", "impossible", "not_run"):
        ev.status = STATUS_NOT_RUN
        ev.checks_attempted = 0
    else:
        ev.status = STATUS_UNKNOWN
        ev.mark_incomplete(REASON_OUTCOME_NOT_OBSERVED)
    if ev.checks:
        _fill_counts(ev)
    return ev


def _from_review_checks(review: list) -> VerificationEvidence:
    ev = VerificationEvidence(source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_OPENSHARD_EXECUTED)
    for item in review[:MAX_CHECKS]:
        if not isinstance(item, dict):
            ev.mark_incomplete(REASON_MALFORMED_CHECK)
            continue
        name = _safe_text(item.get("name"), MAX_NAME) or "check"
        st = item.get("status")
        status = st if st in (CHECK_PASSED, CHECK_FAILED, CHECK_SKIPPED) else CHECK_UNKNOWN
        ev.checks.append(VerificationCheck(name=name, status=status, kind="review"))
    ev.status = aggregate_status(ev.checks) if ev.checks else STATUS_UNKNOWN
    _fill_counts(ev)
    return ev


def _hook_check_events(entry: dict) -> list[dict]:
    events = entry.get("events")
    if not isinstance(events, list):
        return []
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event_type") != "tool.invoked":
            continue
        raw_meta = ev.get("metadata")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        if meta.get("command_kind") in ("test", "lint"):
            out.append(ev)
    return out


def hook_verification_source(status: str, *, outcome_reported: bool = False) -> str:
    """The evidence source of a hook-observed verification block with *status*.

    OpenShard receives the agent's hook events itself, so an observed check
    invocation -- and the absence of one -- is ``directly_observed``, with
    status ``unknown`` while its outcome was not seen. Any *outcome* a hook
    carries -- the agent's "tool failed" signal, or (verification v2) an exit
    code or error field the agent reports for the command -- is the agent's
    account of a command OpenShard did not run, so it is ``agent_reported``
    however precise it is. Only ``openshard verify`` (OpenShard runs the
    check and reads its exit code) yields a ``directly_observed`` outcome.
    """
    if outcome_reported or status in (STATUS_FAILED, STATUS_PASSED, STATUS_PARTIAL):
        return SOURCE_AGENT_REPORTED
    return SOURCE_DIRECTLY_OBSERVED


def _from_hook_record(entry: dict, capture: dict, attempted: bool) -> VerificationEvidence:
    """A hook-captured session with no stored block (written before v1)."""
    raw_completeness = capture.get("completeness")
    completeness: dict = raw_completeness if isinstance(raw_completeness, dict) else {}
    lost = int(capture.get("hook_events_dropped") or 0) > 0 or completeness.get("status") == "incomplete"
    check_events = _hook_check_events(entry)
    # The hook events themselves are directly observed (see hook_verification_source).
    ev = VerificationEvidence(source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT)
    if attempted or check_events:
        for item in check_events[:MAX_CHECKS]:
            meta: dict = item.get("metadata") or {}
            name = _safe_text(item.get("action"), MAX_NAME) or "check command"
            status = CHECK_FAILED if item.get("status") == "failed" else CHECK_UNKNOWN
            kind = "test" if meta.get("command_kind") == "test" else "lint"
            ev.checks.append(VerificationCheck(name=name, status=status, kind=kind))
        stamps = [s for s in (_stamp(e.get("occurred_at")) for e in check_events) if s]
        ev.started_at = min(stamps) if stamps else None
        if ev.checks:
            _fill_counts(ev)
            ev.status = aggregate_status(ev.checks)
        else:
            # The record says a check ran but its events were not kept.
            ev.status = STATUS_UNKNOWN
            ev.checks_attempted = None
            ev.mark_incomplete(REASON_CAPTURE_LOSS)
        if ev.status == STATUS_UNKNOWN:
            ev.mark_incomplete(REASON_OUTCOME_NOT_OBSERVED)
            ev.reason = "Check command(s) observed through agent hooks; outcome not observed."
        if lost:
            ev.mark_incomplete(REASON_CAPTURE_LOSS)
        ev.source = hook_verification_source(ev.status)
        return ev
    if lost:
        ev.status = STATUS_UNKNOWN
        ev.mark_incomplete(REASON_CAPTURE_LOSS)
        ev.reason = "No check command observed, but some capture events were lost."
        return ev
    ev.status = STATUS_NOT_RUN
    ev.checks_attempted = 0
    ev.reason = "No check command observed in the agent's tool events."
    return ev


# ---------------------------------------------------------------------------
# Projections used by receipts and sync
# ---------------------------------------------------------------------------


def status_token(ev: VerificationEvidence) -> str:
    """The flat ``verification_status`` token for this evidence ("" when nothing was recorded)."""
    return ev.status if ev.recorded else ""


def summary_reason(ev: VerificationEvidence) -> str:
    """A short, safe one-line reason that keeps the evidence source explicit."""
    if not ev.recorded:
        return ""
    parts: list[str] = []
    if ev.checks_attempted is not None and ev.checks_attempted > 0:
        known = []
        if ev.checks_passed:
            known.append(f"{ev.checks_passed} passed")
        if ev.checks_failed:
            known.append(f"{ev.checks_failed} failed")
        parts.append(f"{ev.checks_attempted} check(s) attempted" + (f" ({', '.join(known)})" if known else ""))
    if ev.reason:
        parts.append(ev.reason)
    label = ev.source or "not observed"
    if ev.artifact_sha:
        label += f" @ {ev.artifact_sha[:12]}"
    text = "; ".join(parts) if parts else ev.status.replace("_", " ")
    return f"{text} [{label}]"[:MAX_REASON]


__all__ = [
    "MODE_AGENT_CLAIM",
    "MODE_CI_REPORT",
    "MODE_HOOK_TOOL_EVENT",
    "MODE_LEGACY_BOOLEAN",
    "MODE_NONE",
    "MODE_NOT_OBSERVABLE",
    "MODE_OPENSHARD_EXECUTED",
    "SOURCES",
    "SOURCE_AGENT_REPORTED",
    "SOURCE_DIRECTLY_OBSERVED",
    "SOURCE_GIT_VERIFIED",
    "SOURCE_INDEPENDENTLY_VERIFIED",
    "STATUSES",
    "STATUS_FAILED",
    "STATUS_NOT_RUN",
    "STATUS_PARTIAL",
    "STATUS_PASSED",
    "STATUS_UNKNOWN",
    "VERIFICATION_BLOCK_VERSION",
    "VerificationCheck",
    "VerificationEvidence",
    "aggregate_status",
    "build_verification",
    "derive_verification",
    "hook_verification_source",
    "not_observable_verification",
    "parse_verification_block",
    "status_token",
    "summary_reason",
]
