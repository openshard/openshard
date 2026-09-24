"""Model policy config v1: user/team control over the routable pool.

This module sits between provider availability detection and the final
routable pool construction. It applies user-declared policy rules on top of
what is technically available, without touching provider detection or executor
constraint logic.

Policy filter order inside apply_model_policy():
  0. Upstream exclusions preserved — models already unavailable
     (access_restricted, no_api_key, executor_constraint) pass through
     unchanged. Policy never overwrites these reasons.
  1. custom_roster  - restrict to roster when mode == "custom_roster"
  2. blocked_models - wins over allowed_models
  3. allowed_models - restrict to explicit list (empty = allow all)
  4. blocked_providers - vendor prefix exclusion
  5. allowed_providers - vendor prefix allowlist (empty = allow all)
  6. max_cost_class - exclude models above cost cap (None = no cap)
  7. allow_openrouter_wide - exclude OpenRouter-only models when False

Lifecycle gates (allow_specialist, allow_experimental, allow_watchlist,
allow_deprecated, allow_open_weight, allow_fallback) are NOT applied here.
They are expressed via _eligible_lifecycles() and consumed by
provider_availability.build_routable_pool(). This avoids duplicating the
lifecycle filter logic.

Access-restricted models (RESTRICTED_MODEL_IDS in provider_availability) are
excluded upstream in build_available_pool() before policy runs. Policy
cannot re-admit them, even with allow_watchlist=True or explicit
allowed_models. This is intentional and documented.

provider_family mode is reserved for a future release. When encountered it
is treated as auto with a warning; no silent failure.
"""
from __future__ import annotations

import logging
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field

from openshard.models.registry import all_models
from openshard.routing.provider_availability import ModelAvailability

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stable reason tokens (recorded in Shard metadata — never change these)
# ---------------------------------------------------------------------------

REASON_POLICY_BLOCKED_MODEL = "policy:blocked_model"
REASON_POLICY_NOT_ALLOWED_MODEL = "policy:not_in_allowed_models"
REASON_POLICY_BLOCKED_PROVIDER = "policy:blocked_provider"
REASON_POLICY_NOT_ALLOWED_PROVIDER = "policy:not_in_allowed_providers"
REASON_POLICY_COST_CLASS = "policy:cost_class_exceeded"
REASON_POLICY_OPENROUTER_WIDE = "policy:openrouter_wide_not_allowed"
REASON_POLICY_CUSTOM_ROSTER = "policy:not_in_custom_roster"

# Cost class ordering: free < tiny < cheap < mid < expensive.
# unknown maps to the highest value so it is excluded when a cap is set
# (explicit cost unknown is treated as potentially expensive).
# Document this in user-facing config comments.
COST_CLASS_ORDER: dict[str, int] = {
    "free": 0,
    "tiny": 1,
    "cheap": 2,
    "mid": 3,
    "expensive": 4,
    "unknown": 5,
}

_VALID_MODES = frozenset({"auto", "provider_family", "custom_roster"})


# ---------------------------------------------------------------------------
# Policy dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPolicyConfig:
    """Declarative user/team policy for model routing.

    All defaults preserve the behaviour of OpenShard before model policy was
    introduced:
    - No allow/block lists → no filtering beyond provider availability.
    - max_cost_class=None → no cost cap.
    - allow_specialist/experimental/watchlist/deprecated=False → only
      active_default lifecycle models enter the routable pool (unchanged from
      ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES).
    - allow_open_weight=False / allow_fallback=False → matches current
      ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES = frozenset({"active_default"}).
    - allow_openrouter_wide=True → OpenRouter reaches all available models.
    """

    mode: str = "auto"
    allowed_models: frozenset[str] = field(default_factory=frozenset)
    blocked_models: frozenset[str] = field(default_factory=frozenset)
    allowed_providers: frozenset[str] = field(default_factory=frozenset)
    blocked_providers: frozenset[str] = field(default_factory=frozenset)
    max_cost_class: str | None = None  # None = no cap
    allow_specialist: bool = False
    allow_experimental: bool = False
    allow_watchlist: bool = False
    allow_deprecated: bool = False
    allow_open_weight: bool = False  # matches ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES
    allow_fallback: bool = False     # matches ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES
    allow_openrouter_wide: bool = True
    custom_roster_models: frozenset[str] = field(default_factory=frozenset)
    custom_roster_name: str = "default"
    # Routing-class pins from ``models.routing_classes`` as sorted
    # (class_name, canonical_model_id) pairs. A pin is explicit selection: it
    # may name a discovery-only model and bypasses the lifecycle gate.
    class_pins: tuple[tuple[str, str], ...] = ()

    @property
    def class_pin_map(self) -> dict[str, str]:
        return dict(self.class_pins)


