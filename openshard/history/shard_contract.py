from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from openshard.history.capture_completeness import (
    derive_capture_completeness,
)
from openshard.history.receipt_evidence import project_entry_evidence, recorded_line_counts
from openshard.history.receipt_identity import stored_receipt_id
from openshard.history.run_cost import run_total_cost
from openshard.history.shard import (
    Shard,
    build_shard,
    control_evidence_view,
    derive_shard_identity,
)
from openshard.history.shard_hash import integrity_as_stored, verify_shard_hash
from openshard.history.task_identity import stored_task_id
from openshard.history.task_title import derive_task_title, resolve_task_title
from openshard.history.usage_evidence import effective_usage
from openshard.history.verification import (
    REASON_OUTCOME_NOT_OBSERVED,
    SOURCE_AGENT_REPORTED,
    STATUS_FAILED,
    STATUS_NOT_RUN,
    STATUS_PARTIAL,
    STATUS_PASSED,
    VerificationEvidence,
    derive_verification,
    status_token,
    summary_reason,
)
from openshard.history.verification_truth import (
    counts_phrase,
    integrity_label,
    interpret_receipt,
    replaced_failures_phrase,
    verification_label,
)
from openshard.models.pricing import (
    COST_PROVENANCE_OFFICIAL_RATE,
    estimate_usage_cost,
    single_pricing_model,
)
from openshard.verification.gaming_signals import gaming_signals_block

_PROFILE_TO_STRATEGY: dict[str, str] = {
    "native_light": "Single",
    "native_deep": "Plan + Execute",
    "native_swarm": "Multi-stage",
}

_RISK_LABELS: dict[str, str] = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}

_STAGE_DISPLAY_LABELS: dict[str, str] = {
    "planning": "Planning",
    "implementation": "Execution",
    "analysis": "Analysis",
    "ask": "Ask",
    "verify": "Verify",
    "retry": "Retry",
}

# Full provider/slug → friendly display name.
# Keys are lowercase. Values use full model family names (not abbreviations).
_MODEL_FRIENDLY_NAMES: dict[str, str] = {
    "deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek/deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "deepseek/deepseek-v4-flash": "DeepSeek V4 Flash",
    "anthropic/claude-sonnet-4.6": "Claude Sonnet 4.6",
    "anthropic/claude-sonnet-4-6": "Claude Sonnet 4.6",
    "anthropic/claude-opus-4.7": "Claude Opus 4.7",
    "anthropic/claude-opus-4-7": "Claude Opus 4.7",
    "anthropic/claude-haiku-4.5": "Claude Haiku 4.5",
    "anthropic/claude-haiku-4-5": "Claude Haiku 4.5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-sonnet-4.6": "Claude Sonnet 4.6",
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4.7": "Claude Opus 4.7",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "claude-haiku-4.5": "Claude Haiku 4.5",
    # Current Claude models (platform.claude.com models overview, 2026-10-03).
    # Direct API ids use dashes, OpenRouter slugs use dots; without these the
    # slug formatter would render "claude-opus-5-5" as "Claude Opus 5 5".
    "claude-fable-5-1": "Claude Fable 5.1",
    "claude-fable-5.1": "Claude Fable 5.1",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-opus-5.5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-sonnet-5.5": "Claude Sonnet 5.5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-opus-4.8": "Claude Opus 4.8",
    "openai/gpt-5.5": "GPT-5.5",
    "z-ai/glm-5.1": "GLM-5.1",
}

# Trailing -YYYYMMDD snapshot date on a provider model id.
_DATE_SUFFIX_RE = re.compile(r"-20\d{6}$")

# Words rendered in ALL CAPS in model names (abbreviations and well-known initialisms).
_ABBREV_WORDS: frozenset[str] = frozenset({"gpt", "llm", "ai", "api", "url", "id", "ui", "ml", "glm"})

def _stdout_supports_unicode() -> bool:
    try:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        "━—✖⚠✓".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE_OK: bool = _stdout_supports_unicode()
_SEP: str = ("━" if _UNICODE_OK else "-") * 40
_EM: str = "—" if _UNICODE_OK else "-"
_ARROW: str = "→" if _UNICODE_OK else "->"
_INDENT: str = "  "
_COL: int = 12

_SEVERITY_ORDER: list[str] = ["Critical", "High", "Medium", "Low", "Note"]

_FINDING_ICONS: dict[str, str] = {
    "Critical": "✖" if _UNICODE_OK else "X",
    "High": "⚠" if _UNICODE_OK else "!",
    "Medium": "~",
    "Low": "✓" if _UNICODE_OK else "+",
    "Note": "-",
}


@dataclass
class ShardFinding:
    severity: str
    message: str
    path: str | None = None
    line: int | None = None


@dataclass
class FileEvidence:
    path: str
    roles: list[str]  # ordered subset of: inspected, finding_source, changed


@dataclass
class ExecutionSpan:
    """OTel-ready/OTel-inspired span shape. No tracing dependency."""
    span_id: str
    name: str
    kind: str
    started_at: str | None = None
    duration_ms: int | None = None
    status: str | None = None
    error_class: str | None = None
    summary: str | None = None


@dataclass
class EvidenceCapsule:
    """Structured evidence unit. No raw content stored."""
    capsule_id: str
    kind: str
    summary: str
    source: str | None = None
    path: str | None = None
    line: int | None = None
    severity: str | None = None


_ROLE_ORDER = ["inspected", "finding_source", "changed"]

_ROLE_LABELS: dict[str, str] = {
    "inspected": "inspected/read context",
    "finding_source": "finding source",
    "changed": "changed",
}

# Directories that are always noisy regardless of depth (e.g. src/__pycache__/x.pyc).
_NOISY_EVIDENCE_ANY_SEGMENT: frozenset[str] = frozenset({
    "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules",
    ".mypy_cache", ".ruff_cache", ".tox", ".next", ".git",
})

# Directories that are only noisy when they are the top-level path component.
# Checking any segment for these would risk over-filtering real source paths.
_NOISY_EVIDENCE_ROOT_SEGMENT: frozenset[str] = frozenset({
    "dist", "build", "coverage", "cache", ".cache", "tmp", "temp",
})


def _is_noisy_evidence_path(path: str) -> bool:
    """Return True if *path* should be excluded from user-facing inspected evidence."""
    parts = path.replace("\\", "/").split("/")
    if not parts:
        return False
    if any(part in _NOISY_EVIDENCE_ANY_SEGMENT for part in parts):
        return True
    return parts[0] in _NOISY_EVIDENCE_ROOT_SEGMENT


def _build_file_evidence(
    inspected: list[str],
    referenced: list[str],
    touched: list[str],
) -> list[FileEvidence]:
    acc: dict[str, set[str]] = {}
    for p in inspected:
        if not _is_noisy_evidence_path(p):
            acc.setdefault(p, set()).add("inspected")
    for p in referenced:
        acc.setdefault(p, set()).add("finding_source")
    for p in touched:
        acc.setdefault(p, set()).add("changed")
    result = [
        FileEvidence(path=p, roles=[r for r in _ROLE_ORDER if r in roles])
        for p, roles in acc.items()
    ]
    return sorted(result, key=lambda e: e.path)


def _safe_str_list(val: object) -> list[str]:
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, str)]


def _coerce_finding_list(val: object, default_severity: str = "Note") -> list[ShardFinding]:
    if not isinstance(val, list):
        return []
    out: list[ShardFinding] = []
    for item in val:
        if isinstance(item, dict) and "message" in item:
            sev = item.get("severity") or default_severity
            if sev not in _SEVERITY_ORDER:
                sev = "Note"
            line_val = item.get("line")
            out.append(ShardFinding(
                severity=sev,
                message=str(item["message"]),
                path=item.get("path") or None,
                line=int(line_val) if line_val is not None else None,
            ))
        elif isinstance(item, str):
            out.append(ShardFinding(severity=default_severity, message=item))
    return out


_METADATA_NOISE_PHRASES: tuple[str, ...] = (
    "has no tags block",
    "has no labels block",
    "missing required tags",
    "missing required labels",
    "add owner and environment",
)

_DEFAULT_FINDING_CAPS: dict[str, int] = {"Critical": 3, "High": 3, "Medium": 2, "Low": 1}


def _is_metadata_noise(f: ShardFinding) -> bool:
    msg = f.message.lower()
    return any(p in msg for p in _METADATA_NOISE_PHRASES)


