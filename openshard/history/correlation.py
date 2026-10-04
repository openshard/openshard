"""Declared cross-system links, never authority or verification evidence.

Shard/run/receipt/task identities stay unchanged. External IDs are opaque,
namespaced hints; equality does not prove that two records share an outcome.
Only explicit launch context is stamped, never the ambient environment on read.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from openshard.history.receipt_evidence import unsafe_text

CONTEXT_ENV = "OPENSHARD_CORRELATION_CONTEXT"
MAX_CONTEXT_BYTES = 8192
MAX_EXTERNAL_IDS = 16
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")
_NAMESPACE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_TRACE = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})\Z")


def identifier(value: object) -> str | None:
    """Reject unsafe/oversized IDs; truncation could alias different work."""
    if not isinstance(value, str) or not _ID.fullmatch(value) or unsafe_text(value):
        return None
    return value


def correlation_block(value: object) -> dict | None:
    """Privacy-bounded generic adapter boundary. Unknown stays absent.

    This v1 boundary accepts W3C version 00 traceparent only; it neither
    participates in nor forwards traces. Baggage/tracestate are not persisted.
    Malformed fields are discarded independently, never used to mint identity.
    """
    if not isinstance(value, dict):
        return None
    result: dict = {}
    for key in ("parent_run_id", "trigger", "source"):
        clean = identifier(value.get(key))
        if clean is not None:
            result[key] = clean
    traceparent = value.get("traceparent")
    match = _TRACE.fullmatch(traceparent) if isinstance(traceparent, str) else None
    if match and int(match[1], 16) and int(match[2], 16):
        result["traceparent"] = traceparent
    external = value.get("external_ids")
    if isinstance(external, list):
        links = []
        seen = set()
        for item in external[:MAX_EXTERNAL_IDS]:
            if not isinstance(item, dict):
                continue
            namespace, raw_id = item.get("namespace"), item.get("id")
            clean_id = identifier(raw_id)
            if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace) or clean_id is None:
                continue
            pair = (namespace, clean_id)
            if pair not in seen:
                links.append({"namespace": namespace, "id": clean_id})
                seen.add(pair)
        if links:
            result["external_ids"] = links
    if not result:
        return None
    return {"evidence": "declared", **result}


def stamp_launch_correlation(entry: dict, env: Mapping[str, str] | None = None) -> None:
    """Stamp once before sealing a new record; never overwrite supplied context."""
    if "correlation" in entry:
        return
    raw = (os.environ if env is None else env).get(CONTEXT_ENV)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return
    try:
        block = correlation_block(json.loads(raw))
    except (ValueError, RecursionError):
        return
    if block:
        entry["correlation"] = block


def workflow_timeline(entries: list[dict], *, task_id: str) -> list[dict]:
    """Join only explicitly attached task IDs, keeping individual Receipts.

    A local read-only view, not proof of causality or a hosted grouping key.
    Never match by timestamp, prompt, trace ID or unscoped shard labels.
    """
    from openshard.history.task_identity import is_task_id

    if not is_task_id(task_id):
        raise ValueError("A canonical task_id is required")
    rows = []
    for entry in entries:
        if entry.get("task_id") != task_id:
            continue
        rows.append({
            "task_id": task_id,
            "receipt_id": identifier(entry.get("receipt_id")),
            "shard_id": identifier(entry.get("shard_id")),
            "run_id": identifier(entry.get("run_id")),
            "timestamp": identifier(entry.get("timestamp")),
            "correlation": correlation_block(entry.get("correlation")),
        })
    return sorted(rows, key=lambda row: (row["timestamp"] or "", row["receipt_id"] or ""))
