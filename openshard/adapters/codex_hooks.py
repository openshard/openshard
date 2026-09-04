"""Codex hook payload translation for OpenShard capture (PR12).

Codex's structured hook system (``.codex/hooks.json`` / ``~/.codex``)
delivers a JSON document on stdin to a *command* hook, exactly the way
Claude Code's command hooks do, with the same top-level vocabulary
(``hook_event_name``, ``session_id``, ``cwd``, ``tool_name``,
``tool_input``, ``prompt``, ``source``) plus a few Codex-only fields
(``model``, ``turn_id``, ``permission_mode``). Codex has no HTTP hook
type, so every event runs ``openshard hooks codex``, whose whole job is to
hand the raw document to the local capture service
(``adapters/claude_capture_client.run_hook_via_service``) and exit. The
service calls :func:`extract_codex_payload` below on its blocking path and
everything after that -- reduction, queue, fold, receipt -- is the shared
code in ``adapters/claude_hooks.py``.

Field audit -- what is read, and on what authority
--------------------------------------------------
Confirmed by OpenAI's Codex hooks reference (``developers.openai.com/codex/hooks``):

* Events: ``SessionStart``, ``SessionEnd``, ``UserPromptSubmit``,
  ``Stop``, ``Interrupt``, ``PostToolUse`` are the ones OpenShard
  subscribes to. ``Interrupt`` (the user interrupted a turn) maps to the
  neutral ``Interrupt`` event: an activity fact, never a completion.
  Codex has **no** ``PostToolUseFailure``; ``PostToolUse`` also runs for
  shell commands that exit non-zero, so a Codex ``PostToolUse`` is never
  a success signal (``HookPayload.tool_success`` stays ``None``, and the
  fold records file tools as ``unknown`` with no hook-reported paths).
* Shared fields: ``session_id``, ``cwd``, ``hook_event_name``, ``model``
  (the active model slug, every event). ``turn_id`` / ``permission_mode``
  / ``tool_use_id`` exist but are not needed and never read.
* ``PostToolUse``: ``tool_name`` is ``Bash`` for shell commands (the
  hook-facing name even when the underlying tool is ``exec_command`` /
  unified exec), ``apply_patch`` for file edits, or ``mcp__server__tool``.
  ``tool_input.command`` (a string) carries the shell command and, for
  ``apply_patch``, the patch envelope. Hook *matchers* accept ``Edit`` /
  ``Write`` as aliases for ``apply_patch``, but hook input still reports
  ``tool_name: "apply_patch"`` -- so no Edit/Write-with-``file_path`` tool
  exists in Codex hook input and none is parsed.
* ``SessionEnd``: ``reason`` (currently only ``"other"``); ``SessionStart``:
  ``source``. ``SessionEnd`` / ``Interrupt`` hooks default to a 1 s timeout
  (3 s maximum); ``async: true`` is supported on command hooks
  (``SessionEnd`` always runs synchronously).

Tolerated but unconfirmed shapes -- each can only *under*-report, never
add evidence:

* ``tool_input.command`` as an argv list (the shape of Codex's pre-hooks
  shell tool). Joined into the scrubbed command summary; nothing else.
* ``apply_patch`` text under ``tool_input.patch`` (seen in community hook
  templates as ``${tool_input.patch}``). Tried only after ``command``.
* ``prompt`` on ``UserPromptSubmit`` and ``stop_hook_active`` on ``Stop``
  are the Claude Code field names Codex's hook vocabulary mirrors; the
  former only feeds the scrubbed task excerpt, the latter is carried and
  never acted on.

What is never read: ``transcript_path``, ``tool_response``,
``last_assistant_message``, ``end_reason``, and every unknown key. An
``apply_patch`` document is read for its *file headers only* (``*** Add /
Update / Delete File:`` and ``*** Move to:``), to learn which repository
files Codex says it tried to change; the patch body is never looked at,
and those paths become the ``tool.invoked`` target (status ``unknown``),
never hook-reported file evidence. Cost and token counts are not exposed
by Codex hooks, so a Codex record never carries them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from openshard.adapters.capture_agents import AGENT_CODEX
from openshard.adapters.claude_hooks import (
    _SESSION_ID_RE,
    EVENT_INTERRUPT,
    EVENT_POST_TOOL_USE,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    TOOL_KIND_COMMAND,
    TOOL_KIND_FILE,
    TOOL_KIND_OTHER,
    HookPayload,
    _str_or_none,
)

# Codex hook events OpenShard subscribes to (see codex_hooks_install.HOOK_SPECS).
CODEX_HOOK_EVENTS: tuple[str, ...] = (
    EVENT_SESSION_START,
    EVENT_USER_PROMPT_SUBMIT,
    EVENT_POST_TOOL_USE,
    EVENT_STOP,
    EVENT_SESSION_END,
    EVENT_INTERRUPT,
)
_SUPPORTED: frozenset[str] = frozenset(CODEX_HOOK_EVENTS)

# Hook-facing tool names (documented): ``Bash`` for shell commands,
# ``apply_patch`` for file edits. Compared case-insensitively. Any other
# name (MCP tools, future tools) is recorded by name only.
COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"bash"})
FILE_TOOL_NAMES: frozenset[str] = frozenset({"apply_patch"})
# Documented key for both the shell command and the apply_patch envelope,
# then the unconfirmed community-template key (see module docstring).
_PATCH_INPUT_KEYS: tuple[str, ...] = ("command", "patch")

_PATCH_HEADER_RE = re.compile(r"^\*\*\*\s+(Add|Update|Delete)\s+File:\s*(.+?)\s*$")
_PATCH_MOVE_RE = re.compile(r"^\*\*\*\s+Move\s+to:\s*(.+?)\s*$")
_MAX_PATCH_SCAN_LINES = 5_000
_MAX_PATCH_FILES = 20


def parse_apply_patch_files(patch: object) -> list[tuple[str, str]]:
    """``(path, change_type)`` for every file header in an apply_patch document.

    Reads header lines only (``*** Add/Update/Delete File:``, plus the
    ``*** Move to:`` rename target), bounded in lines and files; the diff
    body is skipped. Never raises; a non-string yields ``[]``.
    """
    if not isinstance(patch, str) or "***" not in patch:
        return []
    files: list[tuple[str, str]] = []
    change_types = {"Add": "create", "Update": "update", "Delete": "delete"}
    for line in patch.splitlines()[:_MAX_PATCH_SCAN_LINES]:
        if len(files) >= _MAX_PATCH_FILES:
            break
        if not line.startswith("***"):
            continue
        match = _PATCH_HEADER_RE.match(line)
        if match:
            files.append((match.group(2), change_types[match.group(1)]))
            continue
        move = _PATCH_MOVE_RE.match(line)
        if move:
            files.append((move.group(1), "create"))
    return files


def _command_text(value: object) -> str | None:
    """A shell command from ``tool_input.command``: a string (documented) or an argv list (tolerated)."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, list) and all(isinstance(p, str) for p in value):
        return " ".join(value) or None
    return None


