"""Global Receipt identity (v0.4.4).

``receipt_id`` is the globally unique identifier of one persisted Receipt
record. It is minted once, at record creation, by whichever writer creates
the record (the hook fold, ``wrap``, ``import``, the native pipeline) and
is never derived from the record's position in ``runs.jsonl``, from a
timestamp, or from anything another machine could also compute.

Why not ``shard_id``
--------------------
``shard_id`` (``shard-YYYYMMDD-NNNN``, see ``shard_contract._make_shard_id``)
is the *history-position* identity: the date plus the runs.jsonl line
count. It is stable and human-friendly inside one repository's history
and every existing consumer (grouping, ``get_shard``, attempt linkage,
search) keys on it, so it is kept unchanged. It is not safe across
repositories, machines, developers or organisations -- two histories with
the same line count on the same day mint the same value -- and two
sessions created at the same instant in one repository can too.

Receipt identity vs task identity
---------------------------------
A ``receipt_id`` identifies one *record*. It says nothing about whether two
records are attempts at the same engineering task; that is task/work
identity (``task_id`` / ``work_id``), which OpenShard does not yet have an
authoritative source for. Attempt linkage remains explicit
(``run_attempt.resolve_shard_for_attempt``) and is never inferred from
prompt similarity or timing.

Format: ``rcpt_`` + 32 lowercase hex characters (a UUID4: 122 random bits,
no dependency, collision-resistant for this purpose).
"""

from __future__ import annotations

import re
import uuid

RECEIPT_ID_FIELD = "receipt_id"
RECEIPT_ID_PREFIX = "rcpt_"
_RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9a-f]{32}$")


def new_receipt_id() -> str:
    """Mint a fresh, globally unique receipt id. Never raises."""
    return f"{RECEIPT_ID_PREFIX}{uuid.uuid4().hex}"


def is_receipt_id(value: object) -> bool:
    """True when *value* is a well-formed receipt id."""
    return isinstance(value, str) and bool(_RECEIPT_ID_RE.match(value))


def stored_receipt_id(entry: object) -> str | None:
    """The receipt id persisted on *entry*, or None. Never mints."""
    if not isinstance(entry, dict):
        return None
    value = entry.get(RECEIPT_ID_FIELD)
    return value if is_receipt_id(value) else None


def ensure_receipt_id(entry: dict) -> str:
    """Stamp a receipt id on a record being *created* if it has none. Returns it.

    Only for writers at creation time -- read/render paths must use
    :func:`stored_receipt_id` so an old record is never given an identity
    it did not have when it was written.
    """
    existing = stored_receipt_id(entry)
    if existing is not None:
        return existing
    value = new_receipt_id()
    entry[RECEIPT_ID_FIELD] = value
    return value
