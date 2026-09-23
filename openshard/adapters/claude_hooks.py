"""Claude Code hook capture for OpenShard (Demo v1 PR5).

Turns the JSON payloads Claude Code's official *hooks* deliver on stdin into
canonical OpenShard Events, and folds one Claude Code work session into one
normal ``.openshard/runs.jsonl`` run record (Run/Attempt -> Shard -> Receipt)
-- no manual ``openshard import claude`` / ``openshard wrap claude`` step.

    claude (normal use)
      -> Claude Code fires SessionStart / UserPromptSubmit / PostToolUse /
         PostToolUseFailure / Stop / SessionEnd hooks
      -> each runs `openshard hooks claude` with the hook JSON on stdin
      -> this module: parse -> sanitize -> canonical Events
      -> per-session staging buffer (.openshard/claude_sessions/<id>.json)
      -> at every Stop / SessionEnd: one snapshot record upserted into
         .openshard/runs.jsonl (same record shape as claude_code_import)

Session boundary (Demo v1, deliberately conservative)
-----------------------------------------------------
One Claude Code *session* (Claude's own ``session_id``) becomes one new
OpenShard Shard, attempt 1, created the first time the session shows work
(a user prompt). This is the most trustworthy deterministic boundary the
hook lifecycle offers today; it is **not** a claim that a Claude session
*is* a Shard. A Shard is a meaningful engineering task -- a session may
contain several tasks, and one task may span several sessions. Nothing
here groups sessions by prompt text or timing: task-level grouping can be
layered on later by attaching an entry to an existing ``shard_id`` through
``run_attempt.resolve_shard_for_attempt`` without changing the Event model.
Claude's ``session_id`` is preserved as ``capture.session_id`` metadata; it
is never the Shard identity itself (``shard_id`` is minted exactly like the
import/wrap adapters do, via ``_make_shard_id``).

Evidence honesty
----------------
* Lifecycle facts OpenShard's own hook process was invoked for (session
  started / prompt submitted / turn finished / session ended) ->
  ``EVIDENCE_DIRECTLY_OBSERVED``: OpenShard itself observed the hook fire.
* Claims relayed *inside* the payload (tool X ran on file Y, a Bash command
  ran) -> ``EVIDENCE_AGENT_REPORTED``: Claude Code reported them; OpenShard
  did not execute or verify them. A Bash test command is recorded as a tool
  invocation, never as a verification result -- OpenShard did not run it.
* Files from ``git diff`` (against the HEAD snapshotted at session start,
  so commits made during the session are still seen) ->
  ``EVIDENCE_GIT_OBSERVED``.
* Session end with no verification -> ``run.completed`` with status
  ``unknown``. Never "passed". The Shard stays ``external_observed`` /
  ``partial`` capture depth (see ``shard.derive_shard_identity``).

What is stored / not stored
---------------------------
Stored: Claude ``session_id`` (regex-validated), timestamps, hook event
names, tool names, repo-relative file paths, a secret-scrubbed bounded
excerpt of the *first* user prompt as the Shard task (the same thing
``import claude --task`` asks the user to type), a scrubbed bounded Bash
command summary, prompt/tool/turn counts, git branch/HEAD/dirty state.
Never stored: transcripts or ``transcript_path``, full prompts, later
prompts, assistant messages (``last_assistant_message``), tool responses,
tool errors, file contents, environment variables, absolute paths, or
anything matching the secret scrubber.

Staging buffer
--------------
``.openshard/claude_sessions/<session_id>.json`` holds a session's
not-yet-final state so per-tool hooks stay O(1) (no history load, no
runs.jsonl rewrite per tool call). It is transient working state, deleted
at SessionEnd, and never read by any query/receipt path: the only durable
Event location remains the ``events`` field of the ``runs.jsonl`` record,
exactly as for import/wrap. If SessionEnd never fires (crash, kill), the
last Stop snapshot already in ``runs.jsonl`` is kept as-is with
``capture.session_end_observed = False`` -- honest partial capture, never
fabricated completion.

Public API never raises and never writes to stdout (Claude Code treats
hook stdout specially for some events); diagnostics go to stderr only.

Latency (PR7)
-------------
The status line (``handle_claude_status``) is Claude Code's most frequent,
synchronous entrypoint into this module, so it never folds: it only
updates the staging buffer above and lets the next real fold boundary
(a throttled tool-hook snapshot, ``Stop``, or ``SessionEnd``) pick up the
model/cost/token values it observed. A fold's git-identity lookup
(``git config --get remote.origin.url``) is cached on the buffer and
computed at most once per session. Both the hook and status-line handlers
acquire the buffer's lock with a bounded timeout, never Claude Code's
unbounded wait -- see ``docs/capture-performance.md`` for what was
measured and why.

Near-zero blocking capture (PR9.5)
----------------------------------
In normal operation this module no longer runs inside the process Claude
Code is waiting on. Claude Code's HTTP hooks POST each payload to the warm
local capture service (``adapters/claude_capture_service.py``), whose
blocking path only validates, *reduces* the payload to the privacy-safe
shape below, appends it to a per-session queue file (fsync) and returns.
A background worker then replays the queue through exactly the same
``_apply`` / ``_fold`` code as before, so every semantic documented above
(evidence levels, fold boundaries, buffer lifecycle, receipt shape) is
unchanged -- only *when* it runs changed.

``ReducedHookPayload`` is the only representation of a hook that is ever
persisted outside ``runs.jsonl``: it carries the already-scrubbed task
excerpt, the repo-relative file target and the summarized command, never
the raw prompt, absolute path or raw command text. Replays are idempotent
(``dedup_id``) and carry the time the hook was *received* (``at``), so a
queue replayed after a crash still records the right timestamps.
``handle_claude_hook`` / ``handle_claude_status`` remain the synchronous
in-process path and are used as the fallback when no service is reachable.

Codex and OpenCode (PR12)
-------------------------
The fold logic in this module is agent-neutral: Codex hooks
(``adapters/codex_hooks.py``) and the OpenCode plugin
(``adapters/opencode_plugin.py``) translate their own payloads into the very
same ``HookPayload`` / ``StatusPayload`` shapes, and everything from
``reduce_hook_payload`` on -- the queue line, the staging buffer, the fold,
the receipt fields -- is shared. What differs per agent (labels, executor,
Event source/actor, task placeholder) comes from one static table,
``adapters/capture_agents.py``; the buffer remembers its ``agent`` and every
label is looked up from it, so no branch below tests an agent name. Agent
identity is preserved on the record (``executor``, ``capture.agent``,
``capture.agent_vendor``, ``capture.provider``) and is never collapsed:
an OpenCode session stays "OpenCode" even when its provider/model are
known, and a Codex session records the model slug Codex reports without
inventing cost or token figures Codex does not expose.

Fail-closed tool semantics across agents: a ``tool.invoked`` Event for a
file tool is ``passed`` -- and its path joins the hook-reported file list
used when git is unavailable -- only when the *translator* attached a
positive success signal (``HookPayload.tool_success``). Claude Code's
``PostToolUse`` is documented to fire only after a tool completed
successfully (failures go to ``PostToolUseFailure``), so its translator
sets that flag; Codex's ``PostToolUse`` also fires for failed shell
commands and OpenCode's ``tool.execute.after`` carries no outcome, so
theirs never do and their file tools stay ``unknown``. OpenCode's own
``file.edited`` event (published only after a successful write) is the
positive signal that feeds its hook-reported file list. Git-observed
changes are evidence on their own regardless of agent. OpenCode's
``session.idle`` is a neutral activity boundary (``SessionIdle``): it
snapshots the record but never counts as a completed turn.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from openshard.adapters.capture_agents import (
    AGENT_CLAUDE_CODE,
    CLAUDE_CODE_PROFILE,
    AgentProfile,
    agent_for_executor,
    is_known_agent,
    profile_for,
)
from openshard.history.capture_completeness import (
    REASON_SESSION_END_NOT_OBSERVED,
    build_completeness,
    make_reason,
)
from openshard.history.task_title import derive_task_title
from openshard.util.git import run_git

# Claude Code identity constants -- kept as module names for existing
# callers/tests; the values are the Claude profile's (adapters/capture_agents.py).
EXECUTOR = CLAUDE_CODE_PROFILE.executor
IMPORT_SOURCE = CLAUDE_CODE_PROFILE.import_source
IMPORT_METHOD = CLAUDE_CODE_PROFILE.import_method
CAPTURE_SOURCE = CLAUDE_CODE_PROFILE.capture_source
IMPORT_NOTE = CLAUDE_CODE_PROFILE.import_note

SESSIONS_DIRNAME = "claude_sessions"
BUFFER_SCHEMA_VERSION = 1

EVENT_SESSION_START = "SessionStart"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
EVENT_STOP = "Stop"
EVENT_SESSION_END = "SessionEnd"
# PR12: two canonical lifecycle events Claude Code never emits but Codex
# (``Interrupt``) and OpenCode (``file.edited`` -> ``FileEdited``) do. They
# are part of the neutral vocabulary every agent translator targets.
EVENT_INTERRUPT = "Interrupt"
EVENT_FILE_EDITED = "FileEdited"
# A neutral "the session went idle" boundary (OpenCode ``session.idle``).
# Unlike ``Stop`` it proves neither that an assistant turn completed nor
# that anything succeeded, so it only snapshots the record.
EVENT_SESSION_IDLE = "SessionIdle"
# 0.4.7: the agent invoked its model (Google Antigravity ``PreInvocation``).
# Proves the agent did work in this session and names the model it used for
# that invocation; it is neither a user prompt nor a completed turn.
EVENT_MODEL_INVOCATION = "ModelInvocation"
# 0.4.7 (Hermes Agent): a subagent the agent delegated work to started /
# stopped, and a human-approval gate was raised / answered. Agent-reported
# facts about the session; they are never work (``_has_activity``) on their own.
EVENT_SUBAGENT_START = "SubagentStart"
EVENT_SUBAGENT_STOP = "SubagentStop"
EVENT_APPROVAL_REQUEST = "ApprovalRequest"
EVENT_APPROVAL_DECISION = "ApprovalDecision"
SUPPORTED_HOOK_EVENTS: tuple[str, ...] = (
    EVENT_SESSION_START,
    EVENT_USER_PROMPT_SUBMIT,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_STOP,
    EVENT_SESSION_END,
    EVENT_INTERRUPT,
    EVENT_FILE_EDITED,
    EVENT_SESSION_IDLE,
    EVENT_MODEL_INVOCATION,
    EVENT_SUBAGENT_START,
    EVENT_SUBAGENT_STOP,
    EVENT_APPROVAL_REQUEST,
    EVENT_APPROVAL_DECISION,
)

# Tools whose tool_input.file_path names a file Claude Code says it changed.
FILE_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# Local agent/OpenShard state is never a task's work (see _git_changed_files).
# ``.agents/`` also holds a user's shared rules/workflows, so only
# Antigravity's hook configuration file is excluded from it.
_LOCAL_STATE_PREFIXES: tuple[str, ...] = (
    ".openshard/", ".claude/", ".codex/", ".opencode/", ".cursor/", ".agents/hooks.json",
)
_MAX_ATTRS = 12  # small scalar facts one payload may carry (see _clean_attrs)
_ATTR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Which ``HookPayload.attrs`` reach an Event's metadata, per event family
# (``sanitize_metadata`` keeps at most ten keys, so each list is short).
_TOOL_ATTR_KEYS: tuple[str, ...] = ("duration_ms", "tool_status", "tool_call_id", "turn_id")
_SUBAGENT_ATTR_KEYS: tuple[str, ...] = (
    "child_role", "child_status", "duration_ms", "tool_calls", "child_subagent_id",
    "child_session_id", "parent_subagent_id",
)
_APPROVAL_ATTR_KEYS: tuple[str, ...] = (
    "choice", "surface", "pattern_key", "decided_by", "tool_call_id", "turn_id",
)
# Hermes' ``post_approval_response.choice`` values. A grant lets the command
# run; ``deny`` / ``smart_deny`` is a refusal; the rest mean nobody answered
# (or the prompt could not be delivered), so the command did not run and no
# one refused it either.
_APPROVAL_GRANTED = frozenset({"once", "session", "always", "smart_approve"})
_APPROVAL_DENIED = frozenset({"deny", "smart_deny"})
# v0.4.4 change attribution (see _snapshot_baseline / _classify_changed_files).
_BASELINE_MAX_PATHS = 500  # dirty/untracked paths remembered at session start
_GIT_DIFF_MAX_FILES = 200  # git diff rows examined at fold (pre-existing ones are then excluded)
_MAX_REPORTED_FILES = 50  # files counted as this session's changes, on the record
_MAX_EXCLUDED_FILES = 50  # excluded (pre-existing / other-session) files kept for provenance
_MAX_OTHER_BUFFERS = 20  # sibling session buffers consulted for other-session attribution
ATTR_AGENT_REPORTED = "agent_reported"
ATTR_GIT_OBSERVED = "git_observed"
ATTR_PRE_EXISTING = "pre_existing"
ATTR_OTHER_SESSION = "other_session"
_EXCLUDED_ATTRIBUTIONS = frozenset({ATTR_PRE_EXISTING, ATTR_OTHER_SESSION})
COMMAND_TOOLS: frozenset[str] = frozenset({"Bash"})
# Agent-neutral tool classification carried on the reduced payload.
TOOL_KIND_FILE = "file"
TOOL_KIND_COMMAND = "command"
TOOL_KIND_OTHER = "other"
# 0.4.7: a tool that reads one file or directory (Antigravity ``view_file``,
# ``list_dir``...). Its target is the repo-relative path read; it is never a
# change and never an attempted edit.
TOOL_KIND_READ = "read"
_TOOL_KINDS = frozenset({TOOL_KIND_FILE, TOOL_KIND_COMMAND, TOOL_KIND_OTHER, TOOL_KIND_READ})

_TASK_CAP = 300
_TASK_PLACEHOLDER = CLAUDE_CODE_PROFILE.task_placeholder
_COMMAND_CAP = 100
_PATH_CAP = 200
_MAX_BUFFERED_EVENTS = 200
_MAX_HOOK_FILES = 50
_MAX_FILE_TARGETS = 20  # files one tool call (e.g. a Codex apply_patch) may name
_MAX_USAGE_KEYS = 200  # per-message usage reports remembered per session (OpenCode)
_MAX_TOOL_FILE_EVENTS = 20  # fallback file.changed events when git is unavailable
# Tool hooks normally only stage; at most one runs.jsonl snapshot per this
# many seconds is taken from a tool hook, so an interrupted turn (Stop never
# fires on user interrupt) loses at most this window of tool evidence.
_TOOL_FOLD_INTERVAL_SECONDS = 30
# A buffer idle this long whose session never ended is folded (and removed)
# by the next SessionStart in the repo, so a crashed/killed session's staged
# evidence still reaches runs.jsonl. Deliberately generous: an idle-but-live
# session is only ever snapshotted, never marked ended.
_STALE_BUFFER_SECONDS = 60 * 60
_MAX_STALE_SWEEP = 20

# Requirement: hook processing must never hang Claude Code on lock
# contention (a stuck/contended sidecar lock, e.g. antivirus scanning the
# .openshard directory, or an unusually slow concurrent hook). Every lock
# acquisition and JSONL write on this module's hot path is bounded by this
# timeout; on expiry the caller's existing top-level `except Exception`
# fails the single capture open (skips it, diagnostics to stderr) rather
# than blocking the hook -- and therefore Claude Code -- indefinitely. Not
# used for the (rarer, already-async) stale-buffer sweep, which gets a
# somewhat longer allowance since it is not gating a single hook's turn.
_LOCK_TIMEOUT_SECONDS = 3.0
_SWEEP_LOCK_TIMEOUT_SECONDS = 5.0

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_FIRST_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
_TEST_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|(])(?:pytest|py\.test|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|go\s+test|"
    r"cargo\s+test|jest|vitest|mocha|unittest|make\s+test|rspec|dotnet\s+test|mvn\s+test|"
    r"gradle\w*\s+test|tox|nox)(?:\s|$)",
    re.IGNORECASE,
)
_LINT_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|(])(?:ruff|mypy|flake8|pylint|eslint|tsc|prettier|black|isort|gofmt|"
    r"golangci-lint|cargo\s+(?:clippy|fmt)|terraform\s+(?:fmt|validate)|tflint)(?:\s|$)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_since(stamp: object, now: datetime | None = None) -> float | None:
    """Seconds elapsed since an OpenShard UTC timestamp string (until *now*,
    default the current time); None if unparsable."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ((now or datetime.now(UTC)) - then).total_seconds()


