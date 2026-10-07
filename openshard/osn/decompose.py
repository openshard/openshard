"""Typed task decomposition for parallel OSN workers.

The planner may propose that a task contains genuinely independent work:
a list of *subtasks*, each with an objective, the paths it may write, its
dependencies and what its verification must show. The proposal is
model-declared. This module turns it into a typed, bounded contract and
*validates* it; the harness grants authority, never the model:

* at most ``MAX_SUBTASKS`` subtasks, each with a non-empty objective and at
  least one safe, repo-relative write scope (a path or a glob);
* dependencies name known subtasks and form no cycle;
* two subtasks that may run in parallel must have disjoint write scopes
  (a scope overlaps another when one is the other or lies under it);
* anything else makes the decomposition invalid, with the reasons recorded,
  and the run falls back to a single executor.

A valid decomposition is still only a *proposal* for parallel work; the
topology decision (``openshard.osn.topology``) decides whether to use it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from openshard.osn.actions import _clean_text

MAX_SUBTASKS = 3
MAX_SCOPES = 6
MAX_DEPENDENCIES = 3
MAX_OBJECTIVE_CHARS = 300
MAX_ITEM_CHARS = 160
MAX_ITEMS = 6
MAX_ID_CHARS = 24

_ID_RE = re.compile(r"^[A-Za-z][\w-]{0,23}$")

REASON_TOO_MANY = "too_many_subtasks"
REASON_NO_OBJECTIVE = "subtask_without_objective"
REASON_NO_SCOPE = "subtask_without_write_scope"
REASON_UNSAFE_SCOPE = "unsafe_write_scope"
REASON_DUPLICATE_ID = "duplicate_subtask_id"
REASON_UNKNOWN_DEPENDENCY = "unknown_dependency"
REASON_DEPENDENCY_CYCLE = "dependency_cycle"
REASON_OVERLAPPING_SCOPES = "overlapping_parallel_scopes"
REASON_SINGLE_SUBTASK = "single_subtask"
REASON_NONE_PARALLEL = "no_parallel_safe_subtask"

_CAPABILITIES = frozenset({"routine_coding", "deep_reasoning", "fast_control", "long_context"})


@dataclass(frozen=True)
class Subtask:
    id: str
    objective: str
    allowed_write_paths: tuple[str, ...]  # repo-relative paths or globs; the worker's whole write authority
    likely_scope: tuple[str, ...] = ()  # paths the worker will probably read (advisory)
    dependencies: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    expected_output: str = ""
    verification_criteria: tuple[str, ...] = ()
    parallel_safe: bool = True
    required: bool = True  # a failed required subtask cannot be dropped silently
    preferred_capability: str | None = None  # a requirement class hint for routing

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "allowed_write_paths": list(self.allowed_write_paths),
            "likely_scope": list(self.likely_scope),
            "dependencies": list(self.dependencies),
            "required_evidence": list(self.required_evidence),
            "expected_output": self.expected_output,
            "verification_criteria": list(self.verification_criteria),
            "parallel_safe": self.parallel_safe,
            "required": self.required,
            "preferred_capability": self.preferred_capability,
        }


@dataclass
class Decomposition:
    subtasks: list[Subtask]
    valid: bool
    reasons: list[str] = field(default_factory=list)  # why it is not usable for parallel work
    source: str = "planner"

    @property
    def parallel_subtasks(self) -> list[Subtask]:
        return [s for s in self.subtasks if s.parallel_safe]

    def to_record(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "valid": self.valid,
            "reasons": list(self.reasons),
            "subtasks": [s.to_record() for s in self.subtasks],
        }


def safe_scope(pattern: str) -> bool:
    """A repo-relative path or glob: no absolute form, no traversal, no drive, no control characters."""
    if not isinstance(pattern, str) or not pattern.strip():
        return False
    norm = pattern.replace("\\", "/").strip()
    if norm.startswith(("/", "~")) or ":" in norm or ".." in norm.split("/"):
        return False
    if any(unicodedata.category(ch) == "Cc" for ch in norm):
        return False
    return True


def _scope_prefix(pattern: str) -> str:
    """The literal directory/file prefix of a scope: ``src/api/**`` -> ``src/api``, ``tests/*.py`` -> ``tests``."""
    norm = pattern.replace("\\", "/").strip().strip("/")
    parts = norm.split("/")
    literal: list[str] = []
    for part in parts:
        if any(ch in part for ch in "*?["):
            break
        literal.append(part)
    return "/".join(literal)


def scopes_overlap(a: str, b: str) -> bool:
    """True when the two write scopes could name the same file."""
    pa, pb = _scope_prefix(a), _scope_prefix(b)
    if pa == pb:
        return True
    if pa == "" or pb == "":
        return True  # a repository-wide glob overlaps everything
    return pa.startswith(pb + "/") or pb.startswith(pa + "/")


def _clean_list(values: Any, *, cap: int, item_cap: int) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    out = [_clean_text(v, item_cap) for v in values if isinstance(v, str) and v.strip()]
    return tuple(v for v in out if v)[:cap]


def parse_subtasks(raw: Any) -> list[Subtask]:
    """Bounded subtasks from the planner's ``subtasks`` list. Malformed entries are dropped, never guessed."""
    if not isinstance(raw, list):
        return []
    out: list[Subtask] = []
    for i, item in enumerate(raw[: MAX_SUBTASKS + 1]):
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        sid = sid.strip()[:MAX_ID_CHARS] if isinstance(sid, str) and _ID_RE.match(sid.strip()[:MAX_ID_CHARS] or "x") else f"subtask-{i + 1}"
        scopes = tuple(
            p.replace("\\", "/").strip()
            for p in _clean_list(item.get("allowed_write_paths", item.get("write_paths")), cap=MAX_SCOPES, item_cap=400)
            if safe_scope(p)
        )
        cap = item.get("preferred_capability")
        parallel = item.get("parallel_safe")
        required = item.get("required")
        out.append(Subtask(
            id=sid,
            objective=_clean_text(item.get("objective", ""), MAX_OBJECTIVE_CHARS),
            allowed_write_paths=scopes,
            likely_scope=tuple(p for p in _clean_list(item.get("likely_scope"), cap=MAX_ITEMS, item_cap=400) if safe_scope(p)),
            dependencies=_clean_list(item.get("dependencies"), cap=MAX_DEPENDENCIES, item_cap=MAX_ID_CHARS),
            required_evidence=_clean_list(item.get("required_evidence"), cap=MAX_ITEMS, item_cap=MAX_ITEM_CHARS),
            expected_output=_clean_text(item.get("expected_output", ""), MAX_ITEM_CHARS),
            verification_criteria=_clean_list(item.get("verification_criteria"), cap=MAX_ITEMS, item_cap=MAX_ITEM_CHARS),
            parallel_safe=parallel if isinstance(parallel, bool) else True,
            required=required if isinstance(required, bool) else True,
            preferred_capability=cap if isinstance(cap, str) and cap in _CAPABILITIES else None,
        ))
    return out


def validate_decomposition(subtasks: list[Subtask]) -> Decomposition:
    """Decide whether *subtasks* may drive parallel workers; every refusal reason is recorded."""
    reasons: list[str] = []
    if len(subtasks) > MAX_SUBTASKS:
        reasons.append(REASON_TOO_MANY)
    if len(subtasks) < 2:
        reasons.append(REASON_SINGLE_SUBTASK)
    ids = [s.id for s in subtasks]
    if len(set(ids)) != len(ids):
        reasons.append(REASON_DUPLICATE_ID)
    known = set(ids)
    for s in subtasks:
        if not s.objective:
            reasons.append(REASON_NO_OBJECTIVE)
        if not s.allowed_write_paths:
            reasons.append(REASON_NO_SCOPE)
        for dep in s.dependencies:
            if dep not in known or dep == s.id:
                reasons.append(REASON_UNKNOWN_DEPENDENCY)
    if _has_cycle(subtasks):
        reasons.append(REASON_DEPENDENCY_CYCLE)
    parallel = [s for s in subtasks if s.parallel_safe and not s.dependencies]
    if len(parallel) < 2:
        reasons.append(REASON_NONE_PARALLEL)
    for i, a in enumerate(parallel):
        for b in parallel[i + 1:]:
            if any(scopes_overlap(x, y) for x in a.allowed_write_paths for y in b.allowed_write_paths):
                reasons.append(REASON_OVERLAPPING_SCOPES)
                break
    seen: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return Decomposition(list(subtasks[:MAX_SUBTASKS]), valid=not seen, reasons=seen)


def _has_cycle(subtasks: list[Subtask]) -> bool:
    graph = {s.id: set(s.dependencies) for s in subtasks}
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        st = state.get(node, 0)
        if st == 1:
            return True
        if st == 2:
            return False
        state[node] = 1
        for dep in graph.get(node, ()):
            if dep in graph and visit(dep):
                return True
        state[node] = 2
        return False

    return any(visit(n) for n in graph)


def decomposition_from_plan(plan: dict[str, Any] | None) -> Decomposition | None:
    """The planner's decomposition, parsed and validated, or None when the plan proposed none."""
    if not isinstance(plan, dict) or not isinstance(plan.get("subtasks"), list) or not plan["subtasks"]:
        return None
    return validate_decomposition(parse_subtasks(plan["subtasks"]))


__all__ = [
    "MAX_SUBTASKS",
    "Decomposition",
    "Subtask",
    "decomposition_from_plan",
    "parse_subtasks",
    "safe_scope",
    "scopes_overlap",
    "validate_decomposition",
]
