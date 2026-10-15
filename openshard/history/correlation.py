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
from typing import Any

from openshard.history.receipt_evidence import unsafe_text

CONTEXT_ENV = "OPENSHARD_CORRELATION_CONTEXT"
MAX_CONTEXT_BYTES = 8192
MAX_EXTERNAL_IDS = 16
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")
_NAMESPACE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_TRACE = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})\Z")


def identifier(value: object) -> str | None:
    """Reject unsafe/oversized IDs; truncation could alias different work."""
    if not isinstance(value, str) or not _ID.fullmatch(value) or "://" in value or unsafe_text(value):
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


def parse_correlation(raw: object) -> dict | None:
    """Decode bounded explicit context from an environment value or header."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return None
    try:
        return correlation_block(json.loads(raw))
    except (ValueError, RecursionError):
        return None


def launch_correlation(env: Mapping[str, str] | None = None) -> dict | None:
    """Explicit launch context, filled in with the Herdr pane when run inside one.

    ``OPENSHARD_CORRELATION_CONTEXT`` always wins: Herdr only supplies a
    missing ``source`` and appends its ids while the 16-id cap has room.
    """
    mapping = os.environ if env is None else env
    explicit = parse_correlation(mapping.get(CONTEXT_ENV))
    try:
        from openshard.util import herdr

        merged = herdr.merge_launch_context(explicit, herdr.launch_context(mapping), MAX_EXTERNAL_IDS)
    except Exception:  # Herdr is a best-effort enrichment, never a capture failure
        return explicit
    return correlation_block(merged)


def herdr_delegated_sessions(command: object, env: Mapping[str, str] | None = None) -> list[str]:
    """The child ``session_key`` of each Herdr delegation in *command*.

    Raw agent session ids are hashed with the function that produces the
    child Receipt's ``session.session_key`` and never stored.
    """
    return herdr_delegations(command, env)[0]


def herdr_delegations(command: object, env: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """``(child session_keys, target terminal ids)`` of the Herdr delegations in *command*.

    One ``herdr agent get`` per target. The terminal id is kept even when
    Herdr reports no agent session for the target, so the parent still names
    which pane it delegated to.
    """
    from openshard.history.receipt_evidence import session_key_for
    from openshard.util import herdr

    sessions, terminals = herdr.delegations(command, env)
    keys: list[str] = []
    for agent, session in sessions:
        key = identifier(session_key_for(agent, session))
        if key is not None and key not in keys:
            keys.append(key)
    clean_terminals = [t for t in (identifier(t) for t in terminals) if t is not None]
    return keys, list(dict.fromkeys(clean_terminals))


def herdr_delegated_or_none(value: object) -> list[str] | None:
    """Well-formed delegation ids only, deduplicated and capped."""
    if not isinstance(value, list):
        return None
    clean: list[str] = []
    for item in value:
        ident = identifier(item)
        if ident is not None and ident not in clean and len(clean) < MAX_EXTERNAL_IDS:
            clean.append(ident)
    return clean or None


def record_herdr_delegated(entry: Mapping[str, Any], namespace: str | None = None) -> list[str]:
    """Delegation ids a persisted record carries (sessions by default, or *namespace*)."""
    from openshard.util.herdr import DELEGATED_NAMESPACE

    wanted = namespace or DELEGATED_NAMESPACE
    block = correlation_block(entry.get("correlation"))
    return [link["id"] for link in (block or {}).get("external_ids") or [] if link.get("namespace") == wanted]


def record_herdr_terminals(entry: Mapping[str, Any]) -> list[str]:
    """Delegated Herdr terminal ids a persisted record carries."""
    from openshard.util.herdr import DELEGATED_TERMINAL_NAMESPACE

    return record_herdr_delegated(entry, DELEGATED_TERMINAL_NAMESPACE)


def stamp_record_correlation(
    entry: dict, correlation: object, herdr_delegated: object, herdr_terminals: object = None,
) -> None:
    """Copy a buffer's correlation onto its record, then append Herdr delegation ids (parent side).

    Declared and deterministic: delegated sessions then delegated terminals,
    first-seen order, after every other id; extras beyond the 16-id cap are
    dropped, so launch and explicit ids are never displaced.
    """
    from openshard.util.herdr import DELEGATED_NAMESPACE, DELEGATED_TERMINAL_NAMESPACE

    if correlation_block(correlation) is not None:
        entry["correlation"] = correlation_block(correlation)
    delegated = herdr_delegated_or_none(herdr_delegated) or []
    terminals = herdr_delegated_or_none(herdr_terminals) or []
    if not delegated and not terminals:
        return
    block = correlation_block(entry.get("correlation")) or {}
    ours = (DELEGATED_NAMESPACE, DELEGATED_TERMINAL_NAMESPACE)
    links = [link for link in block.get("external_ids") or [] if link.get("namespace") not in ours]
    links.extend({"namespace": DELEGATED_NAMESPACE, "id": ident} for ident in delegated)
    links.extend({"namespace": DELEGATED_TERMINAL_NAMESPACE, "id": ident} for ident in terminals)
    merged = correlation_block({**block, "external_ids": links})
    if merged is not None:
        entry["correlation"] = merged


def stamp_launch_correlation(entry: dict, env: Mapping[str, str] | None = None) -> None:
    """Stamp once before sealing a new record; never overwrite supplied context."""
    if "correlation" in entry:
        return
    block = launch_correlation(env)
    if block:
        entry["correlation"] = block


def workflow_timeline(entries: list[dict], *, task_id: str, herdr_terminal: str | None = None) -> list[dict]:
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
        if herdr_terminal is not None and not any(
            link.get("namespace") == "herdr.terminal" and link.get("id") == herdr_terminal
            for link in (correlation_block(entry.get("correlation")) or {}).get("external_ids", [])
        ):
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
