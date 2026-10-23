"""Task edges: the declared parent task and the subagents an agent reported.

Two kinds of edge, each keeping its own evidence level:

* **Parent** -- ``parent_task_id`` in the record's ``correlation`` block,
  declared by the launcher (``OPENSHARD_PARENT_TASK_ID``, see
  ``history/task_identity.py``). ``evidence: declared``; never inferred.
* **Children** -- ``capture.subagent_edges``: the subagent ids the agent's own
  hooks reported (Claude Code ``SubagentStart`` / ``SubagentStop``
  ``agent_id`` and ``agent_type``). ``evidence: agent_reported``: OpenShard
  saw the agent say a subagent ran, not the subagent's work, and verified
  nothing about it.

Only ids and the agent's type label are kept; never the subagent's prompt,
transcript or output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EVIDENCE_AGENT_REPORTED = "agent_reported"
MAX_SUBAGENT_EDGES = 16
SUBAGENT_EDGES_FIELD = "subagent_edges"
SUBAGENT_RUNS_FIELD = "subagent_runs"
SUBAGENT_COST_FIELD = "subagent_cost"

# Hook-attribute keys the shared subagent fold already reads (``claude_hooks._SUBAGENT_ATTR_KEYS``).
ATTR_CHILD_ID = "child_subagent_id"
ATTR_CHILD_ROLE = "child_role"

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_AGENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. :-]{0,63}$")
_COUNT_NAMES = ("started", "stopped", "failed")


def _agent_id(value: object) -> str | None:
    return value if isinstance(value, str) and _AGENT_ID_RE.fullmatch(value) else None


def _agent_type(value: object) -> str | None:
    return value if isinstance(value, str) and _AGENT_TYPE_RE.fullmatch(value) else None


def claude_subagent_attrs(event: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """The hook attributes a Claude Code ``SubagentStart`` / ``SubagentStop`` payload carries.

    Only ``agent_id`` and ``agent_type``; anything malformed is left out.
    """
    if event not in ("SubagentStart", "SubagentStop"):
        return {}
    attrs: dict[str, Any] = {}
    if (agent_id := _agent_id(data.get("agent_id"))) is not None:
        attrs[ATTR_CHILD_ID] = agent_id
    if (agent_type := _agent_type(data.get("agent_type"))) is not None:
        attrs[ATTR_CHILD_ROLE] = agent_type
    return attrs


def _clean_edges(raw: object) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        agent_id = _agent_id(item.get("agent_id")) if isinstance(item, dict) else None
        if agent_id is None or any(e["agent_id"] == agent_id for e in out) or len(out) >= MAX_SUBAGENT_EDGES:
            continue
        out.append({
            "agent_id": agent_id,
            "agent_type": _agent_type(item.get("agent_type")),
            "started": item.get("started") is True,
            "stopped": item.get("stopped") is True,
            "evidence": EVIDENCE_AGENT_REPORTED,
        })
    return out


def note_subagent(counts: dict, attrs: Mapping[str, Any], *, started: bool) -> None:
    """Record one reported subagent start/stop as a child edge in the buffer's subagent *counts*.

    A payload without a well-formed id adds no edge (its count still moves).
    At most :data:`MAX_SUBAGENT_EDGES` distinct ids are kept.
    """
    agent_id = _agent_id(attrs.get(ATTR_CHILD_ID))
    if agent_id is None:
        return
    edges = _clean_edges(counts.get("edges"))
    edge = next((e for e in edges if e["agent_id"] == agent_id), None)
    if edge is None:
        if len(edges) >= MAX_SUBAGENT_EDGES:
            counts["edges"] = edges
            return
        edge = {"agent_id": agent_id, "agent_type": None, "started": False, "stopped": False,
                "evidence": EVIDENCE_AGENT_REPORTED}
        edges.append(edge)
    edge["started" if started else "stopped"] = True
    edge["agent_type"] = edge["agent_type"] or _agent_type(attrs.get(ATTR_CHILD_ROLE))
    counts["edges"] = edges


def restore_subagents(capture: object) -> dict:
    """The buffer's subagent counts and child edges, rebuilt from a persisted ``capture`` block."""
    capture = capture if isinstance(capture, dict) else {}
    raw = capture.get("subagents")
    counts: dict[str, Any] = {}
    if isinstance(raw, dict):
        counts.update({n: int(raw.get(n) or 0) for n in _COUNT_NAMES if isinstance(raw.get(n), int)})
    edges = _clean_edges(capture.get(SUBAGENT_EDGES_FIELD))
    if edges:
        counts["edges"] = edges
    return counts


