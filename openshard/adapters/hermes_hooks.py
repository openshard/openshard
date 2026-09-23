"""Hermes Agent hook payload translation for OpenShard capture (0.4.7).

Hermes Agent (Nous Research) can run *shell hooks* declared in its
``config.yaml``: for every matching lifecycle event it spawns a subprocess,
writes one JSON document to its stdin and reads an optional JSON reply from
its stdout. Every subscribed event runs ``openshard hooks hermes``, whose
whole job is to hand the raw document to the local (authenticated) capture
service (``adapters/claude_capture_client.run_hermes_hook``) and answer with
the empty object Hermes documents as a no-op. The service calls
:func:`extract_hermes_payload` below on its blocking path; everything after
that -- reduction, queue, fold, receipt -- is the shared code in
``adapters/claude_hooks.py``. This is **observation only**: OpenShard never
subscribes ``pre_tool_call`` (the one hook that can block, rewrite or
escalate a tool call) and never returns a directive.

Sources and field audit -- what is read, and on what authority
--------------------------------------------------------------
Official documentation: ``hermes-agent.nousresearch.com/docs/user-guide/features/hooks``
(the plugin-hook catalog and the *Shell Hooks* section), cross-checked against
the runtime that builds the payload (``agent/shell_hooks.py`` ``_payload_fields``,
``model_tools.py`` ``_tool_result_observer_fields``, ``tools/file_tools.py``,
``tools/approval_context.py``).

The stdin document is::

    {"hook_event_name": "post_tool_call", "tool_name": "terminal",
     "tool_input": {...}, "session_id": "...", "cwd": "...",
     "profile": "default", "extra": {<every other hook kwarg>}}

``tool_input`` is the tool's arguments; ``session_id`` falls back to the
parent session for subagent events; ``cwd`` is the Hermes process's working
directory; ``extra`` holds the event-specific kwargs. OpenShard reads
``hook_event_name``, ``tool_name``, ``tool_input``, ``session_id``, ``cwd``
and the named ``extra`` keys below. **``profile`` is never read**, and an
agent label in the payload is never trusted: the agent is fixed by the
receiver path the hook process posts to.

Events subscribed (the neutral event each becomes):

* ``on_session_start`` (``model``) -> ``SessionStart``: fires once for a brand
  new session, so ``source`` is ``startup``.
* ``pre_llm_call`` (``user_message``, ``model``) -> ``UserPromptSubmit``: fires
  once per turn before the tool loop, so a turn interrupted before it ends
  still leaves a trace. The hook's reply is always the empty object, so no
  context is ever injected. A multimodal message contributes only its text
  parts. ``conversation_history`` (the full transcript) is **never read**.
* ``post_tool_call`` (``tool_name``, ``tool_input``, ``extra.status``,
  ``extra.duration_ms``, ``extra.tool_call_id``, ``extra.turn_id``) ->
  ``PostToolUse`` / ``PostToolUseFailure``. Hermes derives ``status`` itself:
  ``ok``, ``error`` (the tool returned an error, including a command that
  exited non-zero) or ``blocked`` (a policy hook stopped it, so it never
  ran). ``ok`` is the one positive success signal and the only thing that
  lets a file tool's paths into the hook-reported list; ``error`` and
  ``blocked`` are failures; an absent or unrecognised ``status`` proves
  nothing. Tool classification uses Hermes' own tool names::

      command  terminal                      tool_input.command
      write    write_file                    tool_input.path
               patch (mode=replace)          tool_input.path
               patch (mode=patch, V4A)       the ``*** Add|Update|Delete|Move File:`` headers
      read     read_file                     tool_input.path

  Everything else (``search_files``, ``execute_code``, ``delegate_task``,
  web, browser, skill and MCP tools) is recorded by name only. **File
  contents, replacement strings, the V4A hunks and the tool ``result`` /
  ``error_message`` are never read.**
* ``post_api_request`` (``model``, ``provider``, ``api_request_id``,
  ``api_call_count``, ``usage``) -> a usage observation, not a lifecycle fact:
  Hermes' own per-request token counts (``input_tokens``, ``output_tokens``,
  ``cache_creation_input_tokens``, ``cache_read_input_tokens``) keyed by the
  request id, so a re-reported request replaces rather than double counts.
  It also names the model and the provider Hermes actually called. It fires
  only after a request succeeded. ``response`` and ``assistant_message`` are
  never read. Hermes reports **no cost**, so cost is never recorded.
* ``on_session_end`` (``completed``, ``interrupted``) -- Hermes fires it at
  the end of *every turn* (and at CLI exit when a turn was running), not at
  the end of the session -> ``Stop`` for a completed turn, ``Interrupt`` for
  an interrupted one, and the neutral ``SessionIdle`` otherwise (a failed or
  incomplete turn, or a reduced exit-path payload), which snapshots the
  record but never counts as a completed turn.
* ``on_session_finalize`` (``reason``) -> ``SessionEnd``: the real teardown.
* ``subagent_start`` / ``subagent_stop`` -> ``SubagentStart`` / ``SubagentStop``
  with the child's role, session id and subagent id, the parent subagent id,
  and (stop) its ``child_status``, ``duration_ms`` and the *number* of tool
  calls in ``tool_call_history``. **The delegated goal, the child's summary
  and the tool history entries are never read.** Both events carry the parent
  session id (the shell-hook payload falls back to it).
* ``pre_approval_request`` / ``post_approval_response`` -> ``ApprovalRequest`` /
  ``ApprovalDecision`` with ``surface``, ``pattern_key``, the ``choice`` and
  ``decided_by`` (smart mode), and the command the gate was raised for
  (scrubbed and capped like any command). They carry the session id only
  when Hermes has bound its correlation context; otherwise the event has no
  session and is dropped rather than attributed to a guess.

Not subscribed: ``pre_tool_call`` (a control hook -- OpenShard does not
enforce anything in this release), ``post_llm_call`` (needs the whole
transcript and only repeats what ``on_session_end`` says), the stream and
auxiliary-call hooks (per-token / per-side-call volume) and the gateway,
kanban and skill hooks.

Consent: Hermes runs a shell hook only after it is allowlisted (per
``(event, command)``). The installer records that consent in Hermes' own
documented allowlist file; see ``adapters/hermes_hooks_install.py``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from openshard.adapters.capture_agents import AGENT_HERMES
from openshard.adapters.claude_hooks import (
    _SESSION_ID_RE,
    EVENT_APPROVAL_DECISION,
    EVENT_APPROVAL_REQUEST,
    EVENT_INTERRUPT,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_SESSION_END,
    EVENT_SESSION_IDLE,
    EVENT_SESSION_START,
    EVENT_STOP,
    EVENT_SUBAGENT_START,
    EVENT_SUBAGENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    TOOL_KIND_COMMAND,
    TOOL_KIND_FILE,
    TOOL_KIND_OTHER,
    TOOL_KIND_READ,
    HookPayload,
    StatusPayload,
    _int_or_none,
    _str_or_none,
)

# Hermes-side hook event names OpenShard subscribes to. ``on_session_end`` and
# ``post_tool_call`` are refined by their payload; ``post_api_request`` is not
# a lifecycle event at all (it becomes a ``StatusPayload``).
HERMES_EVENT_MAP: dict[str, str] = {
    "on_session_start": EVENT_SESSION_START,
    "pre_llm_call": EVENT_USER_PROMPT_SUBMIT,
    "post_tool_call": EVENT_POST_TOOL_USE,
    "on_session_end": EVENT_STOP,
    "on_session_finalize": EVENT_SESSION_END,
    "subagent_start": EVENT_SUBAGENT_START,
    "subagent_stop": EVENT_SUBAGENT_STOP,
    "pre_approval_request": EVENT_APPROVAL_REQUEST,
    "post_approval_response": EVENT_APPROVAL_DECISION,
}
USAGE_EVENT = "post_api_request"
HERMES_HOOK_EVENTS: tuple[str, ...] = (*HERMES_EVENT_MAP, USAGE_EVENT)

COMMAND_TOOL_NAMES: frozenset[str] = frozenset({"terminal"})
READ_TOOL_NAMES: frozenset[str] = frozenset({"read_file"})
WRITE_TOOL_NAMES: frozenset[str] = frozenset({"write_file", "patch"})

# Hermes' V4A patch headers (tools/file_tools.py ``_V4A_*_HEADER_RE``).
_V4A_SINGLE_HEADER_RE = re.compile(r"^\*\*\*\s*(Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
_V4A_MOVE_HEADER_RE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)
_V4A_CHANGE_TYPES = {"Update": "update", "Add": "create", "Delete": "delete"}
_MAX_PATCH_SCAN_CHARS = 200_000  # only the headers matter; bound the regex scan
_MAX_FILE_PATHS = 20
_MAX_USAGE_KEY = 80


def classify_hermes_tool(tool_name: str | None) -> str:
    name = (tool_name or "").lower()
    if name in COMMAND_TOOL_NAMES:
        return TOOL_KIND_COMMAND
    if name in WRITE_TOOL_NAMES:
        return TOOL_KIND_FILE
    if name in READ_TOOL_NAMES:
        return TOOL_KIND_READ
    return TOOL_KIND_OTHER


def resolve_event_name(data: Mapping[str, Any], event_override: str | None) -> str | None:
    """The Hermes event a document belongs to: the payload's own name first."""
    name = data.get("hook_event_name")
    if isinstance(name, str) and name:
        return name
    return event_override if isinstance(event_override, str) and event_override else None


