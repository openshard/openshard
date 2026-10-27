"""The Receipt's steering block, read back: validated facts and derived autonomy (docs/steering-capture.md).

Capture (``adapters/steering.py``) stores ``capture.steering``: the steering
facts it observed (kind, intent, confidence, times, counts; never prompt
text), the turn ends it saw, and the signals this agent does not expose.
This module is the one reader. Everything here is derived from that stored,
sealed block, so it never adds a claim the record does not hold:

* ``human_inputs``: how many times a person stepped in, by kind. A follow-up
  the agent took from its queue was typed during the previous turn and is
  already counted there as an interjection, so it is not counted twice.
* ``longest_unsupervised_seconds``: the longest time the agent worked with no
  human input, inside turns whose end was observed; None when none was.
* ``interrupts``; ``permission_prompts`` asked / allowed / denied, each None
  when the agent does not expose it (never 0 for "not observed").
* ``steering_seconds``: a person's think time between a turn's end and their
  next prompt, each pause capped at 15 minutes (a longer one is time away).
* Local vs cloud and the permission mode are not repeated here: they are the
  Receipt's existing ``ran_on`` and ``runtime_configuration.permission_mode``.

Stability: this block is part of the sealed ``history --json`` projection,
which verification recomputes. The version 1 derivation is therefore frozen;
a different derivation needs a new block version (and keeps this one for
version 1 blocks), or every sealed Receipt carrying a block would read as
changed after sealing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

KINDS: tuple[str, ...] = (
    "initial_prompt", "follow_up", "interjection", "interrupt", "permission_decision", "manual_edit",
)
_INTENTS = frozenset({"correction", "redirect", "clarification", "approval", "continue", "new_task", "unknown"})
_AFTER = frozenset({"turn_end", "failed_check", "tool_call", "permission_prompt", "none", "unknown"})
_SOURCES = frozenset({"hook", "transcript", "git", "github"})
_DECISIONS = frozenset({"allow", "deny", "always_allow"})
_CONFIDENCES = frozenset({"high", "medium", "low"})
_MAX_EVENTS = 100
_MAX_SYNC_EVENTS = 50

# Where the steered session ran (docs/steering-capture.md, "Environment on every block").
ENV_LOCAL_TERMINAL = "local_terminal"
ENV_LOCAL_IDE = "local_ide"
ENV_CLOUD_AGENT = "cloud_agent"
ENV_CLOUD_WORKSPACE = "cloud_workspace"
ENV_WEB = "web"
ENV_CI = "ci"
ENVIRONMENTS: tuple[str, ...] = (
    ENV_LOCAL_TERMINAL, ENV_LOCAL_IDE, ENV_CLOUD_AGENT, ENV_CLOUD_WORKSPACE, ENV_WEB, ENV_CI,
)
# ``capture.agent_host`` apps (adapters/agent_env.py) that settle local terminal vs local IDE.
_IDE_APPS = frozenset({"vscode", "jetbrains"})
_TERMINAL_APPS = frozenset({
    "terminal", "iterm2", "macos_terminal", "wezterm", "ghostty", "warp", "tmux", "hyper", "kitty",
})
_RAN_ON_ENVIRONMENTS = {"cloud_agent": ENV_CLOUD_AGENT, "cloud_dev": ENV_CLOUD_WORKSPACE, "ci": ENV_CI}
_GITHUB_VIA = frozenset({"review_changes_requested", "review_approved", "review_commented", "agent_mention"})

# Keys an event may carry, by type; anything else is dropped on read.
_INT_KEYS = ("turn_index", "prompt_chars", "files", "lines_added", "lines_removed", "lines_unknown_files")
_NUM_KEYS = ("think_seconds",)
_BOOL_KEYS = ("from_queue", "think_capped", "incomplete")
_STR_KEYS = ("at", "since", "intent_rule")
# Local only: a manual edit's paths stay in runs.jsonl and are never synced.
_LOCAL_ONLY_KEYS = frozenset({"paths"})
# The GitHub-derived block's counts (``github/steering.py``).
_GITHUB_COUNT_KEYS = ("review_comments", "review_rerequests", "bot_comments_ignored")


def environment_from_host(host: object) -> tuple[str | None, str | None]:
    """``(environment, None)`` from a recorded ``capture.agent_host``, or ``(None, reason)`` when it does not say.

    ``cloud_agent:*`` is a hosted agent VM, ``cloud_dev:*`` a cloud workspace
    (Codespaces, Gitpod, Replicas), ``ci:*`` a CI runner. ``local`` needs the
    app to tell a terminal from an IDE; a headless SDK run, a desktop app or an
    unrecorded app leaves it unknown. ``web`` is never derived from a host: it
    is the GitHub-derived block's (the person steered on GitHub, where
    OpenShard had no runtime presence).
    """
    if not isinstance(host, str) or "|" not in host:
        return None, "where_it_ran_not_recorded"
    ran_on, app = host.split("|", 1)
    kind = ran_on.partition(":")[0]
    if kind in _RAN_ON_ENVIRONMENTS:
        return _RAN_ON_ENVIRONMENTS[kind], None
    if kind != "local":
        return None, "where_it_ran_not_recorded"
    if app in _IDE_APPS:
        return ENV_LOCAL_IDE, None
    if app in _TERMINAL_APPS:
        return ENV_LOCAL_TERMINAL, None
    return None, "local_app_not_recorded" if not app else "local_app_not_terminal_or_ide"


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else None


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _event(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or raw.get("kind") not in KINDS or _parse(raw.get("at")) is None:
        return None
    out: dict[str, Any] = {"kind": raw["kind"]}
    for key in _STR_KEYS:
        if isinstance(raw.get(key), str) and len(raw[key]) <= 80:
            out[key] = raw[key]
    for key in _INT_KEYS:
        if _count(raw.get(key)) is not None:
            out[key] = raw[key]
        elif key in raw and raw[key] is None and key in ("lines_added", "lines_removed"):
            out[key] = None  # recorded as unknown
    for key in _NUM_KEYS:
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    for key in _BOOL_KEYS:
        if raw.get(key) is True:
            out[key] = True
    if raw.get("intent") in _INTENTS:
        out["intent"] = raw["intent"]
        if raw.get("intent_confidence") in _CONFIDENCES:
            out["intent_confidence"] = raw["intent_confidence"]
    out["after"] = raw.get("after") if raw.get("after") in _AFTER else "unknown"
    out["source"] = raw.get("source") if raw.get("source") in _SOURCES else "hook"
    if raw.get("decision") in _DECISIONS:
        out["decision"] = raw["decision"]
    if raw.get("via") in _GITHUB_VIA:
        out["via"] = raw["via"]
    paths = raw.get("paths")
    if isinstance(paths, list):
        out["paths"] = [p for p in paths if isinstance(p, str)][:20]
    return out


def stored_block(capture: object) -> dict[str, Any] | None:
    """The validated ``capture.steering`` block; None for records without one (older Receipts)."""
    if not isinstance(capture, Mapping):
        return None
    raw = capture.get("steering")
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        return None
    events = [e for e in (_event(r) for r in raw.get("events") or []) if e is not None][:_MAX_EVENTS]
    ends = [t for t in raw.get("turn_ends") or [] if isinstance(t, Mapping) and _parse(t.get("at"))]
    not_available = raw.get("not_available")
    block: dict[str, Any] = {
        "version": 1,
        "intent_method": raw.get("intent_method") if isinstance(raw.get("intent_method"), str) else None,
        "events": sorted(events, key=lambda e: _parse(e["at"]) or _EPOCH),
        "events_dropped": _count(raw.get("events_dropped")) or 0,
        "turn_ends": [str(t["at"]) for t in ends],
        "not_available": {str(k): str(v)[:200] for k, v in not_available.items() if k in KINDS}
        if isinstance(not_available, Mapping) else {},
    }
    for key in ("permission_prompts_asked",):
        if _count(raw.get(key)) is not None:
            block[key] = raw[key]
    if isinstance(raw.get("log_not_read_reason"), str):
        block["log_not_read_reason"] = raw["log_not_read_reason"][:40]
    # Present only on blocks that recorded them: an older block reads back exactly as before.
    if raw.get("environment") in ENVIRONMENTS:
        block["environment"] = raw["environment"]
        if isinstance(raw.get("environment_source"), str):
            block["environment_source"] = raw["environment_source"][:40]
    elif isinstance(raw.get("environment_not_recorded_reason"), str):
        block["environment_not_recorded_reason"] = raw["environment_not_recorded_reason"][:40]
    github = raw.get("github")
    if isinstance(github, Mapping):
        facts: dict[str, Any] = {k: github[k] for k in _GITHUB_COUNT_KEYS if _count(github.get(k)) is not None}
        if isinstance(github.get("commit_after_review"), bool):
            facts["commit_after_review"] = github["commit_after_review"]
        for key in ("window_from", "window_to"):
            if _parse(github.get(key)) is not None:
                facts[key] = github[key]
        block["github"] = facts
    return block


def _counted(event: Mapping[str, Any]) -> bool:
    return not (event["kind"] == "follow_up" and event.get("from_queue"))


def _longest_unsupervised(events: list[dict], turn_ends: list[str]) -> float | None:
    """Longest gap between human inputs while the agent worked, over turns whose end was seen."""
    openers = [e for e in events if e["kind"] in ("initial_prompt", "follow_up")]
    ends = sorted({*turn_ends, *(e["at"] for e in events if e["kind"] == "interrupt")},
                  key=lambda s: _parse(s) or _EPOCH)
    inside = [e for e in events if e["kind"] in ("interjection", "permission_decision")]
    longest: float | None = None
    for i, opener in enumerate(openers):
        start = _parse(opener["at"])
        upper = _parse(openers[i + 1]["at"]) if i + 1 < len(openers) else None
        end = next((t for t in (_parse(s) for s in ends) if t is not None and start is not None and t >= start
                    and (upper is None or t <= upper)), None)
        if start is None or end is None:
            continue  # the turn's end was not observed: its stretch is unknown
        marks = [start] + sorted(t for t in (_parse(e["at"]) for e in inside) if t and start <= t <= end) + [end]
        gap = max((b - a).total_seconds() for a, b in zip(marks, marks[1:], strict=False))
        longest = gap if longest is None else max(longest, gap)
    return round(longest, 1) if longest is not None else None


def autonomy(block: Mapping[str, Any], capture: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derived autonomy facts for one Receipt (see the module docstring)."""
    events: list[dict] = list(block.get("events") or [])
    counted = [e for e in events if _counted(e)]
    by_kind: dict[str, int | None] = {kind: sum(1 for e in counted if e["kind"] == kind) for kind in KINDS}
    for kind in block.get("not_available") or {}:
        by_kind[kind] = None  # this agent does not expose it: unknown, not zero
    decisions = [e.get("decision") for e in events if e["kind"] == "permission_decision"]
    unavailable = block.get("not_available") or {}
    agent = (capture or {}).get("agent")
    hermes = agent == "hermes"
    claude = agent == "claude_code"
    permission = {
        "asked": block.get("permission_prompts_asked") if hermes else None,
        "allowed": sum(1 for d in decisions if d in ("allow", "always_allow")) if hermes else None,
        "denied": sum(1 for d in decisions if d == "deny")
        if (hermes or claude) and "permission_decision" not in unavailable else None,
    }
    think = [e["think_seconds"] for e in events if e["kind"] == "follow_up" and "think_seconds" in e]
    out: dict[str, Any] = {
        "human_inputs": sum(v for v in by_kind.values() if isinstance(v, int)),
        "human_inputs_by_kind": by_kind,
        "intents": _intent_counts(counted),
        "interrupts": by_kind.get("interrupt"),
        "permission_prompts": permission,
        "longest_unsupervised_seconds": _longest_unsupervised(events, list(block.get("turn_ends") or [])),
        "steering_seconds": round(sum(think), 1) if think else (0.0 if not any(
            e["kind"] == "follow_up" for e in counted) else None),
        "steering_pause_cap_seconds": 15 * 60,
    }
    return out


