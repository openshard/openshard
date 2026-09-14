"""Receipt Contract v2: the evidence record for one agent run.

A Shard receipt in v0.4 answers "what ran, what changed, what checks passed".
v0.5 asks the receipt to answer nineteen questions (see
``docs/architecture/RECEIPT_CONTRACT_V2.md``): who owns the work, who
requested it, which agent and model executed it, what it was allowed to do,
which policy governed it, whether approval was required and who granted it,
what evidence was captured, which checks were independent, whether capture
was complete, what it cost to generate, verify and retry, what happened
afterwards, and whether the record was altered.

This module is a **read-time projection**, exactly like ``ShardReceipt``:

* It is built from a persisted run entry (plus, optionally, the other
  attempts of the same Shard) and never persisted back.
* It never raises. Anything it cannot establish is ``None`` or an explicit
  ``"unknown"`` token, never a fabricated value.
* It reads the v0.4.3 fields first and the optional v0.5 namespaced blocks
  (``RECEIPT_V2_FIELDS``) second. A record with none of the new blocks still
  produces a complete contract with honest gaps.
* Every free-text field passes through the shared sanitizer and is bounded.

The receipt **state** is derived from explicit evidence in a fixed priority
order (``derive_receipt_state``), and always carries the reason and the
facts it was derived from. There is no score here; the trust score remains
a separate consumer of the receipt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from openshard.history.shard import (
    CAPTURE_FULL,
    CAPTURE_PARTIAL,
    ORIGIN_OPENSHARD_ROUTED,
    derive_shard_identity,
)
from openshard.history.shard_hash import verify_shard_hash
from openshard.safety.sanitize import sanitize_text

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt

RECEIPT_CONTRACT_VERSION = "2.0"

# ---------------------------------------------------------------------------
# Receipt states
# ---------------------------------------------------------------------------

STATE_VERIFIED = "VERIFIED"
STATE_UNVERIFIED = "UNVERIFIED"
STATE_VERIFICATION_FAILED = "VERIFICATION_FAILED"
STATE_BLOCKED = "BLOCKED"
STATE_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
STATE_APPROVED = "APPROVED"
STATE_VERIFIED_AFTER_RETRY = "VERIFIED_AFTER_RETRY"
STATE_VERIFIED_AFTER_ESCALATION = "VERIFIED_AFTER_ESCALATION"

VALID_STATES: frozenset[str] = frozenset({
    STATE_VERIFIED,
    STATE_UNVERIFIED,
    STATE_VERIFICATION_FAILED,
    STATE_BLOCKED,
    STATE_APPROVAL_REQUIRED,
    STATE_APPROVED,
    STATE_VERIFIED_AFTER_RETRY,
    STATE_VERIFIED_AFTER_ESCALATION,
})

# States in which the run's result was independently or directly verified.
VERIFIED_STATES: frozenset[str] = frozenset({
    STATE_VERIFIED,
    STATE_VERIFIED_AFTER_RETRY,
    STATE_VERIFIED_AFTER_ESCALATION,
})

# ---------------------------------------------------------------------------
# Optional persisted v2 blocks (all top-level keys on the run entry)
# ---------------------------------------------------------------------------

FIELD_ACTORS = "actors"
FIELD_PERMISSIONS = "permissions"
FIELD_POLICY = "policy"
FIELD_APPROVAL = "approval"
FIELD_VERIFIERS = "verifiers"
FIELD_ESCALATION = "escalation"
FIELD_COST_BREAKDOWN = "cost_breakdown"
FIELD_OUTCOME = "outcome"
FIELD_ATTESTATION = "attestation"

RECEIPT_V2_FIELDS: frozenset[str] = frozenset({
    FIELD_ACTORS,
    FIELD_PERMISSIONS,
    FIELD_POLICY,
    FIELD_APPROVAL,
    FIELD_VERIFIERS,
    FIELD_ESCALATION,
    FIELD_COST_BREAKDOWN,
    FIELD_OUTCOME,
    FIELD_ATTESTATION,
})

# Vocabularies. Unknown values are kept only where noted; elsewhere they fall
# back to the ``unknown`` token so consumers can rely on the set.
PRINCIPAL_KINDS: frozenset[str] = frozenset({"user", "agent", "service", "team", "policy", "unknown"})
APPROVAL_STATUSES: frozenset[str] = frozenset({"not_required", "pending", "granted", "denied", "unknown"})
APPROVAL_MECHANISMS: frozenset[str] = frozenset({
    "cli_prompt", "auto_policy", "dashboard", "github_review", "api", "unknown",
})
VERIFIER_KINDS: frozenset[str] = frozenset({
    "test_runner", "linter", "typecheck", "build", "static_check", "human_review",
    "llm_judge", "ci", "openshard_runner", "agent_reported", "unknown",
})
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_UNKNOWN = "unknown"
OUTCOME_STATUSES: frozenset[str] = frozenset({
    "pending", "accepted", "rejected", "merged", "deployed", "rolled_back", "reverted",
    "partial", "unknown",
})

_TEXT = 200
_SHORT = 80
_MAX_LIST = 20


# ---------------------------------------------------------------------------
# Small coercion helpers (never raise)
# ---------------------------------------------------------------------------


def _text(value: object, limit: int = _TEXT) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        value = str(value)
    return sanitize_text(value, limit) or None


def _token(value: object, allowed: frozenset[str], fallback: str = "unknown") -> str:
    if isinstance(value, str) and value in allowed:
        return value
    return fallback


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _str_list(value: object, limit: int = _MAX_LIST) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        t = _text(item, _TEXT)
        if t:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _sum(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present), 6)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Principal:
    """Who or what did something. Owner, requester, executor and approver are
    all Principals, but they are separate fields and never inferred from one
    another."""

    kind: str = "unknown"
    id: str | None = None
    display: str | None = None
    source: str | None = None

    @classmethod
    def from_obj(cls, raw: object, *, default_source: str | None = None) -> Principal | None:
        if isinstance(raw, str):
            t = _text(raw, _SHORT)
            return cls(kind="unknown", id=t, display=t, source=default_source) if t else None
        if not isinstance(raw, dict):
            return None
        kind = _token(raw.get("kind"), PRINCIPAL_KINDS)
        ident = _text(raw.get("id"), _SHORT)
        display = _text(raw.get("display") or raw.get("name"), _SHORT)
        if ident is None and display is None:
            return None
        return cls(kind=kind, id=ident, display=display or ident,
                   source=_text(raw.get("source"), _SHORT) or default_source)

    def label(self) -> str:
        return self.display or self.id or "unknown"


@dataclass
class Actors:
    owner: Principal | None = None
    requested_by: Principal | None = None
    executed_by: Principal | None = None
    approved_by: Principal | None = None


@dataclass
class Execution:
    agent: str = "unknown"
    agent_key: str | None = None
    executor: str | None = None
    workflow: str | None = None
    model: str | None = None
    provider: str | None = None
    models_seen: list[str] = field(default_factory=list)
    model_source: str | None = None


@dataclass
class Permissions:
    requested: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    blocked_commands_count: int = 0
    source: str | None = None


@dataclass
class PolicyRecord:
    policy_id: str | None = None
    policy_version: str | None = None
    name: str | None = None
    source: str | None = None
    decision: str = "not_applicable"
    reason: str | None = None
    evaluated_at: str | None = None
    decisions_count: int = 0
    deny_count: int = 0
    ask_count: int = 0
    decisions: list[dict] = field(default_factory=list)


@dataclass
class ApprovalRecord:
    required: bool = False
    status: str = "not_required"
    approver: Principal | None = None
    approved_at: str | None = None
    mechanism: str | None = None
    reason: str | None = None
    request_action: str | None = None
    request_id: str | None = None


@dataclass
class VerificationCheck:
    name: str
    status: str
    verifier_kind: str = "unknown"
    verifier_id: str | None = None
    independent: bool | None = None
    duration_seconds: float | None = None
    cost_usd: float | None = None
    summary: str | None = None


@dataclass
class VerificationRecord:
    status: str = "unknown"
    reason: str | None = None
    checks: list[VerificationCheck] = field(default_factory=list)
    independent_checks: int = 0
    independent: bool | None = None
    verifier_kinds: list[str] = field(default_factory=list)
    returncode: int | None = None
    duration_seconds: float | None = None
    raw_output_stored: bool = False


@dataclass
class CaptureRecord:
    origin: str = "unknown"
    depth: str = "unknown"
    coverage: str = COVERAGE_UNKNOWN
    executed_by_openshard: bool = False
    completeness_percent: int | None = None
    missing_fields: list[str] = field(default_factory=list)
    hook_events_dropped: int = 0
    session_end_observed: bool | None = None
    task_status: str | None = None
    note: str | None = None


@dataclass
class EscalationRecord:
    occurred: bool = False
    from_model: str | None = None
    to_model: str | None = None
    from_attempt: int | None = None
    to_attempt: int | None = None
    reason: str | None = None
    source: str | None = None


@dataclass
class PriorAttempt:
    attempt_number: int
    run_id: str | None
    model: str | None
    verification_status: str
    cost_usd: float | None


@dataclass
class AttemptsRecord:
    attempt_number: int | None = None
    retry_triggered: bool = False
    retries: int | None = None
    attempts_observed: int | None = None
    prior_attempts: list[PriorAttempt] = field(default_factory=list)
    escalation: EscalationRecord = field(default_factory=EscalationRecord)


@dataclass
class CostRecord:
    generation_usd: float | None = None
    verification_usd: float | None = None
    retry_usd: float | None = None
    total_usd: float | None = None
    attempts_total_usd: float | None = None
    cost_per_verified_success_usd: float | None = None
    currency: str = "USD"
    provenance: str | None = None
    is_estimate: bool = True
    tokens_input: int | None = None
    tokens_output: int | None = None


@dataclass
class OutcomeRecord:
    status: str = "unknown"
    source: str | None = None
    recorded_at: str | None = None
    reference: str | None = None
    feedback_outcome: str | None = None
    human_intervention: bool | None = None


@dataclass
class IntegrityRecord:
    content_hash: str | None = None
    hash_status: str = "missing"
    attestation: dict | None = None


@dataclass
class ReceiptContract:
    contract_version: str
    shard_id: str
    run_id: str | None
    created_at: str
    task: str
    state: str
    state_reason: str
    state_evidence: list[str]
    repository: dict
    timing: dict
    actors: Actors
    execution: Execution
    permissions: Permissions
    policy: PolicyRecord
    approval: ApprovalRecord
    verification: VerificationRecord
    capture: CaptureRecord
    attempts: AttemptsRecord
    cost: CostRecord
    outcome: OutcomeRecord
    integrity: IntegrityRecord
    v2_fields_present: list[str]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe, bounded projection. Stable key set for sync and dashboards."""
        return asdict(self)

    def is_verified(self) -> bool:
        return self.state in VERIFIED_STATES


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_execution(entry: dict, receipt: ShardReceipt) -> Execution:
    agent, _origin, _depth = derive_shard_identity(entry)
    capture = _dict(entry.get("capture"))
    model = (
        entry.get("routing_selected_model")
        or entry.get("execution_model")
        or next(
            (s.get("model") for s in (entry.get("stage_runs") or []) if isinstance(s, dict) and s.get("model")),
            None,
        )
    )
    model = _text(model, _SHORT)
    if model == "unknown":
        model = None
    provider = _text(entry.get("routing_selected_provider") or capture.get("provider"), _SHORT)
    if provider is None and model and "/" in model:
        # A "provider/model" slug names its provider explicitly; nothing is guessed.
        provider = model.split("/", 1)[0]
    return Execution(
        agent=agent,
        agent_key=_text(capture.get("agent") or entry.get("executor") or entry.get("workflow"), _SHORT),
        executor=_text(entry.get("executor"), _SHORT),
        workflow=_text(entry.get("workflow"), _SHORT),
        model=model,
        provider=provider,
        models_seen=_str_list(capture.get("models_seen"), 5),
        model_source=_text(capture.get("model_source"), _SHORT),
    )


