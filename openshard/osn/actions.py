"""Typed action contract for the iterative OSN agent loop.

One model turn returns a small, bounded list of *declared* actions. Every
action here is a proposal: the harness (``openshard.osn.agent_loop``) decides
whether it may happen, performs it in the isolated copy, and records what was
observed. Nothing in this module executes anything.

Fail closed: an unknown kind, a missing or wrongly typed field, an oversized
value or too many actions makes the whole turn unusable
(:class:`ActionParseError`); the caller may re-ask once, as it does for a
malformed write list today.

The ``intent`` field is a short audit note (what the action is for), never a
reasoning transcript. It is capped and stored as evidence; the model's wider
reply text is not kept.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Action kinds the contract knows. ``run_command`` is deliberately absent: a
# good first agent does not need arbitrary shell access, and verification is
# the loop's own command.
KIND_LIST_FILES = "list_files"
KIND_READ_FILE = "read_file"
KIND_SEARCH_REPO = "search_repo"
KIND_GET_DIFF = "get_diff"
KIND_WRITE_FILE = "write_file"
KIND_RUN_VERIFICATION = "run_verification"
KIND_FINISH = "finish"

READ_ONLY_KINDS: frozenset[str] = frozenset({KIND_LIST_FILES, KIND_READ_FILE, KIND_SEARCH_REPO, KIND_GET_DIFF})
MUTATING_KINDS: frozenset[str] = frozenset({KIND_WRITE_FILE})
CONTROL_KINDS: frozenset[str] = frozenset({KIND_RUN_VERIFICATION, KIND_FINISH})
ALL_KINDS: frozenset[str] = READ_ONLY_KINDS | MUTATING_KINDS | CONTROL_KINDS

MAX_ACTIONS_PER_TURN = 8
MAX_INTENT_CHARS = 200
MAX_TARGET_CHARS = 400
MAX_QUERY_CHARS = 200
MAX_CONTENT_BYTES = 200_000
MAX_NOTE_CHARS = 300

# Authority outcomes recorded on every action (the harness decides these).
DECISION_ALLOW = "allow"
DECISION_ASK = "ask"
DECISION_DENY = "deny"
DECISION_INVALID = "invalid"  # rejected by the harness at execution time (unsafe path, cap reached)
DECISION_NOT_APPLICABLE = "not_applicable"  # control actions carry no path policy

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)


class ActionParseError(ValueError):
    """The model reply was not a usable action list."""


@dataclass(frozen=True)
class AgentAction:
    """A declared action. ``target`` is a repo-relative path or a search query."""

    kind: str
    target: str = ""
    content: str | None = None  # write_file only; never stored in a Receipt
    intent: str = ""
    args: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.kind in READ_ONLY_KINDS


@dataclass
class TurnResult:
    """What one model turn declared, plus the short note it closed with."""

    actions: list[AgentAction]
    note: str = ""
    # A legacy ``{"writes": [...]}`` reply, accepted as writes + finish.
    legacy_writes: bool = False
    # The planner role's structured plan, when the reply carried one (raw; bounded by the caller).
    plan: dict[str, Any] | None = None
    # Exploration questions the planner asks to have answered in parallel (bounded here).
    explore: list[dict[str, Any]] = field(default_factory=list)
    # An explorer's compact answer: findings and the repo-relative paths they rest on (bounded here).
    findings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _clean_text(value: Any, cap: int) -> str:
    """A bounded single-line string without control characters; '' for non-strings."""
    if not isinstance(value, str):
        return ""
    out = "".join(ch if unicodedata.category(ch) != "Cc" else " " for ch in value)
    out = " ".join(out.split())
    return out[:cap]


def _require_str(obj: dict, key: str, cap: int, *, what: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ActionParseError(f"{what} needs a non-empty string '{key}'")
    if len(value) > cap:
        raise ActionParseError(f"{what} '{key}' is too long (>{cap} chars)")
    return value


def _parse_one(raw: Any, index: int) -> AgentAction:
    what = f"action {index + 1}"
    if not isinstance(raw, dict):
        raise ActionParseError(f"{what} must be an object")
    kind = raw.get("kind", raw.get("action", raw.get("tool")))
    if not isinstance(kind, str) or kind not in ALL_KINDS:
        raise ActionParseError(f"{what} has an unknown kind {kind!r}")
    intent = _clean_text(raw.get("intent", raw.get("reason", "")), MAX_INTENT_CHARS)
    if kind == KIND_LIST_FILES:
        subdir = raw.get("path", raw.get("subdir", "."))
        if subdir is None or subdir == "":
            subdir = "."
        if not isinstance(subdir, str) or len(subdir) > MAX_TARGET_CHARS:
            raise ActionParseError(f"{what} 'path' must be a short string")
        return AgentAction(kind, subdir, None, intent)
    if kind == KIND_READ_FILE:
        path = _require_str(raw, "path", MAX_TARGET_CHARS, what=what)
        return AgentAction(kind, path, None, intent)
    if kind == KIND_SEARCH_REPO:
        query = _require_str(raw, "query", MAX_QUERY_CHARS, what=what)
        args: dict[str, Any] = {}
        mm = raw.get("max_matches")
        if isinstance(mm, int) and not isinstance(mm, bool) and 1 <= mm <= 200:
            args["max_matches"] = mm
        return AgentAction(kind, query, None, intent, args)
    if kind == KIND_GET_DIFF:
        diff_path = raw.get("path")
        if diff_path is not None and (not isinstance(diff_path, str) or len(diff_path) > MAX_TARGET_CHARS):
            raise ActionParseError(f"{what} 'path' must be a short string")
        return AgentAction(kind, diff_path if isinstance(diff_path, str) else "", None, intent)
    if kind == KIND_WRITE_FILE:
        path = _require_str(raw, "path", MAX_TARGET_CHARS, what=what)
        content = raw.get("content")
        if not isinstance(content, str):
            raise ActionParseError(f"{what} needs string 'content' (the complete new file)")
        if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
            raise ActionParseError(f"{what} content too large")
        return AgentAction(kind, path, content, intent)
    if kind == KIND_RUN_VERIFICATION:
        return AgentAction(kind, "", None, intent)
    # finish
    return AgentAction(kind, "", None, intent or _clean_text(raw.get("summary", ""), MAX_INTENT_CHARS))


def parse_turn(content: str) -> TurnResult:
    """Parse one model reply into a bounded :class:`TurnResult`. Raises :class:`ActionParseError`.

    Accepted shapes::

        {"actions": [{"kind": "read_file", "path": "a.py", "intent": "..."}, ...], "note": "..."}
        {"writes": [{"path": "a.py", "content": "..."}]}      # legacy one-shot reply

    The legacy shape becomes write actions followed by ``finish``.
    """
    text = (content or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionParseError("reply is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ActionParseError("reply must be a JSON object")
    note = _clean_text(data.get("note", ""), MAX_NOTE_CHARS)
    raw_actions = data.get("actions")
    legacy = False
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else None
    explore = _parse_explore(data.get("explore"))
    findings = _parse_text_list(data.get("findings"), cap=MAX_FINDINGS, item_cap=MAX_FINDING_CHARS)
    sources = [s for s in _parse_text_list(data.get("sources"), cap=MAX_SOURCES, item_cap=MAX_TARGET_CHARS)
               if _safe_display_path(s)]
    if raw_actions is None and (findings or sources):
        raw_actions = [{"kind": KIND_FINISH}]  # an explorer's answer alone ends its turns
    if raw_actions is None and isinstance(data.get("writes"), list):
        legacy = True
        raw_actions = [
            {"kind": KIND_WRITE_FILE, **w} if isinstance(w, dict) else w for w in data["writes"]
        ] + [{"kind": KIND_FINISH}]
    if raw_actions is None and plan is not None:
        raw_actions = [{"kind": KIND_FINISH}]  # a plan alone ends the planner's turns
    if not isinstance(raw_actions, list):
        raise ActionParseError("reply has no 'actions' list")
    if not raw_actions:
        raise ActionParseError("'actions' is empty; use a finish action to stop")
    if len(raw_actions) > MAX_ACTIONS_PER_TURN + 1:  # +1: a trailing finish is free
        raise ActionParseError(f"too many actions in one turn (>{MAX_ACTIONS_PER_TURN})")
    actions = [_parse_one(raw, i) for i, raw in enumerate(raw_actions)]
    non_finish = [a for a in actions if a.kind != KIND_FINISH]
    if len(non_finish) > MAX_ACTIONS_PER_TURN:
        raise ActionParseError(f"too many actions in one turn (>{MAX_ACTIONS_PER_TURN})")
    # Anything after a finish is ignored by the harness; keep the list honest.
    out: list[AgentAction] = []
    for a in actions:
        out.append(a)
        if a.kind == KIND_FINISH:
            break
    return TurnResult(out, note, legacy, plan, explore, findings, sources)


MAX_EXPLORE_QUESTIONS = 3
MAX_QUESTION_CHARS = 200
MAX_PATH_HINTS = 5
MAX_FINDINGS = 8
MAX_FINDING_CHARS = 240
MAX_SOURCES = 8


def _safe_display_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    if norm.startswith(("/", "~")) or ":" in norm or ".." in norm.split("/"):
        return False
    return not any(unicodedata.category(ch) == "Cc" for ch in path)


def _parse_text_list(values: Any, *, cap: int, item_cap: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out = [_clean_text(v, item_cap) for v in values if isinstance(v, str) and v.strip()]
    return [v for v in out if v][:cap]


def _parse_explore(raw: Any) -> list[dict[str, Any]]:
    """Bounded exploration questions: ``[{"question": str, "paths_hint": [repo-relative paths]}]``."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            continue
        question = _clean_text(item.get("question", ""), MAX_QUESTION_CHARS)
        if not question:
            continue
        hints = [h.replace("\\", "/")[:MAX_TARGET_CHARS]
                 for h in _parse_text_list(item.get("paths_hint"), cap=MAX_PATH_HINTS, item_cap=MAX_TARGET_CHARS)
                 if _safe_display_path(h)]
        out.append({"question": question, "paths_hint": hints})
        if len(out) >= MAX_EXPLORE_QUESTIONS:
            break
    return out


