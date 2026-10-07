"""The agent graph of an OSN run: who took part, on which model, and how they were connected.

Derived from what the run recorded (``osn_loop.roles`` with the planner's
explorers, ``osn_loop.workers``, ``osn_loop.candidates``, ``osn_loop.synthesis``,
``osn_loop.reviews``). Nothing here is observed anew: every node and edge
points at a record that already carries its own evidence, and the graph says
so (``evidence.graph: derived_from_recorded_roles``).

Node ids are the agent ids the run used (``planner``, ``executor``,
``verifier``, ``explorer-1``, ``worker-1``, ``candidate-1``) plus
``synthesis``, the harness step that combined workers' or candidates' files
(no model, OpenShard-observed). Edge kinds name what happened between them.
"""
from __future__ import annotations

from typing import Any

EDGE_PLANNED = "planned_for"
EDGE_ASKED = "asked"
EDGE_ANSWERED = "answered"
EDGE_DECOMPOSED = "decomposed_into"
EDGE_PRODUCED = "produced_for"
EDGE_EVALUATED = "evaluated_by"
EDGE_SELECTED = "selected_by"
EDGE_RESOLVED = "resolved_by"
EDGE_VERIFIED_WITHOUT = "verified_without"
EDGE_REVIEWED = "reviewed_by"
EDGE_RECOVERY = "recovery_requested_from"
EDGE_CARRIED = "carried_from_checkpoint"

ROLE_HARNESS = "harness"
MAX_NODES = 16


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _node(agent_id: str, role: str, rec: dict[str, Any] | None, *, parent: str | None = None,
          outcome: str | None = None) -> dict[str, Any]:
    rec = rec or {}
    return {
        "agent_id": agent_id,
        "role": role,
        "parent": parent,
        "status": rec.get("status"),
        "reason": rec.get("reason"),
        "model": rec.get("model"),
        "requested_model": rec.get("requested_model"),
        "model_source": rec.get("source") or rec.get("model_source"),
        "independent": rec.get("independent"),
        "calls": rec.get("calls") if isinstance(rec.get("calls"), int) else None,
        "turns": rec.get("turns") if isinstance(rec.get("turns"), int) else None,
        "cost_usd": _num(rec.get("cost_usd")),
        "cost_source": rec.get("cost_source"),
        "outcome": outcome,
    }


