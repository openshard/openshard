"""Usage and cost evidence for a Receipt: which tokens and dollars, who reported them, how sure.

One agent/provider-neutral block, built two ways and merged at read time:

* :func:`usage_from_record` reads what the stored record already carries
  (``prompt_tokens`` ... ``tokens_provenance``, ``estimated_cost`` /
  ``cost_provenance``, retry attempts). Nothing is inferred: no token
  provenance means tokens are unknown, never 0.
* A *usage attestation* (``.openshard/usage.jsonl``, written by
  ``adapters/cursor_usage``) is usage a runtime reported after the Receipt was
  written, matched to it by a strong identifier. It names the Receipt; the
  Receipt itself is never modified, so nothing that was synced or hashed
  changes underneath it.

:func:`effective_usage` combines them per dimension (tokens, cost, model):
the strongest source wins; at equal strength what the record observed at the
time stays. Each dimension carries its own ``status`` and ``source``:

``status``
    ``observed`` (reported for this execution and captured with it),
    ``reconciled`` (reported later and matched by a strong id), ``estimated``
    (a calculation or an agent's own estimate), ``pending`` (the surface was
    asked and has nothing recorded yet) or ``unknown``.
``source``
    who produced the figure: ``provider_reported``, ``runtime_reported``,
    ``vendor_telemetry``, ``imported_transcript``, ``agent_reported`` or
    ``openshard_calculated``. ``None`` when the record never said.

``tokens.total`` counts every token the source counted, cache reads and
writes included (the convention Cursor's usage APIs use); the record's own
``total_tokens`` (input + output) is not reused for it. ``agent`` and
``model`` are separate facts: an agent name never implies a model.

When OpenShard calculates a cost, the block carries the dated list rate it
used (``cost.rate``). A reconciled attestation stores that rate, so a later
rate-card change never rewrites an old Receipt's figure.

Pure, never raises.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from openshard.models.pricing import COST_PROVENANCE_OFFICIAL_RATE

USAGE_VERSION = 1

STATUS_OBSERVED = "observed"
STATUS_RECONCILED = "reconciled"
STATUS_ESTIMATED = "estimated"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"
STATUSES: frozenset[str] = frozenset(
    {STATUS_OBSERVED, STATUS_RECONCILED, STATUS_ESTIMATED, STATUS_PENDING, STATUS_UNKNOWN}
)

SOURCE_PROVIDER = "provider_reported"
SOURCE_RUNTIME = "runtime_reported"
SOURCE_VENDOR_TELEMETRY = "vendor_telemetry"
SOURCE_IMPORTED_TRANSCRIPT = "imported_transcript"
SOURCE_AGENT = "agent_reported"
SOURCE_OPENSHARD = "openshard_calculated"
SOURCES: frozenset[str] = frozenset({
    SOURCE_PROVIDER, SOURCE_RUNTIME, SOURCE_VENDOR_TELEMETRY,
    SOURCE_IMPORTED_TRANSCRIPT, SOURCE_AGENT, SOURCE_OPENSHARD,
})

SURFACE_RECORD = "receipt_record"
SURFACE_CURSOR_AGENTS_API = "cursor_cloud_agents_api"
SURFACE_CURSOR_ADMIN_EVENTS = "cursor_admin_usage_events"
SURFACES: frozenset[str] = frozenset({SURFACE_RECORD, SURFACE_CURSOR_AGENTS_API, SURFACE_CURSOR_ADMIN_EVENTS})

TOKEN_KEYS: tuple[str, ...] = ("input", "output", "cache_read", "cache_write", "reasoning", "other")

# Strength of a reported figure, per dimension. A direct provider response is
# strongest; a runtime's own usage/billing API next; vendor telemetry and a
# transcript read later below that; an agent's word and OpenShard's own
# arithmetic last. Equal strength never displaces what the record observed.
_TOKEN_RANK: dict[str | None, int] = {
    SOURCE_PROVIDER: 5, SOURCE_RUNTIME: 4, SOURCE_VENDOR_TELEMETRY: 3,
    SOURCE_IMPORTED_TRANSCRIPT: 3, SOURCE_AGENT: 1, None: 0,
}
_COST_RANK: dict[str | None, int] = {
    SOURCE_PROVIDER: 5, SOURCE_RUNTIME: 4, SOURCE_VENDOR_TELEMETRY: 3,
    SOURCE_AGENT: 2, SOURCE_OPENSHARD: 1, None: 0,
}

_TOKEN_PROVENANCE_SOURCES: dict[str, str] = {
    "provider_reported": SOURCE_PROVIDER,
    "vendor_telemetry": SOURCE_VENDOR_TELEMETRY,
    "imported_transcript": SOURCE_IMPORTED_TRANSCRIPT,
    "agent_reported": SOURCE_AGENT,
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/@()+-]{0,119}$")
_NOT_A_MODEL = frozenset({"unknown", "not recorded", "auto", "default", ""})
_MAX_MODELS = 5
_MAX_USD = 1_000_000.0
_ATTESTATION_ID_RE = re.compile(r"^uat_[0-9a-f]{32}$")

USAGE_FILENAME = "usage.jsonl"
KIND_USAGE = "usage_reconciliation"
ATTESTATION_VERSION = 1
OUTCOME_RECORDED = "recorded"
OUTCOME_UNCHANGED = "unchanged"



def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usd(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) and 0 <= f <= _MAX_USD else None


def _token(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def model_id(value: object) -> str | None:
    """A recorded model id, or None for absent / placeholder values. Never derived from an agent name."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower() in _NOT_A_MODEL or not _MODEL_RE.match(text):
        return None
    return text


