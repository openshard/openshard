from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from openshard.history.shard import (
    ORIGIN_EXTERNAL_OBSERVED,
    Shard,
    build_shard,
    derive_shard_identity,
)
from openshard.run.timeline import normalize_timeline

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
    "openai/gpt-5.5": "GPT-5.5",
    "z-ai/glm-5.1": "GLM-5.1",
}

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
            tagged.append(("word", part.capitalize()))
    out = ""
    for i, (kind, text) in enumerate(tagged):
        if i == 0:
            out = text
        elif kind == "version" and tagged[i - 1][0] == "abbrev":
            out += "-" + text
        else:
            out += " " + text
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
    repo: str | None = None
    branch: str | None = None
    git_state: str | None = None
    context_quality: str | None = None
    files_read_count: int | None = None
    inspected_files: list[str] = field(default_factory=list)
    files_referenced: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    files_detail: list[dict] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    blocked_commands: list[str] = field(default_factory=list)
    check_results: list[str] = field(default_factory=list)
    diff_added: int | None = None
    diff_removed: int | None = None
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


def _workspace_folder_name(raw: object) -> str | None:
    if not raw:
        return None
    value = str(raw).rstrip("\\/")
    if not value:
        return None
    if "\\" in value:
        return PureWindowsPath(value).name or None
    return Path(value).name or None


def build_shard_receipt(entry: dict, index: int | None = None) -> ShardReceipt:
    """Convert a raw run-history entry dict into a ShardReceipt. Never raises."""
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
    # Mirror the live-receipt review task risk floor: review runs are always at least High
    if entry.get("is_review_task") and risk in ("Not recorded", "Low"):
        risk = "High"

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
    if files_changed == 0:
        fr = entry.get("final_report") or {}
        diff = entry.get("diff_review") or {}
        diff_files = fr.get("diff_files") or diff.get("changed_files") or []
        if diff_files:
            files_changed = len(diff_files)

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
        checks_display = "Not run"
        status = "No checks run"

    (
        _v_status,
        _v_reason,
        _v_returncode,
        _v_duration,
        _v_raw_stored,
    ) = _verification_from_osn_contract(entry)

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
    elif ff_read_only or write_path == "pipeline":
        approval = "Not required"
    else:
        approval = "Not recorded"
    _approval_required = bool(approval_receipt_raw)
    _approval_granted: bool | None = (
        approval_receipt_raw.get("granted") if approval_receipt_raw else None
    )
    _approval_reason: str = approval_receipt_raw.get("reason", "") if approval_receipt_raw else ""

    cost_raw = entry.get("estimated_cost")
    if cost_raw is None:
        _sr_costs = [
            s["cost"] for s in (entry.get("stage_runs") or [])
            if isinstance(s, dict) and s.get("cost") is not None
        ]
        if _sr_costs:
            cost_raw = sum(_sr_costs)
    cost_display = f"${cost_raw:.4f}" if cost_raw is not None else "Not recorded"

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

    files_detail_raw = entry.get("files_detail") or []
    files_touched = [f["path"] for f in files_detail_raw if isinstance(f, dict) and "path" in f]

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

    _shard_id_val = entry.get("shard_id") or _make_shard_id(timestamp, index)
    _task_short_val = _trunc(task, 70)
    _run_id_val = entry.get("run_id") or timestamp or None
    _attempt_number_val = entry.get("attempt_number") if isinstance(entry.get("attempt_number"), int) else None

    return ShardReceipt(
        shard_id=_shard_id_val,
        created_at=timestamp,
        task_short=_task_short_val,
        task_full=task,
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
        status=status,
        duration_seconds=entry.get("duration_seconds"),
        repo=repo,
        branch=entry.get("git_branch") or None,
        git_state=git_state,
        context_quality=context_quality,
        files_read_count=files_read_count,
        inspected_files=inspected_files,
        files_referenced=files_referenced,
        files_detail=files_detail_raw,
        files_touched=files_touched,
        diff_added=diff_added,
        diff_removed=diff_removed,
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
        run_id=_run_id_val,
        attempt_number=_attempt_number_val,
        shard=build_shard(
            entry,
            shard_id=_shard_id_val,
            created_at=timestamp,
            task_short=_task_short_val,
            task_full=task,
        ),
    )


def _row(label: str, value: str, width: int = _COL) -> str:
    return f"{_INDENT}{label:<{width}}{value}"