def _build_actors(entry: dict, execution: Execution, approval: ApprovalRecord) -> Actors:
    raw = _dict(entry.get(FIELD_ACTORS))
    owner = Principal.from_obj(raw.get("owner"), default_source=FIELD_ACTORS)
    requested_by = Principal.from_obj(raw.get("requested_by"), default_source=FIELD_ACTORS)
    executed_by = Principal.from_obj(raw.get("executed_by"), default_source=FIELD_ACTORS)
    if executed_by is None:
        executed_by = Principal(
            kind="agent",
            id=execution.agent_key,
            display=execution.agent,
            source="derived:executor",
        )
    approved_by = Principal.from_obj(raw.get("approved_by"), default_source=FIELD_ACTORS)
    if approved_by is None and approval.approver is not None:
        approved_by = approval.approver
    return Actors(owner=owner, requested_by=requested_by, executed_by=executed_by, approved_by=approved_by)


def _build_permissions(entry: dict) -> Permissions:
    raw = _dict(entry.get(FIELD_PERMISSIONS))
    command_policy = _dict(entry.get("command_policy"))
    blocked_commands = command_policy.get("blocked_commands")
    return Permissions(
        requested=_str_list(raw.get("requested")),
        used=_str_list(raw.get("used")),
        denied=_str_list(raw.get("denied")),
        allowed_paths=_str_list(command_policy.get("allowed_paths")),
        blocked_paths=_str_list(command_policy.get("blocked_paths")),
        blocked_commands_count=len(blocked_commands) if isinstance(blocked_commands, list) else 0,
        source=_text(raw.get("source"), _SHORT) or ("command_policy" if command_policy else None),
    )


