"""Policy boundary: ALLOW / DENY / ASK with a reason, from an identified policy.

``PolicyDecision`` (``openshard.policy.decision``) already models a single
decision with a reason and source. This boundary adds what v0.5 needs
around it: the **context** a policy is evaluated against (who, what, where,
how risky) and a **verdict** that names the governing policy and version so
the receipt can say which policy governed the run, not just what was decided.

Runtime gates (``openshard.execution.gates``) keep working unchanged; a
future evaluator wraps them or replaces them behind this Protocol.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from openshard.contracts._common import now_iso
from openshard.policy.decision import PolicyDecision, make_allow, resolve_policy_decisions

DECISIONS: frozenset[str] = frozenset({"allow", "ask", "deny", "not_applicable"})


@dataclass
class PolicyContext:
    """Everything a policy may look at. All fields optional; None means unknown."""

    action: str  # e.g. "write", "shell", "network", "run"
    resource: str | None = None  # repo-relative path, command summary, ...
    owner: dict | None = None  # Principal dicts (see receipt_contract.Principal)
    requested_by: dict | None = None
    executed_by: dict | None = None
    repo_identity: str | None = None
    branch: str | None = None
    risk_level: str | None = None  # low | medium | high | critical
    permissions_requested: list[str] = field(default_factory=list)
    estimated_cost_usd: float | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class PolicyVerdict:
    policy_id: str
    policy_version: str
    name: str
    decision: str
    reason: str
    source: str = "policy"
    decisions: list[PolicyDecision] = field(default_factory=list)
    evaluated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            self.decision = "deny"
            self.reason = f"invalid decision replaced by deny: {self.reason}"[:200]

    @property
    def requires_approval(self) -> bool:
        return self.decision == "ask"

    @property
    def blocked(self) -> bool:
        return self.decision == "deny"

    def to_receipt_block(self) -> dict:
        """The receipt's ``policy`` block."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "name": self.name,
            "source": self.source,
            "decision": self.decision,
            "reason": self.reason,
            "evaluated_at": self.evaluated_at,
        }

    def to_policy_decisions(self) -> list[dict]:
        """The receipt's existing ``policy_decisions`` list (v0.4 shape)."""
        return [asdict(d) for d in self.decisions]

    @classmethod
    def from_decisions(
        cls, decisions: list[PolicyDecision], *, policy_id: str, policy_version: str, name: str,
        source: str = "policy",
    ) -> PolicyVerdict:
        resolved = resolve_policy_decisions(decisions)
        return cls(
            policy_id=policy_id, policy_version=policy_version, name=name,
            decision=resolved.decision,
            reason=resolved.reason or "no reason recorded",
            source=source, decisions=list(decisions),
        )


class PolicyEvaluator(Protocol):
    def evaluate(self, context: PolicyContext) -> PolicyVerdict: ...


class AllowAllPolicy:
    """Explicitly permissive default. Its reason says that no policy is configured,
    so a receipt never presents "allowed" as if a real policy had been consulted."""

    POLICY_ID = "builtin:allow-all"
    VERSION = "1"

    def evaluate(self, context: PolicyContext) -> PolicyVerdict:
        decision = make_allow(
            context.action, resource=context.resource,
            reason="no policy configured; default allow", source="policy",
        )
        return PolicyVerdict.from_decisions(
            [decision], policy_id=self.POLICY_ID, policy_version=self.VERSION, name="allow-all",
        )