def explicit_selection_ids(policy: ModelPolicyConfig | None) -> frozenset[str]:
    """Model ids the user named explicitly, which bypass the lifecycle gate.

    Class pins always count. Custom-roster ids count only when they are
    discovery-only (not in the curated registry): curated roster models keep
    their lifecycle gates (``allow_*`` flags) exactly as before, while a
    discovered model has no other way in. Access restrictions still apply.
    """
    if policy is None:
        return frozenset()
    ids = {mid for _, mid in policy.class_pins}
    if policy.mode == "custom_roster":
        known = {e.id for e in all_models()}
        ids |= {m for m in policy.custom_roster_models if m not in known}
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Lifecycle expansion (consumed by build_routable_pool)
# ---------------------------------------------------------------------------


def eligible_lifecycles(policy: ModelPolicyConfig | None) -> frozenset[str]:
    """Return the lifecycle set that build_routable_pool should treat as eligible.

    When policy is None, returns the static ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES
    so callers without policy get the same behaviour as before this module
    existed.
    """
    from openshard.models.registry import ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES

    if policy is None:
        return ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES
    base: set[str] = {"active_default"}
    if policy.allow_specialist:
        base.add("active_specialist")
    if policy.allow_experimental:
        base.add("experimental")
    if policy.allow_watchlist:
        base.add("watchlist")
    if policy.allow_deprecated:
        base.add("deprecated")
    if policy.allow_open_weight:
        base.add("open_weight")
    if policy.allow_fallback:
        base.add("fallback")
    return frozenset(base)


# ---------------------------------------------------------------------------
# Vendor prefix extraction
# ---------------------------------------------------------------------------


