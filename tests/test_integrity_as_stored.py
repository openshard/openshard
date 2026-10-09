"""Integrity is verified over the record as stored, on every read path.

The hosted sync and the Insights warehouse read history coerced
(``load_history(coerce=True)``); ``openshard last`` reads it as stored.
Coercion applies today's rules (blocked fields, metadata sanitising,
defaults) to a record written under yesterday's, so recomputing the content
hash over the coerced content reports ``mismatch`` for a record nobody has
edited, and the hosted Receipt says "Checksum mismatch (record edited after
it was written)" while the local one says "Checksum matches". Every reader
must give the same verdict for the same bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from openshard.history.shard_contract import (
    build_shard_receipt,
    integrity_display,
    integrity_status,
)
from openshard.history.shard_hash import (
    INTEGRITY_AS_STORED_FIELD,
    compute_shard_hash,
    integrity_as_stored,
)
from openshard.history.shard_schema import coerce_shard_entry
from openshard.history.store import load_history
from openshard.sync.envelope import receipt_payload

RECEIPT_ID = "rcpt_0123456789abcdef0123456789abcdef"


def _written_by_an_older_core() -> dict:
    """A record as an older writer stored it: a field today's coercion strips
    (``transcript`` is blocked now) and a metadata value today's sanitiser
    drops, with the hash stamped over exactly that content."""
    entry = {
        "timestamp": "2026-09-01T10:00:00+00:00",
        "task": "Fix the flaky integration test",
        "agent": "Claude Code (external)",
        "receipt_id": RECEIPT_ID,
        "schema_version": "1.1",
        "capture": {"depth": "partial"},
        "metadata": {"workspace": "/Users/dev/widget"},
        "transcript": "persisted before the field was blocked",
    }
    entry["content_hash"] = compute_shard_hash(entry)
    return entry


def _write(tmp_path: Path, *entries: dict) -> Path:
    runs = tmp_path / ".openshard" / "runs.jsonl"
    runs.parent.mkdir(parents=True)
    runs.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return runs


def test_coercion_changes_the_content_but_not_the_verdict(tmp_path: Path):
    runs = _write(tmp_path, _written_by_an_older_core())
    [coerced] = load_history(runs, coerce=True)
    [stored] = load_history(runs, coerce=False)

    # The premise: today's coercion really does alter this record.
    assert "transcript" not in coerced and coerced.get("metadata") == {}
    assert compute_shard_hash(coerced) != stored["content_hash"]

    assert integrity_status(stored) == "valid"
    assert integrity_status(coerced) == "valid"
    assert integrity_display(coerced) == "Checksum matches"


def test_hosted_receipt_agrees_with_openshard_last(tmp_path: Path):
    runs = _write(tmp_path, _written_by_an_older_core())
    [coerced] = load_history(runs, coerce=True)
    [stored] = load_history(runs, coerce=False)

    local = build_shard_receipt(stored, index=0)
    hosted = receipt_payload(coerced, 0)
    assert local.integrity_status == "valid"
    assert local.integrity == "Checksum matches"
    assert hosted["integrity"] == local.integrity
    # The loader's marker is bookkeeping for readers, never part of the Receipt.
    assert INTEGRITY_AS_STORED_FIELD not in hosted


def test_an_edited_record_still_reads_as_a_mismatch_everywhere(tmp_path: Path):
    edited = _written_by_an_older_core()
    edited["task"] = "a task text changed after the hash was written"
    runs = _write(tmp_path, edited)
    [coerced] = load_history(runs, coerce=True)
    [stored] = load_history(runs, coerce=False)

    assert integrity_status(stored) == "mismatch"
    assert integrity_status(coerced) == "mismatch"
    assert receipt_payload(coerced, 0)["integrity"] == "Checksum mismatch (record edited after it was written)"


def test_a_legacy_record_without_a_hash_stays_not_recorded(tmp_path: Path):
    legacy = _written_by_an_older_core()
    del legacy["content_hash"]
    runs = _write(tmp_path, legacy)
    [coerced] = load_history(runs, coerce=True)
    assert integrity_as_stored(coerced) == "missing"
    assert integrity_display(coerced) == "Not recorded"


def test_a_marker_in_the_stored_record_is_never_trusted(tmp_path: Path):
    forged = _written_by_an_older_core()
    forged["task"] = "edited"
    forged[INTEGRITY_AS_STORED_FIELD] = "valid"
    runs = _write(tmp_path, forged)
    [coerced] = load_history(runs, coerce=True)
    [stored] = load_history(runs, coerce=False)

    assert INTEGRITY_AS_STORED_FIELD not in stored
    assert integrity_status(stored) == "mismatch"
    assert integrity_status(coerced) == "mismatch"
    # Coercion alone drops the reserved key too, whatever the caller passes in.
    assert INTEGRITY_AS_STORED_FIELD not in coerce_shard_entry(forged)


def test_an_in_memory_record_is_verified_directly():
    fresh = coerce_shard_entry({"timestamp": "2026-10-01T10:00:00+00:00", "task": "x"})
    assert integrity_as_stored(fresh) is None
    assert integrity_status(fresh) == "valid"
    fresh["task"] = "y"
    assert integrity_status(fresh) == "mismatch"