_VALID_DECISIONS = frozenset({"allow", "ask", "deny", "not_applicable"})
_DECISION_RANK = {"not_applicable": -1, "allow": 0, "ask": 1, "deny": 2}


def _build_policy(entry: dict, receipt: ShardReceipt) -> PolicyRecord:
    raw = _dict(entry.get(FIELD_POLICY))
    decisions: list[dict] = []
    for pd in receipt.policy_decisions:
        if not isinstance(pd, dict):
            continue
        decisions.append({
            "decision_id": _text(pd.get("decision_id"), _SHORT),
            "action": _text(pd.get("action"), _SHORT),
            "decision": _token(pd.get("decision"), _VALID_DECISIONS, "not_applicable"),
            "reason": _text(pd.get("reason")),
            "source": _text(pd.get("source"), _SHORT),
            "severity": _text(pd.get("severity"), _SHORT),
            "approval_required": bool(pd.get("approval_required")),
            "approval_granted": _bool(pd.get("approval_granted")),
        })
    active = [d for d in decisions if d["decision"] != "not_applicable"]
    resolved = "not_applicable"
    resolved_reason: str | None = None
    if active:
        top = sorted(active, key=lambda d: -_DECISION_RANK[d["decision"]])[0]
        resolved, resolved_reason = top["decision"], top["reason"]
    explicit_decision = raw.get("decision")
    if isinstance(explicit_decision, str) and explicit_decision in _VALID_DECISIONS:
        resolved = explicit_decision
        resolved_reason = _text(raw.get("reason")) or resolved_reason
    return PolicyRecord(
        policy_id=_text(raw.get("policy_id"), _SHORT),
        policy_version=_text(raw.get("policy_version"), _SHORT),
        name=_text(raw.get("name"), _SHORT),
        source=_text(raw.get("source"), _SHORT) or (decisions[0]["source"] if decisions else None),
        decision=resolved,
        reason=resolved_reason,
        evaluated_at=_text(raw.get("evaluated_at"), _SHORT),
        decisions_count=len(decisions),
        deny_count=sum(1 for d in decisions if d["decision"] == "deny"),
        ask_count=sum(1 for d in decisions if d["decision"] == "ask"),
        decisions=decisions[:_MAX_LIST],
    )