def _diag(message: str) -> None:
    """stderr-only diagnostic. Never stdout."""
    try:
        sys.stderr.write(f"[openshard hooks] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Payload parsing -- only known fields, everything else ignored
# ---------------------------------------------------------------------------


@dataclass
class HookPayload:
    """The subset of a Claude Code hook payload OpenShard reads.

    ``prompt`` and ``command`` are held only long enough to derive a
    scrubbed, bounded excerpt; the raw strings are never written anywhere.
    """

    event: str
    session_id: str | None
    cwd: str | None
    source: str | None = None  # SessionStart: startup|resume|clear|compact|fork
    reason: str | None = None  # SessionEnd: clear|resume|logout|prompt_input_exit|other
    prompt: str | None = None  # UserPromptSubmit
    tool_name: str | None = None  # PostToolUse / PostToolUseFailure
    file_path: str | None = None  # tool_input.file_path for file tools
    command: str | None = None  # tool_input.command for Bash
    stop_hook_active: bool = False  # Stop
    # PR12 -- filled by the Codex / OpenCode translators, never by Claude Code:
    agent: str = AGENT_CLAUDE_CODE
    tool_kind: str | None = None  # file | command | other; None = classify by Claude tool name
    file_paths: list[tuple[str, str]] = field(default_factory=list)  # (raw path, change_type)
    model_id: str | None = None  # model slug the agent itself reports on the hook
    provider_id: str | None = None  # model provider id, only when the agent exposes it
    # Positive success signal for this tool call, set only by a translator
    # whose provider *documents* that the event fires exclusively after a
    # successful tool run (Claude Code's PostToolUse). None = not known;
    # the fold then never marks the call ``passed`` nor trusts its paths.
    tool_success: bool | None = None
    # 0.4.7: small scalar facts a translator read off the payload (durations,
    # correlation ids, subagent role/status, approval choice...). Bounded and
    # sanitised by ``_clean_attrs`` when reduced; never free-form content.
    attrs: dict[str, Any] = field(default_factory=dict)


def parse_hook_payload(raw: object) -> dict | None:
    """Decode hook stdin into a dict. Returns None for empty/malformed/non-object input."""
    if raw is None:
        return None
    if isinstance(raw, bytes | bytearray):
        try:
            text = bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            return None
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _str_or_none(value: object, limit: int = 4_000) -> str | None:
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def _clean_attrs(raw: object) -> dict[str, Any]:
    """Bounded scalar attributes: snake_case keys, bool/int/float/short-string values."""
    if not isinstance(raw, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if len(clean) >= _MAX_ATTRS:
            break
        if not isinstance(key, str) or not _ATTR_KEY_RE.match(key):
            continue
        if isinstance(value, bool | int | float):
            clean[key] = value
        elif isinstance(value, str) and value:
            clean[key] = value[:80]
    return clean


def extract_hook_payload(data: Mapping[str, Any], *, event_override: str | None = None) -> HookPayload | None:
    """Pick the supported fields out of a decoded hook payload.

    Unknown keys are ignored. Returns None when no supported event name can
    be determined. ``transcript_path``, ``tool_response``/``tool_result``,
    ``error``, ``last_assistant_message`` and every other field are never
    read.
    """
    event = data.get("hook_event_name")
    if not isinstance(event, str) or not event:
        event = event_override
    if not isinstance(event, str) or event not in SUPPORTED_HOOK_EVENTS:
        return None

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        session_id = None

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Documented field is ``prompt``; accept the alternate spelling some
    # reference examples use, never both.
    prompt = _str_or_none(data.get("prompt"))
    if prompt is None:
        prompt = _str_or_none(data.get("user_message"))

    return HookPayload(
        event=event,
        session_id=session_id,
        cwd=_str_or_none(data.get("cwd"), 1_000),
        source=_str_or_none(data.get("source"), 40),
        reason=_str_or_none(data.get("reason"), 40) or _str_or_none(data.get("end_reason"), 40),
        prompt=prompt,
        tool_name=_str_or_none(data.get("tool_name"), 80),
        file_path=_str_or_none(tool_input.get("file_path") or tool_input.get("notebook_path"), 2_000),
        command=_str_or_none(tool_input.get("command")),
        stop_hook_active=bool(data.get("stop_hook_active")),
        # Claude Code documents PostToolUse as "runs immediately after a tool
        # completes successfully" (failures fire PostToolUseFailure instead),
        # so the event itself is the positive success signal for Claude.
        tool_success=True if event == EVENT_POST_TOOL_USE else None,
    )


# ---------------------------------------------------------------------------
# Status-line payload parsing -- Claude Code's *status line* is a separate,
# documented mechanism from hooks (a single `statusLine` command Claude Code
# invokes with JSON on stdin, whose stdout becomes the rendered status line).
# It is the only official, local, no-network surface that carries model id,
# cumulative session cost, and token counts -- no hook payload ever does (see
# module docstring). OpenShard reads it opportunistically, in addition to
# hooks, never in place of them.
# ---------------------------------------------------------------------------


@dataclass
class StatusPayload:
    session_id: str | None
    cwd: str | None
    model_id: str | None = None
    cost_total_usd: float | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_creation: int | None = None
    tokens_cache_read: int | None = None
    # PR12: a usage report from another agent. ``usage_key`` (OpenCode: the
    # assistant message id) makes the report *per message* rather than a
    # cumulative session figure: the buffer keeps the latest value per key
    # and sums them, so the same message re-reported while streaming never
    # double counts. ``provider_id`` is the model provider when exposed.
    agent: str = AGENT_CLAUDE_CODE
    provider_id: str | None = None
    usage_key: str | None = None

    def to_dict(self) -> dict:
        """Queue representation: every field except ``cwd`` (used for repo resolution only)."""
        return {
            "session_id": self.session_id,
            "model_id": self.model_id,
            "cost_total_usd": self.cost_total_usd,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_cache_creation": self.tokens_cache_creation,
            "tokens_cache_read": self.tokens_cache_read,
            "agent": self.agent,
            "provider_id": self.provider_id,
            "usage_key": self.usage_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StatusPayload | None:
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            return None
        agent = data.get("agent")
        return cls(
            session_id=session_id,
            cwd=None,
            model_id=_str_or_none(data.get("model_id"), 200),
            cost_total_usd=_number_or_none(data.get("cost_total_usd")),
            tokens_input=_int_or_none(data.get("tokens_input")),
            tokens_output=_int_or_none(data.get("tokens_output")),
            tokens_cache_creation=_int_or_none(data.get("tokens_cache_creation")),
            tokens_cache_read=_int_or_none(data.get("tokens_cache_read")),
            agent=str(agent) if is_known_agent(agent) else AGENT_CLAUDE_CODE,
            provider_id=_str_or_none(data.get("provider_id"), 80),
            usage_key=_str_or_none(data.get("usage_key"), 80),
        )


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    n = _number_or_none(value)
    return int(n) if n is not None else None


def extract_status_payload(data: Mapping[str, Any]) -> StatusPayload | None:
    """Pick the supported fields out of a decoded status-line payload.

    Unknown keys (rate limits, prompt cache stats, vim mode, workspace repo
    details, ...) are never read. Returns None only when the payload has no
    usable session id -- unlike hooks, a status payload has no event name to
    validate.
    """
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    model = data.get("model")
    model_id = _str_or_none(model.get("id"), 200) if isinstance(model, dict) else None
    cost = data.get("cost")
    cost_total_usd = _number_or_none(cost.get("total_cost_usd")) if isinstance(cost, dict) else None
    ctx = data.get("context_window")
    usage = ctx.get("current_usage") if isinstance(ctx, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return StatusPayload(
        session_id=session_id,
        cwd=_str_or_none(data.get("cwd"), 1_000),
        model_id=model_id,
        cost_total_usd=cost_total_usd,
        tokens_input=_int_or_none(usage.get("input_tokens")),
        tokens_output=_int_or_none(usage.get("output_tokens")),
        tokens_cache_creation=_int_or_none(usage.get("cache_creation_input_tokens")),
        tokens_cache_read=_int_or_none(usage.get("cache_read_input_tokens")),
    )


def _status_line_text(data: Mapping[str, Any]) -> str:
    """A minimal, honest replacement status line: folder name + model, if known.

    Runs even when the payload can't be attributed to a repo/session -- the
    status line must always show *something* reasonable. Never raises.
    """
    try:
        cwd = data.get("cwd")
        folder = None
        if isinstance(cwd, str) and cwd:
            folder = (PureWindowsPath(cwd) if "\\" in cwd else PurePosixPath(cwd)).name or None
        model = data.get("model")
        display = model.get("display_name") if isinstance(model, dict) else None
        display = display if isinstance(display, str) and display else None
        parts = [p for p in (folder, display) if p]
        return " · ".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Repository resolution and path privacy
# ---------------------------------------------------------------------------


def _is_forbidden_capture_root(root: Path) -> bool:
    """True when *root* must never be treated as the repository a hook captures into.

    Guards against a payload whose ``cwd``/``CLAUDE_PROJECT_DIR`` has no
    project git repository of its own walking (or falling back, when no
    ``.git`` exists at all) all the way up to -- or beyond -- the user's
    home directory. This is a real, observed condition on at least one
    development machine (the home directory is itself a git repository),
    and capturing there would run ``git diff``/``git ls-files`` across the
    user's entire home tree and start writing ``.openshard/`` there --
    never the intent of a Claude Code hook. A real project that happens to
    live *under* the home directory and has its own nearer ``.git`` is
    unaffected: the walk already stops at the first ``.git`` it finds,
    which is never home in that case. Never raises.
    """
    try:
        home = Path.home().resolve()
    except OSError:
        return False
    try:
        resolved = root.resolve()
    except OSError:
        return False
    try:
        return resolved == home or home.is_relative_to(resolved)
    except (ValueError, OSError):
        return resolved == home


def resolve_repo_root(payload: HookPayload | StatusPayload, env: Mapping[str, str] | None = None) -> Path | None:
    """Locate the repository this hook/status payload belongs to. Never raises.

    ``CLAUDE_PROJECT_DIR`` (the project root whose ``.claude/settings.local.json``
    fired this hook) wins, then the payload's ``cwd``. An agent whose hooks
    are configured user-globally (``AgentProfile.opt_in_repo``) resolves only
    to a git repository that already has an ``.openshard/`` directory. The nearest enclosing
    git root is used; a directory that is not inside a git repository is
    used as-is (``.openshard/`` is created there). Environment variables
    are only ever *read* here to find the repo -- never stored. A resolved
    root that is the user's home directory (or an ancestor of it -- see
    ``_is_forbidden_capture_root``) is refused; the next candidate (or
    ``None``) is used instead, so no git command ever runs against it.
    """
    from openshard.adapters.claude_mcp_install import find_repo_root

    env = env if env is not None else os.environ
    candidates: list[str] = []
    project_dir = env.get("CLAUDE_PROJECT_DIR")
    if isinstance(project_dir, str) and project_dir.strip():
        candidates.append(project_dir.strip())
    if payload.cwd:
        candidates.append(payload.cwd)
    opt_in = profile_for(payload.agent).opt_in_repo
    for raw in candidates:
        try:
            p = Path(raw)
            if not p.is_dir():
                continue
            root = find_repo_root(p)
            if opt_in and (root is None or not (root / ".openshard").is_dir()):
                # A user-global agent hook (Hermes) fires in every directory:
                # only a git repository that already has ``.openshard/`` has
                # opted in to capture.
                continue
            resolved = root if root is not None else p.resolve()
            if _is_forbidden_capture_root(resolved):
                continue
            return resolved
        except Exception:
            continue
    return None


def _to_repo_relative(raw_path: str | None, repo_root: Path) -> str | None:
    """Return *raw_path* relative to *repo_root* as a posix string, or None.

    Absolute paths outside the repository are dropped entirely (not even
    the basename is kept). Never raises.
    """
    if not raw_path or not isinstance(raw_path, str):
        return None
    from openshard.safety.sanitize import sanitize_path

    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            # Windows absolute paths arriving on a POSIX-flavoured Path (or
            # vice versa) still need anchoring under the repo root.
            if PureWindowsPath(raw_path).is_absolute() or PurePosixPath(raw_path).is_absolute():
                return None
            candidate = repo_root / candidate
        rel = candidate.resolve().relative_to(repo_root.resolve())
    except Exception:
        return None
    posix = rel.as_posix()
    if not posix or posix == ".":
        return None
    return sanitize_path(posix, _PATH_CAP)


# ---------------------------------------------------------------------------
# Free-text sanitization (reuses the existing scrubbers)
# ---------------------------------------------------------------------------


def sanitize_task_excerpt(prompt: str | None) -> str | None:
    """First-prompt excerpt used as the Shard task: scrubbed, bounded, or None."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    from openshard.adapters.claude_code_import import _sanitize_task

    text = _sanitize_task(prompt, placeholder="", cap=_TASK_CAP)
    return text or None


def summarize_command(command: str | None, label: str = "Bash") -> tuple[str, str | None, str]:
    """Return ``(action_text, target_program, command_kind)`` for a shell command.

    The command is secret-scrubbed, whitespace-collapsed and capped; if the
    scrubbed text still looks unsafe to ``sanitize_text`` (secret-like run,
    absolute path) the whole command text is replaced by a neutral label so
    nothing risky is stored. ``command_kind`` is a deterministic
    classification (``test`` / ``lint`` / ``other``) used as metadata only --
    never as a verification result. *label* is the tool's own name
    (``Bash`` for Claude Code/Codex, ``bash`` for OpenCode).
    """
    from openshard.safety.sanitize import sanitize_text
    from openshard.security.secret_scan import scrub_text_for_secrets

    if not isinstance(command, str) or not command.strip():
        return f"{label} command", None, "other"
    kind = "test" if _TEST_COMMAND_RE.search(command) else ("lint" if _LINT_COMMAND_RE.search(command) else "other")
    scrubbed, _ = scrub_text_for_secrets(command[:1_000], source_label="<hook-command>")
    collapsed = " ".join(scrubbed.split())
    safe = sanitize_text(collapsed, _COMMAND_CAP)
    first = collapsed.split(" ", 1)[0] if collapsed else ""
    target = first if _FIRST_TOKEN_RE.match(first) else None
    if not safe:
        return f"{label} command (redacted)", target, kind
    return f"{label}: {safe}", target, kind


# ---------------------------------------------------------------------------
# Reduced payload -- the only hook representation ever persisted outside
# runs.jsonl (the capture service's per-session queue). Everything free-text
# or path-like is already scrubbed / repo-anchored here, so a queue file can
# never leak what the module docstring promises is never stored.
# ---------------------------------------------------------------------------


@dataclass
class ReducedHookPayload:
    event: str
    session_id: str
    source: str | None = None  # SessionStart
    reason: str | None = None  # SessionEnd
    task_excerpt: str | None = None  # UserPromptSubmit: scrubbed, bounded excerpt (never the prompt)
    tool_name: str | None = None  # PostToolUse / PostToolUseFailure
    file_target: str | None = None  # repo-relative path, or None
    file_dropped: bool = False  # a file_path was given but fell outside the repository
    command_action: str | None = None  # summarized Bash command text ("Bash: ..."), scrubbed
    command_target: str | None = None
    command_kind: str | None = None  # test | lint | other
    stop_hook_active: bool = False
    # PR12 (agent-neutral additions; absent on pre-PR12 queue lines):
    agent: str = AGENT_CLAUDE_CODE
    tool_kind: str | None = None  # file | command | other
    file_targets: list[dict] = field(default_factory=list)  # [{"path", "change_type"}], repo-relative
    model_id: str | None = None
    provider_id: str | None = None
    tool_success: bool | None = None  # see HookPayload.tool_success
    attrs: dict[str, Any] = field(default_factory=dict)  # see HookPayload.attrs
    # v0.4.4: on a SessionStart queued by the capture service, the working-tree
    # baseline taken when the event was *received* (see _snapshot_baseline),
    # so a replay that lags behind the agent's first edits still anchors
    # attribution at session start. Absent on every other line.
    baseline: dict | None = None

    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "event": self.event,
            "session_id": self.session_id,
            "source": self.source,
            "reason": self.reason,
            "task_excerpt": self.task_excerpt,
            "tool_name": self.tool_name,
            "file_target": self.file_target,
            "file_dropped": self.file_dropped,
            "command_action": self.command_action,
            "command_target": self.command_target,
            "command_kind": self.command_kind,
            "stop_hook_active": self.stop_hook_active,
            "agent": self.agent,
            "tool_kind": self.tool_kind,
            "file_targets": list(self.file_targets),
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "tool_success": self.tool_success,
        }
        if self.attrs:
            data["attrs"] = dict(self.attrs)
        if self.baseline is not None:
            data["baseline"] = self.baseline
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReducedHookPayload | None:
        """Rebuild from a queue line; None when the line is not a valid reduced hook."""
        event = data.get("event")
        session_id = data.get("session_id")
        if not isinstance(event, str) or event not in SUPPORTED_HOOK_EVENTS:
            return None
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            return None
        agent = data.get("agent")
        raw_targets = data.get("file_targets")
        file_targets: list[dict] = []
        if isinstance(raw_targets, list):
            for item in raw_targets[:_MAX_FILE_TARGETS]:
                if not isinstance(item, dict):
                    continue
                path = _str_or_none(item.get("path"), _PATH_CAP)
                if path is None:
                    continue
                change_type = item.get("change_type")
                if change_type not in ("create", "update", "delete"):
                    change_type = "update"
                file_targets.append({"path": path, "change_type": change_type})
        tool_kind = data.get("tool_kind")
        agent_key = str(agent) if is_known_agent(agent) else AGENT_CLAUDE_CODE
        raw_success = data.get("tool_success")
        tool_success: bool | None = raw_success if isinstance(raw_success, bool) else None
        if "tool_success" not in data and agent_key == AGENT_CLAUDE_CODE and event == EVENT_POST_TOOL_USE:
            # Queue line written before the flag existed: every such line is
            # a Claude Code hook, whose PostToolUse is itself the success signal.
            tool_success = True
        return cls(
            event=event,
            session_id=session_id,
            source=_str_or_none(data.get("source"), 40),
            reason=_str_or_none(data.get("reason"), 40),
            task_excerpt=_str_or_none(data.get("task_excerpt"), _TASK_CAP),
            tool_name=_str_or_none(data.get("tool_name"), 80),
            file_target=_str_or_none(data.get("file_target"), _PATH_CAP),
            file_dropped=bool(data.get("file_dropped")),
            command_action=_str_or_none(data.get("command_action"), _COMMAND_CAP + 32),
            command_target=_str_or_none(data.get("command_target"), 40),
            command_kind=_str_or_none(data.get("command_kind"), 16),
            stop_hook_active=bool(data.get("stop_hook_active")),
            agent=agent_key,
            tool_kind=tool_kind if tool_kind in _TOOL_KINDS else None,
            file_targets=file_targets,
            model_id=_str_or_none(data.get("model_id"), 200),
            provider_id=_str_or_none(data.get("provider_id"), 80),
            tool_success=tool_success,
            attrs=_clean_attrs(data.get("attrs")),
            baseline=_valid_baseline(data.get("baseline")),
        )


def _valid_baseline(raw: object) -> dict | None:
    """A queued baseline block, or None when absent/malformed (never a guess)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
        return None
    paths = {
        k: (v if isinstance(v, str) or v is None else "?")
        for k, v in list(raw["paths"].items())[:_BASELINE_MAX_PATHS] if isinstance(k, str)
    }
    return {
        "source": raw.get("source") if isinstance(raw.get("source"), str) else "git_status",
        "at": raw.get("at") if isinstance(raw.get("at"), str) else None,
        "paths": paths,
        "truncated": bool(raw.get("truncated")),
    }


def _classify_claude_tool(tool: str) -> str:
    if tool in FILE_TOOLS:
        return TOOL_KIND_FILE
    if tool in COMMAND_TOOLS:
        return TOOL_KIND_COMMAND
    return TOOL_KIND_OTHER


def reduce_hook_payload(payload: HookPayload, repo_root: Path) -> ReducedHookPayload | None:
    """Scrub and repo-anchor *payload* into the persistable ``ReducedHookPayload``.

    This is the only place raw prompt / file path / command text is ever
    looked at; the result is what both the synchronous path and the capture
    service's queue carry from here on. Returns None without a session id.
    Agent-neutral: a translator that already classified its tool
    (``tool_kind``) or named several files (``file_paths``) is honoured;
    a Claude Code payload is classified by tool name exactly as before.
    """
    if payload.session_id is None:
        return None
    reduced = ReducedHookPayload(
        event=payload.event,
        session_id=payload.session_id,
        source=payload.source,
        reason=payload.reason,
        stop_hook_active=payload.stop_hook_active,
        agent=payload.agent if is_known_agent(payload.agent) else AGENT_CLAUDE_CODE,
        model_id=_str_or_none(payload.model_id, 200),
        provider_id=_str_or_none(payload.provider_id, 80),
        tool_success=payload.tool_success if isinstance(payload.tool_success, bool) else None,
        attrs=_clean_attrs(payload.attrs),
    )
    if payload.event == EVENT_USER_PROMPT_SUBMIT:
        reduced.task_excerpt = sanitize_task_excerpt(payload.prompt)
    elif payload.event in (EVENT_POST_TOOL_USE, EVENT_POST_TOOL_USE_FAILURE):
        tool = payload.tool_name or "unknown"
        reduced.tool_name = tool
        kind = payload.tool_kind or _classify_claude_tool(tool)
        reduced.tool_kind = kind
        if kind == TOOL_KIND_FILE:
            reduced.file_target = _to_repo_relative(payload.file_path, repo_root)
            reduced.file_dropped = reduced.file_target is None and bool(payload.file_path)
            for raw_path, change_type in payload.file_paths[:_MAX_FILE_TARGETS]:
                rel = _to_repo_relative(raw_path, repo_root)
                if rel is None:
                    reduced.file_dropped = True
                    continue
                ct = change_type if change_type in ("create", "update", "delete") else "update"
                reduced.file_targets.append({"path": rel, "change_type": ct})
            if reduced.file_target is None and reduced.file_targets:
                reduced.file_target = reduced.file_targets[0]["path"]
        elif kind == TOOL_KIND_COMMAND:
            action, target, ckind = summarize_command(payload.command, label=tool)
            reduced.command_action, reduced.command_target, reduced.command_kind = action, target, ckind
        elif kind == TOOL_KIND_READ:
            reduced.file_target = _to_repo_relative(payload.file_path, repo_root)
            reduced.file_dropped = reduced.file_target is None and bool(payload.file_path)
    elif payload.event == EVENT_FILE_EDITED:
        reduced.file_target = _to_repo_relative(payload.file_path, repo_root)
        reduced.file_dropped = reduced.file_target is None and bool(payload.file_path)
    elif payload.event in (EVENT_APPROVAL_REQUEST, EVENT_APPROVAL_DECISION):
        # The command the gate was raised for: scrubbed and capped like any
        # other command; the raw text is never persisted.
        action, target, _kind = summarize_command(payload.command, label="command")
        reduced.command_action, reduced.command_target = action, target
    return reduced


# ---------------------------------------------------------------------------
# Per-session staging buffer
# ---------------------------------------------------------------------------


def sessions_dir(repo_root: Path) -> Path:
    return repo_root / ".openshard" / SESSIONS_DIRNAME


def buffer_path(repo_root: Path, session_id: str, agent: str = AGENT_CLAUDE_CODE) -> Path:
    """Staging-buffer file for one agent session.

    Scoped per agent (PR12): session ids are minted by the agents
    themselves, so two different agents could in principle hand OpenShard
    the same id, and their sessions must never share a buffer (they are
    separate Shards with separate executors). Claude Code keeps the
    pre-PR12 ``<sid>.json`` name so live buffers survive an upgrade.
    """
    if agent == AGENT_CLAUDE_CODE:
        return sessions_dir(repo_root) / f"{session_id}.json"
    return sessions_dir(repo_root) / f"{agent}.{session_id}.json"


def _buffer_profile(buf: Mapping[str, Any]) -> AgentProfile:
    """The agent profile a buffer belongs to (pre-PR12 buffers: Claude Code)."""
    return profile_for(buf.get("agent"))


def _new_buffer(
    session_id: str, repo_root: Path, first_hook: str, *, now: str | None = None, agent: str = AGENT_CLAUDE_CODE,
    baseline: dict | None = None,
) -> dict:
    from openshard.analysis.repo_map import collect_git_info

    git_info = collect_git_info(repo_root)
    now = now or _now()
    profile = profile_for(agent)
    buf: dict = {
        "schema_version": BUFFER_SCHEMA_VERSION,
        "agent": profile.key,
        "session_id": session_id,
        "started_at": now,
        "last_activity_at": now,
        "start_source": None,
        "git_branch": git_info.branch,
        "git_head_commit_hash": git_info.head_commit,
        "git_dirty": git_info.dirty,
        # v0.4.4: the working tree as it already was when this session was
        # first observed. Anything dirty/untracked here is not this
        # session's work unless it changes again (blob id) or the agent
        # reports editing it. See _snapshot_baseline. A baseline the capture
        # service took when SessionStart was received takes precedence over
        # one taken now (a replay may lag behind the agent's first edits).
        "baseline": baseline if baseline is not None else _snapshot_baseline(repo_root, now),
        "task": None,
        "prompt_count": 0,
        "tool_call_count": 0,
        "tool_failure_count": 0,
        "turn_count": 0,
        "hook_files": {},  # repo-relative path -> "create" | "update"
        "events": [],  # canonical Event dicts (run/shard ids stamped at fold)
        "dropped_events": 0,
        "git_file_event_ids": {},  # "path|change_type" -> stable event_id across folds
        "record": None,  # {run_id, shard_id, attempt_number, timestamp}
        "ended": None,  # {reason, at}
        # Turn-boundary timestamps (Requirement: task completion must not
        # require SessionEnd) -- first_prompt_at/last_stop_at bound the task's
        # actual work, never the whole (possibly much longer-lived) session.
        "first_prompt_at": None,
        "last_stop_at": None,
        # Neutral idle boundaries observed (OpenCode session.idle): counted
        # and timestamped for visibility, never a completed turn.
        "idle_count": 0,
        "last_idle_at": None,
        # Model/cost/token capture -- populated opportunistically by the
        # Claude Code *status line* channel (see handle_claude_status), never
        # by hooks (no hook payload carries this data; see module docstring).
        "model_current": None,
        "models_seen": [],  # distinct model ids, first-seen order, bounded
        "cost_total_usd": None,  # latest cumulative session cost observed
        "cost_baseline_usd": None,  # cost observed at the first status ping
        "tokens_current": None,  # {"input", "output", "cache_creation", "cache_read"}
        "status_last_seen_at": None,
        # PR12: where the model id came from (status_line | codex_hook |
        # opencode_plugin), the provider id when an agent exposes one, and
        # per-message usage reports (OpenCode) keyed by message id.
        "model_source": None,
        "provider_current": None,
        "usage_by_key": {},
        "usage_provenance": None,
        # Ids of queued events already applied (capture service replay
        # idempotency; see apply_reduced_hook). Bounded, most recent last.
        "applied_ids": [],
        # v0.4.4: evidence known to be lost for this session -- each item is
        # a completeness reason ({kind, count, detail}); see
        # history/capture_completeness.py and apply_capture_loss.
        "capture_losses": [],
        # Check-shaped commands (test/lint) the agent's hook stream reported,
        # kept apart from ``events`` so the verification evidence survives the
        # event cap: [{"name", "kind", "status", "at"}], bounded; the total
        # keeps counting past the bound. See _hook_verification.
        "checks": [],
        "checks_total": 0,
    }
    _append_event(
        buf,
        event_type="session.started",
        action=f"{profile.label} session observed (first hook: {first_hook})",
        status="started",
        evidence="directly_observed",
        metadata={"hook": first_hook},
        occurred_at=now,
    )
    return buf


_MAX_APPLIED_IDS = 512  # in-memory buffer cap, while a session is live
_PERSISTED_APPLIED_IDS = 64  # smaller tail written to runs.jsonl (see build_hook_entry)


def _append_event(
    buf: dict,
    *,
    event_type: str,
    action: str,
    status: str,
    evidence: str,
    target: str | None = None,
    target_is_path: bool = False,
    metadata: dict | None = None,
    occurred_at: str | None = None,
) -> None:
    """Build one canonical Event (occurred_at = *occurred_at* or now) and stage it.

    ``target_is_path``: pass True only when *target* is a repo-relative file
    path (already produced by ``_to_repo_relative``), so it uses
    ``sanitize_path`` instead of the free-text scrubber -- see
    ``make_event``. Never set it for a Bash command's target (a short first
    token, not a path).
    """
    from openshard.history.event import make_event

    if len(buf["events"]) >= _MAX_BUFFERED_EVENTS:
        buf["dropped_events"] = int(buf.get("dropped_events") or 0) + 1
        return
    record = buf.get("record") or {}
    profile = _buffer_profile(buf)
    ev = make_event(
        event_type=event_type,
        source=profile.event_source,
        action=action,
        occurred_at=occurred_at or _now(),
        run_id=record.get("run_id"),
        shard_id=record.get("shard_id"),
        attempt_number=record.get("attempt_number"),
        actor=profile.import_source,
        target=target,
        target_is_path=target_is_path,
        status=status,
        evidence=evidence,
        metadata=metadata,
    )
    buf["events"].append(ev.to_dict())


def _read_buffer(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    return data


def _write_buffer(path: Path, buf: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    blob = json.dumps(buf)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _buffer_from_entry(entry: dict, session_id: str) -> dict | None:
    """Rebuild a staging buffer from an already-persisted record.

    Used when a hook arrives for a session whose buffer is gone (resumed
    after SessionEnd, or a background Stop hook finishing after SessionEnd
    deleted it) so no later hook can ever overwrite the record with an
    empty snapshot.
    """
    capture = entry.get("capture")
    if not isinstance(capture, dict):
        return None
    raw_events = entry.get("events")
    events: list = raw_events if isinstance(raw_events, list) else []
    hook_events: list[dict] = []
    git_ids: dict[str, str] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        raw_meta = ev.get("metadata")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        if meta.get("evidence_source") == "git_diff":
            target = ev.get("target")
            action = str(ev.get("action") or "")
            change_type = action.split(" ", 1)[1] if " " in action else "update"
            if isinstance(target, str) and isinstance(ev.get("event_id"), str):
                git_ids[f"{target}|{change_type}"] = ev["event_id"]
        else:
            hook_events.append(ev)
    raw_detail = entry.get("files_detail")
    files_detail: list = raw_detail if isinstance(raw_detail, list) else []
    hook_files = {
        f["path"]: f.get("change_type", "update")
        for f in files_detail
        if isinstance(f, dict) and isinstance(f.get("path"), str)
        and (
            f.get("attribution") == ATTR_AGENT_REPORTED
            or (isinstance(f.get("summary"), str) and f["summary"].startswith("reported by "))
        )
    }
    ended = None
    if capture.get("session_end_observed"):
        ended = {"reason": capture.get("session_end_reason"), "at": capture.get("last_activity_at")}
    raw_usage = capture.get("usage_by_key")
    usage_by_key = {
        k: v for k, v in raw_usage.items() if isinstance(k, str) and isinstance(v, dict)
    } if isinstance(raw_usage, dict) else {}
    return {
        "schema_version": BUFFER_SCHEMA_VERSION,
        "agent": agent_for_executor(entry.get("executor")),
        "session_id": session_id,
        "started_at": capture.get("started_at") or entry.get("timestamp") or _now(),
        "last_activity_at": capture.get("last_activity_at") or _now(),
        "start_source": capture.get("start_source"),
        "git_branch": entry.get("git_branch"),
        "git_head_commit_hash": entry.get("git_head_commit_hash"),
        "git_dirty": entry.get("git_dirty"),
        "task": entry.get("task") if capture.get("task_source") == "first_user_prompt_excerpt" else None,
        "prompt_count": int(capture.get("prompt_count") or 0),
        "tool_call_count": int(capture.get("tool_call_count") or 0),
        "tool_failure_count": int(capture.get("tool_failure_count") or 0),
        "turn_count": int(capture.get("turn_count") or 0),
        "hook_files": hook_files,
        "events": hook_events[:_MAX_BUFFERED_EVENTS],
        "dropped_events": int(capture.get("hook_events_dropped") or 0),
        "git_file_event_ids": git_ids,
        "record": {
            "run_id": entry.get("run_id"),
            "shard_id": entry.get("shard_id"),
            # An old record (pre-0.4.4) has no receipt_id; the rebuilt buffer
            # keeps None and build_hook_entry then leaves the field absent,
            # so history is never given an identity after the fact.
            "receipt_id": entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None,
            "attempt_number": entry.get("attempt_number") if isinstance(entry.get("attempt_number"), int) else 1,
            "timestamp": entry.get("timestamp"),
        },
        "ended": ended,
        "first_prompt_at": capture.get("first_prompt_at"),
        "last_stop_at": capture.get("last_turn_completed_at"),
        "idle_count": int(capture.get("idle_count") or 0),
        "invocation_count": int(capture.get("invocation_count") or 0),
        "subagents": _stored_counts(capture.get("subagents"), ("started", "stopped", "failed")),
        "approvals": _stored_counts(capture.get("approvals"), ("requested", "granted", "denied", "unanswered")),
        "last_invoked_model": (
            entry.get("execution_model") if entry.get("execution_model") not in (None, "unknown") else None
        ),
        "last_idle_at": capture.get("last_idle_at") if isinstance(capture.get("last_idle_at"), str) else None,
        "model_current": entry.get("execution_model") if entry.get("execution_model") not in (None, "unknown") else None,
        "models_seen": [m for m in (capture.get("models_seen") or []) if isinstance(m, str)],
        "cost_total_usd": capture.get("cost_total_usd") if isinstance(capture.get("cost_total_usd"), (int, float)) else None,
        "cost_baseline_usd": (
            capture.get("cost_baseline_usd") if isinstance(capture.get("cost_baseline_usd"), (int, float)) else None
        ),
        "tokens_current": (
            {
                "input": entry.get("prompt_tokens") or 0,
                "output": entry.get("completion_tokens") or 0,
                "cache_creation": entry.get("cache_creation_tokens") or 0,
                "cache_read": entry.get("cache_read_tokens") or 0,
            }
            if isinstance(entry.get("prompt_tokens"), int)
            else None
        ),
        "status_last_seen_at": capture.get("last_status_ping_at"),
        "model_source": capture.get("model_source") if capture.get("model_source") != "not_captured" else None,
        "provider_current": capture.get("provider") if isinstance(capture.get("provider"), str) else None,
        "usage_by_key": usage_by_key,
        "usage_provenance": next(
            (v for v in (entry.get("tokens_provenance"), entry.get("cost_provenance")) if isinstance(v, str)),
            None,
        ),
        "applied_ids": [i for i in (capture.get("applied_event_ids") or []) if isinstance(i, str)],
        "check_command_seen": bool(entry.get("verification_attempted")),
        **_stored_checks(entry),
        "capture_losses": _stored_losses(capture),
        "baseline": _stored_baseline(entry, capture),
    }


def _stored_counts(raw: object, names: tuple[str, ...]) -> dict:
    """The per-name counters a persisted ``capture`` block carries (empty when none)."""
    if not isinstance(raw, dict):
        return {}
    return {n: int(raw.get(n) or 0) for n in names if isinstance(raw.get(n), int)}


def _stored_checks(entry: dict) -> dict:
    """The check list and total a persisted record's ``verification`` block carries.

    A pre-v1 record has none: the rebuilt buffer then starts empty and
    ``check_command_seen`` alone keeps the "attempted" fact (see
    _hook_verification).
    """
    block = entry.get("verification")
    if not isinstance(block, dict):
        return {"checks": [], "checks_total": 0}
    checks = [
        {"name": c.get("name"), "kind": c.get("kind"), "status": c.get("status"), "at": None}
        for c in (block.get("checks") or [])
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    ][:_MAX_BUFFERED_CHECKS]
    total = block.get("checks_attempted")
    total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else len(checks)
    if checks and isinstance(block.get("started_at"), str):
        checks[0]["at"] = block["started_at"]
    return {"checks": checks, "checks_total": max(total, len(checks))}


def _stored_baseline(entry: dict, capture: dict) -> dict:
    """The session-start baseline a persisted record carries (v0.4.4 ``changes.baseline``).

    A pre-0.4.4 record has none: the rebuilt buffer then has an empty
    baseline whose source says so, and nothing is assumed pre-existing.
    """
    changes = entry.get("changes")
    stored = changes.get("baseline") if isinstance(changes, dict) else None
    if isinstance(stored, dict) and isinstance(stored.get("paths"), dict):
        return {
            "source": stored.get("source") or "git_status",
            "at": stored.get("at") or capture.get("started_at"),
            "paths": {k: v for k, v in stored["paths"].items() if isinstance(k, str)},
            "truncated": bool(stored.get("truncated")),
        }
    return {"source": "not_available", "at": capture.get("started_at"), "paths": {}, "truncated": False}


def _stored_losses(capture: dict) -> list[dict]:
    """The loss reasons a persisted record already carries (none for pre-0.4.4 records)."""
    block = capture.get("completeness")
    reasons = block.get("reasons") if isinstance(block, dict) else None
    return [r for r in reasons if isinstance(r, dict)] if isinstance(reasons, list) else []


def _is_session_entry(entry: dict, session_id: str, executor: str = EXECUTOR) -> bool:
    capture = entry.get("capture")
    return (
        entry.get("executor") == executor
        and isinstance(capture, dict)
        and capture.get("session_id") == session_id
    )


def _find_persisted_entry(repo_root: Path, session_id: str, executor: str = EXECUTOR) -> dict | None:
    """One raw scan of runs.jsonl for this session's record (no coercion)."""
    path = repo_root / ".openshard" / "runs.jsonl"
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and _is_session_entry(d, session_id, executor):
                return d
    except OSError:
        return None
    return None


def _load_or_create_buffer(
    repo_root: Path, session_id: str, first_hook: str, *, now: str | None = None,
    agent: str = AGENT_CLAUDE_CODE, baseline: dict | None = None,
) -> dict:
    path = buffer_path(repo_root, session_id, agent)
    buf = _read_buffer(path) if path.exists() else None
    if buf is not None:
        return buf
    persisted = _find_persisted_entry(repo_root, session_id, profile_for(agent).executor)
    if persisted is not None:
        rebuilt = _buffer_from_entry(persisted, session_id)
        if rebuilt is not None:
            return rebuilt
    return _new_buffer(session_id, repo_root, first_hook, now=now, agent=agent, baseline=baseline)


def _load_buffer_light(repo_root: Path, session_id: str, agent: str = AGENT_CLAUDE_CODE) -> dict | None:
    """Like ``_load_or_create_buffer``, but never creates a brand-new buffer.

    Used only by the status-line path (``handle_claude_status``), which must
    never spawn git (see module docstring / Requirement 7): ``_new_buffer``
    collects git branch/HEAD/dirty state via four git subprocess calls, which
    is fine as a one-time per-session cost paid by a real lifecycle hook, but
    is not acceptable on the status line's frequent, synchronous hot path.
    If the session has no buffer yet and no persisted record either, there is
    nothing useful this status ping can do, so it is simply not recorded --
    the next real hook (SessionStart/UserPromptSubmit) creates the buffer
    normally, and a later status ping is then captured as usual.
    """
    path = buffer_path(repo_root, session_id, agent)
    if path.exists():
        return _read_buffer(path)
    persisted = _find_persisted_entry(repo_root, session_id, profile_for(agent).executor) or {}
    return _buffer_from_entry(persisted, session_id) or None


# ---------------------------------------------------------------------------
# Record creation and fold (snapshot into runs.jsonl)
# ---------------------------------------------------------------------------


def _count_history_lines(repo_root: Path) -> int | None:
    runs_path = repo_root / ".openshard" / "runs.jsonl"
    try:
        if not runs_path.exists():
            return 0
        with runs_path.open(encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except Exception:
        return None


def _ensure_record(buf: dict, repo_root: Path) -> None:
    """Mint run/shard identity once per session: a new Shard, attempt 1.

    Uses the same ``_make_shard_id(timestamp, run_index)`` minting as the
    import/wrap adapters and the native pipeline. No existing-Shard
    linkage is guessed; that stays an explicit future extension.
    """
    if buf.get("record"):
        return
    from openshard.history.receipt_identity import new_receipt_id
    from openshard.history.shard_contract import _make_shard_id

    timestamp = buf.get("started_at") or _now()
    run_index = _count_history_lines(repo_root)
    sid = str(buf.get("session_id") or "")
    buf["record"] = {
        "run_id": f"{timestamp}-{sid[:8]}" if sid else timestamp,
        "shard_id": _make_shard_id(timestamp, run_index),
        # v0.4.4: the global identity of this record. Minted once here, at
        # creation, and carried through every fold; never position-derived.
        "receipt_id": new_receipt_id(),
        "attempt_number": 1,
        "timestamp": timestamp,
    }
    for ev in buf["events"]:
        if isinstance(ev, dict):
            ev["run_id"] = buf["record"]["run_id"]
            ev["shard_id"] = buf["record"]["shard_id"]
            ev["attempt_number"] = 1


def _cached_repo_identity(buf: dict, repo_root: Path) -> str | None:
    """The repo's ``git config --get remote.origin.url`` identity, computed once per session.

    A remote origin does not change mid-session, so re-running this git
    subprocess on every fold (every ~30s of tool activity, every Stop) is
    pure waste; the first computed value (including ``None``, meaning no
    usable origin) is cached on the buffer and reused for the rest of the
    session's folds.
    """
    if buf.get("repo_identity_computed"):
        value = buf.get("repo_identity")
        return value if isinstance(value, str) else None
    from openshard.history.repo_identity import capture_repo_identity

    identity = capture_repo_identity(repo_root)
    buf["repo_identity_computed"] = True
    buf["repo_identity"] = identity
    return identity


def _blob_ids(repo_root: Path, paths: list[str]) -> dict[str, str | None]:
    """``path -> git blob id`` of each path's *current working-tree content*.

    ``None`` for a path that does not exist (deleted). A path whose id could
    not be computed is absent from the result, and callers treat "unknown"
    as "cannot prove unchanged".
    """
    out: dict[str, str | None] = {}
    existing: list[str] = []
    for path in paths:
        if (repo_root / path).is_file():
            existing.append(path)
        elif not (repo_root / path).exists():
            out[path] = None
    if existing:
        text = run_git(repo_root, ["hash-object", "--stdin-paths"], stdin="".join(f"{p}\n" for p in existing))
        if text is not None:
            ids = text.split()
            if len(ids) == len(existing):
                out.update(zip(existing, ids, strict=True))
    return out


def _snapshot_baseline(repo_root: Path, now: str) -> dict:
    """Working-tree changes already present when the session was first observed.

    ``git status --porcelain -z --untracked-files=all`` gives every dirty and
    untracked path; their current blob ids let a later fold tell "still the
    same pre-existing change" (excluded) from "changed again during the
    session" (git-observed, actor unknown). Bounded; ``truncated`` says when
    the bound was hit, and ``source == "not_available"`` when git could not
    answer -- in both cases attribution stays conservative (nothing beyond
    the snapshot is ever assumed pre-existing).
    """
    from openshard.safety.sanitize import sanitize_path

    text = run_git(repo_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if text is None:
        return {"source": "not_available", "at": now, "paths": {}, "truncated": False}
    paths: list[str] = []
    truncated = False
    fields = text.split("\0")
    i = 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if len(item) < 4:
            continue
        code, raw_path = item[:2], item[3:]
        candidates = [raw_path]
        if code[0] in "RC" or code[1] in "RC":
            # A rename/copy carries the original path in the next field.
            if i < len(fields) and fields[i]:
                candidates.append(fields[i])
            i += 1
        for candidate in candidates:
            if candidate.startswith(_LOCAL_STATE_PREFIXES):
                continue
            safe = sanitize_path(candidate, _PATH_CAP)
            if not safe or safe in paths:
                continue
            if len(paths) >= _BASELINE_MAX_PATHS:
                truncated = True
                break
            paths.append(safe)
    blobs = _blob_ids(repo_root, paths) if paths else {}
    return {
        "source": "git_status",
        "at": now,
        "paths": {p: blobs.get(p, "?") for p in paths},
        "truncated": truncated,
    }


def _other_sessions_reported_paths(repo_root: Path, buf: dict) -> set[str]:
    """Paths other live session buffers in this repository report having edited.

    Best-effort and bounded: only what sibling agent sessions themselves
    reported with a positive success signal (their ``hook_files``). Used to
    label a change ``other_session`` instead of counting it as this
    session's work.
    """
    own = str(buf.get("session_id") or "")
    found: set[str] = set()
    try:
        directory = sessions_dir(repo_root)
        if not directory.is_dir():
            return found
        for path in sorted(directory.glob("*.json"))[: _MAX_OTHER_BUFFERS * 4]:
            if len(found) > _MAX_HOOK_FILES * _MAX_OTHER_BUFFERS:
                break
            other = _read_buffer(path)
            if other is None or str(other.get("session_id") or "") == own:
                continue
            hook_files = other.get("hook_files")
            if isinstance(hook_files, dict):
                found.update(p for p in hook_files if isinstance(p, str))
    except Exception:
        pass
    return found


def _attempted_file_targets(buf: dict) -> set[str]:
    """Paths the agent *tried* to change (file-tool targets without a success signal)."""
    out: set[str] = set()
    for ev in buf.get("events") or []:
        if not isinstance(ev, dict) or ev.get("event_type") != "tool.invoked":
            continue
        _meta_raw = ev.get("metadata")
        meta: dict = _meta_raw if isinstance(_meta_raw, dict) else {}
        if "command_kind" in meta:
            continue  # a shell command's first token, not a path
        if meta.get("access") == "read":
            continue  # a file the agent read, never an attempted edit
        target = ev.get("target")
        if isinstance(target, str) and target:
            out.add(target)
    return out


def _classify_changed_files(buf: dict, repo_root: Path, files: list[dict]) -> list[dict]:
    """Stamp ``attribution`` (and provenance flags) on every git-observed change.

    Git proves the repository changed; it does not prove who changed it.
    So: a path the agent reported with a positive success signal is
    ``agent_reported``; a path that was already dirty at session start and
    whose content is unchanged since is ``pre_existing`` (excluded from the
    session's counts); a path another live session reported is
    ``other_session`` (excluded); everything else is ``git_observed`` --
    the actor is not established, and the receipt says so.
    """
    profile = _buffer_profile(buf)
    _baseline_raw = buf.get("baseline")
    baseline: dict = _baseline_raw if isinstance(_baseline_raw, dict) else {}
    _paths_raw = baseline.get("paths")
    base_paths: dict = _paths_raw if isinstance(_paths_raw, dict) else {}
    _hook_raw = buf.get("hook_files")
    hook_files: dict = _hook_raw if isinstance(_hook_raw, dict) else {}
    attempted = _attempted_file_targets(buf)
    others = _other_sessions_reported_paths(repo_root, buf)
    need_blob = [f["path"] for f in files if f.get("path") in base_paths and f.get("path") not in hook_files]
    current = _blob_ids(repo_root, need_blob) if need_blob else {}
    out: list[dict] = []
    for f in files:
        path = f.get("path")
        if not isinstance(path, str):
            continue
        item = dict(f)
        pre = path in base_paths
        if path in hook_files:
            attribution = ATTR_AGENT_REPORTED
            summary = f"reported by {profile.label} hook"
            if pre:
                summary += "; file already had uncommitted changes before the session"
        elif pre:
            base_blob = base_paths.get(path)
            cur = current.get(path, "?")
            unchanged = base_blob not in (None, "?") and cur == base_blob
            deleted_both = base_blob is None and path in current and cur is None
            if unchanged or deleted_both:
                attribution = ATTR_PRE_EXISTING
                summary = "changed before the session started; not counted as this session's work"
            else:
                attribution = ATTR_GIT_OBSERVED
                summary = (
                    "observed in git diff; changed again during the session; actor not established"
                )
        elif path in others:
            attribution = ATTR_OTHER_SESSION
            summary = "reported by another agent session; not counted as this session's work"
        else:
            attribution = ATTR_GIT_OBSERVED
            summary = "observed in git diff; actor not established"
            if path in attempted:
                summary = f"observed in git diff; {profile.label} attempted an edit (success not reported)"
        item["attribution"] = attribution
        item["pre_existing"] = pre
        if attribution == ATTR_GIT_OBSERVED and path in attempted:
            item["agent_attempted"] = True
        item["summary"] = summary
        out.append(item)
    return out


def _git_changed_files(buf: dict, repo_root: Path) -> tuple[list[dict], str]:
    from openshard.adapters.claude_code_import import _parse_git_changed_files

    base = buf.get("git_head_commit_hash")
    files, source = _parse_git_changed_files(
        repo_root,
        base_ref=base if isinstance(base, str) and base else "HEAD",
        include_untracked=True,
        max_files=_GIT_DIFF_MAX_FILES,
    )
    if source == "not_available" and isinstance(base, str) and base:
        # The snapshotted commit may be unreachable (e.g. rewritten history); fall back.
        files, source = _parse_git_changed_files(
            repo_root, base_ref="HEAD", include_untracked=True, max_files=_GIT_DIFF_MAX_FILES,
        )
    # OpenShard's own store / Claude Code's local settings are never the
    # task's work, even in a repository that tracks them.
    files = [f for f in files if not str(f.get("path", "")).startswith(_LOCAL_STATE_PREFIXES)]
    return files, source


def _build_git_file_events(buf: dict, files: list[dict]) -> list[dict]:
    """file.changed Events for the current git diff, ids stable across folds."""
    from openshard.history.event import make_event

    record = buf.get("record") or {}
    profile = _buffer_profile(buf)
    ids: dict[str, str] = buf.setdefault("git_file_event_ids", {})
    fresh: dict[str, str] = {}
    events: list[dict] = []
    for f in files:
        path = f.get("path")
        change_type = f.get("change_type", "update")
        if not isinstance(path, str):
            continue
        key = f"{path}|{change_type}"
        metadata: dict[str, Any] = {"evidence_source": "git_diff"}
        if isinstance(f.get("attribution"), str):
            # v0.4.4: git proves the change, the attribution says what else
            # is (and is not) known about who made it.
            metadata["attribution"] = f["attribution"]
            if f.get("pre_existing"):
                metadata["pre_existing"] = True
            if f.get("agent_attempted"):
                metadata["agent_attempted"] = True
        ev = make_event(
            event_type="file.changed",
            source=profile.event_source,
            action=f"file {change_type}",
            event_id=ids.get(key),
            occurred_at=_now(),
            run_id=record.get("run_id"),
            shard_id=record.get("shard_id"),
            attempt_number=record.get("attempt_number"),
            actor=profile.import_source,
            target=path,
            target_is_path=True,
            status="unknown",
            evidence="git_observed",
            metadata=metadata,
        )
        fresh[key] = ev.event_id
        events.append(ev.to_dict())
    buf["git_file_event_ids"] = fresh
    return events


def _hook_file_events(buf: dict) -> list[dict]:
    """Fallback file.changed Events from hook-reported paths (git unavailable)."""
    from openshard.history.event import make_event

    record = buf.get("record") or {}
    profile = _buffer_profile(buf)
    events: list[dict] = []
    for path, change_type in list(buf.get("hook_files", {}).items())[:_MAX_TOOL_FILE_EVENTS]:
        ev = make_event(
            event_type="file.changed",
            source=profile.event_source,
            action=f"file {change_type}",
            occurred_at=_now(),
            run_id=record.get("run_id"),
            shard_id=record.get("shard_id"),
            attempt_number=record.get("attempt_number"),
            actor=profile.import_source,
            target=path,
            target_is_path=True,
            status="unknown",
            evidence="agent_reported",
            metadata={"evidence_source": profile.hook_evidence_source},
        )
        events.append(ev.to_dict())
    return events


_MAX_BUFFERED_CHECKS = 20


def _record_check(buf: dict, name: str, kind: str, *, failed: bool, at: str) -> None:
    """Remember one hook-observed check command. Its outcome stays ``unknown``
    unless the agent's own hook reported the tool call as failed."""
    buf["checks_total"] = int(buf.get("checks_total") or 0) + 1
    checks = buf.get("checks")
    if not isinstance(checks, list):
        checks = buf["checks"] = []
    if len(checks) < _MAX_BUFFERED_CHECKS:
        checks.append({"name": name, "kind": kind, "status": "failed" if failed else "unknown", "at": at})


def _hook_verification(buf: dict) -> dict:
    """The record's ``verification`` block (history/verification.py) for a hook session.

    OpenShard receives the hook events itself, so a check-shaped command's
    invocation is ``directly_observed`` -- but its exit code never is. So an
    observed check is ``unknown`` with ``outcome_not_observed`` (never
    ``passed``); a tool failure the agent's hook reported is ``failed`` and
    ``agent_reported`` (the agent's own signal); and "no check command seen"
    is ``not_run`` only while no capture evidence is known lost -- otherwise
    ``unknown``. See history/verification.py ``hook_verification_source``.
    """
    from openshard.history.verification import (
        MODE_HOOK_TOOL_EVENT,
        REASON_CAPTURE_LOSS,
        REASON_CHECKS_TRUNCATED,
        REASON_OUTCOME_NOT_OBSERVED,
        SOURCE_DIRECTLY_OBSERVED,
        STATUS_FAILED,
        STATUS_UNKNOWN,
        build_verification,
        hook_verification_source,
    )

    checks = [c for c in (buf.get("checks") or []) if isinstance(c, dict)]
    total = max(int(buf.get("checks_total") or 0), len(checks))
    lost = bool(_all_losses(buf))
    incomplete: list[str] = [REASON_CAPTURE_LOSS] if lost else []
    if total == 0 and bool(buf.get("check_command_seen")):
        # Older buffer (rebuilt from a pre-v1 record): a check was seen but
        # its details were never kept.
        return build_verification(
            source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT, status=STATUS_UNKNOWN,
            reason="Check command(s) observed through agent hooks; outcome not observed.",
            incomplete_reasons=[REASON_OUTCOME_NOT_OBSERVED, REASON_CAPTURE_LOSS],
        )
    if total == 0:
        if lost:
            return build_verification(
                source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT, status=STATUS_UNKNOWN,
                reason="No check command observed, but some capture events were lost.",
                incomplete_reasons=incomplete,
            )
        return build_verification(
            source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT, checks_attempted=0,
            reason="No check command observed in the agent's tool events.",
        )
    failed = sum(1 for c in checks if c.get("status") == "failed")
    stamps = [c["at"] for c in checks if isinstance(c.get("at"), str)]
    if total > len(checks):
        incomplete.append(REASON_CHECKS_TRUNCATED)
    if failed:
        reason = "The agent's hook reported a check command as failed; exit codes are not observed."
    else:
        incomplete.append(REASON_OUTCOME_NOT_OBSERVED)
        reason = "Check command(s) observed through agent hooks; outcome not observed."
    return build_verification(
        source=hook_verification_source(STATUS_FAILED if failed else STATUS_UNKNOWN),
        observation_mode=MODE_HOOK_TOOL_EVENT,
        checks=[{"name": c.get("name"), "kind": c.get("kind"), "status": c.get("status")} for c in checks],
        checks_attempted=total,
        checks_passed=0,
        checks_failed=failed,
        checks_skipped=0,
        started_at=min(stamps) if stamps else None,
        reason=reason,
        incomplete_reasons=incomplete,
    )


def _all_losses(buf: dict) -> list[dict]:
    """Every loss reason for *buf*: recorded losses plus the buffer's own drop counter."""
    losses = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict)]
    dropped = int(buf.get("dropped_events") or 0)
    if dropped > 0:
        losses.append(make_reason("dropped_hook_events", dropped))
    return losses


def _turn_duration_seconds(buf: dict) -> float | None:
    """Task-boundary duration: first prompt -> most recent Stop. Never the whole session.

    None until at least one turn has completed (Stop observed) -- an
    in-progress session has no honest end boundary yet, so no number is
    fabricated for it.
    """
    start = buf.get("first_prompt_at")
    end = buf.get("last_stop_at")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((t1 - t0).total_seconds(), 2))


def _task_status(buf: dict, ended: dict | None) -> str:
    """Turn-completion status -- independent of SessionEnd (Requirement 1).

    ``turn_completed`` as soon as one Stop has fired, regardless of whether
    the Claude session itself is still open; SessionEnd never has to happen
    first. Never a stronger claim than "the turn finished" -- OpenShard
    cannot see whether Claude considered the result successful (that would
    require reading the assistant's message, which this adapter never
    stores), so this is deliberately not "verified" or "succeeded".
    """
    if int(buf.get("turn_count") or 0) > 0:
        return "turn_completed"
    if ended:
        return "ended_no_turn"
    return "in_progress"


def build_hook_entry(buf: dict, repo_root: Path) -> dict:
    """Build the coerced runs.jsonl record for a session's current state.

    Same record shape as ``claude_code_import.build_claude_code_import_entry``
    (so every existing receipt/query/MCP path renders it unchanged), plus a
    ``capture`` block describing the hook capture itself. Never raises.
    """
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry

    _ensure_record(buf, repo_root)
    record = buf["record"]
    profile = _buffer_profile(buf)

    changed_files, files_source = _git_changed_files(buf, repo_root)
    excluded_files: list[dict] = []
    _baseline_raw = buf.get("baseline")
    baseline: dict = _baseline_raw if isinstance(_baseline_raw, dict) else {}
    _baseline_paths_raw = baseline.get("paths")
    baseline_paths: dict = _baseline_paths_raw if isinstance(_baseline_paths_raw, dict) else {}
    if files_source == "git_diff_inferred":
        classified = _classify_changed_files(buf, repo_root, changed_files)
        included = [f for f in classified if f.get("attribution") not in _EXCLUDED_ATTRIBUTIONS]
        excluded_files = [f for f in classified if f.get("attribution") in _EXCLUDED_ATTRIBUTIONS]
        files_truncated = len(included) > _MAX_REPORTED_FILES or len(excluded_files) > _MAX_EXCLUDED_FILES
        changed_files = included[:_MAX_REPORTED_FILES]
        excluded_files = excluded_files[:_MAX_EXCLUDED_FILES]
        file_events = _build_git_file_events(buf, changed_files)
    else:
        files_truncated = False
        changed_files = [
            {"path": p, "change_type": ct, "summary": f"reported by {profile.label} hook",
             "attribution": ATTR_AGENT_REPORTED, "pre_existing": p in baseline_paths}
            for p, ct in list(buf.get("hook_files", {}).items())[:_MAX_TOOL_FILE_EVENTS]
        ]
        files_source = profile.files_source_label if changed_files else "not_available"
        file_events = _hook_file_events(buf) if changed_files else []
    changes_block = {
        "agent_reported": sum(1 for f in changed_files if f.get("attribution") == ATTR_AGENT_REPORTED),
        "git_observed": sum(1 for f in changed_files if f.get("attribution") == ATTR_GIT_OBSERVED),
        "pre_existing_excluded": sum(1 for f in excluded_files if f.get("attribution") == ATTR_PRE_EXISTING),
        "other_session_excluded": sum(1 for f in excluded_files if f.get("attribution") == ATTR_OTHER_SESSION),
        "files_truncated": files_truncated,
        "baseline": {
            "source": baseline.get("source") or "not_available",
            "at": baseline.get("at") or buf.get("started_at"),
            "dirty_paths": len(baseline_paths),
            "truncated": bool(baseline.get("truncated")),
            # Kept so a buffer rebuilt from this record (a late hook after
            # SessionEnd) keeps excluding the same pre-existing changes.
            "paths": dict(list(baseline_paths.items())[:_BASELINE_MAX_PATHS]),
        },
    }

    ended = buf.get("ended") if isinstance(buf.get("ended"), dict) else None
    prompt_count = int(buf.get("prompt_count") or 0)
    tool_calls = int(buf.get("tool_call_count") or 0)
    turn_count = int(buf.get("turn_count") or 0)
    task_status = _task_status(buf, ended)
    # The turn/task outcome is reported independently of SessionEnd: a
    # completed turn already reads as "completed" even while the underlying
    # Claude session is still open. Session-end is appended as a trailing,
    # separate fact -- it never gates or qualifies the turn status above.
    idle_count = int(buf.get("idle_count") or 0)
    _task_status_text = {
        "turn_completed": f"{turn_count} turn(s) completed",
        "in_progress": "in progress (no turn completed yet)",
        "ended_no_turn": "session ended before any turn completed",
    }[task_status]
    if idle_count and turn_count == 0:
        # Idle boundaries were seen but no completed turn was ever confirmed.
        _task_status_text += f"; {idle_count} idle boundary(ies) observed, turn completion not confirmed"
    end_text = f" Session ended (reason={ended.get('reason') or 'unknown'})." if ended else ""
    # First sentence kept short: the receipt's Result line shows the first
    # complete sentence (see shard_contract._result_display).
    _attr_text = (
        f" Files: {changes_block['agent_reported']} agent-reported, {changes_block['git_observed']} git-observed"
        + (f", {changes_block['pre_existing_excluded']} pre-existing excluded"
           if changes_block["pre_existing_excluded"] else "")
        + (f", {changes_block['other_session_excluded']} other-session excluded"
           if changes_block["other_session_excluded"] else "")
        + "."
    )
    summary = (
        f"{profile.label} session: {len(changed_files)} file(s) changed, {tool_calls} tool call(s)."
        f"{_attr_text} {prompt_count} prompt(s), {_task_status_text}, observed via hooks.{end_text}"
    )

    task = buf.get("task") if isinstance(buf.get("task"), str) and buf.get("task") else None

    # Model/cost/tokens -- opportunistically populated by the Claude Code
    # status line (handle_claude_status) or by what another agent's own hook
    # stream reports (PR12), never guessed from names/env vars. Absent
    # entirely (not merely None) when never observed, so old readers and
    # the "verification never fabricated" contract both stay honest.
    models_seen = [m for m in (buf.get("models_seen") or []) if isinstance(m, str)][:5]
    model_current = buf.get("model_current") if isinstance(buf.get("model_current"), str) else None
    execution_model = model_current or "unknown"
    model_source = buf.get("model_source") if isinstance(buf.get("model_source"), str) else None
    provider_current = buf.get("provider_current") if isinstance(buf.get("provider_current"), str) else None
    usage_provenance = (
        buf.get("usage_provenance") if isinstance(buf.get("usage_provenance"), str) else profile.usage_provenance
    )

    cost_total = buf.get("cost_total_usd")
    cost_baseline = buf.get("cost_baseline_usd")
    estimated_cost: float | None = None
    cost_provenance: str | None = None
    if isinstance(cost_total, (int, float)) and isinstance(cost_baseline, (int, float)):
        # Claude Code's own cumulative session cost, windowed to this Shard's
        # session by subtracting the value observed at the first status ping
        # (usually ~0, but some Claude Code versions carry cost over across
        # /clear -- see status-line docs). Never the raw whole-session total.
        # For per-message usage reports (OpenCode) the baseline is 0 and the
        # total is the sum over distinct message ids (see _apply_status).
        estimated_cost = round(max(0.0, float(cost_total) - float(cost_baseline)), 6)
        cost_provenance = usage_provenance

    tokens_current = buf.get("tokens_current") if isinstance(buf.get("tokens_current"), dict) else None
    prompt_tokens = completion_tokens = total_tokens = None
    cache_creation_tokens = cache_read_tokens = None
    tokens_provenance: str | None = None
    if tokens_current:
        prompt_tokens = int(tokens_current.get("input") or 0)
        completion_tokens = int(tokens_current.get("output") or 0)
        total_tokens = prompt_tokens + completion_tokens
        cache_creation_tokens = int(tokens_current.get("cache_creation") or 0)
        cache_read_tokens = int(tokens_current.get("cache_read") or 0)
        tokens_provenance = usage_provenance

    duration_seconds = _turn_duration_seconds(buf)
    raw_usage = buf.get("usage_by_key") if isinstance(buf.get("usage_by_key"), dict) else {}

    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": record["timestamp"],
        "task": task or profile.task_placeholder,
        # Deterministic display title; never a model call on the capture path.
        "task_title": derive_task_title(task or profile.task_placeholder),
        "execution_model": execution_model,
        "executor": profile.executor,
        "import_source": profile.import_source,
        "import_method": profile.import_method,
        "import_note": profile.import_note,
        "files_source": files_source,
        # A test/lint command being *invoked* (see the TOOL_KIND_COMMAND
        # branch of _apply) is directly-observable from the hook stream, so
        # it is honestly reported as "attempted"; its outcome is not --
        # OpenShard never reads Bash stdout/exit codes for an externally
        # observed session -- so verification_passed stays None forever
        # here (see module docstring "Evidence honesty").
        "verification_attempted": bool(buf.get("check_command_seen")),
        "verification_passed": None,
        # Structured evidence (history/verification.py): what was seen, who
        # reported it and what is known missing. The two booleans above stay
        # for older readers.
        "verification": _hook_verification(buf),
        # Counts cover this session's changes only: agent-reported and
        # git-observed. Pre-existing / other-session changes are excluded
        # from the counts and listed after them in files_detail with their
        # attribution (v0.4.4; see _classify_changed_files).
        "files_created": sum(1 for f in changed_files if f.get("change_type") == "create"),
        "files_updated": sum(1 for f in changed_files if f.get("change_type") == "update"),
        "files_deleted": sum(1 for f in changed_files if f.get("change_type") == "delete"),
        "files_detail": changed_files + excluded_files,
        "changes": changes_block,
        "git_branch": buf.get("git_branch"),
        "git_head_commit_hash": buf.get("git_head_commit_hash"),
        "git_dirty": buf.get("git_dirty"),
        "summary": summary,
        "run_id": record["run_id"],
        "shard_id": record["shard_id"],
        "attempt_number": record["attempt_number"],
        "capture": {
            "source": profile.capture_source,
            # PR12: explicit agent identity, never inferred from the model.
            "agent": profile.key,
            "agent_vendor": profile.vendor,
            "provider": provider_current,
            "session_id": buf.get("session_id"),
            "status": "ended" if ended else "in_progress",
            "session_end_observed": bool(ended),
            "session_end_reason": (ended or {}).get("reason"),
            "start_source": buf.get("start_source"),
            "started_at": buf.get("started_at"),
            "last_activity_at": buf.get("last_activity_at"),
            "prompt_count": prompt_count,
            "turn_count": turn_count,
            "tool_call_count": tool_calls,
            "tool_failure_count": int(buf.get("tool_failure_count") or 0),
            "task_source": "first_user_prompt_excerpt" if task else "not_captured",
            "hook_events_dropped": int(buf.get("dropped_events") or 0),
            # v0.4.4: what this capture knows it is missing. Hook capture is
            # ``partial`` by nature (OpenShard observed, it did not execute or
            # verify); any known loss makes it ``incomplete`` with reasons.
            "completeness": build_completeness(_all_losses(buf)),
            # Dedup ids applied so far (capture-service replay idempotency).
            # Persisted (bounded to a small tail, not the full in-memory cap)
            # so a session's dedup memory survives the buffer being deleted
            # at SessionEnd -- a leftover queue file replayed after the
            # session has already ended (e.g. a crash mid-drain) is rebuilt
            # from this list and still recognizes its own old events as
            # duplicates instead of re-applying them. See _buffer_from_entry.
            "applied_event_ids": [i for i in (buf.get("applied_ids") or []) if isinstance(i, str)][
                -_PERSISTED_APPLIED_IDS:
            ],
            # Turn completion -- independent of session_end_observed above.
            "task_status": task_status,
            "first_prompt_at": buf.get("first_prompt_at"),
            "last_turn_completed_at": buf.get("last_stop_at"),
            # Neutral idle boundaries (OpenCode): visibility only, never a turn.
            "idle_count": idle_count,
            "last_idle_at": buf.get("last_idle_at") if isinstance(buf.get("last_idle_at"), str) else None,
            # Model/cost/token provenance (status-line capture; see above).
            "models_seen": models_seen,
            "model_source": (model_source or profile.model_source) if model_current else "not_captured",
            "cost_total_usd": cost_total if isinstance(cost_total, (int, float)) else None,
            "cost_baseline_usd": cost_baseline if isinstance(cost_baseline, (int, float)) else None,
            "last_status_ping_at": buf.get("status_last_seen_at"),
        },
    }
    if isinstance(record.get("receipt_id"), str) and record["receipt_id"]:
        # v0.4.4 global identity -- present on every record created by this
        # version; absent (never back-filled) on records rebuilt from older history.
        entry["receipt_id"] = record["receipt_id"]
    invocation_count = int(buf.get("invocation_count") or 0)
    if invocation_count:
        # Model invocations observed (Antigravity PreInvocation); absent for
        # agents whose hooks expose no such event.
        entry["capture"]["invocation_count"] = invocation_count
    for count_key, count_names in (
        ("subagents", ("started", "stopped", "failed")),
        ("approvals", ("requested", "granted", "denied", "unanswered")),
    ):
        raw_counts = buf.get(count_key)
        if isinstance(raw_counts, dict) and any(raw_counts.get(n) for n in count_names):
            # Delegation / approval-gate facts the agent reported (Hermes);
            # absent for agents whose hooks expose none.
            entry["capture"][count_key] = {n: int(raw_counts.get(n) or 0) for n in count_names}
    if raw_usage:
        # Per-message usage memory (OpenCode), bounded; lets a buffer rebuilt
        # from this record keep deduplicating re-reported messages.
        entry["capture"]["usage_by_key"] = dict(list(raw_usage.items())[-_MAX_USAGE_KEYS:])
    if estimated_cost is not None:
        entry["estimated_cost"] = estimated_cost
        entry["cost_provenance"] = cost_provenance
    if tokens_provenance is not None:
        entry["prompt_tokens"] = prompt_tokens
        entry["completion_tokens"] = completion_tokens
        entry["total_tokens"] = total_tokens
        entry["cache_creation_tokens"] = cache_creation_tokens
        entry["cache_read_tokens"] = cache_read_tokens
        entry["tokens_provenance"] = tokens_provenance
    if duration_seconds is not None:
        entry["duration_seconds"] = duration_seconds
    try:
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD

        identity = _cached_repo_identity(buf, repo_root)
        if identity:
            entry[REPO_IDENTITY_FIELD] = identity
    except Exception:
        pass

    entry["events"] = [dict(e) for e in buf["events"] if isinstance(e, dict)] + file_events
    return coerce_shard_entry(entry)


def _fold(buf: dict, repo_root: Path) -> tuple[dict, str]:
    """Snapshot the session into runs.jsonl (replace this session's line or append)."""
    from openshard.history.jsonl_store import upsert_jsonl

    entry = build_hook_entry(buf, repo_root)
    session_id = str(buf.get("session_id"))
    executor = _buffer_profile(buf).executor
    outcome = upsert_jsonl(
        repo_root / ".openshard" / "runs.jsonl",
        entry,
        lambda e: _is_session_entry(e, session_id, executor),
        timeout=_LOCK_TIMEOUT_SECONDS,
    )
    buf["last_fold_at"] = _now()
    return entry, outcome


def sweep_stale_buffers(
    repo_root: Path, *, max_age_seconds: float = _STALE_BUFFER_SECONDS, now: datetime | None = None,
) -> list[str]:
    """Fold and remove staging buffers of sessions idle for *max_age_seconds*.

    Called (outside the caller's own session lock) on SessionStart, and by
    Platform sync before it picks what to send (sync/client.py). A stale
    buffer is snapshotted into runs.jsonl exactly as a Stop would do it --
    ``capture.session_end_observed`` stays False and no ``run.completed``
    Event is fabricated -- then removed; a later hook for that session
    rebuilds its buffer from the persisted record. Returns the session ids
    folded. Never raises.
    """
    folded: list[str] = []
    try:
        directory = sessions_dir(repo_root)
        if not directory.is_dir():
            return folded
        from openshard.history.jsonl_store import history_file_lock

        candidates = sorted(p for p in directory.glob("*.json") if p.is_file())
        for path in candidates[: _MAX_STALE_SWEEP * 4]:
            if len(folded) >= _MAX_STALE_SWEEP:
                break
            peek = _read_buffer(path)
            if peek is None:
                continue
            age = _seconds_since(peek.get("last_activity_at"), now)
            if age is None or age < max_age_seconds:
                continue
            sid = str(peek.get("session_id") or path.stem)
            if not _SESSION_ID_RE.match(sid):
                continue
            try:
                with history_file_lock(path, timeout=_SWEEP_LOCK_TIMEOUT_SECONDS):
                    buf = _read_buffer(path)
                    if buf is None:
                        continue
                    age = _seconds_since(buf.get("last_activity_at"), now)
                    if age is None or age < max_age_seconds:
                        continue
                    if _has_activity(buf):
                        # Swept without a SessionEnd: say so on the record
                        # instead of leaving it looking normally finished.
                        losses = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict)]
                        if not any(r.get("kind") == REASON_SESSION_END_NOT_OBSERVED for r in losses):
                            losses.append(make_reason(REASON_SESSION_END_NOT_OBSERVED))
                        buf["capture_losses"] = losses
                        _fold(buf, repo_root)
                    path.unlink()
                folded.append(sid)
            except Exception:
                continue
    except Exception:
        pass
    return folded


def _has_activity(buf: dict) -> bool:
    return (
        int(buf.get("prompt_count") or 0) > 0
        or int(buf.get("tool_call_count") or 0) > 0
        or int(buf.get("invocation_count") or 0) > 0
    )


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


@dataclass
class HookOutcome:
    """What one hook invocation did. ``action`` is one of:
    ``buffered`` | ``record_created`` | ``record_updated`` | ``record_finalized``
    | ``ignored`` | ``error``."""

    event: str
    action: str
    session_id: str | None = None
    repo_root: Path | None = None
    shard_id: str | None = None
    run_id: str | None = None
    detail: str = ""
    warnings: list[str] = field(default_factory=list)


def _apply(payload: ReducedHookPayload, buf: dict, repo_root: Path, *, now: str) -> tuple[str, bool, bool]:
    """Mutate *buf* for one hook. Returns ``(detail, should_fold, should_delete_buffer)``.

    *now* is the time the hook was observed (received) -- for a synchronous
    call that is the current time; for a queued replay it is the time the
    capture service accepted the event, so timestamps stay honest even when
    the replay happens later (e.g. after a service restart).
    """
    buf["last_activity_at"] = now
    event = payload.event
    profile = _buffer_profile(buf)
    if payload.model_id:
        # The agent's own hook stream names the model (Codex: every payload;
        # OpenCode: the user message's selected model). Recorded as observed.
        _observe_model(buf, payload.model_id, payload.provider_id, profile.model_source)

    if event == EVENT_SESSION_START:
        source = payload.source or "unknown"
        if source == "compact":
            return "compaction ignored", False, False
        if not buf.get("start_source"):
            buf["start_source"] = source
        if source == "resume":
            _append_event(
                buf, event_type="session.activity", action=f"{profile.label} session resumed",
                status="unknown", evidence="directly_observed", metadata={"hook": event, "source": source},
                occurred_at=now,
            )
        return f"session start ({source})", False, False

    if event == EVENT_SESSION_IDLE:
        # Neutral boundary: the agent's session went idle. Not a completed
        # turn (turn_count / last_stop_at untouched), not a success; just a
        # good moment to snapshot whatever evidence is already staged.
        buf["idle_count"] = int(buf.get("idle_count") or 0) + 1
        buf["last_idle_at"] = now
        _append_event(
            buf, event_type="session.activity", action="session idle (turn completion not confirmed)",
            status="unknown", evidence="directly_observed",
            metadata={"hook": event, "idle_index": buf["idle_count"]}, occurred_at=now,
        )
        if buf.get("record") and _has_activity(buf):
            return "session idle; snapshot", True, False
        return "session idle (no work yet; not recorded)", False, False

    if event == EVENT_FILE_EDITED:
        # A file the agent says it edited (OpenCode ``file.edited``, which
        # OpenCode publishes only after the write succeeded -- the positive
        # signal that lets the path into the hook-reported list). Used for
        # the git-unavailable fallback only; the authoritative file.changed
        # Events still come from git at fold time.
        if payload.file_target:
            files = buf.setdefault("hook_files", {})
            if payload.file_target in files or len(files) < _MAX_HOOK_FILES:
                files[payload.file_target] = files.get(payload.file_target) or "update"
        return "file edit buffered", False, False

    if event == EVENT_INTERRUPT:
        _append_event(
            buf, event_type="session.activity", action="assistant turn interrupted by user",
            status="unknown", evidence="directly_observed", metadata={"hook": event}, occurred_at=now,
        )
        if buf.get("record") and _has_activity(buf):
            return "turn interrupted", True, False
        return "turn interrupted (no work yet; not recorded)", False, False

    if event == EVENT_MODEL_INVOCATION:
        # The agent invoked its model (Antigravity ``PreInvocation``): real
        # work, so the record is created on the first one, exactly like a
        # first prompt. Invocations are counted, not staged one by one (an
        # agent loop can invoke the model hundreds of times); an Event is
        # staged only when the reported model differs from the previous
        # invocation's, so each model the session used keeps its own
        # timestamped Event without flooding the buffer.
        buf["invocation_count"] = int(buf.get("invocation_count") or 0) + 1
        model = _sanitize_model_id(payload.model_id)
        if model and model != buf.get("last_invoked_model"):
            buf["last_invoked_model"] = model
            _append_event(
                buf, event_type="session.activity", action=f"model invoked: {model}",
                status="unknown", evidence="agent_reported",
                metadata={"hook": event, "model": model, "invocation_index": buf["invocation_count"]},
                occurred_at=now,
            )
        created = buf.get("record") is None
        if created:
            _ensure_record(buf, repo_root)
        return ("first model invocation: record created" if created else "model invocation counted"), created, False

    if event == EVENT_USER_PROMPT_SUBMIT:
        buf["prompt_count"] = int(buf.get("prompt_count") or 0) + 1
        if not buf.get("first_prompt_at"):
            buf["first_prompt_at"] = now
        if not buf.get("task"):
            buf["task"] = payload.task_excerpt
        _append_event(
            buf, event_type="session.activity", action="user prompt submitted",
            status="unknown", evidence="directly_observed",
            metadata={"hook": event, "prompt_index": buf["prompt_count"]},
            occurred_at=now,
        )
        # First prompt = the session has real work: create the record now so
        # even a session interrupted before any Stop leaves a trace.
        created = buf.get("record") is None
        if created:
            _ensure_record(buf, repo_root)
        return ("first prompt: record created" if created else "prompt buffered"), created, False

    if event in (EVENT_POST_TOOL_USE, EVENT_POST_TOOL_USE_FAILURE):
        failed = event == EVENT_POST_TOOL_USE_FAILURE
        tool = payload.tool_name or "unknown"
        buf["tool_call_count"] = int(buf.get("tool_call_count") or 0) + 1
        if failed:
            buf["tool_failure_count"] = int(buf.get("tool_failure_count") or 0) + 1
        metadata: dict[str, Any] = {"hook": event, "tool": tool}
        for attr_key in _TOOL_ATTR_KEYS:  # agent-reported facts about this call, when supplied
            if attr_key in payload.attrs:
                metadata[attr_key] = payload.attrs[attr_key]
        target: str | None = None
        action = f"tool {tool}"
        status = "failed" if failed else "unknown"
        kind = payload.tool_kind or _classify_claude_tool(tool)
        if kind == TOOL_KIND_READ:
            target = payload.file_target
            metadata["access"] = "read"
            if target is None and payload.file_dropped:
                metadata["path_dropped"] = "outside repository"
        elif kind == TOOL_KIND_FILE:
            target = payload.file_target
            if target is None and payload.file_dropped:
                metadata["path_dropped"] = "outside repository"
            targets = list(payload.file_targets) or ([{"path": target, "change_type": None}] if target else [])
            if len(targets) > 1:
                metadata["file_count"] = len(targets)
            if not failed and payload.tool_success is True:
                # Only a translator-attested success (Claude Code: PostToolUse
                # fires solely after a successful tool run) makes the edit
                # ``passed`` and lets its paths into the hook-reported list.
                # Codex/OpenCode attach no such signal, so their file tools
                # stay ``unknown`` and contribute no hook-reported paths;
                # git-observed changes remain evidence on their own.
                status = "passed"
                files = buf.setdefault("hook_files", {})
                for item in targets:
                    path = item.get("path")
                    if not isinstance(path, str) or not path:
                        continue
                    if path in files or len(files) < _MAX_HOOK_FILES:
                        default_ct = "create" if tool == "Write" else "update"
                        files[path] = files.get(path) or item.get("change_type") or default_ct
        elif kind == TOOL_KIND_COMMAND:
            action = payload.command_action or f"{tool} command"
            target = payload.command_target
            metadata["command_kind"] = payload.command_kind or "other"
            # A command exiting non-zero still fires PostToolUse; outcome unknown.
            status = "failed" if failed else "unknown"
            if payload.command_kind in ("test", "lint"):
                # A check-shaped command was directly observed running --
                # enough to say "attempted" honestly. Its pass/fail outcome
                # is never inferred from this (OpenShard does not read tool
                # stdout/exit codes), so verification_passed stays None; see
                # build_hook_entry.
                buf["check_command_seen"] = True
                _record_check(buf, action, payload.command_kind, failed=failed, at=now)
        _append_event(
            buf, event_type="tool.invoked", action=action, target=target,
            target_is_path=(kind in (TOOL_KIND_FILE, TOOL_KIND_READ)),
            status=status, evidence="agent_reported", metadata=metadata, occurred_at=now,
        )
        # Bounded periodic snapshot (see _TOOL_FOLD_INTERVAL_SECONDS): only
        # once a record exists, and never more often than the interval.
        if buf.get("record"):
            since = _seconds_since(buf.get("last_fold_at"))
            if since is None or since >= _TOOL_FOLD_INTERVAL_SECONDS:
                return f"tool {tool} buffered; periodic snapshot", True, False
        return f"tool {tool} buffered", False, False

    if event in (EVENT_SUBAGENT_START, EVENT_SUBAGENT_STOP):
        # A subagent the agent delegated work to (Hermes ``subagent_start`` /
        # ``subagent_stop``). Counted and staged as agent-reported; the
        # delegated goal and the child's summary are free text and are never
        # stored. The child's own session, if it emits hooks, is its own Shard.
        started = event == EVENT_SUBAGENT_START
        counts = buf.get("subagents")
        if not isinstance(counts, dict):
            counts = buf["subagents"] = {}
        child_status = payload.attrs.get("child_status")
        failed_child = (not started) and child_status in ("failed", "error")
        counts["started" if started else "stopped"] = int(counts.get("started" if started else "stopped") or 0) + 1
        if failed_child:
            counts["failed"] = int(counts.get("failed") or 0) + 1
        meta: dict[str, Any] = {"hook": event}
        for attr_key in _SUBAGENT_ATTR_KEYS:
            if attr_key in payload.attrs:
                meta[attr_key] = payload.attrs[attr_key]
        role = payload.attrs.get("child_role")
        if started:
            action = f"subagent started (role={role})" if isinstance(role, str) else "subagent started"
            status = "started"
        else:
            action = f"subagent stopped ({child_status})" if isinstance(child_status, str) else "subagent stopped"
            status = "failed" if failed_child else "unknown"
        _append_event(
            buf, event_type="session.activity", action=action, status=status,
            evidence="agent_reported", metadata=meta, occurred_at=now,
        )
        return ("subagent started" if started else "subagent stopped"), False, False

    if event in (EVENT_APPROVAL_REQUEST, EVENT_APPROVAL_DECISION):
        # The agent's human-approval gate (Hermes ``pre_approval_request`` /
        # ``post_approval_response``). Observed only -- OpenShard neither
        # raises nor answers it. Only a documented grant/deny choice becomes a
        # granted/denied Event; a timeout, withdrawal or undeliverable prompt
        # means nobody decided, and is recorded as exactly that.
        counts = buf.get("approvals")
        if not isinstance(counts, dict):
            counts = buf["approvals"] = {}
        meta = {"hook": event}
        for attr_key in _APPROVAL_ATTR_KEYS:
            if attr_key in payload.attrs:
                meta[attr_key] = payload.attrs[attr_key]
        command_text = payload.command_action or "command"
        if event == EVENT_APPROVAL_REQUEST:
            counts["requested"] = int(counts.get("requested") or 0) + 1
            _append_event(
                buf, event_type="approval.requested", action=f"approval requested: {command_text}",
                target=payload.command_target, status="started", evidence="agent_reported",
                metadata=meta, occurred_at=now,
            )
            return "approval requested", False, False
        choice = payload.attrs.get("choice")
        if choice in _APPROVAL_GRANTED:
            counts["granted"] = int(counts.get("granted") or 0) + 1
            event_type, status, verb = "approval.granted", "passed", f"approval granted ({choice})"
        elif choice in _APPROVAL_DENIED:
            counts["denied"] = int(counts.get("denied") or 0) + 1
            event_type, status, verb = "approval.denied", "failed", f"approval denied ({choice})"
        else:
            counts["unanswered"] = int(counts.get("unanswered") or 0) + 1
            event_type, status = "session.activity", "unknown"
            verb = f"approval not decided ({choice})" if isinstance(choice, str) else "approval not decided"
        _append_event(
            buf, event_type=event_type, action=f"{verb}: {command_text}", target=payload.command_target,
            status=status, evidence="agent_reported", metadata=meta, occurred_at=now,
        )
        return "approval decision", False, False

    if event == EVENT_STOP:
        buf["turn_count"] = int(buf.get("turn_count") or 0) + 1
        buf["last_stop_at"] = now
        _append_event(
            buf, event_type="session.activity", action="assistant turn completed",
            status="unknown", evidence="directly_observed",
            metadata={"hook": event, "turn_index": buf["turn_count"]}, occurred_at=now,
        )
        if _has_activity(buf):
            return "turn completed", True, False
        return "turn completed (no work yet; not recorded)", False, False

    if event == EVENT_SESSION_END:
        reason = payload.reason or "unknown"
        buf["ended"] = {"reason": reason, "at": now}
        if not _has_activity(buf):
            return "session ended with no work; nothing recorded", False, True
        _append_event(
            buf, event_type="run.completed", action=f"{profile.label} session ended (reason={reason})",
            status="unknown", evidence="directly_observed", metadata={"hook": event, "reason": reason},
            occurred_at=now,
        )
        return f"session ended ({reason})", True, True

    return "unsupported event", False, False


def _sanitize_model_id(model_id: str | None) -> str | None:
    """The stored form of a reported model id, or None when there is none / it is unsafe."""
    from openshard.adapters.claude_code_import import _sanitize_model

    if not model_id:
        return None
    safe = _sanitize_model(model_id)
    return None if safe == "unknown" else safe


def _observe_model(buf: dict, model_id: str | None, provider_id: str | None, source: str) -> bool:
    """Record a model (and provider, when exposed) the agent reported. Returns True if changed.

    The stored ``model_current`` is ``provider/model`` when a provider id is
    known (OpenShard's usual slug convention) and the bare slug otherwise --
    a provider is never guessed from the model name.
    """
    from openshard.adapters.claude_code_import import _sanitize_model

    if not model_id:
        return False
    safe_model = _sanitize_model(model_id)
    if safe_model == "unknown":
        return False
    safe_provider = _sanitize_model(provider_id) if provider_id else "unknown"
    known_provider = buf.get("provider_current")
    if (
        safe_provider == "unknown" and isinstance(known_provider, str) and known_provider
        and buf.get("model_current") == f"{known_provider}/{safe_model}"
    ):
        # The same model, named again by a hook that carries no provider
        # (Hermes: only its request hooks do): keep the provider-qualified
        # form already observed rather than downgrading it to the bare slug.
        return False
    if safe_provider != "unknown":
        if buf.get("provider_current") != safe_provider:
            buf["provider_current"] = safe_provider
        if "/" not in safe_model:
            safe_model = f"{safe_provider}/{safe_model}"
    changed = False
    if buf.get("model_current") != safe_model:
        buf["model_current"] = safe_model
        buf["model_source"] = source
        changed = True
    seen = buf.setdefault("models_seen", [])
    if safe_model not in seen and len(seen) < 5:
        seen.append(safe_model)
        changed = True
    return changed


def _already_applied(buf: dict, dedup_id: str | None) -> bool:
    if not dedup_id:
        return False
    applied = buf.get("applied_ids")
    return isinstance(applied, list) and dedup_id in applied


def _mark_applied(buf: dict, dedup_id: str | None) -> None:
    if not dedup_id:
        return
    applied = buf.get("applied_ids")
    if not isinstance(applied, list):
        applied = []
    applied.append(dedup_id)
    if len(applied) > _MAX_APPLIED_IDS:
        del applied[: len(applied) - _MAX_APPLIED_IDS]
    buf["applied_ids"] = applied


def _emit_receipt_telemetry(entry: dict, action: str) -> None:
    """Telemetry (0.4.2): one ``receipt.created`` when a session's record is
    first written, one ``receipt.completed`` when it is finalized -- counts
    and enums only (see ``telemetry/events.py``). Never raises; a periodic
    snapshot (``record_updated``) emits nothing."""
    if action not in ("record_created", "record_finalized"):
        return
    try:
        from openshard.telemetry import emit
        from openshard.telemetry.events import receipt_properties

        emit("receipt.created" if action == "record_created" else "receipt.completed", **receipt_properties(entry))
    except Exception:
        pass


def apply_reduced_hook(
    payload: ReducedHookPayload,
    repo_root: Path,
    *,
    dedup_id: str | None = None,
    at: str | None = None,
) -> HookOutcome:
    """Stage/fold one already-reduced hook for *repo_root*. Never raises.

    The shared core of the synchronous path (``handle_claude_hook``) and
    the capture service's queue replay. ``dedup_id`` makes a replay
    idempotent: an id already recorded in the session buffer is ignored
    (so a queue re-read after a crash never double-counts). ``at`` is the
    observation time to record (default: now).
    """
    try:
        now = at or _now()
        from openshard.history.jsonl_store import history_file_lock

        path = buffer_path(repo_root, payload.session_id, payload.agent)
        with history_file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
            buf = _load_or_create_buffer(
                repo_root, payload.session_id, payload.event, now=now, agent=payload.agent,
                baseline=payload.baseline,
            )
            if _already_applied(buf, dedup_id):
                return HookOutcome(event=payload.event, action="ignored", session_id=payload.session_id,
                                   repo_root=repo_root, detail="duplicate event id")
            detail, should_fold, should_delete = _apply(payload, buf, repo_root, now=now)
            _mark_applied(buf, dedup_id)
            if buf.get("ended") and _has_activity(buf):
                # A hook arriving after SessionEnd (a background Stop that
                # finished late, or a resume of an ended session): snapshot
                # and drop the rebuilt buffer again rather than leave it behind.
                should_fold, should_delete = True, True
            entry: dict | None = None
            outcome = ""
            if should_fold:
                entry, outcome = _fold(buf, repo_root)
            if should_delete:
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
            else:
                _write_buffer(path, buf)
        if should_delete:
            # Best-effort sidecar cleanup once the lock is released; a
            # concurrent holder (Windows) simply keeps it, which is harmless.
            try:
                path.with_name(path.name + ".lock").unlink()
            except OSError:
                pass
        if payload.event == EVENT_SESSION_START or (
            payload.event == EVENT_MODEL_INVOCATION and detail.startswith("first model invocation")
        ):
            # An agent with no start hook (Antigravity) opens a session with
            # its first model invocation; it has no end hook either, so this
            # sweep is what eventually closes its idle sessions.
            sweep_stale_buffers(repo_root)

        record = buf.get("record") or {}
        if entry is not None:
            if payload.event == EVENT_SESSION_END:
                action = "record_finalized"
            elif outcome == "appended":
                action = "record_created"
            else:
                action = "record_updated"
            _emit_receipt_telemetry(entry, action)
        else:
            action = "buffered" if not should_delete else "ignored"
        return HookOutcome(
            event=payload.event, action=action, session_id=payload.session_id, repo_root=repo_root,
            shard_id=record.get("shard_id"), run_id=record.get("run_id"), detail=detail,
        )
    except Exception as exc:  # observational hook: never propagate
        return HookOutcome(event=payload.event, action="error", session_id=payload.session_id,
                           detail=f"{type(exc).__name__}")


def apply_capture_loss(
    session_id: str,
    repo_root: Path,
    *,
    kind: str,
    count: int = 1,
    agent: str = AGENT_CLAUDE_CODE,
    at: str | None = None,
) -> HookOutcome:
    """Record that evidence for *session_id* is known to be lost. Never raises.

    Called by the capture service when a queued line could not be decoded
    (see ``claude_capture_service._replay_file``). The loss is staged on the
    session buffer and, when the session already has a record, folded so
    ``capture.completeness`` becomes ``incomplete`` at once. A session with
    no activity yet gets no fabricated record; the loss stays on its buffer
    and lands on the record the moment the session shows work.
    """
    try:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            return HookOutcome(event="CaptureLoss", action="ignored", detail="invalid session id")
        now = at or _now()
        from openshard.history.jsonl_store import history_file_lock

        path = buffer_path(repo_root, session_id, agent)
        with history_file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
            buf = _load_or_create_buffer(repo_root, session_id, "CaptureLoss", now=now, agent=agent)
            losses = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict)]
            losses.append(make_reason(kind, count))
            buf["capture_losses"] = losses[-_MAX_BUFFERED_EVENTS:]
            buf["last_activity_at"] = now
            if buf.get("record") and _has_activity(buf):
                entry, outcome = _fold(buf, repo_root)
                action = "record_updated" if outcome == "replaced" else "record_created"
            else:
                action = "buffered"
            if buf.get("ended") and _has_activity(buf):
                # The session had already ended and its buffer was rebuilt
                # only to record this loss: fold done above, drop it again.
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
            else:
                _write_buffer(path, buf)
        record = buf.get("record") or {}
        return HookOutcome(
            event="CaptureLoss", action=action, session_id=session_id, repo_root=repo_root,
            shard_id=record.get("shard_id"), run_id=record.get("run_id"), detail=f"{kind} x{count}",
        )
    except Exception as exc:  # observational: never propagate
        return HookOutcome(event="CaptureLoss", action="error", session_id=session_id, detail=f"{type(exc).__name__}")


