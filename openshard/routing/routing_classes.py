"""Routing classes: routing policy by capability, not by permanent model version.

A routing class (``cheap_coding``, ``balanced_coding``, ``frontier_reasoning``,
``fast``, ``vision``) declares *what kind* of model a routing decision needs.
:func:`select_for_class` picks the model from a :class:`ModelCatalog`
deterministically:

1. **Pin** - an explicit user/team pin (``models.routing_classes`` in config)
   wins if the pinned model is recognised, meets the class's required
   capability tags and is not deprecated/blocked. Pins are explicit selection,
   so they may name a discovery-only model.
2. **Eligible pool** - otherwise only entries whose curated lifecycle is in the
   class's lifecycles are considered. Discovery-only models are never in this
   pool: a brand-new model cannot become a default without curation.
3. Filters: required tags (hard), then cost class and tier. When nothing
   survives and the class allows it, cost/tier are relaxed (lifecycle and
   required tags stay).
4. Order: most ``roles_hint`` matches, then most ``preferred_tags`` matches,
   then id. Same catalog in, same model out.

Promotion candidates are reported, never selected: discovery-only (or
not-promoted) models in the selected model's family, from the same provider,
released after it, that meet the class requirements. That is how a newer model
such as a DeepSeek Flash successor surfaces for evaluation without being
hardcoded anywhere.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from openshard.models.catalog import CatalogEntry, ModelCatalog

# Statuses a dynamically selected (non-pinned) model may not have.
# ``floating_alias`` points at a moving target, which breaks determinism.
_UNSELECTABLE_STATUSES = frozenset({"deprecated", "floating_alias"})
# Statuses a pin may not have.
_UNPINNABLE_STATUSES = frozenset({"deprecated"})


@dataclass(frozen=True)
class RoutingClass:
    name: str
    description: str
    lifecycles: frozenset[str]
    # Hard requirement: a model without these tags never serves the class.
    required_tags: frozenset[str] = frozenset()
    # Soft preference: ranks after role hints; required of promotion candidates.
    preferred_tags: frozenset[str] = frozenset()
    cost_classes: frozenset[str] = frozenset()
    tiers: frozenset[str] = frozenset()
    roles_hint: tuple[str, ...] = ()
    # Relax cost/tier (never lifecycle or tags) when the filtered pool is empty.
    relax_when_empty: bool = False


ROUTING_CLASSES: dict[str, RoutingClass] = {
    "cheap_coding": RoutingClass(
        name="cheap_coding",
        description="Low-cost coding worker for boilerplate and small, low-risk edits.",
        lifecycles=frozenset({"active_default"}),
        preferred_tags=frozenset({"tools"}),
        cost_classes=frozenset({"cheap", "tiny", "free"}),
        tiers=frozenset({"cheap", "tiny"}),
        roles_hint=("cheap_control", "boilerplate"),
        relax_when_empty=True,
    ),
    "balanced_coding": RoutingClass(
        name="balanced_coding",
        description="Default coding worker for standard feature work.",
        lifecycles=frozenset({"active_default"}),
        preferred_tags=frozenset({"tools"}),
        cost_classes=frozenset({"cheap", "mid"}),
        tiers=frozenset({"mid", "value_worker"}),
        roles_hint=("routine_engineering", "standard_coding"),
        relax_when_empty=True,
    ),
    "frontier_reasoning": RoutingClass(
        name="frontier_reasoning",
        description="Frontier escalation for hard, high-risk or ambiguous tasks.",
        lifecycles=frozenset({"active_specialist"}),
        preferred_tags=frozenset({"reasoning"}),
        tiers=frozenset({"frontier"}),
        roles_hint=("escalation",),
    ),
    "fast": RoutingClass(
        name="fast",
        description="Low-latency model for control-plane and quick answers.",
        lifecycles=frozenset({"active_default"}),
        required_tags=frozenset({"fast"}),
        preferred_tags=frozenset({"tools"}),
        cost_classes=frozenset({"cheap", "tiny", "free"}),
        roles_hint=("cheap_control", "fast_chat", "summariser"),
    ),
    "vision": RoutingClass(
        name="vision",
        description="Image-capable specialist for UI and visual tasks.",
        lifecycles=frozenset({"active_specialist"}),
        required_tags=frozenset({"vision"}),
        roles_hint=("visual", "multimodal"),
    ),
}

CLASS_NAMES: tuple[str, ...] = tuple(ROUTING_CLASSES)

# Legacy resolver roles (routing/model_resolver.py) backed by a routing class.
# "strong" and "complex" keep their legacy role queries in this change.
ROLE_TO_CLASS: dict[str, str] = {
    "cheap": "cheap_coding",
    "main": "balanced_coding",
    "escalate": "frontier_reasoning",
    "visual": "vision",
}


@dataclass(frozen=True)
class ClassSelection:
    class_name: str
    model: str | None
    # "pinned" | "catalog" | "none"
    source: str
    considered: tuple[str, ...] = ()
    promotion_candidates: tuple[str, ...] = ()
    rejected_pin: str | None = None
    rejected_pin_reason: str | None = None
    catalog_fingerprint: str = ""


def _rank_key(cls: RoutingClass, entry: CatalogEntry) -> tuple:
    hints = sum(1 for h in cls.roles_hint if h in entry.roles)
    preferred = len(cls.preferred_tags & set(entry.capability_tags))
    return (-hints, -preferred, entry.id)


def filter_for_class(
    cls: RoutingClass, entries: Iterable[CatalogEntry]
) -> list[CatalogEntry]:
    """Apply lifecycle/status/tag, then cost/tier filters (with relaxation)."""
    base = [
        e
        for e in entries
        if e.lifecycle in cls.lifecycles
        and e.routing_eligibility != "blocked"
        and e.status not in _UNSELECTABLE_STATUSES
        and cls.required_tags <= set(e.capability_tags)
    ]
    narrowed = [
        e
        for e in base
        if (not cls.cost_classes or e.cost_class in cls.cost_classes)
        and (not cls.tiers or e.tier in cls.tiers)
    ]
    if not narrowed and cls.relax_when_empty:
        narrowed = base
    return sorted(narrowed, key=lambda e: _rank_key(cls, e))


def pin_rejection_reason(cls: RoutingClass, entry: CatalogEntry | None) -> str | None:
    """Why *entry* cannot be pinned to *cls*, or None when the pin is valid."""
    if entry is None:
        return "unknown_model"
    if entry.routing_eligibility == "blocked" or entry.status in _UNPINNABLE_STATUSES:
        return "deprecated_or_blocked"
    missing = cls.required_tags - set(entry.capability_tags)
    if missing:
        return "missing_capability:" + ",".join(sorted(missing))
    return None


def promotion_candidates(
    cls: RoutingClass, catalog: ModelCatalog, selected: CatalogEntry
) -> tuple[str, ...]:
    """Newer same-family models that meet the class but are not promoted."""
    if not selected.release_date:
        return ()
    found: list[CatalogEntry] = []
    for e in catalog.family_members(selected.family):
        if e.id == selected.id or ":" in e.id:
            continue
        if e.routing_eligibility not in ("not_promoted",):
            continue
        if e.status in _UNSELECTABLE_STATUSES or e.provider != selected.provider:
            continue
        if not e.release_date or e.release_date <= selected.release_date:
            continue
        if not (cls.required_tags | cls.preferred_tags) <= set(e.capability_tags):
            continue
        if cls.cost_classes and e.cost_class not in cls.cost_classes:
            continue
        found.append(e)
    found.sort(key=lambda e: (e.release_date or "", e.id), reverse=True)
    return tuple(e.id for e in found)


def select_for_class(
    class_name: str,
    catalog: ModelCatalog,
    *,
    pins: Mapping[str, str] | None = None,
) -> ClassSelection:
    """Deterministically select the model for *class_name*. Never raises for
    known classes; raises ``KeyError`` for an unknown class name."""
    cls = ROUTING_CLASSES[class_name]
    fp = catalog.snapshot.fingerprint
    rejected_pin: str | None = None
    rejected_reason: str | None = None

    pin = (pins or {}).get(class_name)
    if pin:
        entry = catalog.get(pin)
        reason = pin_rejection_reason(cls, entry)
        if reason is None:
            assert entry is not None
            return ClassSelection(
                class_name, entry.id, "pinned", (entry.id,), (), None, None, fp
            )
        rejected_pin, rejected_reason = pin, reason

    ranked = filter_for_class(cls, catalog.entries)
    if not ranked:
        return ClassSelection(
            class_name, None, "none", (), (), rejected_pin, rejected_reason, fp
        )
    chosen = ranked[0]
    return ClassSelection(
        class_name,
        chosen.id,
        "catalog",
        tuple(e.id for e in ranked),
        promotion_candidates(cls, catalog, chosen),
        rejected_pin,
        rejected_reason,
        fp,
    )


def select_all_classes(
    catalog: ModelCatalog, *, pins: Mapping[str, str] | None = None
) -> dict[str, ClassSelection]:
    return {name: select_for_class(name, catalog, pins=pins) for name in CLASS_NAMES}