def _models_label_and_value(receipt: ShardReceipt) -> tuple[str, str]:
    """Return (label, value) for the Model/Models row in the compact receipt."""
    if receipt.model_stages:
        unique = list(dict.fromkeys(m for _, m in receipt.model_stages))
        if len(unique) == 1:
            return "Model", unique[0]
        if len(unique) == 2:
            return "Models", f"{unique[0]} → {unique[1]}"
        return "Models", ", ".join(unique)
    return "Model", receipt.model_display


def _truncate_compact(text: str, max_chars: int) -> str:
    """Return first line of text, word-safely truncated to max_chars with … if cut."""
    line = text.split("\n")[0]
    if len(line) <= max_chars:
        return line
    cut = line.rfind(" ", 0, max_chars)
    if cut > 0:
        return line[:cut] + "…"
    return line[:max_chars] + "…"


def _format_timeline_label(ev: dict) -> str:
    """Return an enriched display label for a timeline event using count/target fields."""
    key = ev.get("event", "")
    label = ev.get("label") or ""
    count = ev.get("count")
    target = ev.get("target")
    if key == "model_response_received" and target:
        return f"model responded: {target}"
    if key == "model_request_failed" and target:
        return f"model failed: {target}"
    if key == "review_checks_recorded" and count is not None:
        return f"checks recorded: {count}"
    if key == "static_findings_detected" and count is not None:
        return f"findings detected: {count}"
    if key == "receipt_saved" and target:
        return f"receipt saved: {target}"
    return label


def render_compact_shard_receipt(receipt: ShardReceipt) -> str:
    """Render a bordered, column-aligned RECEIPT block. Pure, no I/O."""
    file_str = f"{receipt.files_changed} file{'s' if receipt.files_changed != 1 else ''}"
    model_label, model_value = _models_label_and_value(receipt)

    lines = [
        _SEP,
        f"{_INDENT}RECEIPT {_EM} {receipt.shard_id}",
        _SEP,
        _row("Task", receipt.task_short),
        _row("Executor", receipt.agent),
    ]
    if receipt.shard is not None and receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED:
        lines.append(_row(
            "Capture",
            f"{receipt.shard.capture_depth} {_EM} OpenShard did not execute or verify this run",
        ))
    lines += [
        _row(model_label, model_value),
        _row("Risk", receipt.risk),
        _row("Sandbox", receipt.sandbox),
        _row("Changed", file_str),
        _row("Checks", receipt.checks_display),
        _row("Approval", receipt.approval),
        _row("Cost", receipt.cost_display),
        _row("Result", receipt.result),
    ]
    _secret_count = sum(1 for c in receipt.evidence_capsules if c.kind == "secret_scan")
    if _secret_count:
        lines.append(_row("Secrets", f"{_secret_count} finding(s) — see full receipt"))
    if receipt.developer_feedback:
        lines.append(_row("Feedback", receipt.developer_feedback.get("outcome", "")))

    _TOP_SEVERITIES = {"Critical", "High", "Medium"}
    top_raw = [f for f in receipt.findings if f.severity in _TOP_SEVERITIES]
    if top_raw:
        # Use generous per-severity caps — the hard limit is the total of 5 visible slots.
        # group_review_findings handles dedup and metadata grouping.
        visible_sub, meta_group, _, _ = group_review_findings(
            top_raw,
            caps={"Critical": 5, "High": 5, "Medium": 5, "Low": 0},
        )
        compact_list: list[ShardFinding] = list(visible_sub)
        if meta_group and len(compact_list) < 5:
            compact_list.append(meta_group)
        compact_list = compact_list[:5]
        hidden_receipt = max(0, len(top_raw) - len(compact_list))
        lines.append(_SEP)
        lines.append(f"{_INDENT}FINDINGS")
        for f in compact_list:
            icon = _FINDING_ICONS.get(f.severity, "⚠" if _UNICODE_OK else "!")
            lines.append(f"{_INDENT}{icon}  {_truncate_compact(f.message, 79)}")
        if hidden_receipt > 0:
            lines.append(f"{_INDENT}+{hidden_receipt} more findings recorded.")

    # Warning line: only shown when a note contains an explicit blocker keyword
    _WARNING_KEYWORDS = ("DO NOT RUN", "WARNING:", "BLOCKER:", "DANGER:", "DO NOT APPLY")
    warning_note = next(
        (n for n in receipt.agent_notes if any(kw in n.upper() for kw in _WARNING_KEYWORDS)),
        None,
    )
    if warning_note:
        lines += [_SEP, f"{_INDENT}{warning_note}"]

    lines.append(_SEP)
    return "\n".join(lines)


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
        _checks = "Not run"
        _status = "No checks run"

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