def extract_agent_payload(
    data: Mapping[str, Any], *, agent: str = AGENT_CLAUDE_CODE, event_override: str | None = None
) -> HookPayload | StatusPayload | None:
    """Translate one decoded payload from *agent* into the neutral payload shapes.

    The single translator seam: Claude Code payloads are read here directly;
    Codex and OpenCode ones are handed to their own small translators. A
    ``StatusPayload`` result is a usage/model observation (no lifecycle
    fact); a ``HookPayload`` is a lifecycle/tool fact; ``None`` is ignored.
    """
    if agent == AGENT_CLAUDE_CODE:
        return extract_hook_payload(data, event_override=event_override)
    if agent == "codex":
        from openshard.adapters.codex_hooks import extract_codex_payload

        return extract_codex_payload(data, event_override=event_override)
    if agent == "opencode":
        from openshard.adapters.opencode_plugin import extract_opencode_payload

        return extract_opencode_payload(data)
    if agent == "cursor":
        from openshard.adapters.cursor_hooks import extract_cursor_payload

        return extract_cursor_payload(data, event_override=event_override)
    if agent == "antigravity":
        from openshard.adapters.antigravity_hooks import extract_antigravity_payload

        return extract_antigravity_payload(data, event_override=event_override)
    if agent == "hermes":
        from openshard.adapters.hermes_hooks import extract_hermes_payload

        return extract_hermes_payload(data, event_override=event_override)
    return None


