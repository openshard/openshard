"""Canonical record -> receipt sync envelope v1, and when a record may go.

The ``receipt`` object is ``history.views.receipt_to_dict(receipt,
extended=True)`` built from the stored record -- the projection
``openshard history --json`` prints -- including the structured
``verification`` block and ``task_title``. ``receipt_payload`` only omits
unrecorded optional keys and adds the recorded ``session.task_source`` and
``session.hook_config_hash`` (contract values only); nothing is renamed or
filled in: a key the Platform contract does not define is the Platform's to reject,
and that rejection is recorded locally rather than papered over.

Quiescence
----------
A hook-captured session is upserted into ``runs.jsonl`` at every ``Stop``,
so its record changes for as long as the agent session is open, while the
Platform keeps the first copy it accepted and reports different content
under the same ``receipt_id`` as a conflict. A hook record is therefore
eligible only once the session ended (``capture.session_end_observed``) or
has been idle for :data:`QUIESCENT_SECONDS` (the same hour after which the
capture path itself sweeps a stale session). Records written once (imports,
``wrap``, native runs) are eligible immediately.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from openshard.history.receipt_identity import stored_receipt_id
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_schema import SHARD_SCHEMA_VERSION
from openshard.history.views import receipt_to_dict

CONTRACT = "openshard.receipt-sync"
CONTRACT_VERSION = "1"
SOURCE_PRODUCT = "openshard-core"

QUIESCENT_SECONDS = 60 * 60

REASON_NO_RECEIPT_ID = "no_receipt_id"
REASON_SESSION_IN_PROGRESS = "session_in_progress"
REASON_SESSION_ENDED = "session_ended"
REASON_SESSION_QUIESCENT = "session_quiescent"
REASON_TURN_SEALED = "turn_sealed"
REASON_RECORD_COMPLETE = "record_complete"

# Platform receipt-sync ``session.task_source`` values (Platform #230).
_HOOK_CONFIG_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TASK_SOURCES = frozenset({"first_user_prompt_excerpt", "agent_reported", "not_captured", "imported", "unknown"})
# Platform receipt-sync ``task_grouping`` values Core records (Platform #243; ``branch`` is not recorded yet).
TASK_GROUPINGS = frozenset({"explicit", "session"})


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str


def _seconds_since(stamp: object, now: datetime) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds()


def eligibility(entry: dict, *, now: datetime | None = None) -> Eligibility:
    """Whether *entry* may be synced right now, and why (not). Never raises."""
    if stored_receipt_id(entry) is None:
        return Eligibility(False, REASON_NO_RECEIPT_ID)
    capture = entry.get("capture")
    if not isinstance(capture, dict) or "session_end_observed" not in capture:
        return Eligibility(True, REASON_RECORD_COMPLETE)
    if capture.get("session_end_observed") is True or capture.get("status") == "ended":
        return Eligibility(True, REASON_SESSION_ENDED)
    if capture.get("receipt_scope") == "turn" and isinstance(capture.get("turn_sealed_at"), str):
        # A per-turn Receipt (docs/per-turn-receipts.md) is final once its turn completed.
        return Eligibility(True, REASON_TURN_SEALED)
    completeness = capture.get("completeness")
    reasons = completeness.get("reasons", []) if isinstance(completeness, dict) else []
    if any(isinstance(r, dict) and r.get("kind") == "session_end_not_observed" for r in reasons):
        return Eligibility(True, REASON_SESSION_QUIESCENT)
    current = now if now is not None else datetime.now(UTC)
    age = _seconds_since(capture.get("last_activity_at"), current)
    if age is None or age >= QUIESCENT_SECONDS:
        return Eligibility(True, REASON_SESSION_QUIESCENT)
    return Eligibility(False, REASON_SESSION_IN_PROGRESS)


def receipt_payload(entry: dict, index: int, *, multi_agent: bool = False) -> dict:
    """The privacy-bounded machine receipt for the record at history position *index*.

    ``multi_agent`` keeps the multi-agent block (Platform contract
    ``multi_agent``); without it the key is removed entirely, so a Platform
    that does not know the block, or an organisation that has not enabled
    ``advanced_osn``, receives exactly the receipt it did before.
    """
    payload = receipt_to_dict(build_shard_receipt(entry, index=index), extended=True)
    # The Platform contract accepts the reason since Platform #230; it is
    # sent only when recorded, never as a placeholder.
    if payload.get("lines_not_recorded_reason") is None:
        payload.pop("lines_not_recorded_reason", None)
    # Two recorded capture facts the session block does not project yet (receipt_evidence
    # is at its size budget): where the task text came from, as one of the contract's
    # values, and the digest of the hook configuration that captured the session (#586),
    # never the configuration. Absent or malformed: omitted (unknown).
    session = payload.get("session")
    capture = entry.get("capture")
    if isinstance(session, dict) and isinstance(capture, dict):
        if capture.get("task_source") in TASK_SOURCES:
            session["task_source"] = capture["task_source"]
        digest = capture.get("hook_config_hash")
        if isinstance(digest, str) and _HOOK_CONFIG_HASH_RE.match(digest):
            session["hook_config_hash"] = digest
    if isinstance(capture, dict) and capture.get("task_grouping") in TASK_GROUPINGS and payload.get("task_id"):
        # How Core arrived at task_id (Platform #243): ``explicit`` (declared) or
        # ``session`` (OpenShard grouped a session's turns; never a declaration).
        # Sent only with the task_id it labels; absent means explicit.
        payload["task_grouping"] = capture["task_grouping"]
    correlation = payload.get("correlation")
    if isinstance(correlation, dict) and "parent_task_id" in correlation:
        # A declared parent task (history/task_edges.py) is local until the
        # Platform contract defines it; the rest of the block is unchanged.
        correlation.pop("parent_task_id")
        if set(correlation) <= {"evidence"}:
            payload.pop("correlation")
    if not multi_agent:
        payload.pop("multi_agent", None)
    subagents = subagents_block(capture)
    if subagents is not None:
        payload["subagents"] = subagents
    return payload


# The Platform contract's ``subagents`` keys (Platform ``subagentRunSchema``); anything else stays local.
_SUBAGENT_RUN_KEYS = frozenset({
    "agent_id", "agent_type", "agent_type_source", "parent_agent", "parent_source", "parent_not_recorded_reason",
    "models", "models_source", "tokens", "tokens_source", "tokens_not_recorded_reason", "tokens_incomplete",
    "cost_usd", "cost_estimate", "cost_provenance", "cost_source", "cost_not_recorded_reason",
    "started_at", "ended_at", "times_source", "times_not_recorded_reason", "ended_not_recorded_reason",
    "tool_calls", "tool_calls_source", "result", "result_source", "result_reason",
})
_SUBAGENT_COST_KEYS = frozenset({"status", "subagents_usd", "session_usd", "subagents_priced", "subagents", "reason"})


def subagents_block(capture: object) -> dict | None:
    """``{agents, cost}``: what each subagent did, as capture recorded it
    (``adapters/claude_subagents.py``); None when none was recorded."""
    if not isinstance(capture, dict):
        return None
    runs = capture.get("subagent_runs")
    agents = [{k: v for k, v in run.items() if k in _SUBAGENT_RUN_KEYS}
              for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []
    if not agents:
        return None
    cost = capture.get("subagent_cost")
    return {"agents": agents[:16],
            "cost": {k: v for k, v in cost.items() if k in _SUBAGENT_COST_KEYS} if isinstance(cost, dict) else None}


def rejected_for_subagents(result: object) -> bool:
    """True when the Platform refused the envelope for ``receipt.subagents`` itself, as one
    that predates the block does (``Unrecognized key: "subagents"`` on ``receipt``)."""
    if getattr(result, "kind", None) != "rejected" or getattr(result, "code", None) != "invalid_payload":
        return False
    details = getattr(result, "details", None)
    for d in details if isinstance(details, list) else []:
        path, message = (d.get("path"), d.get("message")) if isinstance(d, dict) else (None, None)
        if isinstance(path, str) and (path == "receipt.subagents" or path.startswith("receipt.subagents.")):
            return True
        if path == "receipt" and isinstance(message, str) and '"subagents"' in message:
            return True
    return False


def build_envelope(entry: dict, index: int, *, core_version: str, multi_agent: bool = False) -> dict:
    """The v1 sync envelope for *entry*. ``source.receipt_schema_version`` is the
    record's own stamped version (``"unknown"`` for pre-stamping records, as
    Core itself labels them), never today's. The optional ``seal`` (seal v2)
    is added by the flush beside ``receipt``, never inside it
    (``sync/seal.py``)."""
    schema_version = entry.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        schema_version = None
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "source": {
            "product": SOURCE_PRODUCT,
            "version": core_version,
            "receipt_schema_version": schema_version,
        },
        "receipt": receipt_payload(entry, index, multi_agent=multi_agent),
    }


def payload_hash(receipt: dict) -> str:
    """``sha256:<hex>`` of the canonical JSON (sorted keys, compact) of *receipt*.

    Local bookkeeping only: it says whether the receipt this machine would
    send today is the one it sent before. The Platform computes its own
    hash over what it received; the two are not compared.
    """
    blob = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "QUIESCENT_SECONDS",
    "SHARD_SCHEMA_VERSION",
    "SOURCE_PRODUCT",
    "Eligibility",
    "build_envelope",
    "eligibility",
    "payload_hash",
    "receipt_payload",
    "rejected_for_subagents",
    "subagents_block",
]