def _model_vendor(model_id: str) -> str:
    """Vendor key from a model id; tolerates the ``~`` fallback-alias prefix.

    Uses the model ID prefix (e.g. ``anthropic`` from
    ``anthropic/claude-sonnet-4.6``), NOT entry.provider, because provider
    display names are inconsistent across registry entries.
    """
    return model_id.lstrip("~").split("/", 1)[0]


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def model_policy_from_config(config: dict) -> ModelPolicyConfig:
    """Parse the ``models`` section of *config* into a :class:`ModelPolicyConfig`.

    Raises :exc:`ValueError` with a clear message for:
    - Unknown model IDs in allowed_models or blocked_models.
    - Unknown mode value.

    Never raises for missing keys (all defaults apply).
    ``provider_family`` mode is reserved; logs a warning and is treated as
    ``auto``.
    """
    raw = config.get("models") or {}
    if not isinstance(raw, dict):
        raw = {}

    known_ids: frozenset[str] = frozenset(e.id for e in all_models())
    catalog_box: list = []

    def _canonical(model_id: str) -> str | None:
        """Registry id as-is; else resolve id/alias via the cached catalog.

        The catalog is read from the local cache only (never the network) and
        only when an id is not a curated registry id.
        """
        if model_id in known_ids:
            return model_id
        if not catalog_box:
            from openshard.models.catalog import load_catalog

            catalog_box.append(load_catalog(refresh="never"))
        return catalog_box[0].resolve(model_id)

    def _canonicalize(ids: Iterable[str], key: str) -> frozenset[str]:
        resolved: set[str] = set()
        unknown: list[str] = []
        for mid in ids:
            canonical = _canonical(mid)
            if canonical is None:
                unknown.append(mid)
            else:
                resolved.add(canonical)
        if unknown:
            sample = sorted(unknown)[:3]
            raise ValueError(
                f"models.{key} contains unknown model ID(s): {sample}. "
                "Check openshard models list (or openshard models catalog "
                "after openshard models sync-openrouter) for valid IDs."
            )
        return frozenset(resolved)

    def _parse_model_set(key: str) -> frozenset[str]:
        val = raw.get(key) or []
        if not isinstance(val, (list, tuple)):
            val = list(val)
        return _canonicalize((str(v) for v in val if v), key)

    def _parse_provider_set(key: str) -> frozenset[str]:
        val = raw.get(key) or []
        if not isinstance(val, (list, tuple)):
            val = list(val)
        return frozenset(str(v) for v in val if v)

    allowed_models = _parse_model_set("allowed_models")
    blocked_models = _parse_model_set("blocked_models")
    allowed_providers = _parse_provider_set("allowed_providers")
    blocked_providers = _parse_provider_set("blocked_providers")

    raw_mode = str(raw.get("mode", "auto"))
    if raw_mode not in _VALID_MODES:
        raise ValueError(
            f"models.mode '{raw_mode}' is not valid. "
            f"Choose one of: {sorted(_VALID_MODES)}"
        )
    mode = raw_mode
    if mode == "provider_family":
        warnings.warn(
            "models.mode='provider_family' is reserved for a future release. "
            "Treating as 'auto'.",
            stacklevel=2,
        )
        mode = "auto"

    raw_max_cost = raw.get("max_cost_class")
    if raw_max_cost is not None:
        raw_max_cost = str(raw_max_cost)
        if raw_max_cost not in COST_CLASS_ORDER:
            raise ValueError(
                f"models.max_cost_class '{raw_max_cost}' is not valid. "
                f"Choose one of: {sorted(COST_CLASS_ORDER)} or null."
            )

    roster_raw = raw.get("custom_roster") or {}
    if not isinstance(roster_raw, dict):
        roster_raw = {}
    roster_models_raw = roster_raw.get("models") or []
    if not isinstance(roster_models_raw, (list, tuple)):
        roster_models_raw = []
    roster_models = _canonicalize(
        (str(m) for m in roster_models_raw if m), "custom_roster.models"
    )

    class_pins = _parse_class_pins(raw.get("routing_classes"), _canonical)

    return ModelPolicyConfig(
        mode=mode,
        allowed_models=allowed_models,
        blocked_models=blocked_models,
        allowed_providers=allowed_providers,
        blocked_providers=blocked_providers,
        max_cost_class=raw_max_cost,
        allow_specialist=bool(raw.get("allow_specialist", False)),
        allow_experimental=bool(raw.get("allow_experimental", False)),
        allow_watchlist=bool(raw.get("allow_watchlist", False)),
        allow_deprecated=bool(raw.get("allow_deprecated", False)),
        allow_open_weight=bool(raw.get("allow_open_weight", False)),
        allow_fallback=bool(raw.get("allow_fallback", False)),
        allow_openrouter_wide=bool(raw.get("allow_openrouter_wide", True)),
        custom_roster_models=roster_models,
        custom_roster_name=str(roster_raw.get("name", "default")),
        class_pins=class_pins,
    )


def _parse_class_pins(raw_pins, canonical) -> tuple[tuple[str, str], ...]:
    """Parse ``models.routing_classes: {class_name: model_id}``.

    Unknown class names, unrecognised ids and ids lacking the class's required
    capabilities are config errors (ValueError). A pin to a model that is now
    deprecated is dropped with a warning instead: expiry can happen after the
    config was written and must not break runs; routing falls back to the
    class's normal selection.
    """
    from openshard.models.catalog import curated_catalog, load_catalog
    from openshard.routing.routing_classes import ROUTING_CLASSES, pin_rejection_reason

    if not raw_pins:
        return ()
    if not isinstance(raw_pins, dict):
        raise ValueError("models.routing_classes must be a mapping of class name to model ID.")
    pins: list[tuple[str, str]] = []
    discovered_catalog = None
    for class_name, model_id in sorted(raw_pins.items()):
        if not model_id:
            continue
        cls = ROUTING_CLASSES.get(str(class_name))
        if cls is None:
            raise ValueError(
                f"models.routing_classes has unknown class '{class_name}'. "
                f"Choose from: {sorted(ROUTING_CLASSES)}"
            )
        mid = canonical(str(model_id))
        if mid is None:
            raise ValueError(
                f"models.routing_classes.{class_name} names unknown model ID "
                f"'{model_id}'. Run openshard models sync-openrouter to discover new models."
            )
        entry = curated_catalog().get(mid)
        if entry is None:
            if discovered_catalog is None:
                discovered_catalog = load_catalog(refresh="never")
            entry = discovered_catalog.get(mid)
        reason = pin_rejection_reason(cls, entry)
        if reason == "deprecated_or_blocked":
            warnings.warn(
                f"models.routing_classes.{class_name}: {mid} is deprecated or blocked; "
                "ignoring the pin and using the class default.",
                stacklevel=2,
            )
            continue
        if reason is not None:
            raise ValueError(
                f"models.routing_classes.{class_name}: {mid} cannot serve this class ({reason})."
            )
        pins.append((str(class_name), mid))
    return tuple(pins)


