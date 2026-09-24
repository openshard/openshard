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
Verified against a real Grok Build 1.0.41 on Windows (a headless task that
edited a file, added a test and ran pytest, plus a permission denial, a
``--max-turns`` interruption, a non-zero exit, a missing file and a subagent)
and Grok's own bundled documentation (``~/.grok/docs/user-guide/10-hooks.md``).
Every document carries **both** vocabularies at once: Grok's camelCase keys
(``hookEventName`` -- a *snake_case value* such as ``post_tool_use`` --,
``sessionId``, ``cwd``, ``workspaceRoot``, ``toolName``, ``toolInput``,
``toolResult``, ``promptId``, ``timestamp``, ``permissionMode``,
``transcriptPath``) and Claude-compatible aliases (``hook_event_name`` with a
PascalCase value, ``session_id``, ``tool_name``, ``tool_input``,
``tool_response``, ``transcript_path``, ``permission_mode``). That is why the
Claude Code receiver must recognise and refuse these documents
(``claude_hooks.is_grok_build_document``): to it, a Grok document is a
perfectly valid Claude payload.

Read here, all confirmed in real payloads:

* the event: the installed ``--event`` first, then ``hook_event_name``, then
  ``hookEventName`` (snake_case, mapped back to the PascalCase name);
* ``sessionId`` (a UUIDv7), ``cwd``;
* ``prompt`` on ``UserPromptSubmit`` -> the scrubbed, bounded task excerpt;
* ``source`` on ``SessionStart`` (``new``), ``reason`` on ``SessionEnd``
  (``shutdown``) and on ``Stop`` (``end_turn`` for a real turn end);
* ``toolName`` plus, from ``toolInput``, only: ``command`` for
  ``run_terminal_command``; ``file_path`` for ``search_replace`` (Grok's one
  edit tool -- Claude's ``Edit`` / ``Write`` / ``MultiEdit`` all alias to it);
  ``target_file`` for ``read_file``; ``target_directory`` for ``list_dir``.

* (verification v2) on ``PostToolUse`` for ``run_terminal_command`` only: the
  one integer ``toolResult.exit_code`` (``tool_response`` alias), unless
  ``toolResultTruncated`` is true. It is Grok's report of the command's exit
  status -> the check outcome is ``agent_reported`` (see ``_terminal_exit_code``).

**Never read:** the rest of ``toolResult`` / ``tool_response`` (command
output, ``output_for_prompt``), ``toolInput`` content other than the one path or the command line
(``old_string`` / ``new_string``, ``description``, queries), ``transcriptPath``
and Grok's session files, ``lastAssistantMessage``, ``workspaceRoot``, and
every unknown key. An agent label in the payload is never read: the agent is
fixed by the receiver path the hook process posts to.

Real-payload behaviours the mapping depends on
----------------------------------------------
* ``PostToolUse`` fires for **every tool that ran**, including a shell
  command that exited non-zero and a ``read_file`` of a missing file (both
  observed); ``PostToolUseFailure`` is reserved for a tool that failed to
  dispatch or an MCP error. So ``PostToolUse`` is never a success signal:
  file tools are recorded ``unknown`` with no hook-reported path, and git
  supplies the file evidence. A shell command's outcome comes only from its
  ``exit_code`` (above), never from the event itself.
* ``Stop`` fires **twice** for a normal one-turn session: ``reason:
  "end_turn"`` with a ``promptId`` when the turn ends, and again *after*
  ``SessionEnd`` with ``reason: "shutdown"``. Only ``end_turn`` is a completed
  turn; a ``Stop`` with any other reason is ignored.
* ``StopCancelled`` (``reason: max_turns`` observed; also a user interrupt or
  a declined permission) fires **instead of** ``Stop`` -> a neutral
  ``SessionIdle`` boundary, never a completed turn. ``StopFailure`` likewise.
* A subagent runs as a session of its own: a new ``sessionId``, and every one
  of its events (including its ``UserPromptSubmit`` and ``SessionEnd``) carries
  ``subagentType``. Such an event is ignored, so a subagent never becomes a
  phantom Shard. The parent's own ``spawn_subagent`` call is an ordinary tool
  record, and files a subagent changed are still found by git.

Event mapping (see ``docs/agent-capture.md`` for the full table)
----------------------------------------------------------------
``SessionStart`` -> ``SessionStart``; ``UserPromptSubmit`` ->
``UserPromptSubmit``; ``PostToolUse`` -> ``PostToolUse`` with **no success
signal**; ``PostToolUseFailure`` -> ``PostToolUseFailure`` (a failed call);
``PermissionDenied`` -> ``PermissionDenied`` (an ``approval.denied`` Event,
``agent_reported``, tool name only); ``Stop`` (``end_turn``) -> ``Stop``;
``StopFailure`` / ``StopCancelled`` -> ``SessionIdle``; ``SessionEnd`` ->
``SessionEnd``.

Not subscribed, on purpose: ``PreToolUse`` is Grok's only blocking event
(a deny decision or exit code 2 stops the tool) and OpenShard records, it
does not gate; it adds no fact ``PostToolUse`` / ``PermissionDenied`` do not
carry. ``SubagentStart`` / ``SubagentStop`` (a subagent's id and type only;
its own events are ignored, see above), ``Notification``, ``PreCompact`` and
``PostCompact`` carry no fact a Receipt reports.

Model, provider, token counts and cost are not part of any hook payload
(observed), so a Grok Build record never carries them.
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
    "StopCancelled": EVENT_SESSION_IDLE,
    "SessionEnd": EVENT_SESSION_END,
}
GROK_BUILD_HOOK_EVENTS: tuple[str, ...] = tuple(GROK_BUILD_EVENT_MAP)

