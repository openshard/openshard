"""Provider-aware routing eligibility v1: known -> available -> routable.

This module narrows the model registry in three honest, explainable steps:

1. **Known** - every entry in the curated registry.
2. **Available** - models actually callable with the API keys present in the
   environment, given the provider clients OpenShard has implemented today
   (OpenRouter, Anthropic, OpenAI).
3. **Routable** - available models that are also lifecycle-eligible for
   default routing, allowed by the active executor, and not user-blocked.

Everything here is read-only and deterministic: this branch computes and
records eligibility, it does not enforce it. routing/engine.py,
routing/model_resolver.py, and dispatch are unchanged.

V1 assumptions (deliberate, documented):

* **OpenRouter-wide availability is offline/static.** When an OpenRouter key
  is present, every registry model is assumed reachable because registry IDs
  use the OpenRouter ``vendor/model`` form. Live verification against
  ``https://openrouter.ai/api/v1/models`` is future work.
* **Key presence is not key validity.** A present-but-invalid key still
  counts as "available"; no network calls are made.
* **Only implemented provider clients count.** Env vars for providers
  without a client (e.g. ``DEEPSEEK_API_KEY``) do not expand the pool.
  ``_DIRECT_VENDORS`` is the single place to extend when a direct client
  (DeepSeek, Moonshot/Kimi, Google, xAI, Qwen, ...) is actually added.
* **Claude-family routing stays conservative.** No assumption is made that
  Anthropic-keyed tooling can call non-Anthropic models; a direct Anthropic
  key reaches ``anthropic/*`` models only.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from openshard.models.registry import (
    ModelEntry,
    all_models,
)

ELIGIBILITY_VERSION = "provider_eligibility_v1"

# Providers with an implemented client today (see providers/manager.py).
# Mirrors the canonical key-detection tuple in config/settings.py.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("openrouter", "anthropic", "openai")

# Direct (non-aggregator) providers mapped to the model-id vendor prefixes
# they can call. Extend this dict when a new direct provider client lands;
# nothing else in this module needs to change.
_DIRECT_VENDORS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"anthropic"}),
    "openai": frozenset({"openai"}),
}

# Access-restricted models: never available or routable by default, even with
# every supported key present. Currently exactly one (limited availability,
# approved customers only - see its registry entry).
RESTRICTED_MODEL_IDS: frozenset[str] = frozenset({"anthropic/claude-mythos-5"})

# Exclusion reasons (stable tokens; recorded in Shard metadata).
REASON_NO_API_KEY = "no_api_key"
REASON_ACCESS_RESTRICTED = "access_restricted"
REASON_LIFECYCLE_PREFIX = "lifecycle:"
REASON_EXECUTOR_CONSTRAINT = "executor_constraint"
REASON_USER_BLOCKED = "user_blocked"


# ---------------------------------------------------------------------------
# 1. Provider detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderAvailability:
    """Which supported providers have an API key present. Presence only -
    key values are never stored."""

    detected: tuple[str, ...]  # subset of SUPPORTED_PROVIDERS, priority order
    openrouter: bool
    anthropic: bool
    openai: bool
    source: str = "env"


def detect_provider_availability(
    env: Mapping[str, str] | None = None,
) -> ProviderAvailability:
    """Detect available providers from *env* (default ``os.environ``).

    Reads only the canonical key vars from ``config.settings.KEY_VARS``;
    unrecognised provider env vars (e.g. ``DEEPSEEK_API_KEY``) are ignored
    until a direct client exists. Never raises.
    """
    from openshard.config.settings import KEY_VARS

    if env is None:
        env = os.environ
    detected = tuple(
        provider for env_var, provider in KEY_VARS if env.get(env_var, "")
    )
    return ProviderAvailability(
        detected=detected,
        openrouter="openrouter" in detected,
        anthropic="anthropic" in detected,
        openai="openai" in detected,
    )


# ---------------------------------------------------------------------------
# 2. Executor constraints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutorConstraint:
    """Which providers an executor can dispatch through.

    Derived from executor implementations, not aspiration: opencode prefixes
    model ids with ``openrouter/`` and is OpenRouter-only; native, direct and
    staged go through ProviderManager and accept any configured provider.
    Binary presence (e.g. ``shutil.which("opencode")``) remains the caller's
    job, as in run/pipeline.py.
    """

    executor: str
    allowed_providers: frozenset[str]
    requires_binary: str | None = None


_ALL_PROVIDERS = frozenset(SUPPORTED_PROVIDERS)

EXECUTOR_CONSTRAINTS: dict[str, ExecutorConstraint] = {
    "native": ExecutorConstraint("native", _ALL_PROVIDERS),
    "direct": ExecutorConstraint("direct", _ALL_PROVIDERS),
    "staged": ExecutorConstraint("staged", _ALL_PROVIDERS),
    "opencode": ExecutorConstraint(
        "opencode", frozenset({"openrouter"}), requires_binary="opencode"
    ),
}


# ---------------------------------------------------------------------------
# 3. Available pool (known -> available)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelAvailability:
    """One registry model's callability verdict."""

    entry: ModelEntry
    available: bool
    via: tuple[str, ...]  # providers that can call it, priority order
    reason: str | None  # exclusion reason when unavailable


def _vendor_prefix(model_id: str) -> str:
    """Vendor key from a model id; tolerates the ``~`` fallback-alias prefix."""
    return model_id.lstrip("~").split("/", 1)[0]


