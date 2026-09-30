"""Later verification evidence for an already-synced Receipt: the envelope Core sends.

A hosted Receipt is a first copy and is never resent (``sync/client.py``), so
evidence that arrives afterwards cannot travel inside it. ``openshard
verify`` and ``openshard verify --ci`` append attestations to
``.openshard/verifications.jsonl``; this module projects them, together with
Core's interpretation of them, into the Platform's
``openshard.verification-evidence`` contract:

``evidence``
    Each attestation that names the Receipt, as ``summarize_attestation``
    already validates it: id, time, kind, the verification block, and for CI
    the provider/binding/outcome tokens. Nothing is merged or recomputed.
``state``
    ``verification_truth.interpret_receipt`` over the Receipt's own record
    plus that evidence: the current outcome, who vouches for it, for which
    commit, and the history. The rendered label and the local integrity
    verdict are not sent.

The Receipt's own ``verification`` block is not part of this envelope and is
never changed by it. Privacy is unchanged from the Receipt path: a check is a
scrubbed, path-free name and an exit code; no output, log, URL or argv.

An attestation without a well-formed ``attestation_id`` or timestamp cannot
be made idempotent on the hosted side and is left out. When the evidence that
remains cannot support the state (the state rests on an attestation that was
left out), nothing is sent: a partial story is not sent as the whole one.

Pure, never raises.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from openshard.history.shard_contract import build_shard_receipt
from openshard.history.verification import SOURCES, STATUSES
from openshard.history.verification_truth import BASIS_CI, BASIS_POST_SESSION, interpret_receipt
from openshard.verification.post_session import KIND_CI, KIND_POST_SESSION, latest_for_entry

CONTRACT = "openshard.verification-evidence"
CONTRACT_VERSION = "1"
SOURCE_PRODUCT = "openshard-core"

_ATTESTATION_ID_RE = re.compile(r"^vat_[0-9a-f]{32}$")
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_HISTORY_KINDS = frozenset({"session", "rerun", "ci"})
_MAX_FAILED_CHECKS = 5
_MAX_HISTORY = 21

# ``VerificationTruth.to_dict`` keys that cross the boundary unchanged.
_STATE_ENUM_KEYS = ("state", "authority", "effective_status", "basis")
_STATE_STATUS_KEYS = ("session_status", "claim_status")
_STATE_SOURCE_KEYS = ("session_source", "claim_source")
_STATE_COUNT_KEYS = ("checks_passed", "checks_failed", "checks_attempted")


def _stamp(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 64 and _STAMP_RE.match(value) else None


def _sha(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA_RE.match(value) else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _choice(value: object, allowed: object) -> str | None:
    return value if isinstance(value, str) and value in allowed else None  # type: ignore[operator]


def evidence_items(post: dict | None) -> list[dict[str, Any]]:
    """The attestations carried by ``latest_for_entry``'s result that can be sent, oldest first."""
    raw = post.get("evidence") if isinstance(post, dict) else None
    items: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        attestation_id = item.get("attestation_id")
        created_at = _stamp(item.get("created_at"))
        block = item.get("verification")
        kind = item.get("kind")
        if (
            not isinstance(attestation_id, str)
            or not _ATTESTATION_ID_RE.match(attestation_id)
            or created_at is None
            or not isinstance(block, dict)
            or kind not in (KIND_POST_SESSION, KIND_CI)
        ):
            continue
        projected: dict[str, Any] = {
            "attestation_id": attestation_id,
            "created_at": created_at,
            "kind": kind,
            "verification": block,
        }
        if kind == KIND_CI:
            raw_ci = item.get("ci")
            ci: dict = raw_ci if isinstance(raw_ci, dict) else {}
            projected["ci"] = {key: ci.get(key) if isinstance(ci.get(key), str) else None
                               for key in ("provider", "binding", "outcome")}
        items.append(projected)
    return items


def _history_row(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict) or row.get("kind") not in _HISTORY_KINDS:
        return None
    return {
        "kind": row["kind"],
        "at": _stamp(row.get("at")),
        "source": _choice(row.get("source"), SOURCES),
        "status": _choice(row.get("status"), STATUSES),
        "checks_passed": _count(row.get("checks_passed")),
        "checks_failed": _count(row.get("checks_failed")),
        "checks_attempted": _count(row.get("checks_attempted")),
        "artifact_sha": _sha(row.get("artifact_sha")),
    }


def verification_state(truth: dict[str, Any]) -> dict[str, Any]:
    """``VerificationTruth.to_dict()`` as the contract carries it: structured fields only."""
    state: dict[str, Any] = {"version": 1}
    for key in _STATE_ENUM_KEYS:
        state[key] = truth.get(key)
    for key in _STATE_STATUS_KEYS:
        state[key] = _choice(truth.get(key), STATUSES)
    for key in _STATE_SOURCE_KEYS:
        state[key] = _choice(truth.get(key), SOURCES)
    state["artifact_sha"] = _sha(truth.get("artifact_sha"))
    for key in _STATE_COUNT_KEYS:
        state[key] = _count(truth.get(key))
    failed = truth.get("failed_checks")
    state["failed_checks"] = [
        name[:120] for name in (failed if isinstance(failed, list) else []) if isinstance(name, str) and name
    ][:_MAX_FAILED_CHECKS]
    history = truth.get("history")
    rows = [_history_row(row) for row in (history if isinstance(history, list) else [])]
    state["history"] = [row for row in rows if row is not None][-_MAX_HISTORY:]
    return state


def _supported(state: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """Whether the evidence being sent contains what the state rests on."""
    basis = state.get("basis")
    if basis not in (BASIS_CI, BASIS_POST_SESSION):
        return True
    kind = KIND_CI if basis == BASIS_CI else KIND_POST_SESSION
    return any(
        item["kind"] == kind
        and item["verification"].get("status") == state.get("effective_status")
        and item["verification"].get("artifact_sha") == state.get("artifact_sha")
        for item in items
    )


def build_evidence_envelope(
    entry: dict, index: int, attestations: list[dict], *, core_version: str,
) -> dict[str, Any] | None:
    """The evidence envelope for *entry*, or None when it has no later evidence to send."""
    try:
        receipt_id = entry.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            return None
        post = latest_for_entry(entry, attestations)
        items = evidence_items(post)
        if not items:
            return None
        truth = interpret_receipt(build_shard_receipt(entry, index=index, post_session_verification=post)).to_dict()
        state = verification_state(truth)
        if not _supported(state, items):
            return None
        return {
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "source": {"product": SOURCE_PRODUCT, "version": core_version},
            "receipt_id": receipt_id,
            "evidence": items,
            "state": state,
        }
    except Exception:
        return None


def evidence_hash(envelope: dict) -> str:
    """``sha256:<hex>`` of the evidence and state this machine would send. Local bookkeeping only."""
    body = {"evidence": envelope.get("evidence"), "state": envelope.get("state")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "build_evidence_envelope",
    "evidence_hash",
    "evidence_items",
    "verification_state",
]
