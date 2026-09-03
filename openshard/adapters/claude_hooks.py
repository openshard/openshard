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

EXECUTOR = "claude_code_hooks"
IMPORT_SOURCE = "claude_code"
IMPORT_METHOD = "openshard_claude_hooks_v0"
CAPTURE_SOURCE = "claude_code_hooks"
IMPORT_NOTE = (
    "Captured automatically from Claude Code lifecycle hooks. "
    "Tool/file facts are as reported by Claude Code; files are inferred from git diff. "
    "Model/cost/tokens are read from Claude Code's status line when one is configured "
    "(see `openshard mcp install claude`); otherwise they stay Unknown/Not recorded. "
    "Verification is never recorded by OpenShard for this capture path."
)

SESSIONS_DIRNAME = "claude_sessions"
BUFFER_SCHEMA_VERSION = 1

EVENT_SESSION_START = "SessionStart"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
EVENT_STOP = "Stop"
EVENT_SESSION_END = "SessionEnd"
SUPPORTED_HOOK_EVENTS: tuple[str, ...] = (
    EVENT_SESSION_START,
    EVENT_USER_PROMPT_SUBMIT,
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_STOP,
    EVENT_SESSION_END,
)

# Tools whose tool_input.file_path names a file Claude Code says it changed.
FILE_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_LOCAL_STATE_PREFIXES: tuple[str, ...] = (".openshard/", ".claude/")
COMMAND_TOOLS: frozenset[str] = frozenset({"Bash"})

_TASK_CAP = 300
_TASK_PLACEHOLDER = "Claude Code session (task not captured)"
_COMMAND_CAP = 100
_PATH_CAP = 200
_MAX_BUFFERED_EVENTS = 200
_MAX_HOOK_FILES = 50
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