def classify_codex_tool(tool_name: str | None) -> str:
    name = (tool_name or "").lower()
    if name in COMMAND_TOOL_NAMES:
        return TOOL_KIND_COMMAND
    if name in FILE_TOOL_NAMES:
        return TOOL_KIND_FILE
    return TOOL_KIND_OTHER


def extract_codex_payload(data: Mapping[str, Any], *, event_override: str | None = None) -> HookPayload | None:
    """Pick the supported fields out of a decoded Codex hook document.

    Returns ``None`` for an event OpenShard does not subscribe to or when
    the document is not a Codex hook. Unknown keys are ignored. Never
    attaches a success signal: Codex documents no such thing.
    """
    event = data.get("hook_event_name")
    if not isinstance(event, str) or not event:
        event = event_override
    if not isinstance(event, str) or event not in _SUPPORTED:
        return None

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        session_id = None

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    payload = HookPayload(
        event=event,
        session_id=session_id,
        cwd=_str_or_none(data.get("cwd"), 1_000),
        source=_str_or_none(data.get("source"), 40),
        reason=_str_or_none(data.get("reason"), 40),
        prompt=_str_or_none(data.get("prompt")),
        stop_hook_active=bool(data.get("stop_hook_active")),
        agent=AGENT_CODEX,
        model_id=_str_or_none(data.get("model"), 200),
        tool_success=None,
    )
    if event == EVENT_POST_TOOL_USE:
        tool_name = _str_or_none(data.get("tool_name"), 80)
        payload.tool_name = tool_name
        kind = classify_codex_tool(tool_name)
        payload.tool_kind = kind
        if kind == TOOL_KIND_COMMAND:
            payload.command = _command_text(tool_input.get("command"))
        elif kind == TOOL_KIND_FILE:
            for key in _PATCH_INPUT_KEYS:
                files = parse_apply_patch_files(tool_input.get(key))
                if files:
                    payload.file_paths = files
                    break
    return payload
