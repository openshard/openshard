"""Google Antigravity hook payload translation for OpenShard capture (0.4.7).

Antigravity (the agent-first IDE and its ``agy`` CLI) runs *command* hooks
declared in a ``hooks.json`` file: each subscribed event runs a command
with one JSON document on stdin and reads one JSON reply on stdout -- the
same shape as Cursor's and Codex's command hooks. There is no HTTP hook
type, so every subscribed event runs ``openshard hooks antigravity``, whose
whole job is to hand the raw document to the local capture service
(``adapters/claude_capture_client.run_antigravity_hook``) and write the
small reply Antigravity requires. The service calls
:func:`extract_antigravity_payload` below on its blocking path; everything
after that -- reduction, queue, fold, receipt -- is the shared code in
``adapters/claude_hooks.py``.

Sources and field audit -- what is read, and on what authority
--------------------------------------------------------------
Google documents hooks at ``antigravity.google/docs/hooks`` (CLI) and
``antigravity.google/docs/ide/hooks`` (IDE). The documented events are
``PreToolUse``, ``PostToolUse``, ``PreInvocation``, ``PostInvocation`` and
``Stop``; stdin is camelCase JSON; ``PreToolUse`` / ``PostToolUse`` take a
regex ``matcher`` and the three others a plain handler list. Field shapes
below are cross-checked against independent open-source integrations that
parse live Antigravity payloads (atuin, AgentNotch, emdash), because the
reference is terse on per-event fields:

* Every document carries ``conversationId`` (the session), ``workspacePaths``
  (the first entry is the working directory), ``modelName``,
  ``transcriptPath`` and ``artifactDirectoryPath``. OpenShard reads
  ``conversationId``, ``workspacePaths[0]`` and ``modelName``.
  **``transcriptPath`` and ``artifactDirectoryPath`` are never read.**
* The event name is **not reliably in the payload** (some builds send
  ``hookEventName``, others nothing), so the installer puts it on the
  command line (``--event PreInvocation``). The command-line name wins;
  ``hookEventName`` is only a fallback. An agent label in the payload is
  never read: the agent is fixed by the receiver path the hook process
  posts to, not by anything the document claims.
* ``PreInvocation`` (``invocationNum``) fires before every model call ->
  ``ModelInvocation``: the session did work, with ``modelName`` as the
  model for that call. ``invocationNum`` is not read (integrations
  disagree on whether it starts at 0 or 1 and whether it resets per turn).
  No user prompt text is delivered to a command hook, so the task stays
  the profile placeholder -- never inferred from the transcript.
* ``PostToolUse`` (``toolCall.name``, ``toolCall.args``, ``stepIdx``,
  ``error``) -> ``PostToolUse``, or ``PostToolUseFailure`` when ``error``
  is a non-empty string (a failed call, including a command exiting
  non-zero). The reference describes ``error`` as empty on success, so an
  *explicitly present, empty* ``error`` is the success signal for a file
  tool; an absent ``error`` attaches none. Tool classification uses the
  documented agent tool names (PascalCase args in the IDE, snake_case for
  the ACP ``client_*`` tools)::

      command  run_command                     args.CommandLine
      write    write_to_file                   args.TargetFile (create; update when args.Overwrite is true)
               replace_file_content,           args.TargetFile (update)
               multi_replace_file_content
               client_create_file / client_write_file / client_edit_file
                                               target_file | file_path | path
      read     view_file, view_file_outline    args.AbsolutePath
               view_code_item                  args.File
               list_dir                        args.DirectoryPath
               client_view_file                absolute_path | file_path | path

  Everything else (search, browser, MCP and custom tools) is recorded by
  name only. **``toolCall.args`` content other than the one path or the
  command line -- file contents, replacement chunks, search queries -- and
  the tool result are never read.**
* ``Stop`` (``terminationReason``, ``fullyIdle``, ``error``): no error and
  ``fullyIdle`` not ``false`` -> ``Stop`` (a completed turn); an error, or
  ``fullyIdle: false`` (background work still running) -> ``SessionIdle``,
  a neutral boundary that snapshots the record and is never a completed
  turn. ``terminationReason`` is not read.
* Not subscribed: ``PreToolUse`` is a permission gate (an unrecognised
  reply denies the tool) and recording never needs to gate anything;
  ``PostInvocation`` repeats ``PreInvocation``'s model and would double the
  per-call hook cost. Antigravity has no session-end hook, so a session
  is closed by the shared idle sweep (``session_end_not_observed``).

Token counts, cost and the model provider are not exposed to hooks, so an
Antigravity record never carries them and a provider is never guessed from
the model name (Antigravity also runs non-Google models).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openshard.adapters.capture_agents import AGENT_ANTIGRAVITY
from openshard.adapters.claude_hooks import (
    _SESSION_ID_RE,
    EVENT_MODEL_INVOCATION,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_SESSION_IDLE,
    EVENT_STOP,
    TOOL_KIND_COMMAND,
    TOOL_KIND_FILE,
    TOOL_KIND_OTHER,
    TOOL_KIND_READ,
    HookPayload,
    _str_or_none,
)

# Antigravity-side event names OpenShard subscribes to -> the neutral event
# each becomes (``PostToolUse`` / ``Stop`` are refined by their payload).
ANTIGRAVITY_EVENT_MAP: dict[str, str] = {
    "PreInvocation": EVENT_MODEL_INVOCATION,
    "PostToolUse": EVENT_POST_TOOL_USE,
    "Stop": EVENT_STOP,
}
ANTIGRAVITY_HOOK_EVENTS: tuple[str, ...] = tuple(ANTIGRAVITY_EVENT_MAP)

COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"run_command"})
# tool name -> default change type
WRITE_TOOL_NAMES: dict[str, str] = {
    "write_to_file": "create",
    "replace_file_content": "update",
    "multi_replace_file_content": "update",
    "client_create_file": "create",
    "client_write_file": "update",
    "client_edit_file": "update",
}
READ_TOOL_NAMES: frozenset[str] = frozenset({
    "view_file", "view_file_outline", "view_code_item", "list_dir", "client_view_file",
})
# The one path argument read per tool family; first present wins, nothing
# else in ``args`` is looked at.
_WRITE_PATH_KEYS: tuple[str, ...] = ("TargetFile", "target_file", "file_path", "path")
_READ_PATH_KEYS: tuple[str, ...] = (
    "AbsolutePath", "absolute_path", "File", "DirectoryPath", "file_path", "path",
)
_COMMAND_KEYS: tuple[str, ...] = ("CommandLine", "command_line", "command")


def classify_antigravity_tool(tool_name: str | None) -> str:
    name = (tool_name or "").lower()
    if name in COMMAND_TOOL_NAMES:
        return TOOL_KIND_COMMAND
    if name in WRITE_TOOL_NAMES:
        return TOOL_KIND_FILE
    if name in READ_TOOL_NAMES:
        return TOOL_KIND_READ
    return TOOL_KIND_OTHER


def resolve_event_name(data: Mapping[str, Any], event_override: str | None) -> str | None:
    """The Antigravity event a document belongs to: the installed ``--event`` first."""
    if isinstance(event_override, str) and event_override:
        return event_override
    name = data.get("hookEventName")
    return name if isinstance(name, str) and name else None


def _session_id(data: Mapping[str, Any]) -> str | None:
    value = data.get("conversationId")
    if isinstance(value, str) and _SESSION_ID_RE.match(value):
        return value
    return None


def _cwd(data: Mapping[str, Any]) -> str | None:
    paths = data.get("workspacePaths")
    if isinstance(paths, list):
        for item in paths:
            value = _str_or_none(item, 1_000)
            if value:
                return value
    return None


def _first_str(args: Mapping[str, Any], keys: tuple[str, ...], limit: int) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:limit]
    return None


def extract_antigravity_payload(
    data: Mapping[str, Any], *, event_override: str | None = None
) -> HookPayload | None:
    """Pick the supported fields out of a decoded Antigravity hook document.

    Returns ``None`` for an event OpenShard does not subscribe to. Unknown
    keys are ignored and malformed shapes under-report (a tool record with
    no path or command), never raise.
    """
    name = resolve_event_name(data, event_override)
    if name not in ANTIGRAVITY_EVENT_MAP:
        return None
    event = ANTIGRAVITY_EVENT_MAP[name]
    error = data.get("error")
    has_error = isinstance(error, str) and bool(error.strip())

    if name == "Stop":
        fully_idle = data.get("fullyIdle")
        if has_error or fully_idle is False:
            event = EVENT_SESSION_IDLE
    elif name == "PostToolUse" and has_error:
        event = EVENT_POST_TOOL_USE_FAILURE

    payload = HookPayload(
        event=event,
        session_id=_session_id(data),
        cwd=_cwd(data),
        agent=AGENT_ANTIGRAVITY,
        model_id=_str_or_none(data.get("modelName"), 200),
        tool_success=None,
    )
    if name != "PostToolUse":
        return payload

    call = data.get("toolCall")
    if not isinstance(call, dict):
        call = {}
    tool_name = _str_or_none(call.get("name"), 80)
    payload.tool_name = tool_name
    kind = classify_antigravity_tool(tool_name)
    payload.tool_kind = kind
    args = call.get("args")
    if not isinstance(args, dict):
        args = {}
    if kind == TOOL_KIND_COMMAND:
        payload.command = _first_str(args, _COMMAND_KEYS, 4_000)
    elif kind == TOOL_KIND_FILE:
        path = _first_str(args, _WRITE_PATH_KEYS, 2_000)
        if path:
            change_type = WRITE_TOOL_NAMES.get((tool_name or "").lower(), "update")
            if change_type == "create" and args.get("Overwrite") is True:
                change_type = "update"
            payload.file_path = path
            payload.file_paths = [(path, change_type)]
        # Only an explicitly present, empty ``error`` is the documented
        # success signal; an absent one proves nothing.
        if error == "":
            payload.tool_success = True
    elif kind == TOOL_KIND_READ:
        payload.file_path = _first_str(args, _READ_PATH_KEYS, 2_000)
    return payload