def _seconds_since(stamp: object) -> float | None:
    """Seconds elapsed since an OpenShard UTC timestamp string; None if unparsable."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - then).total_seconds()


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


def resolve_repo_root(payload: HookPayload | StatusPayload, env: Mapping[str, str] | None = None) -> Path | None:
    """Locate the repository this hook/status payload belongs to. Never raises.

    ``CLAUDE_PROJECT_DIR`` (the project root whose ``.claude/settings.local.json``
    fired this hook) wins, then the payload's ``cwd``. The nearest enclosing
    git root is used; a directory that is not inside a git repository is
    used as-is (``.openshard/`` is created there). Environment variables
    are only ever *read* here to find the repo -- never stored.
    """
    from openshard.adapters.claude_mcp_install import find_repo_root

    env = env if env is not None else os.environ
    candidates: list[str] = []
    project_dir = env.get("CLAUDE_PROJECT_DIR")
    if isinstance(project_dir, str) and project_dir.strip():
        candidates.append(project_dir.strip())
    if payload.cwd:
        candidates.append(payload.cwd)
    for raw in candidates:
        try:
            p = Path(raw)
            if not p.is_dir():
                continue
            root = find_repo_root(p)
            return root if root is not None else p.resolve()
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
    from openshard.safety.sanitize import sanitize_text

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
    return sanitize_text(posix, _PATH_CAP)


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


def summarize_command(command: str | None) -> tuple[str, str | None, str]:
    """Return ``(action_text, target_program, command_kind)`` for a Bash command.

    The command is secret-scrubbed, whitespace-collapsed and capped; if the
    scrubbed text still looks unsafe to ``sanitize_text`` (secret-like run,
    absolute path) the whole command text is replaced by a neutral label so
    nothing risky is stored. ``command_kind`` is a deterministic
    classification (``test`` / ``lint`` / ``other``) used as metadata only --
    never as a verification result.
    """
    from openshard.safety.sanitize import sanitize_text
    from openshard.security.secret_scan import scrub_text_for_secrets

    if not isinstance(command, str) or not command.strip():
        return "Bash command", None, "other"
    kind = "test" if _TEST_COMMAND_RE.search(command) else ("lint" if _LINT_COMMAND_RE.search(command) else "other")
    scrubbed, _ = scrub_text_for_secrets(command[:1_000], source_label="<hook-command>")
    collapsed = " ".join(scrubbed.split())
    safe = sanitize_text(collapsed, _COMMAND_CAP)
    first = collapsed.split(" ", 1)[0] if collapsed else ""
    target = first if _FIRST_TOKEN_RE.match(first) else None
    if not safe:
        return "Bash command (redacted)", target, kind
    return f"Bash: {safe}", target, kind


# ---------------------------------------------------------------------------
# Per-session staging buffer
# ---------------------------------------------------------------------------


def sessions_dir(repo_root: Path) -> Path:
    return repo_root / ".openshard" / SESSIONS_DIRNAME


def buffer_path(repo_root: Path, session_id: str) -> Path:
    return sessions_dir(repo_root) / f"{session_id}.json"


def _new_buffer(session_id: str, repo_root: Path, first_hook: str) -> dict:
    from openshard.analysis.repo_map import collect_git_info

    git_info = collect_git_info(repo_root)
    now = _now()
    buf: dict = {
        "schema_version": BUFFER_SCHEMA_VERSION,
        "session_id": session_id,
        "started_at": now,
        "last_activity_at": now,
        "start_source": None,
        "git_branch": git_info.branch,
        "git_head_commit_hash": git_info.head_commit,
        "git_dirty": git_info.dirty,
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
        # Model/cost/token capture -- populated opportunistically by the
        # Claude Code *status line* channel (see handle_claude_status), never
        # by hooks (no hook payload carries this data; see module docstring).
        "model_current": None,
        "models_seen": [],  # distinct model ids, first-seen order, bounded
        "cost_total_usd": None,  # latest cumulative session cost observed
        "cost_baseline_usd": None,  # cost observed at the first status ping
        "tokens_current": None,  # {"input", "output", "cache_creation", "cache_read"}
        "status_last_seen_at": None,
    }
    _append_event(
        buf,
        event_type="session.started",
        action=f"Claude Code session observed (first hook: {first_hook})",
        status="started",
        evidence="directly_observed",
        metadata={"hook": first_hook},
    )
    return buf


def _append_event(
    buf: dict,
    *,
    event_type: str,
    action: str,
    status: str,
    evidence: str,
    target: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Build one canonical Event now (occurred_at = now) and stage it."""
    from openshard.history.event import SOURCE_CLAUDE_CODE_HOOKS, make_event

    if len(buf["events"]) >= _MAX_BUFFERED_EVENTS:
        buf["dropped_events"] = int(buf.get("dropped_events") or 0) + 1
        return
    record = buf.get("record") or {}
    ev = make_event(
        event_type=event_type,
        source=SOURCE_CLAUDE_CODE_HOOKS,
        action=action,
        occurred_at=_now(),
        run_id=record.get("run_id"),
        shard_id=record.get("shard_id"),
        attempt_number=record.get("attempt_number"),
        actor=IMPORT_SOURCE,
        target=target,
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
        and f.get("summary") == "reported by Claude Code hook"
    }
    ended = None
    if capture.get("session_end_observed"):
        ended = {"reason": capture.get("session_end_reason"), "at": capture.get("last_activity_at")}
    return {
        "schema_version": BUFFER_SCHEMA_VERSION,
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
            "attempt_number": entry.get("attempt_number") if isinstance(entry.get("attempt_number"), int) else 1,
            "timestamp": entry.get("timestamp"),
        },
        "ended": ended,
        "first_prompt_at": capture.get("first_prompt_at"),
        "last_stop_at": capture.get("last_turn_completed_at"),
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
    }


def _is_session_entry(entry: dict, session_id: str) -> bool:
    capture = entry.get("capture")
    return (
        entry.get("executor") == EXECUTOR
        and isinstance(capture, dict)
        and capture.get("session_id") == session_id
    )


def _find_persisted_entry(repo_root: Path, session_id: str) -> dict | None:
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
            if isinstance(d, dict) and _is_session_entry(d, session_id):
                return d
    except OSError:
        return None
    return None


