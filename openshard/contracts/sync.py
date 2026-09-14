"""Sync boundary: how a local receipt reaches a hosted store, and what comes back.

The envelope carries the Receipt Contract v2 projection plus a bounded,
privacy-safe view of the run entry (the same ``receipt_to_dict`` projection
the MCP server exposes). It never carries blocked fields, raw prompts,
diffs, transcripts, absolute paths or credentials; the transport adds the
bearer token at send time from the environment.

``openshard.sync`` provides the HTTPS transport and CLI; this module is the
shape they agree on with OpenShard Cloud.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from openshard.contracts._common import now_iso

SYNC_ENVELOPE_VERSION = "1"


@dataclass
class SyncEnvelope:
    envelope_version: str
    shard_id: str
    run_id: str | None
    attempt_number: int | None
    repo_identity: str | None
    content_hash: str | None
    receipt_contract: dict
    receipt: dict
    openshard_version: str
    sent_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "envelope_version": self.envelope_version,
            "shard_id": self.shard_id,
            "run_id": self.run_id,
            "attempt_number": self.attempt_number,
            "repo_identity": self.repo_identity,
            "content_hash": self.content_hash,
            "receipt_contract": self.receipt_contract,
            "receipt": self.receipt,
            "openshard_version": self.openshard_version,
            "sent_at": self.sent_at,
        }


@dataclass
class SyncResult:
    accepted: bool
    status: str  # "created" | "updated" | "unchanged" | "rejected" | "unreachable" | "unauthorized"
    remote_id: str | None = None
    error_category: str | None = None  # never raw error text
    detail: str | None = None


class SyncTransport(Protocol):
    def push(self, envelope: SyncEnvelope) -> SyncResult: ...


class RecordingSyncTransport:
    """Tests: remembers every envelope; ``fail`` makes pushes fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.envelopes: list[dict] = []
        self.fail = fail

    def push(self, envelope: SyncEnvelope) -> SyncResult:
        if self.fail:
            return SyncResult(accepted=False, status="unreachable", error_category="transport")
        self.envelopes.append(envelope.to_dict())
        return SyncResult(accepted=True, status="created", remote_id=f"rec_{len(self.envelopes)}")


def build_sync_envelope(entry: dict, *, siblings: list[dict] | None = None, index: int | None = None) -> SyncEnvelope:
    """Project a persisted run entry into a sync envelope. Never raises on odd input.

    Uses the two existing privacy boundaries (``receipt_to_dict`` and
    ``ReceiptContract.to_dict``) rather than sending the entry itself.
    """
    from openshard import __version__
    from openshard.history.receipt_contract import build_receipt_contract
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.shard_hash import stored_shard_hash
    from openshard.history.shard_schema import coerce_shard_entry
    from openshard.history.views import receipt_to_dict

    contract = build_receipt_contract(entry, index=index, siblings=siblings)
    receipt = build_shard_receipt(coerce_shard_entry(entry), index)
    return SyncEnvelope(
        envelope_version=SYNC_ENVELOPE_VERSION,
        shard_id=contract.shard_id,
        run_id=contract.run_id,
        attempt_number=contract.attempts.attempt_number,
        repo_identity=contract.repository.get("repo_identity") if isinstance(contract.repository, dict) else None,
        content_hash=stored_shard_hash(entry) if isinstance(entry, dict) else None,
        receipt_contract=contract.to_dict(),
        receipt=receipt_to_dict(receipt, extended=True),
        openshard_version=str(__version__),
    )
