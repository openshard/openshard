"""Organisation policy fetched from the OpenShard Platform at OSN run start.

A linked organisation policy is a hard control-plane input, not a UI hint.
OSN reads it once before work begins, combines it with repository policy using
stricter-wins semantics, then freezes that effective policy for the run.

Safety rules:
- no Platform link means normal local-only behaviour;
- a linked organisation with no saved policy also runs local-only;
- a linked organisation whose policy cannot be refreshed fails closed;
- the full organisation policy is never copied into a Receipt. Receipts keep
  only version/hash/source and the fingerprint of the effective policy.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Callable

from openshard.osn.budget import BudgetLimits
from openshard.routing.model_policy import (
    COST_CLASS_ORDER,
    ModelPolicyConfig,
    model_policy_from_config,
)
from openshard.sync.config import PlatformLink, resolve_link, sync_disabled
from openshard.sync.transport import TOTAL_TIMEOUT_SECONDS

SOURCE_FRESH = "fresh"
SOURCE_NONE = "none"
REASON_NO_POLICY = "no_organisation_policy"
REASON_UNAVAILABLE = "platform_policy_unavailable"
REASON_SYNC_DISABLED = "platform_sync_disabled"

_MAX_BODY_BYTES = 256 * 1024
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COST_CLASSES = frozenset({"free", "tiny", "cheap", "mid", "expensive"})
_MODEL_KEYS = frozenset({
    "allowed_models", "blocked_models", "allowed_providers", "blocked_providers",
    "max_cost_class", "allow_specialist", "allow_experimental", "allow_watchlist",
    "allow_deprecated", "allow_open_weight", "allow_fallback", "allow_openrouter_wide",
})
_BUDGET_KEYS = ("max_spend_usd", "max_attempts", "max_commands", "max_writes")

PolicyFetcher = Callable[[PlatformLink], "OrganisationPolicyState | None"]


class PolicyUnavailable(RuntimeError):
    """The linked organisation's policy could not be refreshed safely."""


@dataclass(frozen=True)
class OrganisationPolicyState:
    organisation_id: str
    version: int | None
    policy_hash: str | None
    document: dict[str, Any] | None
    source: str
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.document is not None and self.version is not None and self.policy_hash is not None

    def receipt_record(
        self,
        *,
        effective_policy_hash: str | None,
        repository_override_applied: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "organisation_policy_version": self.version,
            "organisation_policy_hash": self.policy_hash,
            "source": self.source,
            "applied": self.applied,
            "repository_override_applied": bool(repository_override_applied),
            "effective_policy_hash": effective_policy_hash if self.applied else None,
            "refreshed_at_run_start": True,
            "reason": self.reason,
        }


def _valid_string_list(value: Any, *, max_items: int = 100, max_len: int = 256) -> list[str] | None:
    if not isinstance(value, list) or len(value) > max_items:
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > max_len:
            return None
        out.append(item)
    return out


def _parse_policy_document(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"schema_version", "models", "budgets"}:
        return None
    if value.get("schema_version") != 1:
        return None
    models = value.get("models")
    budgets = value.get("budgets")
    if not isinstance(models, dict) or set(models) != _MODEL_KEYS or not isinstance(budgets, dict):
        return None
    if set(budgets) != set(_BUDGET_KEYS):
        return None

    parsed_models: dict[str, Any] = {}
    for key in ("allowed_models", "blocked_models"):
        parsed = _valid_string_list(models.get(key))
        if parsed is None:
            return None
        parsed_models[key] = parsed
    for key in ("allowed_providers", "blocked_providers"):
        parsed = _valid_string_list(models.get(key), max_len=64)
        if parsed is None:
            return None
        parsed_models[key] = parsed

    max_cost = models.get("max_cost_class")
    if max_cost is not None and max_cost not in _COST_CLASSES:
        return None
    parsed_models["max_cost_class"] = max_cost
    for key in (
        "allow_specialist", "allow_experimental", "allow_watchlist", "allow_deprecated",
        "allow_open_weight", "allow_fallback", "allow_openrouter_wide",
    ):
        if not isinstance(models.get(key), bool):
            return None
        parsed_models[key] = models[key]

    parsed_budgets: dict[str, int | float | None] = {}
    for key in _BUDGET_KEYS:
        raw = budgets.get(key)
        if raw is None:
            parsed_budgets[key] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
            return None
        if key != "max_spend_usd":
            if isinstance(raw, float) and not raw.is_integer():
                return None
            raw = int(raw)
        else:
            raw = float(raw)
        parsed_budgets[key] = raw

    return {"schema_version": 1, "models": parsed_models, "budgets": parsed_budgets}


