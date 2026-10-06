"""Canonical provider/surface identity and economic cost evidence.

Receipts keep their concrete runtime identity. Insights may aggregate compatible
surfaces under a provider/product family without erasing the original surface.

Economic cost is intentionally separate from model usage cost:
provider/billed > token-equivalent list-rate > allocated subscription cost.
The strongest available figure is selected, but its kind and provenance are
always retained so an allocation is never presented as an invoice.
"""
from __future__ import annotations

import math
from typing import Any

COST_PROVIDER = "provider_billed"
COST_EQUIVALENT = "token_equivalent"
COST_ALLOCATED = "subscription_allocated"
COST_KINDS = frozenset({COST_PROVIDER, COST_EQUIVALENT, COST_ALLOCATED})
_COST_RANK = {COST_PROVIDER: 3, COST_EQUIVALENT: 2, COST_ALLOCATED: 1}

_OPENAI_SURFACES = {
    "chatgpt": ("chatgpt", "chat"),
    "chatgpt-chat": ("chatgpt", "chat"),
    "chatgpt-work": ("chatgpt", "work"),
    "work": ("chatgpt", "work"),
    "codex": ("codex", "codex"),
    "codex-cli": ("codex", "codex_cli"),
    "codex-cloud": ("codex", "codex_cloud"),
}


def canonical_surface(*, agent: object = None, surface: object = None, provider: object = None) -> dict[str, str | None]:
    """Return additive analytics identity; never rewrites the Receipt's raw agent/surface."""
    raw = surface if isinstance(surface, str) and surface.strip() else agent if isinstance(agent, str) else ""
    key = raw.strip().lower().replace("_", "-")
    provider_key = provider.strip().lower() if isinstance(provider, str) else ""
    if key in _OPENAI_SURFACES or provider_key == "openai" or key.startswith(("codex", "chatgpt")):
        product, canonical = _OPENAI_SURFACES.get(key, ("openai", key or "unknown"))
        return {"provider_family": "openai", "product_family": product, "surface": canonical, "raw_surface": raw or None}
    if key.startswith("claude") or provider_key == "anthropic":
        return {"provider_family": "anthropic", "product_family": "claude", "surface": key or "unknown", "raw_surface": raw or None}
    return {"provider_family": provider_key or None, "product_family": None, "surface": key or None, "raw_surface": raw or None}


def cost_evidence(kind: str, usd: object, *, source: str, complete: bool | None = None,
                  currency: str = "USD", metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Validate one economic-cost observation."""
    if kind not in COST_KINDS or not isinstance(source, str) or not source:
        return None
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        return None
    value = float(usd)
    if not math.isfinite(value) or value < 0:
        return None
    return {"kind": kind, "usd": value, "currency": currency, "source": source,
            "complete": complete, "metadata": dict(metadata or {})}


def strongest_cost(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the strongest valid economic cost without collapsing provenance."""
    valid = [item for item in items if isinstance(item, dict) and item.get("kind") in COST_KINDS
             and isinstance(item.get("usd"), (int, float)) and not isinstance(item.get("usd"), bool)]
    if not valid:
        return None
    return max(valid, key=lambda item: (_COST_RANK[item["kind"]], item.get("complete") is True))


def allocate_subscription_cost(monthly_usd: object, units: object, total_units: object, *,
                               source: str = "subscription_allocation") -> dict[str, Any] | None:
    """Allocate subscription economics by explicit activity units.

    This is deliberately an economic allocation, never provider/billed usage.
    Callers choose the unit (receipts, credits, active minutes, etc.) and should
    record that choice in metadata.
    """
    if isinstance(monthly_usd, bool) or not isinstance(monthly_usd, (int, float)):
        return None
    if isinstance(units, bool) or not isinstance(units, (int, float)):
        return None
    if isinstance(total_units, bool) or not isinstance(total_units, (int, float)):
        return None
    monthly, used, total = float(monthly_usd), float(units), float(total_units)
    if not all(math.isfinite(v) for v in (monthly, used, total)) or monthly < 0 or used < 0 or total <= 0 or used > total:
        return None
    return cost_evidence(COST_ALLOCATED, monthly * used / total, source=source, complete=True,
                         metadata={"allocation_units": used, "allocation_total_units": total})
