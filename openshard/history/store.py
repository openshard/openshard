"""Canonical read and amend access to ``.openshard/runs.jsonl``.

Every CLI surface that reads history, finds "the latest Receipt", or adds
metadata to a stored record goes through this module, so all of them agree on
three things:

1. **What a record is.** A line that parses to a JSON object. Blank lines,
   malformed lines and JSON that is not an object are skipped on read and
   preserved byte-for-byte on write -- never "repaired", never re-serialized.
2. **What the latest record is.** The last such line in the file. The loader's
   final element and the amendment target are chosen by the same rule.
3. **How a record is presented.** Either as stored (``coerce=False``: the
   receipt renderers whitelist what they print, and the proof layer must see
   a persisted blocked field to flag the record unsafe) or coerced through
   :func:`openshard.history.shard_schema.coerce_shard_entry` with
   ``stamp_hash=False`` (``coerce=True``, the default for aggregate consumers:
   blocked fields stripped, defaults filled). In both modes a record written
   without a ``content_hash`` is *not* given one on read. Integrity is a
   property of what is stored, so a legacy Receipt keeps reporting
   ``Not recorded`` rather than a fabricated ``Matches``.

Amendment contract (``amend_latest_record``)
--------------------------------------------
OpenShard itself legitimately adds metadata to a Receipt after it was written
(``openshard note``, ``openshard feedback``). The stored ``content_hash`` is a
fingerprint of the record's content, so appending to the record without
re-stamping turns a truthful ``Integrity: Matches`` into a false
``Mismatch``. The canonical path therefore:

* verifies the stored hash **before** applying the change, on the persisted
  record as-is;
* applies the caller's mutation to a copy of that persisted record (the
  historical content is never re-coerced or re-shaped on the way back to
  disk);
* appends an additive ``amendments`` entry recording what was amended, when,
  by which source, and what the integrity verdict was beforehand;
* preserves the integrity *verdict* across the amendment:

  ==========  ============================================================
  before      after
  ==========  ============================================================
  valid       ``content_hash`` re-stamped over the amended content -> valid
  missing     left without a hash -> still ``Not recorded``
  mismatch    stored hash left untouched -> still ``Mismatch``
  ==========  ============================================================

  A record that already read as tampered is never quietly re-blessed, and a
  record that never carried a hash is never retroactively given one.

``receipt_id`` and ``shard_id`` are not touched: the former is a stored field
this module only ever copies through, the latter is derived from the record's
position and timestamp, neither of which an amendment changes.
"""

from __future__ import annotations

import copy
import datetime
import json
from collections.abc import Callable
from pathlib import Path

from openshard.history.jsonl_store import amend_last_jsonl
from openshard.history.shard_hash import SHARD_HASH_FIELD, compute_shard_hash, verify_shard_hash
from openshard.history.shard_schema import coerce_shard_entry

AMENDMENTS_FIELD = "amendments"
AMENDMENT_SCHEMA_VERSION = 1

# Integrity verdicts, as reported by ``verify_shard_hash``.
_INTEGRITY_VALID = "valid"


def _parse_record(line: str) -> dict | None:
    """The JSON object on *line*, or ``None`` for blank / malformed / non-object."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_history(runs_path: Path, *, coerce: bool = True) -> list[dict]:
    """Return every well-formed record in *runs_path*, oldest first.

    With ``coerce=True`` records are coerced for consumers (blocked fields
    stripped, defaults filled); with ``coerce=False`` they are returned as
    stored. Neither mode gives a record a ``content_hash`` it did not carry on
    disk. Blank, malformed and non-object lines are skipped. A missing or
    unreadable file yields ``[]``. Never raises on content.
    """
    runs_path = Path(runs_path)
    try:
        if not runs_path.exists():
            return []
        text = runs_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        parsed = _parse_record(line)
        if parsed is None:
            continue
        records.append(coerce_shard_entry(parsed, stamp_hash=False) if coerce else parsed)
    return records


def latest_record(runs_path: Path, *, coerce: bool = True) -> tuple[int, dict] | None:
    """The newest well-formed record and its index among well-formed records.

    The index is the record's position in :func:`load_history` -- the value
    ``shard_id`` derivation and the receipt renderers already use. ``None``
    when there is no history.
    """
    records = load_history(runs_path, coerce=coerce)
    if not records:
        return None
    return len(records) - 1, records[-1]


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def amend_latest_record(
    runs_path: Path,
    kind: str,
    mutate: Callable[[dict], None],
    *,
    source: str = "cli",
) -> dict | None:
    """Apply *mutate* to the latest stored record under the history lock.

    *kind* names what is being amended (``"note"``, ``"developer_feedback"``)
    and is recorded in the additive ``amendments`` list. *mutate* receives a
    deep copy of the persisted record and edits it in place; it must only add
    or replace the metadata it owns.

    Returns the record exactly as written, or ``None`` when there is no record
    to amend (nothing is written and no directory is created in that case).
    See the module docstring for the integrity contract.
    """
    runs_path = Path(runs_path)

    def _transform(stored: dict) -> dict:
        before = verify_shard_hash(stored)
        record = copy.deepcopy(stored)
        mutate(record)

        existing = record.get(AMENDMENTS_FIELD)
        amendments = (
            list(existing)
            if isinstance(existing, list) and all(isinstance(a, dict) for a in existing)
            else []
        )
        restamp = before["status"] == _INTEGRITY_VALID
        amendments.append({
            "schema_version": AMENDMENT_SCHEMA_VERSION,
            "kind": kind,
            "recorded_at": _utc_now_iso(),
            "source": source,
            "integrity_before": before["status"],
            "content_hash_restamped": restamp,
        })
        record[AMENDMENTS_FIELD] = amendments

        if restamp:
            record[SHARD_HASH_FIELD] = compute_shard_hash(record)
        # "missing": never stamp on amendment -- the record stays "Not recorded".
        # "mismatch": leave the stored hash alone -- the record stays "Mismatch".
        return record

    return amend_last_jsonl(runs_path, _transform)
