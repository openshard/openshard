"""Grok Bot (Cursor) capture: two paths with deliberately different evidence.

Grok Bot is Cursor's always-on cloud teammate. It works on a persistent
*cloud* computer, so none of the local mechanisms OpenShard uses for Claude
Code, Codex, Cursor IDE, OpenCode or Antigravity apply: there are no agent
hooks, no local repository to diff, and (per Cursor staff, forum thread
168182, Aug 2026) Grok Bot does not attach MCP servers running on the
user's machine -- stdio or localhost. This is **not** Grok Build.

Path A -- Enterprise: Action Recording -> OpenTelemetry Export -> here
---------------------------------------------------------------------
Cursor Enterprise teams can turn on *Action Recording* (off by default) on
the Grok Bot admin page and configure *OpenTelemetry Export* (Team Settings).
Cursor's own infrastructure then pushes OTLP/HTTP **protobuf** logs to a
public HTTPS collector the customer runs, resource-tagged
``cursor.surface=grok_bot``. Wire reference:
``cursor.com/docs/enterprise/opentelemetry-export/wire``. Event names are
the constant log *body*:

=====================================  ==========================================
body                                   OpenShard Event
=====================================  ==========================================
``grok_bot_shell_command``             ``tool.invoked`` (``shell.allowed=true``,
                                       status ``unknown`` -- no exit code is
                                       exported) or ``approval.denied``
                                       (``shell.allowed=false``: Cursor's own
                                       shell-policy decision, status ``skipped``)
``grok_bot_mcp_tool_call``             ``tool.invoked``; ``cursor.tool.status``
                                       ``success``/``failure`` -> passed/failed
``grok_bot_browser_navigation``        ``tool.invoked``, status ``unknown``;
                                       only the host is kept
``grok_bot_computer_use_session``      ``tool.invoked``, status ``unknown``;
                                       counts and duration only
``api_request`` / ``api_error``        no Event: token counts / model name /
                                       error count on the conversation's record
=====================================  ==========================================

These events are emitted by **Cursor's platform**, not written by the Bot's
model, so every action Event is ``directly_observed`` -- observed by
Cursor's Action Recording and relayed unchanged; ``metadata.observer`` says
so and ``metadata.provenance`` keeps Cursor's ``client`` (recorded on the
Bot's computer) / ``server`` split. OpenShard itself observed nothing and
ran nothing, so the Shard stays ``external_observed`` / ``partial``.

One ``cursor.conversation.id`` = one Shard (the documented session/join
key). Idempotent on ``cursor.event.id`` (documented deterministic across
Cursor retries and replays): re-ingesting the same export changes nothing.

What Action Recording cannot tell us, and so is never recorded: the task
text (conversation content is a separate opt-in export and is not read),
shell exit codes or output, file changes, a conversation end, whether a
check passed, and cost (only exported as a metric; metrics are ignored).

Path B -- every plan: Bot self-report (agent_reported)
------------------------------------------------------
Individual and Teams plans get no Action Recording, no OpenTelemetry
Export and no audit log. The only integration surfaces are plugins /
remote MCP connectors (must be reachable from the public internet -- the
Bot cannot reach this machine's loopback) and skills. The honest route that
needs no public endpoint: an OpenShard *skill* tells the Bot to run
``openshard grok-bot report`` on the user's desktop through Grok Bot's
*Execution on Local Computer* setting (per-command approval by default),
handing over a JSON report it wrote itself. Everything in it is a claim by
the agent: every Event is ``agent_reported``, verification is an agent
claim, and the record says so. Receiving the report does not make any of
it observed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openshard.adapters.otlp_logs import LogRecord

SURFACE_GROK_BOT = "grok_bot"

EXECUTOR_OTEL = "grok_bot_otel"
EXECUTOR_REPORT = "grok_bot_report"
AGENT_GROK_BOT = "grok_bot"
AGENT_LABEL = "Grok Bot"
AGENT_VENDOR = "Anysphere"

SOURCE_OTEL = "grok_bot_action_recording"
SOURCE_REPORT = "grok_bot_self_report"
OBSERVER_OTEL = "cursor_action_recording"

REPORT_SCHEMA = "openshard.grok_bot.report/v1"

KIND_SHELL = "shell_command"
KIND_MCP = "mcp_tool_call"
KIND_BROWSER = "browser_navigation"
KIND_COMPUTER_USE = "computer_use_session"
KIND_API_REQUEST = "api_request"
KIND_API_ERROR = "api_error"
ACTION_KINDS = (KIND_SHELL, KIND_MCP, KIND_BROWSER, KIND_COMPUTER_USE)

# Log body (or event name) -> kind. Both the body constants from the wire
# reference and the dotted event names from the overview page are accepted.
_BODY_KINDS: dict[str, str] = {
    "grok_bot_shell_command": KIND_SHELL,
    "grok_bot_mcp_tool_call": KIND_MCP,
    "grok_bot_browser_navigation": KIND_BROWSER,
    "grok_bot_computer_use_session": KIND_COMPUTER_USE,
    "api_request": KIND_API_REQUEST,
    "api_error": KIND_API_ERROR,
    "cursor.grok_bot.shell_command": KIND_SHELL,
    "cursor.grok_bot.mcp_tool_call": KIND_MCP,
    "cursor.grok_bot.browser_navigation": KIND_BROWSER,
    "cursor.grok_bot.computer_use_session": KIND_COMPUTER_USE,
    "cursor.api.request": KIND_API_REQUEST,
    "cursor.api.error": KIND_API_ERROR,
}

_MAX_EVENTS = 500
_MAX_EVENT_IDS = 2_000
_MAX_TURN_IDS = 500
_MAX_MODELS = 5
_MAX_CHECKS = 20
_MAX_REPORT_ACTIONS = 200
_MAX_REPORT_FILES = 200
_MAX_REPORT_BYTES = 256 * 1024
_ID_LIMIT = 200

OTEL_TASK_PLACEHOLDER = "Grok Bot conversation (task not captured)"
REPORT_TASK_PLACEHOLDER = "Grok Bot task (self-reported, no task text)"

OTEL_IMPORT_NOTE = (
    "Ingested from Cursor's OpenTelemetry Export of Grok Bot Action Recording (Enterprise). "
    "Actions were recorded by Cursor's platform, not reported by the Bot; OpenShard did not run "
    "or verify anything. Shell exit codes, file changes, the task text, cost and the "
    "conversation end are not exported and stay Not recorded."
)
REPORT_IMPORT_NOTE = (
    "Self-reported by Grok Bot through the OpenShard skill (openshard grok-bot report). "
    "Every fact in this record is the Bot's own claim; nothing was observed by OpenShard "
    "or by Cursor's Action Recording."
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_nanos(nanos: int | None) -> str | None:
    if not isinstance(nanos, int) or nanos <= 0:
        return None
    try:
        return datetime.fromtimestamp(nanos / 1e9, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _str(value: Any, limit: int = _ID_LIMIT) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Normalization: OTLP LogRecord -> Observation
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One Grok Bot fact from Cursor's export, already reduced (nothing raw)."""

    kind: str
    event_id: str
    conversation_id: str
    occurred_at: str | None
    team_id: int | None = None
    turn_id: str | None = None
    box_id: str | None = None
    provenance: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizeSkip:
    reason: str


