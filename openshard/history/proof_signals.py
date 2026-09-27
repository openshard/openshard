"""Neutral, public proof-signal helpers derived from a Shard receipt.

These are small, pure reductions of a ``ShardReceipt`` into proof signals that
several consumers need (the proof contract, the CI policy check, completeness).
They live in the ``history`` layer - the home of core Shard logic - so that
``history`` modules never have to reach into private ``ci`` helpers.

All functions are pure (no I/O), never raise, and emit only safe tokens / ints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openshard.history.shard_contract import ShardReceipt


_VERIFICATION_TOKENS: frozenset[str] = frozenset(
    {"passed", "failed", "partial", "skipped", "manual_review", "not_run", "unknown"}
)


def verification_status_from_receipt(receipt: ShardReceipt) -> str:
    """Map a receipt's evidence into a verification enum.

    Returns one of: ``passed`` | ``failed`` | ``partial`` | ``skipped`` |
    ``manual_review`` | ``not_run`` | ``unknown``.

    This is the ``effective_status`` of ``history.verification_truth``: the
    latest ``openshard verify`` re-run carried on the receipt wins, an
    observed outcome is reported as-is, and an agent-reported *pass* is
    ``unknown`` (recorded, but not something OpenShard verified). Every
    consumer of the flat token -- proof contract, trust score, CI policy
    check, completeness, quality summary -- therefore agrees.
    """
    try:
        from openshard.history.verification_truth import interpret_receipt

        token = interpret_receipt(receipt).effective_status
        return token if token in _VERIFICATION_TOKENS else "unknown"
    except Exception:
        return "unknown"


def secret_scan_finding_count(receipt: ShardReceipt) -> int:
    """Count redacted secret-scan evidence capsules on the receipt."""
    try:
        return sum(
            1
            for ec in (receipt.evidence_capsules or [])
            if getattr(ec, "kind", None) == "secret_scan"
        )
    except Exception:
        return 0