def _build_approval(entry: dict, receipt: ShardReceipt) -> ApprovalRecord:
    raw = _dict(entry.get(FIELD_APPROVAL))
    request = _dict(entry.get("approval_request"))
    legacy_receipt = _dict(entry.get("approval_receipt"))

    required = bool(raw.get("required")) if "required" in raw else (
        bool(request.get("requires_approval")) or bool(legacy_receipt)
    )
    if raw.get("status") in APPROVAL_STATUSES:
        status = str(raw["status"])
    elif not required:
        status = "not_required"
    elif legacy_receipt:
        status = "granted" if legacy_receipt.get("granted") else "denied"
    else:
        status = "pending"
    if status != "not_required":
        required = True

    approver = Principal.from_obj(raw.get("approver"), default_source=FIELD_APPROVAL)
    mechanism = _text(raw.get("mechanism"), _SHORT)
    if mechanism is not None and mechanism not in APPROVAL_MECHANISMS:
        mechanism = "unknown"
    return ApprovalRecord(
        required=required,
        status=status,
        approver=approver,
        approved_at=_text(raw.get("approved_at"), _SHORT),
        mechanism=mechanism,
        reason=_text(raw.get("reason")) or _text(legacy_receipt.get("reason")) or None,
        request_action=_text(raw.get("request_action") or request.get("action"), _SHORT),
        request_id=_text(raw.get("request_id"), _SHORT),
    )


_REVIEW_CHECK_STATUSES = frozenset({"passed", "failed", "skipped"})


def _build_verification(entry: dict, receipt: ShardReceipt, execution: Execution) -> VerificationRecord:
    from openshard.history.proof_signals import verification_status_from_receipt

    status = verification_status_from_receipt(receipt)
    checks: list[VerificationCheck] = []
    seen: set[str] = set()

    # Explicit v2 verifier records win: they carry identity and independence.
    for v in (entry.get(FIELD_VERIFIERS) or [])[:_MAX_LIST] if isinstance(entry.get(FIELD_VERIFIERS), list) else []:
        if not isinstance(v, dict):
            continue
        name = _text(v.get("check") or v.get("name"), _SHORT)
        if not name:
            continue
        checks.append(VerificationCheck(
            name=name,
            status=_token(v.get("status"), frozenset({"passed", "failed", "skipped", "not_run", "unknown"})),
            verifier_kind=_token(v.get("verifier_kind"), VERIFIER_KINDS),
            verifier_id=_text(v.get("verifier_id"), _SHORT),
            independent=_bool(v.get("independent")),
            duration_seconds=_number(v.get("duration_seconds")),
            cost_usd=_number(v.get("cost_usd")),
            summary=_text(v.get("summary")),
        ))
        seen.add(name)

    # OpenShard's own static review checks run outside the model: independent.
    for rc in (entry.get("review_checks") or []) if isinstance(entry.get("review_checks"), list) else []:
        if not isinstance(rc, dict):
            continue
        name = _text(rc.get("name"), _SHORT)
        if not name or name in seen:
            continue
        checks.append(VerificationCheck(
            name=name,
            status=_token(rc.get("status"), _REVIEW_CHECK_STATUSES, "unknown"),
            verifier_kind="static_check",
            verifier_id="openshard.review",
            independent=True,
            summary=_text(rc.get("summary") or rc.get("reason")),
        ))
        seen.add(name)

    # OSN verification contract: OpenShard executed the command itself.
    osn = _dict(entry.get("osn_verification_contract"))
    if osn.get("enabled"):
        for label, st in (
            *((c, "passed") for c in _str_list(osn.get("passed_checks"))),
            *((c, "failed") for c in _str_list(osn.get("failed_checks"))),
            *((c, "skipped") for c in _str_list(osn.get("skipped_checks"))),
        ):
            if label in seen:
                continue
            checks.append(VerificationCheck(
                name=label, status=st, verifier_kind="openshard_runner",
                verifier_id="openshard.native", independent=True,
            ))
            seen.add(label)

    # Pipeline-level verification (verification_attempted/passed) with no
    # per-check detail: represent it once, attributed by who observed it.
    if not checks and entry.get("verification_attempted"):
        executed_by_openshard = execution.executor not in _EXTERNAL_EXECUTORS and (
            "retry_triggered" in entry or execution.executor in ("native", "opencode")
        )
        vp = entry.get("verification_passed")
        checks.append(VerificationCheck(
            name=_text(entry.get("verification_command_summary"), _SHORT) or "verification",
            status="passed" if vp is True else ("failed" if vp is False else "unknown"),
            verifier_kind="openshard_runner" if executed_by_openshard else "agent_reported",
            verifier_id="openshard.pipeline" if executed_by_openshard else execution.agent_key,
            independent=True if executed_by_openshard else False,
        ))

    independent_checks = sum(1 for c in checks if c.independent is True)
    independent: bool | None
    if not checks:
        independent = None
    else:
        independent = independent_checks > 0
    return VerificationRecord(
        status=status,
        reason=_text(receipt.verification_reason) or None,
        checks=checks,
        independent_checks=independent_checks,
        independent=independent,
        verifier_kinds=sorted({c.verifier_kind for c in checks}),
        returncode=receipt.verification_returncode,
        duration_seconds=receipt.verification_duration_seconds,
        raw_output_stored=False,
    )


_EXTERNAL_EXECUTORS = frozenset({
    "claude_code_import", "claude_code_wrap", "claude_code_hooks",
    "codex_hooks", "opencode_plugin", "cursor_hooks",
})