def group_review_findings(
    findings: list[ShardFinding],
    *,
    caps: dict[str, int] | None = None,
) -> tuple[list[ShardFinding], ShardFinding | None, int, int]:
    """Group and rank findings for compact display.

    Returns (visible_substantive, grouped_metadata_or_None, hidden_substantive_count, raw_total).

    visible_substantive: deduplicated non-metadata findings sorted Critical→Low,
        each severity capped by *caps* (defaults to Critical=3, High=3, Medium=2, Low=1).
    grouped_metadata_or_None: a single synthetic ShardFinding (severity=Medium) that
        summarises all metadata-noise findings, or None if none found.
    hidden_substantive_count: substantive findings cut by the cap.
    raw_total: len(findings) before any grouping.
    """
    effective_caps = dict(_DEFAULT_FINDING_CAPS)
    if caps:
        effective_caps.update(caps)

    raw_total = len(findings)

    # Deduplicate by (severity, message)
    seen: set[tuple[str, str]] = set()
    deduped: list[ShardFinding] = []
    for f in findings:
        key = (f.severity, f.message)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    substantive = [f for f in deduped if not _is_metadata_noise(f)]
    metadata    = [f for f in deduped if _is_metadata_noise(f)]

    # Sort substantive by severity order
    substantive.sort(
        key=lambda f: _SEVERITY_ORDER.index(f.severity) if f.severity in _SEVERITY_ORDER else len(_SEVERITY_ORDER),
    )

    # Apply per-severity caps
    visible: list[ShardFinding] = []
    hidden_count = 0
    by_sev: dict[str, list[ShardFinding]] = {}
    for f in substantive:
        by_sev.setdefault(f.severity, []).append(f)
    for sev in _SEVERITY_ORDER:
        group = by_sev.get(sev, [])
        cap = effective_caps.get(sev, 1)
        visible.extend(group[:cap])
        hidden_count += max(0, len(group) - cap)

    # Build grouped metadata finding
    meta_group: ShardFinding | None = None
    if metadata:
        # Detect term (labels vs tags) from messages
        uses_labels = any("label" in f.message.lower() for f in metadata)
        has_gcp     = any("google_" in f.message for f in metadata)
        term   = "labels" if uses_labels else "tags"
        prefix = "GCP " if has_gcp else ""

        # Extract up to 3 resource names from messages (pattern: "Resource TYPE.NAME ...")
        examples: list[str] = []
        seen_ex: set[str] = set()
        for f in metadata:
            parts = f.message.split()
            if len(parts) >= 2 and parts[0].lower() == "resource":
                name = parts[1]
                if name not in seen_ex:
                    seen_ex.add(name)
                    examples.append(name)
                    if len(examples) >= 3:
                        break
        ex_str = ", ".join(examples)
        n = len(metadata)
        msg = (
            f"{n} {prefix}resources are missing ownership/environment {term}, "
            "making cost tracking and incident response harder."
        )
        if ex_str:
            msg += f"\n  Examples: {ex_str}"
        meta_group = ShardFinding(severity="Medium", message=msg)

    return visible, meta_group, hidden_count, raw_total


def _extract_findings(entry: dict) -> list[ShardFinding]:
    findings: list[ShardFinding] = []

    findings.extend(_coerce_finding_list(entry.get("findings")))

    for note in _safe_str_list(entry.get("agent_notes")):
        findings.append(ShardFinding(severity="Note", message=note))

    fr = entry.get("final_report") or {}
    findings.extend(_coerce_finding_list(fr.get("findings")))

    for w in _safe_str_list(fr.get("warnings")):
        findings.append(ShardFinding(severity="Note", message=w))

    dr = entry.get("diff_review") or {}
    for w in _safe_str_list(dr.get("warnings")):
        findings.append(ShardFinding(severity="Note", message=w))

    pl = entry.get("plan") or {}
    for w in _safe_str_list(pl.get("warnings")):
        findings.append(ShardFinding(severity="Note", message=w))

    obs = entry.get("observation") or {}
    for w in _safe_str_list(obs.get("warnings")):
        findings.append(ShardFinding(severity="Note", message=w))

    findings.sort(key=lambda f: _SEVERITY_ORDER.index(f.severity) if f.severity in _SEVERITY_ORDER else 99)
    return findings


def _display_model_name(slug: str) -> str:
    """Convert a provider/model slug to a user-friendly display name.

    Checks an explicit lookup table first; falls back to a best-effort formatter.
    Keeps raw slugs in stored history — only used in rendered receipt output.
    """
    if not slug:
        return slug
    key = slug.lower().strip()
    if key in _MODEL_FRIENDLY_NAMES:
        return _MODEL_FRIENDLY_NAMES[key]
    # Try without provider prefix
    name_key = key.split("/", 1)[-1]
    if name_key in _MODEL_FRIENDLY_NAMES:
        return _MODEL_FRIENDLY_NAMES[name_key]
    # Dated snapshot ids (claude-haiku-4-5-20251001) name the same model.
    undated = _DATE_SUFFIX_RE.sub("", name_key)
    if undated != name_key and undated in _MODEL_FRIENDLY_NAMES:
        return _MODEL_FRIENDLY_NAMES[undated]
    # Fall back to centralized registry for models not in the local table.
    from openshard.models.registry import display_name_for as _reg_display
    reg_name = _reg_display(slug)
    if reg_name != slug:
        return reg_name
    return _format_model_slug_shard(slug.split("/", 1)[-1])


def display_model_name(slug: str) -> str:
    """Public wrapper — convert a provider/model slug to a user-friendly display name."""
    return _display_model_name(slug)


def _format_model_slug_shard(name: str) -> str:
    """Best-effort formatter for unknown model slugs (e.g. 'gemini-2.0-flash' → 'Gemini 2.0 Flash')."""
    parts = [p for p in name.split("-") if p]
    tagged: list[tuple[str, str]] = []
    for part in parts:
        lower = part.lower()
        if lower in _ABBREV_WORDS:
            tagged.append(("abbrev", part.upper()))
        elif re.match(r"^v\d", lower):
            tagged.append(("version", part[0].upper() + part[1:]))
        elif re.match(r"^\d+[a-z]+$", lower):
            tagged.append(("version", re.sub(r"[a-z]+$", lambda m: m.group().upper(), part)))
        elif part[0].isdigit():
            tagged.append(("version", part))
        else:
            tagged.append(("word", "DeepSeek" if lower == "deepseek" else part.capitalize()))
    def _digit(j: int) -> bool:
        return tagged[j][0] == "version" and len(tagged[j][1]) == 1 and tagged[j][1].isdigit()

    out = ""
    joined = False
    for i, (kind, text) in enumerate(tagged):
        if i == 0:
            out = text
        elif _digit(i) and _digit(i - 1) and not joined:
            # Two single-digit parts are one version: "claude-opus-5-5" ->
            # "Claude Opus 5.5". Dated parts ("2024-08-06") never match.
            out += "." + text
            joined = True
            continue
        elif kind == "version" and tagged[i - 1][0] == "abbrev":
            out += "-" + text
        else:
            out += " " + text
        joined = False
    return out


