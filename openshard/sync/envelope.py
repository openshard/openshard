"""Canonical record -> receipt sync envelope v1, and when a record may go.

The ``receipt`` object is ``history.views.receipt_to_dict(receipt,
extended=True)`` built from the stored record -- the projection
``openshard history --json`` prints -- including the structured
``verification`` block and ``task_title``. Nothing is added, removed,
renamed or filled in here: a key the Platform contract does not define is
the Platform's to reject, and that rejection is recorded locally rather than
papered over.

Quiescence
----------
A hook-captured session is upserted into ``runs.jsonl`` at every ``Stop``,
so its record changes for as long as the agent session is open, while the
Platform keeps the first copy it accepted and reports different content
under the same ``receipt_id`` as a conflict. A hook record is therefore
eligible only once the session ended (``capture.session_end_observed``) or
has been idle for :data:`QUIESCENT_SECONDS` (the same hour after which the
capture path itself sweeps a stale session). Records written once (imports,
``wrap``, native runs) are eligible immediately.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from openshard.history.receipt_identity import stored_receipt_id
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_schema import SHARD_SCHEMA_VERSION
from openshard.history.views import receipt_to_dict

CONTRACT = "openshard.receipt-sync"
CONTRACT_VERSION = "1"
SOURCE_PRODUCT = "openshard-core"

QUIESCENT_SECONDS = 60 * 60

REASON_NO_RECEIPT_ID = "no_receipt_id"
REASON_SESSION_IN_PROGRESS = "session_in_progress"
REASON_SESSION_ENDED = "session_ended"
REASON_SESSION_QUIESCENT = "session_quiescent"
REASON_RECORD_COMPLETE = "record_complete"


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str


def _seconds_since(stamp: object, now: datetime) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds()


def eligibility(entry: dict, *, now: datetime | None = None) -> Eligibility:
    """Whether *entry* may be synced right now, and why (not). Never raises."""
    if stored_receipt_id(entry) is None:
        return Eligibility(False, REASON_NO_RECEIPT_ID)
    capture = entry.get("capture")
    if not isinstance(capture, dict) or "session_end_observed" not in capture:
        return Eligibility(True, REASON_RECORD_COMPLETE)
    if capture.get("session_end_observed") is True or capture.get("status") == "ended":
        return Eligibility(True, REASON_SESSION_ENDED)
    completeness = capture.get("completeness")
    reasons = completeness.get("reasons", []) if isinstance(completeness, dict) else []
    if any(isinstance(r, dict) and r.get("kind") == "session_end_not_observed" for r in reasons):
        return Eligibility(True, REASON_SESSION_QUIESCENT)
    current = now if now is not None else datetime.now(UTC)
    age = _seconds_since(capture.get("last_activity_at"), current)
    if age is None or age >= QUIESCENT_SECONDS:
        return Eligibility(True, REASON_SESSION_QUIESCENT)
    return Eligibility(False, REASON_SESSION_IN_PROGRESS)


def receipt_payload(entry: dict, index: int, *, multi_agent: bool = False) -> dict:
    """The privacy-bounded machine receipt for the record at history position *index*.

    ``multi_agent`` keeps the multi-agent block (Platform contract
    ``multi_agent``); without it the key is removed entirely, so a Platform
    that does not know the block, or an organisation that has not enabled
    ``advanced_osn``, receives exactly the receipt it did before.
    """
    payload = receipt_to_dict(build_shard_receipt(entry, index=index), extended=True)
    if not multi_agent:
        payload.pop("multi_agent", None)
    return payload


def build_envelope(entry: dict, index: int, *, core_version: str, multi_agent: bool = False) -> dict:
    """The v1 sync envelope for *entry*. ``source.receipt_schema_version`` is the
    record's own stamped version (``"unknown"`` for pre-stamping records, as
    Core itself labels them), never today's."""
    schema_version = entry.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        schema_version = None
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "source": {
            "product": SOURCE_PRODUCT,
            "version": core_version,
            "receipt_schema_version": schema_version,
        },
        "receipt": receipt_payload(entry, index, multi_agent=multi_agent),
    }


def payload_hash(receipt: dict) -> str:
    """``sha256:<hex>`` of the canonical JSON (sorted keys, compact) of *receipt*.

    Local bookkeeping only: it says whether the receipt this machine would
    send today is the one it sent before. The Platform computes its own
    hash over what it received; the two are not compared.
    """
    blob = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "QUIESCENT_SECONDS",
    "SHARD_SCHEMA_VERSION",
    "SOURCE_PRODUCT",
    "Eligibility",
    "build_envelope",
    "eligibility",
    "payload_hash",
    "receipt_payload",
]
