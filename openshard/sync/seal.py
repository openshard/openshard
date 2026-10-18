"""The install seal that travels with a synced receipt (seal v2, docs/receipt-seal-v2.md).

Sync never reads the seal sidecar or the salts file. It asks the history
layer for one seal, ``seal_for_sync``, which returns the
latest seal when it already names this exact receipt, or makes a chained
``synced`` seal that does. A Receipt that was never sealed, whose latest
change was not signed, or with signing off is sent exactly as before,
without a seal: sync never seals a record after the fact.

The public key travels with the seal so the Platform can check the
signature; it trusts the key only once it is registered to the
organisation (``register_key``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openshard.history import receipt_keys as rk
from openshard.history.locate import HISTORY_RELPATH
from openshard.history.receipt_seal_v2 import sync_digest
from openshard.history.receipt_signing import seal_for_sync

ATTACHED = "attached"
UNSIGNED = "unsigned"
DEFERRED = "deferred"  # changed since sync read it, or the sidecar was busy: send nothing this flush
_DEFER_REASONS = frozenset({"record_changed_since_discovery", "sidecar_lock_timeout"})


@dataclass(frozen=True)
class SealDecision:
    status: str
    reason: str
    seal: dict | None = None
    keyid: str | None = None


def seal_for(root: Path, receipt_id: str, record_hash: str | None, receipt: dict) -> SealDecision:
    """The ``seal`` block for *receipt*, or why there is none. Never raises."""
    if not isinstance(record_hash, str):
        return SealDecision(UNSIGNED, "no_content_hash")
    try:
        digest = sync_digest(receipt)
    except ValueError:
        return SealDecision(UNSIGNED, "receipt_not_canonical")
    outcome = seal_for_sync(Path(root) / HISTORY_RELPATH.parent, receipt_id, record_hash=record_hash,
                            sync_sha256=digest)
    if outcome.reason in _DEFER_REASONS:
        return SealDecision(DEFERRED, outcome.reason)
    envelope = outcome.envelope
    if envelope is None:
        return SealDecision(UNSIGNED, outcome.reason)
    keyids = [s.get("keyid") for s in envelope.get("signatures") or [] if isinstance(s, dict)]
    try:
        ring = rk.keyring()
    except Exception:
        ring = {}
    key = next((ring[k] for k in keyids if isinstance(k, str) and k in ring), None)
    if key is None:  # a seal whose public key is not here cannot be checked by anyone we send it to
        return SealDecision(UNSIGNED, "public_key_not_available")
    return SealDecision(ATTACHED, outcome.reason,
                        {"envelope": envelope, "public_key": rk.public_key_record(key.keyid, key.public_raw)},
                        key.keyid)


def rejected_for_seal(result: Any) -> bool:
    """True when the Platform refused the envelope because of ``seal`` itself
    (``invalid_payload`` naming ``seal`` or a path under it), as a Platform
    that predates seal v2 does. Any other rejection is about the receipt."""
    if getattr(result, "kind", None) != "rejected" or getattr(result, "code", None) != "invalid_payload":
        return False
    details = getattr(result, "details", None)
    paths = [d.get("path") for d in details if isinstance(d, dict)] if isinstance(details, list) else []
    return any(isinstance(p, str) and (p == "seal" or p.startswith("seal.")) for p in paths)


def register_key(sender: Any, seal: dict) -> str:
    """Register the seal's public key with the Platform once per flush.

    ``registered`` / ``already_registered`` when the Platform accepted it,
    ``unsupported`` when it has no route (the seal is still sent and reads
    "key not registered" there), ``failed`` otherwise. Never raises and never
    blocks sending.
    """
    register = getattr(sender, "register_signing_key", None)
    if not callable(register):
        return "unsupported"
    try:
        result = register(seal["public_key"])
    except Exception:
        return "failed"
    if result.kind == "created":
        return "registered"
    if result.kind == "duplicate":
        return "already_registered"
    return "unsupported" if result.kind == "unsupported" else "failed"


__all__ = ["ATTACHED", "DEFERRED", "UNSIGNED", "SealDecision", "register_key", "rejected_for_seal", "seal_for"]
