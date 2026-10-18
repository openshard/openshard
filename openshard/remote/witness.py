"""Who holds a Receipt's capture: the Platform's witness, or only this machine.

``capture_witness`` is computed when a Receipt is read, from this machine's
remote-capture spool (``remote/spool.py``):

* ``platform`` -- at least one of the Receipt's Events was in a batch the
  Platform accepted *and* acknowledged with a witness block. The counts say
  how many: ``events_witnessed`` of ``events_in_receipt``.
* ``self_held`` -- no such acknowledgement is recorded here: the capture is
  held only by this machine (not attached, nothing sent yet, or sent without
  an acknowledgement). No counts are given.

It is never stored in the Receipt: an acknowledgement can arrive after the
Receipt was sealed, and it describes delivery, not the work. Being witnessed
is not being verified (docs/signing.md, "The hosted witness").
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from openshard.remote import spool

WITNESS_PLATFORM = "platform"
WITNESS_SELF_HELD = "self_held"


def _receipt_event_ids(entry: dict) -> list[str]:
    events = entry.get("events")
    ids = [e.get("event_id") for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    return list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))


def _scopes_naming(receipt_ids: set[str], env: dict | os._Environ | None) -> Iterable[dict | os._Environ | None]:
    """The spool scopes (manual capture root, connected sessions) whose links name one of *receipt_ids*."""
    candidates: list[dict | os._Environ | None] = [spool.session_env(env, "legacy"), *spool.connected_envs(env)]
    for scope in candidates:
        state = spool.read_state(scope) or {}
        links = state.get("links")
        if isinstance(links, list) and any(
            isinstance(link, dict) and link.get("receipt_id") in receipt_ids for link in links
        ):
            yield scope


def witness_for_entries(entries: list[dict], env: dict | os._Environ | None = None) -> dict[str, dict[str, Any]]:
    """``{receipt_id: capture_witness block}`` for every entry with a receipt id. Never raises."""
    wanted = {e["receipt_id"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("receipt_id"), str)}
    witnessed: set[str] = set()
    try:
        for scope in _scopes_naming(set(wanted), env):
            witnessed |= spool.witnessed_ids(scope)
    except Exception:  # the spool is bookkeeping; unreadable means nothing is claimed
        witnessed = set()
    out: dict[str, dict[str, Any]] = {}
    for receipt_id, entry in wanted.items():
        ids = _receipt_event_ids(entry)
        seen = sum(1 for i in ids if i in witnessed)
        if seen:
            out[receipt_id] = {"capture_witness": WITNESS_PLATFORM, "events_witnessed": seen,
                               "events_in_receipt": len(ids)}
        else:
            out[receipt_id] = {"capture_witness": WITNESS_SELF_HELD, "events_witnessed": None,
                               "events_in_receipt": None}
    return out


def witness_for_entry(entry: dict, env: dict | os._Environ | None = None) -> dict[str, Any]:
    """The ``capture_witness`` block for one Receipt (``self_held`` when it has no receipt id)."""
    rid = entry.get("receipt_id") if isinstance(entry, dict) else None
    found = witness_for_entries([entry], env).get(rid) if isinstance(rid, str) else None
    return found or {"capture_witness": WITNESS_SELF_HELD, "events_witnessed": None, "events_in_receipt": None}


def witness_line(block: dict[str, Any]) -> str:
    """``platform · 41 of 43 events acknowledged`` / ``self-held (no Platform acknowledgement)``."""
    if block.get("capture_witness") == WITNESS_PLATFORM:
        return f"platform · {block['events_witnessed']} of {block['events_in_receipt']} events acknowledged"
    return "self-held (no Platform acknowledgement)"


def attach_capture_witness(rows: list[dict], entries: list[dict], env: dict | os._Environ | None = None) -> None:
    """Add ``capture_witness`` to each ``history --json`` row that names a Receipt."""
    blocks = witness_for_entries(entries, env)
    for row in rows:
        row["capture_witness"] = blocks.get(row.get("receipt_id") or "")


__all__ = [
    "WITNESS_PLATFORM",
    "WITNESS_SELF_HELD",
    "attach_capture_witness",
    "witness_for_entries",
    "witness_for_entry",
    "witness_line",
]
