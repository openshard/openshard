"""Whether this machine's Receipts reach the Openshard Platform, and the evidence for it.

``openshard setup`` and ``openshard sync status`` say "ready" or "connected"
only on evidence: the Platform accepted a Receipt from this repository (an
entry ``synced`` in the sync outbox), or it witnessed a captured session. A
link that exists but has not delivered yet is reported as exactly that, and
no link at all means Receipts stay on this machine. Two kinds of link exist:

* ``api_key``: ``openshard sync connect`` / ``login`` (or the
  ``OPENSHARD_PLATFORM_*`` variables).
* ``connected``: connected capture (``OPENSHARD_CONNECTED_*``, often with a
  proxy-injected credential in cloud sandboxes). Events the Platform
  acknowledged are said, but they are not a delivered Receipt.

Read-only and never raises.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

KIND_API_KEY = "api_key"
KIND_CONNECTED = "connected"
KIND_NONE = "none"
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_NOT_CONFIRMED = "not_confirmed"
DELIVERY_NONE = "none"


@dataclass(frozen=True)
class PlatformLinkStatus:
    kind: str  # api_key | connected | none
    delivery: str  # confirmed | not_confirmed | none
    text: str  # one human line, no secrets

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _synced(root: Path | None, endpoint: str, organisation_id: str | None = None) -> int:
    """Receipts from *root* the Platform at *endpoint* accepted (for one organisation when named).

    Connected capture delivers through a capture attachment too, recorded under
    ``remote-capture:<id>`` instead of an organisation; that counts for it.
    """
    if root is None:
        return 0
    from openshard.sync import outbox

    def counts(record: dict) -> bool:
        org = str(record.get("organisation_id") or "")
        if organisation_id is None:
            return record.get("endpoint") == endpoint or org.startswith("remote-capture:")
        return outbox.matches_link(record, endpoint=endpoint, organisation_id=organisation_id)

    return sum(1 for r in outbox.load_outbox(root).values() if r.get("state") == outbox.STATE_SYNCED and counts(r))


def _receipts(n: int) -> str:
    return f"{n} Receipt{'' if n == 1 else 's'} delivered"


def _connected_status(root: Path | None, env: dict | os._Environ | None) -> PlatformLinkStatus | None:
    from openshard.connected.config import resolve_connection
    from openshard.remote import spool
    from openshard.remote.collector import witness_status

    connection = resolve_connection(env)
    if connection is None:
        return None
    via = "proxy credential" if connection.proxy_backed else "stored credential"
    witnessed, acked = False, 0
    for session_env in spool.connected_envs(env):
        state = spool.read_state(session_env) or {}
        witnessed = witnessed or witness_status(state).get("state") == "acknowledged"
        acked += int(state.get("acked_seq") or 0)
    synced = _synced(root, connection.endpoint)
    if synced or witnessed:
        evidence = _receipts(synced) if synced else "the Platform witnessed a captured session"
        return PlatformLinkStatus(KIND_CONNECTED, DELIVERY_CONFIRMED, f"connected capture ({via}); {evidence}")
    pending = f"{acked} events acknowledged, no Receipt delivered yet" if acked else "nothing delivered yet"
    return PlatformLinkStatus(KIND_CONNECTED, DELIVERY_NOT_CONFIRMED, f"connected capture ({via}); {pending}")


def _api_key_status(root: Path | None, env: dict | os._Environ | None) -> PlatformLinkStatus | None:
    from openshard.sync.config import resolve_link

    link = resolve_link(env)
    if link is None:
        return None
    synced = _synced(root, link.endpoint, link.organisation_id)
    if synced:
        return PlatformLinkStatus(KIND_API_KEY, DELIVERY_CONFIRMED, f"API key link; {_receipts(synced)}")
    return PlatformLinkStatus(KIND_API_KEY, DELIVERY_NOT_CONFIRMED, "API key link; nothing delivered yet")


def platform_link(root: Path | None = None, env: dict | os._Environ | None = None) -> PlatformLinkStatus:
    """This machine's link to the Platform for *root*'s Receipts, with its delivery evidence. Never raises."""
    links: list[PlatformLinkStatus] = []
    for kind, read in ((KIND_API_KEY, _api_key_status), (KIND_CONNECTED, _connected_status)):
        try:
            found = read(root, env)
        except Exception:  # unknown stays unknown: never "not linked", never "confirmed"
            found = PlatformLinkStatus(kind, DELIVERY_NOT_CONFIRMED, "the link could not be checked")
        if found is not None:
            links.append(found)
    confirmed = [link for link in links if link.delivery == DELIVERY_CONFIRMED]
    return (confirmed or links or [PlatformLinkStatus(KIND_NONE, DELIVERY_NONE,
                                                      "not linked; Receipts stay on this machine")])[0]
