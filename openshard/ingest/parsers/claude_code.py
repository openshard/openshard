"""``claude_code_jsonl`` parser: a Claude Code session transcript.

Format (inspected on real Claude Code 2.1.x transcripts, 2026-09): one JSON
object per line. Conversation records have ``type`` ``user`` / ``assistant``
/ ``system`` / ``attachment`` and carry ``sessionId``, ``timestamp``,
``cwd``, ``gitBranch``, ``version``, ``isSidechain``. Assistant records carry
``message.model``, ``message.id``, ``message.usage`` and ``message.content``
items (``text`` / ``thinking`` / ``tool_use{id,name,input}``). Tool results
come back on user records as ``tool_result{tool_use_id,is_error,content}``
with a sibling ``toolUseResult`` object. Metadata records (``mode``,
``permission-mode``, ``ai-title``, ``pr-link``, ``file-history-*`` ...) have
no conversation content.

Only facts the transcript records are recovered. Claude records the branch
but not the HEAD commit, so ``head_at_start`` is never set here (git
enrichment may infer one, labelled ``git_observed``). Sidechain (subagent)
records inside the file fold into this session. Assistant streaming splits
one API message over several records sharing ``message.id``; token usage is
counted once per message id.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import PurePath
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
    iter_json_lines,
    ref,
    span,
    text_str,
)

AGENT = "claude_code"

_CONVERSATION_TYPES = frozenset({"user", "assistant", "system", "attachment"})
_METADATA_TYPES = frozenset({
    "summary", "mode", "permission-mode", "atis-latch", "file-history-snapshot",
    "file-history-delta", "last-prompt", "ai-title", "custom-title", "pr-link",
    "cost-state", "bridge-session", "queue-operation", "agent-name", "tag",
})
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_EDIT_TOOLS: dict[str, str] = {"Edit": "update", "MultiEdit": "update", "Write": "update", "NotebookEdit": "update"}
# User "prompts" that are really client plumbing, not a task.
_NON_PROMPT_PREFIXES = ("<command-", "<local-command", "<system-reminder>", "<bash-", "Caveat:", "[Request interrupted")

LOSS_UNKNOWN_RECORD_TYPE = "unknown_record_type"
LOSS_UNMATCHED_TOOL_RESULT = "unmatched_tool_result"


def normalize_stamp(value: object) -> str | None:
    """An ISO 8601 timestamp as UTC ``...Z``; ``None`` when unparseable (never invented)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # a zoneless stamp cannot be placed on a timeline honestly
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _content_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(p for p in parts if isinstance(p, str))
        return joined or None
    return None


def _is_prompt(text: str | None) -> bool:
    if not text or not text.strip():
        return False
    return not text.lstrip().startswith(_NON_PROMPT_PREFIXES)