def empty_tokens(status: str = STATUS_UNKNOWN) -> dict[str, Any]:
    return {"status": status, "source": None, "surface": None, **dict.fromkeys(TOKEN_KEYS), "total": None,
            "complete": None}


def empty_cost(status: str = STATUS_UNKNOWN) -> dict[str, Any]:
    return {"status": status, "source": None, "surface": None, "usd": None,
            "model_cost_usd": None, "platform_fee_usd": None, "complete": None, "rate": None}


def make_tokens(
    *, status: str, source: str | None, surface: str | None, complete: bool | None = True,
    total: int | None = None, **counts: object,
) -> dict[str, Any]:
    """A tokens dimension. ``total`` defaults to the sum of the counts that are known."""
    block = empty_tokens(status)
    block.update(source=source, surface=surface, complete=complete)
    for key in TOKEN_KEYS:
        block[key] = _count(counts.get(key))
    known = [block[k] for k in TOKEN_KEYS if block[k] is not None]
    block["total"] = _count(total) if total is not None else (sum(known) if known else None)
    return block


def rate_snapshot(rate: Any) -> dict[str, Any] | None:
    """The dated list rate a calculation used, stored so history keeps its own economics."""
    if rate is None:
        return None
    return {
        "provider": getattr(rate, "provider", None),
        "model_id": getattr(rate, "model_id", None),
        "pricing_version": getattr(rate, "as_of", None),
        "source": getattr(rate, "source", None),
        "input_per_mtok": getattr(rate, "input_per_mtok", None),
        "output_per_mtok": getattr(rate, "output_per_mtok", None),
        "cache_read_per_mtok": getattr(rate, "cached_input_per_mtok", None),
        "cache_write_per_mtok": getattr(rate, "cache_write_per_mtok", None),
    }


