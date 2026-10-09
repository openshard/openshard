"""Cursor-reported usage for a Receipt that already exists: fetch, correlate, attest.

Cursor hooks and Grok Bot's OpenTelemetry export rarely carry a cost, and
Cursor hooks carry no tokens at all, so a Cursor / Grok Bot Receipt is usually
written with usage unknown. Cursor reports usage later, on two surfaces:

``GET /v1/agents/{id}/usage`` (Cloud Agents API, ``CURSOR_API_KEY``)
    Per-run ``inputTokens`` / ``outputTokens`` / ``cacheWriteTokens`` /
    ``cacheReadTokens`` / ``totalTokens``. No model, no cost. A run with no
    recorded usage yet reports **zeros in every field** and omits
    ``usageUuid``: such a run is ``pending``, never an observed zero. A run
    with ``usageUuid`` and zeros is a recorded zero.
``POST /teams/filtered-usage-events`` (Admin API, ``CURSOR_ADMIN_API_KEY``)
    Per-request events with ``model``, ``tokenUsage`` (with ``totalCents``,
    the model cost), ``chargedCents`` (what Cursor charged, including the
    Cursor Token Rate when it applies), ``conversationId`` and
    ``cloudAgentId``. Data is aggregated hourly.

Correlation is by identifier equality only, never by time proximity alone:

* the Receipt's key is a Cursor id a capture path *observed*: the
  conversation id Cursor's hooks deliver (``capture.session_id`` on a
  ``cursor_hooks`` record, which is the ``bc-...`` agent id for a Cloud
  Agent) or the ``cursor.conversation.id`` Cursor exported for Grok Bot. A
  self-reported Grok Bot ``conversation_id`` is the Bot's claim and is never
  used as a key;
* exactly one Receipt in the history may carry the key. Two (a resumed
  session's segments) is ambiguous, and nothing is recorded;
* an Admin usage event must carry the key itself; an event whose
  ``cloudAgentId`` contradicts the Receipt, or that falls more than an hour
  (Cursor's aggregation grain) outside the Receipt's capture window, makes
  the match ambiguous.

What is recorded is one ``usage_reconciliation`` attestation in
``.openshard/usage.jsonl`` naming the Receipt (``history/usage_evidence``
reads it). ``pending``, ``unavailable``, ``no_match`` and ``ambiguous`` are
reported to the caller and recorded nowhere. Re-running with the same answer
appends nothing. API keys are read from the environment, sent only as HTTP
basic auth to ``api.cursor.com`` and never stored or printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.usage_evidence import (
    ATTESTATION_VERSION,
    KIND_USAGE,
    OUTCOME_RECORDED,
    OUTCOME_UNCHANGED,
    SOURCE_PROVIDER,
    SOURCE_RUNTIME,
    STATUS_ESTIMATED,
    STATUS_RECONCILED,
    STATUS_UNKNOWN,
    SURFACE_CURSOR_ADMIN_EVENTS,
    SURFACE_CURSOR_AGENTS_API,
    empty_cost,
    empty_tokens,
    load_usage_attestations,
    make_tokens,
    model_id,
    price_tokens,
    record_usage_attestation,
    usage_attestations_for_entry,
    usage_from_record,
    usage_path,
)

ENV_API_KEY = "CURSOR_API_KEY"
ENV_ADMIN_API_KEY = "CURSOR_ADMIN_API_KEY"
API_BASE = "https://api.cursor.com"


OUTCOME_PENDING = "pending"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_AMBIGUOUS = "ambiguous"

KEY_CONVERSATION = "cursor_conversation_id"
KEY_CLOUD_AGENT = "cursor_cloud_agent_id"
KEY_SOURCE_HOOKS = "cursor_hooks"
KEY_SOURCE_OTEL = "cursor_otel"

SCOPE_AGENT = "cloud_agent_total"
SCOPE_RUN = "cloud_agent_run"
SCOPE_EVENTS = "conversation_events"

EVENT_WINDOW_SLACK_MS = 60 * 60 * 1000
MAX_EVENT_PAGES = 20
EVENT_PAGE_SIZE = 1000

_CLOUD_AGENT_RE = re.compile(r"^bc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RUN_ID_RE = re.compile(r"^run-[0-9A-Za-z-]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_RUNS = 500
_MAX_EVENTS = 20_000


# ---------------------------------------------------------------------------
# Which Cursor ids a Receipt carries
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def receipt_keys(entry: dict) -> dict[str, tuple[str, str]]:
    """``{key_kind: (value, key_source)}`` for the Cursor ids a capture path observed on *entry*."""
    capture = _as_dict(entry.get("capture"))
    sid = capture.get("session_id")
    if not isinstance(sid, str) or not _KEY_RE.match(sid):
        return {}
    executor = entry.get("executor")
    if executor == "cursor_hooks" and capture.get("source") == "cursor_hooks":
        keys = {KEY_CONVERSATION: (sid, KEY_SOURCE_HOOKS)}
        if _CLOUD_AGENT_RE.match(sid):
            keys[KEY_CLOUD_AGENT] = (sid, KEY_SOURCE_HOOKS)
        return keys
    if executor == "grok_bot_otel" and capture.get("observer") == "cursor_action_recording":
        return {KEY_CONVERSATION: (sid, KEY_SOURCE_OTEL)}
    return {}


@dataclass
class Match:
    entry: dict | None = None
    index: int | None = None
    key_kind: str | None = None
    key_value: str | None = None
    key_source: str | None = None
    refusal: str | None = None  # no_match | ambiguous
    detail: str = ""


def match_receipt(entries: list[dict], key_kind: str, key_value: str, *,
                  receipt_ref: str | None = None, run_id: str | None = None) -> Match:
    """The one Receipt carrying *key_value* as *key_kind*, or why there is none."""
    carriers = [
        (i, e) for i, e in enumerate(entries)
        if isinstance(e, dict) and receipt_keys(e).get(key_kind, ("",))[0] == key_value
    ]
    if run_id is not None:
        carriers = [(i, e) for i, e in carriers if stored_cursor_run_id(e) == run_id]
    if not carriers:
        return Match(refusal=OUTCOME_NO_MATCH, detail=f"no Receipt in this history carries {key_kind} {key_value}")
    if len(carriers) > 1:
        return Match(refusal=OUTCOME_AMBIGUOUS, detail=(
            f"{len(carriers)} Receipts carry {key_kind} {key_value} (a resumed session); "
            "Cursor's usage cannot be split between them"
        ))
    index, entry = carriers[0]
    if receipt_ref and receipt_ref not in (entry.get("receipt_id"), entry.get("shard_id"), entry.get("run_id")):
        return Match(refusal=OUTCOME_NO_MATCH, detail=f"Receipt {receipt_ref} does not carry {key_kind} {key_value}")
    if not isinstance(entry.get("receipt_id"), str) or not entry["receipt_id"]:
        return Match(refusal=OUTCOME_NO_MATCH, detail="the matching record has no receipt_id (written before v0.4.4)")
    value, source = receipt_keys(entry)[key_kind]
    return Match(entry=entry, index=index, key_kind=key_kind, key_value=value, key_source=source)


def stored_cursor_run_id(entry: dict) -> str | None:
    """The Cloud Agents run id this Receipt observed, if the capture stored one."""
    capture = _as_dict(entry.get("capture"))
    for key in ("cursor_run_id", "cursor_generation_id"):
        value = capture.get(key)
        if isinstance(value, str) and _RUN_ID_RE.match(value):
            return value
    return None


@dataclass
class AgentUsage:
    outcome: str  # recorded (usage known) | pending | unavailable
    tokens: dict[str, int] = field(default_factory=dict)
    scope: str = SCOPE_AGENT
    run_id: str | None = None
    runs: int = 0
    pending_runs: list[str] = field(default_factory=list)
    detail: str = ""


_AGENT_TOKEN_FIELDS = (
    ("input", "inputTokens"), ("output", "outputTokens"),
    ("cache_write", "cacheWriteTokens"), ("cache_read", "cacheReadTokens"),
)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _run_counts(usage: object) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}
    for key, wire in _AGENT_TOKEN_FIELDS + (("total", "totalTokens"),):
        value = _int(usage.get(wire))
        if value is None:
            return None
        out[key] = value
    if out["total"] != out["input"] + out["output"] + out["cache_write"] + out["cache_read"]:
        return None
    return out


def parse_agent_usage(body: object, *, run_id: str | None = None) -> AgentUsage:
    """Interpret a ``GET /v1/agents/{id}/usage`` response. Pure; never raises."""
    if not isinstance(body, dict) or not isinstance(body.get("runs"), list):
        return AgentUsage(OUTCOME_UNAVAILABLE, detail="the response has no runs list")
    runs: list[tuple[str, bool, dict[str, int]]] = []
    for item in body["runs"][:_MAX_RUNS]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return AgentUsage(OUTCOME_UNAVAILABLE, detail="a run in the response is malformed")
        counts = _run_counts(item.get("usage"))
        if counts is None:
            return AgentUsage(OUTCOME_UNAVAILABLE, detail=f"run {item['id']} has malformed usage")
        recorded = isinstance(item.get("usageUuid"), str) and bool(item["usageUuid"])
        runs.append((item["id"], recorded, counts))
    if run_id is not None:
        runs = [r for r in runs if r[0] == run_id]
        if not runs:
            return AgentUsage(OUTCOME_UNAVAILABLE, scope=SCOPE_RUN, run_id=run_id,
                              detail=f"run {run_id} is not in the response")
    scope = SCOPE_RUN if run_id is not None else SCOPE_AGENT
    if not runs:
        return AgentUsage(OUTCOME_PENDING, scope=scope, detail="the agent has no runs yet")
    pending = [rid for rid, recorded, _ in runs if not recorded]
    if pending:
        # Cursor reports zeros for a run with no recorded usage: not a measured zero.
        return AgentUsage(OUTCOME_PENDING, scope=scope, run_id=run_id, runs=len(runs), pending_runs=pending,
                          detail=f"Cursor has not recorded usage for {len(pending)} run(s) yet")
    totals = {key: sum(c[key] for _, _, c in runs) for key in ("input", "output", "cache_write", "cache_read", "total")}
    if run_id is None:
        reported = _run_counts(body.get("totalUsage"))
        if reported is not None and reported != totals:
            return AgentUsage(OUTCOME_UNAVAILABLE, scope=scope, runs=len(runs),
                              detail="totalUsage does not equal the sum of the runs")
    return AgentUsage(OUTCOME_RECORDED, tokens=totals, scope=scope, run_id=run_id, runs=len(runs))


# ---------------------------------------------------------------------------
# Admin API: POST /teams/filtered-usage-events
# ---------------------------------------------------------------------------


@dataclass
class UsageEvent:
    timestamp_ms: int | None
    conversation_id: str | None
    cloud_agent_id: str | None
    model: str | None
    chargeable: bool | None
    tokens: dict[str, int] | None
    model_cents: float | None
    charged_cents: float | None
    token_fee_cents: float | None


def _cents(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f >= 0 and f == f and f != float("inf") else None


def _event(raw: dict) -> UsageEvent:
    stamp = raw.get("timestamp")
    try:
        ts = int(stamp) if isinstance(stamp, (str, int)) and not isinstance(stamp, bool) else None
    except ValueError:
        ts = None
    tokens: dict[str, int] | None = None
    model_cents: float | None = None
    usage = raw.get("tokenUsage")
    if raw.get("isTokenBasedCall") is True and isinstance(usage, dict):
        counts = {key: _int(usage.get(wire)) for key, wire in _AGENT_TOKEN_FIELDS}
        if all(v is not None for v in counts.values()):
            tokens = {k: int(v) for k, v in counts.items() if v is not None}
        model_cents = _cents(usage.get("totalCents"))
    conv = raw.get("conversationId")
    agent = raw.get("cloudAgentId")
    return UsageEvent(
        timestamp_ms=ts,
        conversation_id=conv if isinstance(conv, str) and _KEY_RE.match(conv) else None,
        cloud_agent_id=agent if isinstance(agent, str) and _KEY_RE.match(agent) else None,
        model=model_id(raw.get("model")),
        chargeable=raw.get("isChargeable") if isinstance(raw.get("isChargeable"), bool) else None,
        tokens=tokens,
        model_cents=model_cents,
        charged_cents=_cents(raw.get("chargedCents")),
        token_fee_cents=_cents(raw.get("cursorTokenFee")),
    )


def parse_usage_events(body: object) -> list[UsageEvent] | None:
    """Events from one response, a list of page responses, or a bare event list. None when unreadable."""
    pages = body if isinstance(body, list) and all(isinstance(p, dict) and "usageEvents" in p for p in body) \
        else [body]
    events: list[UsageEvent] = []
    for page in pages:
        raw = page.get("usageEvents") if isinstance(page, dict) else page
        if not isinstance(raw, list):
            return None
        for item in raw:
            if isinstance(item, dict):
                events.append(_event(item))
            if len(events) >= _MAX_EVENTS:
                return events
    return events


def _iso_ms(stamp: object) -> int | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return int(then.timestamp() * 1000)


def receipt_window_ms(entry: dict) -> tuple[int | None, int | None]:
    capture = _as_dict(entry.get("capture"))
    start = _iso_ms(capture.get("started_at")) or _iso_ms(entry.get("timestamp"))
    end = _iso_ms(capture.get("last_activity_at")) or start
    return start, end


@dataclass
class EventMatch:
    outcome: str  # recorded | no_match | ambiguous
    events: list[UsageEvent] = field(default_factory=list)
    detail: str = ""


def select_events(events: list[UsageEvent], entry: dict) -> EventMatch:
    """The events that are this Receipt's usage, by id equality; ambiguity refuses the whole set."""
    keys = receipt_keys(entry)
    agent_id = keys.get(KEY_CLOUD_AGENT, (None,))[0]
    conv_id = keys.get(KEY_CONVERSATION, (None,))[0]
    chosen: list[UsageEvent] = []
    for ev in events:
        by_agent = agent_id is not None and ev.cloud_agent_id == agent_id
        by_conv = conv_id is not None and ev.conversation_id == conv_id
        if not (by_agent or by_conv):
            continue
        if agent_id is not None and ev.cloud_agent_id is not None and ev.cloud_agent_id != agent_id:
            return EventMatch(OUTCOME_AMBIGUOUS, detail="an event for this conversation names a different cloud agent")
        chosen.append(ev)
    if not chosen:
        return EventMatch(OUTCOME_NO_MATCH, detail="no usage event carries this Receipt's Cursor id")
    start, end = receipt_window_ms(entry)
    if start is None or end is None:
        return EventMatch(OUTCOME_AMBIGUOUS, detail="the Receipt has no capture window to bound the events")
    for ev in chosen:
        if ev.timestamp_ms is None or not (start - EVENT_WINDOW_SLACK_MS <= ev.timestamp_ms <= end + EVENT_WINDOW_SLACK_MS):
            return EventMatch(OUTCOME_AMBIGUOUS, detail=(
                "this conversation has Cursor usage outside the Receipt's capture window; "
                "it cannot be split honestly"
            ))
    return EventMatch(OUTCOME_RECORDED, events=chosen)


