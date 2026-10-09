"""Deterministic content hash for Shard run records (Shard hash v1).

This module computes a stable SHA256 over the *content* of a persisted run
record so each Shard can carry (new records) or expose (legacy records) a
tamper-evidence fingerprint. It is **tamper-evidence only** — it proves the
stored record content has not changed since the hash was written. It is *not* a
correctness proof and *not* a cryptographic signature (no keys, no signing).

Design constraints
------------------
* Deterministic: the same logical record always hashes to the same value,
  regardless of key insertion order (canonical, sorted-key JSON).
* Self-referential safe: the ``content_hash`` field is always excluded from the
  hash input, so stamping a record never changes its own hash.
* No new secrets: the hash is computed over the record as-is. Callers stamp the
  hash *after* ``coerce_shard_entry`` has stripped ``SHARD_BLOCKED_FIELDS``, so
  no raw secret enters the input beyond what is already safely persisted.
* Never raises: ``compute_shard_hash`` and ``verify_shard_hash`` always return a
  value, even on odd/non-JSON-native field types (``default=str``).

Verification model
------------------
The stored ``content_hash`` is frozen at write time. ``verify_shard_hash``
recomputes the hash from the record's *current* content and compares, so a
record whose content was edited while keeping its old ``content_hash`` reports
``"mismatch"`` rather than silently returning the stale stored value.
"""

from __future__ import annotations

import hashlib
import json

# Public field name written into the run record.
SHARD_HASH_FIELD = "content_hash"

# Informational version. The algorithm is also self-describing via the
# ``sha256:`` prefix on every value, so a future v2 can swap algorithms without
# ambiguity.
SHARD_HASH_VERSION = "1"

# Set by ``history.store.load_history`` on a *coerced* record: the verdict of
# ``verify_shard_hash`` over the record exactly as it is stored on disk. Never
# persisted; a value found in a stored record is discarded on read, so the only
# writer is the loader. See :func:`integrity_as_stored`.
INTEGRITY_AS_STORED_FIELD = "_integrity_as_stored"

_INTEGRITY_VERDICTS = frozenset({"valid", "mismatch", "missing"})

# Never part of the hash input: the record's own hash, and the loader's
# read-time verdict (bookkeeping that is never written to disk).
_EXCLUDED_FROM_HASH = frozenset({SHARD_HASH_FIELD, INTEGRITY_AS_STORED_FIELD})


def _canonical_json(obj: object) -> str:
    """Serialize *obj* to canonical JSON: sorted keys, compact, stable.

    ``sort_keys`` makes key order irrelevant; the compact separators keep the
    byte stream stable; ``default=str`` keeps the call total on odd types so the
    hash never fails to compute.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def compute_shard_hash(entry: dict) -> str:
    """Return the canonical ``sha256:<hex>`` content hash of *entry*.

    The ``content_hash`` field itself is excluded from the input so stamping a
    record does not change its own hash (no self-referential bug), as is the
    loader's read-time ``_integrity_as_stored`` marker. Always reflects the
    record's *current* content. Never raises.
    """
    payload = {k: v for k, v in entry.items() if k not in _EXCLUDED_FROM_HASH}
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def stored_shard_hash(entry: dict) -> str | None:
    """Return the hash *written in* the record, or ``None`` if absent.

    Purely a reader of what is persisted — never recomputes.
    """
    value = entry.get(SHARD_HASH_FIELD)
    return value if isinstance(value, str) and value else None


def integrity_as_stored(entry: dict) -> str | None:
    """The loader's verdict over the stored bytes (``"valid"`` / ``"mismatch"`` /
    ``"missing"``), or ``None`` when *entry* carries none.

    Integrity is a property of what is stored. A coerced record (blocked
    fields stripped, defaults filled, metadata re-sanitised under *today's*
    rules) can differ from the bytes its ``content_hash`` was stamped over
    without anyone having edited the file, so recomputing the hash over the
    coerced content reports ``mismatch`` for an intact record. The loader
    verifies the parsed record before coercing it and carries that verdict
    here; readers prefer it to a recompute over coerced content.
    """
    value = entry.get(INTEGRITY_AS_STORED_FIELD) if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value in _INTEGRITY_VERDICTS else None


def verify_shard_hash(entry: dict) -> dict:
    """Verify the stored hash against a fresh recompute of current content.

    Returns a small, JSON-safe result::

        {
          "present": bool,            # a stored content_hash exists
          "stored_hash": str | None,  # the value written in the record (or None)
          "computed_hash": str,       # recomputed from current content (always)
          "matches": bool | None,     # stored == computed; None when no stored hash
          "status": str,              # "valid" | "mismatch" | "missing"
        }

    ``status`` is ``"missing"`` for legacy/pre-feature records (no stored hash),
    ``"valid"`` when the stored hash matches the current content, and
    ``"mismatch"`` when it does not (content edited after the hash was written).
    Never raises.
    """
    stored = stored_shard_hash(entry)
    computed = compute_shard_hash(entry)
    if stored is None:
        return {
            "present": False,
            "stored_hash": None,
            "computed_hash": computed,
            "matches": None,
            "status": "missing",
        }
    matches = stored == computed
    return {
        "present": True,
        "stored_hash": stored,
        "computed_hash": computed,
        "matches": matches,
        "status": "valid" if matches else "mismatch",
    }
