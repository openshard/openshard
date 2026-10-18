"""Constants of the Claude Code hook capture path (``claude_hooks``).

Hook event names, tool kinds and classification sets, file-attribution
labels, caps and timeouts, and the regexes the payload parser and the fold
match against. Values only: nothing here has behaviour, and this module
imports nothing from ``claude_hooks``. ``claude_hooks`` imports back every
name it uses, so ``openshard.adapters.claude_hooks.<NAME>`` keeps resolving
for callers and tests, and patching that name still affects the code there.
"""

from __future__ import annotations

import re

from openshard.adapters.capture_agents import CLAUDE_CODE_PROFILE

# Claude Code identity constants (module names kept for callers/tests; values: capture_agents.py).
# Only EXECUTOR is re-exported from claude_hooks; nothing imports the others from there.
EXECUTOR = CLAUDE_CODE_PROFILE.executor
IMPORT_SOURCE = CLAUDE_CODE_PROFILE.import_source
IMPORT_METHOD = CLAUDE_CODE_PROFILE.import_method
CAPTURE_SOURCE = CLAUDE_CODE_PROFILE.capture_source
IMPORT_NOTE = CLAUDE_CODE_PROFILE.import_note

SESSIONS_DIRNAME = "claude_sessions"
BUFFER_SCHEMA_VERSION = 1

EVENT_SESSION_START = "SessionStart"
EVENT_MODEL_SWITCH = "PostModelSwitch"
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
# Unreleased: the agent reported that a tool call was denied by its permission
# system (Grok Build ``PermissionDenied``). A directly reported fact about one
# tool name; not work done (it never opens a record) and not a policy
# decision OpenShard made.
EVENT_PERMISSION_DENIED = "PermissionDenied"
# 0.4.7 (Hermes Agent): a subagent the agent delegated work to started /
# stopped, and a human-approval gate was raised / answered. Agent-reported
# facts about the session; they are never work (``_has_activity``) on their own.
EVENT_SUBAGENT_START = "SubagentStart"
EVENT_SUBAGENT_STOP = "SubagentStop"
EVENT_APPROVAL_REQUEST = "ApprovalRequest"
EVENT_APPROVAL_DECISION = "ApprovalDecision"
SUPPORTED_HOOK_EVENTS: tuple[str, ...] = (
    EVENT_SESSION_START,
    EVENT_MODEL_SWITCH,
    EVENT_USER_PROMPT_SUBMIT,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_STOP,
    EVENT_SESSION_END,
    EVENT_INTERRUPT,
    EVENT_FILE_EDITED,
    EVENT_SESSION_IDLE,
    EVENT_MODEL_INVOCATION,
    EVENT_PERMISSION_DENIED,
    EVENT_SUBAGENT_START,
    EVENT_SUBAGENT_STOP,
    EVENT_APPROVAL_REQUEST,
    EVENT_APPROVAL_DECISION,
)