def handle_hook(
    data: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    event_override: str | None = None,
    agent: str = AGENT_CLAUDE_CODE,
) -> HookOutcome:
    """Process one decoded hook payload from *agent* synchronously. Never raises.

    Safe to call repeatedly: a repeated identical payload only bumps counts
    (tool/prompt/turn) -- it can never create a second record for the same
    session, because the record is upserted by ``capture.session_id`` and
    the agent's executor.
    """
    try:
        payload = extract_agent_payload(data, agent=agent, event_override=event_override)
        if payload is None:
            return HookOutcome(event=str(data.get("hook_event_name") or event_override or ""), action="ignored",
                               detail="unsupported or missing hook_event_name")
        if payload.session_id is None:
            event = getattr(payload, "event", "status")
            return HookOutcome(event=event, action="ignored", detail="missing or invalid session_id")
        repo_root = resolve_repo_root(payload, env)
        if repo_root is None:
            event = getattr(payload, "event", "status")
            return HookOutcome(event=event, action="ignored", session_id=payload.session_id,
                               detail="could not resolve repository directory")
        if isinstance(payload, StatusPayload):
            recorded = apply_status_payload(payload, repo_root)
            return HookOutcome(event="status", action="buffered" if recorded else "ignored",
                               session_id=payload.session_id, repo_root=repo_root,
                               detail="usage recorded" if recorded else "no session buffer yet")
        reduced = reduce_hook_payload(payload, repo_root)
        if reduced is None:
            return HookOutcome(event=payload.event, action="ignored", detail="missing or invalid session_id")
        return apply_reduced_hook(reduced, repo_root)
    except Exception as exc:  # observational hook: never propagate
        return HookOutcome(event=str(data.get("hook_event_name") or ""), action="error",
                           detail=f"{type(exc).__name__}")


