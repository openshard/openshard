"""Cursor hook payload translation for OpenShard capture (0.4.2).

Cursor's agent hooks (``.cursor/hooks.json``) deliver a JSON document on
stdin to a *command* hook, the same way Claude Code's and Codex's command
hooks do, but with Cursor's own event names (camelCase) and field
vocabulary. Cursor has no HTTP hook type, so every subscribed event runs
``openshard hooks cursor``, whose whole job is to hand the raw document to
the local capture service (``adapters/claude_capture_client.run_cursor_hook``)
and write the small decision reply Cursor expects on stdout. The service
calls :func:`extract_cursor_payload` below on its blocking path and
everything after that -- reduction, queue, fold, receipt -- is the shared
code in ``adapters/claude_hooks.py``.

Field audit -- what is read, and on what authority
--------------------------------------------------
Confirmed against Cursor's hooks reference (``cursor.com/docs/agent/hooks``):

* Every hook document carries ``conversation_id``, ``generation_id``,
  ``model`` (``model_id`` optionally), ``hook_event_name``,
  ``cursor_version``, ``workspace_roots``, ``user_email`` and
  ``transcript_path``. OpenShard reads ``conversation_id`` (the session),
  ``hook_event_name``, ``model``/``model_id`` and ``workspace_roots[0]``
  (as the fallback working directory). **``user_email`` and
  ``transcript_path`` are never read** -- not even to discard them.
* ``sessionStart`` (``session_id``, ``is_background_agent``,
  ``composer_mode``) -> ``SessionStart``. ``is_background_agent`` becomes
  the start source (``background`` / ``startup``); ``composer_mode`` is
  not read. Cloud/background agents do not fire ``sessionStart`` /
  ``sessionEnd`` at all, so the fold's lazy buffer creation on the first
  hook seen is what makes those sessions capturable.
* ``beforeSubmitPrompt`` (``prompt``, ``attachments``) ->
  ``UserPromptSubmit``. Only ``prompt`` is read, and only to derive the
  scrubbed, bounded task excerpt; ``attachments`` (file paths and rule
  files) are never read. This is the one *blocking* event OpenShard
  subscribes to: the hook must answer ``{"continue": true}`` on stdout,
  which ``run_cursor_hook`` does unconditionally (see there).
* ``postToolUse`` / ``postToolUseFailure`` (``tool_name``, ``tool_input``,
  ``tool_use_id``, ``cwd``, ``duration``; failures add ``error_message``,
  ``failure_type``, ``is_interrupt``; successes add ``tool_output``) ->
  ``PostToolUse`` / ``PostToolUseFailure``. ``tool_name`` is one of
  ``Shell | Read | Write | Grep | Delete | Task | MCP:<tool>``. ``Shell``
  is the command tool (``tool_input.command``); ``Write`` / ``Delete`` are
  file tools whose ``tool_input`` path key is not documented, so
  ``file_path`` / ``path`` are tolerated and anything else under-reports.
  Every other tool is recorded by name only. **``error_message``,
  ``working_directory`` and ``tool_output`` -- except the one ``exitCode``
  integer of a ``Shell`` result (verification v2, see ``_shell_outcome``) --
  are never read.** Cursor now documents ``postToolUse`` as "Called after
  successful tool execution", but that is the *tool* succeeding (its
  ``Shell`` example carries an ``exitCode``), so no file-tool success
  signal is attached yet (``tool_success`` stays ``None``): file tools are
  recorded ``unknown`` and contribute no hook-reported paths.
  ``failure_type`` / ``is_interrupt`` only tell a shell timeout / denial /
  interrupt (no result) apart from an error.
* ``afterFileEdit`` (``file_path``, ``edits``) -> ``FileEdited``. Cursor
  fires it after an edit was applied, so, like OpenCode's ``file.edited``,
  the path is the positive signal that feeds the hook-reported file list
  used when git is unavailable. **``edits`` (old/new strings) is never
  read.**
* ``stop`` (``status``: ``completed | aborted | error``, ``loop_count``):
  ``completed`` -> ``Stop`` (a completed turn); ``aborted`` ->
  ``Interrupt`` (activity, never completion); ``error`` or a missing /
  unknown status -> ``SessionIdle`` (a neutral boundary that snapshots the
  record and never counts as a completed turn). No ``followup_message`` is
  ever returned.
* ``sessionEnd`` (``session_id``, ``reason``: ``completed | aborted |
  error | window_close | user_close``, ``duration_ms``,
  ``is_background_agent``, ``final_status``, ``error_message``) ->
  ``SessionEnd``; only ``reason`` is read.
* Not subscribed: ``afterShellExecution`` (its ``command`` duplicates the
  ``postToolUse`` ``Shell`` record and it carries the full terminal
  ``output``), ``beforeReadFile`` (carries file ``content``),
  ``afterAgentResponse`` / ``afterAgentThought`` (assistant text),
  ``preToolUse`` / ``beforeShellExecution`` / ``beforeMCPExecution``
  (permission decisions OpenShard must not make), ``subagentStop``
  (a sub-agent's turn is not the session's; its work still arrives as
  tool/file events), ``preCompact`` (context housekeeping, no evidence),
  the Tab hooks, and ``workspaceOpen``.

Cost and token counts are not exposed by Cursor hooks, so a Cursor record
never carries them. The model provider is not exposed either and is never
guessed from the model name.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from openshard.adapters.capture_agents import AGENT_CURSOR
from openshard.adapters.claude_hooks import (
    _SESSION_ID_RE,
    EVENT_FILE_EDITED,
    EVENT_INTERRUPT,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_SESSION_END,
    EVENT_SESSION_IDLE,
    EVENT_SESSION_START,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    OUTCOME_NOT_COMPLETED,
    TOOL_KIND_COMMAND,
    TOOL_KIND_FILE,
    TOOL_KIND_OTHER,
    HookPayload,
    _str_or_none,
)

# Cursor-side event names OpenShard subscribes to -> the neutral event each
# becomes (``stop`` is refined by its ``status``; see ``_STOP_STATUS_EVENTS``).
CURSOR_EVENT_MAP: dict[str, str] = {
    "sessionStart": EVENT_SESSION_START,
    "beforeSubmitPrompt": EVENT_USER_PROMPT_SUBMIT,
    "postToolUse": EVENT_POST_TOOL_USE,
    "postToolUseFailure": EVENT_POST_TOOL_USE_FAILURE,
    "afterFileEdit": EVENT_FILE_EDITED,
    "stop": EVENT_STOP,
    "sessionEnd": EVENT_SESSION_END,
}
CURSOR_HOOK_EVENTS: tuple[str, ...] = tuple(CURSOR_EVENT_MAP)

# ``stop.status`` -> neutral event. Anything but a positive ``completed`` is
# never a completed turn.
_STOP_STATUS_EVENTS: dict[str, str] = {
    "completed": EVENT_STOP,
    "aborted": EVENT_INTERRUPT,
    "error": EVENT_SESSION_IDLE,
}

# Documented hook-facing tool names, compared case-insensitively.
COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"shell"})
FILE_TOOL_NAMES: frozenset[str] = frozenset({"write", "delete"})
# The path key of a Write/Delete ``tool_input`` is not documented; these are
# tolerated (each can only under-report) and nothing else is looked at.
_FILE_INPUT_KEYS: tuple[str, ...] = ("file_path", "path")


def classify_cursor_tool(tool_name: str | None) -> str:
    name = (tool_name or "").lower()
    if name in COMMAND_TOOL_NAMES:
        return TOOL_KIND_COMMAND
    if name in FILE_TOOL_NAMES:
        return TOOL_KIND_FILE
    return TOOL_KIND_OTHER


def _session_id(data: Mapping[str, Any]) -> str | None:
    for key in ("conversation_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and _SESSION_ID_RE.match(value):
            return value
    return None


def _cwd(data: Mapping[str, Any]) -> str | None:
    cwd = _str_or_none(data.get("cwd"), 1_000)
    if cwd:
        return cwd
    roots = data.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return _str_or_none(roots[0], 1_000)
    return None


def _model(data: Mapping[str, Any]) -> str | None:
    return _str_or_none(data.get("model_id"), 200) or _str_or_none(data.get("model"), 200)


_MAX_TOOL_OUTPUT_CHARS = 1_000_000  # a larger tool_output is not parsed (exit code stays unknown)
_NOT_COMPLETED_FAILURES = frozenset({"timeout", "permission_denied"})


def _shell_outcome(name: str, data: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """The outcome Cursor reports for one ``Shell`` call (verification v2).

    ``postToolUse``: Cursor documents ``tool_output`` as the "JSON-stringified
    result payload from the tool" and its reference ``Shell`` example carries
    ``exitCode``. Only that one integer is read (``stdout`` and the rest are
    never looked at); no ``exitCode`` -> no outcome, because "successful tool
    execution" means the tool ran, not that the command exited 0.
    ``postToolUseFailure``: ``is_interrupt`` or a ``failure_type`` of
    ``timeout`` / ``permission_denied`` means the command has no result
    (``not_completed``); ``error`` stays a failed tool call.
    """
    if name == "postToolUseFailure":
        if data.get("is_interrupt") is True or data.get("failure_type") in _NOT_COMPLETED_FAILURES:
            return OUTCOME_NOT_COMPLETED, None
        return None, None
    raw = data.get("tool_output")
    parsed: object = raw
    if isinstance(raw, str):
        if len(raw) > _MAX_TOOL_OUTPUT_CHARS:
            return None, None
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            return None, None
    if not isinstance(parsed, Mapping):
        return None, None
    code = parsed.get("exitCode")
    if isinstance(code, bool) or not isinstance(code, int):
        return None, None
    return None, code


def extract_cursor_payload(data: Mapping[str, Any], *, event_override: str | None = None) -> HookPayload | None:
    """Pick the supported fields out of a decoded Cursor hook document.

    Returns ``None`` for an event OpenShard does not subscribe to or when
    the document is not a Cursor hook. Unknown keys are ignored. Never
    attaches a file-tool success signal; a ``Shell`` call carries only the
    exit code Cursor reported (see ``_shell_outcome``).
    """
    name = data.get("hook_event_name")
    if not isinstance(name, str) or not name:
        name = event_override
    if not isinstance(name, str) or name not in CURSOR_EVENT_MAP:
        return None

    event = CURSOR_EVENT_MAP[name]
    source: str | None = None
    reason: str | None = None
    if name == "stop":
        status = data.get("status")
        event = _STOP_STATUS_EVENTS.get(status, EVENT_SESSION_IDLE) if isinstance(status, str) else EVENT_SESSION_IDLE
    elif name == "sessionStart":
        source = "background" if data.get("is_background_agent") is True else "startup"
    elif name == "sessionEnd":
        reason = _str_or_none(data.get("reason"), 40)

    payload = HookPayload(
        event=event,
        session_id=_session_id(data),
        cwd=_cwd(data),
        source=source,
        reason=reason,
        agent=AGENT_CURSOR,
        model_id=_model(data),
        tool_success=None,
    )

    if name == "beforeSubmitPrompt":
        payload.prompt = _str_or_none(data.get("prompt"))
    elif name in ("postToolUse", "postToolUseFailure"):
        tool_name = _str_or_none(data.get("tool_name"), 80)
        payload.tool_name = tool_name
        kind = classify_cursor_tool(tool_name)
        payload.tool_kind = kind
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        if kind == TOOL_KIND_COMMAND:
            command = tool_input.get("command")
            payload.command = command if isinstance(command, str) and command else None
            payload.command_outcome, payload.command_exit_code = _shell_outcome(name, data)
        elif kind == TOOL_KIND_FILE:
            for key in _FILE_INPUT_KEYS:
                path = tool_input.get(key)
                if isinstance(path, str) and path:
                    payload.file_path = _str_or_none(path, 2_000)
                    break
    elif name == "afterFileEdit":
        payload.file_path = _str_or_none(data.get("file_path"), 2_000)
    return payload