# Tools whose tool_input.file_path names a file Claude Code says it changed.
FILE_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# Local agent/OpenShard state is never a task's work (see _git_changed_files).
# ``.agents/`` and ``.grok/`` also hold a user's shared rules/skills, so only
# Antigravity's and Grok Build's OpenShard hook configuration files are
# excluded from them.
_LOCAL_STATE_PREFIXES: tuple[str, ...] = (
    ".openshard/", ".claude/", ".codex/", ".opencode/", ".cursor/", ".agents/hooks.json",
    ".grok/hooks/openshard.json",
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
# Claude Code's shell tools (``PowerShell`` on Windows; same hook contract as Bash).
COMMAND_TOOLS: frozenset[str] = frozenset({"Bash", "PowerShell"})
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
_HOOK_SWEEP_DEADLINE_SECONDS = 2.0  # overall budget of the sweep a session-opening hook runs


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_FIRST_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")


OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_COMPLETED = "not_completed"
_COMMAND_OUTCOMES = frozenset({OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_NOT_COMPLETED})


_COMMAND_SAFETIES = frozenset({"safe", "needs_approval", "blocked"})


# Keys only Grok Build puts in a hook document. Grok also emits Claude-compatible
# aliases (``hook_event_name``, ``session_id``, ``tool_name``, ...) in the *same*
# document, so to the Claude receiver a Grok Build event is otherwise a valid
# Claude Code payload. Claude Code itself never sends any of these camelCase keys.
_GROK_BUILD_MARKER_KEYS: tuple[str, ...] = ("hookEventName", "sessionId", "workspaceRoot")


PERMISSION_MODES: frozenset[str] = frozenset({"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"})


# ``PostToolUseFailure.error`` for a shell tool: "a command that ran and
# exited produces a first line ``Exit code N``" -- the only part of the
# string Claude Code documents as stable; the rest is display text and is
# never read.
_CLAUDE_EXIT_LINE_RE = re.compile(r"^Exit code (-?\d{1,10})\s*$")
_CLAUDE_TIMEOUT_LINE_RE = re.compile(r"^Command timed out\b")


TOKENS_SOURCE_TRANSCRIPT = "transcript"
# The model ids the API reported in the transcript's assistant messages: the
# receipt's model when nothing else names one (headless ``claude -p`` has no
# status line). Provider-reported, never guessed; a status-line model wins.
MODEL_SOURCE_TRANSCRIPT = "transcript"
TOKENS_NOT_RECORDED_TRANSCRIPT_UNAVAILABLE = "transcript_unavailable"
# Kept equal to claude_workspace.USAGE_ELSEWHERE_REASON (asserted in tests).
USAGE_ELSEWHERE_REASON = "workspace_usage_attributed_to_other_repository"
TOKENS_INCOMPLETE_FILES_CAPPED = "transcript_files_capped"


# Claude Code wraps a pasted prompt as ``<pasted_content id="...">...</pasted_content>``;
# the wrapper is transport, not the task text.
_PASTED_OPEN_RE = re.compile(r"^\s*<pasted_content\b[^<>]*>\s*", re.IGNORECASE)
_PASTED_CLOSE_RE = re.compile(r"\s*</pasted_content\s*>", re.IGNORECASE)


_COMMAND_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


_MAX_APPLIED_IDS = 512  # in-memory buffer cap, while a session is live
_PERSISTED_APPLIED_IDS = 64  # smaller tail written to runs.jsonl (see build_hook_entry)


CLOUD_SESSION_NAMESPACE = "claude.cloud_session"


# Hooks that can start a resumed segment of an ended session at once; any
# other hook (a late async PostToolUse/Stop of the ended session) is ignored
# while it arrives within the grace period after the session ended.
_SEGMENT_OPENING_EVENTS = frozenset({EVENT_SESSION_START, EVENT_USER_PROMPT_SUBMIT, EVENT_MODEL_INVOCATION})
_LATE_HOOK_GRACE_SECONDS = 60


_MAX_OWN_COMMIT_PATHS = 2000


_TASK_CONTEXT_KEYS = ("source", "variable", "evidence", "bound_at", "bound_by")


_GH_TIMEOUT_SECONDS = 5.0
PR_LOOKUP_ENV = "OPENSHARD_PR_LOOKUP"  # "off" disables the ``gh`` pull-request lookup


_SESSION_COMMIT_GRACE_SECONDS = 120  # clock slack after the session's last observed hook
_GIT_WRITE_CORROBORATION_SECONDS = 900  # a commit may take this long before its tool call returns
# A shell command that can create commits (the agent's own tool call is the
# corroboration a session-created commit needs). Matched on the raw command
# at reduce time; only a boolean is kept.
_GIT_WRITE_RE = re.compile(r"\bgit\b[^|;&]*\b(?:commit|cherry-pick|revert|merge|pull|am)\b")
_MAX_GIT_WRITES = 50