def handle_claude_hook(
    data: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    event_override: str | None = None,
) -> HookOutcome:
    """Process one decoded Claude Code hook payload synchronously. Never raises."""
    return handle_hook(data, env=env, event_override=event_override, agent=AGENT_CLAUDE_CODE)


def run_hook_from_stream(
    stream: object,
    *,
    env: Mapping[str, str] | None = None,
    event_override: str | None = None,
) -> HookOutcome:
    """Read one hook payload from *stream* (stdin) and handle it. Never raises.

    Nothing is ever written to stdout. Claude Code injects hook stdout into
    the model's context for some events (SessionStart, UserPromptSubmit), so
    silence is the only safe observational behaviour.
    """
    raw: object = None
    try:
        source = getattr(stream, "buffer", None) or stream
        reader = getattr(source, "read", None)
        raw = reader() if callable(reader) else None
    except Exception:
        raw = None
    data = parse_hook_payload(raw)
    if data is None:
        outcome = HookOutcome(event=event_override or "", action="ignored", detail="empty or malformed payload")
    else:
        outcome = handle_claude_hook(data, env=env, event_override=event_override)
    if outcome.action == "error" or os.environ.get("OPENSHARD_HOOK_DEBUG"):
        _diag(f"{outcome.event or '?'}: {outcome.action} ({outcome.detail})")
    return outcome


