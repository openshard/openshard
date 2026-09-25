"""RoutingOutcome: what actually happened after a routing decision.

Outcomes are derived at read time from a Receipt (``runs.jsonl`` record); they
are never written back into it. That keeps sealed Receipts untouched, lets
every existing v0.4.7 Receipt produce an outcome without migration, and lets
later evidence (an ``openshard verify`` attestation, a human correction) join
without rewriting history.

Only observed values are populated. In particular:

* ``verified_success`` is ``True``/``False`` only when a pass/fail was
  observed by OpenShard or an independent system; an agent's own claim, an
  unknown status or no checks leave it ``None``. The raw status and source
  stay on the outcome so a consumer can apply a different threshold.
* ``attempts`` is exact only when no retry happened; Receipts record that a
  retry occurred, not how many models the escalation loop tried.
* ``cost_usd`` sums recorded estimates; if a retry happened but its cost was
  not recorded, the total is unknown rather than understated.
* ``human_correction`` is ``None``: Receipts do not record corrections yet.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from openshard.history.verification import (
    SOURCE_AGENT_REPORTED,
    STATUS_FAILED,
    STATUS_PASSED,
    derive_verification,
)

ROUTING_OUTCOME_VERSION = 1


@dataclass(frozen=True)
class RoutingOutcome:
    receipt_id: str | None
    decision_fingerprint: str | None
    policy_name: str | None
    routed_model: str | None
    final_model: str | None
    routing_class: str | None
    harness: str | None
    verification_status: str
    verification_source: str | None
    verification_observation_mode: str
    verified_success: bool | None
    retry_observed: bool | None
    escalation_model: str | None
    attempts: int | None
    latency_seconds: float | None
    cost_usd: float | None
    human_correction: bool | None = None
    shadow_agreed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"version": ROUTING_OUTCOME_VERSION, **self.__dict__}


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None  # NaN/Inf are not observations


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def verified_success_for(status: str, source: str | None) -> bool | None:
    """Tri-state success from observed verification only."""
    if source is None or source == SOURCE_AGENT_REPORTED:
        return None
    if status == STATUS_PASSED:
        return True
    if status == STATUS_FAILED:
        return False
    return None


def outcome_from_receipt(entry: object) -> RoutingOutcome:
    """Derive the outcome for one Receipt. Never raises; never mutates *entry*."""
    if not isinstance(entry, dict):
        entry = {}
    ev = derive_verification(entry)
    prov = _dict(entry.get("routing_provenance"))
    fps = _dict(prov.get("fingerprints"))
    policy = _dict(prov.get("policy"))

    retry = entry.get("retry_triggered")
    retry_observed = retry if isinstance(retry, bool) else None
    attempts = 1 if retry_observed is False else None

    cost = _float(entry.get("estimated_cost"))
    if retry_observed:
        retry_cost = _float(entry.get("retry_estimated_cost"))
        cost = cost + retry_cost if cost is not None and retry_cost is not None else None

    final_model = entry.get("execution_model")
    routed = prov.get("selected_model")
    agreed = prov.get("agrees_with_execution")
    context = _dict(prov.get("context"))
    return RoutingOutcome(
        receipt_id=entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None,
        decision_fingerprint=fps.get("decision") if isinstance(fps.get("decision"), str) else None,
        policy_name=policy.get("name") if isinstance(policy.get("name"), str) else None,
        routed_model=routed if isinstance(routed, str) else None,
        final_model=final_model if isinstance(final_model, str) else None,
        routing_class=(
            prov.get("resolved_class") if isinstance(prov.get("resolved_class"), str) else None
        ),
        harness=(
            context.get("harness") if isinstance(context.get("harness"), str)
            else entry.get("executor") if isinstance(entry.get("executor"), str) else None
        ),
        verification_status=ev.status if ev.recorded else "unknown",
        verification_source=ev.source,
        verification_observation_mode=ev.observation_mode,
        verified_success=verified_success_for(ev.status, ev.source) if ev.recorded else None,
        retry_observed=retry_observed,
        escalation_model=(
            entry.get("fixer_model")
            if retry_observed and isinstance(entry.get("fixer_model"), str)
            else None
        ),
        attempts=attempts,
        latency_seconds=_float(entry.get("duration_seconds")),
        cost_usd=cost,
        shadow_agreed=agreed if isinstance(agreed, bool) else None,
    )