@dataclass
class ShardReceipt:
    shard_id: str
    created_at: str
    task_short: str
    task_full: str
    agent: str
    strategy: str
    model_display: str
    risk: str
    sandbox: str
    files_changed: int
    checks_display: str
    approval: str
    cost_display: str
    result: str
    status: str
    duration_seconds: float | None
    # Concise display title (history/task_title.py) -- display metadata only;
    # task_short/task_full keep the recorded task text unchanged.
    task_title: str = ""
    human_summary: str | None = None
    owner: str | None = None
    repo: str | None = None
    # Canonical ``host/owner/repo`` from the record's additive ``repo_identity``
    # field (history/repo_identity.py); None for records without one. ``repo``
    # stays the folder name.
    repo_identity: str | None = None
    branch: str | None = None
    git_state: str | None = None
    context_quality: str | None = None
    files_read_count: int | None = None
    inspected_files: list[str] = field(default_factory=list)
    files_referenced: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    files_detail: list[dict] = field(default_factory=list)
    # OSN iterative runs: every declared action with the harness's decision and
    # observed effect (``osn_loop.attempts[*].actions``), flattened in order.
    osn_actions: list[dict] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    blocked_commands: list[str] = field(default_factory=list)
    check_results: list[str] = field(default_factory=list)
    diff_added: int | None = None
    diff_removed: int | None = None
    lines_not_recorded_reason: str | None = None
    cost_raw: float | None = None
    # Each tuple is (friendly_stage_label, friendly_model_name).
    model_stages: list[tuple[str, str]] = field(default_factory=list)
    findings: list[ShardFinding] = field(default_factory=list)
    agent_notes: list[str] = field(default_factory=list)
    run_timeline: list = field(default_factory=list)
    developer_feedback: dict | None = None
    approval_required: bool = False
    approval_granted: bool | None = None
    approval_reason: str = ""
    file_evidence: list[FileEvidence] = field(default_factory=list)
    model_advisory: list[dict] = field(default_factory=list)
    feedback_routing_advisory: dict | None = None
    # Schema versioning — None for old entries; "1.1" for entries written by this version
    schema_version: str | None = None
    schema_notes: list[str] = field(default_factory=list)
    # Git attribution — supplements existing branch/git_state/repo fields
    git_base_branch: str | None = None
    git_base_commit_hash: str | None = None
    git_head_commit_hash: str | None = None
    git_dirty: bool | None = None
    # Error classification — normalised class; no raw command output
    error_class: str | None = None
    error_message: str | None = None
    # Context utilisation summary — intentionally None until future branches populate
    context_files_considered_count: int | None = None
    context_files_injected_count: int | None = None
    context_utilisation_ratio: float | None = None
    # OTel-ready execution spans — empty until future branches populate
    execution_spans: list[ExecutionSpan] = field(default_factory=list)
    # Evidence capsules — structured, no raw content
    evidence_capsules: list[EvidenceCapsule] = field(default_factory=list)
    # Provenance records — derived at read-time from evidence_capsules and review_checks; not persisted
    provenance: list = field(default_factory=list)
    # Canonical Events — derived at read-time via Migration 3's conversion seam
    # (events_from_entry); not persisted. Empty list for legacy entries or when
    # no canonical Events can be derived.
    events: list = field(default_factory=list)
    # Policy decisions — structured gate/policy outcomes; empty until populated branches
    policy_decisions: list[dict] = field(default_factory=list)
    # Adapter execution metadata — optional; only set for explicit external adapter runs
    adapter: str | None = None
    adapter_available: bool | None = None
    adapter_command: list[str] = field(default_factory=list)
    adapter_exit_code: int | None = None
    adapter_stdout_summary: str | None = None
    adapter_stderr_summary: str | None = None
    adapter_duration_ms: int | None = None
    # Safe workspace identity — set when a sandbox was used for this run
    safe_workspace_kind: str | None = None
    safe_workspace_display_name: str | None = None
    # Canonical verification signal, populated from the OSN verification contract
    # when present. Empty token means "fall back to the boolean/status logic".
    # No raw output; returncode and duration are bounded scalars.
    verification_status: str = ""
    verification_reason: str = ""
    verification_returncode: int | None = None
    verification_duration_seconds: float | None = None
    verification_raw_output_stored: bool = False
    # Structured verification evidence (history/verification.py, block v1):
    # status, source, observation mode, check counts, artifact SHA and
    # completeness. Always populated by build_shard_receipt -- from the
    # record's stored ``verification`` block, or derived (``derived: True``)
    # from an older record's fields. None only for hand-built receipts.
    verification: dict | None = None
    # Canonical Shard identity — durable task + honest origin/capture-depth.
    # See openshard/history/shard.py. None only for receipts built without
    # going through build_shard_receipt (e.g. some hand-built test fixtures).
    shard: Shard | None = None
    # Run/Attempt identity (Migration 2). run_id is populated from the entry's
    # own run_id/timestamp and is never None once built via build_shard_receipt.
    # attempt_number is only set for entries written with explicit attempt
    # tracking; None for legacy/Claude-import entries that predate it — never
    # fabricated.
    run_id: str | None = None
    attempt_number: int | None = None
    # Turn-completion status -- independent of whether the underlying agent
    # session itself has ended (see openshard.adapters.claude_hooks). None
    # for entries that don't carry this signal (most executors); never
    # conflated with verification ("Completed" is not "verified").
    task_completion: str | None = None
    # Token usage -- only ever populated from a trustworthy provider/agent
    # source (see build_shard_receipt); None means genuinely not known, never 0.
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_creation: int | None = None
    tokens_cache_read: int | None = None
    tokens_provenance: str | None = None
    # Cost provenance -- distinguishes a provider-reported figure from an
    # OpenShard-calculated or OpenShard-estimated one (see cost_display).
    cost_provenance: str | None = None
    # v0.4.4 global Receipt identity (history/receipt_identity.py). Read
    # from the record only -- None for records written before 0.4.4, never
    # minted at display time. ``shard_id`` above keeps its historic,
    # history-position meaning.
    receipt_id: str | None = None
    # task_id (history/task_identity.py): one explicitly declared engineering
    # task across attempts, agents and potentially repositories. Read from
    # the record only -- never minted, inferred or backfilled at build/
    # display time. Additive alongside shard_id/receipt_id; None for records
    # that never had one attached.
    task_id: str | None = None
    # v0.4.4 capture completeness (history/capture_completeness.py):
    # {"depth": full|partial|unknown, "status": complete|incomplete|unknown,
    #  "reasons": [...], "derived": bool}. ``depth`` is how much could be
    # observed (also on ``shard.capture_depth``); ``status`` whether evidence
    # is known lost. Always populated by build_shard_receipt; None only for
    # hand-built receipts.
    capture_completeness: dict | None = None
    # v0.4.4 integrity: "Matches (content hash)" | "Mismatch (content hash)" |
    # "Not recorded" from history/shard_hash.verify_shard_hash. An unkeyed
    # content hash is tamper-evidence for the stored record only; it never
    # proves who wrote it, and the wording never says "signed".
    integrity: str = "Not recorded"
    # The checksum state as a token ("valid" | "mismatch" | "missing") so
    # consumers never parse the display string above.
    integrity_status: str = "missing"
    # Verification v2: the latest ``openshard verify`` attestation for this
    # Receipt (``verification/post_session.summarize_attestation``), joined
    # by the caller at read time. None when never re-verified. The stored
    # record is never modified; ``history/verification_truth`` gives this
    # precedence over the session's own block when it carries an outcome.
    post_session_verification: dict | None = None
    # v0.4.4 change provenance (adapters/claude_hooks._classify_changed_files):
    # counts per attribution plus the session-start baseline summary. None for
    # records written before attribution existed. ``files_detail`` holds the
    # changes counted as this run's; ``files_excluded`` the pre-existing /
    # other-session changes git also showed, kept for provenance only.
    changes: dict | None = None
    files_excluded: list[dict] = field(default_factory=list)
    # Bounded control/proof/cost evidence already stored on the record
    # (history/receipt_evidence.py): approval_detail, sandbox_detail,
    # execution_loop, base_commit, content_hash, session, routing, retry and
    # model_stage_metrics. Read-only projections; None for hand-built receipts.
    recorded_evidence: dict | None = None
    # Usage and cost evidence (history/usage_evidence.py): the record's own
    # usage, strengthened by usage attestations that name this Receipt
    # (``.openshard/usage.jsonl``), joined by the caller at read time. The
    # flat token/cost fields above stay exactly what the record says.
    usage: dict | None = None


def _verification_from_osn_contract(
    entry: dict,
) -> tuple[str, str, int | None, float | None, bool]:
    """Map a persisted OSN verification contract to canonical receipt fields.

    Returns (status_token, reason, returncode, duration_seconds, raw_output_stored).
    The status token is one of passed, failed, skipped, manual_review, not_run,
    unknown, or empty when no OSN contract is present (callers then fall back to
    the boolean and string logic for old or non-native records). manual_review
    and impossible are mapped deliberately and never collapsed into unknown.
    """
    osn = entry.get("osn_verification_contract")
    if not isinstance(osn, dict) or not osn.get("enabled"):
        return "", "", None, None, False

    raw_status = str(osn.get("status") or "").strip()
    manual_review = bool(osn.get("manual_review_required"))

    if raw_status == "failed":
        token = "failed"
    elif raw_status == "passed":
        token = "passed"
    elif manual_review:
        # A run that still needs human intervention, regardless of whether the
        # underlying status was skipped, impossible, manual_review, or unknown.
        token = "manual_review"
    elif raw_status in ("skipped", "impossible"):
        # impossible means the check could not run; treat as skipped with reason.
        token = "skipped"
    elif raw_status == "manual_review":
        token = "manual_review"
    elif raw_status == "not_run":
        token = "not_run"
    else:
        token = "unknown"

    reason = (str(osn.get("skipped_reason") or "") or str(osn.get("summary") or ""))[:200]
    rc = osn.get("returncode")
    returncode = rc if isinstance(rc, int) else None
    dur = osn.get("duration_seconds")
    duration = float(dur) if isinstance(dur, (int, float)) else None
    raw_stored = bool(osn.get("raw_output_stored"))
    return token, reason, returncode, duration, raw_stored


_WEAK_VERIFICATION_STATUSES: frozenset[str] = frozenset(
    {"Not recorded", "No checks run", "Checks attempted, result not verified"}
)


def _verification_display(ev: VerificationEvidence) -> tuple[str, str]:
    """(checks_display, status) for structured evidence, in the receipt's existing vocabulary.

    The string is part of the synced receipt projection, so it stays
    source-free; renderers add the source label (``checks_label``).
    """
    attempted = ev.checks_attempted
    if ev.status == STATUS_PASSED:
        return (f"{ev.checks_passed}/{attempted} passed" if attempted else "Passed"), "Passed"
    if ev.status == STATUS_FAILED:
        return (f"{ev.checks_passed or 0}/{attempted} passed" if attempted else "Failed"), "Failed"
    if ev.status == STATUS_PARTIAL:
        return (f"{ev.checks_passed or 0}/{attempted} passed, rest unverified" if attempted else "Partial"), "Partial"
    if ev.status == STATUS_NOT_RUN:
        return "Not run", "No checks run"
    if attempted or ev.checks or REASON_OUTCOME_NOT_OBSERVED in ev.incomplete_reasons:
        return "Attempted (unverified)", "Checks attempted, result not verified"
    return "Not recorded", "Not recorded"