def stamp_subagent_edges(entry: dict, buf: Mapping[str, Any]) -> None:
    """Copy the buffer's child edges onto the record being built (absent when there are none)."""
    counts = buf.get("subagents")
    edges = _clean_edges(counts.get("edges")) if isinstance(counts, dict) else []
    capture = entry.get("capture")
    if edges and isinstance(capture, dict):
        capture[SUBAGENT_EDGES_FIELD] = edges


def task_edges_block(entry: object) -> dict[str, Any] | None:
    """``{parent_task_id, parent_evidence, children}`` for *entry*, or None when it records neither."""
    from openshard.history.correlation import correlation_block

    if not isinstance(entry, dict):
        return None
    parent = (correlation_block(entry.get("correlation")) or {}).get("parent_task_id")
    capture = entry.get("capture")
    children = _clean_edges(capture.get(SUBAGENT_EDGES_FIELD)) if isinstance(capture, dict) else []
    if parent is None and not children:
        return None
    block: dict[str, Any] = {
        "parent_task_id": parent,
        "parent_evidence": "declared" if parent else None,
        "children": children,
    }
    # What each subagent did, when capture read it (adapters/claude_subagents.py).
    runs = capture.get(SUBAGENT_RUNS_FIELD) if isinstance(capture, dict) else None
    by_id = {r["agent_id"]: r for r in runs if isinstance(r, dict) and isinstance(r.get("agent_id"), str)} if isinstance(runs, list) else {}
    if by_id:
        block["children"] = [{**child, "run": by_id.get(child["agent_id"])} for child in children]
        cost = capture.get(SUBAGENT_COST_FIELD) if isinstance(capture, dict) else None
        block["subagent_cost"] = cost if isinstance(cost, dict) else None
    return block


def task_edge_lines(block: object) -> list[tuple[str, str]]:
    """``(label, value)`` rows for a receipt: the declared parent, then each reported subagent."""
    if not isinstance(block, dict):
        return []
    rows: list[tuple[str, str]] = []
    if isinstance(block.get("parent_task_id"), str):
        rows.append(("Parent task", f"{block['parent_task_id']} (declared)"))
    for child in block.get("children") or []:
        kind = f" {child['agent_type']}" if child.get("agent_type") else ""
        run = child.get("run")
        if isinstance(run, dict):
            rows.append(("Subagent", f"{child['agent_id']}{kind} · {_run_text(run)}"))
            continue
        state = "stopped" if child.get("stopped") else "started"
        rows.append(("Subagent", f"{child['agent_id']}{kind} · {state} (agent-reported)"))
    cost = block.get("subagent_cost")
    if isinstance(cost, dict) and cost.get("status") == "exceeds_session_total":
        rows.append(("Subagent cost", f"${cost['subagents_usd']:.4f} est. is more than this Receipt's "
                                      f"${cost['session_usd']:.4f}: the figures do not reconcile"))
    return rows


def _seconds(start: object, end: object) -> float | None:
    from datetime import datetime

    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (last - first).total_seconds() if start and end and last >= first else None


def _run_text(run: dict) -> str:
    """One subagent's recorded facts; anything not recorded says so, never 0."""
    parts = []
    parent = run.get("parent_agent")
    parts.append("from lead" if parent == "lead" else f"from {parent}" if isinstance(parent, str) else "parent not recorded")
    models = run.get("models") or []
    parts.append(", ".join(models) if models else "model not recorded")
    tokens = run.get("tokens")
    if isinstance(tokens, dict):
        parts.append(f"{tokens['total_tokens']:,} tokens" + (" (incomplete)" if run.get("tokens_incomplete") else ""))
    else:
        parts.append("tokens not recorded")
    cost = run.get("cost_usd")
    parts.append(f"${cost:.4f} est." if isinstance(cost, (int, float)) else "cost not recorded")
    seconds = _seconds(run.get("started_at"), run.get("ended_at"))
    if seconds is not None:
        parts.append(f"{seconds:.0f}s")
    calls = run.get("tool_calls")
    parts.append(f"{calls} tool call{'' if calls == 1 else 's'}" if isinstance(calls, int) else "tool calls not recorded")
    parts.append(f"{run.get('result') or 'unknown'} (agent-reported)")
    return " · ".join(parts)


__all__ = [
    "EVIDENCE_AGENT_REPORTED",
    "MAX_SUBAGENT_EDGES",
    "claude_subagent_attrs",
    "note_subagent",
    "restore_subagents",
    "stamp_subagent_edges",
    "task_edge_lines",
    "task_edges_block",
]
