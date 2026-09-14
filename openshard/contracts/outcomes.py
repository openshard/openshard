"""Outcome boundary: what happened to the work after the run.

Accepted, merged, deployed, rolled back, reverted, rejected. Outcomes arrive
late (a PR merges hours later), from a different system (GitHub, CI, a
deploy pipeline) and from a different actor than the run. They are therefore
recorded **beside** the run record, never by editing it, so the receipt's
content hash stays valid. ``openshard.history.outcomes`` is the local
recorder; hosted outcome ingestion lives in OpenShard Cloud.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from openshard.contracts._common import now_iso

OUTCOME_STATUSES: frozenset[str] = frozenset({
    "pending", "accepted", "rejected", "merged", "deployed", "rolled_back", "reverted", "partial",
})


@dataclass
class OutcomeReport:
    shard_id: str
    status: str
    source: str  # "cli" | "github" | "ci" | "deploy" | "dashboard" | ...
    reference: str | None = None  # e.g. "PR #341", a deploy id; short and path-free
    recorded_by: dict | None = None  # Principal dict
    human_intervention: bool | None = None
    note: str | None = None
    recorded_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(f"unknown outcome status {self.status!r}; expected one of {sorted(OUTCOME_STATUSES)}")

    def to_receipt_block(self) -> dict:
        """The receipt's ``outcome`` block."""
        return {
            "status": self.status,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "reference": self.reference,
            "human_intervention": self.human_intervention,
        }


class OutcomeReporter(Protocol):
    def report(self, outcome: OutcomeReport) -> bool: ...


class RecordingOutcomeReporter:
    def __init__(self) -> None:
        self.reports: list[OutcomeReport] = []

    def report(self, outcome: OutcomeReport) -> bool:
        self.reports.append(outcome)
        return True
