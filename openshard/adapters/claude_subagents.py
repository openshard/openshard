"""What each Claude Code subagent of a Receipt did, read from what Claude Code itself writes.

The hooks only say a subagent ran (``SubagentStart`` / ``SubagentStop``:
``agent_id``, ``agent_type``; see ``history/task_edges.py``). Claude Code
also writes, beside the session transcript ``<session>.jsonl``:

* ``<session>/subagents/agent-<id>.jsonl`` -- the subagent's own transcript:
  every API message with its ``model`` and ``usage``, its ``tool_use``
  blocks and a timestamp on every line;
* ``<session>/subagents/agent-<id>.meta.json`` -- ``agentType``,
  ``toolUseId`` (the ``Agent`` call that launched it) and ``spawnDepth``
  (1: launched by the session's lead agent);

and in the session transcript, the launch's outcome: the ``Agent`` tool
result (``toolUseResult.status``, ``completed`` for a foreground subagent)
or, for a background one, a ``<task-notification>`` with its ``<status>``.

From those, per subagent: models, tokens (counted once per message id, as
for the session), a list-rate cost estimate, first and last transcript
timestamps, tool calls, the parent agent and the outcome. Anything not
written stays None with a reason, never 0. Each fact names its source:
``provider_reported`` (the API's usage counters), ``agent_reported``
(what Claude Code wrote) or ``openshard_calculated`` (the cost estimate).

The subagents' costs are compared with the Receipt's own cost: they are
part of it, so a sum above it means the figures do not reconcile, and the
Receipt says so rather than showing both as if they agreed.

Only ids, labels, counts, model ids and timestamps are kept; never a
prompt, output, transcript content or path. Never raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openshard.history.task_edges import MAX_SUBAGENT_EDGES, _agent_id, _agent_type

SUBAGENT_RUNS_FIELD = "subagent_runs"
SUBAGENT_COST_FIELD = "subagent_cost"

RESULT_FINISHED = "finished"
RESULT_FAILED = "failed"
RESULT_UNKNOWN = "unknown"
PARENT_LEAD = "lead"

SOURCE_AGENT = "agent_reported"
SOURCE_PROVIDER = "provider_reported"
SOURCE_OPENSHARD = "openshard_calculated"

REASON_TRANSCRIPT_UNAVAILABLE = "transcript_unavailable"
REASON_NO_USAGE_IN_WINDOW = "no_usage_in_receipt_window"
REASON_USAGE_INCOMPLETE = "usage_incomplete"
REASON_NO_RATE = "no_official_rate_for_usage"
REASON_PARENT_NOT_RECORDED = "parent_not_recorded"
REASON_NOT_STOPPED = "stop_not_observed"
REASON_OUTCOME_NOT_RECORDED = "outcome_not_recorded"
REASON_STATUS_UNRECOGNISED = "status_unrecognised"
REASON_SESSION_COST_UNKNOWN = "session_cost_unknown"
REASON_SUBAGENT_COSTS_UNKNOWN = "subagent_costs_unknown"

RECONCILED = "within_session_total"
NOT_RECONCILED = "exceeds_session_total"
RECONCILE_UNKNOWN = "unknown"

# Claude Code's launch statuses. ``async_launched`` is a launch, not an outcome.
_FINISHED_STATUSES = frozenset({"completed"})
_FAILED_STATUSES = frozenset({"failed", "error", "killed", "cancelled", "canceled", "aborted", "interrupted"})
_PENDING_STATUSES = frozenset({"async_launched", "running"})
_STATUS_RE = re.compile(r"^[a-z_]{1,32}$")
_NOTIFICATION_ID_RE = re.compile(r"<task-id>([A-Za-z0-9_.:-]{1,80})</task-id>")
_NOTIFICATION_STATUS_RE = re.compile(r"<status>([a-z_]{1,32})</status>")
_TOOL_USE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_MAX_OUTCOMES = 64
_MAX_TOOL_USE_IDS = 2_000
_MAX_META_BYTES = 16_384
_MAX_USD_DRIFT = 1e-6


def _subagent_dir(main: Path) -> Path:
    return main.with_suffix("") / "subagents"


def _stamp(value: object) -> str | None:
    from openshard.adapters.claude_transcript_usage import _parse_utc

    return value if isinstance(value, str) and len(value) <= 40 and _parse_utc(value) is not None else None


def _scan_outcomes(buf: dict, main: Path) -> dict[str, str]:
    """Each launched subagent's latest status in the session transcript, read incrementally."""
    from openshard.adapters.claude_transcript_usage import _file_head

    scan = buf.get("subagent_outcome_scan")
    if not isinstance(scan, dict) or not isinstance(scan.get("outcomes"), dict):
        scan = {"offset": 0, "head": None, "outcomes": {}}
    try:
        size = main.stat().st_size
        head = _file_head(main)
        offset = int(scan.get("offset") or 0)
        if size < offset or (offset and scan.get("head") and head != scan.get("head")):
            scan, offset = {"offset": 0, "head": None, "outcomes": {}}, 0  # truncated or replaced
        with main.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return dict(scan["outcomes"])
    end = chunk.rfind(b"\n")
    outcomes: dict[str, str] = scan["outcomes"]
    for raw in chunk[: end + 1].splitlines() if end >= 0 else []:
        if b'"agentId"' not in raw and b"task-notification" not in raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user":
            continue
        found: list[tuple[object, object]] = []
        result = rec.get("toolUseResult")
        if isinstance(result, dict):
            found.append((result.get("agentId"), result.get("status")))
        content = (rec.get("message") or {}).get("content") if isinstance(rec.get("message"), dict) else None
        if isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
            ids, statuses = _NOTIFICATION_ID_RE.findall(content), _NOTIFICATION_STATUS_RE.findall(content)
            if len(ids) == 1 and len(statuses) == 1:
                found.append((ids[0], statuses[0]))
        for agent_id, status in found:
            agent_id = _agent_id(agent_id)
            if agent_id is None or not isinstance(status, str) or not _STATUS_RE.fullmatch(status):
                continue
            if status in _PENDING_STATUSES and agent_id in outcomes:
                continue  # a later launch line never hides an outcome already written
            if agent_id not in outcomes and len(outcomes) >= _MAX_OUTCOMES:
                continue
            outcomes[agent_id] = status
    if end >= 0:
        scan["offset"] = offset + end + 1
    scan["head"] = head
    buf["subagent_outcome_scan"] = scan
    return dict(outcomes)