def checks_label(receipt: ShardReceipt) -> str:
    """``checks_display`` for rendering, with ``(agent-reported)`` when the agent -- not
    OpenShard -- supplied the outcome, so a reported pass never reads like an observed one.

    This is the *session's* record. The current evidence state, including a
    later ``openshard verify`` re-run, is the ``Verified`` row (``verified_label``).
    """
    block = receipt.verification if isinstance(receipt.verification, dict) else {}
    display = receipt.checks_display
    if block.get("status") in (STATUS_FAILED, STATUS_PARTIAL) and not receipt.check_results:
        # "19/50 passed" does not say whether the other 31 failed or were
        # never seen to finish; name each group. The stored/synced
        # ``checks_display`` string itself is unchanged.
        display = counts_phrase(
            block.get("checks_passed"), block.get("checks_failed"), block.get("checks_attempted"),
        ) or display
    return (replaced_failures_phrase(block) or display) + agent_reported_suffix(receipt)


def agent_reported_suffix(receipt: ShardReceipt) -> str:
    """`` (agent-reported)`` when the session's check outcome is the agent's own claim, else ``""``.

    ``status`` / ``checks_display`` stay source-free because they are synced;
    every surface that prints them on their own appends this.
    """
    block = receipt.verification if isinstance(receipt.verification, dict) else {}
    if block.get("source") == SOURCE_AGENT_REPORTED and block.get("status") in (
        STATUS_PASSED, STATUS_FAILED, STATUS_PARTIAL,
    ):
        return " (agent-reported)"
    return ""


def permission_mode_label(receipt: ShardReceipt) -> str | None:
    """The permission regime an external agent reported running under, or None.

    ``acceptEdits (Claude Code's own permission mode, agent-reported; not
    enforced by OpenShard)``; a session that changed mode reads ``default →
    acceptEdits``. Only from ``runtime_configuration`` evidence a Claude hook
    carried; OSN's own, enforced permission evidence is a different row.
    """
    runtime = (receipt.recorded_evidence or {}).get("runtime_configuration")
    if not isinstance(runtime, dict):
        return None
    mode = runtime.get("permission_mode")
    if not isinstance(mode, str) or not mode:
        return None
    seen = [m for m in (runtime.get("permission_modes_seen") or []) if isinstance(m, str) and m]
    path = f" {_ARROW} ".join(seen) if len(seen) > 1 else mode
    return f"{path} (Claude Code's own permission mode, agent-reported; not enforced by OpenShard)"


def verified_label(receipt: ShardReceipt) -> str:
    """The one sentence every surface uses for "was this verified, and by whom?"."""
    return verification_label(interpret_receipt(receipt), replaced=replaced_failures_phrase(receipt.verification))


def _make_shard_id(timestamp: str, index: int | None) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y%m%d")
    except (ValueError, AttributeError, TypeError):
        date_str = datetime.now(UTC).strftime("%Y%m%d")
    n = (index + 1) if index is not None else 1
    return f"shard-{date_str}-{n:04d}"


def _trunc(s: str, n: int) -> str:
    if not s or len(s) <= n:
        return s or ""
    return s[: n - 1] + ("…" if _UNICODE_OK else ".")


_MAX_RESULT: int = 60

# Words that signal a dangling clause when a sentence is clipped at them.
_RE_TRAILING_CONNECTIVE = re.compile(
    r"\s+(and|or|with|for|but|as|of|to|in|a|an|the|covering|including|"
    r"such|which|that|when|where|while|by|on|at|from|its|their|this|these|"
    r"its|both|also|well|via|across|using|within)\s*$",
    re.IGNORECASE,
)


def _result_display(summary: str) -> str:
    """Return a short, complete result line from a full run summary.

    Prefers the first complete sentence when one is found within _MAX_RESULT.
    Falls back to a clean word-boundary clip. Never appends an ellipsis or
    leaves a dangling clause.
    """
    if not summary:
        return "Not recorded"
    line = summary.split("\n")[0].strip()
    if not line:
        return "Not recorded"
    # Always try first complete sentence before considering full-line length.
    for sep in (". ", "; ", "! ", "? "):
        idx = line.find(sep, 0, _MAX_RESULT)
        if idx != -1:
            candidate = line[:idx + 1].strip()
            if len(candidate) >= 4:
                return candidate
    # No internal sentence boundary — use full line when it fits.
    if len(line) <= _MAX_RESULT:
        return line
    # Long line: clip at a word boundary, strip trailing connectives, add ellipsis.
    clipped = line[:_MAX_RESULT]
    sp = clipped.rfind(" ")
    if sp > _MAX_RESULT // 3:
        clipped = clipped[:sp]
    clipped = clipped.rstrip(" ,;:")
    clipped = _RE_TRAILING_CONNECTIVE.sub("", clipped).rstrip(" ,;:")
    return clipped or line[:_MAX_RESULT]


def _stored_repo_identity(entry: dict) -> str | None:
    """The canonical ``repo_identity`` written on *entry*, or None. Never derives one."""
    from openshard.history.repo_identity import REPO_IDENTITY_FIELD

    value = entry.get(REPO_IDENTITY_FIELD)
    return value if isinstance(value, str) and value else None


def _workspace_folder_name(raw: object) -> str | None:
    if not raw:
        return None
    value = str(raw).rstrip("\\/")
    if not value:
        return None
    if "\\" in value:
        return PureWindowsPath(value).name or None
    return Path(value).name or None


_EXCLUDED_ATTRIBUTIONS = frozenset({"pre_existing", "other_session"})
_ATTRIBUTION_TAGS: dict[str, str] = {
    "agent_reported": "agent-reported",
    "git_observed": "git-observed",
    "pre_existing": "pre-existing",
    "other_session": "other session",
}


def _changes_summary(block: dict | None) -> dict | None:
    """The bounded, path-free ``changes`` block for a receipt (None for old records)."""
    if not isinstance(block, dict):
        return None

    def _n(key: str) -> int:
        value = block.get(key)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    _baseline_raw = block.get("baseline")
    baseline: dict = _baseline_raw if isinstance(_baseline_raw, dict) else {}
    summary: dict = {
        "agent_reported": _n("agent_reported"),
        "git_observed": _n("git_observed"),
        "pre_existing_excluded": _n("pre_existing_excluded"),
        "other_session_excluded": _n("other_session_excluded"),
        "files_truncated": bool(block.get("files_truncated")),
        "baseline": {
            "source": str(baseline.get("source") or "not_available"),
            "at": baseline.get("at") if isinstance(baseline.get("at"), str) else None,
            "dirty_paths": int(baseline.get("dirty_paths") or 0),
            "truncated": bool(baseline.get("truncated")),
        },
    }
    if block.get("files_observable") is False:
        # Only integrations that cannot see file changes at all (Grok Bot's
        # Action Recording export) store this; absent everywhere else, so
        # existing receipts -- and their sync payload hashes -- are unchanged.
        summary["files_observable"] = False
    return summary


def integrity_status(entry: dict) -> str:
    """``valid`` / ``mismatch`` / ``missing``. Never raises.

    The verdict is over the record as stored: a coerced record carries the
    loader's verdict (``shard_hash.integrity_as_stored``), which wins over a
    recompute over the coerced content. A record read as stored, or built in
    memory, is verified directly (``shard_hash.verify_shard_hash``).
    """
    try:
        as_stored = integrity_as_stored(entry)
        if as_stored is not None:
            return as_stored
        status = verify_shard_hash(entry).get("status")
    except Exception:
        return "missing"
    return status if status in ("valid", "mismatch") else "missing"


def integrity_display(entry: dict) -> str:
    """``Checksum matches`` / ``Checksum mismatch (...)`` / ``Not recorded``.

    Technically precise on purpose: the hash is an unkeyed SHA-256 over the
    stored record (``shard_hash``). A match means the record's content is
    what it was when the hash was written; it says nothing about authorship
    (see ``verification_truth.integrity_label``).
    """
    return integrity_label(integrity_status(entry))


def file_changes_unobservable(receipt: ShardReceipt) -> bool:
    """True when the capture path cannot see file changes and none were recorded.

    A count of 0 on such a Receipt means "unknown", never "no files changed".
    """
    changes = receipt.changes
    return (
        receipt.files_changed == 0
        and not receipt.files_detail
        and isinstance(changes, dict)
        and changes.get("files_observable") is False
    )


def changed_files_display(receipt: ShardReceipt) -> str:
    """``2 files (1 agent-reported, 1 git-observed)`` when provenance is known, else ``2 files``."""
    n = receipt.files_changed
    changes = receipt.changes
    if file_changes_unobservable(receipt):
        return "Not observable (this integration exports no file changes)"
    text = f"{n} file{'s' if n != 1 else ''}"
    if changes and n > 0:
        parts = []
        if changes.get("agent_reported"):
            parts.append(f"{changes['agent_reported']} agent-reported")
        if changes.get("git_observed"):
            parts.append(f"{changes['git_observed']} git-observed, actor not established")
        if parts:
            text += f" ({'; '.join(parts)})"
    return text