def _fmt_timestamp(ts: str) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, AttributeError):
        return ts


def render_full_shard_receipt(receipt: ShardReceipt, detail: str = "full") -> str:
    """Render a full structured SHARD block with consistent separator style. Pure, no I/O."""
    lines: list[str] = []

    lines += [_SEP, f"{_INDENT}SHARD {_EM} {receipt.shard_id}", _SEP, ""]

    if receipt.schema_version:
        lines.append(f"{_INDENT}SCHEMA")
        lines.append(_row("Version", receipt.schema_version))
        lines.append("")

    lines += [f"{_INDENT}TASK", f"{_INDENT}{receipt.task_full}", ""]

    lines.append(f"{_INDENT}EXECUTION")
    lines.append(_row("Executor", receipt.agent))
    lines.append(_row("Strategy", receipt.strategy))

    if receipt.model_stages:
        unique = list(dict.fromkeys(m for _, m in receipt.model_stages))
        if len(unique) == 1:
            lines.append(_row("Model", unique[0]))
        else:
            lines.append(f"{_INDENT}Models")
            _stage_col = max(len(s) for s, _ in receipt.model_stages) + 2
            for stage_lbl, model_name in receipt.model_stages:
                lines.append(f"{_INDENT}  {stage_lbl:<{_stage_col}}{model_name}")
    else:
        lines.append(_row("Model", receipt.model_display))

    dur = f"{receipt.duration_seconds:.1f}s" if receipt.duration_seconds is not None else "-"
    lines.append(_row("Duration", dur))
    lines.append(_row("Status", receipt.status))
    if receipt.attempt_number is not None:
        lines.append(_row("Attempt", f"{receipt.attempt_number} (Shard {receipt.shard_id})"))
    if receipt.shard is not None and receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED:
        lines.append(_row(
            "Capture",
            f"{receipt.shard.capture_depth} {_EM} OpenShard did not execute or verify this run",
        ))
    lines.append("")

    if receipt.adapter:
        lines.append(f"{_INDENT}ADAPTER")
        lines.append(_row("Name", receipt.adapter))
        if receipt.adapter_available is not None:
            lines.append(_row("Available", "yes" if receipt.adapter_available else "no"))
        if receipt.adapter_exit_code is not None:
            lines.append(_row("Exit code", str(receipt.adapter_exit_code)))
        if receipt.adapter_duration_ms is not None:
            lines.append(_row("Duration", f"{receipt.adapter_duration_ms} ms"))
        if receipt.adapter_command:
            _cmd_tokens = receipt.adapter_command[:3]
            _cmd_preview = " ".join(_cmd_tokens)
            if len(receipt.adapter_command) > 3:
                _cmd_preview += " …"
            lines.append(_row("Command", _cmd_preview[:120]))
        if receipt.adapter_stdout_summary:
            lines.append(_row("Stdout", receipt.adapter_stdout_summary[:200]))
        if receipt.adapter_stderr_summary:
            lines.append(_row("Stderr", receipt.adapter_stderr_summary[:200]))
        lines.append("")

    if receipt.error_class:
        lines.append(f"{_INDENT}ERROR")
        lines.append(_row("Class", receipt.error_class))
        if receipt.error_message:
            lines.append(_row("Message", receipt.error_message[:120]))
        lines.append("")

    if receipt.run_timeline:
        _chk = "✓" if _UNICODE_OK else "+"
        _fail = "✖" if _UNICODE_OK else "x"
        lines.append(f"{_INDENT}TIMELINE")
        _tl_events = normalize_timeline(receipt.run_timeline)
        _receipt_ev = next((e for e in _tl_events if e.get("event") == "receipt_saved"), None)
        _regular_evs = [e for e in _tl_events if e.get("event") != "receipt_saved"]
        _has_checks_ev = any(e.get("event") == "review_checks_recorded" for e in _regular_evs)
        _has_risk_ev = any(e.get("event") == "risk_classified" for e in _regular_evs)
        _has_inspected_ev = any(e.get("event") == "files_inspected" for e in _regular_evs)
        for _ev in _regular_evs:
            _sym = _chk if _ev.get("status", "completed") != "failed" else _fail
            lines.append(f"{_INDENT}  {_sym} {_format_timeline_label(_ev)}")
        # Synthesise proof facts from receipt fields when not covered by stored events
        if not _has_inspected_ev and receipt.files_read_count:
            lines.append(f"{_INDENT}  {_chk} files inspected: {receipt.files_read_count}")
        if not _has_risk_ev and receipt.risk and receipt.risk not in ("Not recorded", "-", ""):
            lines.append(f"{_INDENT}  {_chk} risk classified: {receipt.risk}")
        if not _has_checks_ev and receipt.checks_display and receipt.checks_display != "Not run":
            lines.append(f"{_INDENT}  {_chk} checks: {receipt.checks_display}")
        # receipt_saved always last
        if _receipt_ev:
            _sym = _chk if _receipt_ev.get("status", "completed") != "failed" else _fail
            lines.append(f"{_INDENT}  {_sym} {_format_timeline_label(_receipt_ev)}")
        lines.append("")

    lines.append(f"{_INDENT}CONTEXT")
    lines.append(_row("Repo", receipt.repo or "Not recorded"))
    lines.append(_row("Branch", receipt.branch or "Not recorded"))
    lines.append(_row("Git state", receipt.git_state or "Not recorded"))
    lines.append(_row("Quality", receipt.context_quality or "Not recorded"))
    if receipt.files_read_count is not None:
        lines.append(_row("Read", f"{receipt.files_read_count} file{'s' if receipt.files_read_count != 1 else ''}"))
    else:
        lines.append(_row("Read", "Not recorded"))
    touched_count = len(receipt.files_touched)
    if touched_count > 0:
        lines.append(_row("Touched", f"{touched_count} file{'s' if touched_count != 1 else ''}"))
    else:
        lines.append(_row("Touched", "-"))
    lines.append("")

    _git_new = (
        receipt.git_head_commit_hash is not None
        or receipt.git_base_branch is not None
        or receipt.git_base_commit_hash is not None
        or receipt.git_dirty is not None
        or receipt.safe_workspace_display_name is not None
    )
    if _git_new:
        lines.append(f"{_INDENT}GIT")
        if receipt.git_head_commit_hash:
            lines.append(_row("Head commit", receipt.git_head_commit_hash))
        if receipt.git_base_branch:
            lines.append(_row("Base branch", receipt.git_base_branch))
        if receipt.git_base_commit_hash:
            lines.append(_row("Base commit", receipt.git_base_commit_hash))
        if receipt.git_dirty is not None:
            lines.append(_row("Dirty", "yes" if receipt.git_dirty else "no"))
        if receipt.safe_workspace_kind and receipt.safe_workspace_kind != "none":
            lines.append(_row("Workspace", receipt.safe_workspace_kind))
        if receipt.safe_workspace_display_name:
            lines.append(_row("Workspace ID", receipt.safe_workspace_display_name))
        lines.append("")

    _ctx_util = (
        receipt.context_files_considered_count is not None
        or receipt.context_files_injected_count is not None
        or receipt.context_utilisation_ratio is not None
    )
    if _ctx_util:
        lines.append(f"{_INDENT}CONTEXT USAGE")
        if receipt.context_files_considered_count is not None:
            lines.append(_row("Considered", str(receipt.context_files_considered_count)))
        if receipt.context_files_injected_count is not None:
            lines.append(_row("Injected", str(receipt.context_files_injected_count)))
        if receipt.context_utilisation_ratio is not None:
            lines.append(_row("Utilisation", f"{receipt.context_utilisation_ratio:.0%}"))
        lines.append("")

    lines.append(f"{_INDENT}FILE EVIDENCE")
    lines.append("")
    _fe_cap = 10
    _fe_inspected = [fe for fe in receipt.file_evidence if "inspected" in fe.roles]
    _fe_findings = [fe for fe in receipt.file_evidence if "finding_source" in fe.roles]
    _fe_changed = [fe for fe in receipt.file_evidence if "changed" in fe.roles]
    for _fe_heading, _fe_group in (
        ("INSPECTED FILES", _fe_inspected),
        ("FILES WITH FINDINGS", _fe_findings),
    ):
        if _fe_group:
            lines.append(f"{_INDENT}  {_fe_heading}")
            for _fe in _fe_group[:_fe_cap]:
                lines.append(f"{_INDENT}    {_fe.path}")
            if len(_fe_group) > _fe_cap:
                lines.append(f"{_INDENT}    +{len(_fe_group) - _fe_cap} more")
            lines.append("")
    lines.append(f"{_INDENT}  CHANGED FILES")
    if _fe_changed:
        for _fe in _fe_changed[:_fe_cap]:
            lines.append(f"{_INDENT}    {_fe.path}")
        if len(_fe_changed) > _fe_cap:
            lines.append(f"{_INDENT}    +{len(_fe_changed) - _fe_cap} more")
    else:
        lines.append(f"{_INDENT}    none")
    lines.append("")

    if receipt.evidence_capsules:
        lines.append(f"{_INDENT}EVIDENCE CAPSULES")
        _ec_cap = 10
        for _ec in receipt.evidence_capsules[:_ec_cap]:
            _ec_loc = f"  [{_ec.path}:{_ec.line}]" if _ec.path and _ec.line is not None else (f"  [{_ec.path}]" if _ec.path else "")
            lines.append(f"{_INDENT}  {_ec.kind}  {_ec.summary}{_ec_loc}")
        if len(receipt.evidence_capsules) > _ec_cap:
            lines.append(f"{_INDENT}  +{len(receipt.evidence_capsules) - _ec_cap} more")
        lines.append("")

    lines.append(f"{_INDENT}POLICY")
    lines.append(_row("Risk", receipt.risk))
    lines.append(_row("Sandbox", receipt.sandbox))
    if receipt.allowed_paths:
        lines.append(_row("Allowed", ", ".join(receipt.allowed_paths[:3])))
    if receipt.blocked_paths:
        lines.append(_row("Blocked", ", ".join(receipt.blocked_paths[:3])))
    if receipt.blocked_commands:
        lines.append(_row("Commands", f"{len(receipt.blocked_commands)} blocked"))
    lines.append(_row("Approval", receipt.approval))
    lines.append("")

    if receipt.approval_required:
        lines.append(f"{_INDENT}APPROVAL")
        lines.append(_row("Required", "yes"))
        lines.append(_row("Status", "granted" if receipt.approval_granted else "denied"))
        if receipt.approval_reason:
            lines.append(_row("Reason", receipt.approval_reason))
        if not receipt.approval_granted:
            lines.append(_row("Result", "Writes blocked"))
        lines.append("")

    if receipt.policy_decisions:
        lines.append(f"{_INDENT}POLICY DECISIONS")
        _pd_cap = 10
        _pd_col_dec = 6
        _pd_col_act = 16
        for _pd in receipt.policy_decisions[:_pd_cap]:
            _pd_dec = str(_pd.get("decision") or "").ljust(_pd_col_dec)
            _pd_act = str(_pd.get("action") or "").ljust(_pd_col_act)
            _pd_reason = str(_pd.get("reason") or "")
            lines.append(f"{_INDENT}  {_pd_dec}  {_pd_act}  {_pd_reason}")
        if len(receipt.policy_decisions) > _pd_cap:
            lines.append(f"{_INDENT}  +{len(receipt.policy_decisions) - _pd_cap} more")
        lines.append("")

    lines.append(f"{_INDENT}CHECKS")
    if receipt.check_results:
        for cr in receipt.check_results:
            _cr = cr if detail == "full" else _truncate_compact(cr, 90)
            lines.append(f"{_INDENT}  {_cr}")
    else:
        lines.append(f"{_INDENT}{receipt.checks_display}")
    lines.append("")

    if receipt.execution_spans:
        lines.append(f"{_INDENT}EXECUTION SPANS")
        _es_cap = 10
        for _es in receipt.execution_spans[:_es_cap]:
            _dur = f"  {_es.duration_ms}ms" if _es.duration_ms is not None else ""
            _st = f"  {_es.status}" if _es.status else ""
            _ec_str = f"  [{_es.error_class}]" if _es.error_class else ""
            lines.append(f"{_INDENT}  {_es.kind}  {_es.name}{_st}{_dur}{_ec_str}")
        if len(receipt.execution_spans) > _es_cap:
            lines.append(f"{_INDENT}  +{len(receipt.execution_spans) - _es_cap} more")
        lines.append("")

    lines.append(f"{_INDENT}FINDINGS")
    if receipt.findings:
        # Show substantive findings (no cap in full receipt) grouped by severity.
        visible_sub, meta_group, _, _ = group_review_findings(
            receipt.findings,
            caps={"Critical": 999, "High": 999, "Medium": 999, "Low": 999},
        )
        current_severity: str | None = None
        for finding in visible_sub:
            if finding.severity != current_severity:
                if current_severity is not None:
                    lines.append("")
                lines.append(f"{_INDENT}{finding.severity.upper()}")
                current_severity = finding.severity
            icon = _FINDING_ICONS.get(finding.severity, "-")
            msg = finding.message
            if finding.path:
                loc = f"{finding.path}:{finding.line}" if finding.line is not None else finding.path
                msg = f"{msg}  [{loc}]"
            lines.append(f"{_INDENT}  {icon} {msg}")
        # Metadata section: show all examples (up to 10)
        if meta_group:
            if current_severity is not None:
                lines.append("")
            lines.append(f"{_INDENT}METADATA")
            # Collect all metadata findings for the expanded view
            meta_findings = [f for f in receipt.findings if _is_metadata_noise(f)]
            n = len(meta_findings)
            uses_labels = any("label" in f.message.lower() for f in meta_findings)
            has_gcp     = any("google_" in f.message for f in meta_findings)
            term   = "labels" if uses_labels else "tags"
            prefix = "GCP " if has_gcp else ""
            lines.append(f"{_INDENT}  ~ {n} {prefix}resources missing ownership/environment {term}")
            examples: list[str] = []
            seen_ex: set[str] = set()
            for f in meta_findings:
                parts = f.message.split()
                if len(parts) >= 2 and parts[0].lower() == "resource":
                    name = parts[1]
                    if name not in seen_ex:
                        seen_ex.add(name)
                        examples.append(name)
            for ex in examples[:10]:
                lines.append(f"{_INDENT}    {ex}")
            if len(examples) > 10:
                lines.append(f"{_INDENT}    ...and {len(examples) - 10} more")
    else:
        lines.append(f"{_INDENT}  No structured findings recorded.")
    lines.append("")

    if receipt.model_advisory:
        lines.append(f"{_INDENT}MODEL ADVISORY")
        lines.append(f"{_INDENT}  Advisory only — routing unchanged")
        lines.append(f"{_INDENT}  Generated from risk signal only")
        for _adv in receipt.model_advisory:
            _name = _adv.get("display_name") or _adv.get("model_id", "?")
            lines.append(f"{_INDENT}  {_name}")
            for _r in _adv.get("reasons", [])[:3]:
                lines.append(f"{_INDENT}    ↳ {_r}")
        lines.append("")

    if detail == "full" and receipt.feedback_routing_advisory:
        _fra = receipt.feedback_routing_advisory
        lines.append(f"{_INDENT}FEEDBACK ROUTING ADVISORY")
        lines.append(f"{_INDENT}  Advisory only — routing unchanged")
        _rec = _fra.get("recommendation", "").replace("_", " ")
        lines.append(f"{_INDENT}  Recommendation  {_rec}")
        lines.append(f"{_INDENT}  Confidence      {_fra.get('confidence', '')}")
        _reason = _fra.get("reason", "")
        if _reason:
            lines.append(f"{_INDENT}  Reason          {_reason}")
        _sigs = _fra.get("signals_considered") or {}
        _sig_parts = [f"{k}={v}" for k, v in _sigs.items() if isinstance(v, int) and v > 0]
        if _sig_parts:
            lines.append(f"{_INDENT}  Signals         {', '.join(_sig_parts)}")
        lines.append("")

    lines.append(f"{_INDENT}CHANGES")
    file_str = f"{receipt.files_changed} file{'s' if receipt.files_changed != 1 else ''}"
    if receipt.diff_added is not None and receipt.diff_removed is not None:
        file_str += f" changed (+{receipt.diff_added} / -{receipt.diff_removed})"
    elif receipt.files_changed > 0:
        file_str += " changed"
    lines.append(f"{_INDENT}{file_str}")
    for _fd in receipt.files_detail[:10]:
        if isinstance(_fd, dict) and "path" in _fd:
            lines.append(f"{_INDENT}  {_fd['path']}")
    if len(receipt.files_detail) > 10:
        lines.append(f"{_INDENT}  (+{len(receipt.files_detail) - 10} more)")
    lines.append("")

    lines.append(f"{_INDENT}COST")
    lines.append(f"{_INDENT}{receipt.cost_display}")
    lines.append("")

    lines += [_SEP, f"{_INDENT}RECEIPT"]
    lines.append(_row("Shard ID", receipt.shard_id))
    lines.append(_row("Created", _fmt_timestamp(receipt.created_at)))
    lines.append(_row("Result", receipt.result))
    lines.append(_SEP)

    return "\n".join(lines)