def _read_meta(sub: Path, agent_id: str) -> dict[str, Any]:
    try:
        path = sub / f"agent-{agent_id}.meta.json"
        if path.stat().st_size > _MAX_META_BYTES:
            return {}
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _activity(file: Path, since: object, until: object) -> dict[str, Any] | None:
    """First/last timestamp, distinct ``tool_use`` ids and the ids of its launched agents, in the window."""
    from openshard.adapters.claude_transcript_usage import _parse_utc

    lo, hi = _parse_utc(since), _parse_utc(until)
    first: str | None = None
    last: str | None = None
    first_at = last_at = None
    tool_uses: set[str] = set()
    try:
        with file.open("rb") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(rec, dict):
                    continue
                at = _parse_utc(rec.get("timestamp"))
                if at is None or (lo is not None and at < lo) or (hi is not None and at >= hi):
                    continue
                if first_at is None or at < first_at:
                    first, first_at = rec["timestamp"], at
                if last_at is None or at > last_at:
                    last, last_at = rec["timestamp"], at
                msg = rec.get("message")
                if rec.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                    continue
                for block in msg["content"]:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and isinstance(block.get("id"), str) and len(tool_uses) < _MAX_TOOL_USE_IDS):
                        tool_uses.add(block["id"])
    except OSError:
        return None
    return {"started_at": _stamp(first), "last_at": _stamp(last), "tool_use_ids": tool_uses}


def _usage(file: Path, since: object, until: object) -> dict | None:
    from openshard.adapters.claude_transcript_usage import (
        _new_transcript_state,
        _read_transcript_increment,
        _summarise_transcript_state,
    )

    state = _new_transcript_state(since, until if isinstance(until, str) else None)
    if not _read_transcript_increment(state, [file]):
        return None
    return _summarise_transcript_state(state)


def _tokens(usage: Mapping[str, Any]) -> dict[str, int]:
    totals = usage.get("totals") or {}
    # ``*_tokens`` keys: ``output`` alone is a key the Platform's privacy guard refuses.
    row = {k: int(totals.get(src) or 0) for k, src in (
        ("input_tokens", "input"), ("output_tokens", "output"), ("cache_read_tokens", "cache_read"),
        ("cache_write_tokens", "cache_creation"))}
    row["total_tokens"] = sum(row.values())
    return row


def _result(edge: Mapping[str, Any], status: str | None) -> tuple[str, str | None]:
    if status in _FINISHED_STATUSES:
        return RESULT_FINISHED, None
    if status in _FAILED_STATUSES:
        return RESULT_FAILED, None
    if status is not None and status not in _PENDING_STATUSES:
        return RESULT_UNKNOWN, REASON_STATUS_UNRECOGNISED
    return RESULT_UNKNOWN, REASON_OUTCOME_NOT_RECORDED if edge.get("stopped") else REASON_NOT_STOPPED