def excluded_changes_rows(receipt: ShardReceipt) -> list[str]:
    """Rows for changes git showed that are *not* counted as this run's work."""
    rows: list[str] = []
    changes = receipt.changes
    if not changes:
        return rows
    pre = changes.get("pre_existing_excluded") or 0
    other = changes.get("other_session_excluded") or 0
    # Label fits the receipt's 12-column label gutter; the value names the kind.
    if pre:
        rows.append(_row("Excluded", f"{pre} pre-existing (changed before the {'turn' if ((receipt.recorded_evidence or {}).get('session') or {}).get('receipt_scope') == 'turn' else 'session'})"))
    if other:
        rows.append(_row("Excluded", f"{other} other-session (reported by another agent session)"))
    return rows


def _file_line(fd: dict) -> str | None:
    path = fd.get("path")
    if not isinstance(path, str) or not path:
        return None
    change_type = fd.get("change_type")
    letter = _FILE_CHANGE_LETTERS.get(change_type, "M") if isinstance(change_type, str) else "M"
    attribution = fd.get("attribution")
    tag = _ATTRIBUTION_TAGS.get(attribution) if isinstance(attribution, str) else None
    return f"{_INDENT}  {letter} {path}" + (f"  {_EM} {tag}" if tag else "")


def build_shard_receipt(
    entry: dict, index: int | None = None, *, post_session_verification: dict | None = None,
    usage_attestations: list[dict] | None = None,
) -> ShardReceipt:
    """Convert a raw run-history entry dict into a ShardReceipt. Never raises.

    *post_session_verification* is the latest ``openshard verify`` attestation
    summary for this record (resolved by the caller from
    ``.openshard/verifications.jsonl``); it is carried on the receipt so
    every consumer interprets the same evidence (``verification_truth``).
    *usage_attestations* are the usage attestations naming this record
    (``.openshard/usage.jsonl``); they only ever feed ``usage``.
    """
    stored_entry = entry
    entry = control_evidence_view(entry)
    timestamp = entry.get("timestamp") or ""
    task = entry.get("task") or ""

    agent, _, _ = derive_shard_identity(entry)

    profile = entry.get("execution_profile")
    strategy = _PROFILE_TO_STRATEGY.get(profile) if profile else None
    strategy = strategy or "Not recorded"

    routing_model = entry.get("routing_selected_model")
    exec_model = entry.get("execution_model")
    _sr_quick = entry.get("stage_runs") or []
    _first_stage_model = next(
        (_display_model_name(s["model"]) for s in _sr_quick if isinstance(s, dict) and s.get("model")),
        None,
    )
    if routing_model:
        model_display = f"Auto → {_display_model_name(routing_model)}"
    elif exec_model:
        model_display = _display_model_name(exec_model)
    elif _first_stage_model:
        model_display = _first_stage_model
    else:
        model_display = "Not recorded"

    form_factor = entry.get("form_factor") or {}
    plan = entry.get("plan") or {}
    risk_raw = form_factor.get("risk_level") or plan.get("risk")
    risk = _RISK_LABELS.get(str(risk_raw).lower(), str(risk_raw).capitalize()) if risk_raw else "Not recorded"
    # v0.4.4: the receipt shows the risk that was *recorded*. The former
    # display-time rule that raised a review task's missing/Low risk to High
    # silently turned one fact into another; a Receipt never does that.

    write_path = entry.get("write_path")
    ff_read_only = form_factor.get("read_only")
    if write_path == "sandbox":
        sandbox = "On"
    elif write_path == "pipeline":
        sandbox = "Off"
    elif ff_read_only or entry.get("is_review_task"):
        sandbox = "Off"
    else:
        sandbox = "Not recorded"

    fc = entry.get("files_created") or 0
    fu = entry.get("files_updated") or 0
    fd = entry.get("files_deleted") or 0
    files_changed = fc + fu + fd
    # Files whose type could not be established are listed with the neutral
    # "changed" type and are in none of the three counts above.
    _detail = entry.get("files_detail")
    _detail = _detail if isinstance(_detail, list) else []
    files_changed += sum(
        1 for f in _detail if isinstance(f, dict) and f.get("path") and f.get("change_type") == "changed"
    )
    if files_changed == 0:
        fr = entry.get("final_report") or {}
        diff = entry.get("diff_review") or {}
        diff_files = fr.get("diff_files") or diff.get("changed_files") or []
        if diff_files:
            files_changed = len(diff_files)
        else:
            # Records that list files but wrote zero counts (older OSN runs) still changed the
            # files they list -- except ones listed as not this run's (pre-existing, other session).
            files_changed = sum(1 for f in _detail if isinstance(f, dict) and f.get("path") and f.get("attribution") not in _EXCLUDED_ATTRIBUTIONS)

    v_attempted = entry.get("verification_attempted")
    v_passed = entry.get("verification_passed")
    if v_attempted is None:
        fr = entry.get("final_report") or {}
        v_attempted = fr.get("verification_attempted")
        if v_passed is None:
            v_passed = fr.get("verification_passed")

    if v_attempted is None:
        checks_display = "Not recorded"
        status = "Not recorded"
    elif not v_attempted:
        checks_display = "Not run"
        status = "No checks run"
    elif v_passed is True:
        checks_display = "1/1 passed"
        status = "Passed"
    elif v_passed is False:
        checks_display = "0/1 passed"
        status = "Failed"
    else:
        # Attempted (a check-shaped command was directly observed running)
        # but no pass/fail outcome was ever recorded -- e.g. an externally
        # observed Claude Code/Codex/OpenCode session, where OpenShard never
        # reads the command's stdout/exit code. Distinct from "Not run":
        # something did happen, its result just wasn't verified.
        checks_display = "Attempted (unverified)"
        status = "Checks attempted, result not verified"

    (
        _v_status,
        _v_reason,
        _v_returncode,
        _v_duration,
        _v_raw_stored,
    ) = _verification_from_osn_contract(entry)

    # Structured verification evidence. A stored ``verification`` block is
    # authoritative; otherwise an OSN contract keeps its own token (manual_review
    # / skipped stay distinct), and every other record gets the token derived
    # from its fields -- so a hook/import/wrap Receipt no longer crosses the
    # history --json / sync boundary with verification_status = null when a
    # check was observed (or when the capture path cannot see checks at all).
    _vev = derive_verification(entry)
    if not _v_status or not _vev.derived:
        _v_status = status_token(_vev)
        _v_reason = summary_reason(_vev)
        if _vev.exit_code is not None:
            _v_returncode = _vev.exit_code
        if _vev.duration_seconds is not None:
            _v_duration = _vev.duration_seconds
    if _vev.recorded and status in _WEAK_VERIFICATION_STATUSES:
        # The legacy booleans said less than (or contradicted) the evidence:
        # e.g. import/wrap stored verification_attempted=False, which read as
        # "No checks run" although those paths cannot observe checks.
        checks_display, status = _verification_display(_vev)

    check_results: list[str] = []
    _review_checks_raw = entry.get("review_checks")
    if _review_checks_raw and isinstance(_review_checks_raw, list):
        checks_display, check_results = _format_review_checks(_review_checks_raw)
        status = f"Checks: {checks_display}"

    approval_receipt_raw = entry.get("approval_receipt") or {}
    if approval_receipt_raw:
        if approval_receipt_raw.get("granted"):
            approval = "Required → Granted"
        else:
            approval = "Required → Denied"
    elif ff_read_only or write_path in ("pipeline", "sandbox"):
        approval = "Not required"
    else:
        approval = "Not recorded"
    _approval_required = bool(approval_receipt_raw)
    _approval_granted: bool | None = (
        approval_receipt_raw.get("granted") if approval_receipt_raw else None
    )
    _approval_reason: str = approval_receipt_raw.get("reason", "") if approval_receipt_raw else ""

    cost_raw = entry.get("estimated_cost")
    # A retried run's headline cost is the true total, but only when the record stored
    # every escalation's cost. Otherwise it stays exactly what was recorded (the first
    # attempt); nothing is added on a guess.
    if entry.get("retry_triggered") is True:
        _run_total, _run_total_complete = run_total_cost(entry)
        if _run_total_complete and _run_total is not None:
            cost_raw = _run_total
    cost_provenance = entry.get("cost_provenance") if isinstance(entry.get("cost_provenance"), str) else None
    if cost_raw is None:
        _sr_costs = [
            s["cost"] for s in (entry.get("stage_runs") or [])
            if isinstance(s, dict) and s.get("cost") is not None
        ]
        if _sr_costs:
            cost_raw = sum(_sr_costs)

    # Some agent surfaces expose trustworthy token usage but no dollar total.
    # In that case price the recorded usage against the exact model's dated
    # official list rate. Provider/agent-reported cost still wins, unknown or
    # multi-model usage stays unknown, and no token provenance means no estimate.
    # A capture that recorded per-model usage (Claude Code transcript, with
    # its 5m/1h cache-write split) already priced it per model -- or decided
    # it cannot be priced honestly; one aggregate rate would understate it.
    _capture_raw = entry.get("capture")
    _per_model_usage = isinstance(_capture_raw, dict) and isinstance(_capture_raw.get("usage_by_model"), dict)
    if cost_raw is None and isinstance(entry.get("tokens_provenance"), str) and not _per_model_usage:
        _pricing_model = single_pricing_model(entry)
        _cost_estimate = estimate_usage_cost(
            _pricing_model,
            input_tokens=entry.get("prompt_tokens"),
            output_tokens=entry.get("completion_tokens"),
            cache_read_tokens=entry.get("cache_read_tokens"),
            cache_write_tokens=entry.get("cache_creation_tokens"),
        )
        if _cost_estimate is not None:
            cost_raw = _cost_estimate.usd
            cost_provenance = COST_PROVENANCE_OFFICIAL_RATE

    if cost_raw is None:
        cost_display = "Not recorded"
    elif cost_provenance:
        # Provider/agent-reported figures (Claude Code's own status-line cost,
        # OpenTelemetry cost metrics, ...) are all documented as approximate,
        # never billing truth -- labelled "est." rather than shown as fact.
        cost_display = f"${cost_raw:.2f} est."
    else:
        cost_display = f"${cost_raw:.4f}"

    summary = entry.get("summary") or ""
    _review_findings_raw = entry.get("findings") or []
    if _review_findings_raw:
        _fp_for_result = [
            f["path"] for f in (entry.get("files_detail") or [])
            if isinstance(f, dict) and f.get("path")
        ]
        _findings_objs = [
            ShardFinding(severity=f.get("severity", "Note"), message=f.get("message", ""))
            for f in _review_findings_raw if isinstance(f, dict)
        ]
        _vis_sub, _meta_g, _, _raw_total = group_review_findings(_findings_objs)
        _vis_areas = len(_vis_sub) + (1 if _meta_g else 0)
        _base = (
            f"{_raw_total} {'issue' if _raw_total == 1 else 'issues'} found"
            if _raw_total == _vis_areas
            else f"{_vis_areas} issue areas found. {_raw_total} raw findings recorded"
        )
        result = _base + ("; review files created." if _fp_for_result else ".")
    elif entry.get("is_review_task"):
        _dom = entry.get("review_domain") or ""
        _dfiles = entry.get("domain_files") or []
        if not _dfiles and _dom:
            from openshard.review.domain_files import no_files_message
            _msg = no_files_message(_dom)
            result = _msg if _msg else "Review completed."
        elif _dfiles and _dom == "docs_onboarding":
            _readme = next((f for f in _dfiles if "readme" in f.lower()), _dfiles[0])
            result = f"{_readme} inspected."
        else:
            result = "Review completed."
    else:
        result = _result_display(summary) or "Not recorded"

    _files_all = [f for f in (entry.get("files_detail") or []) if isinstance(f, dict)]
    files_detail_raw = [f for f in _files_all if f.get("attribution") not in _EXCLUDED_ATTRIBUTIONS]
    _files_excluded = [f for f in _files_all if f.get("attribution") in _EXCLUDED_ATTRIBUTIONS]
    _changes_block = entry.get("changes") if isinstance(entry.get("changes"), dict) else None
    files_touched = [f["path"] for f in files_detail_raw if "path" in f]

    diff_review = entry.get("diff_review") or {}
    if not files_touched:
        _dr_changed = diff_review.get("changed_files")
        if isinstance(_dr_changed, list):
            files_touched = [f for f in _dr_changed if isinstance(f, str)]

    # For runs that explicitly recorded zero file changes, clear files_touched so
    # that any stale files_detail entries (e.g. a model-generated report that was
    # discarded by the read-only safety net) do not appear as changed evidence.
    # Only fires when the entry carries explicit counters — older/minimal entries
    # that lack these keys but have diff_review.changed_files are left untouched.
    _change_counter_keys = ("files_created", "files_updated", "files_deleted")
    _has_explicit_counters = any(k in entry for k in _change_counter_keys)
    if _has_explicit_counters:
        _changed_count = sum(int(entry.get(k) or 0) for k in _change_counter_keys)
        if _changed_count == 0:
            files_touched = []

    diff_added = diff_review.get("added_lines")
    diff_removed = diff_review.get("removed_lines")
    if diff_added is None:
        fr = entry.get("final_report") or {}
        diff_added = fr.get("added_lines")
    if diff_removed is None:
        fr = entry.get("final_report") or {}
        diff_removed = fr.get("removed_lines")
    lines_not_recorded_reason: str | None = None
    if diff_added is None and diff_removed is None:  # hook capture's git numstat counts, or why unknown
        diff_added, diff_removed, lines_not_recorded_reason = recorded_line_counts(entry)

    stage_runs = entry.get("stage_runs") or []
    _is_ro = entry.get("routing_rationale") == "read-only analysis" or bool(form_factor.get("read_only"))
    model_stages: list[tuple[str, str]] = [
        (
            _STAGE_DISPLAY_LABELS.get(
                "analysis" if (_is_ro and s["stage_type"] == "implementation") else s["stage_type"],
                s["stage_type"].capitalize(),
            ),
            _display_model_name(s["model"]),
        )
        for s in stage_runs
        if isinstance(s, dict) and "stage_type" in s and "model" in s
    ]
    if not model_stages:
        # Hook capture's observed models (a /model switch, subagents): never one model.
        from openshard.history.observed_models import observed_model_stages

        model_stages = observed_model_stages(entry.get("capture"), _display_model_name)

    command_policy = entry.get("command_policy") or {}
    allowed_paths = list(command_policy.get("allowed_paths") or [])
    blocked_paths = list(command_policy.get("blocked_paths") or [])
    blocked_commands = list(command_policy.get("blocked_commands") or [])

    findings = _extract_findings(entry)
    agent_notes = _safe_str_list(entry.get("agent_notes"))

    repo: str | None = entry.get("repo_name") or None
    if repo is None:
        repo = _workspace_folder_name(entry.get("workspace_path"))

    obs = entry.get("observation") or {}
    _dirty = obs.get("dirty_diff_present")
    if _dirty is True:
        git_state = "Changes pending"
    elif _dirty is False:
        git_state = "Clean"
    else:
        git_state = None
    if git_state is None:
        _gd = entry.get("git_dirty")
        if _gd is True:
            git_state = "Changes pending"
        elif _gd is False:
            git_state = "Clean"

    cqs = entry.get("context_quality_score") or {}
    _cqs_level = cqs.get("level") if isinstance(cqs, dict) else None
    if _cqs_level in ("good", "strong"):
        context_quality: str | None = "Good"
    elif _cqs_level == "fair":
        context_quality = "Partial"
    elif _cqs_level == "weak":
        context_quality = "Weak"
    else:
        context_quality = None

    _fc = entry.get("file_context") or {}
    _fc_read = _fc.get("files_read")
    _fc_paths = _fc.get("paths")
    if type(_fc_read) is int:
        files_read_count: int | None = _fc_read
    else:
        _fr2 = entry.get("final_report") or {}
        _snip = _fr2.get("snippet_files")
        files_read_count = _snip if type(_snip) is int else None
    inspected_files = [p for p in _fc_paths if isinstance(p, str)] if isinstance(_fc_paths, list) else []
    files_referenced: list[str] = sorted({f.path for f in findings if f.path})
    # Merge domain-specific evidence files (CI/CD, auth, docs, tests) as inspected.
    _domain_files_raw = entry.get("domain_files") or []
    _domain_inspected = [p for p in _domain_files_raw if isinstance(p, str)]
    file_evidence = _build_file_evidence(
        inspected_files + _domain_inspected, files_referenced, files_touched
    )

    _adv_raw = entry.get("model_advisory")
    _model_advisory: list[dict] = []
    if isinstance(_adv_raw, list):
        for _a in _adv_raw:
            if isinstance(_a, dict) and "model_id" in _a:
                _model_advisory.append(_a)

    _fra_raw = entry.get("feedback_routing_advisory")
    _feedback_routing_advisory: dict | None = None
    if isinstance(_fra_raw, dict) and _fra_raw.get("advisory_only") is True:
        _feedback_routing_advisory = _fra_raw

    # Build evidence capsules: preserve any existing capsules, then append secret scan findings.
    _evidence_capsules: list[EvidenceCapsule] = []
    _existing_caps = entry.get("evidence_capsules") or []
    if isinstance(_existing_caps, list):
        for _ec_raw in _existing_caps:
            if isinstance(_ec_raw, dict) and "capsule_id" in _ec_raw:
                _evidence_capsules.append(EvidenceCapsule(
                    capsule_id=_ec_raw.get("capsule_id", ""),
                    kind=_ec_raw.get("kind", ""),
                    summary=_ec_raw.get("summary", ""),
                    source=_ec_raw.get("source"),
                    path=_ec_raw.get("path"),
                    line=_ec_raw.get("line"),
                    severity=_ec_raw.get("severity"),
                ))
    _ss_raw = entry.get("secret_scan_result")
    if isinstance(_ss_raw, dict):
        for _ssf in (_ss_raw.get("findings") or []):
            if not isinstance(_ssf, dict):
                continue
            _evidence_capsules.append(EvidenceCapsule(
                capsule_id=_ssf.get("fingerprint") or "secret-unknown",
                kind="secret_scan",
                summary=f"Potential {_ssf.get('kind', 'secret')} detected and redacted",
                source="secret_scanner",
                path=_ssf.get("path"),
                line=_ssf.get("line"),
                severity=_ssf.get("severity"),
            ))

    _valid_decisions = frozenset({"allow", "ask", "deny", "not_applicable"})
    _policy_decisions: list[dict] = []
    _policy_decisions_raw = entry.get("policy_decisions") or []
    if isinstance(_policy_decisions_raw, list):
        for _pd in _policy_decisions_raw:
            if (
                isinstance(_pd, dict)
                and _pd.get("decision_id")
                and _pd.get("decision") in _valid_decisions
            ):
                _policy_decisions.append(_pd)

    # Context utilisation — read flat keys written by _populate_context_usage_metadata.
    _ctx_considered = entry.get("context_files_considered_count")
    if not isinstance(_ctx_considered, int):
        _ctx_considered = None
    _ctx_injected = entry.get("context_files_injected_count")
    if not isinstance(_ctx_injected, int):
        _ctx_injected = None
    _ctx_ratio_raw = entry.get("context_utilisation_ratio")
    if isinstance(_ctx_ratio_raw, bool) or not isinstance(_ctx_ratio_raw, (int, float)):
        _ctx_ratio_raw = None
    _ctx_ratio: float | None = float(_ctx_ratio_raw) if _ctx_ratio_raw is not None else None

    # Execution spans — read list written by _populate_execution_span_metadata.
    _execution_spans: list[ExecutionSpan] = []
    _raw_spans = entry.get("execution_spans") or []
    if isinstance(_raw_spans, list):
        for _s in _raw_spans:
            if not isinstance(_s, dict):
                continue
            _s_id = _s.get("span_id")
            _s_name = _s.get("name")
            if not _s_id or not _s_name:
                continue
            _s_dur = _s.get("duration_ms")
            _execution_spans.append(ExecutionSpan(
                span_id=str(_s_id),
                name=str(_s_name),
                kind=str(_s.get("kind") or "phase"),
                started_at=_s.get("started_at") or None,
                duration_ms=int(_s_dur) if isinstance(_s_dur, int) else None,
                status=_s.get("status") or None,
                error_class=_s.get("error_class") or None,
                summary=str(_s["summary"])[:200] if _s.get("summary") else None,
            ))

    from openshard.history.provenance import build_provenance_from_entry as _build_prov
    try:
        _provenance = _build_prov(entry)
    except Exception:
        _provenance = []

    from openshard.history.event import events_from_entry as _build_events
    try:
        _events = _build_events(entry)
    except Exception:
        _events = []

    _shard_id_val = entry.get("shard_id") or _make_shard_id(timestamp, index)
    _receipt_id_val = stored_receipt_id(entry)
    _task_id_val = stored_task_id(entry)
    _capture_completeness_val = derive_capture_completeness(entry)
    _task_short_val = _trunc(task, 70)
    _run_id_val = entry.get("run_id") or timestamp or None
    _attempt_number_val = entry.get("attempt_number") if isinstance(entry.get("attempt_number"), int) else None

    # Turn-completion status -- see openshard.adapters.claude_hooks._task_status.
    # capture.task_status is namespaced metadata only claude_hooks sets today;
    # harmless (absent -> None -> no line rendered) for every other producer.
    _capture_for_status: dict = entry.get("capture") or {}
    _capture_for_status = _capture_for_status if isinstance(_capture_for_status, dict) else {}
    _task_status_raw = _capture_for_status.get("task_status")
    # v0.4.4 wording: a Stop hook proves the agent's *turn* ended, not that
    # the task is complete or its result correct. Never "Completed" alone.
    _task_completion_display = (
        {
            "turn_completed": "Turn completed (unverified)",
            "in_progress": "In progress",
            "ended_no_turn": "Session ended (no turn observed)",
        }.get(_task_status_raw)
        if isinstance(_task_status_raw, str)
        else None
    )
    _integrity_status_val = integrity_status(stored_entry)
    _integrity_val = integrity_label(_integrity_status_val)

    # Token usage -- only ever surfaced on the receipt when a producer stamped
    # an explicit provenance token alongside the counts (see build_hook_entry).
    # Several older/native entries carry bare prompt_tokens/completion_tokens
    # with no such marker; those must stay off the receipt's Tokens display
    # unchanged (fail closed rather than assume a source for pre-existing data).
    _tokens_provenance = entry.get("tokens_provenance") if isinstance(entry.get("tokens_provenance"), str) else None
    _tokens_input = entry.get("prompt_tokens") if _tokens_provenance else None
    _tokens_output = entry.get("completion_tokens") if _tokens_provenance else None
    _tokens_cache_creation = entry.get("cache_creation_tokens") if _tokens_provenance else None
    _tokens_cache_read = entry.get("cache_read_tokens") if _tokens_provenance else None

    return ShardReceipt(
        shard_id=_shard_id_val,
        created_at=timestamp,
        task_short=_task_short_val,
        task_full=task,
        task_title=resolve_task_title(entry),
        agent=agent,
        strategy=strategy,
        model_display=model_display,
        risk=risk,
        sandbox=sandbox,
        files_changed=files_changed,
        checks_display=checks_display,
        approval=approval,
        approval_required=_approval_required,
        approval_granted=_approval_granted,
        approval_reason=_approval_reason,
        cost_display=cost_display,
        result=result,
        human_summary=entry.get("human_summary") if isinstance(entry.get("human_summary"), str) else None,
        status=status,
        duration_seconds=entry.get("duration_seconds"),
        owner=entry.get("owner") if isinstance(entry.get("owner"), str) else None,
        repo=repo,
        repo_identity=_stored_repo_identity(entry),
        branch=entry.get("git_branch") or None,
        git_state=git_state,
        context_quality=context_quality,
        files_read_count=files_read_count,
        inspected_files=inspected_files,
        files_referenced=files_referenced,
        files_detail=files_detail_raw,
        files_touched=files_touched,
        osn_actions=_osn_actions(entry),
        diff_added=diff_added,
        diff_removed=diff_removed,
        lines_not_recorded_reason=lines_not_recorded_reason,
        cost_raw=cost_raw,
        model_stages=model_stages,
        allowed_paths=allowed_paths,
        blocked_paths=blocked_paths,
        blocked_commands=blocked_commands,
        findings=findings,
        agent_notes=agent_notes,
        check_results=check_results,
        run_timeline=[e for e in (entry.get("run_timeline") or []) if isinstance(e, dict) and e.get("label")],
        developer_feedback=entry.get("developer_feedback") or None,
        file_evidence=file_evidence,
        model_advisory=_model_advisory,
        feedback_routing_advisory=_feedback_routing_advisory,
        schema_version=entry.get("schema_version") or None,
        git_dirty=entry.get("git_dirty") if isinstance(entry.get("git_dirty"), bool) else None,
        git_head_commit_hash=entry.get("git_head_commit_hash") or None,
        git_base_branch=entry.get("git_base_branch") or None,
        git_base_commit_hash=entry.get("git_base_commit_hash") or None,
        error_class=entry.get("error_class") or None,
        error_message=entry.get("error_message") or None,
        context_files_considered_count=_ctx_considered,
        context_files_injected_count=_ctx_injected,
        context_utilisation_ratio=_ctx_ratio,
        execution_spans=_execution_spans,
        evidence_capsules=_evidence_capsules,
        provenance=_provenance,
        events=_events,
        policy_decisions=_policy_decisions,
        adapter=entry.get("adapter") or None,
        adapter_available=entry.get("adapter_available") if isinstance(entry.get("adapter_available"), bool) else None,
        adapter_command=entry.get("adapter_command") if isinstance(entry.get("adapter_command"), list) else [],  # type: ignore[arg-type]  # value guarded by isinstance; Any from dict.get
        adapter_exit_code=entry.get("adapter_exit_code") if isinstance(entry.get("adapter_exit_code"), int) else None,
        adapter_stdout_summary=entry.get("adapter_stdout_summary") or None,
        adapter_stderr_summary=entry.get("adapter_stderr_summary") or None,
        adapter_duration_ms=entry.get("adapter_duration_ms") if isinstance(entry.get("adapter_duration_ms"), int) else None,
        safe_workspace_kind=((entry.get("sandbox") or {}).get("sandbox_type") or None),
        safe_workspace_display_name=((entry.get("sandbox") or {}).get("safe_workspace_display_name") or None),
        verification_status=_v_status,
        verification_reason=_v_reason,
        verification_returncode=_v_returncode,
        verification_duration_seconds=_v_duration,
        verification_raw_output_stored=_v_raw_stored,
        verification=_vev.to_dict(),
        run_id=_run_id_val,
        attempt_number=_attempt_number_val,
        task_completion=_task_completion_display,
        tokens_input=_tokens_input if isinstance(_tokens_input, int) else None,
        tokens_output=_tokens_output if isinstance(_tokens_output, int) else None,
        tokens_cache_creation=_tokens_cache_creation if isinstance(_tokens_cache_creation, int) else None,
        tokens_cache_read=_tokens_cache_read if isinstance(_tokens_cache_read, int) else None,
        tokens_provenance=_tokens_provenance,
        cost_provenance=cost_provenance,
        receipt_id=_receipt_id_val,
        task_id=_task_id_val,
        capture_completeness=_capture_completeness_val,
        integrity=_integrity_val,
        integrity_status=_integrity_status_val,
        post_session_verification=(
            post_session_verification if isinstance(post_session_verification, dict) else None
        ),
        changes=_changes_summary(_changes_block),
        files_excluded=_files_excluded,
        recorded_evidence={**project_entry_evidence(entry), "gaming_signals": gaming_signals_block(entry)},
        usage=effective_usage(entry, _own_usage_attestations(entry, usage_attestations)),
        shard=build_shard(
            entry,
            shard_id=_shard_id_val,
            created_at=timestamp,
            task_short=_task_short_val,
            task_full=task,
        ),
    )


