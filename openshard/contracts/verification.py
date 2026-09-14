"""Verification boundary: something other than the executing agent checks a run.

A ``Verifier`` takes a ``VerificationRequest`` (which run, which checks,
where) and returns a ``VerificationOutcome`` whose checks carry the verifier's
identity and whether it is independent of the executor. The outcome maps
directly onto the receipt's ``verifiers`` block.

The existing local runners (``openshard.verification.executor`` and the OSN
verification loop) are the first candidates to be wrapped by this Protocol;
they are not rewritten here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from openshard.contracts._common import now_iso

VERIFICATION_STATUSES: frozenset[str] = frozenset({"passed", "failed", "skipped", "not_run", "unknown"})


@dataclass(frozen=True)
class VerifierIdentity:
    verifier_id: str
    verifier_kind: str  # one of receipt_contract.VERIFIER_KINDS
    independent: bool
    display: str | None = None


@dataclass
class VerificationRequest:
    shard_id: str
    run_id: str | None
    repo_path: str | None
    checks: list[str] = field(default_factory=list)  # path-free check labels or argv summaries
    timeout_seconds: float = 120.0
    requested_by: str | None = None


@dataclass
class VerificationCheckResult:
    check: str
    status: str
    duration_seconds: float | None = None
    cost_usd: float | None = None
    summary: str | None = None


@dataclass
class VerificationOutcome:
    verifier: VerifierIdentity
    status: str
    checks: list[VerificationCheckResult] = field(default_factory=list)
    reason: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    raw_output_stored: bool = False

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            self.status = "unknown"
        self.raw_output_stored = False

    def to_receipt_block(self) -> list[dict]:
        """Entries for the receipt's ``verifiers`` block."""
        return [
            {
                "check": c.check,
                "status": c.status if c.status in VERIFICATION_STATUSES else "unknown",
                "verifier_kind": self.verifier.verifier_kind,
                "verifier_id": self.verifier.verifier_id,
                "independent": self.verifier.independent,
                "duration_seconds": c.duration_seconds,
                "cost_usd": c.cost_usd,
                "summary": c.summary,
            }
            for c in self.checks
        ]


class Verifier(Protocol):
    """A verification backend. Implementations must never store raw output."""

    def identity(self) -> VerifierIdentity: ...

    def verify(self, request: VerificationRequest) -> VerificationOutcome: ...


class NullVerifier:
    """Runs nothing and says so. Used when no verifier is configured."""

    def identity(self) -> VerifierIdentity:
        return VerifierIdentity(verifier_id="null", verifier_kind="unknown", independent=False)

    def verify(self, request: VerificationRequest) -> VerificationOutcome:
        return VerificationOutcome(
            verifier=self.identity(),
            status="not_run",
            reason="no verifier configured",
            finished_at=now_iso(),
        )
