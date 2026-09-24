"""Registry-backed routing model resolver.

Replaces the six hardcoded string constants in routing/engine.py with
live lookups against the model registry. Falls back to the original
hardcoded IDs when no eligible registry candidate is found, so the
system degrades gracefully if the registry is empty or malformed.

Roles cheap/main/escalate/visual are backed by routing classes
(``openshard.routing.routing_classes``): the class declares the capability
requirement, and the model is selected from the curated catalog. strong and
complex keep their legacy role queries. Import-time resolution uses the
curated-only catalog (no disk or network), so it is identical online and
offline; discovery-only models never become defaults here.

Module-level constants (MODEL_CHEAP, MODEL_MAIN, …) are evaluated once
at import time and cached. The registry is static at startup, so the
values are stable for the lifetime of the process.

Usage in engine.py and anywhere else that needs routing model IDs::

    from openshard.routing.model_resolver import (
        MODEL_CHEAP, MODEL_MAIN, MODEL_STRONG,
        MODEL_ESCALATE, MODEL_VISUAL, MODEL_COMPLEX,
        ESCALATION_CHAIN,
        resolution_source,
    )
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache

from openshard.routing.provider_availability import RoutablePool
from openshard.routing.routing_classes import ROLE_TO_CLASS, ROUTING_CLASSES

# ---------------------------------------------------------------------------
# Hardcoded fallbacks — exact IDs that were previously in engine.py.
# These are ONLY used when the registry returns no eligible candidate.
# ---------------------------------------------------------------------------

_FALLBACKS: dict[str, str] = {
    "cheap":    "deepseek/deepseek-v4-flash",
    "main":     "z-ai/glm-5.1",
    "strong":   "anthropic/claude-sonnet-4.6",
    "escalate": "anthropic/claude-opus-4.7",
    "visual":   "moonshotai/kimi-k2.5",
    "complex":  "minimax/m2.7",
}

# ---------------------------------------------------------------------------
# Role query specifications.
#
# Each entry describes how to search the registry for the best candidate:
#   lifecycle   — primary lifecycle filter (also tries "active_specialist" as
#                 secondary for roles that historically live there)
#   cost_classes — acceptable cost_class values (empty = no cost filter)
#   tiers        — acceptable tier values (empty = no tier filter)
#   roles_hint   — preferred role names used for tie-breaking (not hard filter)
# ---------------------------------------------------------------------------

def _class_query(class_name: str) -> dict:
    cls = ROUTING_CLASSES[class_name]
    lifecycles = sorted(cls.lifecycles)
    return {
        "lifecycle":     lifecycles[0],
        "cost_classes":  set(cls.cost_classes),
        "tiers":         set(cls.tiers),
        "roles_hint":    cls.roles_hint,
        "required_tags": frozenset(cls.required_tags),
        "preferred_tags": frozenset(cls.preferred_tags),
        "routing_class": class_name,
    }


_ROLE_QUERY: dict[str, dict] = {
    "cheap":    _class_query(ROLE_TO_CLASS["cheap"]),
    "main":     _class_query(ROLE_TO_CLASS["main"]),
    "strong": {
        "lifecycle":    "active_default",
        "cost_classes": set(),          # no cost ceiling for strong
        "tiers":        {"strong"},
        "roles_hint":   ("planner", "reviewer"),
    },
    "escalate": _class_query(ROLE_TO_CLASS["escalate"]),
    "visual":   _class_query(ROLE_TO_CLASS["visual"]),
    "complex": {
        "lifecycle":    "active_specialist",  # complex/long-horizon specialist
        "cost_classes": set(),
        "tiers":        set(),
        "roles_hint":   ("complex", "long_context"),
    },
}


def _entry_tags(entry) -> frozenset[str]:
    from openshard.models.catalog import capability_tags_for_model_entry

    return frozenset(capability_tags_for_model_entry(entry))


@cache
def resolve_routing_model(role: str) -> str:
    """Return the best registry-backed model ID for *role*.

    Resolution order:
    1. Filter ``_REGISTRY`` to entries with the target lifecycle.
    2. Apply cost_class and tier filters where specified.
    3. Prefer entries whose roles overlap with the role hint list.
    4. Stable tie-break: alphabetical by model ID for determinism.
    5. If no candidate survives any step, return ``_FALLBACKS[role]``.

    Never raises. Unknown role strings return the "main" fallback.
    """
    if role not in _ROLE_QUERY:
        return _FALLBACKS.get(role, _FALLBACKS["main"])

    class_name = ROLE_TO_CLASS.get(role)
    if class_name is not None:
        try:
            from openshard.models.catalog import build_catalog
            from openshard.models.registry import models_by_lifecycle as _by_lifecycle
            from openshard.routing.routing_classes import select_for_class

            # Curated entries only, fetched per class lifecycle so the registry
            # seam (models_by_lifecycle) stays the single source.
            curated = [
                m
                for lc in sorted(ROUTING_CLASSES[class_name].lifecycles)
                for m in _by_lifecycle(lc)
            ]
            selected = select_for_class(class_name, build_catalog(curated)).model
        except Exception:
            selected = None
        return selected or _FALLBACKS[role]

    try:
        from openshard.models.registry import models_by_lifecycle as _by_lifecycle
    except Exception:
        return _FALLBACKS[role]

    spec = _ROLE_QUERY[role]
    lifecycle: str = spec["lifecycle"]
    cost_classes: set = spec["cost_classes"]
    tiers: set = spec["tiers"]
    roles_hint: tuple = spec["roles_hint"]

    # Gather candidates from primary lifecycle
    candidates = _by_lifecycle(lifecycle)

    # For roles whose primary lifecycle is active_specialist, we only search
    # there. For roles whose primary lifecycle is active_default, also try
    # active_specialist as a secondary source if the primary yields nothing
    # after filtering — this covers edge cases where the registry evolves.

    def _apply_filters(pool):
        result = []
        for m in pool:
            if cost_classes and m.cost_class not in cost_classes:
                continue
            if tiers and m.tier not in tiers:
                continue
            result.append(m)
        return result

    filtered = _apply_filters(candidates)

    # If primary lifecycle is active_default and nothing survived, try
    # relaxing cost/tier filters before giving up.
    if not filtered and lifecycle == "active_default":
        filtered = candidates  # relax filters, keep lifecycle constraint

    if not filtered:
        return _FALLBACKS[role]

    # Tie-break 1: prefer entries with a matching roles_hint
    def _hint_score(m) -> int:
        return sum(1 for h in roles_hint if h in m.roles)

    # Tie-break 2: stable alphabetical by ID
    filtered.sort(key=lambda m: (-_hint_score(m), m.id))
    return filtered[0].id


@cache
def resolution_source(role: str) -> str:
    """Return ``"registry"`` if the model was resolved from the registry,
    ``"hardcoded"`` if the fallback constant was used.
    """
    resolved = resolve_routing_model(role)
    fallback = _FALLBACKS.get(role, "")
    return "hardcoded" if resolved == fallback else "registry"


# ---------------------------------------------------------------------------
# Module-level constants — same names as engine.py previously defined.
# All other modules (stages.py, dispatch.py, pipeline.py) continue to
# import these from engine.py which re-exports them from here.
# ---------------------------------------------------------------------------

try:
    MODEL_CHEAP    = resolve_routing_model("cheap")
    MODEL_MAIN     = resolve_routing_model("main")
    MODEL_STRONG   = resolve_routing_model("strong")
    MODEL_ESCALATE = resolve_routing_model("escalate")
    MODEL_VISUAL   = resolve_routing_model("visual")
    MODEL_COMPLEX  = resolve_routing_model("complex")
except Exception:  # pragma: no cover — pathological registry failure
    MODEL_CHEAP    = _FALLBACKS["cheap"]
    MODEL_MAIN     = _FALLBACKS["main"]
    MODEL_STRONG   = _FALLBACKS["strong"]
    MODEL_ESCALATE = _FALLBACKS["escalate"]
    MODEL_VISUAL   = _FALLBACKS["visual"]
    MODEL_COMPLEX  = _FALLBACKS["complex"]

ESCALATION_CHAIN: list[str] = [MODEL_STRONG, MODEL_ESCALATE]


# ---------------------------------------------------------------------------
# Provider-aware (context-aware) resolver.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderAwareResolution:
    """Result of a provider-constrained model resolution."""

    model: str | None
    role: str
    source: str              # "routable_pool" | "class_pin" | "no_eligible_model"
    enforcement_applied: bool
    rejected_model: str | None   # unconstrained choice, when it differs from model
    selected_model: str | None   # alias for model; explicit in metadata
    routable_pool_size: int


def resolve_routing_model_for_context(
    role: str,
    pool: RoutablePool,
    *,
    class_pins: Mapping[str, str] | None = None,
) -> ProviderAwareResolution:
    """Context-aware resolver: select from pool-eligible entries only.

    Applies the same scoring/hint logic as ``resolve_routing_model`` but
    filters candidates to those present in ``pool.routable``.  Uses the
    pool entries directly (they are full ``ModelEntry`` objects) so no
    additional registry import is needed.

    Resolution order:
    1. Lifecycle + cost/tier filtered candidates from pool.
    2. If none: relax cost/tier (active_default roles only), or try all pool
       entries (specialist roles whose lifecycle is absent from the pool).
    3. If pool is completely empty: return ``model=None`` with
       ``source="no_eligible_model"``.

    A routing-class pin (``class_pins``, canonical ids from
    ``models.routing_classes`` config) for the role's class wins over steps
    1-2 when the pinned model is in the routable pool; ``source="class_pin"``.

    Never raises.
    """
    pool_size = len(pool.routable)
    unconstrained = resolve_routing_model(role)

    def _make(chosen: str | None, src: str) -> ProviderAwareResolution:
        enforced = chosen != unconstrained
        return ProviderAwareResolution(
            model=chosen,
            role=role,
            source=src,
            enforcement_applied=enforced,
            rejected_model=(unconstrained if enforced and chosen is not None else None),
            selected_model=chosen,
            routable_pool_size=pool_size,
        )

    if not pool.routable:
        return ProviderAwareResolution(
            model=None,
            role=role,
            source="no_eligible_model",
            enforcement_applied=True,
            rejected_model=unconstrained,
            selected_model=None,
            routable_pool_size=0,
        )

    pinned = (class_pins or {}).get(ROLE_TO_CLASS.get(role, ""))
    if pinned and any(m.id == pinned for m in pool.routable):
        return _make(pinned, "class_pin")

    spec = _ROLE_QUERY.get(role)
    if spec is None:
        # Unknown role: pick any routable model alphabetically.
        chosen = sorted(pool.routable, key=lambda m: m.id)[0].id
        return _make(chosen, "routable_pool")

    lifecycle: str = spec["lifecycle"]
    cost_classes: set = spec["cost_classes"]
    tiers: set = spec["tiers"]
    roles_hint: tuple = spec["roles_hint"]

    def _hint_score(m) -> int:
        return sum(1 for h in roles_hint if h in m.roles)

    required_tags: frozenset = spec.get("required_tags", frozenset())

    def _apply_filters(entries):
        result = []
        for m in entries:
            if required_tags and not required_tags <= _entry_tags(m):
                continue
            if cost_classes and m.cost_class not in cost_classes:
                continue
            if tiers and m.tier not in tiers:
                continue
            result.append(m)
        return result

    # Step 1: lifecycle-matched candidates from pool.
    lc_candidates = [m for m in pool.routable if m.lifecycle == lifecycle]
    filtered = _apply_filters(lc_candidates)

    # Step 2a: relax cost/tier for active_default roles.
    if not filtered and lifecycle == "active_default":
        filtered = [
            m for m in lc_candidates
            if not required_tags or required_tags <= _entry_tags(m)
        ] or lc_candidates

    # Step 2b: specialist roles (visual, complex, escalate) have lifecycle
    # active_specialist, which is excluded from the default-routable pool.
    # For these roles, check whether the unconstrained model is accessible
    # via the available providers before falling through to pool models.
    # If openrouter is available, all registry models are reachable, so the
    # specialist model is correct and enforcement should not override it.
    # For direct providers, check if the unconstrained model's vendor matches.
    if not filtered and lifecycle != "active_default":
        available_providers = pool.available_providers  # ('openrouter',) etc.
        if "openrouter" in available_providers:
            # OpenRouter reaches every registry model — no enforcement needed.
            return _make(unconstrained, "routable_pool")
        vendor = unconstrained.lstrip("~").split("/", 1)[0] if unconstrained else ""
        if vendor and vendor in available_providers:
            return _make(unconstrained, "routable_pool")
        # Specialist model not reachable — fall through to any pool model.

    if not filtered:
        filtered = list(pool.routable)

    preferred_tags: frozenset = spec.get("preferred_tags", frozenset())
    filtered.sort(
        key=lambda m: (-_hint_score(m), -len(preferred_tags & _entry_tags(m)), m.id)
    )
    chosen = filtered[0].id
    return _make(chosen, "routable_pool")
