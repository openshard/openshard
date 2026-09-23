"""Grok Build hook payload translation for OpenShard capture (unreleased).

Grok Build (xAI's terminal coding agent, the ``grok`` CLI) has its own
native hook system, documented at ``docs.x.ai/build/features/hooks``. It is
Claude-Code-*shaped* (an event -> matcher-group -> handler config, JSON on
stdin, exit code 2 = deny) but it is **not** Claude Code: the config lives in
``.grok/hooks/*.json``, the stdin vocabulary is camelCase
(``hookEventName``, ``sessionId``, ``toolName``, ``toolInput``), the event
set is different (``PermissionDenied``, ``StopFailure``), and Grok also
*loads* ``.claude/settings.json`` for compatibility. This translator reads
only Grok's own vocabulary, never Claude's snake_case one, and stamps every
payload ``agent="grok_build"`` so a Grok Build session is never recorded as
a Claude Code one.

Grok Build supports ``command`` and ``http`` handlers. OpenShard installs
``command`` handlers: the ``http`` type is documented only as "POST the
event to a url" with no header or authentication contract, and OpenShard's
capture service requires a bearer token on every request. A command hook
(``openshard hooks grok-build``) hands the raw document to the local capture
service over authenticated loopback
(``adapters/claude_capture_client.run_grok_build_hook``) and prints ``{}``.
The service calls :func:`extract_grok_build_payload` below on its blocking
path; reduction, queue, fold and receipt are the shared code in
``adapters/claude_hooks.py``.

Sources and field audit -- what is read, and on what authority
--------------------------------------------------------------
**Documented** (docs.x.ai hooks reference): every event carries
``hookEventName``, ``sessionId``, ``cwd`` and ``workspaceRoot``; tool events
add ``toolName`` and ``toolInput``. The events are ``SessionStart``,
``SessionEnd``, ``UserPromptSubmit``, ``PreToolUse``, ``PostToolUse``,
``PostToolUseFailure``, ``PermissionDenied``, ``Stop``, ``StopFailure``,
``Notification``, ``SubagentStart``, ``SubagentStop``, ``PreCompact`` and
``PostCompact``. ``PostToolUseFailure`` is a separate event from
``PostToolUse``. Tool names are Grok's own (``run_terminal_command`` ...);
matchers alias the Claude names (``Bash`` matches ``run_terminal_command``).

**Not documented -- read defensively, can only under-report.** The reference
lists no per-event fields beyond the ones above, so these are tolerated
rather than relied on, each behind an ``isinstance`` check and a length cap:

* ``prompt`` on ``UserPromptSubmit`` -> the scrubbed, bounded task excerpt.
  Absent, the task stays the profile placeholder; it is never inferred from
  a transcript or session file.
* ``source`` on ``SessionStart`` and ``reason`` on ``SessionEnd``: short
  strings carried as-is.
* the file path (``filePath`` | ``file_path`` | ``path``) and command
  (``command``) inside ``toolInput`` for tool names OpenShard recognises.

**Never read:** any tool result / output / error field, ``toolInput``
content other than the one path or the command line (file contents,
replacement text, queries), transcript and session files under
``~/.grok``, ``workspaceRoot`` (the working directory is ``cwd``), and every
unknown key. An agent label in the payload is never read: the agent is fixed
by the receiver path the hook process posts to.

Event mapping (see ``docs/agent-capture.md`` for the full table)
----------------------------------------------------------------
``SessionStart`` -> ``SessionStart``; ``UserPromptSubmit`` ->
``UserPromptSubmit``; ``PostToolUse`` -> ``PostToolUse`` with **no success
signal** (the reference does not say it fires only after a successful run,
so a file tool is recorded ``unknown`` and contributes no hook-reported
paths; git-observed changes are the file evidence); ``PostToolUseFailure`` ->
``PostToolUseFailure`` (a failed call); ``PermissionDenied`` ->
``PermissionDenied`` (an ``approval.denied`` Event, ``agent_reported``);
``Stop`` -> ``Stop`` (a completed turn); ``StopFailure`` -> ``SessionIdle``
(a neutral boundary -- the turn ended in an error, so it is never a
completed turn); ``SessionEnd`` -> ``SessionEnd``.

Not subscribed, on purpose: ``PreToolUse`` is Grok's only blocking event
(a deny decision or exit code 2 stops the tool) and OpenShard records, it
does not gate; it adds no fact ``PostToolUse`` / ``PermissionDenied`` do not
carry. ``SubagentStart`` / ``SubagentStop`` are documented without a payload,
so nothing useful (or safe -- a subagent may carry its own ``sessionId``) can
be read from them. ``Notification``, ``PreCompact`` and ``PostCompact`` carry
no fact a Receipt reports.

Model, provider, token counts and cost are not part of the documented hook
payload, so a Grok Build record never carries them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openshard.adapters.capture_agents import AGENT_GROK_BUILD
from openshard.adapters.claude_hooks import (
    _SESSION_ID_RE,
    EVENT_PERMISSION_DENIED,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_SESSION_END,
    EVENT_SESSION_IDLE,
    EVENT_SESSION_START,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    TOOL_KIND_COMMAND,
    TOOL_KIND_FILE,
    TOOL_KIND_OTHER,
    TOOL_KIND_READ,
    HookPayload,
    _str_or_none,
)

# Grok-side event name -> the neutral event it becomes.
GROK_BUILD_EVENT_MAP: dict[str, str] = {
    "SessionStart": EVENT_SESSION_START,
    "UserPromptSubmit": EVENT_USER_PROMPT_SUBMIT,
    "PostToolUse": EVENT_POST_TOOL_USE,
    "PostToolUseFailure": EVENT_POST_TOOL_USE_FAILURE,
    "PermissionDenied": EVENT_PERMISSION_DENIED,
    "Stop": EVENT_STOP,
    "StopFailure": EVENT_SESSION_IDLE,
    "SessionEnd": EVENT_SESSION_END,
}
GROK_BUILD_HOOK_EVENTS: tuple[str, ...] = tuple(GROK_BUILD_EVENT_MAP)

# Tool names compared case-insensitively. Grok's own shell tool is
# ``run_terminal_command``; ``bash`` / ``edit`` / ``write`` / ``read`` are the
# Claude names Grok's matchers alias. Anything else (MCP tools, search, web,
# future tools) is recorded by name only.
COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"run_terminal_command", "bash"})
FILE_TOOL_NAMES: dict[str, str] = {"edit": "update", "multiedit": "update", "write": "create"}
READ_TOOL_NAMES: frozenset[str] = frozenset({"read"})
_PATH_KEYS: tuple[str, ...] = ("filePath", "file_path", "path")


def classify_grok_build_tool(tool_name: str | None) -> str:
    name = (tool_name or "").lower()
    if name in COMMAND_TOOL_NAMES:
        return TOOL_KIND_COMMAND
    if name in FILE_TOOL_NAMES:
        return TOOL_KIND_FILE
    if name in READ_TOOL_NAMES:
        return TOOL_KIND_READ
    return TOOL_KIND_OTHER


def resolve_event_name(data: Mapping[str, Any], event_override: str | None) -> str | None:
    """The Grok event a document belongs to: the installed ``--event`` first, then ``hookEventName``."""
    if isinstance(event_override, str) and event_override:
        return event_override
    name = data.get("hookEventName")
    return name if isinstance(name, str) and name else None


def _first_str(args: Mapping[str, Any], keys: tuple[str, ...], limit: int) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:limit]
    return None


def extract_grok_build_payload(
    data: Mapping[str, Any], *, event_override: str | None = None
) -> HookPayload | None:
    """Pick the supported fields out of a decoded Grok Build hook document.

    Returns ``None`` for an event OpenShard does not subscribe to. Unknown
    keys are ignored and malformed shapes under-report (a tool record with
    no path or command), never raise. Never attaches a success signal.
    """
    name = resolve_event_name(data, event_override)
    if name not in GROK_BUILD_EVENT_MAP:
        return None
    event = GROK_BUILD_EVENT_MAP[name]

    session_id = data.get("sessionId")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        session_id = None

    payload = HookPayload(
        event=event,
        session_id=session_id,
        cwd=_str_or_none(data.get("cwd"), 1_000),
        agent=AGENT_GROK_BUILD,
        tool_success=None,
    )
    if name == "SessionStart":
        payload.source = _str_or_none(data.get("source"), 40)
    elif name == "SessionEnd":
        payload.reason = _str_or_none(data.get("reason"), 40)
    elif name == "UserPromptSubmit":
        payload.prompt = _str_or_none(data.get("prompt"))
    elif name in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
        tool_name = _str_or_none(data.get("toolName"), 80)
        payload.tool_name = tool_name
        if name == "PermissionDenied":
            return payload  # the denial names a tool, nothing about its arguments
        kind = classify_grok_build_tool(tool_name)
        payload.tool_kind = kind
        tool_input = data.get("toolInput")
        if not isinstance(tool_input, dict):
            tool_input = {}
        if kind == TOOL_KIND_COMMAND:
            payload.command = _first_str(tool_input, ("command",), 4_000)
        elif kind == TOOL_KIND_FILE:
            path = _first_str(tool_input, _PATH_KEYS, 2_000)
            if path:
                payload.file_path = path
                payload.file_paths = [(path, FILE_TOOL_NAMES.get((tool_name or "").lower(), "update"))]
        elif kind == TOOL_KIND_READ:
            payload.file_path = _first_str(tool_input, _PATH_KEYS, 2_000)
    return payload