# ---------------------------------------------------------------------------
# Usage blocks and attestations
# ---------------------------------------------------------------------------


def agent_usage_block(usage: AgentUsage, entry: dict) -> dict[str, Any]:
    """Cloud Agents API tokens (no model, no cost); cost only as a dated list-rate estimate when one applies."""
    tokens = make_tokens(status=STATUS_RECONCILED, source=SOURCE_RUNTIME, surface=SURFACE_CURSOR_AGENTS_API,
                         complete=True, total=usage.tokens.get("total"),
                         **{k: usage.tokens.get(k) for k in ("input", "output", "cache_read", "cache_write")})
    record_model = usage_from_record(entry)["model"]
    cost = price_tokens(record_model.get("id"), tokens) or empty_cost()
    return {"tokens": tokens, "cost": cost, "model": {"id": None, "source": None, "models": []}}


def events_usage_block(events: list[UsageEvent]) -> dict[str, Any]:
    """Admin usage events summed: Cursor-reported tokens, model(s) and charged cost."""
    if not events:
        return {"tokens": empty_tokens(), "cost": empty_cost(), "model": {"id": None, "source": None, "models": []}}
    with_tokens = [ev.tokens for ev in events if ev.tokens is not None]
    counts = {k: sum(t[k] for t in with_tokens) for k in ("input", "output", "cache_read", "cache_write")} \
        if with_tokens else {}
    tokens = make_tokens(status=STATUS_RECONCILED, source=SOURCE_RUNTIME, surface=SURFACE_CURSOR_ADMIN_EVENTS,
                         complete=len(with_tokens) == len(events), **counts)
    if not with_tokens:
        tokens = make_tokens(status=STATUS_UNKNOWN, source=None, surface=None, complete=None)
    cost = empty_cost()
    charged = [ev.charged_cents for ev in events]
    if all(c is not None for c in charged):
        model_cents = [ev.model_cents for ev in events]
        fees = [ev.token_fee_cents for ev in events]
        cost.update(
            status=STATUS_ESTIMATED if any(ev.chargeable is False for ev in events) else STATUS_RECONCILED,
            source=SOURCE_PROVIDER if all(ev.chargeable is True for ev in events) else SOURCE_RUNTIME,
            surface=SURFACE_CURSOR_ADMIN_EVENTS, complete=True,
            usd=round(sum(c for c in charged if c is not None) / 100, 8),
            model_cost_usd=round(sum(m for m in model_cents if m is not None) / 100, 8)
            if all(m is not None for m in model_cents) else None,
            platform_fee_usd=round(sum(f for f in fees if f is not None) / 100, 8)
            if all(f is not None for f in fees) else None,
        )
    if cost.get("usd") is not None:
        if all(ev.chargeable is True for ev in events):
            cost["kind"] = "provider_billed"
        elif any(ev.chargeable is False for ev in events):
            cost["kind"] = "runtime_estimate"
    models = list(dict.fromkeys(ev.model for ev in events if ev.model))[:5]
    single = models[0] if len(models) == 1 and all(ev.model for ev in events) else None
    model = {"id": single, "source": SURFACE_CURSOR_ADMIN_EVENTS if models else None, "models": models}
    return {"tokens": tokens, "cost": cost, "model": model}


