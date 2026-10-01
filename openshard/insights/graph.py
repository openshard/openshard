"""Relationship graph derived from privacy-bounded Receipt analytics.

This is not a graph database and it never becomes a source of truth. Nodes and
edges are rebuilt from the current ReceiptWarehouse so every relationship is
traceable back to canonical Receipt evidence.
"""
from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Any

from openshard.insights.warehouse import ReceiptWarehouse


def _node_id(kind: str, label: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{label}".encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    label: str
    properties: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "properties": self.properties}


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str
    properties: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "properties": self.properties,
        }


@dataclass(frozen=True)
class ReceiptGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    def find_nodes(self, text: str, *, kind: str | None = None) -> list[GraphNode]:
        needle = text.strip().lower()
        return [
            node
            for node in self.nodes
            if (kind is None or node.kind == kind)
            and (not needle or needle in node.label.lower() or needle in node.id.lower())
        ]

    def neighborhood(self, node_id: str, *, depth: int = 1, limit: int = 100) -> ReceiptGraph:
        if depth < 0:
            raise ValueError("depth must be >= 0")
        adjacency: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source, []).append(edge)
            adjacency.setdefault(edge.target, []).append(edge)

        seen = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        chosen_edges: list[GraphEdge] = []
        edge_keys: set[tuple[str, str, str]] = set()

        while queue and len(seen) <= limit:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for edge in adjacency.get(current, []):
                key = (edge.source, edge.target, edge.kind)
                if key not in edge_keys:
                    edge_keys.add(key)
                    chosen_edges.append(edge)
                other = edge.target if edge.source == current else edge.source
                if other not in seen and len(seen) < limit:
                    seen.add(other)
                    queue.append((other, current_depth + 1))

        chosen_nodes = tuple(node for node in self.nodes if node.id in seen)
        return ReceiptGraph(chosen_nodes, tuple(chosen_edges))


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[tuple[str, str, str], GraphEdge] = {}

    def node(self, kind: str, label: str, **properties: Any) -> str:
        node_id = label if kind == "receipt" else _node_id(kind, label)
        existing = self.nodes.get(node_id)
        merged = dict(existing.properties) if existing else {}
        merged.update({k: v for k, v in properties.items() if v is not None})
        self.nodes[node_id] = GraphNode(node_id, kind, label, merged)
        return node_id

    def edge(self, source: str, target: str, kind: str, **properties: Any) -> None:
        key = (source, target, kind)
        existing = self.edges.get(key)
        merged = dict(existing.properties) if existing else {}
        merged.update({k: v for k, v in properties.items() if v is not None})
        self.edges[key] = GraphEdge(source, target, kind, merged)

    def build(self) -> ReceiptGraph:
        nodes = tuple(sorted(self.nodes.values(), key=lambda n: (n.kind, n.label, n.id)))
        edges = tuple(sorted(self.edges.values(), key=lambda e: (e.source, e.kind, e.target)))
        return ReceiptGraph(nodes, edges)


def build_receipt_graph(warehouse: ReceiptWarehouse) -> ReceiptGraph:
    """Build the relationship graph from one warehouse snapshot."""

    builder = _Builder()
    receipts = warehouse.receipt_rows()

    for row in receipts:
        receipt_id = row.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            continue
        receipt_node = builder.node(
            "receipt",
            receipt_id,
            created_at=row.get("created_at"),
            task_title=row.get("task_title"),
            verification_status=row.get("verification_status"),
            verified_success=row.get("verified_success"),
            cost_usd=row.get("cost_usd"),
            failure_category=row.get("failure_category"),
        )

        for kind, field, edge_kind in (
            ("agent", "agent", "USED_AGENT"),
            ("model", "model", "USED_MODEL"),
            ("task", "task_category", "CLASSIFIED_AS"),
            ("repository", "repo_identity", "BELONGS_TO"),
        ):
            value = row.get(field)
            if isinstance(value, str) and value:
                target = builder.node(kind, value)
                builder.edge(receipt_node, target, edge_kind)

    for row in warehouse.file_rows():
        receipt_id, path = row.get("receipt_id"), row.get("path")
        if isinstance(receipt_id, str) and isinstance(path, str) and receipt_id and path:
            target = builder.node("file", path)
            builder.edge(
                receipt_id,
                target,
                "CHANGED_FILE",
                change_type=row.get("change_type"),
                attribution=row.get("attribution"),
            )

    for row in warehouse.check_rows():
        receipt_id, name = row.get("receipt_id"), row.get("name")
        if isinstance(receipt_id, str) and isinstance(name, str) and receipt_id and name:
            target = builder.node("check", name, check_kind=row.get("kind"))
            builder.edge(receipt_id, target, "RAN_CHECK", status=row.get("status"))

    for row in warehouse.policy_rows():
        receipt_id, decision = row.get("receipt_id"), row.get("decision")
        if isinstance(receipt_id, str) and isinstance(decision, str) and receipt_id and decision:
            area = row.get("area") if isinstance(row.get("area"), str) else None
            label = f"{decision}:{area}" if area else decision
            target = builder.node("policy", label, decision=decision, area=area)
            builder.edge(receipt_id, target, "HAD_POLICY_DECISION")

    by_file: dict[str, list[str]] = {}
    for row in warehouse.file_rows():
        receipt_id, path = row.get("receipt_id"), row.get("path")
        if isinstance(receipt_id, str) and isinstance(path, str) and receipt_id and path:
            by_file.setdefault(path, []).append(receipt_id)
    for path, receipt_ids in by_file.items():
        unique = list(dict.fromkeys(receipt_ids))
        if len(unique) < 2:
            continue
        for previous, current in zip(unique, unique[1:]):
            builder.edge(previous, current, "SHARES_CHANGED_FILE", path=path)

    return builder.build()