def _record_kind(rec: LogRecord) -> str | None:
    for candidate in (rec.body, rec.event_name, rec.attributes.get("event.name")):
        if isinstance(candidate, str) and candidate in _BODY_KINDS:
            return _BODY_KINDS[candidate]
    return None


def _fallback_event_id(rec: LogRecord) -> str:
    blob = json.dumps(
        [rec.resource, rec.attributes, rec.body if isinstance(rec.body, str) else None, rec.time_unix_nano],
        sort_keys=True, default=str,
    )
    return "openshard-derived:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _browser_host(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host[:253] if host else None


def normalize_record(rec: LogRecord, *, team_id: int | None = None) -> Observation | NormalizeSkip:
    """Reduce one OTLP log record to an :class:`Observation`, or say why not.

    Only ``cursor.surface=grok_bot`` records are accepted, and -- when
    *team_id* is given -- only that Cursor team's. Only the attributes named
    in the module docstring are read; the Cursor user id is never kept.
    """
    surface = rec.resource.get("cursor.surface")
    if surface != SURFACE_GROK_BOT:
        return NormalizeSkip("not_grok_bot")
    rec_team = _int(rec.resource.get("cursor.team.id"))
    if team_id is not None and rec_team != team_id:
        return NormalizeSkip("other_team")
    kind = _record_kind(rec)
    if kind is None:
        return NormalizeSkip("unsupported_event")
    attrs = rec.attributes
    conversation_id = _str(attrs.get("cursor.conversation.id"))
    if conversation_id is None:
        return NormalizeSkip("no_conversation_id")
    event_id = _str(attrs.get("cursor.event.id"), 300) or _fallback_event_id(rec)
    obs = Observation(
        kind=kind,
        event_id=event_id,
        conversation_id=conversation_id,
        occurred_at=_iso_from_nanos(rec.time_unix_nano) or _iso_from_nanos(rec.observed_time_unix_nano),
        team_id=rec_team,
        turn_id=_str(attrs.get("cursor.grok_bot.turn.id")),
        box_id=_str(attrs.get("cursor.grok_bot.box.id")),
        provenance=_str(attrs.get("cursor.grok_bot.provenance"), 20),
    )
    f = obs.fields
    if kind == KIND_SHELL:
        f["command"] = attrs.get("cursor.grok_bot.shell.command") if isinstance(
            attrs.get("cursor.grok_bot.shell.command"), str) else None
        f["command_truncated"] = _bool(attrs.get("cursor.grok_bot.shell.command_truncated"))
        f["shell_kind"] = _str(attrs.get("cursor.grok_bot.shell.kind"), 20)
        f["target"] = _str(attrs.get("cursor.grok_bot.shell.target"), 20)
        f["allowed"] = _bool(attrs.get("cursor.grok_bot.shell.allowed"))
        f["blocked_reason"] = attrs.get("cursor.grok_bot.shell.blocked_reason") if isinstance(
            attrs.get("cursor.grok_bot.shell.blocked_reason"), str) else None
    elif kind == KIND_MCP:
        f["tool"] = _str(attrs.get("cursor.tool.name"), 120)
        f["server"] = _str(attrs.get("cursor.mcp.server.name"), 120)
        f["status"] = _str(attrs.get("cursor.tool.status"), 20)
        f["transport"] = _str(attrs.get("cursor.grok_bot.mcp.transport"), 20)
        f["duration_ms"] = _int(attrs.get("cursor.grok_bot.mcp.duration_ms"))
        f["tool_call_id"] = _str(attrs.get("cursor.grok_bot.tool_call.id"))
    elif kind == KIND_BROWSER:
        f["host"] = _browser_host(attrs.get("cursor.grok_bot.browser.url"))
    elif kind == KIND_COMPUTER_USE:
        f["action_count"] = _int(attrs.get("cursor.grok_bot.computer_use.action_count"))
        f["screenshot_count"] = _int(attrs.get("cursor.grok_bot.computer_use.screenshot_count"))
        f["duration_ms"] = _int(attrs.get("cursor.grok_bot.computer_use.duration_ms"))
    elif kind == KIND_API_REQUEST:
        for short, key in (
            ("input", "cursor.api.request.input_tokens"),
            ("output", "cursor.api.request.output_tokens"),
            ("cache_read", "cursor.api.request.cache_read_tokens"),
            ("cache_creation", "cursor.api.request.cache_creation_tokens"),
        ):
            f[short] = max(0, _int(attrs.get(key)) or 0)
        f["model"] = _str(attrs.get("cursor.model.name"), 120)
    elif kind == KIND_API_ERROR:
        f["model"] = _str(attrs.get("cursor.model.name"), 120)
    return obs


