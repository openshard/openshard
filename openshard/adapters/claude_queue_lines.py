"""Decode a capture-queue file and decide which Receipt each corrupt line counts against.

The capture service replays a queue file line by line (see
``claude_capture_service.CaptureRecorder._replay_file``). With per-turn
Receipts a ``Stop`` line seals its turn, and a sealed Receipt is final, so a
corrupt line has to be counted against the turn it could belong to *before*
that ``Stop`` is applied. The whole file is therefore decoded first and the
losses are planned from the decoded lines:

* a corrupt line inside a turn (after a ``UserPromptSubmit``, or with no
  earlier ``Stop`` in the file) counts against the turn the next ``Stop``
  seals, reported just before that ``Stop`` is applied;
* a corrupt line after a ``Stop`` and before the next ``UserPromptSubmit``
  could be a late event of the sealed turn or the start of the next one.
  Unknown stays unknown: it counts against the sealed turn (before its
  ``Stop``) and, when that ``Stop`` did close the Receipt, again against
  the next segment;
* a corrupt line with no later ``Stop`` is reported once the file has
  settled, onto the session's open record (unchanged behaviour).

A session-scoped Receipt is not closed by ``Stop``, so the second count is
skipped there and the loss is counted once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openshard.adapters.claude_hooks import (
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    ReducedHookPayload,
    StatusPayload,
)


@dataclass
class QueuedLine:
    """One non-empty queue line: decoded, or the reason it is unusable (``corrupt``)."""

    lineno: int
    raw: str
    kind: str | None = None
    payload: Any = None
    dedup_id: str | None = None
    at: str | None = None
    corrupt: str | None = None


@dataclass
class LossPlan:
    """Where each corrupt line's loss is reported; values are queue line numbers.

    ``before[i]``: report before applying the ``Stop`` at index *i*.
    ``carry[i]``: report again, before the next ``Stop`` or once the file
    settles, only when the ``Stop`` at index *i* closed its Receipt.
    ``tail``: no later ``Stop``; report once the file settles.
    """

    before: dict[int, list[int]] = field(default_factory=dict)
    carry: dict[int, list[int]] = field(default_factory=dict)
    tail: list[int] = field(default_factory=list)


def decode_line(lineno: int, raw: str) -> QueuedLine:
    item = QueuedLine(lineno=lineno, raw=raw)
    try:
        line = json.loads(raw)
    except ValueError:
        item.corrupt = "not valid JSON"
        return item
    if not isinstance(line, dict) or not isinstance(line.get("data"), dict):
        item.corrupt = "not a queue line object"
        return item
    item.dedup_id = line.get("id") if isinstance(line.get("id"), str) else None
    item.at = line.get("at") if isinstance(line.get("at"), str) else None
    item.kind = line.get("kind") if isinstance(line.get("kind"), str) else None
    if item.kind == "hook":
        item.payload = ReducedHookPayload.from_dict(line["data"])
        item.corrupt = None if item.payload is not None else "unusable hook payload"
    elif item.kind == "status":
        item.payload = StatusPayload.from_dict(line["data"])
        item.corrupt = None if item.payload is not None else "unusable status payload"
    else:
        item.corrupt = "unknown line kind"
    return item


def decode_queue_text(text: str) -> list[QueuedLine]:
    """Every non-empty line of *text*, in order, decoded or marked corrupt."""
    return [decode_line(n, raw.strip()) for n, raw in enumerate(text.splitlines(), start=1) if raw.strip()]


def is_stop(item: QueuedLine) -> bool:
    return item.kind == "hook" and getattr(item.payload, "event", None) == EVENT_STOP


def plan_losses(lines: list[QueuedLine]) -> LossPlan:
    plan = LossPlan()
    last_stop: int | None = None
    prompt_since_stop = False
    in_turn: list[int] = []
    for index, item in enumerate(lines):
        if item.corrupt is not None:
            if last_stop is not None and not prompt_since_stop:
                plan.before[last_stop].append(item.lineno)
                plan.carry[last_stop].append(item.lineno)
            else:
                in_turn.append(item.lineno)
        elif is_stop(item):
            plan.before[index] = in_turn
            plan.carry[index] = []
            in_turn = []
            last_stop, prompt_since_stop = index, False
        elif item.kind == "hook" and getattr(item.payload, "event", None) == EVENT_USER_PROMPT_SUBMIT:
            prompt_since_stop = True
    plan.tail = in_turn
    return plan