def _parent(meta: Mapping[str, Any], launched_by: Mapping[str, str]) -> dict[str, Any]:
    """Depth 1 is the session's lead; deeper, the subagent whose transcript made the ``Agent`` call."""
    depth, tool_use_id = meta.get("spawnDepth"), meta.get("toolUseId")
    parent = None
    if depth == 1:
        parent = PARENT_LEAD
    elif isinstance(depth, int) and depth > 1 and isinstance(tool_use_id, str) and _TOOL_USE_ID_RE.fullmatch(tool_use_id):
        parent = launched_by.get(tool_use_id)
    if parent is None:
        return {"parent_agent": None, "parent_source": None, "parent_not_recorded_reason": REASON_PARENT_NOT_RECORDED}
    return {"parent_agent": parent, "parent_source": SOURCE_AGENT}


def _usage_facts(usage: Mapping[str, Any] | None, reason: str | None) -> dict[str, Any]:
    """Models, tokens and the list-rate cost estimate from one subagent's usage (None: not recorded, *reason*)."""
    from openshard.adapters.claude_hooks import _transcript_cost
    from openshard.models.pricing import COST_PROVENANCE_OFFICIAL_RATE

    models = [m for m in (usage or {}).get("by_model") or {} if m != "unknown"][:5]
    cost = _transcript_cost(usage) if usage else None
    out: dict[str, Any] = {
        "models": models, "models_source": SOURCE_PROVIDER if models else None,
        "tokens": _tokens(usage) if usage else None, "tokens_source": SOURCE_PROVIDER if usage else None,
        "cost_usd": cost, "cost_estimate": cost is not None,
        "cost_provenance": COST_PROVENANCE_OFFICIAL_RATE if cost is not None else None,
        "cost_source": SOURCE_OPENSHARD if cost is not None else None,
    }
    complete = bool(usage and usage.get("complete"))
    if reason:
        out["tokens_not_recorded_reason"] = reason
    elif not complete:
        out["tokens_incomplete"] = True
    if cost is None:
        out["cost_not_recorded_reason"] = reason or (REASON_NO_RATE if complete else REASON_USAGE_INCOMPLETE)
    return out


def _activity_facts(activity: Mapping[str, Any] | None, edge: Mapping[str, Any], status: str | None,
                    reason: str | None) -> dict[str, Any]:
    """Times, tool calls and the outcome: its transcript's own timestamps and tool_use blocks."""
    result, result_reason = _result(edge, status)
    started = (activity or {}).get("started_at")
    ended = (activity or {}).get("last_at") if result != RESULT_UNKNOWN or edge.get("stopped") else None
    out: dict[str, Any] = {"started_at": started, "ended_at": ended, "times_source": SOURCE_AGENT if started else None}
    if started is None:
        out["times_not_recorded_reason"] = reason or REASON_TRANSCRIPT_UNAVAILABLE
    elif ended is None:
        out["ended_not_recorded_reason"] = REASON_NOT_STOPPED
    calls = len(activity["tool_use_ids"]) if activity is not None and started else None
    out.update({"tool_calls": calls, "tool_calls_source": SOURCE_AGENT if calls is not None else None,
                "result": result, "result_source": SOURCE_AGENT if result != RESULT_UNKNOWN else None})
    if result_reason:
        out["result_reason"] = result_reason
    return out


def subagent_runs(
    edges: list[Mapping[str, Any]],
    main: Path,
    *,
    since: object = None,
    until: object = None,
    usage_elsewhere: str | None = None,
    buf: dict | None = None,
) -> list[dict[str, Any]]:
    """One run per reported subagent *edge*, from the transcripts Claude Code wrote beside *main*."""
    buf = buf if buf is not None else {}
    sub = _subagent_dir(main)
    outcomes = _scan_outcomes(buf, main)
    ids = list(dict.fromkeys(a for a in (_agent_id(e.get("agent_id")) for e in edges[:MAX_SUBAGENT_EDGES]) if a))
    activities = {a: _activity(sub / f"agent-{a}.jsonl", since, until) if (sub / f"agent-{a}.jsonl").is_file() else None
                  for a in ids}
    launched_by: dict[str, str] = {}  # Agent tool_use id -> the subagent whose transcript holds it
    for launcher, launched in activities.items():
        for tool_use_id in (launched or {}).get("tool_use_ids", ()):
            launched_by.setdefault(tool_use_id, launcher)
    runs: list[dict[str, Any]] = []
    for edge in edges[:MAX_SUBAGENT_EDGES]:
        agent_id = _agent_id(edge.get("agent_id"))
        if agent_id is None or any(r["agent_id"] == agent_id for r in runs):
            continue
        activity, meta = activities.get(agent_id), _read_meta(sub, agent_id)
        usage = _usage(sub / f"agent-{agent_id}.jsonl", since, until) if activity and not usage_elsewhere else None
        reason = (usage_elsewhere or (REASON_TRANSCRIPT_UNAVAILABLE if activity is None else None)
                  or (REASON_NO_USAGE_IN_WINDOW if usage is None else None))
        runs.append({
            "agent_id": agent_id,
            "agent_type": _agent_type(edge.get("agent_type")) or _agent_type(meta.get("agentType")),
            "agent_type_source": SOURCE_AGENT,
            **_parent(meta, launched_by),
            **_usage_facts(usage, reason),
            **_activity_facts(activity, edge, outcomes.get(agent_id), reason),
        })
    return runs