# ---------------------------------------------------------------------------
# Shared record plumbing
# ---------------------------------------------------------------------------


def _runs_path(repo_root: Path) -> Path:
    return repo_root / ".openshard" / "runs.jsonl"


def _mint_record(repo_root: Path, timestamp: str, session_id: str) -> dict:
    from openshard.adapters.claude_hooks import _count_history_lines
    from openshard.history.receipt_identity import new_receipt_id
    from openshard.history.shard_contract import _make_shard_id

    return {
        "run_id": f"{timestamp}-{hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:8]}",
        "shard_id": _make_shard_id(timestamp, _count_history_lines(repo_root)),
        "receipt_id": new_receipt_id(),
        "attempt_number": 1,
        "timestamp": timestamp,
    }


def _make_event(record: dict, *, source: str, **kwargs: Any) -> dict:
    from openshard.history.event import make_event

    return make_event(
        source=source,
        run_id=record.get("run_id"),
        shard_id=record.get("shard_id"),
        attempt_number=record.get("attempt_number"),
        actor=AGENT_GROK_BOT,
        **kwargs,
    ).to_dict()


def _upsert(repo_root: Path, entry: dict, executor: str, session_id: str) -> str:
    from openshard.adapters.claude_hooks import _is_session_entry
    from openshard.history.jsonl_store import upsert_jsonl

    return upsert_jsonl(
        _runs_path(repo_root), entry, lambda e: _is_session_entry(e, session_id, executor), timeout=10.0,
    )


def _find_entry(repo_root: Path, executor: str, session_id: str) -> dict | None:
    from openshard.adapters.claude_hooks import _find_persisted_entry

    return _find_persisted_entry(repo_root, session_id, executor)


def _identity_fields(repo_root: Path) -> dict:
    try:
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD, capture_repo_identity

        identity = capture_repo_identity(repo_root)
        return {REPO_IDENTITY_FIELD: identity} if identity else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Path A: fold observations into one record per conversation
# ---------------------------------------------------------------------------


def _new_state(conversation_id: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "team_id": None,
        "started_at": None,
        "last_activity_at": None,
        "applied_event_ids": [],
        "counts": {
            KIND_SHELL: 0, "shell_blocked": 0, KIND_MCP: 0, "mcp_failed": 0,
            KIND_BROWSER: 0, KIND_COMPUTER_USE: 0, KIND_API_REQUEST: 0, KIND_API_ERROR: 0,
            "shell_on_user_machine": 0,
        },
        "provenance": {"client": 0, "server": 0},
        "turn_ids": [],
        "models_seen": [],
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "checks": [],
        "checks_total": 0,
        "events_dropped": 0,
        "event_ids_evicted": 0,
    }


def _state_from_entry(entry: dict | None, conversation_id: str) -> tuple[dict, list[dict], dict | None]:
    """(state, events, record identity) for an existing conversation record."""
    if not isinstance(entry, dict):
        return _new_state(conversation_id), [], None
    capture = entry.get("capture") if isinstance(entry.get("capture"), dict) else {}
    stored = capture.get("grok_bot") if isinstance(capture.get("grok_bot"), dict) else {}
    state = _new_state(conversation_id)
    for key, default in list(state.items()):
        value = stored.get(key)
        if isinstance(default, dict) and isinstance(value, dict):
            state[key] = {**default, **{k: v for k, v in value.items() if isinstance(v, int)}}
        elif isinstance(default, list) and isinstance(value, list):
            state[key] = list(value)
        elif default is None or isinstance(default, int):
            if value is not None:
                state[key] = value
    events = [e for e in (entry.get("events") or []) if isinstance(e, dict)]
    record = {
        "run_id": entry.get("run_id"),
        "shard_id": entry.get("shard_id"),
        "receipt_id": entry.get("receipt_id"),
        "attempt_number": entry.get("attempt_number") or 1,
        "timestamp": entry.get("timestamp"),
    }
    return state, events, record


def _obs_metadata(obs: Observation) -> dict:
    meta: dict[str, Any] = {
        "observer": OBSERVER_OTEL,
        "grok_bot_kind": obs.kind,
    }
    if obs.provenance:
        meta["provenance"] = obs.provenance
    if obs.turn_id:
        meta["turn_id"] = obs.turn_id
    return meta