def _build_capture(entry: dict, receipt: ShardReceipt) -> CaptureRecord:
    from openshard.history.completeness import score_receipt

    _agent, origin, depth = derive_shard_identity(entry)
    capture = _dict(entry.get("capture"))
    dropped = _int(capture.get("hook_events_dropped")) or 0
    session_end = _bool(capture.get("session_end_observed"))
    executed_by_openshard = origin == ORIGIN_OPENSHARD_ROUTED

    if executed_by_openshard and depth == CAPTURE_FULL and dropped == 0:
        coverage = COVERAGE_COMPLETE
        note = "OpenShard executed this run and observed every step"
    elif depth == CAPTURE_PARTIAL or dropped > 0 or session_end is False:
        coverage = COVERAGE_PARTIAL
        note = "OpenShard observed this run externally; it did not execute or verify it"
        if dropped:
            note = f"{dropped} hook event(s) were dropped; {note[0].lower()}{note[1:]}"
    else:
        coverage = COVERAGE_UNKNOWN
        note = "capture depth could not be established"

    try:
        scored = score_receipt(receipt)
        completeness = scored.score_percent
        missing = list(scored.missing_fields)
    except Exception:
        completeness, missing = None, []
    return CaptureRecord(
        origin=origin,
        depth=depth,
        coverage=coverage,
        executed_by_openshard=executed_by_openshard,
        completeness_percent=completeness,
        missing_fields=missing,
        hook_events_dropped=dropped,
        session_end_observed=session_end,
        task_status=_text(capture.get("task_status"), _SHORT),
        note=note,
    )


def _prior_attempt_from(entry: dict, index_hint: int) -> PriorAttempt:
    from openshard.history.proof_signals import verification_status_from_receipt
    from openshard.history.shard_contract import build_shard_receipt

    r = build_shard_receipt(entry)
    n = _int(entry.get("attempt_number")) or index_hint
    return PriorAttempt(
        attempt_number=n,
        run_id=_text(entry.get("run_id") or entry.get("timestamp"), _SHORT),
        model=_text(entry.get("routing_selected_model") or entry.get("execution_model"), _SHORT),
        verification_status=verification_status_from_receipt(r),
        cost_usd=_sum([_number(entry.get("estimated_cost")), _number(entry.get("retry_estimated_cost"))]),
    )


def _build_attempts(
    entry: dict, execution: Execution, siblings: list[dict] | None,
) -> AttemptsRecord:
    attempt_number = _int(entry.get("attempt_number"))
    retry_triggered = bool(entry.get("retry_triggered"))
    run_id = entry.get("run_id") or entry.get("timestamp")

    prior: list[PriorAttempt] = []
    for i, sib in enumerate(siblings or [], start=1):
        if not isinstance(sib, dict):
            continue
        if (sib.get("run_id") or sib.get("timestamp")) == run_id:
            continue
        if attempt_number is not None:
            sib_n = _int(sib.get("attempt_number"))
            if sib_n is not None and sib_n >= attempt_number:
                continue
        prior.append(_prior_attempt_from(sib, i))
    prior.sort(key=lambda p: p.attempt_number)

    retries: int | None
    if attempt_number is not None and attempt_number > 1:
        retries = attempt_number - 1
    elif retry_triggered:
        retries = 1
    elif attempt_number == 1 or prior == []:
        retries = 0 if attempt_number is not None else None
    else:
        retries = len(prior)
    attempts_observed = (len(prior) + 1) if (prior or attempt_number is not None) else None

    raw_esc = _dict(entry.get(FIELD_ESCALATION))
    escalation = EscalationRecord(
        occurred=bool(raw_esc.get("occurred")),
        from_model=_text(raw_esc.get("from_model"), _SHORT),
        to_model=_text(raw_esc.get("to_model"), _SHORT),
        from_attempt=_int(raw_esc.get("from_attempt")),
        to_attempt=_int(raw_esc.get("to_attempt")),
        reason=_text(raw_esc.get("reason")),
        source=FIELD_ESCALATION if raw_esc else None,
    )
    if not escalation.occurred and prior and execution.model:
        # Derived only from directly observed sibling attempts: a different
        # model on an earlier attempt of the same Shard is an escalation.
        earlier = [p for p in prior if p.model and p.model != execution.model]
        if earlier:
            first = earlier[-1]
            escalation = EscalationRecord(
                occurred=True,
                from_model=first.model,
                to_model=execution.model,
                from_attempt=first.attempt_number,
                to_attempt=attempt_number,
                reason=f"attempt {first.attempt_number} ended {first.verification_status}",
                source="derived:sibling_attempts",
            )
    return AttemptsRecord(
        attempt_number=attempt_number,
        retry_triggered=retry_triggered,
        retries=retries,
        attempts_observed=attempts_observed,
        prior_attempts=prior,
        escalation=escalation,
    )


