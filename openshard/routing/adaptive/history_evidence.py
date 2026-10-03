"""Observed routing history, gated so it is used only when it means something.

The long-term routing target is *cost per independently verified successful
task*. This module turns recorded runs into per-model evidence for that metric
and decides, transparently, whether there is enough of it to route on:

* only outcomes with an independently observed verification count
  (``RoutingOutcome.verified_success`` is not None): an agent's own claim or an
  unknown status is never a success and never a failure;
* a model's evidence is *meaningful* only with at least ``MIN_VERIFIED_SAMPLES``
  verified outcomes for the same harness;
* the policy uses history only when at least ``MIN_MODELS_WITH_EVIDENCE``
  eligible candidates have meaningful evidence, so one lucky model cannot be
  preferred on a handful of runs against untested peers;
* cost per verified success is reported only when every verified outcome in
  the sample has a known cost (spend on failures is part of the price).

When the gate is not met the decision records ``history_evidence.used: false``
and why. No rate is ever invented for a model without observations.

History that could not be read in time (a precomputed snapshot that was late,
missing, corrupt or truncated) is *unavailable*, not empty: ``availability``
records why, and the gate reports ``history_<availability>`` with unknown
counts rather than claiming ``no_history``.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openshard.routing.adaptive.outcome import RoutingOutcome, outcome_from_receipt
from openshard.routing.requirements import ObservedEvidence

HISTORY_EVIDENCE_VERSION = 1
MIN_VERIFIED_SAMPLES = 5
MIN_MODELS_WITH_EVIDENCE = 2
MAX_ENTRIES_SCANNED = 5000

REASON_NO_HISTORY = "no_history"
REASON_INSUFFICIENT = "insufficient_observed_data"
REASON_TOO_FEW_MODELS = "too_few_candidates_with_evidence"
REASON_USED = "used"


@dataclass(frozen=True)
class ModelHistory:
    model: str
    harness: str | None
    verified: int
    successes: int
    cost_known: int
    total_cost_usd: float | None

    @property
    def meaningful(self) -> bool:
        return self.verified >= MIN_VERIFIED_SAMPLES

    def as_evidence(self) -> ObservedEvidence:
        rate = self.successes / self.verified if self.verified else 0.0
        cost = None
        if self.successes and self.cost_known == self.verified and self.total_cost_usd is not None:
            cost = self.total_cost_usd / self.successes
        return ObservedEvidence(samples=self.verified, verified_success_rate=rate, cost_per_verified_success=cost)


@dataclass(frozen=True)
class HistoryEvidence:
    """Everything a policy needs to decide whether to route on history."""

    per_model: dict[str, ModelHistory] = field(default_factory=dict)
    harness: str | None = None
    entries_scanned: int | None = 0
    availability: str = "available"  # anything else: history could not be read, counts unknown
    snapshot_id: str | None = None  # the precomputed learning snapshot this came from, if any

    def meaningful_for(self, candidates: Iterable[str]) -> dict[str, ObservedEvidence]:
        """Evidence for the *candidates* that clear the per-model sample gate."""
        out: dict[str, ObservedEvidence] = {}
        for mid in candidates:
            h = self.per_model.get(mid)
            if h is not None and h.meaningful:
                out[mid] = h.as_evidence()
        return out

    def gate(self, candidates: Iterable[str]) -> tuple[Mapping[str, ObservedEvidence] | None, dict[str, Any]]:
        """(evidence to rank on or None, record). The record says what was used and why not."""
        ids = list(candidates)
        meaningful = self.meaningful_for(ids)
        record: dict[str, Any] = {
            "version": HISTORY_EVIDENCE_VERSION,
            "used": False,
            "reason": REASON_NO_HISTORY,
            "min_verified_samples": MIN_VERIFIED_SAMPLES,
            "min_models_with_evidence": MIN_MODELS_WITH_EVIDENCE,
            "harness": self.harness,
            "entries_scanned": self.entries_scanned,
            "candidates_with_evidence": len(meaningful),
            "verified_outcomes_for_candidates": sum(
                self.per_model[m].verified for m in ids if m in self.per_model
            ),
        }
        if self.snapshot_id is not None:
            record["snapshot_id"] = self.snapshot_id
        if self.availability != "available":
            record.update(reason="history_" + self.availability, entries_scanned=None,
                          candidates_with_evidence=None, verified_outcomes_for_candidates=None)
            return None, record
        if not self.per_model:
            return None, record
        if not meaningful:
            record["reason"] = REASON_INSUFFICIENT
            return None, record
        if len(meaningful) < MIN_MODELS_WITH_EVIDENCE:
            record["reason"] = REASON_TOO_FEW_MODELS
            return None, record
        record.update({"used": True, "reason": REASON_USED})
        return meaningful, record


def build_history_evidence(
    outcomes: Iterable[RoutingOutcome], *, harness: str | None = None
) -> HistoryEvidence:
    """Aggregate verified outcomes per final model. Pure."""
    acc: dict[str, dict[str, Any]] = {}
    scanned = 0
    for o in outcomes:
        scanned += 1
        if harness is not None and o.harness != harness:
            continue
        if o.verified_success is None or not o.final_model:
            continue
        a = acc.setdefault(o.final_model, {"verified": 0, "successes": 0, "cost_known": 0, "cost": 0.0})
        a["verified"] += 1
        a["successes"] += 1 if o.verified_success else 0
        if o.cost_usd is not None:
            a["cost_known"] += 1
            a["cost"] += o.cost_usd
    per_model = {
        mid: ModelHistory(
            model=mid, harness=harness, verified=a["verified"], successes=a["successes"],
            cost_known=a["cost_known"],
            total_cost_usd=a["cost"] if a["cost_known"] == a["verified"] else None,
        )
        for mid, a in sorted(acc.items())
    }
    return HistoryEvidence(per_model=per_model, harness=harness, entries_scanned=scanned)


def history_evidence_from_lines(lines: Iterable[str], *, harness: str | None = None) -> HistoryEvidence:
    """Evidence from the first ``MAX_ENTRIES_SCANNED`` lines of a ``runs.jsonl``. Pure."""
    outcomes: list[RoutingOutcome] = []
    for i, line in enumerate(lines):
        if i >= MAX_ENTRIES_SCANNED:
            break
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            try:
                outcomes.append(outcome_from_receipt(entry))
            except Exception:
                continue
    return build_history_evidence(outcomes, harness=harness)


def load_history_evidence(runs_path: Path, *, harness: str | None = None) -> HistoryEvidence:
    """Read ``runs.jsonl`` and build evidence. Never raises; a missing or
    unreadable file is simply no history."""
    try:
        with runs_path.open(encoding="utf-8") as fh:
            return history_evidence_from_lines(fh, harness=harness)
    except OSError:
        return HistoryEvidence(harness=harness)


__all__ = [
    "HISTORY_EVIDENCE_VERSION",
    "MIN_MODELS_WITH_EVIDENCE",
    "MIN_VERIFIED_SAMPLES",
    "REASON_INSUFFICIENT",
    "REASON_NO_HISTORY",
    "REASON_TOO_FEW_MODELS",
    "REASON_USED",
    "HistoryEvidence",
    "ModelHistory",
    "build_history_evidence",
    "history_evidence_from_lines",
    "load_history_evidence",
]