# Agents whose usage normally arrives from Cursor after the Receipt is written.
_CURSOR_USAGE_AGENTS = frozenset({"cursor", "grok_bot"})


def _own_usage_attestations(entry: dict, attestations: list[dict] | None) -> list[dict]:
    """Only attestations naming this record's receipt_id: usage never crosses Receipts."""
    rid = entry.get("receipt_id")
    if not isinstance(rid, str) or not rid or not isinstance(attestations, list):
        return []
    return [a for a in attestations if isinstance(a, dict) and a.get("receipt_id") == rid]

def _row(label: str, value: str, width: int = _COL) -> str:
    return f"{_INDENT}{label:<{width}}{value}"

_FILE_CHANGE_LETTERS: dict[str, str] = {"create": "A", "update": "M", "delete": "D"}

_MAX_OSN_ACTIONS = 200

def _osn_actions(entry: dict) -> list[dict]:
    """The stored action trail of an OSN iterative run, re-validated. Empty for every other record."""
    loop = entry.get("osn_loop") if isinstance(entry.get("osn_loop"), dict) else None
    if not loop:
        return []
    out: list[dict] = []
    for attempt in loop.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        n = attempt.get("n")
        for a in attempt.get("actions") or []:
            if not isinstance(a, dict) or not isinstance(a.get("kind"), str):
                continue
            raw_target = a.get("target")
            target: str = raw_target if isinstance(raw_target, str) else ""
            intent = a.get("intent")
            out.append({
                "attempt": n if isinstance(n, int) and not isinstance(n, bool) else None,
                "turn": a.get("turn") if isinstance(a.get("turn"), int) else None,
                "kind": str(a["kind"])[:40],
                "target": target[:200],
                "intent": intent[:200] if isinstance(intent, str) else "",
                "role": a.get("role") if isinstance(a.get("role"), str) else None,
                "model": a.get("model") if isinstance(a.get("model"), str) else None,
                "decision": a.get("decision") if isinstance(a.get("decision"), str) else None,
                "executed": bool(a.get("executed")),
                "ok": a.get("ok") if isinstance(a.get("ok"), bool) else None,
                "summary": (a.get("result") or {}).get("summary") if isinstance(a.get("result"), dict) else None,
            })
            if len(out) >= _MAX_OSN_ACTIONS:
                return out
    return out