def _intent_counts(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if event["kind"] in ("follow_up", "interjection") and "intent" in event:
            counts[event["intent"]] = counts.get(event["intent"], 0) + 1
    return counts


def steering_evidence(entry: object) -> dict[str, Any] | None:
    """``recorded_evidence["steering"]``: the stored block plus derived autonomy, or None."""
    capture = entry.get("capture") if isinstance(entry, Mapping) else None
    block = stored_block(capture)
    if block is None:
        return None
    return {**block, "autonomy": autonomy(block, capture if isinstance(capture, Mapping) else None)}


def sync_block(evidence: object) -> dict[str, Any] | None:
    """The privacy-bounded block sync sends: no paths, at most 50 events."""
    if not isinstance(evidence, Mapping) or evidence.get("version") != 1:
        return None
    events = [{k: v for k, v in e.items() if k not in _LOCAL_ONLY_KEYS}
              for e in evidence.get("events") or [] if isinstance(e, Mapping)]
    out = {k: v for k, v in evidence.items() if k not in ("events", "turn_ends")}
    out["events"] = events[:_MAX_SYNC_EVENTS]
    if len(events) > _MAX_SYNC_EVENTS:
        out["events_dropped"] = int(out.get("events_dropped") or 0) + len(events) - _MAX_SYNC_EVENTS
    return out


_PLURAL = {
    "correction": ("correction", "corrections"), "redirect": ("redirect", "redirects"),
    "clarification": ("question", "questions"), "approval": ("approval", "approvals"),
    "continue": ("go-ahead", "go-aheads"), "new_task": ("new ask", "new asks"),
}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def summary_line(evidence: object) -> str | None:
    """``2 corrections · 1 interrupt · you stepped in 3 times`` for ``openshard last``; None without a block."""
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("autonomy"), Mapping):
        return None
    facts = evidence["autonomy"]
    parts: list[str] = []
    intents = facts.get("intents") or {}
    for intent in ("correction", "redirect", "clarification", "new_task", "approval", "continue"):
        if intents.get(intent):
            parts.append(_n(intents[intent], *_PLURAL[intent]))
    by_kind = facts.get("human_inputs_by_kind") or {}
    if by_kind.get("interrupt"):
        parts.append(_n(by_kind["interrupt"], "interrupt", "interrupts"))
    denied = (facts.get("permission_prompts") or {}).get("denied")
    if denied:
        parts.append(_n(denied, "permission denied", "permissions denied"))
    edits = [e for e in evidence.get("events") or [] if isinstance(e, Mapping) and e.get("kind") == "manual_edit"]
    files = sum(int(e.get("files") or 0) for e in edits)
    if files:
        parts.append(f"{_n(files, 'file', 'files')} changed between turns, not by the agent")
    stepped = sum(v for k, v in by_kind.items() if k != "initial_prompt" and isinstance(v, int))
    parts.append(f"you stepped in {_n(stepped, 'time', 'times')}" if stepped else "no steering observed")
    return " · ".join(parts)


__all__ = [
    "ENVIRONMENTS", "ENV_CI", "ENV_CLOUD_AGENT", "ENV_CLOUD_WORKSPACE", "ENV_LOCAL_IDE", "ENV_LOCAL_TERMINAL",
    "ENV_WEB", "KINDS", "autonomy", "environment_from_host", "steering_evidence", "stored_block", "summary_line",
    "sync_block",
]
