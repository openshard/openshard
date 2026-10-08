"""Requirement classes: what a routing step needs, resolved against live candidates.

A requirement class describes the *work* of the current step, never a
permanent quality of a model:

    fast_control    control-plane calls and quick answers; prefers cheap
    routine_coding  ordinary implementation work; prefers a mid price band
    deep_reasoning  hard, high-risk or repair work; needs a reasoning model
    vision          the step must read an image
    long_context    the step needs a very large context window
    verifier        an independent check or review of another model's work

Selection is a pure function of (class, catalog entries, promotion states,
observed evidence, cost sensitivity, exclusions). It never reads a curated
tier. The order it applies, each part recorded per candidate so a Receipt can
show why one model beat another:

1. hard requirements: promotion state selectable, provider status current,
   required capability tags, minimum context, not already tried;
2. ``promotion``: a dogfood candidate named for this class first (only when
   dogfood is enabled), then stable/validated, then other dogfood candidates;
3. ``history``: observed evidence, only when the caller passes evidence it
   judged meaningful (see ``ObservedEvidence``); otherwise ``not_used``;
4. ``requirement_fit``: how many preferred tags are missing;
5. ``supersession``: within one family from one provider, the newest release
   first (the provider's own point-release ordering, not a quality claim);
6. ``price_band``: inside the class's preferred output-price band, as
   tightened or relaxed by cost sensitivity;
7. ``curated_hint``: the legacy role hints, advisory and last;
8. ``price``: current output price, cheapest first;
9. model id.

Every component is a fact (2, 4-6, 8), observed evidence (3), or explicitly
labelled advisory (7). Nothing is an unexplained quality score.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from openshard.models.catalog import CatalogEntry, ModelCatalog
from openshard.models.promotion import (
    STATE_ELIGIBLE_FOR_SHADOW,
    STATE_STABLE,
    STATE_VALIDATED,
    promotion_state,
    selectable_states,
)

REQUIREMENTS_VERSION = "requirement_classes_v1"

# Output-price bands ($ per million output tokens), the same scale the catalog
# uses for its price-derived cost class.
PRICE_BANDS: dict[str, float] = {"cheap": 1.0, "mid": 5.0}
_BAND_ORDER: tuple[str | None, ...] = ("cheap", "mid", None)

COST_SENSITIVITIES = frozenset({"low", "normal", "high"})

# Statuses a fresh run may not select without naming the model. ``unlisted``
# is handled through the promotion state (retired only on a fresh snapshot).
UNSELECTABLE_STATUSES: frozenset[str] = frozenset({"deprecated", "floating_alias"})

R_ALREADY_TRIED = "already_tried"
R_PROMOTION_PREFIX = "promotion:"
R_STATUS_PREFIX = "status:"
R_MISSING_CAPABILITY_PREFIX = "missing_capability:"
R_CONTEXT_TOO_SMALL = "context_window_too_small"

HISTORY_NOT_USED = "not_used"
HISTORY_USED = "used"


@dataclass(frozen=True)
class RequirementClass:
    name: str
    description: str
    required_tags: frozenset[str] = frozenset()
    preferred_tags: frozenset[str] = frozenset()
    min_context_tokens: int | None = None
    # Preferred output-price band ceiling (None: no preference).
    price_band: str | None = None
    # Legacy role hints; advisory tie-break only.
    legacy_hint_roles: tuple[str, ...] = ()


REQUIREMENT_CLASSES: dict[str, RequirementClass] = {
    "fast_control": RequirementClass(
        name="fast_control",
        description="Control-plane calls and quick answers; low latency and low cost preferred.",
        preferred_tags=frozenset({"tools", "fast"}),
        price_band="cheap",
        legacy_hint_roles=("cheap_control", "fast_chat", "summariser"),
    ),
    "routine_coding": RequirementClass(
        name="routine_coding",
        description="Ordinary implementation work with tool use.",
        required_tags=frozenset({"tools"}),
        preferred_tags=frozenset({"structured_outputs"}),
        price_band="mid",
        legacy_hint_roles=("routine_engineering", "standard_coding", "coding", "value_worker"),
    ),
    "deep_reasoning": RequirementClass(
        name="deep_reasoning",
        description="Hard, high-risk, ambiguous or repair work; a reasoning model is required.",
        required_tags=frozenset({"tools", "reasoning"}),
        legacy_hint_roles=("escalation", "planner", "reviewer", "reasoning"),
    ),
    "vision": RequirementClass(
        name="vision",
        description="The step must read an image.",
        required_tags=frozenset({"vision"}),
        preferred_tags=frozenset({"tools"}),
        legacy_hint_roles=("visual", "multimodal"),
    ),
    "long_context": RequirementClass(
        name="long_context",
        description="The step needs a very large context window.",
        required_tags=frozenset({"tools", "long_context"}),
        legacy_hint_roles=("long_context", "complex", "high_context", "long_horizon"),
    ),
    "verifier": RequirementClass(
        name="verifier",
        description="Independent check or review of another model's work.",
        required_tags=frozenset({"tools"}),
        preferred_tags=frozenset({"reasoning"}),
        price_band="mid",
        legacy_hint_roles=("verifier", "reviewer", "lightweight_review", "light_review"),
    ),
}

REQUIREMENT_NAMES: tuple[str, ...] = tuple(REQUIREMENT_CLASSES)

# Where a step goes after an observed failure. ``None``: no class above it;
# the same class is retried with the tried models excluded.
ESCALATION_TARGET: dict[str, str | None] = {
    "fast_control": "routine_coding",
    "routine_coding": "deep_reasoning",
    "verifier": "deep_reasoning",
    "long_context": "deep_reasoning",
    "deep_reasoning": None,
    "vision": None,
}

# Legacy routing classes (routing_classes.ROUTING_CLASSES) and resolver roles
# expressed as requirement classes. ``cheap_coding`` is routine work under
# high cost sensitivity, not a different kind of work.
LEGACY_CLASS_TO_REQUIREMENT: dict[str, tuple[str, str | None]] = {
    "cheap_coding": ("routine_coding", "high"),
    "balanced_coding": ("routine_coding", None),
    "frontier_reasoning": ("deep_reasoning", None),
    "fast": ("fast_control", None),
    "vision": ("vision", None),
    "long_context": ("long_context", None),
}


def requirement_for_class_name(name: str | None) -> tuple[str, str | None] | None:
    """(requirement class, cost sensitivity override) for a legacy or V2 class name."""
    if not name:
        return None
    if name in REQUIREMENT_CLASSES:
        return name, None
    return LEGACY_CLASS_TO_REQUIREMENT.get(name)


@dataclass(frozen=True)
class ObservedEvidence:
    """Observed outcomes for one model, already judged meaningful by the caller."""

    samples: int
    verified_success_rate: float
    cost_per_verified_success: float | None = None


@dataclass(frozen=True)
class RankedCandidate:
    model_id: str
    promotion_state: str
    # Ordered components; every value is explainable and JSON-safe.
    components: dict[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    entry: CatalogEntry = field(compare=False, repr=False, default=None)  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {
            "model": self.model_id,
            "promotion_state": self.promotion_state,
            "components": dict(self.components),
            "notes": list(self.notes),
        }


def band_ceiling(cls: RequirementClass, cost_sensitivity: str | None) -> str | None:
    """The class's price band after cost sensitivity: high tightens one notch,
    low removes the preference."""
    band = cls.price_band
    if cost_sensitivity == "low":
        return None
    if cost_sensitivity == "high":
        idx = _BAND_ORDER.index(band)
        return _BAND_ORDER[max(0, idx - 1)]
    return band


def _within_band(price: float | None, band: str | None) -> bool | None:
    """True/False against the band; None when there is no band preference."""
    if band is None:
        return None
    if price is None:
        return False  # an unknown price cannot be shown to be inside the band
    return price <= PRICE_BANDS[band]


def hard_requirement_failure(
    cls: RequirementClass, entry: CatalogEntry, *, min_context_tokens: int | None = None
) -> str | None:
    """Why *entry* cannot serve *cls* on facts alone, or None."""
    if entry.status in UNSELECTABLE_STATUSES:
        return R_STATUS_PREFIX + entry.status
    missing = cls.required_tags - set(entry.capability_tags)
    if missing:
        return R_MISSING_CAPABILITY_PREFIX + ",".join(sorted(missing))
    need = max(x for x in (cls.min_context_tokens, min_context_tokens) if x is not None) \
        if (cls.min_context_tokens is not None or min_context_tokens is not None) else None
    if need is not None and (entry.context_length is None or entry.context_length < need):
        return R_CONTEXT_TOO_SMALL
    return None


def _vendor(model_id: str) -> str:
    return model_id.lstrip("~").split("/", 1)[0].lower()


def _supersession(entries: list[CatalogEntry]) -> dict[str, int]:
    """0 for the newest release in each (family, vendor) group, 1 for older siblings.

    The vendor is the id prefix (``z-ai``), not the curated display name, which
    is not spelled consistently. Unknown release dates never supersede and are
    never superseded.
    """
    newest: dict[tuple[str, str], str] = {}
    for e in entries:
        if not e.release_date:
            continue
        key = (e.family, _vendor(e.id))
        if key not in newest or e.release_date > newest[key]:
            newest[key] = e.release_date
    out: dict[str, int] = {}
    for e in entries:
        key = (e.family, _vendor(e.id))
        out[e.id] = 1 if (e.release_date and newest.get(key) and e.release_date < newest[key]) else 0
    return out


def rank_for_requirement(
    cls: RequirementClass,
    entries: Iterable[CatalogEntry],
    *,
    states: Mapping[str, str],
    dogfood_enabled: bool = False,
    dogfood_for_class: frozenset[str] = frozenset(),
    history: Mapping[str, ObservedEvidence] | None = None,
    cost_sensitivity: str | None = None,
    min_context_tokens: int | None = None,
    exclude: frozenset[str] = frozenset(),
    prefer_price_over_hint: bool = False,
) -> tuple[list[RankedCandidate], list[tuple[str, str]]]:
    """Rank *entries* for *cls*. Returns (ranked, rejected) where rejected pairs
    each id with the first hard rule it failed. Deterministic."""
    allowed = selectable_states(dogfood_enabled=dogfood_enabled)
    band = band_ceiling(cls, cost_sensitivity)
    survivors: list[CatalogEntry] = []
    rejected: list[tuple[str, str]] = []
    for e in sorted(entries, key=lambda x: x.id):
        state = states.get(e.id, STATE_ELIGIBLE_FOR_SHADOW)
        if e.id in exclude:
            rejected.append((e.id, R_ALREADY_TRIED))
        elif state not in allowed:
            rejected.append((e.id, R_PROMOTION_PREFIX + state))
        else:
            failure = hard_requirement_failure(cls, e, min_context_tokens=min_context_tokens)
            if failure is not None:
                rejected.append((e.id, failure))
            else:
                survivors.append(e)

    superseded = _supersession(survivors)
    ranked: list[tuple[tuple, RankedCandidate]] = []
    for e in survivors:
        state = states.get(e.id, STATE_ELIGIBLE_FOR_SHADOW)
        notes: list[str] = []
        if dogfood_enabled and e.id in dogfood_for_class:
            promotion_rank = 0
            notes.append("dogfood_candidate_for_class")
        elif state in (STATE_STABLE, STATE_VALIDATED):
            promotion_rank = 1
        else:
            promotion_rank = 2
            notes.append("dogfood_candidate_not_named_for_class")

        ev = history.get(e.id) if history else None
        if history is None:
            history_rank, history_cost, history_note = 0, 0.0, HISTORY_NOT_USED
        elif ev is None:
            history_rank, history_cost, history_note = 1, float("inf"), "no_meaningful_evidence"
        elif ev.verified_success_rate <= 0.0:
            history_rank, history_cost, history_note = 2, float("inf"), "observed_failures_only"
            notes.append("observed_failures_only")
        else:
            history_rank = 0
            history_cost = ev.cost_per_verified_success if ev.cost_per_verified_success is not None else float("inf")
            history_note = HISTORY_USED

        fit_missing = len(cls.preferred_tags - set(e.capability_tags))
        sup = superseded.get(e.id, 0)
        if sup:
            notes.append("superseded_in_family")
        price = e.pricing.output_per_mtok
        in_band = _within_band(price, band)
        band_rank = 0 if in_band in (True, None) else 1
        if in_band is False:
            notes.append("price_above_band" if price is not None else "price_unknown")
        hints = sum(1 for h in cls.legacy_hint_roles if h in e.roles)
        price_key = price if price is not None else float("inf")
        # A caller that could not read its observed history may choose the
        # current provider price before the legacy curated role hint. The hint
        # remains the normal final advisory tie-break everywhere else.
        tail = (price_key, -hints) if prefer_price_over_hint else (-hints, price_key)
        key = (promotion_rank, history_rank, history_cost, fit_missing, sup, band_rank, *tail, e.id)
        ranked.append((key, RankedCandidate(
            model_id=e.id,
            promotion_state=state,
            components={
                "promotion": promotion_rank,
                "history": history_note,
                "history_samples": ev.samples if ev else None,
                "history_verified_success_rate": (
                    round(ev.verified_success_rate, 3) if ev else None
                ),
                "history_cost_per_verified_success": (
                    round(ev.cost_per_verified_success, 6)
                    if ev and ev.cost_per_verified_success is not None else None
                ),
                "requirement_fit_missing": fit_missing,
                "superseded_in_family": bool(sup),
                "price_band": band,
                "within_price_band": in_band,
                "curated_hint_matches": hints,
                "output_price_per_mtok": price,
                "price_source": e.pricing.source,
            },
            notes=tuple(notes),
            entry=e,
        )))
    ranked.sort(key=lambda kv: kv[0])
    return [rc for _, rc in ranked], rejected


def shadow_candidates(
    cls: RequirementClass,
    entries: Iterable[CatalogEntry],
    *,
    states: Mapping[str, str],
    min_context_tokens: int | None = None,
    limit: int = 3,
) -> tuple[str, ...]:
    """Models eligible for shadow evaluation that would satisfy *cls* if promoted,
    newest first. Includes provider-discovered and curated watchlist entries.
    Reported, never selected."""
    found = [
        e for e in entries
        if states.get(e.id) == STATE_ELIGIBLE_FOR_SHADOW
        and hard_requirement_failure(cls, e, min_context_tokens=min_context_tokens) is None
        and ":" not in e.id
    ]
    found.sort(key=lambda e: (e.release_date or "", e.id), reverse=True)
    return tuple(e.id for e in found[:limit])


@dataclass(frozen=True)
class RequirementSelection:
    requirement: str
    model: str | None
    source: str  # "pinned" | "ranked" | "none"
    ranked: tuple[RankedCandidate, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    shadow_candidates: tuple[str, ...] = ()
    rejected_pin: str | None = None
    rejected_pin_reason: str | None = None
    catalog_fingerprint: str = ""


def states_for(
    catalog: ModelCatalog, *, dogfood: frozenset[str] = frozenset()
) -> dict[str, str]:
    stale = bool(catalog.snapshot.stale)
    return {
        e.id: promotion_state(e, dogfood=dogfood, snapshot_stale=stale) for e in catalog.entries
    }


def select_for_requirement(
    name: str,
    catalog: ModelCatalog,
    *,
    dogfood: Mapping[str, frozenset[str]] | None = None,
    dogfood_enabled: bool = False,
    history: Mapping[str, ObservedEvidence] | None = None,
    cost_sensitivity: str | None = None,
    min_context_tokens: int | None = None,
    exclude: frozenset[str] = frozenset(),
    pins: Mapping[str, str] | None = None,
) -> RequirementSelection:
    """Deterministically select the model for requirement class *name* from the
    whole catalog (no availability or user policy applied; see the adaptive
    candidate set for that). Raises ``KeyError`` for an unknown class."""
    cls = REQUIREMENT_CLASSES[name]
    for_class = frozenset((dogfood or {}).get(name, frozenset()))
    states = states_for(catalog, dogfood=for_class if dogfood_enabled else frozenset())
    fp = catalog.snapshot.fingerprint

    rejected_pin = rejected_reason = None
    pin = (pins or {}).get(name)
    if pin is None and pins:
        # A legacy class pin applies to the requirement class it maps to.
        for legacy, (req, _) in LEGACY_CLASS_TO_REQUIREMENT.items():
            if req == name and pins.get(legacy):
                pin = pins[legacy]
                break
    if pin:
        entry = catalog.get(pin)
        reason = None
        if entry is None:
            reason = "unknown_model"
        elif states.get(entry.id) in ("retired", "restricted"):
            reason = "deprecated_or_blocked"
        else:
            reason = hard_requirement_failure(cls, entry, min_context_tokens=min_context_tokens)
            if reason is not None and reason.startswith(R_STATUS_PREFIX):
                reason = "deprecated_or_blocked"
        if reason is None and entry is not None and entry.id not in exclude:
            return RequirementSelection(name, entry.id, "pinned", (), (), (), None, None, fp)
        rejected_pin, rejected_reason = pin, reason or R_ALREADY_TRIED

    ranked, rejected = rank_for_requirement(
        cls, catalog.entries, states=states, dogfood_enabled=dogfood_enabled,
        dogfood_for_class=for_class, history=history, cost_sensitivity=cost_sensitivity,
        min_context_tokens=min_context_tokens, exclude=exclude,
    )
    shadow = shadow_candidates(cls, catalog.entries, states=states, min_context_tokens=min_context_tokens)
    if not ranked:
        return RequirementSelection(name, None, "none", (), tuple(rejected), shadow, rejected_pin, rejected_reason, fp)
    return RequirementSelection(
        name, ranked[0].model_id, "ranked", tuple(ranked), tuple(rejected), shadow,
        rejected_pin, rejected_reason, fp,
    )


def select_all_requirements(
    catalog: ModelCatalog, **kwargs
) -> dict[str, RequirementSelection]:
    return {name: select_for_requirement(name, catalog, **kwargs) for name in REQUIREMENT_NAMES}


__all__ = [
    "COST_SENSITIVITIES",
    "ESCALATION_TARGET",
    "HISTORY_NOT_USED",
    "HISTORY_USED",
    "LEGACY_CLASS_TO_REQUIREMENT",
    "PRICE_BANDS",
    "REQUIREMENTS_VERSION",
    "REQUIREMENT_CLASSES",
    "REQUIREMENT_NAMES",
    "UNSELECTABLE_STATUSES",
    "ObservedEvidence",
    "RankedCandidate",
    "RequirementClass",
    "RequirementSelection",
    "band_ceiling",
    "hard_requirement_failure",
    "rank_for_requirement",
    "requirement_for_class_name",
    "select_all_requirements",
    "select_for_requirement",
    "shadow_candidates",
    "states_for",
]