def _build_cost(
    entry: dict, receipt: ShardReceipt, verification: VerificationRecord,
    attempts: AttemptsRecord, verified: bool,
) -> CostRecord:
    raw = _dict(entry.get(FIELD_COST_BREAKDOWN))
    generation = _number(raw.get("generation_usd"))
    if generation is None:
        generation = _number(receipt.cost_raw)
    retry = _number(raw.get("retry_usd"))
    if retry is None:
        retry = _number(entry.get("retry_estimated_cost"))
    verification_cost = _number(raw.get("verification_usd"))
    if verification_cost is None:
        verification_cost = _sum([c.cost_usd for c in verification.checks])
    total = _number(raw.get("total_usd"))
    if total is None:
        total = _sum([generation, retry, verification_cost])

    prior_total = _sum([p.cost_usd for p in attempts.prior_attempts])
    attempts_total = _sum([total, prior_total]) if total is not None else None
    per_success = attempts_total if (verified and attempts_total is not None) else None

    provenance = _text(raw.get("provenance"), _SHORT) or _text(receipt.cost_provenance, _SHORT)
    if provenance is None and generation is not None:
        provenance = "openshard_estimated"
    return CostRecord(
        generation_usd=generation,
        verification_usd=verification_cost,
        retry_usd=retry,
        total_usd=total,
        attempts_total_usd=attempts_total,
        cost_per_verified_success_usd=per_success,
        currency=_text(raw.get("currency"), 8) or "USD",
        provenance=provenance,
        is_estimate=True,
        tokens_input=receipt.tokens_input,
        tokens_output=receipt.tokens_output,
    )


def _build_outcome(entry: dict, receipt: ShardReceipt) -> OutcomeRecord:
    raw = _dict(entry.get(FIELD_OUTCOME))
    feedback = receipt.developer_feedback if isinstance(receipt.developer_feedback, dict) else {}
    feedback_outcome = _text(feedback.get("outcome"), _SHORT)
    human = _bool(raw.get("human_intervention"))
    if human is None and feedback:
        human = bool(feedback.get("manual_fix_required")) or None
    return OutcomeRecord(
        status=_token(raw.get("status"), OUTCOME_STATUSES),
        source=_text(raw.get("source"), _SHORT),
        recorded_at=_text(raw.get("recorded_at"), _SHORT),
        reference=_text(raw.get("reference"), _SHORT),
        feedback_outcome=feedback_outcome,
        human_intervention=human,
    )


def _build_integrity(entry: dict) -> IntegrityRecord:
    result = verify_shard_hash(entry)
    raw = entry.get(FIELD_ATTESTATION)
    attestation: dict | None = None
    if isinstance(raw, dict) and raw:
        attestation = {
            "kind": _text(raw.get("kind"), _SHORT),
            "signer": _text(raw.get("signer"), _SHORT),
            "signed_at": _text(raw.get("signed_at"), _SHORT),
            "present": bool(raw.get("signature")),
        }
    return IntegrityRecord(
        content_hash=result["stored_hash"],
        hash_status=result["status"],
        attestation=attestation,
    )


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def derive_receipt_state(
    *,
    verification_status: str,
    policy_decision: str,
    approval_status: str,
    retries: int | None,
    escalation_occurred: bool,
) -> tuple[str, str, list[str]]:
    """Derive ``(state, reason, evidence)`` from explicit facts, in priority order.

    1. A policy deny or a denied approval blocks the run: ``BLOCKED``.
    2. Approval required and still pending: ``APPROVAL_REQUIRED``.
    3. Verification failed: ``VERIFICATION_FAILED``.
    4. Verification passed: ``VERIFIED``, or ``VERIFIED_AFTER_ESCALATION`` when
       a model escalation was observed, or ``VERIFIED_AFTER_RETRY`` when a
       retry was.
    5. Otherwise the result is not verified: ``APPROVED`` when a human or
       policy granted approval (approval is not verification), else
       ``UNVERIFIED``.
    """
    evidence: list[str] = [
        f"verification={verification_status}",
        f"policy={policy_decision}",
        f"approval={approval_status}",
    ]
    if retries:
        evidence.append(f"retries={retries}")
    if escalation_occurred:
        evidence.append("escalation=observed")

    if policy_decision == "deny":
        return STATE_BLOCKED, "a policy decision denied the run", evidence
    if approval_status == "denied":
        return STATE_BLOCKED, "approval was required and denied", evidence
    if approval_status == "pending":
        return STATE_APPROVAL_REQUIRED, "approval is required and has not been recorded", evidence
    if verification_status == "failed":
        return STATE_VERIFICATION_FAILED, "verification ran and failed", evidence
    if verification_status == "passed":
        if escalation_occurred:
            return STATE_VERIFIED_AFTER_ESCALATION, "verification passed after escalating to a stronger model", evidence
        if retries:
            return STATE_VERIFIED_AFTER_RETRY, f"verification passed after {retries} retry(ies)", evidence
        return STATE_VERIFIED, "verification passed", evidence
    if approval_status == "granted":
        return STATE_APPROVED, "approval was granted but the result was not verified", evidence
    reason = {
        "not_run": "no verification was run",
        "skipped": "verification was skipped",
        "manual_review": "verification needs manual review",
    }.get(verification_status, "verification outcome is unknown")
    return STATE_UNVERIFIED, reason, evidence


def receipt_state_for_receipt(entry: dict, receipt: ShardReceipt) -> tuple[str, str]:
    """Cheap ``(state, reason)`` for an already-built ``ShardReceipt``.

    Used by ``build_shard_receipt`` so the compact/full renderers can show
    the state without building the whole contract. Sibling attempts are not
    consulted here, so escalation is only seen through an explicit
    ``escalation`` block. Never raises.
    """
    try:
        from openshard.history.proof_signals import verification_status_from_receipt

        policy = _build_policy(entry, receipt)
        approval = _build_approval(entry, receipt)
        attempt_number = _int(entry.get("attempt_number"))
        retries = (attempt_number - 1) if attempt_number and attempt_number > 1 else (
            1 if entry.get("retry_triggered") else 0
        )
        esc = _dict(entry.get(FIELD_ESCALATION))
        state, reason, _ = derive_receipt_state(
            verification_status=verification_status_from_receipt(receipt),
            policy_decision=policy.decision,
            approval_status=approval.status,
            retries=retries,
            escalation_occurred=bool(esc.get("occurred")),
        )
        return state, reason
    except Exception:
        return STATE_UNVERIFIED, "state could not be derived"