def _digest(receipt_id: str, correlation: dict, usage: dict) -> str:
    blob = json.dumps({"receipt_id": receipt_id, "correlation": correlation, "usage": usage},
                      sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_usage_attestation(
    entry: dict, usage: dict[str, Any], correlation: dict[str, Any], *, created_at: str,
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Reconcile: one call per surface
# ---------------------------------------------------------------------------


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
            "attestation_id": (self.attestation or {}).get("attestation_id") if self.outcome == OUTCOME_RECORDED
            else None,
            "usage": (self.attestation or {}).get("usage"),
            "correlation": (self.attestation or {}).get("correlation"),
        }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def reconcile_agent_usage(
    repo_root: Path, entries: list[dict], agent_id: str, body: object, *,
    run_id: str | None = None, receipt_ref: str | None = None, created_at: str | None = None,
) -> ReconcileResult:
    """Attach a Cloud Agents usage response for *agent_id* to the one Receipt that carries it."""
    if not _CLOUD_AGENT_RE.match(agent_id or ""):
        return ReconcileResult(OUTCOME_UNAVAILABLE, "not a Cursor cloud agent id (bc-<uuid>)")
    if run_id is not None and not _RUN_ID_RE.match(run_id):
        return ReconcileResult(OUTCOME_UNAVAILABLE, "not a Cursor run id (run-...)")
    match = match_receipt(entries, KEY_CLOUD_AGENT, agent_id, receipt_ref=receipt_ref, run_id=run_id)
    if match.entry is None:
        return ReconcileResult(match.refusal or OUTCOME_NO_MATCH, match.detail)
    rid = match.entry.get("receipt_id")
    if run_id is None:
        run_id = stored_cursor_run_id(match.entry)
    usage = parse_agent_usage(body, run_id=run_id)
    if usage.outcome != OUTCOME_RECORDED:
        return ReconcileResult(usage.outcome, usage.detail, receipt_id=rid)
    correlation = {
        "surface": SURFACE_CURSOR_AGENTS_API,
        "key": KEY_CLOUD_AGENT,
        "key_value": agent_id,
        "key_source": match.key_source,
        "scope": usage.scope,
        "run_id": usage.run_id,
        "runs": usage.runs,
    }
    att = build_usage_attestation(match.entry, agent_usage_block(usage, match.entry), correlation,
                                  created_at=created_at or _now())
    outcome = record_usage_attestation(repo_root, att)
    return ReconcileResult(outcome, "", receipt_id=rid, attestation=att)


def reconcile_usage_events(
    repo_root: Path, entries: list[dict], entry: dict, body: object, *, created_at: str | None = None,
) -> ReconcileResult:
    """Attach the Admin usage events that carry *entry*'s Cursor id to it."""
    keys = receipt_keys(entry)
    if not keys:
        return ReconcileResult(OUTCOME_NO_MATCH, "this Receipt carries no observed Cursor id to correlate on")
    kind = KEY_CLOUD_AGENT if KEY_CLOUD_AGENT in keys else KEY_CONVERSATION
    match = match_receipt(entries, kind, keys[kind][0])
    if match.entry is None:
        return ReconcileResult(match.refusal or OUTCOME_NO_MATCH, match.detail)
    if match.entry.get("receipt_id") != entry.get("receipt_id"):
        return ReconcileResult(OUTCOME_AMBIGUOUS, "another Receipt carries the same Cursor id")
    rid = entry.get("receipt_id")
    events = parse_usage_events(body)
    if events is None:
        return ReconcileResult(OUTCOME_UNAVAILABLE, "the response has no usageEvents list", receipt_id=rid)
    selected = select_events(events, entry)
    if selected.outcome != OUTCOME_RECORDED:
        return ReconcileResult(selected.outcome, selected.detail, receipt_id=rid)
    start, end = receipt_window_ms(entry)
    correlation = {
        "surface": SURFACE_CURSOR_ADMIN_EVENTS,
        "key": kind,
        "key_value": keys[kind][0],
        "key_source": keys[kind][1],
        "scope": SCOPE_EVENTS,
        "events": len(selected.events),
        "window_ms": [start, end],
        "window_slack_ms": EVENT_WINDOW_SLACK_MS,
    }
    att = build_usage_attestation(entry, events_usage_block(selected.events), correlation,
                                  created_at=created_at or _now())
    outcome = record_usage_attestation(repo_root, att)
    return ReconcileResult(outcome, "", receipt_id=rid, attestation=att)


# ---------------------------------------------------------------------------
# Live fetch (read-only)
# ---------------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, float], Any]


def _auth(api_key: str) -> str:
    return "Basic " + base64.b64encode(f"{api_key}:".encode()).decode("ascii")


def _call(request: urllib.request.Request, opener: Opener | None, timeout: float) -> tuple[object | None, str]:
    try:
        open_fn = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))  # noqa: S310 - fixed https host
        with open_fn(request, timeout) as resp:
            raw = resp.read(10_000_000)
    except urllib.error.HTTPError as exc:
        return None, f"Cursor answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return None, "Cursor's API could not be reached"
    try:
        return json.loads(raw), ""
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None, "Cursor returned an unreadable response"


