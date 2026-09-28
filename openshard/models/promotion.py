"""Promotion state: how far a model has come from "listed by a provider" to "a default".

Model releases outpace OpenShard releases, so the catalog cannot be a fixed
list of models with a hand-assigned quality. Instead every catalog entry has a
*promotion state* derived from things OpenShard can actually check:

    discovered            the provider lists it; nothing else is known
    eligible_for_shadow   listed, current, priced, with a known context window:
                          shadow routing may report what it would have chosen
    dogfood_candidate     the organisation named it for a requirement class in
                          ``models.dogfood_candidates`` (or curation marked it
                          experimental): applied routing may choose it, but only
                          behind the ``adaptive_routing`` capability
    validated             curated ``active_specialist``: serves requirement
                          classes it fits, never the general default
    stable                curated ``active_default``: the public default pool
    retired               deprecated, expired, or an id the provider no longer
                          lists: readable in old Receipts, never a fresh default
    restricted            access-restricted: never routable

Nothing here judges quality. Moving a model up is an explicit act: naming it
as a dogfood candidate (config, no release needed) or curating its lifecycle
after evaluation (a release). Moving one down happens on its own when the
provider retires it. The rules are pure functions of the catalog entry and the
organisation's dogfood list, so the same inputs always give the same state.
"""
from __future__ import annotations

from collections.abc import Mapping

from openshard.models.catalog import CatalogEntry

STATE_DISCOVERED = "discovered"
STATE_ELIGIBLE_FOR_SHADOW = "eligible_for_shadow"
STATE_DOGFOOD_CANDIDATE = "dogfood_candidate"
STATE_VALIDATED = "validated"
STATE_STABLE = "stable"
STATE_RETIRED = "retired"
STATE_RESTRICTED = "restricted"

PROMOTION_STATES: tuple[str, ...] = (
    STATE_DISCOVERED,
    STATE_ELIGIBLE_FOR_SHADOW,
    STATE_DOGFOOD_CANDIDATE,
    STATE_VALIDATED,
    STATE_STABLE,
    STATE_RETIRED,
    STATE_RESTRICTED,
)

# States applied routing may select without the dogfood capability.
PUBLIC_DEFAULT_STATES: frozenset[str] = frozenset({STATE_STABLE, STATE_VALIDATED})
# States applied routing may additionally select under the dogfood capability.
DOGFOOD_STATES: frozenset[str] = frozenset({STATE_DOGFOOD_CANDIDATE})
# States shadow routing may report a would-have-selected for.
SHADOW_STATES: frozenset[str] = PUBLIC_DEFAULT_STATES | DOGFOOD_STATES | {STATE_ELIGIBLE_FOR_SHADOW}

# Catalog statuses that mean the provider no longer serves the id.
RETIRED_STATUSES: frozenset[str] = frozenset({"deprecated", "unlisted"})

# Curated lifecycle -> promotion state, when status does not retire the model.
_LIFECYCLE_STATE: dict[str, str] = {
    "active_default": STATE_STABLE,
    "active_specialist": STATE_VALIDATED,
    "experimental": STATE_DOGFOOD_CANDIDATE,
    "deprecated": STATE_RETIRED,
}

# ``models.dogfood_candidates`` shape: requirement class -> model ids.
DogfoodCandidates = Mapping[str, frozenset[str]]


def dogfood_ids(candidates: DogfoodCandidates | None, requirement: str | None = None) -> frozenset[str]:
    """Ids named as dogfood candidates for *requirement* (all classes when None)."""
    if not candidates:
        return frozenset()
    if requirement is not None:
        return frozenset(candidates.get(requirement, frozenset()))
    out: set[str] = set()
    for ids in candidates.values():
        out |= set(ids)
    return frozenset(out)


def is_shadow_eligible(entry: CatalogEntry) -> bool:
    """Facts a model must have before shadow routing may even consider it.

    Listed and current (or preview), a known price and a known context window.
    Capability requirements are checked per requirement class, not here.
    """
    return (
        entry.status in ("current", "preview")
        and entry.pricing.output_per_mtok is not None
        and entry.context_length is not None
    )


def promotion_state(
    entry: CatalogEntry,
    *,
    dogfood: frozenset[str] = frozenset(),
    snapshot_stale: bool = False,
) -> str:
    """The promotion state of *entry*. *dogfood* holds the ids the organisation
    named as dogfood candidates (for the requirement class being routed).

    Absence from the provider list (``unlisted``) retires a model only when the
    snapshot is fresh: a stale cache cannot prove a model has gone.
    """
    if entry.status == "deprecated" or entry.lifecycle == "deprecated":
        return STATE_RETIRED
    if entry.status == "unlisted" and not snapshot_stale:
        return STATE_RETIRED
    if entry.routing_eligibility == "blocked":
        return STATE_RESTRICTED
    if entry.status == "floating_alias":
        # A moving target is never promoted; it is usable only when named.
        return STATE_DISCOVERED
    curated = _LIFECYCLE_STATE.get(entry.lifecycle)
    if curated in (STATE_STABLE, STATE_VALIDATED):
        return curated
    if entry.id in dogfood or curated == STATE_DOGFOOD_CANDIDATE:
        return STATE_DOGFOOD_CANDIDATE
    if is_shadow_eligible(entry):
        return STATE_ELIGIBLE_FOR_SHADOW
    return STATE_DISCOVERED


def selectable_states(*, dogfood_enabled: bool) -> frozenset[str]:
    """States an applied decision may select from."""
    return PUBLIC_DEFAULT_STATES | (DOGFOOD_STATES if dogfood_enabled else frozenset())


__all__ = [
    "DOGFOOD_STATES",
    "PROMOTION_STATES",
    "PUBLIC_DEFAULT_STATES",
    "RETIRED_STATUSES",
    "SHADOW_STATES",
    "STATE_DISCOVERED",
    "STATE_DOGFOOD_CANDIDATE",
    "STATE_ELIGIBLE_FOR_SHADOW",
    "STATE_RESTRICTED",
    "STATE_RETIRED",
    "STATE_STABLE",
    "STATE_VALIDATED",
    "DogfoodCandidates",
    "dogfood_ids",
    "is_shadow_eligible",
    "promotion_state",
    "selectable_states",
]
