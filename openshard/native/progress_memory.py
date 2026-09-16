"""OSN Progress Memory v1.

Safe, bounded post-loop progress snapshot built from recorded OSN signals.
No shell execution, no provider calls, no raw output, no raw file contents,
no raw prompts, no absolute paths, no chain-of-thought, no em dash characters.

Built in pipeline.py after osn_observation, osn_loop_summary,
osn_verification_contract, and osn_retry_diagnosis are all finalized.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openshard.safety.sanitize import is_absolute_path as _is_absolute_path

_PM_MAX_SUMMARY_CHARS: int = 240
_PM_MAX_COMPLETED: int = 5
_PM_MAX_ITEM_CHARS: int = 120
_PM_MAX_CURRENT_FOCUS: int = 5
_PM_MAX_RELEVANT_FILES: int = 8
_PM_MAX_UNRESOLVED: int = 5
_PM_MAX_BLOCKERS: int = 5
_PM_MAX_NEXT_SAFE_STEP_CHARS: int = 160
_VALID_CONFIDENCE: frozenset[str] = frozenset({"low", "medium", "high", "unknown"})

_BLOCKING_RETRY_STATUSES: frozenset[str] = frozenset({"blocked", "exhausted"})


@dataclass
class OSNProgressMemory:
    """Safe run-local progress snapshot for OSN native loop runs.

    All list fields are capped and all text fields are truncated in __post_init__.
    raw_content_stored is always False - enforced unconditionally.
    No em dash characters.
    """

    enabled: bool = False
    source: str = "osn_progress_memory_v1"
    summary: str = ""
    completed: list[str] = field(default_factory=list)
    current_focus: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    unresolved_items: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_safe_step: str = ""
    confidence: str = "low"
    raw_content_stored: bool = False

    def __post_init__(self) -> None:
        self.raw_content_stored = False
        self.summary = self.summary[:_PM_MAX_SUMMARY_CHARS]
        self.completed = [s[:_PM_MAX_ITEM_CHARS] for s in self.completed[:_PM_MAX_COMPLETED]]
        self.current_focus = self.current_focus[:_PM_MAX_CURRENT_FOCUS]
        self.relevant_files = self.relevant_files[:_PM_MAX_RELEVANT_FILES]
        self.unresolved_items = self.unresolved_items[:_PM_MAX_UNRESOLVED]
        self.blockers = self.blockers[:_PM_MAX_BLOCKERS]
        self.next_safe_step = self.next_safe_step[:_PM_MAX_NEXT_SAFE_STEP_CHARS]
        if self.confidence not in _VALID_CONFIDENCE:
            self.confidence = "unknown"


def _is_unsafe_path(p: str) -> bool:
    """Return True for paths that must not be stored."""
    if _is_absolute_path(p):
        return True
    if ".codegraph" in p:
        return True
    return False


def build_osn_progress_memory(
    *,
    osn_observation: Any | None = None,
    osn_loop_summary: Any | None = None,
    osn_verification_contract: Any | None = None,
    osn_retry_diagnosis: Any | None = None,
) -> OSNProgressMemory:
    """Build a deterministic progress snapshot from recorded OSN signals.

    Does not execute shell commands.
    Does not call providers or models.
    Does not read file contents.
    Does not store raw prompts, raw outputs, or raw file contents.
    Does not store absolute paths.
    Does not invent facts not present in the inputs.
    """
    obs_enabled = osn_observation is not None and getattr(osn_observation, "enabled", False)
    loop_enabled = osn_loop_summary is not None and getattr(osn_loop_summary, "enabled", False)

    if not obs_enabled and not loop_enabled:
        return OSNProgressMemory(enabled=False, confidence="unknown")

    # -- current_focus from stack signals --
    current_focus: list[str] = []
    if osn_observation is not None:
        raw_signals = getattr(osn_observation, "stack_signals", []) or []
        current_focus = list(raw_signals)[:_PM_MAX_CURRENT_FOCUS]

    # -- relevant_files from candidate_files, filtered --
    relevant_files: list[str] = []
    if osn_observation is not None:
        raw_candidates = getattr(osn_observation, "candidate_files", []) or []
        for cf in raw_candidates:
            if isinstance(cf, str) and not _is_unsafe_path(cf):
                relevant_files.append(cf)
            if len(relevant_files) >= _PM_MAX_RELEVANT_FILES:
                break

    # -- completed from passed loop steps --
    completed: list[str] = []
    if osn_loop_summary is not None:
        steps = getattr(osn_loop_summary, "steps", []) or []
        seen: set[str] = set()
        for step in steps:
            st = getattr(step, "status", "") or _safe_dict_get(step, "status", "")
            name = getattr(step, "step_name", "") or _safe_dict_get(step, "step_name", "")
            if st == "passed" and name and name not in seen:
                seen.add(name)
                completed.append(name[:_PM_MAX_ITEM_CHARS])
            if len(completed) >= _PM_MAX_COMPLETED:
                break

    # -- blockers from blocked steps + retry/verification signals --
    blockers: list[str] = []
    if osn_loop_summary is not None:
        steps = getattr(osn_loop_summary, "steps", []) or []
        for step in steps:
            st = getattr(step, "status", "") or _safe_dict_get(step, "status", "")
            name = getattr(step, "step_name", "") or _safe_dict_get(step, "step_name", "")
            if st == "blocked" and name:
                entry = f"step blocked: {name}"[:_PM_MAX_ITEM_CHARS]
                if entry not in blockers:
                    blockers.append(entry)
            if len(blockers) >= _PM_MAX_BLOCKERS:
                break

    if osn_retry_diagnosis is not None:
        rd_status = getattr(osn_retry_diagnosis, "status", "") or ""
        if rd_status in _BLOCKING_RETRY_STATUSES:
            entry = f"retry: {rd_status}"[:_PM_MAX_ITEM_CHARS]
            if entry not in blockers and len(blockers) < _PM_MAX_BLOCKERS:
                blockers.append(entry)
        if getattr(osn_retry_diagnosis, "manual_review_required", False):
            entry = "manual review required"
            if entry not in blockers and len(blockers) < _PM_MAX_BLOCKERS:
                blockers.append(entry)

    if osn_verification_contract is not None:
        if getattr(osn_verification_contract, "manual_review_required", False):
            entry = "manual review required"
            if entry not in blockers and len(blockers) < _PM_MAX_BLOCKERS:
                blockers.append(entry)

    # -- unresolved_items from missing checks --
    unresolved_items: list[str] = []
    if osn_verification_contract is not None:
        raw_missing = getattr(osn_verification_contract, "missing_checks", []) or []
        for mc in raw_missing:
            if isinstance(mc, str) and mc:
                unresolved_items.append(mc[:_PM_MAX_ITEM_CHARS])
            if len(unresolved_items) >= _PM_MAX_UNRESOLVED:
                break

    # -- next_safe_step: retry diagnosis next_action is primary --
    next_safe_step = ""
    if osn_retry_diagnosis is not None:
        next_safe_step = getattr(osn_retry_diagnosis, "next_action", "") or ""
    if not next_safe_step and osn_observation is not None:
        checks = getattr(osn_observation, "suggested_checks", []) or []
        if checks:
            next_safe_step = str(checks[0])
    next_safe_step = next_safe_step[:_PM_MAX_NEXT_SAFE_STEP_CHARS]

    # -- confidence from finalized verification signals --
    vc_status = (
        getattr(osn_verification_contract, "status", "") or ""
        if osn_verification_contract is not None
        else ""
    )
    v_attempted = bool(getattr(osn_loop_summary, "verification_attempted", False)) if osn_loop_summary is not None else False
    v_passed = getattr(osn_loop_summary, "verification_passed", None) if osn_loop_summary is not None else None

    if vc_status == "passed":
        confidence = "high"
    elif v_attempted and v_passed is not True:
        confidence = "medium"
    elif obs_enabled:
        confidence = "low"
    else:
        confidence = "unknown"

    # -- summary: deterministic, no raw content --
    focus_str = ",".join(current_focus[:2]) if current_focus else "unknown"
    summary = f"stack={focus_str}, {len(completed)} steps done, confidence={confidence}"
    summary = summary[:_PM_MAX_SUMMARY_CHARS]

    return OSNProgressMemory(
        enabled=True,
        summary=summary,
        completed=completed,
        current_focus=current_focus,
        relevant_files=relevant_files,
        unresolved_items=unresolved_items,
        blockers=blockers,
        next_safe_step=next_safe_step,
        confidence=confidence,
    )


def _safe_dict_get(obj: Any, key: str, default: str) -> str:
    """Get a value from a dict-like object safely."""
    if isinstance(obj, dict):
        return obj.get(key, default) or default
    return default


def render_osn_progress_context(memory: OSNProgressMemory | None) -> str:
    """Render a prompt-safe OSN progress block for context injection.

    Returns empty string when memory is absent or disabled.
    No raw JSON, no absolute paths, no em dash, no chain-of-thought.
    """
    if memory is None or not memory.enabled:
        return ""
    lines = ["[osn progress]"]
    if memory.summary:
        lines.append(f"summary: {memory.summary}")
    if memory.current_focus:
        lines.append(f"focus: {', '.join(memory.current_focus)}")
    if memory.relevant_files:
        lines.append(f"files: {', '.join(memory.relevant_files)}")
    if memory.unresolved_items:
        lines.append(f"unresolved: {', '.join(memory.unresolved_items[:3])}")
    if memory.next_safe_step:
        lines.append(f"next: {memory.next_safe_step}")
    return "\n".join(lines)