def _load_or_create_buffer(repo_root: Path, session_id: str, first_hook: str) -> dict:
    path = buffer_path(repo_root, session_id)
    buf = _read_buffer(path) if path.exists() else None
    if buf is not None:
        return buf
    persisted = _find_persisted_entry(repo_root, session_id)
    if persisted is not None:
        rebuilt = _buffer_from_entry(persisted, session_id)
        if rebuilt is not None:
            return rebuilt
    return _new_buffer(session_id, repo_root, first_hook)


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
    from openshard.history.shard_contract import _make_shard_id

    timestamp = buf.get("started_at") or _now()
    run_index = _count_history_lines(repo_root)
    sid = str(buf.get("session_id") or "")
    buf["record"] = {
        "run_id": f"{timestamp}-{sid[:8]}" if sid else timestamp,
        "shard_id": _make_shard_id(timestamp, run_index),
        "attempt_number": 1,
        "timestamp": timestamp,
    }
    for ev in buf["events"]:
        if isinstance(ev, dict):
            ev["run_id"] = buf["record"]["run_id"]
            ev["shard_id"] = buf["record"]["shard_id"]
            ev["attempt_number"] = 1


def _git_changed_files(buf: dict, repo_root: Path) -> tuple[list[dict], str]:
    from openshard.adapters.claude_code_import import _parse_git_changed_files

    base = buf.get("git_head_commit_hash")
    files, source = _parse_git_changed_files(
        repo_root,
        base_ref=base if isinstance(base, str) and base else "HEAD",
        include_untracked=True,
    )
    if source == "not_available" and isinstance(base, str) and base:
        # The snapshotted commit may be unreachable (e.g. rewritten history); fall back.
        files, source = _parse_git_changed_files(repo_root, base_ref="HEAD", include_untracked=True)
    # OpenShard's own store / Claude Code's local settings are never the
    # task's work, even in a repository that tracks them.
    files = [f for f in files if not str(f.get("path", "")).startswith(_LOCAL_STATE_PREFIXES)]
    return files, source


def _build_git_file_events(buf: dict, files: list[dict]) -> list[dict]:
    """file.changed Events for the current git diff, ids stable across folds."""
    from openshard.history.event import SOURCE_CLAUDE_CODE_HOOKS, make_event

    record = buf.get("record") or {}
    ids: dict[str, str] = buf.setdefault("git_file_event_ids", {})
    fresh: dict[str, str] = {}
    events: list[dict] = []
    for f in files:
        path = f.get("path")
        change_type = f.get("change_type", "update")
        if not isinstance(path, str):
            continue
        key = f"{path}|{change_type}"
        ev = make_event(
            event_type="file.changed",
            source=SOURCE_CLAUDE_CODE_HOOKS,
            action=f"file {change_type}",
            event_id=ids.get(key),
            occurred_at=_now(),
            run_id=record.get("run_id"),
            shard_id=record.get("shard_id"),
            attempt_number=record.get("attempt_number"),
            actor=IMPORT_SOURCE,
            target=path,
            status="unknown",
            evidence="git_observed",
            metadata={"evidence_source": "git_diff"},
        )
        fresh[key] = ev.event_id
        events.append(ev.to_dict())
    buf["git_file_event_ids"] = fresh
    return events


