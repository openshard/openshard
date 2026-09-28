"""CandidateSet: which catalog models may serve this run, and why the rest may not.

Generation starts from the dynamic :class:`ModelCatalog` (#347) and narrows it
with the rules that already exist, reusing their functions and reason tokens
instead of re-implementing them:

1. provider availability and access restriction
   (``provider_availability.build_available_pool``)
2. user/team model policy - roster, allow/block, providers, cost cap
   (``model_policy.apply_model_policy``)
3. executor (harness) provider constraint (``EXECUTOR_CONSTRAINTS``)
4. per-run user block list
5. catalog status/eligibility: deprecated or blocked never; a floating alias
   only when named explicitly; an id the only reachable provider no longer
   lists (``unlisted``) never, since it could not be dispatched
6. lifecycle: curated routing lifecycles (plus any the policy enables);
   discovery-only and other unpromoted models only when named explicitly or
   named as a dogfood candidate for this run (``dogfood_ids``) - a newly
   discovered model never becomes a candidate on its own
7. hard capability requirements from the context

Every candidate carries its promotion state (``models.promotion``) so a
policy can tell a stable default from a dogfood candidate without re-deriving
it.

Every rejected model keeps exactly one reason (the first rule it failed), so
the set is explainable and ``rejected`` + ``eligible`` always partition the
catalog. Deterministic: same catalog, availability and policy in, same set out.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from openshard.models.catalog import CatalogEntry, ModelCatalog
from openshard.models.promotion import promotion_state
from openshard.routing.model_policy import (
    ModelPolicyConfig,
    apply_model_policy,
    eligible_lifecycles,
    explicit_selection_ids,
)
from openshard.routing.provider_availability import (
    EXECUTOR_CONSTRAINTS,
    REASON_EXECUTOR_CONSTRAINT,
    REASON_LIFECYCLE_PREFIX,
    REASON_NO_API_KEY,
    REASON_USER_BLOCKED,
    ProviderAvailability,
    build_available_pool,
)

CANDIDATE_SET_VERSION = "adaptive_candidates_v2"

# Lifecycles routing classes select from (routing_classes.ROUTING_CLASSES).
CLASS_LIFECYCLES = frozenset({"active_default", "active_specialist"})

REASON_STATUS_PREFIX = "status:"
REASON_UNLISTED_ON_PROVIDER = "status:unlisted_on_provider"
REASON_ELIGIBILITY_BLOCKED = "eligibility:blocked"
REASON_NOT_PROMOTED = "eligibility:not_promoted"
REASON_MISSING_CAPABILITY_PREFIX = "missing_capability:"
REASON_CONTEXT_TOO_SMALL = "context_window_too_small"


@dataclass(frozen=True)
class Candidate:
    """One eligible model, with the facts routing policies rank on."""

    model_id: str
    via: tuple[str, ...]
    explicit: bool
    entry: CatalogEntry = field(compare=False, repr=False)
    # models.promotion state, given this run's dogfood list.
    promotion_state: str = "discovered"
    # Named as a dogfood candidate for this run (any requirement class).
    dogfood: bool = False

    @property
    def lifecycle(self) -> str:
        return self.entry.lifecycle

    @property
    def capability_tags(self) -> tuple[str, ...]:
        return self.entry.capability_tags


@dataclass(frozen=True)
class CandidateSet:
    eligible: tuple[Candidate, ...]
    # (model_id, reason), sorted by model id.
    rejected: tuple[tuple[str, str], ...]
    catalog: ModelCatalog = field(compare=False, repr=False)
    available_providers: tuple[str, ...] = ()
    harness: str | None = None
    explicit_ids: frozenset[str] = frozenset()
    dogfood_ids: frozenset[str] = frozenset()
    version: str = CANDIDATE_SET_VERSION

    @property
    def catalog_fingerprint(self) -> str:
        return self.catalog.snapshot.fingerprint

    def get(self, model_id: str) -> Candidate | None:
        canonical = self.catalog.resolve(model_id) or model_id
        for c in self.eligible:
            if c.model_id == canonical:
                return c
        return None

    def rejection_reason(self, model_id: str) -> str | None:
        canonical = self.catalog.resolve(model_id) or model_id
        for mid, reason in self.rejected:
            if mid == canonical:
                return reason
        return None

    def rejection_counts(self) -> dict[str, int]:
        """Reason -> count, with open-ended reasons bucketed by prefix."""
        counts: dict[str, int] = {}
        for _, reason in self.rejected:
            bucket = reason
            for prefix in (
                REASON_LIFECYCLE_PREFIX,
                REASON_STATUS_PREFIX,
                REASON_MISSING_CAPABILITY_PREFIX,
            ):
                if reason.startswith(prefix):
                    bucket = prefix.rstrip(":")
                    break
            counts[bucket] = counts.get(bucket, 0) + 1
        return dict(sorted(counts.items()))


def _pool_entry(entry: CatalogEntry):
    """The registry ``ModelEntry`` for curated ids, else a projection."""
    from openshard.models.registry import get_model

    return (get_model(entry.id) if entry.curated else None) or entry.to_model_entry()


def build_candidate_set(
    catalog: ModelCatalog,
    availability: ProviderAvailability,
    *,
    policy: ModelPolicyConfig | None = None,
    harness: str | None = None,
    blocked_model_ids: Iterable[str] = (),
    required_capabilities: Iterable[str] = (),
    min_context_tokens: int | None = None,
    explicit_model: str | None = None,
    dogfood_ids: Iterable[str] = (),
) -> CandidateSet:
    """Narrow *catalog* to the models this run may use. Never raises.

    *dogfood_ids* are models the organisation named as dogfood candidates and
    this run is allowed to evaluate (the caller checks the capability). They
    pass the lifecycle gate like an explicit choice but are marked, so a policy
    can keep them out of public selection.
    """
    by_id = {e.id: e for e in catalog.entries}
    explicit = set(explicit_selection_ids(policy))
    if explicit_model:
        explicit.add(catalog.resolve(explicit_model) or explicit_model)
    explicit_ids = frozenset(explicit)
    dogfood = frozenset(catalog.resolve(m) or m for m in dogfood_ids)
    blocked = frozenset(catalog.resolve(m) or m for m in blocked_model_ids)
    required = frozenset(required_capabilities)
    lifecycles = CLASS_LIFECYCLES | eligible_lifecycles(policy)
    constraint = EXECUTOR_CONSTRAINTS.get(harness) if harness else None
    stale = bool(catalog.snapshot.stale)

    available = build_available_pool(
        availability, registry=[_pool_entry(e) for e in catalog.entries]
    )
    if policy is not None:
        available = apply_model_policy(available, policy)

    eligible: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for ma in available:
        mid = ma.entry.id
        entry = by_id[mid]
        is_explicit = mid in explicit_ids
        is_dogfood = mid in dogfood
        reason: str | None = None
        if not ma.available:
            reason = ma.reason or REASON_NO_API_KEY
        elif constraint is not None and not set(ma.via) & constraint.allowed_providers:
            reason = REASON_EXECUTOR_CONSTRAINT
        elif mid in blocked:
            reason = REASON_USER_BLOCKED
        elif entry.status == "deprecated":
            reason = REASON_STATUS_PREFIX + "deprecated"
        elif entry.routing_eligibility == "blocked":
            reason = REASON_ELIGIBILITY_BLOCKED
        elif entry.status == "floating_alias" and not is_explicit:
            reason = REASON_STATUS_PREFIX + "floating_alias"
        elif (
            entry.status == "unlisted" and not is_explicit and not stale
            and set(ma.via) <= {"openrouter"}
        ):
            # A fresh snapshot from the only provider that could dispatch it
            # does not list it. A direct provider key would still reach it, a
            # stale cache proves nothing, and an explicit choice is honoured.
            reason = REASON_UNLISTED_ON_PROVIDER
        elif entry.lifecycle not in lifecycles and not is_explicit and not is_dogfood:
            reason = (
                REASON_NOT_PROMOTED
                if not entry.curated
                else REASON_LIFECYCLE_PREFIX + entry.lifecycle
            )
        elif required - set(entry.capability_tags):
            reason = REASON_MISSING_CAPABILITY_PREFIX + ",".join(
                sorted(required - set(entry.capability_tags))
            )
        elif min_context_tokens is not None and (
            entry.context_length is None or entry.context_length < min_context_tokens
        ):
            reason = REASON_CONTEXT_TOO_SMALL
        if reason is None:
            eligible.append(Candidate(
                mid, ma.via, is_explicit, entry,
                promotion_state=promotion_state(entry, dogfood=dogfood, snapshot_stale=stale),
                dogfood=is_dogfood,
            ))
        else:
            rejected.append((mid, reason))

    return CandidateSet(
        eligible=tuple(sorted(eligible, key=lambda c: c.model_id)),
        rejected=tuple(sorted(rejected)),
        catalog=catalog,
        available_providers=availability.detected,
        harness=harness,
        explicit_ids=explicit_ids,
        dogfood_ids=dogfood,
    )