def build_agent_graph(loop: dict[str, Any]) -> dict[str, Any] | None:
    """The run's agents and their edges, or None for a run that recorded no roles."""
    roles: dict[str, Any] = loop["roles"] if isinstance(loop.get("roles"), dict) else {}
    workers = [w for w in (loop.get("workers") or []) if isinstance(w, dict)]
    candidates: dict[str, Any] = loop["candidates"] if isinstance(loop.get("candidates"), dict) else {}
    synthesis: dict[str, Any] = loop["synthesis"] if isinstance(loop.get("synthesis"), dict) else {}
    reviews = [r for r in (loop.get("reviews") or []) if isinstance(r, dict)]
    topology: dict[str, Any] = loop["topology"] if isinstance(loop.get("topology"), dict) else {}
    resumed: dict[str, Any] = loop["resumed"] if isinstance(loop.get("resumed"), dict) else {}
    if not roles and not workers:
        return None

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    def edge(src: str, dst: str, kind: str) -> None:
        edges.append({"from": src, "to": dst, "kind": kind})

    planner = roles.get("planner") if isinstance(roles.get("planner"), dict) else None
    executor = roles.get("executor") if isinstance(roles.get("executor"), dict) else None
    verifier = roles.get("verifier") if isinstance(roles.get("verifier"), dict) else None

    if planner:
        nodes.append(_node("planner", "planner", planner,
                           outcome="plan" if isinstance(loop.get("plan"), dict) else None))
        for e in (planner.get("explorers") or [])[:3]:
            if not isinstance(e, dict):
                continue
            idx = e.get("index") if isinstance(e.get("index"), int) else len([n for n in nodes if n["role"] == "explorer"]) + 1
            eid = f"explorer-{idx}"
            nodes.append(_node(eid, "explorer", e, parent="planner",
                               outcome=f"{e.get('findings_count') or 0} finding(s)"))
            edge("planner", eid, EDGE_ASKED)
            edge(eid, "planner", EDGE_ANSWERED)
    if executor:
        attempts: list[Any] = loop["attempts"] if isinstance(loop.get("attempts"), list) else []
        outcome = loop.get("status")
        nodes.append(_node("executor", "executor", executor,
                           outcome=f"{outcome} after {len(attempts)} attempt(s)" if outcome else None))
        if planner:
            edge("planner", "executor", EDGE_PLANNED)
    winner = candidates.get("winner") if candidates else None
    kind = "candidate" if candidates else "worker"
    for w in workers[:6]:
        wid = str(w.get("worker_id") or f"{kind}-{len(nodes)}")
        own = (w.get("verification") or {}).get("status") if isinstance(w.get("verification"), dict) else None
        outcome = f"own-copy verification {own}" if own else None
        nodes.append(_node(wid, kind, w, parent="planner" if (planner and not candidates) else None, outcome=outcome))
        if planner and not candidates:
            edge("planner", wid, EDGE_DECOMPOSED)
        if candidates:
            edge(wid, "synthesis", EDGE_SELECTED if wid == winner else EDGE_EVALUATED)
        elif synthesis:
            edge(wid, "synthesis", EDGE_PRODUCED)
    if workers and (synthesis or candidates):
        applied = len(synthesis.get("applied") or [])
        nodes.append(_node("synthesis", ROLE_HARNESS, {"status": "ran", "model": None},
                           outcome=f"applied {applied} file(s); " + (
                               f"winner {winner}" if candidates and winner else
                               "no verified candidate" if candidates else
                               str(synthesis.get("resolution") or "none_needed"))))
        if executor:
            resolved_by_executor = synthesis.get("resolution") == "executor_turns" or bool(candidates and not winner)
            edge("synthesis", "executor", EDGE_RESOLVED if resolved_by_executor else EDGE_VERIFIED_WITHOUT)
    if verifier:
        verdicts = [r.get("verdict") for r in reviews if r.get("verdict")]
        nodes.append(_node("verifier", "verifier", verifier,
                           outcome=", ".join(str(v) for v in verdicts) if verdicts else None))
        if executor:
            edge("executor", "verifier", EDGE_REVIEWED)
        if any(r.get("recovery_requested") for r in reviews) and executor:
            edge("verifier", "executor", EDGE_RECOVERY)
    if resumed and resumed.get("attempts_restored"):
        for n in nodes:
            if n["agent_id"] == "executor":
                edge("checkpoint", "executor", EDGE_CARRIED)
                break

    nodes = nodes[:MAX_NODES]
    ids = {n["agent_id"] for n in nodes} | {"checkpoint"}
    edges = [e for e in edges if e["from"] in ids and e["to"] in ids]
    models = sorted({str(n["model"]) for n in nodes if n.get("model")})
    costs = [n["cost_usd"] for n in nodes if n["role"] != ROLE_HARNESS]
    return {
        "topology": topology.get("topology_selected"),
        "agents": len(nodes),
        "models_distinct": models,
        "nodes": nodes,
        "edges": edges,
        # Rounded like every other recorded cost: a plain float sum differs by
        # Python version (3.12 compensates, 3.11 does not) and the total is evidence.
        "cost_usd": round(sum(c for c in costs if c is not None), 6) if costs and all(c is not None for c in costs) else None,
        "evidence": {"graph": "derived_from_recorded_roles", "costs": "provider_usage_per_call",
                     "verification": "openshard_observed"},
    }


__all__ = ["ROLE_HARNESS", "build_agent_graph"]
