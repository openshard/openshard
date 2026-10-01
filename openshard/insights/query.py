"""Small deterministic question layer over Receipt analytics.

The goal is not a general SQL chatbot. It maps common product questions to
evidence-backed aggregate queries and returns the supporting rows so a caller
can inspect exactly what the answer was based on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openshard.insights.graph import build_receipt_graph
from openshard.insights.warehouse import ReceiptWarehouse


@dataclass(frozen=True)
class InsightAnswer:
    question: str
    intent: str
    summary: str
    data: Any
    caveat: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "intent": self.intent,
            "summary": self.summary,
            "data": self.data,
            "caveat": self.caveat,
        }


def _pct(value: object) -> str:
    return "unknown" if not isinstance(value, (int, float)) else f"{float(value) * 100:.1f}%"


def answer_question(warehouse: ReceiptWarehouse, question: str) -> InsightAnswer:
    q = question.strip().lower()
    if not q:
        raise ValueError("question cannot be empty")

    if any(term in q for term in ("model", "models")) and any(
        term in q for term in ("best", "perform", "pass", "reliable", "work")
    ):
        rows = warehouse.models()
        observed = [row for row in rows if (row.get("observed") or 0) > 0]
        if not observed:
            return InsightAnswer(
                question,
                "model_performance",
                "No model has independently observed pass/fail evidence yet.",
                rows,
                "Agent-reported success is intentionally excluded from the pass rate.",
            )
        top = observed[0]
        return InsightAnswer(
            question,
            "model_performance",
            f"{top['model']} has {_pct(top.get('pass_rate'))} observed verification pass rate "
            f"across {top.get('observed', 0)} observed run(s).",
            rows,
            "This is historical correlation, not a claim that the model will outperform alternatives.",
        )

    if any(term in q for term in ("agent", "agents")) and any(
        term in q for term in ("fail", "failure", "reliable", "problem")
    ):
        rows = warehouse.agents()
        observed = [row for row in rows if (row.get("observed") or 0) > 0]
        if not observed:
            return InsightAnswer(
                question,
                "agent_failures",
                "No agent has independently observed pass/fail evidence yet.",
                rows,
            )
        top = observed[0]
        return InsightAnswer(
            question,
            "agent_failures",
            f"{top['agent']} has {_pct(top.get('failure_rate'))} observed failure rate "
            f"across {top.get('observed', 0)} observed run(s).",
            rows,
            "Rates only use Receipts with independently observed pass/fail evidence.",
        )

    if any(term in q for term in ("cost", "spend", "expensive", "money")):
        by = "agent" if "agent" in q else "task" if "task" in q else "model"
        rows = warehouse.costs(by=by)
        if not rows or rows[0].get("known_cost_usd") is None:
            return InsightAnswer(
                question,
                "cost",
                "No trustworthy cost evidence is available for this history yet.",
                rows,
                "Unknown cost is never treated as $0.",
            )
        top = rows[0]
        return InsightAnswer(
            question,
            "cost",
            f"{top['label']} accounts for ${float(top['known_cost_usd']):.4f} of known recorded cost.",
            rows,
            "Only Receipts with recorded or truthfully estimated cost are included.",
        )

    if any(term in q for term in ("check", "test", "verification")) and any(
        term in q for term in ("catch", "fail", "useful", "find")
    ):
        rows = warehouse.checks()
        if not rows:
            return InsightAnswer(question, "checks", "No structured check evidence is recorded yet.", rows)
        top = rows[0]
        return InsightAnswer(
            question,
            "checks",
            f"{top['name']} caught a failure in {top.get('runs_caught', 0)} recorded run(s).",
            rows,
            "A check that never failed has not yet demonstrated that it catches defects.",
        )

    if any(term in q for term in ("failure", "failures", "wrong", "break")):
        rows = warehouse.failures()
        if not rows:
            return InsightAnswer(question, "failures", "No classified failures are recorded.", rows)
        top = rows[0]
        return InsightAnswer(
            question,
            "failures",
            f"{top['failure_category']} is the most frequently recorded failure category "
            f"({top.get('runs', 0)} run(s)).",
            rows,
        )

    if "learning" in q or "self-improv" in q or "self improve" in q:
        data = warehouse.learning()
        return InsightAnswer(
            question,
            "learning",
            f"Learning usage is recorded on {data['recorded']} run(s); it was used on "
            f"{data['used']} and influenced applied routing on {data['routing_applied']} run(s).",
            data,
            "This is observational evidence; it does not prove learning caused the outcome.",
        )

    if "graph" in q or "relationship" in q or "connected" in q:
        graph = build_receipt_graph(warehouse)
        data = {"nodes": len(graph.nodes), "edges": len(graph.edges)}
        return InsightAnswer(
            question,
            "graph",
            f"The current Receipt graph has {data['nodes']} nodes and {data['edges']} evidence-backed edges.",
            data,
        )

    overview = warehouse.overview()
    return InsightAnswer(
        question,
        "overview",
        f"{overview['receipts']} Receipt(s), {overview['observed']} with independently observed "
        f"verification and {overview['receipts_with_cost']} with known cost.",
        overview,
    )