# ---------------------------------------------------------------------------
# Status-line handling -- see StatusPayload/extract_status_payload above.
# ---------------------------------------------------------------------------


def _apply_status(payload: StatusPayload, buf: dict, *, now: str | None = None) -> bool:
    """Merge one status-line observation into *buf*. Returns True if anything changed.

    Never raises. Model ids/cost/token counts are the only new state; no
    Event is appended for a status ping (it is not itself a lifecycle fact
    worth recording, just metadata about facts already recorded elsewhere).
    """
    changed = False
    buf["status_last_seen_at"] = now or _now()
    profile = profile_for(payload.agent)

    if payload.model_id:
        changed = _observe_model(buf, payload.model_id, payload.provider_id, profile.model_source) or changed

    if payload.usage_key:
        # Per-message usage (OpenCode): remember the latest report per
        # message id and re-derive the session totals from the map, so a
        # message re-reported while streaming replaces rather than adds.
        usage = buf.get("usage_by_key")
        if not isinstance(usage, dict):
            usage = {}
        report = {
            "cost": payload.cost_total_usd,
            "input": payload.tokens_input or 0,
            "output": payload.tokens_output or 0,
            "cache_creation": payload.tokens_cache_creation or 0,
            "cache_read": payload.tokens_cache_read or 0,
        }
        if usage.get(payload.usage_key) != report:
            usage[payload.usage_key] = report
            if len(usage) > _MAX_USAGE_KEYS:
                for stale in list(usage)[: len(usage) - _MAX_USAGE_KEYS]:
                    del usage[stale]
            changed = True
        buf["usage_by_key"] = usage
        buf["usage_provenance"] = profile.usage_provenance
        # OpenCode reports ``cost: 0`` when it has no pricing for the model,
        # which is "unknown", not "free": only strictly positive per-message
        # costs are trustworthy. With none, no cost is recorded at all (the
        # receipt shows Not recorded rather than a fabricated $0.00); with
        # some, the sum of the positive ones is recorded -- a lower bound
        # when other messages were unpriced. Tokens are kept regardless.
        costs = [r.get("cost") for r in usage.values() if isinstance(r, dict)]
        trusted = [float(c) for c in costs if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0]
        if trusted:
            buf["cost_baseline_usd"] = 0.0
            buf["cost_total_usd"] = round(sum(trusted), 6)
        else:
            buf["cost_baseline_usd"] = None
            buf["cost_total_usd"] = None
        totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
        for r in usage.values():
            if isinstance(r, dict):
                for k in totals:
                    totals[k] += int(r.get(k) or 0)
        if any(totals.values()):
            buf["tokens_current"] = totals
        return changed

    if payload.cost_total_usd is not None:
        if buf.get("cost_baseline_usd") is None:
            buf["cost_baseline_usd"] = payload.cost_total_usd
            changed = True
        if buf.get("cost_total_usd") != payload.cost_total_usd:
            buf["cost_total_usd"] = payload.cost_total_usd
            changed = True

    if payload.tokens_input is not None or payload.tokens_output is not None:
        tokens = {
            "input": payload.tokens_input or 0,
            "output": payload.tokens_output or 0,
            "cache_creation": payload.tokens_cache_creation or 0,
            "cache_read": payload.tokens_cache_read or 0,
        }
        if buf.get("tokens_current") != tokens:
            buf["tokens_current"] = tokens
            changed = True

    return changed


