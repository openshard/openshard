"""Reconcile OpenAI Codex App Server token usage onto an existing Receipt.

Codex App Server publishes thread/tokenUsage/updated notifications with
cumulative per-thread token counters. OpenShard accepts that evidence only
when threadId exactly equals the session id observed by one Codex hook
Receipt. Timing, task text, branch names and model names are never used to
guess a match.

The App Server counter is runtime usage, not a ChatGPT invoice. When the
matching Receipt already names one model with a dated official rate,
OpenShard may calculate an API-equivalent list-rate estimate. The estimate is
kept separate from provider/runtime-reported cost and is never called billed
spend.

Regular ChatGPT Chat is not a Codex thread merely because it edited code.
Without an exact Codex thread id this adapter records nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.usage_evidence import (
    ATTESTATION_VERSION,
    KIND_USAGE,
    OUTCOME_RECORDED,
    SOURCE_RUNTIME,
    STATUS_RECONCILED,
    SURFACE_OPENAI_CODEX_APP_SERVER,
    empty_cost,
    make_tokens,
    price_tokens,
    record_usage_attestation,
    usage_from_record,
)

OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_AMBIGUOUS = "ambiguous"

KEY_CODEX_THREAD = "codex_thread_id"
SCOPE_THREAD = "thread_total"

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def receipt_thread_id(entry: dict) -> str | None:
    """The Codex session/thread id OpenShard observed on this Receipt."""
    if entry.get("executor") != "codex_hooks":
        return None
    capture = entry.get("capture")
    if not isinstance(capture, dict) or capture.get("source") != "codex_hooks":
        return None
    sid = capture.get("session_id")
    return sid if isinstance(sid, str) and _KEY_RE.fullmatch(sid) else None


@dataclass
class AppServerUsage:
    outcome: str
    thread_id: str | None = None
    turn_id: str | None = None
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    total: int | None = None
    detail: str = ""


def parse_app_server_notification(body: object) -> AppServerUsage:
    """Parse one official thread/tokenUsage/updated notification."""
    if not isinstance(body, dict) or body.get("method") != "thread/tokenUsage/updated":
        return AppServerUsage(OUTCOME_UNAVAILABLE, detail="not a thread/tokenUsage/updated notification")
    params = body.get("params")
    if not isinstance(params, dict):
        return AppServerUsage(OUTCOME_UNAVAILABLE, detail="notification has no params object")
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    if not isinstance(thread_id, str) or not _KEY_RE.fullmatch(thread_id):
        return AppServerUsage(OUTCOME_UNAVAILABLE, detail="notification has no safe threadId")
    if turn_id is not None and (not isinstance(turn_id, str) or not _KEY_RE.fullmatch(turn_id)):
        return AppServerUsage(OUTCOME_UNAVAILABLE, detail="notification has an invalid turnId")

    token_usage = params.get("tokenUsage")
    total = token_usage.get("total") if isinstance(token_usage, dict) else None
    if not isinstance(total, dict):
        return AppServerUsage(OUTCOME_UNAVAILABLE, thread_id=thread_id, detail="notification has no cumulative token total")

    input_total = _count(total.get("inputTokens"))
    cached = _count(total.get("cachedInputTokens"))
    output = _count(total.get("outputTokens"))
    reported_total = _count(total.get("totalTokens"))
    reasoning = _count(total.get("reasoningOutputTokens"))
    if None in (input_total, cached, output, reported_total, reasoning):
        return AppServerUsage(OUTCOME_UNAVAILABLE, thread_id=thread_id, detail="token counters are missing or malformed")
    assert input_total is not None and cached is not None and output is not None and reported_total is not None
    if cached > input_total:
        return AppServerUsage(OUTCOME_UNAVAILABLE, thread_id=thread_id, detail="cached input exceeds total input")
    # Codex's reasoning-output count is a subset of output, not an additional
    # billable bucket. Its total therefore equals input + output.
    if reported_total != input_total + output:
        return AppServerUsage(OUTCOME_UNAVAILABLE, thread_id=thread_id, detail="totalTokens does not equal inputTokens + outputTokens")

    return AppServerUsage(
        OUTCOME_RECORDED,
        thread_id=thread_id,
        turn_id=turn_id,
        input=input_total - cached,
        output=output,
        cache_read=cached,
        total=reported_total,
    )


@dataclass
class Match:
    entry: dict | None = None
    refusal: str | None = None
    detail: str = ""


def match_receipt(entries: list[dict], thread_id: str, *, receipt_ref: str | None = None) -> Match:
    """Match by exact Codex thread/session id only."""
    carriers = [entry for entry in entries if isinstance(entry, dict) and receipt_thread_id(entry) == thread_id]
    if not carriers:
        return Match(refusal=OUTCOME_NO_MATCH, detail=f"no Codex Receipt carries thread id {thread_id}")
    if len(carriers) != 1:
        return Match(
            refusal=OUTCOME_AMBIGUOUS,
            detail=f"{len(carriers)} Receipts carry Codex thread id {thread_id}; usage cannot be split honestly",
        )
    entry = carriers[0]
    if receipt_ref and receipt_ref not in (entry.get("receipt_id"), entry.get("shard_id"), entry.get("run_id")):
        return Match(refusal=OUTCOME_NO_MATCH, detail=f"Receipt {receipt_ref} does not carry Codex thread id {thread_id}")
    if not isinstance(entry.get("receipt_id"), str) or not entry.get("receipt_id"):
        return Match(refusal=OUTCOME_NO_MATCH, detail="matching Codex record has no receipt_id")
    return Match(entry=entry)


def usage_block(observed: AppServerUsage, entry: dict) -> dict[str, Any]:
    """Runtime counters plus an honest dated list-rate estimate when possible."""
    tokens = make_tokens(
        status=STATUS_RECONCILED,
        source=SOURCE_RUNTIME,
        surface=SURFACE_OPENAI_CODEX_APP_SERVER,
        complete=True,
        total=observed.total,
        input=observed.input,
        output=observed.output,
        cache_read=observed.cache_read,
        # The current App Server notification exposes no separate cache-write
        # counter, matching the cumulative Codex transcript counter Core
        # already consumes. Reasoning is included in outputTokens.
        cache_write=0,
    )
    record_model = usage_from_record(entry)["model"]
    cost = price_tokens(record_model.get("id"), tokens) or empty_cost()
    return {
        "tokens": tokens,
        "cost": cost,
        # The notification itself does not name the model; preserve the
        # Receipt's existing model rather than promoting it as new evidence.
        "model": {"id": None, "source": None, "models": []},
    }


def _digest(receipt_id: str, correlation: dict, usage: dict) -> str:
    blob = json.dumps(
        {"receipt_id": receipt_id, "correlation": correlation, "usage": usage},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_attestation(entry: dict, usage: dict[str, Any], correlation: dict[str, Any], *, created_at: str) -> dict[str, Any]:
    receipt_id = str(entry.get("receipt_id"))
    return {
        "version": ATTESTATION_VERSION,
        "attestation_id": f"uat_{uuid.uuid4().hex}",
        "kind": KIND_USAGE,
        "created_at": created_at,
        "receipt_id": receipt_id,
        "run_id": entry.get("run_id") if isinstance(entry.get("run_id"), str) else None,
        "shard_id": entry.get("shard_id") if isinstance(entry.get("shard_id"), str) else None,
        "executor": entry.get("executor") if isinstance(entry.get("executor"), str) else None,
        "correlation": correlation,
        "usage": usage,
        "digest": _digest(receipt_id, correlation, usage),
    }


@dataclass
class ReconcileResult:
    outcome: str
    detail: str = ""
    receipt_id: str | None = None
    attestation: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "receipt_id": self.receipt_id,
            "attestation_id": (self.attestation or {}).get("attestation_id")
            if self.outcome == OUTCOME_RECORDED
            else None,
            "usage": (self.attestation or {}).get("usage"),
            "correlation": (self.attestation or {}).get("correlation"),
        }


def reconcile_app_server_usage(
    repo_root: Path,
    entries: list[dict],
    body: object,
    *,
    receipt_ref: str | None = None,
    created_at: str | None = None,
) -> ReconcileResult:
    """Attach cumulative Codex App Server usage to the one exact thread Receipt."""
    observed = parse_app_server_notification(body)
    if observed.outcome != OUTCOME_RECORDED or observed.thread_id is None:
        return ReconcileResult(observed.outcome, observed.detail)
    match = match_receipt(entries, observed.thread_id, receipt_ref=receipt_ref)
    if match.entry is None:
        return ReconcileResult(match.refusal or OUTCOME_NO_MATCH, match.detail)
    rid = match.entry["receipt_id"]
    correlation = {
        "surface": SURFACE_OPENAI_CODEX_APP_SERVER,
        "key": KEY_CODEX_THREAD,
        "key_value": observed.thread_id,
        "scope": SCOPE_THREAD,
    }
    att = build_attestation(
        match.entry,
        usage_block(observed, match.entry),
        correlation,
        created_at=created_at or _now(),
    )
    outcome = record_usage_attestation(repo_root, att)
    return ReconcileResult(outcome, "", receipt_id=rid, attestation=att)


__all__ = [
    "KEY_CODEX_THREAD",
    "OUTCOME_AMBIGUOUS",
    "OUTCOME_NO_MATCH",
    "OUTCOME_UNAVAILABLE",
    "SCOPE_THREAD",
    "AppServerUsage",
    "ReconcileResult",
    "build_attestation",
    "match_receipt",
    "parse_app_server_notification",
    "receipt_thread_id",
    "reconcile_app_server_usage",
    "usage_block",
]