def _hook_file_events(buf: dict) -> list[dict]:
    """Fallback file.changed Events from hook-reported paths (git unavailable)."""
    from openshard.history.event import SOURCE_CLAUDE_CODE_HOOKS, make_event

    record = buf.get("record") or {}
    events: list[dict] = []
    for path, change_type in list(buf.get("hook_files", {}).items())[:_MAX_TOOL_FILE_EVENTS]:
        ev = make_event(
            event_type="file.changed",
            source=SOURCE_CLAUDE_CODE_HOOKS,
            action=f"file {change_type}",
            occurred_at=_now(),
            run_id=record.get("run_id"),
            shard_id=record.get("shard_id"),
            attempt_number=record.get("attempt_number"),
            actor=IMPORT_SOURCE,
            target=path,
            status="unknown",
            evidence="agent_reported",
            metadata={"evidence_source": "claude_hook"},
        )
        events.append(ev.to_dict())
    return events


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

    changed_files, files_source = _git_changed_files(buf, repo_root)
    if files_source == "git_diff_inferred":
        file_events = _build_git_file_events(buf, changed_files)
    else:
        changed_files = [
            {"path": p, "change_type": ct, "summary": "reported by Claude Code hook"}
            for p, ct in list(buf.get("hook_files", {}).items())[:_MAX_TOOL_FILE_EVENTS]
        ]
        files_source = "claude_hook_reported" if changed_files else "not_available"
        file_events = _hook_file_events(buf) if changed_files else []

    ended = buf.get("ended") if isinstance(buf.get("ended"), dict) else None
    prompt_count = int(buf.get("prompt_count") or 0)
    tool_calls = int(buf.get("tool_call_count") or 0)
    turn_count = int(buf.get("turn_count") or 0)
    task_status = _task_status(buf, ended)
    # The turn/task outcome is reported independently of SessionEnd: a
    # completed turn already reads as "completed" even while the underlying
    # Claude session is still open. Session-end is appended as a trailing,
    # separate fact -- it never gates or qualifies the turn status above.
    _task_status_text = {
        "turn_completed": f"{turn_count} turn(s) completed",
        "in_progress": "in progress (no turn completed yet)",
        "ended_no_turn": "session ended before any turn completed",
    }[task_status]
    end_text = f" Session ended (reason={ended.get('reason') or 'unknown'})." if ended else ""
    # First sentence kept short: the receipt's Result line shows the first
    # complete sentence (see shard_contract._result_display).
    summary = (
        f"Claude Code session: {len(changed_files)} file(s) changed, {tool_calls} tool call(s). "
        f"{prompt_count} prompt(s), {_task_status_text}, observed via hooks.{end_text}"
    )

    task = buf.get("task") if isinstance(buf.get("task"), str) and buf.get("task") else None

    # Model/cost/tokens -- opportunistically populated by the Claude Code
    # status line (handle_claude_status), never guessed from names/env vars.
    # Absent entirely (not merely None) when never observed, so old readers
    # and the "verification never fabricated" contract both stay honest.
    models_seen = [m for m in (buf.get("models_seen") or []) if isinstance(m, str)][:5]
    model_current = buf.get("model_current") if isinstance(buf.get("model_current"), str) else None
    execution_model = model_current or "unknown"

    cost_total = buf.get("cost_total_usd")
    cost_baseline = buf.get("cost_baseline_usd")
    estimated_cost: float | None = None
    cost_provenance: str | None = None
    if isinstance(cost_total, (int, float)) and isinstance(cost_baseline, (int, float)):
        # Claude Code's own cumulative session cost, windowed to this Shard's
        # session by subtracting the value observed at the first status ping
        # (usually ~0, but some Claude Code versions carry cost over across
        # /clear -- see status-line docs). Never the raw whole-session total.
        estimated_cost = round(max(0.0, float(cost_total) - float(cost_baseline)), 6)
        cost_provenance = "provider_reported"

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
        tokens_provenance = "provider_reported"

    duration_seconds = _turn_duration_seconds(buf)

    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": record["timestamp"],
        "task": task or _TASK_PLACEHOLDER,
        "execution_model": execution_model,
        "executor": EXECUTOR,
        "import_source": IMPORT_SOURCE,
        "import_method": IMPORT_METHOD,
        "import_note": IMPORT_NOTE,
        "files_source": files_source,
        "verification_attempted": False,
        "verification_passed": None,
        "files_created": sum(1 for f in changed_files if f.get("change_type") == "create"),
        "files_updated": sum(1 for f in changed_files if f.get("change_type") == "update"),
        "files_deleted": sum(1 for f in changed_files if f.get("change_type") == "delete"),
        "files_detail": changed_files,
        "git_branch": buf.get("git_branch"),
        "git_head_commit_hash": buf.get("git_head_commit_hash"),
        "git_dirty": buf.get("git_dirty"),
        "summary": summary,
        "run_id": record["run_id"],
        "shard_id": record["shard_id"],
        "attempt_number": record["attempt_number"],
        "capture": {
            "source": CAPTURE_SOURCE,
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
            # Turn completion -- independent of session_end_observed above.
            "task_status": task_status,
            "first_prompt_at": buf.get("first_prompt_at"),
            "last_turn_completed_at": buf.get("last_stop_at"),
            # Model/cost/token provenance (status-line capture; see above).
            "models_seen": models_seen,
            "model_source": "status_line" if model_current else "not_captured",
            "cost_total_usd": cost_total if isinstance(cost_total, (int, float)) else None,
            "cost_baseline_usd": cost_baseline if isinstance(cost_baseline, (int, float)) else None,
            "last_status_ping_at": buf.get("status_last_seen_at"),
        },
    }
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
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD, capture_repo_identity

        identity = capture_repo_identity(repo_root)
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
    outcome = upsert_jsonl(
        repo_root / ".openshard" / "runs.jsonl",
        entry,
        lambda e: _is_session_entry(e, session_id),
    )
    buf["last_fold_at"] = _now()
    return entry, outcome