def _format_review_checks(checks: list[dict]) -> tuple[str, list[str]]:
    """Return (checks_display, per-check lines) for a list of review check dicts."""
    passed = [c for c in checks if c.get("status") == "passed"]
    failed = [c for c in checks if c.get("status") == "failed"]
    skipped = [c for c in checks if c.get("status") == "skipped"]

    parts: list[str] = []
    if failed:
        parts.append(f"{len(failed)} failed")
    if passed:
        parts.append(f"{len(passed)} passed")
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    checks_display = ", ".join(parts) if parts else "Not run"

    pass_icon = "✓" if _UNICODE_OK else "+"
    fail_icon = "✖" if _UNICODE_OK else "x"
    skip_icon = "-"
    lines: list[str] = []
    for c in checks:
        status = c.get("status", "skipped")
        name = c.get("name", "check")
        reason = c.get("reason") or ""
        summary = c.get("summary") or ""
        if status == "passed":
            lines.append(f"{pass_icon} {name:<22}{summary if summary else 'passed'}")
        elif status == "failed":
            lines.append(f"{fail_icon} {name:<22}{summary if summary else 'failed'}")
        else:
            suffix = f"skipped — {reason}" if reason else "skipped"
            lines.append(f"{skip_icon} {name:<22}{suffix}")

    return checks_display, lines