def _clean_rate(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("provider", "model_id", "pricing_version", "source"):
        raw = value.get(key)
        out[key] = raw[:200] if isinstance(raw, str) else None
    for key in ("input_per_mtok", "output_per_mtok", "cache_read_per_mtok", "cache_write_per_mtok"):
        out[key] = _usd(value.get(key))
    if out["model_id"] is None or out["pricing_version"] is None:
        return None
    return out


def price_tokens(model: str | None, tokens: dict[str, Any]) -> dict[str, Any] | None:
    """OpenShard's list-rate calculation for *tokens* on *model*, with the rate it used; None when it cannot.

    Reasoning or other tokens without a published rate, an unknown model, or
    a missing cache rate make the cost unknown rather than understated.
    """
    from openshard.models.pricing import estimate_usage_cost, official_rate

    if tokens.get("status") not in (STATUS_OBSERVED, STATUS_RECONCILED) or tokens.get("complete") is False:
        return None
    if tokens.get("reasoning") or tokens.get("other"):
        return None
    rate = official_rate(model)
    estimate = estimate_usage_cost(
        model,
        input_tokens=tokens.get("input") or 0,
        output_tokens=tokens.get("output") or 0,
        cache_read_tokens=tokens.get("cache_read") or 0,
        cache_write_tokens=tokens.get("cache_write") or 0,
    )
    if rate is None or estimate is None:
        return None
    cost = empty_cost(STATUS_ESTIMATED)
    cost.update(source=SOURCE_OPENSHARD, surface=tokens.get("surface"), usd=estimate.usd, complete=True,
                rate=rate_snapshot(rate))
    return cost


# ---------------------------------------------------------------------------
# What the stored record says
# ---------------------------------------------------------------------------


def _agent(entry: dict) -> str | None:
    capture = _as_dict(entry.get("capture"))
    agent = capture.get("agent")
    if isinstance(agent, str) and agent:
        return agent[:60]
    executor = entry.get("executor")
    return executor[:60] if isinstance(executor, str) and executor else None


def _record_model(entry: dict) -> dict[str, Any]:
    from openshard.models.pricing import single_pricing_model

    capture = _as_dict(entry.get("capture"))
    seen = [m for m in (model_id(x) for x in (capture.get("models_seen") or [])) if m]
    single = model_id(single_pricing_model(entry))
    models = list(dict.fromkeys(seen))[:_MAX_MODELS] or ([single] if single else [])
    if not models:
        return {"id": None, "source": None, "models": []}
    source = capture.get("model_source")
    if not isinstance(source, str) or not source or source == "not_captured":
        source = SURFACE_RECORD
    return {"id": single, "source": source[:60], "models": models}


def _record_tokens(entry: dict) -> dict[str, Any]:
    from openshard.history.run_cost import stored_retry_attempts

    provenance = entry.get("tokens_provenance")
    if not isinstance(provenance, str) or not provenance:
        return empty_tokens()
    source = _TOKEN_PROVENANCE_SOURCES.get(provenance)
    if source is None:
        return empty_tokens()
    counts = {
        "input": _count(entry.get("prompt_tokens")),
        "output": _count(entry.get("completion_tokens")),
        "cache_read": _count(entry.get("cache_read_tokens")),
        "cache_write": _count(entry.get("cache_creation_tokens")),
    }
    if all(v is None for v in counts.values()):
        return empty_tokens()
    complete = True
    capture = _as_dict(entry.get("capture"))
    if capture.get("tokens_incomplete_reason"):
        complete = False
    if entry.get("retry_triggered") is True:
        attempts = stored_retry_attempts(entry)
        if attempts and all(a.get("prompt_tokens") is not None and a.get("completion_tokens") is not None
                            for a in attempts):
            counts["input"] = (counts["input"] or 0) + sum(a["prompt_tokens"] for a in attempts)
            counts["output"] = (counts["output"] or 0) + sum(a["completion_tokens"] for a in attempts)
        else:
            complete = False
    return make_tokens(status=STATUS_OBSERVED, source=source, surface=SURFACE_RECORD, complete=complete, **counts)


def _record_cost(entry: dict, model: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    from openshard.history.run_cost import run_total_cost

    provenance = entry.get("cost_provenance") if isinstance(entry.get("cost_provenance"), str) else None
    usd, complete = run_total_cost(entry)
    usd = _usd(usd)
    if usd is None:
        stage_costs = [_usd(s.get("cost")) for s in (entry.get("stage_runs") or []) if isinstance(s, dict)]
        known = [c for c in stage_costs if c is not None]
        if known:
            usd, complete = sum(known), len(known) == len(stage_costs)
    if usd is None:
        capture = _as_dict(entry.get("capture"))
        if tokens["status"] != STATUS_UNKNOWN and not isinstance(capture.get("usage_by_model"), dict):
            priced = price_tokens(model.get("id"), tokens)
            if priced is not None:
                return priced
        return empty_cost()
    cost = empty_cost()
    cost.update(usd=usd, surface=SURFACE_RECORD, complete=bool(complete))
    if provenance == COST_PROVENANCE_OFFICIAL_RATE:
        cost.update(status=STATUS_ESTIMATED, source=SOURCE_OPENSHARD)
    elif provenance in (SOURCE_PROVIDER, SOURCE_VENDOR_TELEMETRY):
        cost.update(status=STATUS_OBSERVED, source=provenance)
    elif provenance == SOURCE_AGENT:
        # An agent's own running cost (Claude Code's status line) is its estimate.
        cost.update(status=STATUS_ESTIMATED, source=SOURCE_AGENT)
    else:
        # Origin not recorded on the record: shown as an estimate, never as billed.
        cost.update(status=STATUS_ESTIMATED, source=None)
    return cost


def usage_from_record(entry: dict) -> dict[str, Any]:
    """The usage block the stored record supports on its own."""
    try:
        model = _record_model(entry)
        tokens = _record_tokens(entry)
        cost = _record_cost(entry, model, tokens)
        return {"version": USAGE_VERSION, "agent": _agent(entry), "model": model, "tokens": tokens,
                "cost": cost, "reconciled_by": []}
    except Exception:
        return {"version": USAGE_VERSION, "agent": None, "model": {"id": None, "source": None, "models": []},
                "tokens": empty_tokens(), "cost": empty_cost(), "reconciled_by": []}


# ---------------------------------------------------------------------------
# Attestations: usage reported later, matched by a strong id
# ---------------------------------------------------------------------------


def parse_usage_block(value: object) -> dict[str, Any] | None:
    """Re-validate a stored usage block (tokens / cost / model). None when unusable."""
    if not isinstance(value, dict):
        return None
    raw_tokens = _as_dict(value.get("tokens"))
    raw_cost = _as_dict(value.get("cost"))
    raw_model = _as_dict(value.get("model"))
    tokens = make_tokens(
        status=_token(raw_tokens.get("status"), STATUSES) or STATUS_UNKNOWN,
        source=_token(raw_tokens.get("source"), SOURCES),
        surface=_token(raw_tokens.get("surface"), SURFACES),
        complete=raw_tokens.get("complete") if isinstance(raw_tokens.get("complete"), bool) else None,
        total=raw_tokens.get("total") if _count(raw_tokens.get("total")) is not None else None,
        **{k: raw_tokens.get(k) for k in TOKEN_KEYS},
    )
    if tokens["total"] is None and tokens["status"] != STATUS_PENDING:
        tokens["status"] = STATUS_UNKNOWN
    cost = empty_cost(_token(raw_cost.get("status"), STATUSES) or STATUS_UNKNOWN)
    cost.update(
        source=_token(raw_cost.get("source"), SOURCES),
        surface=_token(raw_cost.get("surface"), SURFACES),
        usd=_usd(raw_cost.get("usd")),
        model_cost_usd=_usd(raw_cost.get("model_cost_usd")),
        platform_fee_usd=_usd(raw_cost.get("platform_fee_usd")),
        complete=raw_cost.get("complete") if isinstance(raw_cost.get("complete"), bool) else None,
        rate=_clean_rate(raw_cost.get("rate")),
    )
    if cost["usd"] is None:
        cost.update(status=STATUS_UNKNOWN if cost["status"] != STATUS_PENDING else STATUS_PENDING)
    if cost["source"] == SOURCE_OPENSHARD and cost["rate"] is None:
        cost = empty_cost()  # a calculation without the rate it used cannot be shown honestly
    if cost["source"] == SOURCE_OPENSHARD and cost["status"] != STATUS_UNKNOWN:
        cost["status"] = STATUS_ESTIMATED
    models = [m for m in (model_id(x) for x in (raw_model.get("models") or [])) if m][:_MAX_MODELS]
    raw_source = raw_model.get("source")
    model: dict[str, Any] = {
        "id": model_id(raw_model.get("id")),
        "source": raw_source[:60] if isinstance(raw_source, str) else None,
        "models": models,
    }
    if model["id"] is None and not models:
        model["source"] = None
    return {"tokens": tokens, "cost": cost, "model": model}


def _replaces(candidate: dict[str, Any], current: dict[str, Any], ranks: dict[str | None, int]) -> bool:
    if candidate["status"] in (STATUS_UNKNOWN, STATUS_PENDING):
        return False
    if current["status"] in (STATUS_UNKNOWN, STATUS_PENDING):
        return True
    cand_rank, cur_rank = ranks.get(candidate["source"], 0), ranks.get(current["source"], 0)
    if cand_rank != cur_rank:
        return cand_rank > cur_rank
    if current["status"] == STATUS_RECONCILED:
        return True  # a newer reconciliation of equal strength supersedes an older one
    return current.get("complete") is False and candidate.get("complete") is True


def effective_usage(entry: dict, attestations: list[dict] | None = None) -> dict[str, Any]:
    """The record's usage, strengthened by usage attestations that name it (oldest first)."""
    block = usage_from_record(entry)
    named = usage_attestations_for_entry(entry, attestations or [])
    for item in named:
        try:
            parsed = parse_usage_block(item.get("usage")) if isinstance(item, dict) else None
            if parsed is None:
                continue
            used = False
            if _replaces(parsed["tokens"], block["tokens"], _TOKEN_RANK):
                block["tokens"] = parsed["tokens"]
                used = True
            if _replaces(parsed["cost"], block["cost"], _COST_RANK):
                block["cost"] = parsed["cost"]
                used = True
            if parsed["model"]["id"] and (block["model"]["id"] is None or used):
                block["model"] = parsed["model"]
                used = True
            if used:
                block["reconciled_by"].append({
                    "attestation_id": item.get("attestation_id") if isinstance(item.get("attestation_id"), str)
                    and _ATTESTATION_ID_RE.match(item["attestation_id"]) else None,
                    "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
                    "surface": _token((item.get("correlation") or {}).get("surface"), SURFACES),
                })
        except Exception:
            continue
    return block


# ---------------------------------------------------------------------------
# Storage: ``.openshard/usage.jsonl``, append-only
# ---------------------------------------------------------------------------

def usage_path(repo_root: Path) -> Path:
    return Path(repo_root) / ".openshard" / USAGE_FILENAME


def load_usage_attestations(history_dir: Path) -> list[dict]:
    """Every well-formed usage attestation next to ``runs.jsonl``. Never raises."""
    path = Path(history_dir) / USAGE_FILENAME
    out: list[dict] = []
    try:
        if not path.is_file():
            return out
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(item, dict) and item.get("kind") == KIND_USAGE and isinstance(item.get("receipt_id"), str):
                    out.append(item)
    except OSError:
        return out
    return out


def usage_attestations_for_entry(entry: dict, attestations: list[dict]) -> list[dict]:
    """The attestations naming *entry*'s ``receipt_id``, oldest first. Never another Receipt's."""
    rid = entry.get("receipt_id")
    if not isinstance(rid, str) or not rid:
        return []
    return [a for a in attestations if isinstance(a, dict) and a.get("receipt_id") == rid]


def record_usage_attestation(repo_root: Path, attestation: dict) -> str:
    """Append *attestation* unless the newest one for the Receipt says exactly the same. Returns the outcome."""
    from openshard.history.jsonl_store import append_jsonl

    existing = usage_attestations_for_entry(
        {"receipt_id": attestation.get("receipt_id")}, load_usage_attestations(usage_path(repo_root).parent)
    )
    if existing and existing[-1].get("digest") == attestation.get("digest"):
        return OUTCOME_UNCHANGED
    append_jsonl(usage_path(repo_root), attestation)
    return OUTCOME_RECORDED


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_RUNTIME_LABEL = {SURFACE_CURSOR_AGENTS_API: "Cursor", SURFACE_CURSOR_ADMIN_EVENTS: "Cursor"}


def _source_label(source: str | None, surface: str | None) -> str:
    if source == SOURCE_RUNTIME:
        return f"{_RUNTIME_LABEL.get(surface or '', 'runtime')}-reported"
    return {
        SOURCE_PROVIDER: "provider-reported",
        SOURCE_VENDOR_TELEMETRY: "vendor telemetry",
        SOURCE_IMPORTED_TRANSCRIPT: "from transcript",
        SOURCE_AGENT: "agent-reported",
        SOURCE_OPENSHARD: "estimated",
    }.get(source or "", "origin not recorded")


def _usd_text(usd: float) -> str:
    return f"${usd:.2f}" if usd >= 0.01 or usd == 0 else f"${usd:.4f}"


def usage_line(block: dict[str, Any] | None) -> str:
    """One line for the Receipt, e.g. ``42,183 tokens · $0.31 · Cursor-reported``."""
    if not isinstance(block, dict):
        return "Usage unavailable from this execution surface"
    tokens = block.get("tokens") or {}
    cost = block.get("cost") or {}
    t_known = tokens.get("status") not in (STATUS_UNKNOWN, STATUS_PENDING, None) and tokens.get("total") is not None
    c_known = cost.get("status") not in (STATUS_UNKNOWN, STATUS_PENDING, None) and cost.get("usd") is not None
    if not t_known and not c_known:
        if tokens.get("status") == STATUS_PENDING or cost.get("status") == STATUS_PENDING:
            return "Usage not recorded yet by the execution surface"
        return "Usage unavailable from this execution surface"
    parts: list[str] = []
    if t_known:
        prefix = "at least " if tokens.get("complete") is False else ""
        parts.append(f"{prefix}{tokens['total']:,} tokens")
    else:
        parts.append("tokens unknown")
    if c_known:
        approx = "~" if cost.get("status") == STATUS_ESTIMATED else ""
        prefix = "at least " if cost.get("complete") is False else ""
        parts.append(f"{prefix}{approx}{_usd_text(cost['usd'])}")
    else:
        parts.append("cost unknown")
    t_label = _source_label(tokens.get("source"), tokens.get("surface")) if t_known else None
    c_label = _source_label(cost.get("source"), cost.get("surface")) if c_known else None
    if t_label and c_label and t_label != c_label:
        parts.append(f"tokens {t_label}, cost {c_label}")
    else:
        parts.append(t_label or c_label or "")
    if any(d.get("status") == STATUS_RECONCILED for d in (tokens, cost)):
        parts[-1] += " (reconciled)"
    return " · ".join(p for p in parts if p)


__all__ = [
    "ATTESTATION_VERSION",
    "KIND_USAGE",
    "SOURCES",
    "SOURCE_AGENT",
    "SOURCE_IMPORTED_TRANSCRIPT",
    "SOURCE_OPENSHARD",
    "SOURCE_PROVIDER",
    "SOURCE_RUNTIME",
    "SOURCE_VENDOR_TELEMETRY",
    "STATUSES",
    "STATUS_ESTIMATED",
    "STATUS_OBSERVED",
    "STATUS_PENDING",
    "STATUS_RECONCILED",
    "STATUS_UNKNOWN",
    "SURFACES",
    "SURFACE_CURSOR_ADMIN_EVENTS",
    "SURFACE_CURSOR_AGENTS_API",
    "SURFACE_RECORD",
    "TOKEN_KEYS",
    "USAGE_VERSION",
    "effective_usage",
    "load_usage_attestations",
    "record_usage_attestation",
    "usage_attestations_for_entry",
    "usage_path",
    "empty_cost",
    "empty_tokens",
    "make_tokens",
    "model_id",
    "parse_usage_block",
    "price_tokens",
    "rate_snapshot",
    "usage_from_record",
    "usage_line",
]