def _action_event(obs: Observation, record: dict, state: dict) -> dict | None:
    """The canonical Event for one action observation (None for usage records)."""
    from openshard.adapters.claude_hooks import summarize_command
    from openshard.history.event import (
        EVENT_APPROVAL_DENIED,
        EVENT_TOOL_INVOKED,
        EVIDENCE_DIRECTLY_OBSERVED,
        STATUS_FAILED,
        STATUS_PASSED,
        STATUS_SKIPPED,
        STATUS_UNKNOWN,
    )

    f = obs.fields
    meta = _obs_metadata(obs)
    common = {
        "source": SOURCE_OTEL,
        "occurred_at": obs.occurred_at,
        "evidence": EVIDENCE_DIRECTLY_OBSERVED,
    }
    if obs.kind == KIND_SHELL:
        action, target, command_kind = summarize_command(f.get("command"), label="Shell")
        meta.update({
            "command_kind": command_kind,
            "shell_target": f.get("target"),
            "shell_kind": f.get("shell_kind"),
            "command_truncated": bool(f.get("command_truncated")),
            "exit_code_observed": False,
        })
        if f.get("allowed") is False:
            meta["decided_by"] = "cursor_shell_policy"
            return _make_event(record, event_type=EVENT_APPROVAL_DENIED, action=f"Blocked by Cursor policy: {action}",
                               target=target, status=STATUS_SKIPPED, metadata=meta, **common)
        return _make_event(record, event_type=EVENT_TOOL_INVOKED, action=action, target=target,
                           status=STATUS_UNKNOWN, metadata=meta, **common)
    if obs.kind == KIND_MCP:
        name = "/".join(p for p in (f.get("server"), f.get("tool")) if p) or "MCP tool"
        status = {"success": STATUS_PASSED, "failure": STATUS_FAILED}.get(f.get("status") or "", STATUS_UNKNOWN)
        meta.update({"transport": f.get("transport"), "duration_ms": f.get("duration_ms"),
                     "status_reported_by": OBSERVER_OTEL})
        return _make_event(record, event_type=EVENT_TOOL_INVOKED, action=f"MCP: {name}",
                           target=f.get("tool"), status=status, metadata=meta, **common)
    if obs.kind == KIND_BROWSER:
        host = f.get("host")
        return _make_event(record, event_type=EVENT_TOOL_INVOKED,
                           action=f"Browser navigation: {host}" if host else "Browser navigation",
                           target=host, status=STATUS_UNKNOWN, metadata=meta, **common)
    if obs.kind == KIND_COMPUTER_USE:
        meta.update({k: f.get(k) for k in ("action_count", "screenshot_count", "duration_ms")})
        return _make_event(record, event_type=EVENT_TOOL_INVOKED,
                           action=f"Computer use session ({f.get('action_count') or 0} action(s))",
                           status=STATUS_UNKNOWN, metadata=meta, **common)
    return None


def _apply_observation(obs: Observation, state: dict, events: list[dict], record: dict) -> bool:
    """Fold one observation into *state*/*events*. False when already applied."""
    from openshard.adapters.claude_hooks import summarize_command

    if obs.event_id in state["applied_event_ids"]:
        return False
    state["applied_event_ids"].append(obs.event_id)
    if len(state["applied_event_ids"]) > _MAX_EVENT_IDS:
        overflow = len(state["applied_event_ids"]) - _MAX_EVENT_IDS
        del state["applied_event_ids"][:overflow]
        state["event_ids_evicted"] = int(state.get("event_ids_evicted") or 0) + overflow

    if obs.team_id is not None and state.get("team_id") is None:
        state["team_id"] = obs.team_id
    if obs.occurred_at:
        if not state["started_at"] or obs.occurred_at < state["started_at"]:
            state["started_at"] = obs.occurred_at
        if not state["last_activity_at"] or obs.occurred_at > state["last_activity_at"]:
            state["last_activity_at"] = obs.occurred_at
    if obs.turn_id and obs.turn_id not in state["turn_ids"] and len(state["turn_ids"]) < _MAX_TURN_IDS:
        state["turn_ids"].append(obs.turn_id)
    if obs.provenance in state["provenance"]:
        state["provenance"][obs.provenance] += 1

    counts = state["counts"]
    counts[obs.kind] = int(counts.get(obs.kind) or 0) + 1
    f = obs.fields
    if obs.kind in (KIND_API_REQUEST, KIND_API_ERROR):
        model = f.get("model")
        if model and model not in state["models_seen"] and len(state["models_seen"]) < _MAX_MODELS:
            state["models_seen"].append(model)
        if obs.kind == KIND_API_REQUEST:
            for key in state["tokens"]:
                state["tokens"][key] += int(f.get(key) or 0)
        return True
    if obs.kind == KIND_SHELL:
        if f.get("allowed") is False:
            counts["shell_blocked"] += 1
        if f.get("target") == "user_machine":
            counts["shell_on_user_machine"] += 1
        action, _target, command_kind = summarize_command(f.get("command"), label="Shell")
        if command_kind in ("test", "lint"):
            state["checks_total"] = int(state.get("checks_total") or 0) + 1
            if len(state["checks"]) < _MAX_CHECKS:
                state["checks"].append({
                    "name": action.removeprefix("Shell: ")[:120],
                    "kind": command_kind,
                    # A blocked command never ran; an allowed one ran with an
                    # exit code Cursor does not export.
                    "status": "skipped" if f.get("allowed") is False else "unknown",
                    "at": obs.occurred_at,
                })
    elif obs.kind == KIND_MCP and f.get("status") == "failure":
        counts["mcp_failed"] += 1

    if len(events) >= _MAX_EVENTS:
        state["events_dropped"] = int(state.get("events_dropped") or 0) + 1
        return True
    ev = _action_event(obs, record, state)
    if ev is not None:
        events.append(ev)
    return True