def build_live_run_receipt(
    *,
    task: str,
    run_id: str,
    run_index: int | None,
    agent: str,
    stage_runs: list,
    routing_model: str | None,
    risk: str,
    sandbox: str,
    files_changed: int,
    verification_attempted: bool | None,
    verification_passed: bool | None,
    approval: str,
    estimated_cost: float | None,
    result_summary: str,
    result: str | None = None,
    agent_notes: list[str] | None = None,
    findings: list[ShardFinding] | None = None,
    run_timeline: list[dict] | None = None,
    review_checks: list[dict] | None = None,
    routing_selected_model: str | None = None,
) -> ShardReceipt:
    """Build a ShardReceipt from live run metadata (before log write). Pure, no I/O.

    result: pre-formatted result string that bypasses _result_display() processing.
            Use when the caller has already computed a clean result (e.g. two-count
            review format "N issue areas found. M raw findings recorded.").
    routing_selected_model: the final scored model (if scoring ran); adds the
            "Auto → {model}" prefix to match build_shard_receipt output.
    """
    _model_stages: list[tuple[str, str]] = [
        (
            _STAGE_DISPLAY_LABELS.get(
                getattr(getattr(sr, "stage", None), "stage_type", "") or "",
                (getattr(getattr(sr, "stage", None), "stage_type", None) or "").capitalize(),
            ),
            _display_model_name(getattr(sr, "model", "") or ""),
        )
        for sr in stage_runs
        if getattr(getattr(sr, "stage", None), "stage_type", None) and getattr(sr, "model", None)
    ]
    if routing_selected_model:
        _model_display = f"Auto → {_display_model_name(routing_selected_model)}"
    elif routing_model:
        _model_display = _display_model_name(routing_model)
    elif _model_stages:
        _model_display = _model_stages[0][1]
    else:
        _model_display = "Not recorded"

    if verification_attempted is None or not verification_attempted:
        _checks = "Not run"
        _status = "No checks run"
    elif verification_passed is True:
        _checks = "1/1 passed"
        _status = "Passed"
    elif verification_passed is False:
        _checks = "0/1 passed"
        _status = "Failed"
    else:
        # Attempted with no recorded outcome is not "no checks run".
        _checks = "Attempted (unverified)"
        _status = "Checks attempted, result not verified"

    _check_results: list[str] = []
    if review_checks:
        _checks, _check_results = _format_review_checks(review_checks)
        _status = f"Checks: {_checks}"

    _cost_display = f"${estimated_cost:.4f}" if estimated_cost is not None else "Not recorded"
    _result = result if result is not None else (_result_display(result_summary or "") or "Not recorded")

    return ShardReceipt(
        shard_id=_make_shard_id(run_id, run_index),
        created_at=run_id,
        task_short=_trunc(task, 70),
        task_full=task,
        task_title=derive_task_title(task),
        agent=agent,
        strategy="Not recorded",
        model_display=_model_display,
        risk=risk,
        sandbox=sandbox,
        files_changed=files_changed,
        checks_display=_checks,
        approval=approval,
        cost_display=_cost_display,
        result=_result,
        status=_status,
        duration_seconds=None,
        model_stages=_model_stages,
        agent_notes=[n.split("\n")[0][:300] for n in (agent_notes or []) if n][:5],
        findings=list(findings) if findings else [],
        run_timeline=list(run_timeline) if run_timeline else [],
        check_results=_check_results,
        schema_version="1.1",
    )


# The renderers moved to history/receipt_render (see its docstring); the names
# stay importable from here for existing callers and tests.
_RENDER_EXPORTS = frozenset({
    "_capture_rows",
    "_capture_rows_full",
    "_display_receipt_number",
    "_display_shard_number",
    "_evidence_summary",
    "_explorer_line",
    "_first_attempt_cost_label",
    "_fmt_timestamp",
    "_format_timeline_label",
    "_format_token_count",
    "_models_label_and_value",
    "_osn_checks_display",
    "_osn_repository_state_line",
    "_render_osn_actions",
    "_render_osn_compact_receipt",
    "_render_osn_parallel",
    "_render_osn_roles",
    "_role_line",
    "_role_model_lines",
    "_shows_usage_row",
    "_tool_activity_counts",
    "_tool_failure_counts",
    "_truncate_compact",
    "failed_activity_rows",
    "render_compact_shard_receipt",
    "render_full_shard_receipt",
})


def __getattr__(name: str) -> Any:
    if name in _RENDER_EXPORTS:
        from openshard.history import receipt_render

        return getattr(receipt_render, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