def build_available_pool(
    avail: ProviderAvailability,
    *,
    registry: list[ModelEntry] | None = None,
) -> list[ModelAvailability]:
    """Classify every known model as available or not. Never raises.

    Sorted by model id for determinism. With no keys present, all entries
    come back ``available=False`` with reason ``no_api_key`` so diagnosis
    stays possible.
    """
    entries = sorted(all_models() if registry is None else registry, key=lambda e: e.id)
    direct_prefixes: set[str] = set()
    for provider in avail.detected:
        direct_prefixes.update(_DIRECT_VENDORS.get(provider, frozenset()))

    result: list[ModelAvailability] = []
    for entry in entries:
        if entry.id in RESTRICTED_MODEL_IDS:
            result.append(
                ModelAvailability(entry, False, (), REASON_ACCESS_RESTRICTED)
            )
            continue
        via: list[str] = []
        if avail.openrouter:
            via.append("openrouter")
        vendor = _vendor_prefix(entry.id)
        if vendor in direct_prefixes:
            via.append(vendor)
        if via:
            result.append(ModelAvailability(entry, True, tuple(via), None))
        else:
            result.append(ModelAvailability(entry, False, (), REASON_NO_API_KEY))
    return result


# ---------------------------------------------------------------------------
# 4. Routable pool (available -> routable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutablePool:
    """Default-routable models plus per-model exclusion reasons."""

    routable: tuple[ModelEntry, ...]
    excluded: tuple[tuple[str, str], ...]  # (model_id, reason)
    available_providers: tuple[str, ...]
    executor: str | None
    eligibility_version: str = ELIGIBILITY_VERSION
    blocked_model_ids: frozenset[str] = field(default_factory=frozenset)


def build_routable_pool(
    avail: ProviderAvailability,
    *,
    executor: str | None = None,
    blocked_model_ids: frozenset[str] = frozenset(),
    registry: list[ModelEntry] | None = None,
    policy=None,  # ModelPolicyConfig | None — imported lazily to avoid circularity
) -> RoutablePool:
    """Narrow available models to the default-routable pool. Never raises.

    Filter chain, each step recording an exclusion reason:
    availability -> [policy] -> lifecycle eligibility -> executor constraint
    -> user block.

    When *policy* is None the behaviour is identical to the pre-policy
    implementation: only ``active_default`` lifecycle models are eligible
    and no model/provider/cost filtering is applied.

    When *policy* is provided:
    - ``apply_model_policy()`` runs on the available pool before the
      lifecycle gate, so policy can expand or restrict the candidate set
      (e.g. ``allow_specialist=True`` admits ``active_specialist`` models).
    - The eligible lifecycle set is derived from *policy* via
      ``eligible_lifecycles(policy)`` rather than the static
      ``ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES``.

    Restricted models (RESTRICTED_MODEL_IDS) are excluded inside
    ``build_available_pool()`` before policy runs and cannot be re-admitted
    by policy.
    """
    from openshard.routing.model_policy import (
        apply_model_policy,
        eligible_lifecycles,
        explicit_selection_ids,
    )

    constraint = EXECUTOR_CONSTRAINTS.get(executor) if executor else None
    eligible = eligible_lifecycles(policy)
    explicit = explicit_selection_ids(policy)

    entries = list(all_models() if registry is None else registry)
    entries += _explicit_discovered_entries(explicit, {e.id for e in entries})

    available = build_available_pool(avail, registry=entries)
    if policy is not None:
        available = apply_model_policy(available, policy)

    routable: list[ModelEntry] = []
    excluded: list[tuple[str, str]] = []

    for ma in available:
        entry = ma.entry
        if not ma.available:
            excluded.append((entry.id, ma.reason or REASON_NO_API_KEY))
            continue
        if entry.lifecycle not in eligible and entry.id not in explicit:
            excluded.append((entry.id, f"{REASON_LIFECYCLE_PREFIX}{entry.lifecycle}"))
            continue
        if constraint is not None and not set(ma.via) & constraint.allowed_providers:
            excluded.append((entry.id, REASON_EXECUTOR_CONSTRAINT))
            continue
        if entry.id in blocked_model_ids:
            excluded.append((entry.id, REASON_USER_BLOCKED))
            continue
        routable.append(entry)

    return RoutablePool(
        routable=tuple(routable),
        excluded=tuple(excluded),
        available_providers=avail.detected,
        executor=executor,
        blocked_model_ids=blocked_model_ids,
    )


def _explicit_discovered_entries(
    explicit: frozenset[str], known: set[str]
) -> list[ModelEntry]:
    """Pool entries for explicitly selected discovery-only models.

    Looked up in the cached catalog only (no network). A model the cache no
    longer lists is skipped; the run then routes without it.
    """
    missing = sorted(explicit - known)
    if not missing:
        return []
    try:
        from openshard.models.catalog import load_catalog

        catalog = load_catalog(refresh="never")
    except Exception:
        return []
    out: list[ModelEntry] = []
    for mid in missing:
        entry = catalog.get(mid)
        if entry is not None:
            out.append(entry.to_model_entry())
    return out


def routing_constraints_metadata(pool: RoutablePool) -> dict:
    """Compact, bounded, JSON-serialisable Shard metadata for *pool*.

    Counts only - never model lists or anything key-derived. ``lifecycle:*``
    reasons are bucketed under a single ``lifecycle`` count.
    """
    counts: dict[str, int] = {}
    for _model_id, reason in pool.excluded:
        bucket = (
            "lifecycle" if reason.startswith(REASON_LIFECYCLE_PREFIX) else reason
        )
        counts[bucket] = counts.get(bucket, 0) + 1
    return {
        "executor": pool.executor,
        "routable_pool_size": len(pool.routable),
        "excluded_counts": counts,
        "eligibility_version": pool.eligibility_version,
    }
