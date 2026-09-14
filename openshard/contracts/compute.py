"""Managed Compute boundary: OpenShard runs an agent somewhere it controls.

A ``ManagedComputeProvider`` accepts a ``ComputeRunSpec`` (task, repository
ref, agent, model, governing policy, budget) and reports ``ComputeRunStatus``
over time. A finished run must produce a receipt like any other; the
``receipt_shard_id`` links the two. No provider is implemented in the open
source runtime: ``UnavailableComputeProvider`` is the explicit "not
configured" answer, never a silent no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from openshard.contracts._common import now_iso

RUN_STATES: frozenset[str] = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "unknown"})


class ComputeUnavailableError(RuntimeError):
    """Raised when no Managed Compute provider is configured or reachable."""


@dataclass
class ComputeRunSpec:
    task: str
    repo_identity: str
    ref: str  # branch, tag or commit
    agent: str  # e.g. "openshard_native", "claude_code"
    model: str | None = None
    policy_id: str | None = None
    requested_by: dict | None = None  # Principal dict
    owner: dict | None = None
    budget_usd: float | None = None
    timeout_seconds: int = 3600
    metadata: dict = field(default_factory=dict)


@dataclass
class ComputeRunStatus:
    run_id: str
    state: str
    submitted_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    receipt_shard_id: str | None = None
    error_category: str | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        if self.state not in RUN_STATES:
            self.state = "unknown"

    @property
    def finished(self) -> bool:
        return self.state in ("succeeded", "failed", "cancelled")


class ManagedComputeProvider(Protocol):
    def submit(self, spec: ComputeRunSpec) -> ComputeRunStatus: ...

    def status(self, run_id: str) -> ComputeRunStatus: ...

    def cancel(self, run_id: str) -> ComputeRunStatus: ...


class UnavailableComputeProvider:
    """The explicit default: every call raises ``ComputeUnavailableError``."""

    name = "unavailable"

    def submit(self, spec: ComputeRunSpec) -> ComputeRunStatus:
        raise ComputeUnavailableError("Managed Compute is not configured in this OpenShard install")

    def status(self, run_id: str) -> ComputeRunStatus:
        raise ComputeUnavailableError("Managed Compute is not configured in this OpenShard install")

    def cancel(self, run_id: str) -> ComputeRunStatus:
        raise ComputeUnavailableError("Managed Compute is not configured in this OpenShard install")
