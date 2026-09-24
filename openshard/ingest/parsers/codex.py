"""``codex_rollout`` parser: a Codex CLI rollout file.

Format (inspected on real Codex CLI rollouts, 2026-05..09): one JSON object
per line, ``{"timestamp", "type", "payload"}``.

* ``session_meta``: ``id``, ``timestamp``, ``cwd``, ``cli_version``,
  ``model_provider`` and, when Codex could read it, ``git{commit_hash,
  branch, repository_url}`` -- the HEAD at session start, recorded by Codex.
* ``turn_context``: ``model``, ``approval_policy``, ``sandbox_policy``.
* ``event_msg``: ``user_message{message}``, ``agent_message{message}``,
  ``exec_command_end{call_id, exit_code, status}``, ``patch_apply_end{call_id,
  success, changes{abs_path: {type}}}``, ``token_count{info.total_token_usage}``,
  ``task_started`` / ``task_complete{last_agent_message}`` and others.
* ``response_item``: ``message``, ``reasoning``, ``function_call{name,
  arguments(JSON string), call_id}``, ``function_call_output{call_id,
  output}``, ``custom_tool_call{name, input, call_id}`` (``apply_patch``).

A rollout may contain more than one ``session_meta`` (resumed/forked
threads); the first one identifies the session. Unknown ``type`` /
``payload.type`` values are counted as losses, never guessed at.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import BinaryIO

from openshard.ingest.model import (
    OUTCOME_FAILED,
    OUTCOME_NOT_COMPLETED,
    OUTCOME_PASSED,
    ParsedSession,
    SourceObject,
    ToolCall,
)
from openshard.ingest.parsers.base import (
    ParseError,
    as_dict,
    as_str,
    claimed_commit_shas,
    git_commit_shas,
    head_records,
    is_sha,
    iter_json_lines,
    ref,
    span,
    text_str,
)
from openshard.ingest.parsers.claude_code import normalize_stamp

AGENT = "codex"

_TOP_TYPES = frozenset({"session_meta", "turn_context", "event_msg", "response_item",
                        "world_state", "token_usage_record", "compacted"})
_EVENT_TYPES = frozenset({
    "user_message", "agent_message", "exec_command_end", "exec_command_begin", "patch_apply_end",
    "patch_apply_begin", "token_count", "task_started", "task_complete", "turn_aborted",
    "item_completed", "thread_name_updated", "mcp_tool_call_end", "mcp_tool_call_begin",
    "thread_settings_applied", "agent_reasoning", "agent_reasoning_raw_content", "error",
    "stream_error", "background_event", "turn_diff", "entered_review_mode", "exited_review_mode",
    "web_search_end", "view_image_tool_call", "context_compacted",
})
_ITEM_TYPES = frozenset({
    "message", "reasoning", "function_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "tool_search_call", "tool_search_output", "local_shell_call",
    "web_search_call", "compaction",
})
_SHELL_FUNCTIONS = frozenset({"shell", "shell_command", "exec_command", "container.exec", "local_shell"})
_PATCH_TOOLS = frozenset({"apply_patch"})
_PATCH_HEADER_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.MULTILINE)
_PATCH_KIND = {"Add": "create", "Update": "update", "Delete": "delete"}
_CHANGE_KIND = {"add": "create", "update": "update", "delete": "delete"}
# User messages Codex injects itself rather than typed by a person.
_NON_PROMPT_PREFIXES = ("<environment_context>", "<user_instructions>", "<turn_aborted>", "# AGENTS.md")

LOSS_UNKNOWN_RECORD_TYPE = "unknown_record_type"
LOSS_UNMATCHED_TOOL_RESULT = "unmatched_tool_result"


def _command_text(value: object) -> str | None:
    if isinstance(value, str):
        return value[:4_000] or None
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        # ["bash", "-lc", "pytest -q"] -> the script; otherwise the joined argv.
        if len(value) >= 3 and value[1] in ("-lc", "-c"):
            return value[2][:4_000] or None
        return " ".join(value)[:4_000] or None
    return None


class CodexParser:
    name = "codex_rollout"
    version = 1

    def sniff(self, head: bytes, obj: SourceObject | None = None) -> float:
        recs = head_records(head, limit=5)
        for r in recs:
            payload = r.get("payload")
            if r.get("type") == "session_meta" and isinstance(payload, dict) and isinstance(payload.get("id"), str):
                return 0.95
        if any(r.get("type") in ("response_item", "event_msg") and isinstance(r.get("payload"), dict) for r in recs):
            return 0.6
        return 0.0

    def peek(self, head: bytes) -> dict:
        for r in head_records(head, limit=5):
            p = r.get("payload")
            if r.get("type") == "session_meta" and isinstance(p, dict):
                return {"session_id": p.get("id"), "cwd": p.get("cwd"), "start": normalize_stamp(p.get("timestamp"))}
        return {}

    def parse(
        self, stream: BinaryIO, obj: SourceObject, *, checkpoint: Callable[[], None] | None = None
    ) -> Iterator[ParsedSession]:
        s = ParsedSession(parser=f"{self.name}@{self.version}", agent=AGENT, native_session_id="")
        calls: dict[str, ToolCall] = {}
        first_line: int | None = None
        last_line: int | None = None
        saw_meta = False
        fallback_prompts: list[tuple[str, str | None]] = []

        for lineno, rec in iter_json_lines(stream, s, checkpoint=checkpoint):
            rtype = rec.get("type")
            payload = as_dict(rec.get("payload"))
            if rtype not in _TOP_TYPES:
                s.add_loss(LOSS_UNKNOWN_RECORD_TYPE)
                continue
            stamp = normalize_stamp(rec.get("timestamp"))
            if stamp:
                if s.start is None or stamp < s.start:
                    s.start, first_line = stamp, lineno
                if s.end is None or stamp >= s.end:
                    s.end, last_line = stamp, lineno

            if rtype == "session_meta":
                if not saw_meta:
                    saw_meta = True
                    self._meta(payload, lineno, s)
            elif rtype == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model and model not in s.models:
                    s.models.append(model)
                    s.model_ref = s.model_ref or ref(lineno)
                if s.approval_policy is None and isinstance(payload.get("approval_policy"), str):
                    s.approval_policy, s.approval_ref = payload["approval_policy"], ref(lineno)
                if s.cwd is None and isinstance(payload.get("cwd"), str):
                    s.cwd, s.cwd_ref = payload["cwd"], ref(lineno)
            elif rtype == "event_msg":
                self._event(payload, lineno, stamp, s, calls)
            elif rtype == "response_item":
                if payload.get("type") == "message" and payload.get("role") == "user":
                    text = "\n".join(
                        c["text"] for c in payload.get("content") or []
                        if isinstance(c, dict) and isinstance(c.get("text"), str)
                    )
                    if text.strip() and not text.lstrip().startswith(_NON_PROMPT_PREFIXES):
                        fallback_prompts.append((text, ref(lineno)))
                self._item(payload, lineno, stamp, s, calls)

        if s.task is None and fallback_prompts:
            # Newer rollouts carry no ``user_message`` event; the typed prompt
            # is then only a ``response_item`` user message.
            s.turns = len(fallback_prompts)
            s.task, s.task_ref = fallback_prompts[0]
        if not saw_meta:
            raise ParseError("no_session_meta")
        if not s.native_session_id:
            raise ParseError("no_session_id")
        s.window_ref = span(first_line, last_line)
        s.tool_calls = list(calls.values())
        yield s

    def _meta(self, p: dict, lineno: int, s: ParsedSession) -> None:
        sid = p.get("id")
        if isinstance(sid, str) and sid:
            s.native_session_id, s.session_ref = sid, ref(lineno)
        if isinstance(p.get("cwd"), str) and p["cwd"]:
            s.cwd, s.cwd_ref = p["cwd"], ref(lineno)
        if isinstance(p.get("cli_version"), str):
            s.agent_version = p["cli_version"]
        if isinstance(p.get("model_provider"), str) and p["model_provider"]:
            s.provider, s.provider_ref = p["model_provider"], ref(lineno)
        git = as_dict(p.get("git"))
        if is_sha(git.get("commit_hash")):
            s.head_at_start, s.head_ref = git["commit_hash"].lower(), ref(lineno)
        if isinstance(git.get("branch"), str) and git["branch"]:
            s.branch, s.branch_ref = git["branch"], ref(lineno)
        if isinstance(git.get("repository_url"), str) and git["repository_url"]:
            s.repository_url, s.repository_ref = git["repository_url"], ref(lineno)

    def _event(self, p: dict, lineno: int, stamp: str | None, s: ParsedSession, calls: dict[str, ToolCall]) -> None:
        ptype = p.get("type")
        if ptype not in _EVENT_TYPES:
            s.add_loss(LOSS_UNKNOWN_RECORD_TYPE)
            return
        if ptype == "user_message":
            text = p.get("message")
            if isinstance(text, str) and text.strip() and not text.lstrip().startswith(_NON_PROMPT_PREFIXES):
                s.turns += 1
                if s.task is None:
                    s.task, s.task_ref = text, ref(lineno)
        elif ptype == "agent_message":
            self._final(p.get("message"), lineno, s)
        elif ptype == "task_complete":
            self._final(p.get("last_agent_message"), lineno, s)
        elif ptype == "token_count":
            info = as_dict(p.get("info"))
            total = as_dict(info.get("total_token_usage"))
            if total:
                tokens = {}
                for src, dst in (("input_tokens", "input"), ("output_tokens", "output"),
                                 ("cached_input_tokens", "cache_read")):
                    v = total.get(src)
                    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                        tokens[dst] = v
                if tokens:
                    # Cumulative counter: the last report is the session total.
                    s.tokens, s.tokens_ref = tokens, ref(lineno)
        elif ptype == "exec_command_end":
            call = calls.get(str(p.get("call_id")))
            if call is None:
                call = ToolCall(call_id=str(p.get("call_id")), name="exec_command", ref=ref(lineno) or "",
                                at=stamp, command=_command_text(p.get("command")), cwd=p.get("cwd"))
                calls[call.call_id or f"line{lineno}"] = call
            code = p.get("exit_code")
            call.outcome_ref = ref(lineno)
            if isinstance(code, int) and not isinstance(code, bool):
                call.exit_code = code
                completed = p.get("status") in (None, "completed", "failed")
                call.outcome = (OUTCOME_PASSED if code == 0 else OUTCOME_FAILED) if completed else OUTCOME_NOT_COMPLETED
            else:
                call.outcome = OUTCOME_NOT_COMPLETED
            if call.command and "commit" in call.command:
                call.commit_shas = git_commit_shas(p.get("aggregated_output") or p.get("stdout"))
        elif ptype == "patch_apply_end":
            call = calls.get(str(p.get("call_id")))
            changes = as_dict(p.get("changes"))
            if call is None:
                call = ToolCall(call_id=str(p.get("call_id")), name="apply_patch", ref=ref(lineno) or "", at=stamp)
                calls[call.call_id or f"line{lineno}"] = call
            if changes:
                call.paths = [
                    (path, _CHANGE_KIND.get(str((v or {}).get("type")), "update") if isinstance(v, dict) else "update")
                    for path, v in changes.items() if isinstance(path, str)
                ]
            call.outcome_ref = ref(lineno)
            if p.get("success") is True:
                call.outcome = OUTCOME_PASSED
            elif p.get("success") is False:
                call.outcome = OUTCOME_FAILED

    def _item(self, p: dict, lineno: int, stamp: str | None, s: ParsedSession, calls: dict[str, ToolCall]) -> None:
        ptype = p.get("type")
        if ptype not in _ITEM_TYPES:
            s.add_loss(LOSS_UNKNOWN_RECORD_TYPE)
            return
        if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = as_str(p.get("name")) or ("local_shell" if ptype == "local_shell_call" else None)
            if not name:
                return
            call_id = as_str(p.get("call_id")) or f"line{lineno}"
            call = calls.get(call_id) or ToolCall(call_id=call_id, name=name, ref=ref(lineno) or "", at=stamp)
            call.name, call.ref, call.at = name, ref(lineno) or "", stamp
            args: dict = {}
            if ptype == "function_call" and isinstance(p.get("arguments"), str):
                try:
                    loaded = json.loads(p["arguments"])
                    args = loaded if isinstance(loaded, dict) else {}
                except ValueError:
                    s.add_loss("malformed_tool_arguments")
            elif ptype == "local_shell_call" and isinstance(p.get("action"), dict):
                args = p["action"]
            if name in _SHELL_FUNCTIONS:
                call.command = call.command or _command_text(args.get("command") or args.get("cmd"))
                call.cwd = as_str(args.get("workdir")) or call.cwd
            elif name in _PATCH_TOOLS and not call.paths:
                patch = as_str(p.get("input")) or as_str(args.get("input"))
                if isinstance(patch, str):
                    call.paths = [(m.group(2).strip(), _PATCH_KIND[m.group(1)]) for m in _PATCH_HEADER_RE.finditer(patch)]
            calls[call_id] = call
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            result_call = calls.get(str(p.get("call_id")))
            if result_call is None:
                s.add_loss(LOSS_UNMATCHED_TOOL_RESULT)
                return
            output = p.get("output")
            # Older rollouts wrap shell results as {"output": str, "metadata": {"exit_code": n}}.
            if isinstance(output, str) and output.startswith("{"):
                try:
                    loaded = json.loads(output)
                except ValueError:
                    loaded = None
                if isinstance(loaded, dict):
                    meta = as_dict(loaded.get("metadata"))
                    code = meta.get("exit_code")
                    if result_call.exit_code is None and isinstance(code, int) and not isinstance(code, bool):
                        result_call.exit_code, result_call.outcome_ref = code, ref(lineno)
                        result_call.outcome = OUTCOME_PASSED if code == 0 else OUTCOME_FAILED
                    output = loaded.get("output")
            if result_call.command and "commit" in result_call.command and not result_call.commit_shas:
                result_call.commit_shas = git_commit_shas(text_str(output))

    def _final(self, text: object, lineno: int, s: ParsedSession) -> None:
        if isinstance(text, str) and text.strip():
            s.final_message, s.final_message_ref = text, ref(lineno)
            for sha in claimed_commit_shas(text):
                if sha not in s.claimed_shas:
                    s.claimed_shas.append(sha)