def _otel_verification(state: dict) -> dict:
    from openshard.history.verification import (
        MODE_HOOK_TOOL_EVENT,
        REASON_CAPTURE_LOSS,
        REASON_CHECKS_TRUNCATED,
        REASON_OUTCOME_NOT_OBSERVED,
        SOURCE_DIRECTLY_OBSERVED,
        STATUS_UNKNOWN,
        build_verification,
    )

    checks = [c for c in state.get("checks") or [] if isinstance(c, dict)]
    total = max(int(state.get("checks_total") or 0), len(checks))
    lost = bool(state.get("events_dropped"))
    if total == 0:
        return build_verification(
            source=SOURCE_DIRECTLY_OBSERVED, observation_mode=MODE_HOOK_TOOL_EVENT,
            status=STATUS_UNKNOWN if lost else None, checks_attempted=None if lost else 0,
            reason=("No check command in Cursor's recorded shell commands"
                    + (", but some events were dropped." if lost else ".")),
            incomplete_reasons=[REASON_CAPTURE_LOSS] if lost else [],
        )
    incomplete = [REASON_OUTCOME_NOT_OBSERVED]
    if total > len(checks):
        incomplete.append(REASON_CHECKS_TRUNCATED)
    if lost:
        incomplete.append(REASON_CAPTURE_LOSS)
    stamps = [c["at"] for c in checks if isinstance(c.get("at"), str)]
    return build_verification(
        source=SOURCE_DIRECTLY_OBSERVED,
        observation_mode=MODE_HOOK_TOOL_EVENT,
        status=STATUS_UNKNOWN,
        checks=[{"name": c.get("name"), "kind": c.get("kind"), "status": c.get("status")} for c in checks],
        checks_attempted=total,
        checks_passed=0,
        checks_failed=0,
        checks_skipped=sum(1 for c in checks if c.get("status") == "skipped"),
        started_at=min(stamps) if stamps else None,
        reason="Check command(s) recorded by Cursor Action Recording; exit codes are not exported.",
        incomplete_reasons=incomplete,
    )


def build_otel_entry(state: dict, events: list[dict], record: dict, repo_root: Path) -> dict:
    """The runs.jsonl record for one Grok Bot conversation seen through Action Recording."""
    from openshard.history.capture_completeness import (
        REASON_DROPPED_HOOK_EVENTS,
        build_completeness,
        make_reason,
    )
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
    from openshard.history.task_title import derive_task_title

    c = state["counts"]
    actions = sum(int(c.get(k) or 0) for k in ACTION_KINDS)
    turns = len(state["turn_ids"])
    losses = []
    if state.get("events_dropped"):
        losses.append(make_reason(REASON_DROPPED_HOOK_EVENTS, int(state["events_dropped"])))
    breakdown = (
        f"{c[KIND_SHELL]} shell ({c['shell_blocked']} blocked by policy), "
        f"{c[KIND_MCP]} MCP ({c['mcp_failed']} failed), "
        f"{c[KIND_BROWSER]} browser, {c[KIND_COMPUTER_USE]} computer-use"
    )
    summary = (
        f"Grok Bot conversation: {actions} action(s) recorded by Cursor. {breakdown}; "
        f"{turns} turn(s). Observed by Cursor Action Recording and exported via OpenTelemetry; "
        f"OpenShard did not run or verify it, and exit codes, file changes and the conversation end "
        f"are not exported."
    )
    tokens = state["tokens"]
    models = list(state["models_seen"])
    entry: dict[str, Any] = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": record["timestamp"],
        "task": OTEL_TASK_PLACEHOLDER,
        "task_title": derive_task_title(OTEL_TASK_PLACEHOLDER),
        "execution_model": models[-1] if models else "unknown",
        "executor": EXECUTOR_OTEL,
        "import_source": AGENT_GROK_BOT,
        "import_method": "openshard_grok_bot_otel_v0",
        "import_note": OTEL_IMPORT_NOTE,
        "files_source": "not_available",
        "verification_attempted": bool(state.get("checks_total")),
        "verification_passed": None,
        "verification": _otel_verification(state),
        "files_created": 0,
        "files_updated": 0,
        "files_deleted": 0,
        "files_detail": [],
        "changes": {"agent_reported": 0, "git_observed": 0, "files_observable": False},
        "summary": summary,
        "run_id": record["run_id"],
        "shard_id": record["shard_id"],
        "attempt_number": record["attempt_number"],
        "capture": {
            "source": SOURCE_OTEL,
            "agent": AGENT_GROK_BOT,
            "agent_vendor": AGENT_VENDOR,
            "provider": None,
            "evidence_level": "platform_observed",
            "observer": OBSERVER_OTEL,
            "session_id": state["conversation_id"],
            "status": "in_progress",
            "session_end_observed": False,
            "session_end_reason": None,
            "started_at": state["started_at"],
            "last_activity_at": state["last_activity_at"],
            "prompt_count": 0,
            "turn_count": turns,
            "tool_call_count": actions,
            "tool_failure_count": int(c.get("mcp_failed") or 0),
            "task_source": "not_captured",
            "task_status": "not_observable",
            "hook_events_dropped": int(state.get("events_dropped") or 0),
            "completeness": build_completeness(losses),
            "models_seen": models,
            "model_source": "cursor_otel_api_request" if models else "not_captured",
            "grok_bot": state,
        },
    }
    if isinstance(record.get("receipt_id"), str) and record["receipt_id"]:
        entry["receipt_id"] = record["receipt_id"]
    if c.get(KIND_API_REQUEST):
        entry["prompt_tokens"] = tokens["input"]
        entry["completion_tokens"] = tokens["output"]
        entry["total_tokens"] = tokens["input"] + tokens["output"]
        entry["cache_read_tokens"] = tokens["cache_read"]
        entry["cache_creation_tokens"] = tokens["cache_creation"]
        entry["tokens_provenance"] = "vendor_telemetry"
    entry.update(_identity_fields(repo_root))
    entry["events"] = events
    return coerce_shard_entry(entry)


