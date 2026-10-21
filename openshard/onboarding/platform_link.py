"""Whether this machine's Receipts reach the Openshard Platform, and the evidence for it.

``openshard setup`` and ``openshard sync status`` say "ready" or "connected"
only on evidence: the Platform acknowledged what was sent. A link that exists
but has not been confirmed yet is reported as exactly that, and no link at all
means Receipts stay on this machine. Two kinds of link exist:

* ``api_key``: ``openshard sync connect`` / ``login`` (or the
  ``OPENSHARD_PLATFORM_*`` variables); confirmed once a Receipt was synced.
* ``connected``: connected capture (``OPENSHARD_CONNECTED_*``, often with a
  proxy-injected credential in cloud sandboxes); confirmed once the Platform
  acknowledged captured events (``openshard remote status``).

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


def _connected_status(env: dict | os._Environ | None) -> PlatformLinkStatus | None:
    from openshard.connected.config import resolve_connection
    from openshard.remote import spool
    from openshard.remote.collector import witness_status

    connection = resolve_connection(env)
    if connection is None:
        return None
    via = "proxy credential" if connection.proxy_backed else "stored credential"
    best: dict[str, Any] | None = None
    for session_env in spool.connected_envs(env):
        witness = witness_status(spool.read_state(session_env) or {})
        if witness.get("state") == "acknowledged" and (
                best is None or str(witness.get("acknowledged_at")) > str(best.get("acknowledged_at"))):
            best = witness
    if best is not None:
        return PlatformLinkStatus(KIND_CONNECTED, DELIVERY_CONFIRMED,
                                  f"connected capture ({via}); {best['text']}")
    return PlatformLinkStatus(KIND_CONNECTED, DELIVERY_NOT_CONFIRMED,
                              f"connected capture ({via}); no delivery acknowledged yet")


def _api_key_status(root: Path | None, env: dict | os._Environ | None) -> PlatformLinkStatus | None:
    from openshard.sync.client import status
    from openshard.sync.config import resolve_link

    if resolve_link(env) is None:
        return None
    synced = int((status(root, env=env) if root is not None else {}).get("synced") or 0)
    if synced:
        noun = "Receipt" if synced == 1 else "Receipts"
        return PlatformLinkStatus(KIND_API_KEY, DELIVERY_CONFIRMED, f"API key link; {synced} {noun} synced")
    return PlatformLinkStatus(KIND_API_KEY, DELIVERY_NOT_CONFIRMED, "API key link; nothing synced yet")


def platform_link(root: Path | None = None, env: dict | os._Environ | None = None) -> PlatformLinkStatus:
    """This machine's link to the Platform for *root*'s Receipts, with its delivery evidence. Never raises."""
    try:
        links = [link for link in (_api_key_status(root, env), _connected_status(env)) if link is not None]
    except Exception:
        links = []
    confirmed = [link for link in links if link.delivery == DELIVERY_CONFIRMED]
    return (confirmed or links or [PlatformLinkStatus(KIND_NONE, DELIVERY_NONE,
                                                      "not linked; Receipts stay on this machine")])[0]
