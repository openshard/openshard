"""Deduplication: ``import_key`` + ``source_sha256`` + live-capture collision (§4).

| Situation                                   | Decision                                 |
|---------------------------------------------|------------------------------------------|
| same key, same source hash                  | skip (duplicate)                         |
| same key, different hash (the session grew) | new receipt superseding the old one, or  |
|                                             | skip when ``no_update``                  |
| the session was captured live by hooks      | append an attachment to the live receipt |
| nothing stored                              | write a new receipt                      |

A sealed receipt is never replaced: superseding is a *new* receipt whose
``import.supersedes`` names the old one, which stays as written.
"""

from __future__ import annotations

from dataclasses import dataclass

from openshard.ingest.store import LiveRecord, StoreSnapshot

WRITE = "write"
SUPERSEDE = "supersede"
ATTACH_LIVE = "attach_live"
SKIP_DUPLICATE = "skip_duplicate"
SKIP_NO_UPDATE = "skip_no_update"


@dataclass(frozen=True)
class Decision:
    action: str
    supersedes_receipt_id: str | None = None
    supersedes_attachment_id: str | None = None
    live: LiveRecord | None = None

    @property
    def writes(self) -> bool:
        return self.action in (WRITE, SUPERSEDE, ATTACH_LIVE)


def decide(
    snap: StoreSnapshot,
    *,
    import_key: str,
    source_sha256: str,
    live_executor: str | None,
    native_session_id: str,
    no_update: bool = False,
) -> Decision:
    existing = snap.imports.get(import_key)
    if existing is not None and existing.source_sha256 == source_sha256:
        return Decision(SKIP_DUPLICATE)
    live = snap.live.get((live_executor, native_session_id)) if live_executor else None
    if live is not None:
        if existing is not None and no_update:
            return Decision(SKIP_NO_UPDATE)
        return Decision(ATTACH_LIVE, live=live,
                        supersedes_attachment_id=existing.attachment_id if existing else None)
    if existing is not None:
        if no_update:
            return Decision(SKIP_NO_UPDATE)
        return Decision(SUPERSEDE, supersedes_receipt_id=existing.receipt_id,
                        supersedes_attachment_id=existing.attachment_id)
    return Decision(WRITE)