@dataclass
class IngestResult:
    records_seen: int = 0
    accepted: int = 0
    duplicates: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    conversations: dict[str, str] = field(default_factory=dict)  # conversation id -> appended/replaced
    shard_ids: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "records_seen": self.records_seen,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "skipped": dict(self.skipped),
            "conversations": len(self.conversations),
            "shards": [
                {"conversation_id": cid, "shard_id": self.shard_ids.get(cid), "outcome": outcome}
                for cid, outcome in self.conversations.items()
            ],
        }


def ingest_log_records(
    records: Iterable[LogRecord], repo_root: Path, *, team_id: int | None = None,
) -> IngestResult:
    """Fold decoded OTLP log records into ``<repo_root>/.openshard/runs.jsonl``.

    Callers must serialize calls for the same repository (the receiver
    handles one request at a time); the read-merge-upsert per conversation
    is not one locked critical section.
    """
    result = IngestResult()
    by_conversation: dict[str, list[Observation]] = {}
    for rec in records:
        result.records_seen += 1
        obs = normalize_record(rec, team_id=team_id)
        if isinstance(obs, NormalizeSkip):
            result.skipped[obs.reason] = result.skipped.get(obs.reason, 0) + 1
            continue
        by_conversation.setdefault(obs.conversation_id, []).append(obs)

    for conversation_id, observations in by_conversation.items():
        observations.sort(key=lambda o: o.occurred_at or "")
        existing = _find_entry(repo_root, EXECUTOR_OTEL, conversation_id)
        state, events, record = _state_from_entry(existing, conversation_id)
        if record is None:
            first = next((o.occurred_at for o in observations if o.occurred_at), None) or _now()
            record = _mint_record(repo_root, first, conversation_id)
        changed = False
        for obs in observations:
            if _apply_observation(obs, state, events, record):
                result.accepted += 1
                changed = True
            else:
                result.duplicates += 1
        if not changed:
            continue
        entry = build_otel_entry(state, events, record, repo_root)
        result.conversations[conversation_id] = _upsert(repo_root, entry, EXECUTOR_OTEL, conversation_id)
        result.shard_ids[conversation_id] = entry.get("shard_id")
    return result


def ingest_otlp_bytes(
    data: bytes, repo_root: Path, *, content_type: str | None = None, team_id: int | None = None,
) -> IngestResult:
    from openshard.adapters.otlp_logs import decode_logs

    return ingest_log_records(decode_logs(data, content_type), repo_root, team_id=team_id)


# ---------------------------------------------------------------------------
# Path B: agent self-report
# ---------------------------------------------------------------------------


class ReportError(ValueError):
    """The self-report is not a valid ``openshard.grok_bot.report/v1`` document."""


_REPORT_STATUSES = {"completed", "failed", "partial", "unknown"}
_REPORT_ACTION_KINDS = {"shell", "mcp", "browser", "computer_use", "file", "other"}
_REPORT_RESULTS = {"passed", "failed", "unknown", "skipped"}
_CHANGE_TYPES = {"create", "update", "delete"}