def cost_reconciliation(runs: Sequence[Mapping[str, Any]], session_cost: object) -> dict[str, Any] | None:
    """Whether the subagents' costs fit inside the Receipt's cost; None without runs."""
    if not runs:
        return None
    known = [float(r["cost_usd"]) for r in runs if isinstance(r.get("cost_usd"), (int, float))]
    block: dict[str, Any] = {
        "subagents_usd": round(sum(known), 6) if known else None,
        "subagents_priced": len(known),
        "subagents": len(runs),
        "session_usd": float(session_cost) if isinstance(session_cost, (int, float)) and not isinstance(session_cost, bool) else None,
    }
    if block["session_usd"] is None or not known:
        block["status"] = RECONCILE_UNKNOWN
        block["reason"] = REASON_SESSION_COST_UNKNOWN if block["session_usd"] is None else REASON_SUBAGENT_COSTS_UNKNOWN
    elif block["subagents_usd"] > block["session_usd"] + _MAX_USD_DRIFT:
        block["status"] = NOT_RECONCILED
    else:
        block["status"] = RECONCILED
    return block


def stamp_subagent_runs(entry: dict, buf: dict, *, agent: str, session_cost: object) -> None:
    """Add ``capture.subagent_runs`` and ``capture.subagent_cost`` for a Claude Code buffer's reported subagents.

    Other agents expose no such files: their reported edges stand alone.
    """
    from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE
    from openshard.adapters.claude_cross_checkout import usage_since, usage_until
    from openshard.adapters.claude_transcript_usage import _valid_transcript_path

    capture = entry.get("capture")
    if agent != AGENT_CLAUDE_CODE or not isinstance(capture, dict):
        return
    edges = capture.get("subagent_edges")
    if not isinstance(edges, list) or not edges:
        return
    try:
        raw = _valid_transcript_path(buf.get("transcript_path"), buf.get("session_id"))
        if raw is None:
            return  # nothing of Claude Code's was readable: the reported edges stand alone
        elsewhere = buf.get("usage_elsewhere")
        runs = subagent_runs(
            edges, Path(raw), since=usage_since(buf), until=usage_until(buf),
            usage_elsewhere=elsewhere if isinstance(elsewhere, str) else None, buf=buf,
        )
    except Exception:
        return
    if runs:
        capture[SUBAGENT_RUNS_FIELD] = runs
        capture[SUBAGENT_COST_FIELD] = cost_reconciliation(runs, session_cost)


# Which ``HookPayload.attrs`` of a subagent event reach the Event's metadata.
_SUBAGENT_ATTR_KEYS: tuple[str, ...] = (
    "child_role", "child_status", "duration_ms", "tool_calls", "child_subagent_id",
    "child_session_id", "parent_subagent_id",
)


def apply_subagent_event(buf: dict, event: str, attrs: Mapping[str, Any], now: str) -> tuple[str, bool, bool]:
    """Fold one ``SubagentStart`` / ``SubagentStop`` into the buffer (moved from ``claude_hooks._apply``).

    A subagent the agent delegated work to (Claude Code, Hermes ``subagent_start`` /
    ``subagent_stop``). Counted and staged as agent-reported; the delegated goal
    and the child's summary are free text and are never stored. The child's own
    session, if it emits hooks, is its own Shard.
    """
    from openshard.adapters.claude_hooks import EVENT_SUBAGENT_START, _append_event
    from openshard.history.task_edges import note_subagent

    started = event == EVENT_SUBAGENT_START
    counts = buf.get("subagents")
    if not isinstance(counts, dict):
        counts = buf["subagents"] = {}
    child_status = attrs.get("child_status")
    failed_child = (not started) and child_status in ("failed", "error")
    counts["started" if started else "stopped"] = int(counts.get("started" if started else "stopped") or 0) + 1
    if failed_child:
        counts["failed"] = int(counts.get("failed") or 0) + 1
    note_subagent(counts, attrs, started=started)
    meta: dict[str, Any] = {"hook": event}
    for attr_key in _SUBAGENT_ATTR_KEYS:
        if attr_key in attrs:
            meta[attr_key] = attrs[attr_key]
    role = attrs.get("child_role")
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


__all__ = [
    "SUBAGENT_COST_FIELD",
    "apply_subagent_event",
    "SUBAGENT_RUNS_FIELD",
    "cost_reconciliation",
    "stamp_subagent_runs",
    "subagent_runs",
]
