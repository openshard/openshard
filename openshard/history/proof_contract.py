"""Shard Proof Contract v1 - the canonical proof shape of a Shard.

OpenShard's wedge is provable AI coding runs. The Shard is the proof record.
Many pieces already read a Shard their own way (receipts, machine JSON, CI
policy check, trust score, completeness stats, provenance, failure taxonomy).
This module is the single, boring statement of *what proof sections a Shard
should contain*, which are present, which are safe to show, and whether the run
is trustworthy as a whole. Every other piece is a consumer of this shape.

Design constraints (a Staff-level data contract):

* Pure and deterministic. No I/O. ``build_shard_proof_contract`` never raises.
* Reuses the existing safe extraction (``coerce_shard_entry`` +
  ``build_shard_receipt``) instead of re-parsing raw entries. It does not
  reimplement trust, completeness, or provenance algorithms - it references
  their presence and reuses their builders.
* Never fabricates proof: a field that is absent is reported as missing,
  unknown, or not_applicable - never invented as present.
* Never emits unsafe raw content. Output is status tokens, integer counts, and
  static field-name strings only. Every entry-derived string passes through the
  shared sanitizer (path + secret redaction, capped).
* JSON-serializable output only.
* No em dashes anywhere in emitted text.

The contract is read-only over a Shard. It is consumed by, and never consumes,
the trust scorer: trust is a consumer of the Shard, not a proof section inside
it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from openshard.history.proof_signals import (
    secret_scan_finding_count,
    verification_status_from_receipt,
)
from openshard.history.provenance import build_provenance_from_entry
from openshard.history.shard_contract import ShardReceipt, build_shard_receipt
from openshard.history.shard_schema import (
    SHARD_BLOCKED_FIELDS,
    coerce_shard_entry,
    shard_changes_made,
)
from openshard.safety.sanitize import sanitize_text

# ---------------------------------------------------------------------------
# Contract version
# ---------------------------------------------------------------------------

SHARD_PROOF_CONTRACT_VERSION = "1.0"

# Cap for any sanitized detail token.
_MAX_DETAIL_CHARS = 80

# ---------------------------------------------------------------------------
# Requirement levels
# ---------------------------------------------------------------------------

LEVEL_REQUIRED = "required"
LEVEL_RECOMMENDED = "recommended"
LEVEL_OPTIONAL = "optional"
LEVEL_CONDITIONAL = "conditional"

LEVELS: frozenset[str] = frozenset(
    {LEVEL_REQUIRED, LEVEL_RECOMMENDED, LEVEL_OPTIONAL, LEVEL_CONDITIONAL}
)

# ---------------------------------------------------------------------------
# Section status values
# ---------------------------------------------------------------------------

PRESENT = "present"
PARTIAL = "partial"
MISSING = "missing"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
UNSAFE = "unsafe"

SECTION_STATUSES: frozenset[str] = frozenset(
    {PRESENT, PARTIAL, MISSING, UNKNOWN, NOT_APPLICABLE, UNSAFE}
)

# ---------------------------------------------------------------------------
# Overall status values
# ---------------------------------------------------------------------------

OVERALL_STRONG = "strong"
OVERALL_USABLE = "usable"
OVERALL_PARTIAL = "partial"
OVERALL_WEAK = "weak"
OVERALL_UNSAFE = "unsafe"
OVERALL_UNKNOWN = "unknown"

OVERALL_STATUSES: frozenset[str] = frozenset(
    {
        OVERALL_STRONG,
        OVERALL_USABLE,
        OVERALL_PARTIAL,
        OVERALL_WEAK,
        OVERALL_UNSAFE,
        OVERALL_UNKNOWN,
    }
)

# ---------------------------------------------------------------------------
# Canonical section names
# ---------------------------------------------------------------------------

TASK = "task"
REPO_STATE = "repo_state"
EXECUTOR = "executor"
STRATEGY = "strategy"
MODEL = "model"
CONTEXT = "context"
POLICY = "policy"
APPROVAL = "approval"
ACTIONS = "actions"
FILES = "files"
CHECKS = "checks"
VERIFICATION = "verification"
TIMELINE = "timeline"
PROVENANCE = "provenance"
COST = "cost"
RESULT = "result"
NEXT_ACTION = "next_action"


@dataclass(frozen=True)
class ProofSectionDefinition:
    """Static definition of one canonical proof section.

    ``trust`` is deliberately not a section: the trust score is a consumer of
    the Shard, not a proof section inside it.
    """

    name: str
    purpose: str
    level: str
    source_fields: tuple[str, ...]


# The canonical 17 sections, in stable display order. The source_fields are the
# Shard / receipt fields that feed each section (documentation + the value
# echoed back in the section result).
SECTION_DEFINITIONS: tuple[ProofSectionDefinition, ...] = (
    ProofSectionDefinition(
        TASK, "What the run was asked to do.", LEVEL_REQUIRED, ("task",)
    ),
    ProofSectionDefinition(
        REPO_STATE,
        "Repository state the run started from.",
        LEVEL_RECOMMENDED,
        ("git_base_branch", "git_head_commit_hash", "git_dirty"),
    ),
    ProofSectionDefinition(
        EXECUTOR,
        "Which agent / workflow executed the run.",
        LEVEL_REQUIRED,
        ("agent", "workflow"),
    ),
    ProofSectionDefinition(
        STRATEGY,
        "Execution strategy / profile used.",
        LEVEL_RECOMMENDED,
        ("strategy", "execution_profile"),
    ),
    ProofSectionDefinition(
        MODEL, "Model(s) used to perform the work.", LEVEL_REQUIRED, ("execution_model",)
    ),
    ProofSectionDefinition(
        CONTEXT,
        "How much repo context was considered / injected.",
        LEVEL_OPTIONAL,
        ("context_files_injected_count", "context_utilisation_ratio"),
    ),
    ProofSectionDefinition(
        POLICY,
        "Policy / gate decisions recorded for the run.",
        LEVEL_CONDITIONAL,
        ("policy_decisions",),
    ),
    ProofSectionDefinition(
        APPROVAL,
        "Whether approval was required and its outcome.",
        LEVEL_CONDITIONAL,
        ("approval_required", "approval_granted"),
    ),
    ProofSectionDefinition(
        ACTIONS,
        "What the run actually did (file changes / work performed).",
        LEVEL_REQUIRED,
        ("files_detail", "result"),
    ),
    ProofSectionDefinition(
        FILES,
        "Files changed and files inspected as evidence.",
        LEVEL_RECOMMENDED,
        ("files_detail", "file_evidence", "inspected_files"),
    ),
    ProofSectionDefinition(
        CHECKS,
        "Named checks run against the change.",
        LEVEL_RECOMMENDED,
        ("check_results",),
    ),
    ProofSectionDefinition(
        VERIFICATION,
        "Whether the change was verified and the outcome.",
        LEVEL_REQUIRED,
        ("verification_attempted", "verification_passed"),
    ),
    ProofSectionDefinition(
        TIMELINE,
        "Ordered timeline of run events.",
        LEVEL_RECOMMENDED,
        ("run_timeline",),
    ),
    ProofSectionDefinition(
        PROVENANCE,
        "Derived provenance records for proof claims.",
        LEVEL_RECOMMENDED,
        ("evidence_capsules", "policy_decisions", "run_timeline"),
    ),
    ProofSectionDefinition(
        COST, "Cost / duration / token usage.", LEVEL_OPTIONAL, ("estimated_cost",)
    ),
    ProofSectionDefinition(
        RESULT, "The terminal result of the run.", LEVEL_REQUIRED, ("status", "result")
    ),
    ProofSectionDefinition(
        NEXT_ACTION,
        "Recommended next action or feedback outcome.",
        LEVEL_OPTIONAL,
        ("developer_feedback",),
    ),
)

SECTION_NAMES: tuple[str, ...] = tuple(d.name for d in SECTION_DEFINITIONS)

_DEFINITIONS_BY_NAME: dict[str, ProofSectionDefinition] = {
    d.name: d for d in SECTION_DEFINITIONS
}

# Required sections form the "strong Shard" floor.
REQUIRED_SECTIONS: tuple[str, ...] = tuple(
    d.name for d in SECTION_DEFINITIONS if d.level == LEVEL_REQUIRED
)


@dataclass
class ProofSectionResult:
    """Evaluation of one section against a single Shard."""

    name: str
    level: str
    status: str
    detail: str
    source_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Safe token helper
# ---------------------------------------------------------------------------


def _token(value: object, fallback: str) -> str:
    """Return a short, safe, path/secret-redacted token for *value*.

    Falls back to a static token when *value* is empty, unsafe, or drops to
    None under sanitization. Never returns raw content.
    """
    if isinstance(value, str):
        clean = sanitize_text(value, _MAX_DETAIL_CHARS)
        return clean if clean is not None else fallback
    return fallback


# ---------------------------------------------------------------------------
# Per-section evaluators - each takes (receipt, entry) and returns (status, detail)
# Each is pure and tolerant of missing fields; none raise.
# ---------------------------------------------------------------------------


def _eval_task(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if receipt.task_full and receipt.task_full.strip():
        return PRESENT, "recorded"
    return MISSING, "not_recorded"


def _eval_repo_state(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    signals = [
        bool(receipt.git_head_commit_hash),
        bool(receipt.git_base_branch),
        receipt.git_dirty is not None,
    ]
    present = sum(1 for s in signals if s)
    if receipt.git_dirty is True:
        detail = "dirty"
    elif receipt.git_dirty is False:
        detail = "clean"
    else:
        detail = "unknown"
    if present >= 2:
        return PRESENT, detail
    if present == 1:
        return PARTIAL, detail
    return MISSING, "not_recorded"


def _eval_executor(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    agent = (receipt.agent or "").strip()
    if agent and agent != "Not recorded":
        return PRESENT, _token(agent, "recorded")
    return MISSING, "not_recorded"


def _eval_strategy(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    strategy = (receipt.strategy or "").strip()
    if strategy and strategy != "Not recorded":
        return PRESENT, _token(strategy, "recorded")
    return MISSING, "not_recorded"


def _eval_model(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    model = (receipt.model_display or "").strip()
    if not model or model == "Not recorded":
        return MISSING, "not_recorded"
    # Carry routing truth in the detail so the proof cannot imply per-role
    # dispatch when one model ran. Role honesty token comes first so it survives
    # the downstream detail-length cap. Plain ASCII separators only.
    from openshard.history.routing_truth import build_routing_truth
    rt = build_routing_truth(entry)
    # Sanitize the model token on its own first so a path/secret-shaped model
    # value collapses to the fallback before it is embedded in the larger detail
    # string (embedding would otherwise defeat the per-value path/secret guard).
    model_token = _token(model, "recorded")
    detail = f"{rt.role_selection_mode} | mode={rt.routing_mode} | {model_token}"
    return PRESENT, detail


def _eval_context(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if receipt.context_utilisation_ratio is not None:
        return PRESENT, "recorded"
    if receipt.context_files_injected_count is not None or (
        receipt.context_files_considered_count is not None
    ):
        return PARTIAL, "counts_only"
    return MISSING, "not_recorded"


def _eval_policy(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    decisions = receipt.policy_decisions or []
    if decisions:
        return PRESENT, f"{len(decisions)} decision(s)"
    # No policy engaged is a genuine state, not a defect.
    return NOT_APPLICABLE, "none"


def _eval_approval(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if receipt.approval_required:
        if receipt.approval_granted is True:
            return PRESENT, "granted"
        if receipt.approval_granted is False:
            return PRESENT, "denied"
        return PARTIAL, "pending"
    if receipt.approval_granted is not None or (
        receipt.approval_reason and receipt.approval_reason.strip()
    ):
        return PRESENT, "recorded"
    return NOT_APPLICABLE, "not_required"


def _eval_actions(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if shard_changes_made(receipt):
        return PRESENT, f"{receipt.files_changed or 0} file change(s)"
    result = (receipt.result or "").strip()
    if result and result != "Not recorded":
        return PRESENT, "no_file_changes"
    return MISSING, "not_recorded"


def _eval_files(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if receipt.files_detail:
        return PRESENT, f"{len(receipt.files_detail)} changed"
    if receipt.file_evidence or receipt.inspected_files:
        return PARTIAL, "inspected_only"
    return MISSING, "not_recorded"


def _eval_checks(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if receipt.check_results:
        return PRESENT, _token(receipt.checks_display, f"{len(receipt.check_results)} check(s)")
    return MISSING, "not_recorded"


def _eval_verification(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    status = verification_status_from_receipt(receipt)
    if status in ("passed", "failed"):
        return PRESENT, status
    if status in ("not_run", "skipped", "manual_review", "partial"):
        # A recorded not_run / skipped / manual_review is weak proof, not absent
        # proof: the run states what happened, just not a clean pass or fail.
        return PARTIAL, status
    return UNKNOWN, "unknown"


def _eval_timeline(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    events = receipt.run_timeline or []
    if events:
        return PRESENT, f"{len(events)} event(s)"
    return MISSING, "not_recorded"


def _eval_provenance(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    try:
        records = build_provenance_from_entry(entry)
    except Exception:
        records = []
    if records:
        return PRESENT, f"{len(records)} record(s)"
    return MISSING, "not_recorded"


def _eval_cost(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    if receipt.cost_raw is not None:
        return PRESENT, "recorded"
    if receipt.duration_seconds is not None:
        return PARTIAL, "duration_only"
    return MISSING, "not_recorded"


def _eval_result(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    result = (receipt.result or "").strip()
    if result and result != "Not recorded":
        return PRESENT, _token(receipt.status, "recorded")
    return MISSING, "not_recorded"


def _eval_next_action(receipt: ShardReceipt, entry: dict) -> tuple[str, str]:
    df = receipt.developer_feedback
    if isinstance(df, dict) and (df.get("action") or df.get("rating")):
        return PRESENT, "feedback_recorded"
    return MISSING, "not_recorded"


_EVALUATORS = {
    TASK: _eval_task,
    REPO_STATE: _eval_repo_state,
    EXECUTOR: _eval_executor,
    STRATEGY: _eval_strategy,
    MODEL: _eval_model,
    CONTEXT: _eval_context,
    POLICY: _eval_policy,
    APPROVAL: _eval_approval,
    ACTIONS: _eval_actions,
    FILES: _eval_files,
    CHECKS: _eval_checks,
    VERIFICATION: _eval_verification,
    TIMELINE: _eval_timeline,
    PROVENANCE: _eval_provenance,
    COST: _eval_cost,
    RESULT: _eval_result,
    NEXT_ACTION: _eval_next_action,
}


# ---------------------------------------------------------------------------
# Unsafe findings - record-level safety signals (safe tokens only)
# ---------------------------------------------------------------------------


def _blocked_fields_present(entry: object, depth: int = 0) -> list[str]:
    """Return names of blocked fields found in the *raw* entry (bounded depth).

    Walks nested dicts as well as lists/tuples so a blocked field buried inside
    a list of sub-records is still detected. Names come from SHARD_BLOCKED_FIELDS
    (our own static constants), never the field values, so nothing unsafe is
    echoed. Coercion strips these before output; this records that the record
    arrived carrying them.
    """
    if depth > 4:
        return []
    found: list[str] = []
    if isinstance(entry, dict):
        for key, val in entry.items():
            if key in SHARD_BLOCKED_FIELDS:
                found.append(key)
            else:
                found.extend(_blocked_fields_present(val, depth + 1))
    elif isinstance(entry, (list, tuple)):
        for item in entry:
            found.extend(_blocked_fields_present(item, depth + 1))
    # De-duplicate while preserving order; cap to keep output bounded.
    seen: set[str] = set()
    ordered = [f for f in found if not (f in seen or seen.add(f))]  # type: ignore[func-returns-value]  # set.add() returning None is intentional dedup idiom
    return ordered[:10]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _overall_status(
    sections: list[ProofSectionResult],
    missing_required: list[str],
    weak_recommended: list[str],
    unsafe_findings: list[str],
) -> str:
    if unsafe_findings:
        return OVERALL_UNSAFE
    # Strong is the strict grade: every required section must be fully present
    # (not partial/unknown/missing), with no unsafe findings and no weak
    # recommended sections. A partial required section (e.g. verification
    # recorded as "not run") downgrades a run to usable, never strong.
    required_all_present = all(
        s.status == PRESENT for s in sections if s.level == LEVEL_REQUIRED
    )
    if not missing_required:
        if required_all_present and not weak_recommended:
            return OVERALL_STRONG
        return OVERALL_USABLE
    if len(missing_required) <= 2:
        return OVERALL_PARTIAL
    return OVERALL_WEAK


def _empty_contract(overall: str, summary: str) -> dict:
    """Minimal, valid, safe contract used for catastrophic fallback."""
    return {
        "contract_version": SHARD_PROOF_CONTRACT_VERSION,
        "sections": [],
        "overall_status": overall,
        "missing_required_sections": [],
        "weak_recommended_sections": [],
        "unsafe_findings": [],
        "summary": summary,
    }


def build_shard_proof_contract(entry: object) -> dict:
    """Build a Shard Proof Contract v1 view from a run entry. Never raises.

    Accepts current, old, partial, and malformed entries. Returns a
    JSON-serializable dict. Never fabricates proof and never emits unsafe raw
    content.
    """
    try:
        # Record-level safety: detect blocked fields on the raw entry before we
        # strip them. Names only; values are never read.
        blocked = _blocked_fields_present(entry)

        coerced = coerce_shard_entry(entry)
        # Truly malformed input (non-dict) coerces to an invalid_entry marker.
        if coerced.get("_coerce_warning") == "invalid_entry":
            return _empty_contract(OVERALL_UNKNOWN, "input was not a run record")

        receipt = build_shard_receipt(coerced, index=None)

        unsafe_findings: list[str] = []
        for name in blocked:
            unsafe_findings.append(f"blocked_field:{name}")

        secret_findings = secret_scan_finding_count(receipt)
        if secret_findings > 0:
            unsafe_findings.append(f"secret_scan_findings:{secret_findings}")

        sections: list[ProofSectionResult] = []
        for definition in SECTION_DEFINITIONS:
            evaluator = _EVALUATORS[definition.name]
            try:
                status, detail = evaluator(receipt, coerced)
            except Exception:
                status, detail = UNKNOWN, "unknown"
            # A secret scan finding makes the checks section itself unsafe.
            if definition.name == CHECKS and secret_findings > 0:
                status, detail = UNSAFE, "secret_scan_finding"
            sections.append(
                ProofSectionResult(
                    name=definition.name,
                    level=definition.level,
                    status=status,
                    detail=_token(detail, status),
                    source_fields=list(definition.source_fields),
                )
            )

        missing_required = [
            s.name
            for s in sections
            if s.level == LEVEL_REQUIRED and s.status in (MISSING, UNKNOWN)
        ]
        weak_recommended = [
            s.name
            for s in sections
            if s.level == LEVEL_RECOMMENDED and s.status in (MISSING, PARTIAL, UNKNOWN)
        ]

        overall = _overall_status(
            sections, missing_required, weak_recommended, unsafe_findings
        )

        present_count = sum(1 for s in sections if s.status == PRESENT)
        summary = (
            f"{present_count}/{len(sections)} sections present; "
            f"{len(missing_required)} required missing"
        )

        return {
            "contract_version": SHARD_PROOF_CONTRACT_VERSION,
            "sections": [s.to_dict() for s in sections],
            "overall_status": overall,
            "missing_required_sections": missing_required,
            "weak_recommended_sections": weak_recommended,
            "unsafe_findings": unsafe_findings,
            "summary": summary,
        }
    except Exception:
        # Last-resort safe fallback; never propagate an exception to a consumer.
        return _empty_contract(OVERALL_UNKNOWN, "could not evaluate proof contract")


# ---------------------------------------------------------------------------
# Validation - a consumer-facing self-check
# ---------------------------------------------------------------------------


def validate_shard_proof_contract(contract: object) -> list[str]:
    """Return a list of problems with *contract* (empty list means valid).

    Checks shape, known section names, valid status / level enums, and JSON
    serializability. Never raises.
    """
    problems: list[str] = []
    if not isinstance(contract, dict):
        return ["contract is not a dict"]

    required_keys = (
        "contract_version",
        "sections",
        "overall_status",
        "missing_required_sections",
        "weak_recommended_sections",
        "unsafe_findings",
        "summary",
    )
    for key in required_keys:
        if key not in contract:
            problems.append(f"missing key: {key}")

    if not isinstance(contract.get("contract_version"), str):
        problems.append("contract_version is not a string")

    overall = contract.get("overall_status")
    if overall not in OVERALL_STATUSES:
        problems.append(f"invalid overall_status: {overall!r}")

    sections = contract.get("sections")
    if not isinstance(sections, list):
        problems.append("sections is not a list")
    else:
        for i, section in enumerate(sections):
            if not isinstance(section, dict):
                problems.append(f"section[{i}] is not a dict")
                continue
            name = section.get("name")
            if name not in SECTION_NAMES:
                problems.append(f"section[{i}] has unknown name: {name!r}")
            if section.get("level") not in LEVELS:
                problems.append(f"section[{i}] has invalid level: {section.get('level')!r}")
            if section.get("status") not in SECTION_STATUSES:
                problems.append(
                    f"section[{i}] has invalid status: {section.get('status')!r}"
                )
            if not isinstance(section.get("detail"), str):
                problems.append(f"section[{i}] detail is not a string")
            src = section.get("source_fields")
            if not isinstance(src, list) or not all(isinstance(x, str) for x in src):
                problems.append(f"section[{i}] source_fields is not a list of strings")

    for list_key in (
        "missing_required_sections",
        "weak_recommended_sections",
        "unsafe_findings",
    ):
        val = contract.get(list_key)
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            problems.append(f"{list_key} is not a list of strings")

    try:
        import json

        json.dumps(contract)
    except (TypeError, ValueError):
        problems.append("contract is not JSON-serializable")

    return problems
