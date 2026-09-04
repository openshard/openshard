"""OpenCode plugin payload translation for OpenShard capture (PR12).

OpenCode's supported extension mechanism is a *plugin*: a JS/TS module in
``.opencode/plugins/`` (project) or ``~/.config/opencode/plugins/``
(global) that returns hook functions. The OpenShard plugin
(``opencode_plugin_install.PLUGIN_SOURCE``) is deliberately tiny: it
observes a handful of lifecycle hooks, reduces each to a small structured
JSON document, and POSTs it to the local capture service at
``/hooks/opencode``. It holds no OpenShard logic, keeps no history, and
never sends message text beyond a bounded excerpt of the user's prompt.

What the plugin sends (one document per observation)::

    {"event": <name>, "session_id": ..., "directory": ..., "worktree": ...,
     "parent_id": ...,                       # session.created only
     "tool": ..., "file_path": ..., "command": ...,   # tool.execute.after
     "prompt": ...,                          # chat.message (bounded)
     "provider_id": ..., "model_id": ...,    # when OpenCode exposes them
     "message_id": ..., "cost": ..., "tokens": {...}}  # message.updated (assistant)

Mapping to the neutral capture vocabulary (``adapters/claude_hooks.py``):

===================  =================  =====================================
OpenCode             OpenShard event    Notes
===================  =================  =====================================
session.created      SessionStart       ``source=startup``
chat.message         UserPromptSubmit   first-prompt excerpt (scrubbed); model
tool.execute.after   PostToolUse        edit/write/patch -> file; bash -> command;
                                        no outcome is carried, so never ``passed``
file.edited          FileEdited         published by OpenCode only after a successful
                                        write: the positive signal for a hook-reported
                                        path; git diff stays authoritative
session.idle         SessionIdle        neutral boundary: snapshot only, never a
                                        completed turn (idle also follows aborts)
session.deleted      SessionEnd         ``reason=deleted``
message.updated      (usage report)     assistant message: provider/model, cost, tokens
===================  =================  =====================================

Identity: the agent is always OpenCode (``executor = opencode_plugin``);
``provider_id``/``model_id`` are recorded as *the provider and model
OpenCode reports it used*, only when present, and never turn the record
into a "provider" session. Cost and token counts come from OpenCode's own
assistant-message accounting, keyed by message id so a message that is
re-reported while streaming replaces rather than adds; they are stamped
``agent_reported``. A reported ``cost`` of ``0`` means OpenCode had no
pricing, not that the message was free: the fold records a cost only when
at least one per-message cost is strictly positive (tokens are kept
either way). Verification is never recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openshard.adapters.capture_agents import AGENT_OPENCODE
from openshard.adapters.claude_hooks import (
    _SESSION_ID_RE,
    EVENT_FILE_EDITED,
    EVENT_POST_TOOL_USE,
    EVENT_SESSION_END,
    EVENT_SESSION_IDLE,
    EVENT_SESSION_START,
    EVENT_USER_PROMPT_SUBMIT,
    TOOL_KIND_COMMAND,
    TOOL_KIND_FILE,
    TOOL_KIND_OTHER,
    HookPayload,
    StatusPayload,
    _int_or_none,
    _number_or_none,
    _str_or_none,
)

PLUGIN_EVENT_SESSION_CREATED = "session.created"
PLUGIN_EVENT_CHAT_MESSAGE = "chat.message"
PLUGIN_EVENT_TOOL_AFTER = "tool.execute.after"
PLUGIN_EVENT_FILE_EDITED = "file.edited"
PLUGIN_EVENT_SESSION_IDLE = "session.idle"
PLUGIN_EVENT_SESSION_DELETED = "session.deleted"
PLUGIN_EVENT_MESSAGE_UPDATED = "message.updated"

_LIFECYCLE: dict[str, str] = {
    PLUGIN_EVENT_SESSION_CREATED: EVENT_SESSION_START,
    PLUGIN_EVENT_CHAT_MESSAGE: EVENT_USER_PROMPT_SUBMIT,
    PLUGIN_EVENT_TOOL_AFTER: EVENT_POST_TOOL_USE,
    PLUGIN_EVENT_FILE_EDITED: EVENT_FILE_EDITED,
    PLUGIN_EVENT_SESSION_IDLE: EVENT_SESSION_IDLE,
    PLUGIN_EVENT_SESSION_DELETED: EVENT_SESSION_END,
}
SUPPORTED_PLUGIN_EVENTS: tuple[str, ...] = (*_LIFECYCLE, PLUGIN_EVENT_MESSAGE_UPDATED)

# OpenCode's built-in tool names (lower-case). ``args.filePath`` names the
# file for the edit-style tools; ``args.command`` the shell command.
FILE_TOOL_NAMES: frozenset[str] = frozenset({"edit", "write", "patch", "multiedit"})
COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"bash"})


def classify_opencode_tool(tool: str | None) -> str:
    name = (tool or "").lower()
    if name in COMMAND_TOOL_NAMES:
        return TOOL_KIND_COMMAND
    if name in FILE_TOOL_NAMES:
        return TOOL_KIND_FILE
    return TOOL_KIND_OTHER


def _cwd(data: Mapping[str, Any]) -> str | None:
    """The repository directory: the git worktree OpenCode reports, else its directory."""
    return _str_or_none(data.get("worktree"), 1_000) or _str_or_none(data.get("directory"), 1_000)


def extract_opencode_payload(data: Mapping[str, Any]) -> HookPayload | StatusPayload | None:
    """Translate one plugin document into a neutral hook or usage observation.

    Returns ``None`` for an unsupported/unknown event. Only the documented
    keys are read; anything else the plugin (or a future version of it)
    might send is ignored.
    """
    if data.get("agent") not in (None, AGENT_OPENCODE):
        return None
    event = data.get("event")
    if not isinstance(event, str) or event not in SUPPORTED_PLUGIN_EVENTS:
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        session_id = None
    cwd = _cwd(data)
    provider_id = _str_or_none(data.get("provider_id"), 80)
    model_id = _str_or_none(data.get("model_id"), 200)

    if event == PLUGIN_EVENT_MESSAGE_UPDATED:
        tokens = data.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        message_id = _str_or_none(data.get("message_id"), 80)
        if message_id is None:
            return None
        return StatusPayload(
            session_id=session_id,
            cwd=cwd,
            model_id=model_id,
            cost_total_usd=_number_or_none(data.get("cost")),
            tokens_input=_int_or_none(tokens.get("input")),
            tokens_output=_int_or_none(tokens.get("output")),
            tokens_cache_creation=_int_or_none(cache.get("write")),
            tokens_cache_read=_int_or_none(cache.get("read")),
            agent=AGENT_OPENCODE,
            provider_id=provider_id,
            usage_key=message_id,
        )

    payload = HookPayload(
        event=_LIFECYCLE[event],
        session_id=session_id,
        cwd=cwd,
        agent=AGENT_OPENCODE,
        model_id=model_id,
        provider_id=provider_id,
        # ``tool.execute.after`` carries no outcome the plugin forwards, so
        # a tool call is never attested successful from here.
        tool_success=None,
    )
    if event == PLUGIN_EVENT_SESSION_CREATED:
        payload.source = "startup"
    elif event == PLUGIN_EVENT_CHAT_MESSAGE:
        payload.prompt = _str_or_none(data.get("prompt"))
    elif event == PLUGIN_EVENT_TOOL_AFTER:
        tool = _str_or_none(data.get("tool"), 80)
        payload.tool_name = tool
        kind = classify_opencode_tool(tool)
        payload.tool_kind = kind
        if kind == TOOL_KIND_FILE:
            payload.file_path = _str_or_none(data.get("file_path"), 2_000)
        elif kind == TOOL_KIND_COMMAND:
            payload.command = _str_or_none(data.get("command"))
    elif event == PLUGIN_EVENT_FILE_EDITED:
        payload.file_path = _str_or_none(data.get("file_path"), 2_000)
    elif event == PLUGIN_EVENT_SESSION_DELETED:
        payload.reason = "deleted"
    return payload
