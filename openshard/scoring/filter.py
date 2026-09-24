from __future__ import annotations

from typing import Callable

from openshard.providers.manager import InventoryEntry
from openshard.scoring.requirements import TaskRequirements

# ---------------------------------------------------------------------------
# Lifecycle-based score adjustments.
#
# active_default    → no penalty (eligible for default routing)
# active_specialist → moderate penalty; still usable when task needs it
# fallback/open_weight → same moderate penalty
# experimental/watchlist → heavy penalty; not production-safe by default
# deprecated        → hard exclude (sentinel value None)
#
# Unknown model IDs (not in registry) get 0.0 — pass-through so unknown
# models in provider inventory still compete normally.
# ---------------------------------------------------------------------------

_LIFECYCLE_SCORE_ADJUSTMENTS: dict[str, float | None] = {
    "active_default":    0.0,
    "active_specialist": -3.0,
    "fallback":          -3.0,
    "open_weight":       -3.0,
    "experimental":      -10.0,
    "watchlist":         -10.0,
    "deprecated":        None,   # sentinel: hard exclude
}

_NONCODING_SUBSTRINGS = (
    "embed",
    "tts",
    "whisper",
    "dall-e",
    "stable-diffusion",
    "moderation",
    "transcri",
    "/image-",
    "image",
    "vision",
)


def lifecycle_adjustments_for_entries(
    entries: list[InventoryEntry],
    *,
    registry_fn: Callable[[str], str | None] | None = None,
) -> dict[str, float]:
    """Return a score-adjustment dict keyed by model_id based on lifecycle.

    Values:
    * ``0.0`` — active_default or unknown model (pass-through)
    * negative float — non-default lifecycle (see ``_LIFECYCLE_SCORE_ADJUSTMENTS``)
    * ``float("-inf")`` — deprecated; callers must exclude these entries

    Args:
        entries: candidate inventory entries to evaluate
        registry_fn: optional override for ``lifecycle_for`` (injectable for tests)
    """
    try:
        from openshard.models.registry import lifecycle_for as _default_lc_fn
    except Exception:
        return {e.model.id: 0.0 for e in entries}

    _lc_fn = registry_fn if registry_fn is not None else _default_lc_fn
    result: dict[str, float] = {}
    for entry in entries:
        mid = entry.model.id
        try:
            lc = _lc_fn(mid)
        except Exception:
            lc = None
        if lc is None:
            result[mid] = 0.0  # unknown model: pass-through
            continue
        adj = _LIFECYCLE_SCORE_ADJUSTMENTS.get(lc, 0.0)
        if adj is None:
            result[mid] = float("-inf")  # deprecated sentinel
        else:
            result[mid] = adj
    return result


def filter_deprecated(
    entries: list[InventoryEntry],
    *,
    registry_fn: Callable[[str], str | None] | None = None,
) -> list[InventoryEntry]:
    """Hard-remove deprecated models from the candidate list.

    Models not present in the registry are kept (conservative pass-through).

    Args:
        entries: candidate inventory entries
        registry_fn: optional override for ``lifecycle_for`` (injectable for tests)
    """
    try:
        from openshard.models.registry import lifecycle_for as _default_lc_fn
    except Exception:
        return list(entries)

    _lc_fn = registry_fn if registry_fn is not None else _default_lc_fn
    result = []
    for entry in entries:
        try:
            lc = _lc_fn(entry.model.id)
        except Exception:
            lc = None
        if lc != "deprecated":
            result.append(entry)
    return result


def filter_unpromoted(
    entries: list[InventoryEntry],
    *,
    allow: frozenset[str] = frozenset(),
    registry_fn: Callable[[str], str | None] | None = None,
) -> list[InventoryEntry]:
    """Drop uncurated models from an inventory that contains curated ones.

    Provider inventories list every model the provider serves, including ones
    released yesterday. Scored selection must not promote those on its own:
    the shortlist keeps the highest version per family, so an uncurated
    ``deepseek-v4.1-flash`` would otherwise silently displace the curated
    DeepSeek models. A model enters scored routing only once it is curated in
    the registry, or when the user named it (roster / class pin).

    Curated means a registry id, or (without *registry_fn*) an alias the
    curated catalog resolves, so direct-provider ids such as
    ``claude-sonnet-4-6`` count as ``anthropic/claude-sonnet-4.6``.
    Like ``build_shortlist``, an inventory with no curated (or allowed) entry
    at all is returned unchanged rather than emptied.

    Args:
        entries: provider inventory entries
        allow: explicitly selected ids that pass regardless of curation
        registry_fn: optional override for ``lifecycle_for`` (injectable for tests)
    """
    try:
        from openshard.models.registry import lifecycle_for as _default_lc_fn
    except Exception:
        return list(entries)

    _lc_fn = registry_fn if registry_fn is not None else _default_lc_fn
    resolve: Callable[[str], str | None] | None = None
    if registry_fn is None:
        try:
            from openshard.models.catalog import curated_catalog

            resolve = curated_catalog().resolve
        except Exception:
            resolve = None

    def _curated(mid: str) -> bool:
        try:
            if _lc_fn(mid) is not None:
                return True
        except Exception:
            pass
        return resolve is not None and resolve(mid) is not None

    kept = [e for e in entries if e.model.id in allow or _curated(e.model.id)]
    return kept if kept else list(entries)


def prefilter_coding(entries: list[InventoryEntry]) -> list[InventoryEntry]:
    """Drop models that are clearly non-coding (embeddings, TTS, image gen, etc.)."""
    result = []
    for entry in entries:
        mid = entry.model.id.lower()
        if any(s in mid for s in _NONCODING_SUBSTRINGS):
            continue
        if mid.endswith("/image"):
            continue
        result.append(entry)
    return result


def _parse_cost(pricing: dict) -> float | None:
    raw = pricing.get("prompt", "")
    if not raw:
        return None
    try:
        val = float(str(raw).lstrip("$"))
    except (ValueError, TypeError):
        return None
    if val <= 0:
        return None
    # Provider APIs return per-token prices; convert to per-million-token.
    return val * 1_000_000


def filter_inventory(
    entries: list[InventoryEntry],
    requirements: TaskRequirements,
) -> list[InventoryEntry]:
    """Return entries that satisfy all hard requirements."""
    result = []
    for entry in entries:
        m = entry.model

        if requirements.needs_vision and not m.supports_vision:
            continue
        if requirements.needs_tools and not m.supports_tools:
            continue
        if (
            requirements.min_context_window is not None
            and m.context_window is not None
            and m.context_window < requirements.min_context_window
        ):
            continue
        if requirements.preferred_max_cost_per_m is not None:
            cost = _parse_cost(m.pricing)
            if cost is None or cost > requirements.preferred_max_cost_per_m:
                continue

        result.append(entry)
    return result