# Grok's own tool names, as they appear in ``toolName`` (observed). Claude's
# names (``Bash``, ``Edit``, ...) only exist as matcher aliases, never in a
# payload. Anything else (MCP, search, web, subagent tools) is recorded by name
# only. ``search_replace`` is Grok's single edit tool.
COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"run_terminal_command"})
FILE_TOOL_NAMES: dict[str, str] = {"search_replace": "update"}
READ_TOOL_NAMES: frozenset[str] = frozenset({"read_file", "list_dir"})
_WRITE_PATH_KEYS: tuple[str, ...] = ("file_path",)
_READ_PATH_KEYS: tuple[str, ...] = ("target_file", "target_directory")
# Stop's ``reason`` for a real turn end; the session-end Stop says "shutdown".
_TURN_END_REASON = "end_turn"

# ``hookEventName``'s snake_case values -> the PascalCase event name.
_SNAKE_EVENT_NAMES: dict[str, str] = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "post_tool_use": "PostToolUse",
    "post_tool_use_failure": "PostToolUseFailure",
    "permission_denied": "PermissionDenied",
    "stop": "Stop",
    "stop_failure": "StopFailure",
    "stop_cancelled": "StopCancelled",
    "session_end": "SessionEnd",
}


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
    """The Grok event a document belongs to.

    The installed ``--event`` first, then the PascalCase ``hook_event_name``
    every document carries, then ``hookEventName`` (a snake_case value).
    """
    if isinstance(event_override, str) and event_override:
        return event_override
    pascal = data.get("hook_event_name")
    if isinstance(pascal, str) and pascal:
        return pascal
    snake = data.get("hookEventName")
    if isinstance(snake, str) and snake:
        return _SNAKE_EVENT_NAMES.get(snake, snake)
    return None


def _first_str(args: Mapping[str, Any], keys: tuple[str, ...], limit: int) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:limit]
    return None


def _terminal_exit_code(data: Mapping[str, Any]) -> int | None:
    """``toolResult.exit_code`` of a ``run_terminal_command`` (verification v2), or None.

    Grok's hooks reference: "the ``PostToolUse`` tool output is
    ``toolResult``" (``tool_response`` is a copy), shaped like ``{"type":
    "Bash", "command": ..., "exit_code": 0, "output_for_prompt": ...}``, and
    "Check ``toolResultTruncated`` first: an oversized payload reaches the hook
    as a plain string". Only that one integer is read -- the output never is;
    a truncated or non-object result yields no exit code.
    """
    if data.get("toolResultTruncated") is True:
        return None
    result = data.get("toolResult")
    if result is None:
        result = data.get("tool_response")
    if not isinstance(result, Mapping):
        return None
    code = result.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def extract_grok_build_payload(
    data: Mapping[str, Any], *, event_override: str | None = None
) -> HookPayload | None:
    """Pick the supported fields out of a decoded Grok Build hook document.

    Returns ``None`` for an event OpenShard does not subscribe to. Unknown
    keys are ignored and malformed shapes under-report (a tool record with
    no path or command), never raise. Never attaches a tool success signal;
    a shell command carries only the exit code Grok reported for it.
    """
    name = resolve_event_name(data, event_override)
    if name not in GROK_BUILD_EVENT_MAP:
        return None
    if "subagentType" in data:
        return None  # a subagent's own session (own sessionId): never a Shard of its own
    if name == "Stop":
        reason = data.get("reason")
        if isinstance(reason, str) and reason != _TURN_END_REASON:
            return None  # the extra Stop Grok fires at session end ("shutdown") is not a turn
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
            if name == "PostToolUse":
                payload.command_exit_code = _terminal_exit_code(data)
        elif kind == TOOL_KIND_FILE:
            path = _first_str(tool_input, _WRITE_PATH_KEYS, 2_000)
            if path:
                payload.file_path = path
                payload.file_paths = [(path, FILE_TOOL_NAMES.get((tool_name or "").lower(), "update"))]
        elif kind == TOOL_KIND_READ:
            payload.file_path = _first_str(tool_input, _READ_PATH_KEYS, 2_000)
    return payload