# ---------------------------------------------------------------------------
# Policy filtering
# ---------------------------------------------------------------------------


def apply_model_policy(
    available: list[ModelAvailability],
    policy: ModelPolicyConfig,
) -> list[ModelAvailability]:
    """Apply *policy* to the *available* pool from build_available_pool().

    Returns a new list. Models that were already excluded upstream
    (``available=False``) are passed through unchanged — policy never
    overwrites upstream exclusion reasons such as ``access_restricted``,
    ``no_api_key``, or ``executor_constraint``. The real reason a model was
    unavailable must remain visible in the Shard record.

    Policy filtering only applies to models that are currently available
    (``available=True``). Excluded models receive ``available=False`` with a
    ``REASON_POLICY_*`` reason token.

    Lifecycle gates are NOT applied here — they live in
    ``eligible_lifecycles()`` and are consumed by ``build_routable_pool()``.
    """
    result: list[ModelAvailability] = []
    for ma in available:
        # Preserve upstream exclusion reasons. Policy must never hide why a
        # model was truly unavailable (access_restricted, no_api_key, etc.).
        if not ma.available:
            result.append(ma)
            continue

        entry = ma.entry
        mid = entry.id
        vendor = _model_vendor(mid)

        policy_reason: str | None = None

        # 1. Custom roster gate
        if policy.mode == "custom_roster":
            if mid not in policy.custom_roster_models:
                policy_reason = REASON_POLICY_CUSTOM_ROSTER

        # 2. Blocked models (wins over allowed_models)
        if policy_reason is None and mid in policy.blocked_models:
            policy_reason = REASON_POLICY_BLOCKED_MODEL

        # 3. Allowed models (non-empty list = explicit allowlist)
        if policy_reason is None and policy.allowed_models:
            if mid not in policy.allowed_models:
                policy_reason = REASON_POLICY_NOT_ALLOWED_MODEL

        # 4. Blocked providers
        if policy_reason is None and vendor in policy.blocked_providers:
            policy_reason = REASON_POLICY_BLOCKED_PROVIDER

        # 5. Allowed providers (non-empty = explicit allowlist)
        if policy_reason is None and policy.allowed_providers:
            if vendor not in policy.allowed_providers:
                policy_reason = REASON_POLICY_NOT_ALLOWED_PROVIDER

        # 6. Max cost class
        if policy_reason is None and policy.max_cost_class is not None:
            model_cost = entry.cost_class or "unknown"
            cap = COST_CLASS_ORDER.get(policy.max_cost_class, 4)
            if COST_CLASS_ORDER.get(model_cost, 5) > cap:
                policy_reason = REASON_POLICY_COST_CLASS

        # 7. OpenRouter-wide restriction
        if policy_reason is None and not policy.allow_openrouter_wide:
            if ma.via == ("openrouter",):
                policy_reason = REASON_POLICY_OPENROUTER_WIDE

        if policy_reason is not None:
            result.append(ModelAvailability(entry, False, (), policy_reason))
        else:
            result.append(ma)

    return result


# ---------------------------------------------------------------------------
# Policy summary for Shard metadata
# ---------------------------------------------------------------------------


def policy_summary(policy: ModelPolicyConfig) -> dict:
    """Compact, JSON-serialisable Shard metadata for *policy*.

    Never records model ID lists or provider names to avoid leaking
    potentially sensitive routing configuration. Counts only.
    """
    return {
        "mode": policy.mode,
        "blocked_models_count": len(policy.blocked_models),
        "allowed_models_count": len(policy.allowed_models),
        "blocked_providers_count": len(policy.blocked_providers),
        "allowed_providers_count": len(policy.allowed_providers),
        "max_cost_class": policy.max_cost_class,
        "allow_specialist": policy.allow_specialist,
        "allow_experimental": policy.allow_experimental,
        "allow_watchlist": policy.allow_watchlist,
        "allow_deprecated": policy.allow_deprecated,
        "allow_open_weight": policy.allow_open_weight,
        "allow_fallback": policy.allow_fallback,
        "custom_roster_name": (
            policy.custom_roster_name if policy.mode == "custom_roster" else None
        ),
        "class_pins_count": len(policy.class_pins),
    }
