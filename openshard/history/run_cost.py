"""Run-level cost and retry accounting.

A run has a first generation and, when verification failed, up to a few
escalation attempts, each on its own model. Older Core kept only the *last*
escalation's usage (each attempt overwrote the previous one) and stored the
*configured* fixer model, so a run that escalated twice lost the first
escalation's cost and never named the models that actually ran.

Records written now carry ``retry_attempts``: an ordered list with one entry
per escalation actually made (model, tokens, cost). ``retry_estimated_cost``
and the ``retry_*`` token counts are sums over those attempts.

Historical records are never rewritten or guessed at. A record without
``retry_attempts`` that retried keeps only what it recorded: its
``estimated_cost`` is the first attempt, its ``retry_estimated_cost`` is at
most the last escalation, and neither is presented as the run total.
:func:`run_total_cost` reports whether a total is *complete* so callers can
say so instead of implying it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from openshard.providers.base import UsageStats

# The escalation chain is short; bound what a record may carry.
MAX_RETRY_ATTEMPTS = 5

_MODEL_MAX = 120


def _num(value: object) -> float | None:
    """A finite, non-negative number (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) and f >= 0 else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass
class RetryUsage(UsageStats):
    """Aggregate usage over every escalation attempt, keeping the per-attempt records.

    Behaves as a plain :class:`UsageStats` for existing readers (``.prompt_tokens``,
    ``.estimated_cost`` ...), where the values are now sums over all attempts.
    """

    attempts: list[dict[str, Any]] = field(default_factory=list)


def retry_attempt_record(model: str, usage: UsageStats | None) -> dict[str, Any]:
    """One escalation attempt as stored. Missing usage stays ``None``, never 0."""
    return {
        "model": str(model)[:_MODEL_MAX],
        "prompt_tokens": _count(getattr(usage, "prompt_tokens", None)),
        "completion_tokens": _count(getattr(usage, "completion_tokens", None)),
        "total_tokens": _count(getattr(usage, "total_tokens", None)),
        "estimated_cost": _num(getattr(usage, "estimated_cost", None)),
    }


def aggregate_retry_usage(attempts: list[dict[str, Any]]) -> RetryUsage | None:
    """Sum the attempts. A cost is only summed when every attempt has one."""
    if not attempts:
        return None

    def total(key: str) -> int:
        return sum(a.get(key) or 0 for a in attempts)

    costs = [a.get("estimated_cost") for a in attempts]
    cost = sum(costs) if all(c is not None for c in costs) else None  # type: ignore[arg-type]
    return RetryUsage(
        prompt_tokens=total("prompt_tokens"),
        completion_tokens=total("completion_tokens"),
        total_tokens=total("total_tokens"),
        estimated_cost=cost,
        attempts=[dict(a) for a in attempts],
    )


def stored_retry_attempts(entry: dict) -> list[dict[str, Any]] | None:
    """The record's ``retry_attempts``, re-validated; None when absent or malformed."""
    raw = entry.get("retry_attempts") if isinstance(entry, dict) else None
    if not isinstance(raw, list) or not raw or len(raw) > MAX_RETRY_ATTEMPTS:
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("model"), str) or not item["model"].strip():
            return None
        out.append(
            {
                "model": item["model"].strip()[:_MODEL_MAX],
                "prompt_tokens": _count(item.get("prompt_tokens")),
                "completion_tokens": _count(item.get("completion_tokens")),
                "total_tokens": _count(item.get("total_tokens")),
                "estimated_cost": _num(item.get("estimated_cost")),
            }
        )
    return out


def run_total_cost(entry: dict) -> tuple[float | None, bool]:
    """``(cost, complete)`` for the whole run as the record can support it.

    * No retry: the first generation is the whole run, ``complete`` is True
      when its cost is known.
    * Retried and every attempt's cost was stored (``retry_attempts``): the
      true total, ``complete`` True.
    * Retried but that is not stored (historical record, or an attempt with
      no cost): the recorded first-attempt cost with ``complete`` False.
      Nothing is added, because what was lost cannot be recovered.
    """
    first = _num(entry.get("estimated_cost"))
    if entry.get("retry_triggered") is not True:
        return first, first is not None
    attempts = stored_retry_attempts(entry)
    if attempts is None or first is None:
        return first, False
    costs = [a["estimated_cost"] for a in attempts]
    if any(c is None for c in costs):
        return first, False
    return first + sum(costs), True


def run_total_from_usage(
    usage: UsageStats | None, retry_usage: UsageStats | None, retry_triggered: bool
) -> tuple[float | None, bool]:
    """Same rule as :func:`run_total_cost`, for the live objects the CLI prints from."""
    first = _num(getattr(usage, "estimated_cost", None))
    if not retry_triggered:
        return first, first is not None
    attempts = getattr(retry_usage, "attempts", None)
    retry_cost = _num(getattr(retry_usage, "estimated_cost", None))
    if not attempts or first is None or retry_cost is None:
        return first, False
    return first + retry_cost, True


def run_cost_usd(entry: dict) -> float | None:
    """The run's cost for summaries: the true total when complete, else the recorded first attempt."""
    return run_total_cost(entry)[0]