def handle_claude_status(data: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> str:
    """Process one Claude Code status-line JSON payload. Never raises.

    Returns the text to print as the rendered status line (Claude Code uses
    this command's stdout directly, unlike the silent hooks command). Model/
    cost/token capture is a side effect only; a failure anywhere in the
    capture path still returns a usable status line.

    Requirement 7 (status-line performance): this function must stay cheap
    and never touch git or rewrite ``runs.jsonl`` -- it may run very
    frequently and is synchronous (Claude Code waits on its stdout to
    render). It therefore only ever updates and persists the small per-
    session staging buffer (a bounded-size local JSON write); the model/
    cost/token values it records are picked up at the *next* natural fold
    boundary (a periodic tool-hook snapshot, ``Stop``, or ``SessionEnd`` --
    see ``_apply``/``_fold``), never folded from here directly.
    """
    fallback = _status_line_text(data) if isinstance(data, Mapping) else ""
    try:
        payload = extract_status_payload(data)
        if payload is None:
            return fallback
        repo_root = resolve_repo_root(payload, env)
        if repo_root is None:
            return fallback
        apply_status_payload(payload, repo_root)
        return fallback
    except Exception:
        return fallback


def apply_status_payload(
    payload: StatusPayload,
    repo_root: Path,
    *,
    dedup_id: str | None = None,
    at: str | None = None,
) -> bool:
    """Merge one status observation into the session's buffer for *repo_root*.

    Returns True when something was recorded. Never raises. Shared by the
    synchronous status-line handler and the capture service's queue replay
    (``dedup_id`` / ``at`` as for :func:`apply_reduced_hook`).
    """
    try:
        if payload.session_id is None:
            return False
        from openshard.history.jsonl_store import history_file_lock

        path = buffer_path(repo_root, payload.session_id, payload.agent)
        with history_file_lock(path, timeout=_LOCK_TIMEOUT_SECONDS):
            buf = _load_buffer_light(repo_root, payload.session_id, payload.agent)
            if buf is None:
                return False
            if _already_applied(buf, dedup_id):
                return False
            _apply_status(payload, buf, now=at)
            _mark_applied(buf, dedup_id)
            _write_buffer(path, buf)
        return True
    except Exception:
        return False


def run_status_from_stream(stream: object, *, env: Mapping[str, str] | None = None) -> str:
    """Read one status-line payload from *stream* (stdin) and handle it. Never raises.

    Always returns text suitable to print as the status line, even on
    completely empty/malformed input.
    """
    raw: object = None
    try:
        source = getattr(stream, "buffer", None) or stream
        reader = getattr(source, "read", None)
        raw = reader() if callable(reader) else None
    except Exception:
        raw = None
    data = parse_hook_payload(raw)
    if data is None:
        return ""
    try:
        return handle_claude_status(data, env=env)
    except Exception:
        return ""