class ClaudeCodeParser:
    name = "claude_code_jsonl"
    version = 1

    def sniff(self, head: bytes, obj: SourceObject | None = None) -> float:
        recs = head_records(head)
        if any("payload" in r and r.get("type") in ("session_meta", "response_item", "event_msg") for r in recs):
            return 0.0
        hits = sum(1 for r in recs if isinstance(r.get("sessionId"), str) and isinstance(r.get("type"), str))
        return 0.9 if hits else 0.0

    def peek(self, head: bytes) -> dict:
        out: dict = {}
        for r in head_records(head, limit=200):
            out.setdefault("session_id", r.get("sessionId") if isinstance(r.get("sessionId"), str) else None)
            if isinstance(r.get("cwd"), str) and "cwd" not in out:
                out["cwd"] = r["cwd"]
            stamp = normalize_stamp(r.get("timestamp"))
            if stamp and "start" not in out:
                out["start"] = stamp
        return out

    def parse(
        self, stream: BinaryIO, obj: SourceObject, *, checkpoint: Callable[[], None] | None = None
    ) -> Iterator[ParsedSession]:
        stem = PurePath(obj.locator).stem
        s = ParsedSession(parser=f"{self.name}@{self.version}", agent=AGENT, native_session_id="")
        session_ids: dict[str, int] = {}
        first_line: int | None = None
        last_line: int | None = None
        model_first_line: int | None = None
        usage_by_msg: dict[str, dict] = {}
        usage_lines: list[int] = []
        calls: dict[str, ToolCall] = {}
        saw_conversation = False

        for lineno, rec in iter_json_lines(stream, s, checkpoint=checkpoint):
            rtype = rec.get("type")
            sid = rec.get("sessionId")
            if isinstance(sid, str) and sid:
                session_ids.setdefault(sid, lineno)
            if rtype not in _CONVERSATION_TYPES and rtype not in _METADATA_TYPES:
                s.add_loss(LOSS_UNKNOWN_RECORD_TYPE)
                continue

            stamp = normalize_stamp(rec.get("timestamp"))
            if stamp:
                if s.start is None or stamp < s.start:
                    s.start, first_line = stamp, lineno
                if s.end is None or stamp >= s.end:
                    s.end, last_line = stamp, lineno

            if rtype in _CONVERSATION_TYPES:
                saw_conversation = True
                if s.cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                    s.cwd, s.cwd_ref = rec["cwd"], ref(lineno)
                if s.branch is None and isinstance(rec.get("gitBranch"), str) and rec["gitBranch"]:
                    s.branch, s.branch_ref = rec["gitBranch"], ref(lineno)
                if s.agent_version is None and isinstance(rec.get("version"), str):
                    s.agent_version = rec["version"]

            if rtype == "permission-mode" and isinstance(rec.get("permissionMode"), str):
                if s.approval_policy is None:
                    s.approval_policy, s.approval_ref = rec["permissionMode"], ref(lineno)
            elif rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
                s.title, s.title_ref = rec["aiTitle"], ref(lineno)
            elif rtype == "pr-link" and isinstance(rec.get("prUrl"), str):
                s.pr_url, s.pr_ref = rec["prUrl"], ref(lineno)
            elif rtype == "user":
                self._user(rec, lineno, s, calls)
            elif rtype == "assistant":
                msg = as_dict(rec.get("message"))
                model = msg.get("model")
                if isinstance(model, str) and model and not model.startswith("<"):
                    if model not in s.models:
                        s.models.append(model)
                    model_first_line = model_first_line or lineno
                usage = msg.get("usage")
                mid = as_str(msg.get("id")) or f"line{lineno}"
                if isinstance(usage, dict):
                    usage_by_msg[mid] = usage
                    usage_lines.append(lineno)
                self._assistant(msg, rec, lineno, s, calls)

        if not saw_conversation:
            if not session_ids and s.record_count == 0:
                raise ParseError("not_a_claude_code_session")
            # Metadata-only file (e.g. a session that never got a prompt).
        s.native_session_id = stem if stem in session_ids else (next(iter(session_ids), "") or "")
        if not s.native_session_id:
            raise ParseError("no_session_id")
        s.session_ref = ref(session_ids.get(s.native_session_id))
        s.window_ref = span(first_line, last_line)
        s.model_ref = ref(model_first_line)
        if usage_by_msg:
            s.tokens = _sum_usage(usage_by_msg.values())
            s.tokens_ref = span(min(usage_lines), max(usage_lines))
        s.tool_calls = list(calls.values())
        yield s

    def _user(self, rec: dict, lineno: int, s: ParsedSession, calls: dict[str, ToolCall]) -> None:
        msg = as_dict(rec.get("message"))
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            tur = as_dict(rec.get("toolUseResult"))
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                call = calls.get(str(item.get("tool_use_id")))
                if call is None:
                    s.add_loss(LOSS_UNMATCHED_TOOL_RESULT)
                    continue
                call.outcome_ref = ref(lineno)
                if tur.get("interrupted") is True or tur.get("backgroundTaskId"):
                    call.outcome = OUTCOME_NOT_COMPLETED
                elif item.get("is_error") is True:
                    call.outcome = OUTCOME_FAILED
                elif item.get("is_error") in (False, None):
                    # Messages API: ``is_error`` is optional and defaults to
                    # false; Claude Code omits it on many successful results.
                    call.outcome = OUTCOME_PASSED
                if call.name in _SHELL_TOOLS and call.command and "commit" in call.command:
                    output = _content_text(item.get("content")) or text_str(tur.get("stdout"))
                    call.commit_shas = git_commit_shas(output)
                if call.name in _EDIT_TOOLS and tur.get("type") in ("create", "update") and call.paths:
                    call.paths = [(p, str(tur["type"])) for p, _ in call.paths]
            return
        if rec.get("isMeta") is True or rec.get("isSidechain") is True:
            return
        text = _content_text(content)
        if _is_prompt(text):
            s.turns += 1
            if s.task is None:
                s.task, s.task_ref = text, ref(lineno)

    def _assistant(self, msg: dict, rec: dict, lineno: int, s: ParsedSession, calls: dict[str, ToolCall]) -> None:
        content = msg.get("content")
        if not isinstance(content, list):
            return
        stamp = normalize_stamp(rec.get("timestamp"))
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text" and isinstance(item.get("text"), str) and item["text"].strip():
                if rec.get("isSidechain") is not True:
                    s.final_message, s.final_message_ref = item["text"], ref(lineno)
                for sha in claimed_commit_shas(item["text"]):
                    if sha not in s.claimed_shas:
                        s.claimed_shas.append(sha)
            elif itype == "tool_use":
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                tool_input = as_dict(item.get("input"))
                call = ToolCall(call_id=as_str(item.get("id")),
                                name=name, ref=ref(lineno) or "", at=stamp, cwd=rec.get("cwd"))
                if name in _SHELL_TOOLS:
                    call.command = text_str(tool_input.get("command"), 4_000)
                elif name in _EDIT_TOOLS:
                    path = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if isinstance(path, str) and path:
                        call.paths = [(path, _EDIT_TOOLS[name])]
                calls[call.call_id or f"line{lineno}:{len(calls)}"] = call


def _sum_usage(usages) -> dict[str, int]:
    keys = {
        "input_tokens": "input",
        "output_tokens": "output",
        "cache_read_input_tokens": "cache_read",
        "cache_creation_input_tokens": "cache_creation",
    }
    total: dict[str, int] = {}
    for usage in usages:
        for src, dst in keys.items():
            v = usage.get(src)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                total[dst] = total.get(dst, 0) + v
    return total