def entry_has_v2_fields(entry: object) -> list[str]:
    """Names of the optional v2 blocks present (and non-empty) on *entry*."""
    if not isinstance(entry, dict):
        return []
    return sorted(k for k in RECEIPT_V2_FIELDS if entry.get(k))


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_receipt_contract(
    entry: object,
    *,
    index: int | None = None,
    siblings: list[dict] | None = None,
    outcome_record: dict | None = None,
) -> ReceiptContract:
    """Build the v2 contract for one run entry. Never raises.

    ``siblings`` are the other persisted attempts of the same Shard (any
    order, may include *entry* itself); they are the only source for
    observed retries/escalation beyond the entry's own attempt fields.
    ``outcome_record`` is a later outcome recorded beside the run (see
    ``openshard.history.outcomes``); it overrides any ``outcome`` block the
    producer wrote at run time because it is newer.
    """
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.shard_schema import coerce_shard_entry

    # Integrity is checked on the record as given: coercion stamps a hash on
    # legacy records, which must not be reported as a "valid" stored hash.
    integrity = _build_integrity(entry if isinstance(entry, dict) else {})
    safe = coerce_shard_entry(entry)
    if isinstance(outcome_record, dict) and outcome_record:
        safe[FIELD_OUTCOME] = dict(outcome_record)
    receipt = build_shard_receipt(safe, index)
    try:
        return _build(safe, receipt, siblings, integrity)
    except Exception:
        return _fallback_contract(safe, receipt, integrity)


def _build(
    entry: dict, receipt: ShardReceipt, siblings: list[dict] | None, integrity: IntegrityRecord,
) -> ReceiptContract:
    execution = _build_execution(entry, receipt)
    approval = _build_approval(entry, receipt)
    actors = _build_actors(entry, execution, approval)
    permissions = _build_permissions(entry)
    policy = _build_policy(entry, receipt)
    verification = _build_verification(entry, receipt, execution)
    capture = _build_capture(entry, receipt)
    attempts = _build_attempts(entry, execution, siblings)

    state, reason, evidence = derive_receipt_state(
        verification_status=verification.status,
        policy_decision=policy.decision,
        approval_status=approval.status,
        retries=attempts.retries,
        escalation_occurred=attempts.escalation.occurred,
    )
    cost = _build_cost(entry, receipt, verification, attempts, state in VERIFIED_STATES)
    outcome = _build_outcome(entry, receipt)

    repository = {
        "repo": _text(receipt.repo, _SHORT),
        "repo_identity": _text(entry.get("repo_identity"), _SHORT),
        "branch": _text(receipt.branch, _SHORT),
        "head_commit": _text(receipt.git_head_commit_hash, _SHORT),
        "base_branch": _text(receipt.git_base_branch, _SHORT),
        "base_commit": _text(receipt.git_base_commit_hash, _SHORT),
        "dirty": receipt.git_dirty,
    }
    capture_block = _dict(entry.get("capture"))
    timing = {
        "started_at": _text(capture_block.get("started_at") or receipt.created_at, _SHORT),
        "ended_at": _text(capture_block.get("last_activity_at"), _SHORT),
        "duration_seconds": _number(receipt.duration_seconds),
    }
    return ReceiptContract(
        contract_version=RECEIPT_CONTRACT_VERSION,
        shard_id=receipt.shard_id,
        run_id=receipt.run_id,
        created_at=receipt.created_at,
        task=receipt.task_full[:1000],
        state=state,
        state_reason=reason,
        state_evidence=evidence,
        repository=repository,
        timing=timing,
        actors=actors,
        execution=execution,
        permissions=permissions,
        policy=policy,
        approval=approval,
        verification=verification,
        capture=capture,
        attempts=attempts,
        cost=cost,
        outcome=outcome,
        integrity=integrity,
        v2_fields_present=entry_has_v2_fields(entry),
    )


def _fallback_contract(entry: dict, receipt: ShardReceipt, integrity: IntegrityRecord) -> ReceiptContract:
    return ReceiptContract(
        contract_version=RECEIPT_CONTRACT_VERSION,
        shard_id=receipt.shard_id,
        run_id=receipt.run_id,
        created_at=receipt.created_at,
        task=receipt.task_full[:1000],
        state=STATE_UNVERIFIED,
        state_reason="contract could not be fully derived",
        state_evidence=["build=failed"],
        repository={},
        timing={},
        actors=Actors(),
        execution=Execution(agent=receipt.agent),
        permissions=Permissions(),
        policy=PolicyRecord(),
        approval=ApprovalRecord(),
        verification=VerificationRecord(),
        capture=CaptureRecord(),
        attempts=AttemptsRecord(),
        cost=CostRecord(),
        outcome=OutcomeRecord(),
        integrity=integrity,
        v2_fields_present=entry_has_v2_fields(entry),
    )


# ---------------------------------------------------------------------------
# The nineteen questions
# ---------------------------------------------------------------------------