def _report_session_id(report: Mapping[str, Any]) -> str:
    rid = _str(report.get("report_id")) or _str(report.get("conversation_id"))
    if rid:
        return rid
    blob = json.dumps(report, sort_keys=True, default=str)
    return "report-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def parse_report(raw: bytes | str) -> dict:
    """Decode and validate a self-report. Raises :class:`ReportError`."""
    if isinstance(raw, bytes):
        if len(raw) > _MAX_REPORT_BYTES:
            raise ReportError("report too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReportError("report is not UTF-8") from exc
    if len(raw) > _MAX_REPORT_BYTES:
        raise ReportError("report too large")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReportError(f"report is not JSON: {exc.msg}") from exc
    if not isinstance(doc, dict):
        raise ReportError("report must be a JSON object")
    if doc.get("schema") != REPORT_SCHEMA:
        raise ReportError(f"report.schema must be {REPORT_SCHEMA!r}")
    if not _str(doc.get("task"), 4_000):
        raise ReportError("report.task is required")
    status = doc.get("status", "unknown")
    if status not in _REPORT_STATUSES:
        raise ReportError(f"report.status must be one of {sorted(_REPORT_STATUSES)}")
    for key in ("actions", "files_changed", "checks"):
        if key in doc and not isinstance(doc[key], list):
            raise ReportError(f"report.{key} must be a list")
    return doc


def build_report_entry(report: Mapping[str, Any], repo_root: Path, *, record: dict | None = None,
                       received_at: str | None = None) -> dict:
    """The runs.jsonl record for one self-report. Every fact is agent_reported."""
    from openshard.adapters.claude_hooks import sanitize_task_excerpt, summarize_command
    from openshard.history.capture_completeness import (
        REASON_INTEGRATION_LIMITATION,
        build_completeness,
        make_reason,
    )
    from openshard.history.event import (
        EVENT_FILE_CHANGED,
        EVENT_RUN_COMPLETED,
        EVENT_RUN_FAILED,
        EVENT_TOOL_INVOKED,
        EVENT_VERIFICATION_FAILED,
        EVENT_VERIFICATION_PASSED,
        EVENT_VERIFICATION_SKIPPED,
        EVENT_VERIFICATION_STARTED,
        EVIDENCE_AGENT_REPORTED,
        STATUS_FAILED,
        STATUS_PASSED,
        STATUS_SKIPPED,
        STATUS_UNKNOWN,
        STATUS_WARNING,
    )
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
    from openshard.history.task_title import derive_task_title
    from openshard.history.verification import (
        MODE_AGENT_CLAIM,
        SOURCE_AGENT_REPORTED,
        build_verification,
    )
    from openshard.safety.sanitize import sanitize_path, sanitize_text

    received_at = received_at or _now()
    session_id = _report_session_id(report)
    if record is None:
        record = _mint_record(repo_root, received_at, session_id)
    task = sanitize_task_excerpt(report.get("task")) or REPORT_TASK_PLACEHOLDER
    status = report.get("status") if report.get("status") in _REPORT_STATUSES else "unknown"
    meta_base = {"reported_by": AGENT_GROK_BOT, "observer": None}
    events: list[dict] = []
    common = {"source": SOURCE_REPORT, "evidence": EVIDENCE_AGENT_REPORTED, "occurred_at": received_at}
    result_status = {"passed": STATUS_PASSED, "failed": STATUS_FAILED, "skipped": STATUS_SKIPPED}

    actions_in = [a for a in report.get("actions") or [] if isinstance(a, dict)]
    for a in actions_in[:_MAX_REPORT_ACTIONS]:
        kind = a.get("kind") if a.get("kind") in _REPORT_ACTION_KINDS else "other"
        if kind == "shell":
            action, target, _ = summarize_command(a.get("command") or a.get("name"), label="Shell")
        else:
            name = sanitize_text(a.get("name"), 100) or kind.replace("_", " ")
            action, target = f"{kind}: {name}", sanitize_text(a.get("target"), 80) or None
        events.append(_make_event(
            record, event_type=EVENT_TOOL_INVOKED, action=f"{action} (self-reported)", target=target,
            status=result_status.get(a.get("result"), STATUS_UNKNOWN),
            metadata={**meta_base, "reported_kind": kind}, **common,
        ))

    files_in = [f for f in report.get("files_changed") or [] if isinstance(f, dict)]
    files_detail = []
    for f in files_in[:_MAX_REPORT_FILES]:
        path = sanitize_path(f.get("path"), 300) if isinstance(f.get("path"), str) else None
        if not path:
            continue
        ctype = f.get("change_type") if f.get("change_type") in _CHANGE_TYPES else "update"
        files_detail.append({"path": path, "change_type": ctype, "summary": "reported by Grok Bot",
                             "attribution": "agent_reported", "pre_existing": False})
        events.append(_make_event(
            record, event_type=EVENT_FILE_CHANGED, action=f"{ctype} {path} (self-reported)", target=path,
            target_is_path=True, status=STATUS_UNKNOWN, metadata=dict(meta_base), **common,
        ))

    checks_in = [c for c in report.get("checks") or [] if isinstance(c, dict)]
    checks = []
    for c in checks_in[:_MAX_CHECKS]:
        action, _t, kind = summarize_command(c.get("command") or c.get("name"), label="Check")
        check_status = c.get("result") if c.get("result") in _REPORT_RESULTS else "unknown"
        checks.append({"name": action.removeprefix("Check: ")[:120],
                       "kind": kind if kind in ("test", "lint") else "other", "status": check_status})
        ev_type = {"passed": EVENT_VERIFICATION_PASSED, "failed": EVENT_VERIFICATION_FAILED,
                   "skipped": EVENT_VERIFICATION_SKIPPED}.get(check_status, EVENT_VERIFICATION_STARTED)
        events.append(_make_event(
            record, event_type=ev_type, action=f"{action} (self-reported)",
            status=result_status.get(check_status, STATUS_UNKNOWN), metadata=dict(meta_base), **common,
        ))

    run_event_type = EVENT_RUN_FAILED if status == "failed" else EVENT_RUN_COMPLETED
    run_status = {"completed": STATUS_PASSED, "failed": STATUS_FAILED, "partial": STATUS_WARNING}.get(
        status, STATUS_UNKNOWN)
    events.append(_make_event(
        record, event_type=run_event_type, action=f"Grok Bot reported task status: {status}",
        status=run_status, metadata=dict(meta_base), **common,
    ))

    verification = (
        build_verification(
            source=SOURCE_AGENT_REPORTED, observation_mode=MODE_AGENT_CLAIM, checks=checks,
            checks_attempted=len(checks_in),
            checks_passed=sum(1 for c in checks if c["status"] == "passed"),
            checks_failed=sum(1 for c in checks if c["status"] == "failed"),
            checks_skipped=sum(1 for c in checks if c["status"] == "skipped"),
            reason="Check results stated by Grok Bot; not observed by OpenShard or Cursor.",
        )
        if checks else
        build_verification(source=None, observation_mode=MODE_AGENT_CLAIM,
                           reason="Grok Bot reported no checks; nothing was observed.")
    )
    agent_summary = sanitize_text(report.get("summary"), 300)
    summary = (
        # First sentence kept short: it is the receipt's Result line.
        f"Self-reported, not observed: {status}. "
        f"Grok Bot claims {len(actions_in)} action(s), {len(files_detail)} file(s), "
        f"{len(checks)} check(s); every fact is the Bot's own claim."
        + (f" Bot summary: {agent_summary}" if agent_summary else "")
    )
    model = sanitize_text(report.get("model"), 120)
    entry: dict[str, Any] = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": record["timestamp"],
        "task": task,
        "task_title": derive_task_title(task),
        "execution_model": model or "unknown",
        "executor": EXECUTOR_REPORT,
        "import_source": AGENT_GROK_BOT,
        "import_method": "openshard_grok_bot_report_v0",
        "import_note": REPORT_IMPORT_NOTE,
        "files_source": "grok_bot_self_reported" if files_detail else "not_available",
        "verification_attempted": bool(checks),
        "verification_passed": None,
        "verification": verification,
        "files_created": sum(1 for f in files_detail if f["change_type"] == "create"),
        "files_updated": sum(1 for f in files_detail if f["change_type"] == "update"),
        "files_deleted": sum(1 for f in files_detail if f["change_type"] == "delete"),
        "files_detail": files_detail,
        "changes": {"agent_reported": len(files_detail), "git_observed": 0},
        "summary": summary,
        "run_id": record["run_id"],
        "shard_id": record["shard_id"],
        "attempt_number": record["attempt_number"],
        "capture": {
            "source": SOURCE_REPORT,
            "agent": AGENT_GROK_BOT,
            "agent_vendor": AGENT_VENDOR,
            "provider": None,
            "evidence_level": "agent_reported",
            "observer": None,
            "session_id": session_id,
            "conversation_id": _str(report.get("conversation_id")),
            "status": "ended",
            "session_end_observed": False,
            "reported_status": status,
            "started_at": record["timestamp"],
            "last_activity_at": received_at,
            "tool_call_count": len(actions_in),
            "task_source": "agent_reported" if task != REPORT_TASK_PLACEHOLDER else "not_captured",
            "model_source": "agent_reported" if model else "not_captured",
            # Self-report is partial by construction: whatever the Bot left
            # out is invisible, and nothing in it can be checked here.
            "completeness": build_completeness([make_reason(
                REASON_INTEGRATION_LIMITATION,
                detail="self-report only: anything the Bot did not report is missing",
            )]),
        },
    }
    if isinstance(record.get("receipt_id"), str) and record["receipt_id"]:
        entry["receipt_id"] = record["receipt_id"]
    entry.update(_identity_fields(repo_root))
    entry["events"] = events
    return coerce_shard_entry(entry)


