"""RoutingDecision: what was chosen, from what, why, and by which policy.

A decision answers, from its own fields: which candidates were considered,
what was selected, why (stable reason tokens), which policy and version made
it, which routing class was requested and which resolved, and whether the
model was explicit, pinned or routed. ``score`` and ``confidence`` exist for
policies that produce real ones; the deterministic baseline produces neither
and leaves them ``None``.

``to_provenance`` is the Receipt projection (``routing_provenance``): bounded,
JSON-safe, model ids but no policy lists (rejections are counted, matching the
``model_policy_summary`` rule), and no task text.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from openshard.routing.adaptive.context import RoutingContext
from openshard.routing.adaptive.recovery import RecoveryPlan

ROUTING_PROVENANCE_VERSION = 1

# selection_mode
MODE_EXPLICIT = "explicit"  # the caller named the model
MODE_PINNED = "pinned"  # a routing-class pin from config
MODE_ROUTED = "routed"  # chosen by the routing policy
MODE_NONE = "none"  # nothing eligible; no model selected

# Provenance recording modes: how the decision relates to what executed.
RECORD_SHADOW = "shadow"  # computed alongside legacy routing; not executed
RECORD_APPLIED = "applied"  # the decision chose the executed model

MAX_CONSIDERED = 8
MAX_PROMOTION = 3


@dataclass(frozen=True)
class RoutingDecision:
    selected_model: str | None
    selection_mode: str
    requested_class: str | None
    resolved_class: str | None
    policy_name: str
    policy_version: str
    reasons: tuple[str, ...] = ()
    # Ranked ids the resolved class chose between (selected first).
    considered: tuple[str, ...] = ()
    selected_via: tuple[str, ...] = ()
    eligible_count: int = 0
    rejected_counts: dict[str, int] = field(default_factory=dict)
    # Classes tried and found empty before ``resolved_class``.
    class_fallbacks: tuple[str, ...] = ()
    rejected_pin: str | None = None
    rejected_pin_reason: str | None = None
    rejected_explicit_reason: str | None = None
    promotion_candidates: tuple[str, ...] = ()
    # Only for policies that compute them. Never filled with placeholders.
    score: float | None = None
    confidence: float | None = None
    recovery: RecoveryPlan = field(default_factory=RecoveryPlan)
    context: RoutingContext = field(default_factory=RoutingContext)
    catalog_fingerprint: str = ""
    candidate_set_version: str = ""

    @property
    def decision_fingerprint(self) -> str:
        """Hash of inputs and output: equal fingerprints = reproduced decision."""
        blob = json.dumps(
            {
                "context": self.context.fingerprint,
                "catalog": self.catalog_fingerprint,
                "policy": [self.policy_name, self.policy_version],
                "selected": self.selected_model,
                "mode": self.selection_mode,
                "class": self.resolved_class,
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def to_provenance(
        self, *, record_mode: str = RECORD_SHADOW, executed_model: str | None = None
    ) -> dict[str, Any]:
        agrees = None
        if executed_model and self.selected_model:
            agrees = executed_model == self.selected_model
        return {
            "version": ROUTING_PROVENANCE_VERSION,
            "record_mode": record_mode,
            "policy": {"name": self.policy_name, "version": self.policy_version},
            "selected_model": self.selected_model,
            "selection_mode": self.selection_mode,
            "selected_via": list(self.selected_via),
            "executed_model": executed_model,
            "agrees_with_execution": agrees,
            "requested_class": self.requested_class,
            "resolved_class": self.resolved_class,
            "class_fallbacks": list(self.class_fallbacks),
            "reasons": list(self.reasons),
            "considered": list(self.considered[:MAX_CONSIDERED]),
            "eligible_count": self.eligible_count,
            "rejected_counts": dict(self.rejected_counts),
            "rejected_pin": self.rejected_pin,
            "rejected_pin_reason": self.rejected_pin_reason,
            "rejected_explicit_reason": self.rejected_explicit_reason,
            "promotion_candidates": list(self.promotion_candidates[:MAX_PROMOTION]),
            "score": self.score,
            "confidence": self.confidence,
            "recovery": self.recovery.to_dict(),
            "context": self.context.to_dict(),
            "fingerprints": {
                "context": self.context.fingerprint,
                "catalog": self.catalog_fingerprint,
                "decision": self.decision_fingerprint,
            },
            "candidate_set_version": self.candidate_set_version,
        }