def _extra(data: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = data.get("extra")
    return extra if isinstance(extra, Mapping) else {}


def _session_id(data: Mapping[str, Any]) -> str | None:
    value = data.get("session_id")
    if isinstance(value, str) and _SESSION_ID_RE.match(value):
        return value
    return None


def _user_message(value: object) -> str | None:
    """The turn's text: a plain string, or the text parts of a multimodal message."""
    if isinstance(value, str):
        return _str_or_none(value)
    if isinstance(value, list):
        parts: list[str] = [
            text for p in value
            if isinstance(p, Mapping) and p.get("type") == "text"
            for text in (p.get("text"),) if isinstance(text, str)
        ]
        return _str_or_none("\n".join(parts))
    return None


def _v4a_paths(patch: object) -> list[tuple[str, str]]:
    """``(path, change_type)`` for every file a V4A patch's *headers* name."""
    if not isinstance(patch, str):
        return []
    text = patch[:_MAX_PATCH_SCAN_CHARS]
    found: list[tuple[str, str]] = []
    for match in _V4A_SINGLE_HEADER_RE.finditer(text):
        found.append((match.group(2).strip(), _V4A_CHANGE_TYPES[match.group(1)]))
    for match in _V4A_MOVE_HEADER_RE.finditer(text):
        found.append((match.group(1).strip(), "delete"))
        found.append((match.group(2).strip(), "create"))
    return [(p[:2_000], ct) for p, ct in found if p][:_MAX_FILE_PATHS]


def _str_attr(source: Mapping[str, Any], key: str, limit: int = 80) -> str | None:
    return _str_or_none(source.get(key), limit)


def _tool_attrs(extra: Mapping[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    duration = extra.get("duration_ms")
    if isinstance(duration, int | float) and not isinstance(duration, bool) and duration >= 0:
        attrs["duration_ms"] = int(duration)
    for key, name in (("status", "tool_status"), ("tool_call_id", "tool_call_id"), ("turn_id", "turn_id")):
        value = _str_attr(extra, key)
        if value:
            attrs[name] = value
    return attrs


def _usage_key(extra: Mapping[str, Any]) -> str | None:
    """A stable, bounded key for one provider request (``<turn>:api:<n>``)."""
    request_id = _str_attr(extra, "api_request_id", 200)
    if not request_id:
        return None
    call_count = extra.get("api_call_count")
    key = request_id
    if isinstance(call_count, int) and not isinstance(call_count, bool) and f":{call_count}" not in request_id:
        key = f"{request_id}:{call_count}"
    if len(key) > _MAX_USAGE_KEY:
        key = "h" + hashlib.sha256(key.encode("utf-8")).hexdigest()[: _MAX_USAGE_KEY - 1]
    return key


def _usage_payload(data: Mapping[str, Any], extra: Mapping[str, Any]) -> StatusPayload | None:
    session_id = _session_id(data)
    usage = extra.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    key = _usage_key(extra) if usage else None
    return StatusPayload(
        session_id=session_id,
        cwd=_str_or_none(data.get("cwd"), 1_000),
        model_id=_str_attr(extra, "model", 200),
        # Hermes reports no cost, so none is ever recorded.
        cost_total_usd=None,
        tokens_input=_int_or_none(usage.get("input_tokens")) if key else None,
        tokens_output=_int_or_none(usage.get("output_tokens")) if key else None,
        tokens_cache_creation=_int_or_none(usage.get("cache_creation_input_tokens")) if key else None,
        tokens_cache_read=_int_or_none(usage.get("cache_read_input_tokens")) if key else None,
        agent=AGENT_HERMES,
        provider_id=_str_attr(extra, "provider"),
        usage_key=key,
    )


def _tool_payload(payload: HookPayload, data: Mapping[str, Any], extra: Mapping[str, Any]) -> None:
    tool_name = _str_or_none(data.get("tool_name"), 80)
    payload.tool_name = tool_name
    kind = classify_hermes_tool(tool_name)
    payload.tool_kind = kind
    args = data.get("tool_input")
    args = args if isinstance(args, Mapping) else {}
    status = _str_attr(extra, "status")
    payload.attrs = _tool_attrs(extra)
    if status in ("error", "blocked"):
        payload.event = EVENT_POST_TOOL_USE_FAILURE
    if kind == TOOL_KIND_COMMAND:
        payload.command = _str_or_none(args.get("command"))
    elif kind == TOOL_KIND_FILE:
        if (tool_name or "").lower() == "patch" and args.get("mode") == "patch":
            payload.file_paths = _v4a_paths(args.get("patch"))
            payload.file_path = payload.file_paths[0][0] if payload.file_paths else None
        else:
            path = _str_or_none(args.get("path"), 2_000)
            if path:
                payload.file_path = path
                payload.file_paths = [(path, "update")]
        # ``ok`` is the only positive success signal Hermes gives a tool call.
        if status == "ok":
            payload.tool_success = True
    elif kind == TOOL_KIND_READ:
        payload.file_path = _str_or_none(args.get("path"), 2_000)


def _subagent_attrs(name: str, extra: Mapping[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key in ("child_role", "child_subagent_id", "child_session_id", "parent_subagent_id"):
        value = _str_attr(extra, key)
        if value:
            attrs[key] = value
    if name == "subagent_stop":
        status = _str_attr(extra, "child_status")
        if status:
            attrs["child_status"] = status
        duration = extra.get("duration_ms")
        if isinstance(duration, int | float) and not isinstance(duration, bool) and duration >= 0:
            attrs["duration_ms"] = int(duration)
        history = extra.get("tool_call_history")
        if isinstance(history, list):
            attrs["tool_calls"] = len(history)
    return attrs


def _approval_attrs(name: str, extra: Mapping[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key in ("surface", "pattern_key", "tool_call_id", "turn_id"):
        value = _str_attr(extra, key)
        if value:
            attrs[key] = value
    if name == "post_approval_response":
        for key in ("choice", "decided_by"):
            value = _str_attr(extra, key)
            if value:
                attrs[key] = value
    return attrs


def extract_hermes_payload(
    data: Mapping[str, Any], *, event_override: str | None = None
) -> HookPayload | StatusPayload | None:
    """Pick the supported fields out of a decoded Hermes shell-hook document.

    Returns ``None`` for an event OpenShard does not subscribe to. Unknown keys
    are ignored and malformed shapes under-report (a tool record with no path
    or command), never raise.
    """
    name = resolve_event_name(data, event_override)
    if name is None or (name not in HERMES_EVENT_MAP and name != USAGE_EVENT):
        return None
    extra = _extra(data)
    if name == USAGE_EVENT:
        return _usage_payload(data, extra)

    event = HERMES_EVENT_MAP[name]
    if name == "on_session_end":
        if extra.get("interrupted") is True:
            event = EVENT_INTERRUPT
        elif extra.get("completed") is not True:
            # A failed or incomplete turn, or an exit path that reports
            # neither flag: a boundary worth a snapshot, never a completed turn.
            event = EVENT_SESSION_IDLE
    payload = HookPayload(
        event=event,
        session_id=_session_id(data),
        cwd=_str_or_none(data.get("cwd"), 1_000),
        agent=AGENT_HERMES,
        model_id=_str_attr(extra, "model", 200),
        provider_id=_str_attr(extra, "provider"),
        tool_success=None,
    )
    if name == "on_session_start":
        payload.source = "startup"
    elif name == "pre_llm_call":
        payload.prompt = _user_message(extra.get("user_message"))
    elif name == "on_session_finalize":
        payload.reason = _str_attr(extra, "reason", 40)
    elif name == "post_tool_call":
        _tool_payload(payload, data, extra)
    elif name in ("subagent_start", "subagent_stop"):
        payload.attrs = _subagent_attrs(name, extra)
    elif name in ("pre_approval_request", "post_approval_response"):
        payload.attrs = _approval_attrs(name, extra)
        payload.command = _str_or_none(extra.get("command"))
    return payload
