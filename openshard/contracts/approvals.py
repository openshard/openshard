"""Approval boundary: a policy said ASK; who answers, how, and when.

An ``ApprovalProvider`` receives an ``ApprovalRequest`` and returns an
``ApprovalOutcome`` that names the approver as a separate principal (never
the requester or the executor by default), the mechanism, and the time.
The outcome maps onto the receipt's ``approval`` block and the
``actors.approved_by`` principal.

Today's interactive CLI confirmation (``click.confirm`` in the run pipeline)
is the ``cli_prompt`` mechanism; hosted approvals in the dashboard are the
``dashboard`` mechanism and live in OpenShard Cloud.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from openshard.contracts._common import now_iso

APPROVAL_STATUSES: frozenset[str] = frozenset({"pending", "granted", "denied"})


@dataclass
class ApprovalRequest:
    shard_id: str
    action: str
    reason: str
    request_id: str = field(default_factory=lambda: f"apr-{uuid.uuid4().hex[:12]}")
    run_id: str | None = None
    risk_level: str | None = None
    requested_by: dict | None = None  # Principal dict
    policy_id: str | None = None
    prompt: str | None = None  # short, sanitized, path-free
    requested_at: str = field(default_factory=now_iso)


@dataclass
class ApprovalOutcome:
    request_id: str
    status: str
    approver: dict | None = None  # Principal dict; None while pending or for auto policy
    mechanism: str = "unknown"
    decided_at: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in APPROVAL_STATUSES:
            self.status = "pending"

    def to_receipt_block(self, *, action: str | None = None) -> dict:
        """The receipt's ``approval`` block."""
        return {
            "required": True,
            "status": self.status,
            "approver": self.approver,
            "approved_at": self.decided_at if self.status == "granted" else None,
            "mechanism": self.mechanism,
            "reason": self.reason,
            "request_action": action,
            "request_id": self.request_id,
        }


class ApprovalProvider(Protocol):
    def request(self, request: ApprovalRequest) -> ApprovalOutcome: ...

    def check(self, request_id: str) -> ApprovalOutcome: ...


class RecordingApprovalProvider:
    """In-memory provider for tests and local dry runs. Decisions are scripted
    up front; anything unscripted stays pending."""

    def __init__(self, *, decisions: dict[str, tuple[str, dict | None]] | None = None,
                 mechanism: str = "api") -> None:
        self._scripted = dict(decisions or {})
        self._mechanism = mechanism
        self.requests: list[ApprovalRequest] = []
        self._outcomes: dict[str, ApprovalOutcome] = {}

    def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        scripted = self._scripted.get(request.request_id) or self._scripted.get(request.shard_id)
        if scripted is None:
            outcome = ApprovalOutcome(request_id=request.request_id, status="pending")
        else:
            status, approver = scripted
            outcome = ApprovalOutcome(
                request_id=request.request_id, status=status, approver=approver,
                mechanism=self._mechanism, decided_at=now_iso(),
            )
        self._outcomes[request.request_id] = outcome
        return outcome

    def check(self, request_id: str) -> ApprovalOutcome:
        return self._outcomes.get(request_id) or ApprovalOutcome(request_id=request_id, status="pending")

    def decide(self, request_id: str, status: str, approver: dict | None, reason: str | None = None) -> ApprovalOutcome:
        outcome = ApprovalOutcome(
            request_id=request_id, status=status, approver=approver,
            mechanism=self._mechanism, decided_at=now_iso(), reason=reason,
        )
        self._outcomes[request_id] = outcome
        return outcome