def ingest_report(raw: bytes | str, repo_root: Path) -> dict:
    """Validate a self-report and upsert it (keyed by report_id / conversation_id)."""
    report = parse_report(raw)
    session_id = _report_session_id(report)
    existing = _find_entry(repo_root, EXECUTOR_REPORT, session_id)
    _state, _events, record = _state_from_entry(existing, session_id)
    entry = build_report_entry(report, repo_root, record=record)
    outcome = _upsert(repo_root, entry, EXECUTOR_REPORT, session_id)
    return {"outcome": outcome, "shard_id": entry.get("shard_id"), "report_id": session_id,
            "evidence": "agent_reported"}


# ---------------------------------------------------------------------------
# The skill that drives Path B
# ---------------------------------------------------------------------------

SKILL_MARKDOWN = """---
name: openshard-report
description: Record a finished task in the user's local OpenShard history as a self-reported receipt.
---

# OpenShard report

Use this at the end of any task the user asked you to record in OpenShard.

What this is: a **self-report**. OpenShard stores exactly what you tell it and
labels every fact `agent_reported`. Report only what actually happened; if you
do not know a result, say `unknown`. Never claim a check passed unless you ran
it and saw it pass.

Steps:

1. Build one JSON object:

   ```json
   {
     "schema": "openshard.grok_bot.report/v1",
     "report_id": "<a stable id for this task, reused if you report it again>",
     "task": "<the user's request, one or two sentences>",
     "status": "completed | failed | partial | unknown",
     "summary": "<one or two sentences on the outcome>",
     "actions": [
       {"kind": "shell | mcp | browser | computer_use | file | other",
        "name": "<tool or short description>", "command": "<shell command, shell only>",
        "result": "passed | failed | unknown"}
     ],
     "files_changed": [{"path": "<repo-relative path>", "change_type": "create | update | delete"}],
     "checks": [{"command": "<test/lint command>", "result": "passed | failed | skipped | unknown"}]
   }
   ```

   Do not include secrets, tokens, credentials, personal data or file contents.

2. Run this on the user's local computer (Grok Bot: Execution on Local
   Computer), in the repository whose OpenShard history should receive it,
   passing the JSON on stdin:

   ```
   openshard grok-bot report -
   ```

3. Tell the user the command's output (it prints the Shard id). If local
   execution is not allowed, show the user the JSON and the command so they
   can run it themselves.
"""