def _money(v: float | None) -> str:
    return "Not recorded" if v is None else f"${v:.4f}"


def _principal(p: Principal | None) -> str:
    return p.label() if p is not None else "Not recorded"


def receipt_questions(c: ReceiptContract) -> list[tuple[str, str]]:
    """Plain-language answers to the questions a v2 receipt must answer.

    Pure; every value is short and safe to print. ``"Not recorded"`` is the
    honest answer whenever the receipt has no evidence.
    """
    v = c.verification
    checks = (
        f"{len(v.checks)} check(s), {v.independent_checks} independent"
        if v.checks else "No checks recorded"
    )
    attempts = (
        f"{c.attempts.attempts_observed} attempt(s), {c.attempts.retries or 0} retry(ies)"
        if c.attempts.attempts_observed is not None else "Not recorded"
    )
    if c.attempts.escalation.occurred:
        attempts += f"; escalated {c.attempts.escalation.from_model} -> {c.attempts.escalation.to_model}"
    exec_label = c.execution.agent
    if c.execution.model:
        exec_label += f" / {c.execution.model}"
    policy = c.policy.name or c.policy.policy_id or (
        f"{c.policy.decisions_count} decision(s) from {c.policy.source}" if c.policy.decisions_count else "Not recorded"
    )
    approval = c.approval.status
    if c.approval.status == "not_required":
        approval = "Not required"
    approver = _principal(c.actors.approved_by) if c.approval.status in ("granted", "denied") else "Not applicable"
    if c.approval.mechanism and c.approval.status == "granted":
        approver += f" ({c.approval.mechanism})"
    permissions = (
        ", ".join(c.permissions.used[:5]) if c.permissions.used
        else (", ".join(c.permissions.requested[:5]) if c.permissions.requested else "Not recorded")
    )
    outcome = c.outcome.status if c.outcome.status != "unknown" else (
        f"feedback: {c.outcome.feedback_outcome}" if c.outcome.feedback_outcome else "Not recorded"
    )
    return [
        ("What was asked", c.task[:120] or "Not recorded"),
        ("Owner", _principal(c.actors.owner)),
        ("Requested by", _principal(c.actors.requested_by)),
        ("Executed by", exec_label),
        ("Allowed to", permissions),
        ("Policy", policy),
        ("Approval", approval),
        ("Approved by", approver),
        ("Checks", f"{v.status} ({checks})"),
        ("Independent", "yes" if v.independent else ("no" if v.independent is False else "Not recorded")),
        ("Capture", f"{c.capture.coverage} ({c.capture.completeness_percent}% of fields)" if c.capture.completeness_percent is not None else c.capture.coverage),
        ("State", c.state),
        ("Generation cost", _money(c.cost.generation_usd)),
        ("Checks cost", _money(c.cost.verification_usd)),
        ("Retry cost", _money(c.cost.retry_usd)),
        ("Attempts", attempts),
        ("Cost per verified success", _money(c.cost.cost_per_verified_success_usd)),
        ("Outcome", outcome),
        ("Integrity", f"content hash {c.integrity.hash_status}"),
    ]


def render_receipt_state_block(c: ReceiptContract, *, indent: str = "  ", col: int = 12) -> list[str]:
    """Lines for the RECEIPT STATE section of the full receipt. Pure."""

    def row(label: str, value: str) -> str:
        return f"{indent}{label:<{col}}{value}"

    lines = [f"{indent}RECEIPT STATE", row("State", c.state), row("Because", c.state_reason)]
    if c.actors.owner or c.actors.requested_by or c.actors.approved_by:
        lines.append(row("Owner", _principal(c.actors.owner)))
        lines.append(row("Requested", _principal(c.actors.requested_by)))
        if c.actors.approved_by:
            lines.append(row("Approved by", _principal(c.actors.approved_by)))
    if c.policy.name or c.policy.policy_id:
        pol = c.policy.name or c.policy.policy_id or ""
        if c.policy.policy_version:
            pol += f" v{c.policy.policy_version}"
        lines.append(row("Policy", f"{pol}: {c.policy.decision}"))
    if c.verification.checks:
        lines.append(row(
            "Verified by",
            f"{c.verification.independent_checks}/{len(c.verification.checks)} independent "
            f"({', '.join(c.verification.verifier_kinds)})",
        ))
    lines.append(row("Capture", c.capture.coverage))
    if c.attempts.attempts_observed and c.attempts.attempts_observed > 1:
        lines.append(row("Attempts", str(c.attempts.attempts_observed)))
    if c.attempts.escalation.occurred:
        lines.append(row("Escalated", f"{c.attempts.escalation.from_model} -> {c.attempts.escalation.to_model}"))
    if c.cost.total_usd is not None and (
        c.cost.verification_usd is not None or c.cost.retry_usd is not None
    ):
        parts = [f"gen {_money(c.cost.generation_usd)}"]
        if c.cost.verification_usd is not None:
            parts.append(f"verify {_money(c.cost.verification_usd)}")
        if c.cost.retry_usd is not None:
            parts.append(f"retry {_money(c.cost.retry_usd)}")
        lines.append(row("Cost split", ", ".join(parts)))
    if c.cost.cost_per_verified_success_usd is not None:
        lines.append(row("Per success", _money(c.cost.cost_per_verified_success_usd)))
    if c.outcome.status != "unknown":
        lines.append(row("Outcome", c.outcome.status))
    return lines
