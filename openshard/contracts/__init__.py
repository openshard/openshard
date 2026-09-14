"""Stable boundaries for the v0.5 platform pieces that do not exist yet.

Each module here defines a small request/result dataclass pair and a
``Protocol`` that a future implementation (local or hosted) must satisfy.
Nothing in this package performs I/O, calls a provider, or changes runtime
behaviour: the local runtime works with none of these configured.

The only implementations shipped are deliberately trivial (``Null*``,
``Recording*``, ``AllowAll*``) and exist so callers and tests can be written
against the boundary today. See ``docs/architecture/V050_CONTRACTS.md``.

Every result type has a ``to_receipt_block()`` that yields the matching
optional block of Receipt Contract v2 (``openshard.history.receipt_contract``)
so an implementation's output lands on the receipt without translation.
"""

from openshard.contracts.approvals import (
    ApprovalOutcome,
    ApprovalProvider,
    ApprovalRequest,
    RecordingApprovalProvider,
)
from openshard.contracts.compute import (
    ComputeRunSpec,
    ComputeRunStatus,
    ComputeUnavailableError,
    ManagedComputeProvider,
    UnavailableComputeProvider,
)
from openshard.contracts.outcomes import (
    OutcomeReport,
    OutcomeReporter,
    RecordingOutcomeReporter,
)
from openshard.contracts.policy import (
    AllowAllPolicy,
    PolicyContext,
    PolicyEvaluator,
    PolicyVerdict,
)
from openshard.contracts.sync import (
    RecordingSyncTransport,
    SyncEnvelope,
    SyncResult,
    SyncTransport,
    build_sync_envelope,
)
from openshard.contracts.verification import (
    NullVerifier,
    VerificationOutcome,
    VerificationRequest,
    Verifier,
    VerifierIdentity,
)

__all__ = [
    "AllowAllPolicy",
    "ApprovalOutcome",
    "ApprovalProvider",
    "ApprovalRequest",
    "ComputeRunSpec",
    "ComputeRunStatus",
    "ComputeUnavailableError",
    "ManagedComputeProvider",
    "NullVerifier",
    "OutcomeReport",
    "OutcomeReporter",
    "PolicyContext",
    "PolicyEvaluator",
    "PolicyVerdict",
    "RecordingApprovalProvider",
    "RecordingOutcomeReporter",
    "RecordingSyncTransport",
    "SyncEnvelope",
    "SyncResult",
    "SyncTransport",
    "UnavailableComputeProvider",
    "VerificationOutcome",
    "VerificationRequest",
    "Verifier",
    "VerifierIdentity",
    "build_sync_envelope",
]
