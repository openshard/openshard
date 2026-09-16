"""Central Shard schema constants, coercion helpers, and canonical predicates.

This module is the single source of truth for:

* The current schema version written to new run records.
* The set of field names that must never be persisted.
* The canonical set of timeline/event field names (documentation for v1.2;
  enforcement at the RunTimelineEvent layer is a v1.3 concern).
* ``coerce_shard_entry`` — the single coercion chokepoint applied at both the
  write path (pipeline._log_run) and the load path (metrics.load_runs).
* Shared receipt predicates that were previously duplicated across
  ``failures.py`` and ``trust_score.py``.

Design constraints
------------------
* ``coerce_shard_entry`` never raises and never mutates its input.
* Blocked fields are always stripped *first* so they cannot leak even if a
  later step raises.
* No proof is fabricated: missing verification/approval fields are left as
  ``None``, never filled with ``False``.
* On exception after the blocked-field strip, a static ``"coerce_failed"``
  token is stored under ``_coerce_warning`` — no exception text, no stack
  traces, no raw data.
* Old records (missing ``schema_version``, or version ``"1.0"``/``"1.1"``)
  load safely and degrade to conservative defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openshard.history.shard_hash import SHARD_HASH_FIELD, compute_shard_hash
from openshard.safety.sanitize import sanitize_metadata

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

SHARD_SCHEMA_VERSION = "1.2"

# ---------------------------------------------------------------------------
# Blocked fields — must never be persisted in run records
# ---------------------------------------------------------------------------

SHARD_BLOCKED_FIELDS: frozenset[str] = frozenset({
    # Raw prompt / instruction content
    "raw_prompt",
    "prompt_text",
    "system_prompt",
    "user_prompt",
    # Raw diff / file content
    "raw_diff",
    "diff_text",
    "raw_file_content",
    "file_content",
    # Raw transcript / model output
    "raw_transcript",
    "transcript",
    "raw_output",
    "model_output",
    # Raw feedback / correction text
    "raw_feedback",
    "feedback_text",
    "raw_correction",
    # Raw error detail
    "raw_error",
    "raw_error_message",
    "raw_exception",
    "stack_trace",
    "traceback",
})

# ---------------------------------------------------------------------------
# Canonical timeline event field names (v1.2 spec, documentation only)
# ---------------------------------------------------------------------------

TIMELINE_EVENT_FIELDS: frozenset[str] = frozenset({
    # Spec-canonical fields (v1.2+)
    "event_id",
    "shard_id",
    "run_id",
    "parent_event_id",
    "timestamp",
    "event_type",
    "stage",
    "actor",
    "status",
    "duration_ms",
    "metadata",
    # Legacy fields (v1.0/v1.1 — still valid, kept for backward compat)
    "event",
    "label",
    "kind",
    "detail",
    "target",
    "count",
})

# ---------------------------------------------------------------------------
# Coercion helper
# ---------------------------------------------------------------------------


def _strip_blocked(obj: object, depth: int = 0) -> object:
    """Recursively strip SHARD_BLOCKED_FIELDS from dicts and lists.

    Bounded to 3 levels deep to keep the cost predictable.
    """
    if depth > 3:
        return obj
    if isinstance(obj, dict):
        return {
            k: _strip_blocked(v, depth + 1)
            for k, v in obj.items()
            if k not in SHARD_BLOCKED_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_blocked(item, depth + 1) for item in obj]
    return obj


def coerce_shard_entry(entry: object, *, stamp_hash: bool = True) -> dict:
    """Coerce a raw run-history entry to a safe, defaults-filled dict.

    Accepts records at any ``schema_version`` (including missing).  Never
    raises.  Returns a new dict — does not mutate *entry*.

    ``stamp_hash`` (default ``True``) is the *write-path* behaviour: a record
    with no ``content_hash`` gets one computed over its coerced content.
    Readers (``history.store.load_history``) pass ``stamp_hash=False`` so a
    legacy record is never given an integrity it did not have when it was
    written -- its receipt keeps reporting ``Not recorded`` instead of a
    fabricated ``Matches``. A stored hash is never overwritten either way.

    * If *entry* is not a dict, returns a minimal safe dict immediately.
    * Blocked fields are stripped recursively **first** so they cannot appear
      in the result even if a later step raises.
    * On exception after stripping, the stripped result is returned with
      ``_coerce_warning`` set to the static token ``"coerce_failed"`` (no
      exception text, no stack traces).
    """
    # Guard: non-dict input returns a minimal safe record immediately.
    if not isinstance(entry, dict):
        return {"schema_version": "unknown", "_coerce_warning": "invalid_entry"}

    # Step 1 — recursively strip blocked fields (fail closed).
    result: dict = {
        k: _strip_blocked(v)
        for k, v in entry.items()
        if k not in SHARD_BLOCKED_FIELDS
    }

    # Step 2 — remaining coercion; any exception returns the stripped result.
    try:
        # Stamp schema_version for pre-hardening records without fabricating a
        # current version — "unknown" signals the record predates validation.
        if "schema_version" not in result:
            result["schema_version"] = "unknown"

        # Sanitize a top-level "metadata" dict if present; leave all other
        # structured sub-dicts (form_factor, osn_*, etc.) intact for consumers.
        if isinstance(result.get("metadata"), dict):
            result["metadata"] = sanitize_metadata(result["metadata"])

        # Stamp the tamper-evidence content hash when missing. Compute-if-missing
        # (never overwrite) keeps a write-time hash authoritative on read, so a
        # later mismatch surfaces tampering instead of being silently re-stamped.
        # Computed last so the hash covers the fully coerced (blocked-fields-
        # stripped) content.
        if stamp_hash and SHARD_HASH_FIELD not in result:
            result[SHARD_HASH_FIELD] = compute_shard_hash(result)

    except Exception:
        result["_coerce_warning"] = "coerce_failed"

    return result


# ---------------------------------------------------------------------------
# Canonical receipt predicates (single source of truth)
# ---------------------------------------------------------------------------


def shard_changes_made(receipt: ShardReceipt) -> bool:
    """True when the run recorded at least one file change."""
    try:
        return bool(receipt.files_detail) or (receipt.files_changed or 0) > 0
    except Exception:
        return False


def shard_manual_fix_required(receipt: ShardReceipt) -> bool:
    """True when developer feedback flagged that a manual fix was required."""
    df = getattr(receipt, "developer_feedback", None)
    return bool(df.get("manual_fix_required")) if isinstance(df, dict) else False
