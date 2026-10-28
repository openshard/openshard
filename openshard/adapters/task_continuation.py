"""Receipts that continue a task carry no prompt text (docs/steering-capture.md, "Privacy").

Only the prompt that starts a task -- the first prompt of an agent session --
may become a Receipt's task excerpt and title. A Receipt that continues the
same task -- the next turn's Receipt (per-turn Receipts), or the segment a
resumed session opens -- takes nothing from its opening prompt: its task and
title are the task's first title plus the turn number (``"Revamp website hero
· turn 3"``), and what the person said is only in its steering facts. A task
that ended (a branch change, its pull request merged or closed) is not
continued: the next prompt starts a new task and may name it.

Stored on such a Receipt: ``capture.task_source: task_title_and_turn``,
``capture.task_base_title`` (the task's first title, already on the task's
first Receipt) and ``capture.task_turn`` (the turn this Receipt opened with;
absent when not known). Receipts sealed before this rule are left as they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openshard.history.task_title import (
    TASK_SOURCE_CONTINUED,
    continued_title,
    normalize_title_candidate,
)

_FIRST = "first_user_prompt_excerpt"


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def inherit(buf: dict, persisted: Mapping[str, Any]) -> None:
    """Mark a new segment of the same agent session as continuing *persisted*'s task. No-op for a new task."""
    if isinstance(buf.get("previous_task_ended"), str):
        return  # the previous task ended: this segment's first prompt starts a new one
    capture = persisted.get("capture")
    capture = capture if isinstance(capture, Mapping) else {}
    if capture.get("task_source") == TASK_SOURCE_CONTINUED:
        base = capture.get("task_base_title")
        before = _count(capture.get("task_turn"))
    else:
        base = persisted.get("task_title") if capture.get("task_source") == _FIRST else None
        # A Receipt from before this rule that was itself a resume: where its turns began is not known.
        before = None if capture.get("start_source") == "resume" else 1
    if buf.get("receipt_scope") == "turn":
        turn = _count(buf.get("turn_index"))
    else:
        prompts = _count(capture.get("prompt_count")) or 0
        turn = before + prompts if before is not None and prompts else None  # no prompts seen: not known
    buf["task_continued"] = {"base_title": normalize_title_candidate(base), "turn": turn or None}


def restore(buf: dict, capture: Mapping[str, Any]) -> None:
    """A lost staging buffer rebuilt from its own Receipt keeps being a continuation."""
    if capture.get("task_source") == TASK_SOURCE_CONTINUED:
        buf["task_continued"] = {"base_title": normalize_title_candidate(capture.get("task_base_title")),
                                 "turn": _count(capture.get("task_turn")) or None}


def continued(buf: Mapping[str, Any]) -> bool:
    return isinstance(buf.get("task_continued"), dict)


def stamp(entry: dict, buf: Mapping[str, Any]) -> None:
    """Title a continuing Receipt from the task's first title and the turn; no prompt text. No-op otherwise."""
    info = buf.get("task_continued")
    if not isinstance(info, dict):
        return
    title = continued_title(info.get("base_title"), info.get("turn"))
    entry["task"] = title
    entry["task_title"] = title
    capture = entry.setdefault("capture", {})
    capture["task_source"] = TASK_SOURCE_CONTINUED
    if isinstance(info.get("base_title"), str):
        capture["task_base_title"] = info["base_title"]
    if _count(info.get("turn")):
        capture["task_turn"] = info["turn"]


__all__ = ["continued", "inherit", "restore", "stamp"]
