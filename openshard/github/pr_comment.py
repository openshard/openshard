"""Local GitHub PR comment generator.

Builds a safe, deterministic GitHub markdown comment from the latest
OpenShard run entry and ShardReceipt.

No network calls. No GitHub API. No gh CLI. No shell execution.
No provider calls. No raw prompts, raw outputs, raw file contents,
raw JSON, absolute paths, .codegraph paths, or chain-of-thought.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt

_MAX_INSPECTED_FILES = 10
_MAX_CHECKS = 20
_MAX_WARNINGS = 20
_MAX_EVIDENCE = 20
_MAX_OSN_SECTIONS = 10
_MAX_TEXT = 300

_EM_DASH = "—"


def _cap_text(value: str) -> str:
    """Truncate to _MAX_TEXT chars and strip em dashes."""
    if not value:
        return ""
    cleaned = value.replace(_EM_DASH, "-")
    return cleaned[:_MAX_TEXT]


def _cap_list(items: list[str], limit: int) -> list[str]:
    """Return at most limit items, stripped and em-dash-free."""
    return [i.replace(_EM_DASH, "-")[:_MAX_TEXT] for i in items[:limit]]


def _is_safe_path(path: str) -> bool:
    """Return False for absolute paths and .codegraph entries."""
    if not path:
        return False
    p = path.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return False
    if ".codegraph" in p:
        return False
    return True


@dataclass
class PRCommentSummary:
    """Safe, bounded summary for a GitHub PR comment.

    raw_content_stored is always False - never populated from entry data.
    All list fields are capped. All text fields are capped at _MAX_TEXT chars.
    No absolute paths, no .codegraph paths, no em dashes.
    """

    enabled: bool = True
    source: str = "github_pr_comment_v1"
    title: str = "OpenShard Run Summary"
    run_status: str = ""
    risk: str = ""
    files_changed: int = 0
    # False when the capture path cannot see file changes: files_changed is then unknown, not zero.
    files_observable: bool = True
    inspected_files: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    osn_sections: list[str] = field(default_factory=list)
    recommended_next_step: str = ""
    manual_review_required: bool = False
    raw_content_stored: bool = False


def _detect_osn_sections(entry: dict) -> list[str]:
    """Detect which OSN sections are present in a run entry.

    Explicit metadata keys take priority. Fallbacks are used only when the
    explicit key is absent. An external or historical record never has OSN
    sections, whatever keys it carries.
    """
    from openshard.history.shard import (
        ORIGIN_EXTERNAL_OBSERVED,
        ORIGIN_HISTORICAL_IMPORT,
        derive_shard_identity,
    )

    if derive_shard_identity(entry)[1] in (ORIGIN_EXTERNAL_OBSERVED, ORIGIN_HISTORICAL_IMPORT):
        return []
    sections: list[str] = []

    # OSN OBSERVATION
    # Explicit: entry["osn_observation"] exists and enabled
    # Fallback: repo_observation step in osn_loop_summary.steps
    obs = entry.get("osn_observation")
    if isinstance(obs, dict) and obs.get("enabled"):
        sections.append("OSN OBSERVATION")
    else:
        loop_summary = entry.get("osn_loop_summary")
        if isinstance(loop_summary, dict):
            for step in (loop_summary.get("steps") or []):
                if (
                    isinstance(step, dict)
                    and step.get("name") == "repo_observation"
                    and step.get("status") not in ("skipped", None)
                ):
                    sections.append("OSN OBSERVATION")
                    break

    # OSN PROGRESS
    # Explicit: entry["osn_progress_memory"] exists and enabled
    # Fallback: osn_loop_summary exists
    prog = entry.get("osn_progress_memory")
    if isinstance(prog, dict) and prog.get("enabled"):
        sections.append("OSN PROGRESS")
    elif entry.get("osn_loop_summary") is not None:
        sections.append("OSN PROGRESS")

    # OSN LOOP
    # Explicit: entry["osn_loop_summary"] exists and enabled
    # Fallback: osn_loop exists
    loop_summary = entry.get("osn_loop_summary")
    if isinstance(loop_summary, dict) and loop_summary.get("enabled"):
        sections.append("OSN LOOP")
    elif entry.get("osn_loop") is not None:
        sections.append("OSN LOOP")

    # OSN VERIFICATION
    # Explicit: entry["osn_verification_contract"] exists and enabled
    # Fallback: verification_contract_result or validation_contract exists
    verif = entry.get("osn_verification_contract")
    if isinstance(verif, dict) and verif.get("enabled"):
        sections.append("OSN VERIFICATION")
    elif (
        entry.get("verification_contract_result") is not None
        or entry.get("validation_contract") is not None
    ):
        sections.append("OSN VERIFICATION")

    # OSN RETRY
    # Explicit: entry["osn_retry_diagnosis"] exists and enabled
    # Fallback: osn_loop_summary.retry_used or retry_count > 0
    retry = entry.get("osn_retry_diagnosis")
    if isinstance(retry, dict) and retry.get("enabled"):
        sections.append("OSN RETRY")
    else:
        loop_s = entry.get("osn_loop_summary")
        if isinstance(loop_s, dict):
            if loop_s.get("retry_used") or (loop_s.get("retry_count") or 0) > 0:
                sections.append("OSN RETRY")

    return sections[:_MAX_OSN_SECTIONS]


def _collect_warnings(entry: dict, receipt: ShardReceipt) -> list[str]:
    warnings: list[str] = []

    if receipt.error_class:
        warnings.append(f"Error class: {receipt.error_class}")

    approval = (receipt.approval or "").lower()
    if "denied" in approval or "rejected" in approval:
        warnings.append("Approval was denied")
    elif "required" in approval and "granted" not in approval:
        warnings.append("Approval required but not confirmed")

    deny_count = sum(
        1 for pd in receipt.policy_decisions
        if isinstance(pd, dict) and pd.get("decision") == "deny"
    )
    if deny_count:
        word = "policy decision" if deny_count == 1 else "policy decisions"
        warnings.append(f"{deny_count} denied {word}")

    for note in receipt.agent_notes:
        clean = (note or "").replace(_EM_DASH, "-").strip()
        if clean:
            warnings.append(clean)

    loop_summary = entry.get("osn_loop_summary")
    if isinstance(loop_summary, dict):
        for w in (loop_summary.get("warnings") or []):
            clean = (w or "").replace(_EM_DASH, "-").strip()
            if clean:
                warnings.append(clean)

        v_status = (loop_summary.get("verification_status") or "").lower()
        if v_status in ("skipped", "failed", "not_run"):
            warnings.append(f"Verification {v_status}")

    return _cap_list(warnings, _MAX_WARNINGS)


def _collect_evidence(receipt: ShardReceipt, osn_sections: list[str]) -> list[str]:
    evidence: list[str] = []

    safe_inspected = [p for p in receipt.inspected_files if _is_safe_path(p)]
    if safe_inspected:
        count = len(safe_inspected)
        word = "file" if count == 1 else "files"
        evidence.append(f"Inspected {count} {word}")

    if receipt.evidence_capsules:
        kinds: list[str] = []
        seen: set[str] = set()
        for cap in receipt.evidence_capsules:
            k = getattr(cap, "kind", None) or ""
            if k and k not in seen:
                seen.add(k)
                kinds.append(k)
        if kinds:
            evidence.append(f"Evidence recorded: {', '.join(kinds[:5])}")
        else:
            evidence.append("Evidence capsules recorded")

    allow_count = sum(
        1 for pd in receipt.policy_decisions
        if isinstance(pd, dict) and pd.get("decision") == "allow"
    )
    deny_count = sum(
        1 for pd in receipt.policy_decisions
        if isinstance(pd, dict) and pd.get("decision") == "deny"
    )
    total_pd = len(receipt.policy_decisions)
    if total_pd:
        parts = [f"{total_pd} policy decision(s)"]
        if deny_count:
            parts.append(f"{deny_count} denied")
        elif allow_count:
            parts.append(f"{allow_count} allowed")
        evidence.append(", ".join(parts))

    for section in osn_sections:
        evidence.append(f"{section} present")

    evidence.append("Shard receipt recorded")

    return _cap_list(evidence, _MAX_EVIDENCE)


def _collect_checks(receipt: ShardReceipt) -> list[str]:
    from openshard.history.shard_contract import agent_reported_suffix

    if receipt.check_results:
        return _cap_list(receipt.check_results, _MAX_CHECKS)
    if receipt.checks_display and receipt.checks_display not in ("Not run", "Not recorded"):
        return [receipt.checks_display + agent_reported_suffix(receipt)]
    return []


def _collect_inspected_files(receipt: ShardReceipt) -> list[str]:
    safe = [p for p in receipt.inspected_files if _is_safe_path(p)]
    return _cap_list(safe, _MAX_INSPECTED_FILES)


def _derive_recommended_next_step(entry: dict, receipt: ShardReceipt) -> str:
    """Cascade through metadata sources for the recommended next step.

    Order: osn_progress_memory.next_safe_step
        -> osn_retry_diagnosis.next_action
        -> osn_verification_contract missing_checks / skipped_reason
        -> reflection.recommended_next_move
        -> validation_contract.verification_commands (old key fallback)
        -> "Review the receipt before merging."
    """
    prog = entry.get("osn_progress_memory")
    if isinstance(prog, dict) and prog.get("next_safe_step"):
        return _cap_text(str(prog["next_safe_step"]))

    retry = entry.get("osn_retry_diagnosis")
    if isinstance(retry, dict) and retry.get("next_action"):
        return _cap_text(str(retry["next_action"]))

    verif = entry.get("osn_verification_contract")
    if isinstance(verif, dict):
        missing = verif.get("missing_checks")
        if missing and isinstance(missing, list) and missing:
            joined = ", ".join(str(c) for c in missing[:3])
            return _cap_text(f"Address missing checks: {joined}")
        skipped = verif.get("skipped_reason")
        if skipped:
            return _cap_text(f"Verification skipped: {skipped}")

    try:
        from openshard.reflection.reflector import build_run_reflection
        reflection = build_run_reflection(receipt)
        if reflection.recommended_next_move:
            return _cap_text(reflection.recommended_next_move)
    except Exception:
        pass

    vc = entry.get("validation_contract")
    if isinstance(vc, dict):
        cmds = vc.get("verification_commands") or []
        if cmds and isinstance(cmds, list) and cmds[0]:
            cmd = str(cmds[0]).replace(_EM_DASH, "-")[:_MAX_TEXT]
            return f"Run verification: {cmd}"

    return "Review the receipt before merging."


def _is_manual_review_required(entry: dict, receipt: ShardReceipt) -> bool:
    """Return True if any explicit or fallback source requires manual review."""
    # Explicit latest metadata first
    prog = entry.get("osn_progress_memory")
    if isinstance(prog, dict) and prog.get("blockers"):
        return True

    verif = entry.get("osn_verification_contract")
    if isinstance(verif, dict) and verif.get("manual_review_required"):
        return True

    retry = entry.get("osn_retry_diagnosis")
    if isinstance(retry, dict) and retry.get("manual_review_required"):
        return True

    # Approval gate
    if receipt.approval_required and not receipt.approval_granted:
        return True

    # Error class signals a failed run requiring review
    if receipt.error_class:
        return True

    # Policy denials
    if any(
        isinstance(pd, dict) and pd.get("decision") == "deny"
        for pd in receipt.policy_decisions
    ):
        return True

    # Fallbacks from older metadata shapes
    vc_result = entry.get("verification_contract_result")
    if isinstance(vc_result, dict):
        if (vc_result.get("overall_status") or "").lower() in ("failed", "skipped"):
            return True

    loop_summary = entry.get("osn_loop_summary")
    if isinstance(loop_summary, dict):
        v_status = (loop_summary.get("verification_status") or "").lower()
        if v_status in ("skipped", "failed", "not_run"):
            return True

    return False


def build_pr_comment_summary(entry: dict, receipt: ShardReceipt) -> PRCommentSummary:
    """Build a safe PRCommentSummary from a run entry and ShardReceipt.

    Uses only safe, structured sources. Does not read repo files, run git,
    call GitHub, or call any provider.
    """
    from openshard.history.shard import control_evidence_view
    from openshard.history.shard_contract import agent_reported_suffix, file_changes_unobservable

    entry = control_evidence_view(entry)
    osn_sections = _detect_osn_sections(entry)
    warnings = _collect_warnings(entry, receipt)
    evidence = _collect_evidence(receipt, osn_sections)
    checks = _collect_checks(receipt)
    inspected_files = _collect_inspected_files(receipt)
    manual_review = _is_manual_review_required(entry, receipt)
    next_step = _derive_recommended_next_step(entry, receipt)

    return PRCommentSummary(
        run_status=_cap_text((receipt.status + agent_reported_suffix(receipt)) if receipt.status else ""),
        risk=_cap_text(receipt.risk or ""),
        files_changed=receipt.files_changed or 0,
        files_observable=not file_changes_unobservable(receipt),
        inspected_files=inspected_files,
        checks=checks,
        warnings=warnings,
        evidence=evidence,
        osn_sections=osn_sections,
        recommended_next_step=_cap_text(next_step),
        manual_review_required=manual_review,
        raw_content_stored=False,
    )


def render_pr_comment(summary: PRCommentSummary) -> str:
    """Render a PRCommentSummary as GitHub markdown.

    Output is deterministic for the same input. No raw JSON, no raw command
    output, no raw file contents, no absolute paths, no em dashes.
    """
    lines: list[str] = []

    lines.append(f"## {summary.title}")
    lines.append("")
    lines.append(f"**Status:** {summary.run_status or 'unknown'}")
    lines.append(f"**Risk:** {summary.risk or 'unknown'}")
    if summary.files_observable:
        lines.append(f"**Files changed:** {summary.files_changed}")
    else:
        lines.append("**Files changed:** not observable (this integration exports no file changes)")
    lines.append(f"**Manual review:** {'yes' if summary.manual_review_required else 'no'}")

    if summary.osn_sections or summary.evidence:
        lines.append("")
        lines.append("### What OpenShard checked")
        lines.append("- Shard receipt recorded")
        for section in summary.osn_sections:
            lines.append(f"- {section} present")

    if summary.evidence:
        lines.append("")
        lines.append("### Evidence")
        for item in summary.evidence:
            lines.append(f"- {item}")

    if summary.checks:
        lines.append("")
        lines.append("### Validation")
        for check in summary.checks:
            lines.append(f"- {check}")

    if summary.warnings:
        lines.append("")
        lines.append("### Warnings")
        for warning in summary.warnings:
            lines.append(f"- {warning}")

    lines.append("")
    lines.append("### Recommended next step")
    lines.append(summary.recommended_next_step or "Review the receipt before merging.")

    lines.append("")
    lines.append("---")
    lines.append("Generated locally by OpenShard. Advisory only.")

    return "\n".join(lines)
