from __future__ import annotations

from dataclasses import dataclass

from openshard.models.registry import _CAPABILITY_ATTRS, CAPABILITY_NAMES, ModelEntry, all_models

_COST_ORDER: dict[str, int] = {
    "free": 0,
    "tiny": 1,
    "cheap": 2,
    "mid": 3,
    "expensive": 4,
    "unknown": 5,
}

# Catalogued-but-unevaluated (watchlist) and retired (deprecated) models are
# not recommended by default: catalog presence alone must not surface a model.
# ``include_watchlist`` lists watchlist entries on request; deprecated never.
_NOT_RECOMMENDED_LIFECYCLES: frozenset[str] = frozenset({"watchlist", "deprecated"})

_HIGH_RISK_TIERS: frozenset[str] = frozenset({"strong", "frontier"})
_LOW_RISK_COSTS: frozenset[str] = frozenset({"free", "tiny", "cheap"})

__all__ = ["CAPABILITY_NAMES", "ModelAdvisory", "build_advisory_for_storage", "recommend_models"]


@dataclass(frozen=True)
class ModelAdvisory:
    model: ModelEntry
    reasons: tuple[str, ...]


def recommend_models(
    *,
    role: str | None = None,
    risk: str | None = None,
    required_capabilities: tuple[str, ...] = (),
    max_cost_class: str | None = None,
    include_experimental: bool = False,
    include_watchlist: bool = False,
    limit: int = 5,
) -> list[ModelAdvisory]:
    for cap in required_capabilities:
        if cap not in _CAPABILITY_ATTRS:
            return []

    excluded = _NOT_RECOMMENDED_LIFECYCLES - ({"watchlist"} if include_watchlist else set())
    candidates = [m for m in all_models() if m.lifecycle not in excluded]

    if role is not None:
        candidates = [m for m in candidates if role in m.roles]
        if not candidates:
            return []

    if not include_experimental:
        candidates = [m for m in candidates if not m.experimental]

    for cap in required_capabilities:
        attr = _CAPABILITY_ATTRS[cap]
        candidates = [m for m in candidates if getattr(m, attr)]

    if max_cost_class is not None:
        ceiling = _COST_ORDER.get(max_cost_class, 5)
        candidates = [m for m in candidates if _COST_ORDER.get(m.cost_class, 5) <= ceiling]

    scored: list[tuple[tuple, ModelAdvisory]] = []
    for model in candidates:
        score = 0
        reasons: list[str] = []

        if role is not None:
            score += 10
            reasons.append(f"matches role {role}")

        if risk == "high":
            if model.tier in _HIGH_RISK_TIERS:
                score += 8
                reasons.append("suitable for high-risk review")
            if model.supports_reasoning:
                score += 4
                if "suitable for high-risk review" not in reasons:
                    reasons.append("suitable for high-risk review")
        elif risk == "low":
            if model.cost_class in _LOW_RISK_COSTS:
                score += 5
            if model.latency_class == "fast":
                score += 3
        elif risk == "medium":
            if model.cost_class in {"cheap", "mid"}:
                score += 3

        for cap in required_capabilities:
            reasons.append(f"supports {cap}")

        if model.cost_class != "unknown":
            reasons.append(f"cost class {model.cost_class}")

        if not model.experimental:
            reasons.append("non-experimental")

        sort_key = (-score, _COST_ORDER.get(model.cost_class, 5), model.id)
        scored.append((sort_key, ModelAdvisory(model=model, reasons=tuple(reasons))))

    scored.sort(key=lambda x: x[0])
    return [advisory for _, advisory in scored[:limit]]


def build_advisory_for_storage(
    risk: str | None,
    *,
    limit: int = 3,
) -> tuple[list[dict], dict]:
    """Return (candidates, meta) for run history storage. Pure, no I/O.

    candidates: serializable advisory dicts, empty when risk is unknown.
    meta: audit record of the inputs and rule-version that produced the list.
    """
    meta: dict = {
        "version": "rules_v1",
        "risk": risk,
        "role": None,
        "required_capabilities": [],
        "limit": limit,
        "advisory_only": True,
    }
    if risk not in ("high", "medium", "low"):
        return [], meta
    advisories = recommend_models(risk=risk, limit=limit)
    candidates = [
        {
            "model_id": a.model.id,
            "display_name": a.model.display_name,
            "tier": a.model.tier,
            "cost_class": a.model.cost_class,
            "experimental": a.model.experimental,
            "reasons": list(a.reasons),
        }
        for a in advisories
    ]
    return candidates, meta