def fetch_agent_usage(
    agent_id: str, api_key: str, *, run_id: str | None = None, opener: Opener | None = None, timeout: float = 30.0,
) -> tuple[object | None, str]:
    """``(body, "")`` from ``GET /v1/agents/{id}/usage``, or ``(None, reason)``. Never raises."""
    if not _CLOUD_AGENT_RE.match(agent_id or "") or (run_id is not None and not _RUN_ID_RE.match(run_id)):
        return None, "invalid agent or run id"
    url = f"{API_BASE}/v1/agents/{urllib.parse.quote(agent_id, safe='')}/usage"
    if run_id:
        url += "?" + urllib.parse.urlencode({"runId": run_id})
    req = urllib.request.Request(url, method="GET", headers={"Authorization": _auth(api_key),
                                                             "Accept": "application/json"})
    return _call(req, opener, timeout)


def fetch_usage_events(
    api_key: str, *, start_ms: int, end_ms: int, cloud_agent_id: str | None = None,
    opener: Opener | None = None, timeout: float = 30.0,
) -> tuple[list[dict] | None, str]:
    """Every page of ``POST /teams/filtered-usage-events`` for the window, or ``(None, reason)``. Never raises."""
    pages: list[dict] = []
    for page in range(1, MAX_EVENT_PAGES + 1):
        payload: dict[str, Any] = {"startDate": start_ms, "endDate": end_ms, "page": page,
                                   "pageSize": EVENT_PAGE_SIZE}
        if cloud_agent_id:
            payload["cloudAgentId"] = cloud_agent_id
        req = urllib.request.Request(
            f"{API_BASE}/teams/filtered-usage-events", method="POST", data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": _auth(api_key), "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        body, err = _call(req, opener, timeout)
        if body is None or not isinstance(body, dict):
            return None, err or "Cursor returned an unreadable response"
        pages.append(body)
        pagination = _as_dict(body.get("pagination"))
        if pagination.get("hasNextPage") is not True:
            return pages, ""
    return None, f"more than {MAX_EVENT_PAGES} pages of usage events in the window; narrow it"


AUTO_POLL_SECONDS = 300.0


def auto_reconcile_usage(
    repo_root: Path, entries: list[dict], entry: dict, *, env: dict | os._Environ,
) -> bool:
    """Poll supported Cursor usage off the hook path; False if no safe query exists.

    Provider credentials are explicit environment configuration. Refuse agent-wide
    totals without a captured run id, and reused session IDs that cannot be split.
    HTTP failures and pending exports leave existing usage untouched for a later poll.
    """
    keys = receipt_keys(entry)
    if not keys:
        return False
    agent_key = env.get(ENV_API_KEY, "").strip()
    admin_key = env.get(ENV_ADMIN_API_KEY, "").strip()
    agent_id = keys.get(KEY_CLOUD_AGENT, (None,))[0]
    run_id = stored_cursor_run_id(entry)
    queried = False
    if agent_key and agent_id and run_id:
        # Match before querying: ambiguous identity does not spend API quota.
        match = match_receipt(entries, KEY_CLOUD_AGENT, agent_id, receipt_ref=entry.get("receipt_id"), run_id=run_id)
        if match.entry is not None:
            queried = True
            body, _ = fetch_agent_usage(agent_id, agent_key, run_id=run_id, timeout=10.0)
            if body is not None:
                reconcile_agent_usage(repo_root, entries, agent_id, body,
                                      run_id=run_id, receipt_ref=entry.get("receipt_id"))
    if admin_key:
        key_kind = KEY_CLOUD_AGENT if agent_id else KEY_CONVERSATION
        value = keys[key_kind][0]
        match = match_receipt(entries, key_kind, value, receipt_ref=entry.get("receipt_id"))
        start, end = receipt_window_ms(entry)
        if match.entry is not None and start is not None and end is not None:
            queried = True
            pages, _ = fetch_usage_events(
                admin_key, start_ms=start - EVENT_WINDOW_SLACK_MS, end_ms=end + EVENT_WINDOW_SLACK_MS,
                cloud_agent_id=agent_id, timeout=10.0,
            )
            if pages is not None:
                reconcile_usage_events(repo_root, entries, entry, pages)
    return queried


__all__ = [
    "ENV_ADMIN_API_KEY",
    "ENV_API_KEY",
    "KEY_CLOUD_AGENT",
    "KEY_CONVERSATION",
    "KIND_USAGE",
    "OUTCOME_AMBIGUOUS",
    "OUTCOME_NO_MATCH",
    "OUTCOME_PENDING",
    "OUTCOME_RECORDED",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_UNCHANGED",
    "AgentUsage",
    "ReconcileResult",
    "UsageEvent",
    "agent_usage_block",
    "build_usage_attestation",
    "events_usage_block",
    "fetch_agent_usage",
    "fetch_usage_events",
    "load_usage_attestations",
    "match_receipt",
    "parse_agent_usage",
    "parse_usage_events",
    "receipt_keys",
    "receipt_window_ms",
    "reconcile_agent_usage",
    "reconcile_usage_events",
    "record_usage_attestation",
    "select_events",
    "stored_cursor_run_id",
    "usage_attestations_for_entry",
    "usage_path",
]
