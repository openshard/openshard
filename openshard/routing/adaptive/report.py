"""Descriptive routing report over recorded runs.

Groups derived ``RoutingOutcome`` records by (routing class, final model) and
summarises them with ``summarize_outcomes``. This is a *descriptive* read of
what was recorded, not a ranking or a learned score: rates cover only the
outcomes whose value is known, and every group reports its coverage so a
small or unverified sample is visible rather than hidden.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any

from openshard.routing.adaptive.evaluation import summarize_outcomes
from openshard.routing.adaptive.outcome import RoutingOutcome, outcome_from_receipt

REPORT_VERSION = 1
UNKNOWN = "unknown"


def _group_key(o: RoutingOutcome) -> tuple[str, str]:
    return (o.routing_class or UNKNOWN, o.final_model or o.routed_model or UNKNOWN)


def _summary(items: list[RoutingOutcome]) -> dict[str, Any]:
    d = asdict(summarize_outcomes(items))
    d.pop("expectation_mismatches", None)  # scenario-only metric
    # Attempts are unknown after a retry, so a mean over the known ones can only
    # ever read 1.0. Keep the coverage count, drop the misleading mean.
    d.pop("mean_attempts", None)
    return d


def build_routing_report(entries: list[dict]) -> dict[str, Any]:
    """Pure: run entries (oldest first) -> JSON-safe report. Never raises on
    a malformed entry; it is counted as skipped."""
    outcomes: list[RoutingOutcome] = []
    skipped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        try:
            outcomes.append(outcome_from_receipt(entry))
        except Exception:
            skipped += 1

    groups: dict[tuple[str, str], list[RoutingOutcome]] = defaultdict(list)
    for o in outcomes:
        groups[_group_key(o)].append(o)

    rows = []
    for (cls, model), items in sorted(groups.items()):
        summary = _summary(items)
        rows.append({
            "routing_class": cls,
            "model": model,
            "runs": len(items),
            "escalations": sum(1 for o in items if o.escalation_model),
            "retries_observed": sum(1 for o in items if o.retry_observed),
            **summary,
        })

    overall = _summary(outcomes)
    return {
        "version": REPORT_VERSION,
        "runs": len(outcomes),
        "skipped_malformed": skipped,
        "overall": overall,
        "groups": rows,
        "notes": [
            "Descriptive statistics over recorded runs; not a learned or ranked router.",
            "Rates use only outcomes with a known value; see *_known counts for coverage.",
            "verified_success is set only from directly observed verification.",
        ],
    }