@dataclass
class ActionRecord:
    """What the harness observed about one declared action. Stored in the Receipt.

    Never carries file contents, tool output, prompts or absolute paths. The
    ``target`` is the display form of a repo-relative path (unsafe inputs are
    masked) or a short search query.
    """

    index: int
    turn: int
    kind: str
    target: str
    intent: str
    role: str
    model: str | None  # the model the harness asked for the turn that declared this action
    decision: str  # allow | ask | deny | invalid | not_applicable
    decision_source: str | None = None
    decision_reason: str | None = None
    approval_granted: bool | None = None
    executed: bool = False
    ok: bool | None = None  # outcome of the executed tool; None when not executed
    error_class: str | None = None  # short, static classification only
    result: dict[str, Any] = field(default_factory=dict)  # counts/hashes only
    started_at: str | None = None
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "turn": self.turn,
            "kind": self.kind,
            "target": self.target,
            "intent": self.intent,
            "role": self.role,
            "model": self.model,
            "decision": self.decision,
            "decision_source": self.decision_source,
            "decision_reason": self.decision_reason,
            "approval_granted": self.approval_granted,
            "executed": self.executed,
            "ok": self.ok,
            "error_class": self.error_class,
            "result": dict(self.result),
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }


def summarize_actions(records: list[ActionRecord]) -> dict[str, int]:
    """Counts the Receipt can show without any path: how the agent spent its turns."""
    out = {
        "actions": len(records),
        "reads": 0,
        "searches": 0,
        "listings": 0,
        "diffs": 0,
        "writes_proposed": 0,
        "writes_applied": 0,
        "writes_blocked": 0,
        "verifications": 0,
        "invalid": 0,
    }
    for r in records:
        if r.kind == KIND_READ_FILE:
            out["reads"] += 1
        elif r.kind == KIND_SEARCH_REPO:
            out["searches"] += 1
        elif r.kind == KIND_LIST_FILES:
            out["listings"] += 1
        elif r.kind == KIND_GET_DIFF:
            out["diffs"] += 1
        elif r.kind == KIND_WRITE_FILE:
            out["writes_proposed"] += 1
            if r.executed and r.ok:
                out["writes_applied"] += 1
            elif r.decision in (DECISION_DENY, DECISION_INVALID) or (
                r.decision == DECISION_ASK and not r.approval_granted
            ):
                out["writes_blocked"] += 1
        elif r.kind == KIND_RUN_VERIFICATION and r.executed:
            out["verifications"] += 1
        if r.decision == DECISION_INVALID:
            out["invalid"] += 1
    return out


__all__ = [
    "ALL_KINDS",
    "CONTROL_KINDS",
    "DECISION_ALLOW",
    "DECISION_ASK",
    "DECISION_DENY",
    "DECISION_INVALID",
    "DECISION_NOT_APPLICABLE",
    "KIND_FINISH",
    "KIND_GET_DIFF",
    "KIND_LIST_FILES",
    "KIND_READ_FILE",
    "KIND_RUN_VERIFICATION",
    "KIND_SEARCH_REPO",
    "KIND_WRITE_FILE",
    "MAX_ACTIONS_PER_TURN",
    "MAX_CONTENT_BYTES",
    "MAX_INTENT_CHARS",
    "MUTATING_KINDS",
    "READ_ONLY_KINDS",
    "SCHEMA_VERSION",
    "ActionParseError",
    "ActionRecord",
    "AgentAction",
    "TurnResult",
    "parse_turn",
    "summarize_actions",
]