def _parse_response(body: bytes, *, organisation_id: str) -> OrganisationPolicyState | None:
    try:
        data = json.loads(body[:_MAX_BODY_BYTES].decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("organisation_id") != organisation_id:
        return None

    version = data.get("version")
    policy_hash = data.get("hash")
    document = data.get("policy")

    if document is None:
        if version is not None or policy_hash is not None:
            return None
        return OrganisationPolicyState(
            organisation_id=organisation_id,
            version=None,
            policy_hash=None,
            document=None,
            source=SOURCE_NONE,
            reason=REASON_NO_POLICY,
        )

    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    if not isinstance(policy_hash, str) or not _HASH_RE.match(policy_hash):
        return None
    parsed = _parse_policy_document(document)
    if parsed is None:
        return None
    return OrganisationPolicyState(
        organisation_id=organisation_id,
        version=version,
        policy_hash=policy_hash,
        document=parsed,
        source=SOURCE_FRESH,
    )


def fetch_organisation_policy(
    link: PlatformLink,
    *,
    user_agent: str = "openshard",
    timeout: float = TOTAL_TIMEOUT_SECONDS,
) -> OrganisationPolicyState | None:
    """Fetch the current policy for exactly this linked organisation. Never raises."""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            return None

    request = urllib.request.Request(
        link.policy_url(),
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {link.api_key}",
            "User-Agent": user_agent,
        },
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - endpoint already validated
            if int(response.status) != 200:
                return None
            body = response.read(_MAX_BODY_BYTES + 1)
    except Exception:
        return None
    if len(body) > _MAX_BODY_BYTES:
        return None
    return _parse_response(body, organisation_id=link.organisation_id)


def resolve_organisation_policy(
    env: dict | os._Environ | None = None,
    *,
    fetcher: PolicyFetcher | None = None,
) -> OrganisationPolicyState | None:
    """Fresh policy snapshot for a new OSN run.

    None means there is no Platform link. Once a link exists, inability to
    refresh is a hard failure so a local switch or outage cannot silently
    bypass a policy that may exist on the Platform.
    """
    link = resolve_link(env)
    if link is None:
        return None
    if sync_disabled(env, None) is not None:
        raise PolicyUnavailable(REASON_SYNC_DISABLED)
    try:
        state = (fetcher or fetch_organisation_policy)(link)
    except Exception:
        state = None
    if state is None or state.organisation_id != link.organisation_id:
        raise PolicyUnavailable(REASON_UNAVAILABLE)
    return state


def _strict_allowlist(a: frozenset[str], b: frozenset[str], *, label: str) -> frozenset[str]:
    if not a:
        return b
    if not b:
        return a
    overlap = a & b
    if not overlap:
        raise ValueError(f"organisation and repository {label} allowlists do not overlap")
    return frozenset(overlap)


def _stricter_cost(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if COST_CLASS_ORDER[a] <= COST_CLASS_ORDER[b] else b


def combine_model_policy(
    repository_config: dict[str, Any],
    organisation: OrganisationPolicyState | None,
) -> ModelPolicyConfig:
    """Repository + organisation model policy, with every restriction combining stricter."""
    local = model_policy_from_config(repository_config)
    if organisation is None or not organisation.applied:
        return local
    assert organisation.document is not None
    org = model_policy_from_config({"models": organisation.document["models"]})
    return replace(
        local,
        allowed_models=_strict_allowlist(local.allowed_models, org.allowed_models, label="model"),
        blocked_models=frozenset(local.blocked_models | org.blocked_models),
        allowed_providers=_strict_allowlist(local.allowed_providers, org.allowed_providers, label="provider"),
        blocked_providers=frozenset(local.blocked_providers | org.blocked_providers),
        max_cost_class=_stricter_cost(local.max_cost_class, org.max_cost_class),
        allow_specialist=local.allow_specialist and org.allow_specialist,
        allow_experimental=local.allow_experimental and org.allow_experimental,
        allow_watchlist=local.allow_watchlist and org.allow_watchlist,
        allow_deprecated=local.allow_deprecated and org.allow_deprecated,
        allow_open_weight=local.allow_open_weight and org.allow_open_weight,
        allow_fallback=local.allow_fallback and org.allow_fallback,
        allow_openrouter_wide=local.allow_openrouter_wide and org.allow_openrouter_wide,
    )


def _organisation_budget(organisation: OrganisationPolicyState | None) -> BudgetLimits:
    if organisation is None or not organisation.applied:
        return BudgetLimits()
    assert organisation.document is not None
    raw = organisation.document["budgets"]
    return BudgetLimits(
        max_spend_usd=raw.get("max_spend_usd"),
        max_attempts=raw.get("max_attempts"),
        max_commands=raw.get("max_commands"),
        max_writes=raw.get("max_writes"),
    )


def _minimum(a: int | float | None, b: int | float | None):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def combine_budget_limits(
    repository_config: dict[str, Any],
    organisation: OrganisationPolicyState | None,
) -> tuple[BudgetLimits, BudgetLimits, BudgetLimits]:
    """Return (effective, repository, organisation) hard limits."""
    local = BudgetLimits.from_config(repository_config.get("agent_budgets"))
    org = _organisation_budget(organisation)
    effective = BudgetLimits(
        max_spend_usd=_minimum(local.max_spend_usd, org.max_spend_usd),
        max_attempts=_minimum(local.max_attempts, org.max_attempts),
        max_commands=_minimum(local.max_commands, org.max_commands),
        max_writes=_minimum(local.max_writes, org.max_writes),
    )
    return effective, local, org


def repository_override_present(repository_config: dict[str, Any], *, has_config_file: bool) -> bool:
    if not has_config_file:
        return False
    return isinstance(repository_config.get("models"), dict) or repository_config.get("agent_budgets") is not None


def effective_policy_hash(model_policy: ModelPolicyConfig, budget: BudgetLimits) -> str:
    """Opaque fingerprint of the complete effective policy; no policy values leave Core."""
    payload = {
        "models": {
            "mode": model_policy.mode,
            "allowed_models": sorted(model_policy.allowed_models),
            "blocked_models": sorted(model_policy.blocked_models),
            "allowed_providers": sorted(model_policy.allowed_providers),
            "blocked_providers": sorted(model_policy.blocked_providers),
            "max_cost_class": model_policy.max_cost_class,
            "allow_specialist": model_policy.allow_specialist,
            "allow_experimental": model_policy.allow_experimental,
            "allow_watchlist": model_policy.allow_watchlist,
            "allow_deprecated": model_policy.allow_deprecated,
            "allow_open_weight": model_policy.allow_open_weight,
            "allow_fallback": model_policy.allow_fallback,
            "allow_openrouter_wide": model_policy.allow_openrouter_wide,
            "custom_roster_models": sorted(model_policy.custom_roster_models),
            "class_pins": list(model_policy.class_pins),
            "dogfood_candidates": list(model_policy.dogfood_candidates),
        },
        "budgets": budget.to_dict(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def model_policy_rejection(model_id: str, policy: ModelPolicyConfig) -> str | None:
    """Policy-only rejection reason for a model OSN is about to dispatch."""
    from openshard.models.catalog import load_catalog
    from openshard.routing.model_policy import apply_model_policy
    from openshard.routing.provider_availability import (
        ModelAvailability,
        build_available_pool,
        detect_provider_availability,
    )

    catalog = load_catalog(refresh="never")
    canonical = catalog.resolve(model_id) or model_id
    entry = next((e for e in catalog.entries if e.id == canonical), None)
    vendor = canonical.lstrip("~").split("/", 1)[0]

    if entry is None:
        if policy.mode == "custom_roster" and canonical not in policy.custom_roster_models:
            return "policy:not_in_custom_roster"
        if canonical in policy.blocked_models:
            return "policy:blocked_model"
        if policy.allowed_models and canonical not in policy.allowed_models:
            return "policy:not_in_allowed_models"
        if vendor in policy.blocked_providers:
            return "policy:blocked_provider"
        if policy.allowed_providers and vendor not in policy.allowed_providers:
            return "policy:not_in_allowed_providers"
        if policy.max_cost_class is not None:
            return "policy:cost_class_exceeded"
        return None

    model_entry = entry.to_model_entry()
    availability = build_available_pool(detect_provider_availability(), registry=[model_entry])[0]
    synthetic = ModelAvailability(model_entry, True, availability.via, None)
    filtered = apply_model_policy([synthetic], policy)[0]
    return filtered.reason if not filtered.available and str(filtered.reason).startswith("policy:") else None


def enforce_models_allowed(models: list[str], policy: ModelPolicyConfig) -> None:
    for model in models:
        reason = model_policy_rejection(model, policy)
        if reason is not None:
            raise ValueError(f"model '{model}' is blocked by the effective policy ({reason})")


__all__ = [
    "OrganisationPolicyState",
    "PolicyUnavailable",
    "combine_budget_limits",
    "combine_model_policy",
    "effective_policy_hash",
    "enforce_models_allowed",
    "fetch_organisation_policy",
    "repository_override_present",
    "resolve_organisation_policy",
]