def sweep_stale_buffers(repo_root: Path, *, max_age_seconds: float = _STALE_BUFFER_SECONDS) -> list[str]:
    """Fold and remove staging buffers of sessions idle for *max_age_seconds*.

    Called (outside the caller's own session lock) on SessionStart. A stale
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
            age = _seconds_since(peek.get("last_activity_at"))
            if age is None or age < max_age_seconds:
                continue
            sid = str(peek.get("session_id") or path.stem)
            if not _SESSION_ID_RE.match(sid):
                continue
            try:
                with history_file_lock(path):
                    buf = _read_buffer(path)
                    if buf is None:
                        continue
                    age = _seconds_since(buf.get("last_activity_at"))
                    if age is None or age < max_age_seconds:
                        continue
                    if _has_activity(buf):
                        _fold(buf, repo_root)
                    path.unlink()
                folded.append(sid)
            except Exception:
                continue
    except Exception:
        pass
    return folded


def _has_activity(buf: dict) -> bool:
    return int(buf.get("prompt_count") or 0) > 0 or int(buf.get("tool_call_count") or 0) > 0


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


def _apply(payload: HookPayload, buf: dict, repo_root: Path) -> tuple[str, bool, bool]:
    """Mutate *buf* for one hook. Returns ``(detail, should_fold, should_delete_buffer)``."""
    buf["last_activity_at"] = _now()
    event = payload.event

    if event == EVENT_SESSION_START:
        source = payload.source or "unknown"
        if source == "compact":
            return "compaction ignored", False, False
        if not buf.get("start_source"):
            buf["start_source"] = source
        if source == "resume":
            _append_event(
                buf, event_type="session.activity", action="Claude Code session resumed",
                status="unknown", evidence="directly_observed", metadata={"hook": event, "source": source},
            )
        return f"session start ({source})", False, False

    if event == EVENT_USER_PROMPT_SUBMIT:
        buf["prompt_count"] = int(buf.get("prompt_count") or 0) + 1
        if not buf.get("first_prompt_at"):
            buf["first_prompt_at"] = _now()
        if not buf.get("task"):
            buf["task"] = sanitize_task_excerpt(payload.prompt)
        _append_event(
            buf, event_type="session.activity", action="user prompt submitted",
            status="unknown", evidence="directly_observed",
            metadata={"hook": event, "prompt_index": buf["prompt_count"]},
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
        target: str | None = None
        action = f"tool {tool}"
        status = "failed" if failed else "unknown"
        if tool in FILE_TOOLS:
            target = _to_repo_relative(payload.file_path, repo_root)
            if target is None and payload.file_path:
                metadata["path_dropped"] = "outside repository"
            if not failed:
                status = "passed"  # PostToolUse only fires when Claude Code applied the edit
                if target:
                    files = buf.setdefault("hook_files", {})
                    if target in files or len(files) < _MAX_HOOK_FILES:
                        files[target] = files.get(target) or ("create" if tool == "Write" else "update")
        elif tool in COMMAND_TOOLS:
            action, target, kind = summarize_command(payload.command)
            metadata["command_kind"] = kind
            # A command exiting non-zero still fires PostToolUse; outcome unknown.
            status = "failed" if failed else "unknown"
        _append_event(
            buf, event_type="tool.invoked", action=action, target=target,
            status=status, evidence="agent_reported", metadata=metadata,
        )
        # Bounded periodic snapshot (see _TOOL_FOLD_INTERVAL_SECONDS): only
        # once a record exists, and never more often than the interval.
        if buf.get("record"):
            since = _seconds_since(buf.get("last_fold_at"))
            if since is None or since >= _TOOL_FOLD_INTERVAL_SECONDS:
                return f"tool {tool} buffered; periodic snapshot", True, False
        return f"tool {tool} buffered", False, False

    if event == EVENT_STOP:
        buf["turn_count"] = int(buf.get("turn_count") or 0) + 1
        buf["last_stop_at"] = _now()
        _append_event(
            buf, event_type="session.activity", action="assistant turn completed",
            status="unknown", evidence="directly_observed",
            metadata={"hook": event, "turn_index": buf["turn_count"]},
        )
        if _has_activity(buf):
            return "turn completed", True, False
        return "turn completed (no work yet; not recorded)", False, False

    if event == EVENT_SESSION_END:
        reason = payload.reason or "unknown"
        buf["ended"] = {"reason": reason, "at": _now()}
        if not _has_activity(buf):
            return "session ended with no work; nothing recorded", False, True
        _append_event(
            buf, event_type="run.completed", action=f"Claude Code session ended (reason={reason})",
            status="unknown", evidence="directly_observed", metadata={"hook": event, "reason": reason},
        )
        return f"session ended ({reason})", True, True

    return "unsupported event", False, False


def handle_claude_hook(
    data: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    event_override: str | None = None,
) -> HookOutcome:
    """Process one decoded Claude Code hook payload. Never raises.

    Safe to call repeatedly: a repeated identical payload only bumps counts
    (tool/prompt/turn) -- it can never create a second record for the same
    session, because the record is upserted by ``capture.session_id``.
    """
    try:
        payload = extract_hook_payload(data, event_override=event_override)
        if payload is None:
            return HookOutcome(event=str(data.get("hook_event_name") or event_override or ""), action="ignored",
                               detail="unsupported or missing hook_event_name")
        if payload.session_id is None:
            return HookOutcome(event=payload.event, action="ignored", detail="missing or invalid session_id")
        repo_root = resolve_repo_root(payload, env)
        if repo_root is None:
            return HookOutcome(event=payload.event, action="ignored", session_id=payload.session_id,
                               detail="could not resolve repository directory")

        from openshard.history.jsonl_store import history_file_lock

        path = buffer_path(repo_root, payload.session_id)
        with history_file_lock(path):
            buf = _load_or_create_buffer(repo_root, payload.session_id, payload.event)
            detail, should_fold, should_delete = _apply(payload, buf, repo_root)
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
        if payload.event == EVENT_SESSION_START:
            sweep_stale_buffers(repo_root)

        record = buf.get("record") or {}
        if entry is not None:
            if payload.event == EVENT_SESSION_END:
                action = "record_finalized"
            elif outcome == "appended":
                action = "record_created"
            else:
                action = "record_updated"
        else:
            action = "buffered" if not should_delete else "ignored"
        return HookOutcome(
            event=payload.event, action=action, session_id=payload.session_id, repo_root=repo_root,
            shard_id=record.get("shard_id"), run_id=record.get("run_id"), detail=detail,
        )
    except Exception as exc:  # observational hook: never propagate
        return HookOutcome(event=str(data.get("hook_event_name") or ""), action="error",
                           detail=f"{type(exc).__name__}")


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


def _apply_status(payload: StatusPayload, buf: dict) -> bool:
    """Merge one status-line observation into *buf*. Returns True if anything changed.

    Never raises. Model ids/cost/token counts are the only new state; no
    Event is appended for a status ping (it is not itself a lifecycle fact
    worth recording, just metadata about facts already recorded elsewhere).
    """
    from openshard.adapters.claude_code_import import _sanitize_model

    changed = False
    buf["status_last_seen_at"] = _now()

    if payload.model_id:
        safe_model = _sanitize_model(payload.model_id)
        if safe_model != "unknown":
            if buf.get("model_current") != safe_model:
                buf["model_current"] = safe_model
                changed = True
            seen = buf.setdefault("models_seen", [])
            if safe_model not in seen and len(seen) < 5:
                seen.append(safe_model)
                changed = True

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
    """
    fallback = _status_line_text(data) if isinstance(data, Mapping) else ""
    try:
        payload = extract_status_payload(data)
        if payload is None:
            return fallback
        repo_root = resolve_repo_root(payload, env)
        if repo_root is None:
            return fallback

        from openshard.history.jsonl_store import history_file_lock

        path = buffer_path(repo_root, payload.session_id)  # type: ignore[arg-type]
        with history_file_lock(path):
            buf = _load_or_create_buffer(repo_root, payload.session_id, "Status")  # type: ignore[arg-type]
            changed = _apply_status(payload, buf)
            if changed and buf.get("record"):
                _fold(buf, repo_root)
            _write_buffer(path, buf)
        return fallback
    except Exception:
        return fallback


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
