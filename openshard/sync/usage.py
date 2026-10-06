"""Later usage evidence for an already-synced Receipt: the envelope Core sends.

A hosted Receipt is a first copy and is never resent, so usage that arrives
afterwards (Cursor Cloud Agents ``/usage``, Admin usage events) cannot travel
inside it. ``openshard usage reconcile`` appends attestations to
``.openshard/usage.jsonl``; this module projects them, together with Core's
interpretation of the record plus those attestations, into the Platform's
``openshard.usage-evidence`` contract:

``usage``
    ``history/usage_evidence.effective_usage`` over the Receipt plus the
    attestations that name it: tokens, cost, model, each with status/source.
``evidence``
    Each usage attestation that names the Receipt: id, time, kind, the
    correlation keys, and the usage block it carried. Nothing is merged.

The Receipt payload itself is not part of this envelope and is never changed
by it. An attestation without a well-formed ``attestation_id`` or timestamp
cannot be made idempotent on the hosted side and is left out. When the usage
that remains cannot support the stated status (it rests on an attestation
that was left out), nothing is sent.

Pure, never raises.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Container
from typing import Any

from openshard.history.usage_evidence import (
    KIND_USAGE,
    SOURCES,
    STATUSES,
    SURFACES,
    TOKEN_KEYS,
    USAGE_VERSION,
    effective_usage,
    parse_usage_block,
    usage_attestations_for_entry,
)

CONTRACT = "openshard.usage-evidence"
CONTRACT_VERSION = "1"
SOURCE_PRODUCT = "openshard-core"

_ATTESTATION_ID_RE = re.compile(r"^uat_[0-9a-f]{32}$")
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?$")


def _stamp(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 64 and _STAMP_RE.match(value) else None


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _choice(value: object, allowed: Container[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def evidence_items(attestations: list[dict]) -> list[dict[str, Any]]:
    """The usage attestations that can be sent, oldest first."""
    items: list[dict[str, Any]] = []
    for item in attestations:
        if not isinstance(item, dict):
            continue
        attestation_id = item.get("attestation_id")
        created_at = _stamp(item.get("created_at"))
        usage = parse_usage_block(item.get("usage"))
        if (
            not isinstance(attestation_id, str)
            or not _ATTESTATION_ID_RE.match(attestation_id)
            or created_at is None
            or usage is None
            or item.get("kind") != KIND_USAGE
        ):
            continue
        raw_corr = _as_dict(item.get("correlation"))
        items.append({
            "attestation_id": attestation_id,
            "created_at": created_at,
            "kind": KIND_USAGE,
            "correlation": {
                "surface": _choice(raw_corr.get("surface"), SURFACES),
                "key": raw_corr.get("key") if isinstance(raw_corr.get("key"), str) else None,
                "key_value": raw_corr.get("key_value") if isinstance(raw_corr.get("key_value"), str) else None,
                "scope": raw_corr.get("scope") if isinstance(raw_corr.get("scope"), str) else None,
            },
            "usage": usage,
        })
    return items


def usage_payload(block: dict[str, Any]) -> dict[str, Any]:
    """``effective_usage`` as the contract carries it: structured fields only."""
    tokens_in = _as_dict(block.get("tokens"))
    cost_in = _as_dict(block.get("cost"))
    model_in = _as_dict(block.get("model"))
    tokens = {
        "status": _choice(tokens_in.get("status"), STATUSES),
        "source": _choice(tokens_in.get("source"), SOURCES),
        "surface": _choice(tokens_in.get("surface"), SURFACES),
        "complete": tokens_in.get("complete") if isinstance(tokens_in.get("complete"), bool) else None,
        "total": tokens_in.get("total") if isinstance(tokens_in.get("total"), int)
        and not isinstance(tokens_in.get("total"), bool) else None,
    }
    for key in TOKEN_KEYS:
        value = tokens_in.get(key)
        tokens[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    rate_in = _as_dict(cost_in.get("rate"))
    cost = {
        "status": _choice(cost_in.get("status"), STATUSES),
        "source": _choice(cost_in.get("source"), SOURCES),
        "surface": _choice(cost_in.get("surface"), SURFACES),
        "usd": cost_in.get("usd") if isinstance(cost_in.get("usd"), (int, float))
        and not isinstance(cost_in.get("usd"), bool) else None,
        "model_cost_usd": cost_in.get("model_cost_usd") if isinstance(cost_in.get("model_cost_usd"), (int, float))
        and not isinstance(cost_in.get("model_cost_usd"), bool) else None,
        "platform_fee_usd": cost_in.get("platform_fee_usd")
        if isinstance(cost_in.get("platform_fee_usd"), (int, float))
        and not isinstance(cost_in.get("platform_fee_usd"), bool) else None,
        "complete": cost_in.get("complete") if isinstance(cost_in.get("complete"), bool) else None,
        "rate": {
            "provider": rate_in.get("provider") if isinstance(rate_in.get("provider"), str) else None,
            "model_id": rate_in.get("model_id") if isinstance(rate_in.get("model_id"), str) else None,
            "pricing_version": rate_in.get("pricing_version") if isinstance(rate_in.get("pricing_version"), str)
            else None,
        } if rate_in else None,
    }
    models = [m for m in (model_in.get("models") or []) if isinstance(m, str)][:5]
    model = {
        "id": model_in.get("id") if isinstance(model_in.get("id"), str) else None,
        "source": model_in.get("source") if isinstance(model_in.get("source"), str) else None,
        "models": models,
    }
    reconciled = []
    for item in block.get("reconciled_by") or []:
        if not isinstance(item, dict):
            continue
        rid = item.get("attestation_id")
        if not isinstance(rid, str) or not _ATTESTATION_ID_RE.match(rid):
            continue
        reconciled.append({
            "attestation_id": rid,
            "created_at": _stamp(item.get("created_at")),
            "surface": _choice(item.get("surface"), SURFACES),
        })
    agent = block.get("agent")
    return {
        "version": USAGE_VERSION,
        "agent": agent if isinstance(agent, str) else None,
        "model": model,
        "tokens": tokens,
        "cost": cost,
        "reconciled_by": reconciled,
    }


def _supported(usage: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """Whether the evidence being sent contains what a reconciled status rests on."""
    for dim_key in ("tokens", "cost"):
        dim = usage.get(dim_key) or {}
        if dim.get("status") != "reconciled":
            continue
        surface = dim.get("surface")
        if surface and not any(
            (item.get("correlation") or {}).get("surface") == surface for item in items
        ):
            return False
    return True


def build_usage_envelope(
    entry: dict, attestations: list[dict], *, core_version: str,
) -> dict[str, Any] | None:
    """The usage-evidence envelope for *entry*, or None when it has none to send."""
    try:
        receipt_id = entry.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            return None
        named = usage_attestations_for_entry(entry, attestations)
        items = evidence_items(named)
        if not items:
            return None
        usage = usage_payload(effective_usage(entry, named))
        if not _supported(usage, items):
            return None
        return {
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "source": {"product": SOURCE_PRODUCT, "version": core_version},
            "receipt_id": receipt_id,
            "usage": usage,
            "evidence": items,
        }
    except Exception:
        return None


def usage_hash(envelope: dict) -> str:
    """``sha256:<hex>`` of the usage and evidence this machine would send. Local bookkeeping only."""
    body = {"usage": envelope.get("usage"), "evidence": envelope.get("evidence")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "build_usage_envelope",
    "evidence_items",
    "usage_hash",
    "usage_payload",
]
