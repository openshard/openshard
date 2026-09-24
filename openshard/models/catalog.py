"""Dynamic model catalog: discovery is dynamic, routing eligibility is controlled.

The curated registry (``openshard.models.registry``) records what OpenShard has
*evaluated*: lifecycle, tier, roles, cost class. Provider metadata (today the
OpenRouter ``/models`` list, cached locally by ``openrouter_fetcher``) records
what *exists*. This module merges the two into one normalized, read-only
:class:`ModelCatalog` so that:

* a newly released model is recognised, displayed and explicitly selectable
  (custom roster, routing-class pin) without an OpenShard release, and
* a newly released model never becomes a routing default on its own. A model
  only OpenRouter knows about gets ``lifecycle="discovered"`` and
  ``routing_eligibility="not_promoted"``; routing classes select only from
  curated lifecycles. Promotion is an explicit act (curate the entry after
  an eval, or pin it in config).

Determinism: :func:`build_catalog` is a pure function of (curated entries,
discovered snapshot, snapshot timestamp). Status derivation uses the snapshot's
own ``synced_at`` as the reference date, never the wall clock, so the same
snapshot always yields the same catalog and the same routing-class selections.

Curated routing fields are never overridden by discovered facts: discovered
pricing, context and modalities are displayed, but capability tags that feed
routing-class selection for curated models come from curated fields only.
Online and offline runs therefore route identically unless the user pins.

Refresh: :func:`load_catalog` reads the local cache and only touches the network
when asked (``refresh="auto"`` with a stale/missing cache, or ``"force"``). A
failed refresh falls back to the stale cache, then to curated-only. The run
path always uses ``refresh="never"``.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path

from openshard.models.registry import ModelEntry, all_models

CATALOG_SCHEMA_VERSION = "1"

# Cache older than this is refreshed by ``load_catalog(refresh="auto")``.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# A model whose provider expiration date is within this window is "expiring".
EXPIRING_WINDOW_DAYS = 90

# Context length at or above which a model gets the ``long_context`` tag.
LONG_CONTEXT_TOKENS = 500_000

# status: what the provider/curation says about the model's currency.
#   current         listed, no known end of life
#   preview         listed, id marks it preview/experimental/beta
#   expiring        listed, provider expiration date within the window
#   deprecated      curated as deprecated, or provider expiration has passed
#   floating_alias  ``~vendor/x-latest`` style pointer to a moving target
#   unlisted        curated, but absent from a non-empty discovery snapshot
STATUS_VALUES = frozenset(
    {"current", "preview", "expiring", "deprecated", "floating_alias", "unlisted"}
)

# discovery_source: where the entry came from.
DISCOVERY_SOURCES = frozenset({"curated", "openrouter", "curated+openrouter"})

# routing_eligibility: what routing may do with the entry without the user
# naming it. Only "default" entries can be picked by general-purpose classes;
# "specialist" entries only by specialist classes. Everything else requires
# explicit user selection.
ELIGIBILITY_VALUES = frozenset({"default", "specialist", "not_promoted", "blocked"})

# Lifecycle given to entries that only provider discovery knows about.
DISCOVERED_LIFECYCLE = "discovered"

# Snapshot origins recorded on the catalog (for display and diagnosis).
ORIGIN_CURATED_ONLY = "curated_only"
ORIGIN_CACHE = "cache"
ORIGIN_REFRESHED = "refreshed"
ORIGIN_STALE_CACHE = "stale_cache"

PRICING_SOURCE_OPENROUTER = "openrouter"
PRICING_SOURCE_STATIC = "openshard_static_snapshot"
PRICING_SOURCE_UNKNOWN = "unknown"

# Curated roles that indicate coding use; informational ``coding`` tag only.
_CODING_ROLES = frozenset(
    {
        "boilerplate", "coding", "coding_agent", "code_generation",
        "code_correction", "routine_engineering", "standard_coding",
        "small_coding_tasks", "value_worker", "agentic_engineering",
    }
)

_PREVIEW_TOKENS = frozenset({"preview", "exp", "experimental", "beta", "alpha"})

_LIFECYCLE_ELIGIBILITY: dict[str, str] = {
    "active_default": "default",
    "active_specialist": "specialist",
    "deprecated": "blocked",
}


@dataclass(frozen=True)
class CatalogPricing:
    """Per-million-token pricing with provenance."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    source: str = PRICING_SOURCE_UNKNOWN
    # ISO timestamp/date the price was observed, when known.
    as_of: str | None = None


@dataclass(frozen=True)
class CatalogEntry:
    """One normalized model record: curated policy + discovered facts."""

    id: str
    provider: str
    display_name: str
    family: str
    aliases: tuple[str, ...] = ()
    status: str = "current"
    release_date: str | None = None
    expiration_date: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    # None = not known (discovered models without supported_parameters).
    supports_tools: bool | None = None
    supports_structured_outputs: bool | None = None
    supports_reasoning: bool | None = None
    pricing: CatalogPricing = field(default_factory=CatalogPricing)
    capability_tags: tuple[str, ...] = ()
    discovery_source: str = "curated"
    # Curated lifecycle, or DISCOVERED_LIFECYCLE for discovery-only entries.
    lifecycle: str = DISCOVERED_LIFECYCLE
    routing_eligibility: str = "not_promoted"
    # Curated routing fields; "unknown"/() for discovery-only entries.
    tier: str = "unknown"
    cost_class: str = "unknown"
    latency_class: str = "unknown"
    roles: tuple[str, ...] = ()

    @property
    def curated(self) -> bool:
        return self.discovery_source != "openrouter"

    def to_model_entry(self) -> ModelEntry:
        """Project into a ``ModelEntry`` so routing pools can hold it.

        Used only for explicitly selected discovery-only models; curated
        models always use their registry entry directly.
        """
        return ModelEntry(
            id=self.id,
            display_name=self.display_name,
            provider=self.provider,
            tier=self.tier,
            roles=self.roles,
            experimental=not self.curated,
            context_length=self.context_length,
            input_modalities=self.input_modalities,
            output_modalities=self.output_modalities,
            supports_tools=bool(self.supports_tools),
            supports_structured_outputs=bool(self.supports_structured_outputs),
            supports_reasoning=bool(self.supports_reasoning),
            supports_multimodal="image" in self.input_modalities,
            latency_class=self.latency_class,
            cost_class=self.cost_class,
            source="unknown",
            lifecycle=self.lifecycle,
        )


@dataclass(frozen=True)
class CatalogSnapshotInfo:
    """Where the discovered half of the catalog came from."""

    origin: str = ORIGIN_CURATED_ONLY
    synced_at: str | None = None
    discovered_count: int = 0
    stale: bool = False
    # Refresh error text when a refresh was attempted and failed.
    error: str | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class ModelCatalog:
    entries: tuple[CatalogEntry, ...]
    snapshot: CatalogSnapshotInfo
    _index: dict[str, CatalogEntry] = field(repr=False, compare=False, default_factory=dict)
    _aliases: dict[str, str] = field(repr=False, compare=False, default_factory=dict)

    def resolve(self, model_id: str) -> str | None:
        """Return the canonical id for *model_id* or one of its aliases."""
        if not model_id:
            return None
        if model_id in self._index:
            return model_id
        return self._aliases.get(model_id.strip().lower())

    def get(self, model_id: str) -> CatalogEntry | None:
        canonical = self.resolve(model_id)
        return self._index.get(canonical) if canonical else None

    def is_recognised(self, model_id: str) -> bool:
        return self.resolve(model_id) is not None

    def discovered_only(self) -> list[CatalogEntry]:
        return [e for e in self.entries if not e.curated]

    def family_members(self, family: str) -> list[CatalogEntry]:
        return [e for e in self.entries if e.family == family]


# ---------------------------------------------------------------------------
# Derivation helpers (pure).
# ---------------------------------------------------------------------------


def _slug(model_id: str) -> str:
    return model_id.lstrip("~").split("/", 1)[-1].split(":", 1)[0].lower()


def derive_family(model_id: str) -> str:
    """Return a version-free family key, e.g. ``deepseek-v4.1-flash`` -> ``deepseek-flash``.

    Tokens carrying digits keep only a leading alphabetic prefix of three or
    more letters (``qwen3.7`` -> ``qwen``); short or purely numeric tokens
    (``v4.1``, ``k2.5``, ``30b``, date suffixes) are dropped.
    """
    slug = _slug(model_id)
    kept: list[str] = []
    for tok in slug.split("-"):
        if tok == "latest" or not tok:
            continue
        if any(ch.isdigit() for ch in tok):
            match = re.match(r"[a-z]*", tok)
            prefix = match.group(0) if match is not None else ""
            if len(prefix) >= 3:
                kept.append(prefix)
            continue
        kept.append(tok)
    if not kept:
        return model_id.lstrip("~").split("/", 1)[0].lower()
    return "-".join(kept)


def _vendor(model_id: str) -> str:
    return model_id.lstrip("~").split("/", 1)[0].lower()


def _dash_alias(model_id: str) -> str | None:
    """``anthropic/claude-opus-4.7`` -> ``anthropic/claude-opus-4-7``."""
    alt = re.sub(r"(?<=\d)\.(?=\d)", "-", model_id)
    return alt if alt != model_id else None


def _per_mtok(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0:  # OpenRouter uses -1 for "variable" router pricing
        return None
    return round(f * 1_000_000, 6)


def cost_class_from_price(output_per_mtok: float | None) -> str:
    """Map an output price ($/Mtok) onto the curated cost-class scale."""
    if output_per_mtok is None:
        return "unknown"
    if output_per_mtok == 0:
        return "free"
    if output_per_mtok <= 1.0:
        return "cheap"
    if output_per_mtok <= 5.0:
        return "mid"
    return "expensive"


def capability_tags_for(
    *,
    supports_tools: bool | None,
    supports_structured_outputs: bool | None,
    supports_reasoning: bool | None,
    input_modalities: Iterable[str],
    context_length: int | None,
    latency_class: str = "unknown",
    cost_class: str = "unknown",
    tier: str = "unknown",
    roles: Iterable[str] = (),
) -> tuple[str, ...]:
    tags: set[str] = set()
    if supports_tools:
        tags.add("tools")
    if supports_structured_outputs:
        tags.add("structured_outputs")
    if supports_reasoning:
        tags.add("reasoning")
    if "image" in tuple(input_modalities):
        tags.add("vision")
    if context_length is not None and context_length >= LONG_CONTEXT_TOKENS:
        tags.add("long_context")
    if latency_class == "fast":
        tags.add("fast")
    if cost_class in ("free", "tiny", "cheap"):
        tags.add("cheap")
    if tier == "frontier":
        tags.add("frontier")
    if _CODING_ROLES & set(roles):
        tags.add("coding")
    return tuple(sorted(tags))


def capability_tags_for_model_entry(entry: ModelEntry) -> tuple[str, ...]:
    """Routing tags for a curated entry, from curated fields only."""
    return capability_tags_for(
        supports_tools=entry.supports_tools,
        supports_structured_outputs=entry.supports_structured_outputs,
        supports_reasoning=entry.supports_reasoning,
        input_modalities=entry.input_modalities,
        context_length=entry.context_length,
        latency_class=entry.latency_class,
        cost_class=entry.cost_class,
        tier=entry.tier,
        roles=entry.roles,
    )


def _ref_date(synced_at: str | None) -> date | None:
    if not synced_at:
        return None
    try:
        return datetime.fromisoformat(synced_at.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _derive_status(
    model_id: str,
    *,
    curated_lifecycle: str | None,
    expiration_date: str | None,
    ref: date | None,
    listed: bool,
    snapshot_nonempty: bool,
) -> str:
    if curated_lifecycle == "deprecated":
        return "deprecated"
    if expiration_date and ref is not None:
        try:
            exp = date.fromisoformat(expiration_date[:10])
        except ValueError:
            exp = None
        if exp is not None:
            if exp <= ref:
                return "deprecated"
            if exp <= ref + timedelta(days=EXPIRING_WINDOW_DAYS):
                return "expiring"
    if model_id.startswith("~"):
        return "floating_alias"
    if curated_lifecycle is not None and snapshot_nonempty and not listed:
        return "unlisted"
    if _PREVIEW_TOKENS & set(_slug(model_id).split("-")):
        return "preview"
    return "current"


def _eligibility(lifecycle: str, status: str, restricted: bool) -> str:
    if restricted or status == "deprecated":
        return "blocked"
    return _LIFECYCLE_ELIGIBILITY.get(lifecycle, "not_promoted")


def static_price_table() -> Mapping[str, tuple[float, float]]:
    """The static pricing snapshot, imported on demand.

    Kept out of :func:`build_catalog`'s default path: the snapshot lives in
    ``providers.openrouter``, which imports ``httpx``, and the import-time
    routing resolver must stay cheap.
    """
    try:
        from openshard.providers.openrouter import MODEL_PRICING
    except Exception:
        return {}
    return MODEL_PRICING


def _static_pricing(model_id: str, table: Mapping[str, tuple[float, float]]) -> CatalogPricing:
    price = table.get(model_id)
    if price is None:
        return CatalogPricing()
    return CatalogPricing(price[0], price[1], PRICING_SOURCE_STATIC, None)


def _restricted_ids() -> frozenset[str]:
    try:
        from openshard.routing.provider_availability import RESTRICTED_MODEL_IDS
    except Exception:
        return frozenset()
    return RESTRICTED_MODEL_IDS


# ---------------------------------------------------------------------------
# Build.
# ---------------------------------------------------------------------------


def _discovered_facts(raw: dict, synced_at: str | None) -> dict:
    arch = raw.get("architecture") or {}
    pricing = raw.get("pricing") or {}
    top = raw.get("top_provider") or {}
    params = raw.get("supported_parameters")
    params_set = set(params) if isinstance(params, list) else None
    created = raw.get("created")
    release = None
    if isinstance(created, (int, float)) and created > 0:
        release = datetime.fromtimestamp(created, UTC).date().isoformat()
    in_mods = arch.get("input_modalities")
    out_mods = arch.get("output_modalities")
    return {
        "name": raw.get("name"),
        "canonical_slug": raw.get("canonical_slug"),
        "release_date": release,
        "expiration_date": raw.get("expiration_date"),
        "context_length": raw.get("context_length"),
        "max_output_tokens": top.get("max_completion_tokens"),
        "input_modalities": tuple(in_mods) if isinstance(in_mods, list) else ("text",),
        "output_modalities": tuple(out_mods) if isinstance(out_mods, list) else ("text",),
        "supports_tools": None if params_set is None else "tools" in params_set,
        "supports_structured_outputs": (
            None if params_set is None
            else bool({"structured_outputs", "response_format"} & params_set)
        ),
        "supports_reasoning": None if params_set is None else "reasoning" in params_set,
        "pricing": CatalogPricing(
            _per_mtok(pricing.get("prompt")),
            _per_mtok(pricing.get("completion")),
            PRICING_SOURCE_OPENROUTER,
            synced_at,
        )
        if pricing
        else CatalogPricing(),
    }


def _fingerprint(entries: Iterable[CatalogEntry]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(
            f"{e.id}|{e.status}|{e.lifecycle}|{e.routing_eligibility}|"
            f"{e.pricing.input_per_mtok}|{e.pricing.output_per_mtok}\n".encode()
        )
    return h.hexdigest()[:16]


def build_catalog(
    curated: Iterable[ModelEntry],
    discovered: Iterable[dict] = (),
    *,
    synced_at: str | None = None,
    origin: str = ORIGIN_CURATED_ONLY,
    stale: bool = False,
    error: str | None = None,
    static_prices: Mapping[str, tuple[float, float]] | None = None,
) -> ModelCatalog:
    """Merge curated entries with a discovered snapshot. Pure and deterministic.

    *discovered* holds normalized OpenRouter dicts (``normalize_model`` shape).
    Input order does not matter; entries are keyed and sorted by id.
    *static_prices* (see :func:`static_price_table`) fills pricing for curated
    models the snapshot does not list; omitted, their pricing is unknown.
    """
    static_prices = static_prices or {}
    curated_by_id = {e.id: e for e in curated}
    raw_by_id: dict[str, dict] = {}
    for raw in discovered:
        mid = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(mid, str) and mid and mid not in raw_by_id:
            raw_by_id[mid] = raw

    ref = _ref_date(synced_at)
    restricted = _restricted_ids()
    snapshot_nonempty = bool(raw_by_id)
    entries: list[CatalogEntry] = []

    for mid in sorted(set(curated_by_id) | set(raw_by_id)):
        cur = curated_by_id.get(mid)
        raw: dict | None = raw_by_id.get(mid)
        facts = _discovered_facts(raw, synced_at) if raw is not None else None
        lifecycle = cur.lifecycle if cur is not None else DISCOVERED_LIFECYCLE
        status = _derive_status(
            mid,
            curated_lifecycle=cur.lifecycle if cur is not None else None,
            expiration_date=facts["expiration_date"] if facts else None,
            ref=ref,
            listed=raw is not None,
            snapshot_nonempty=snapshot_nonempty,
        )
        aliases: set[str] = set()
        if facts and facts["canonical_slug"] and facts["canonical_slug"] != mid:
            aliases.add(facts["canonical_slug"])
        dash = _dash_alias(mid)
        if dash:
            aliases.add(dash)

        if cur is not None:
            pricing = facts["pricing"] if facts and facts["pricing"].source != PRICING_SOURCE_UNKNOWN else _static_pricing(mid, static_prices)
            entry = CatalogEntry(
                id=mid,
                provider=cur.provider,
                display_name=cur.display_name,
                family=derive_family(mid),
                aliases=tuple(sorted(aliases)),
                status=status,
                release_date=facts["release_date"] if facts else None,
                expiration_date=facts["expiration_date"] if facts else None,
                context_length=(facts["context_length"] if facts and facts["context_length"] else cur.context_length),
                max_output_tokens=facts["max_output_tokens"] if facts else None,
                input_modalities=facts["input_modalities"] if facts else cur.input_modalities,
                output_modalities=facts["output_modalities"] if facts else cur.output_modalities,
                supports_tools=cur.supports_tools,
                supports_structured_outputs=cur.supports_structured_outputs,
                supports_reasoning=cur.supports_reasoning,
                pricing=pricing,
                # Routing tags for curated models come from curated fields only.
                capability_tags=capability_tags_for_model_entry(cur),
                discovery_source="curated+openrouter" if raw is not None else "curated",
                lifecycle=lifecycle,
                routing_eligibility=_eligibility(lifecycle, status, mid in restricted),
                tier=cur.tier,
                cost_class=cur.cost_class,
                latency_class=cur.latency_class,
                roles=cur.roles,
            )
        else:
            assert facts is not None
            cost_class = cost_class_from_price(facts["pricing"].output_per_mtok)
            entry = CatalogEntry(
                id=mid,
                provider=_provider_name(facts["name"], mid),
                display_name=facts["name"] or mid,
                family=derive_family(mid),
                aliases=tuple(sorted(aliases)),
                status=status,
                release_date=facts["release_date"],
                expiration_date=facts["expiration_date"],
                context_length=facts["context_length"],
                max_output_tokens=facts["max_output_tokens"],
                input_modalities=facts["input_modalities"],
                output_modalities=facts["output_modalities"],
                supports_tools=facts["supports_tools"],
                supports_structured_outputs=facts["supports_structured_outputs"],
                supports_reasoning=facts["supports_reasoning"],
                pricing=facts["pricing"],
                capability_tags=capability_tags_for(
                    supports_tools=facts["supports_tools"],
                    supports_structured_outputs=facts["supports_structured_outputs"],
                    supports_reasoning=facts["supports_reasoning"],
                    input_modalities=facts["input_modalities"],
                    context_length=facts["context_length"],
                    cost_class=cost_class,
                ),
                discovery_source="openrouter",
                lifecycle=DISCOVERED_LIFECYCLE,
                routing_eligibility=_eligibility(DISCOVERED_LIFECYCLE, status, mid in restricted),
                cost_class=cost_class,
            )
        entries.append(entry)

    index = {e.id: e for e in entries}
    aliases_index = _build_alias_index(entries)
    return ModelCatalog(
        entries=tuple(entries),
        snapshot=CatalogSnapshotInfo(
            origin=origin,
            synced_at=synced_at,
            discovered_count=len(raw_by_id),
            stale=stale,
            error=error,
            fingerprint=_fingerprint(entries),
        ),
        _index=index,
        _aliases=aliases_index,
    )


def _provider_name(name: str | None, model_id: str) -> str:
    # OpenRouter names read "Vendor: Model"; fall back to the id prefix.
    if name and ":" in name:
        return name.split(":", 1)[0].strip()
    return _vendor(model_id)


def _build_alias_index(entries: list[CatalogEntry]) -> dict[str, str]:
    """Lower-cased alias -> canonical id. Ambiguous aliases are dropped.

    Candidate aliases per entry: declared aliases, the lower-cased id, and the
    vendor-less slug (``glm-5.1``). An exact canonical id always wins over an
    alias because ``ModelCatalog.resolve`` checks the index first.
    """
    claims: dict[str, set[str]] = {}

    def _claim(alias: str, mid: str) -> None:
        claims.setdefault(alias.strip().lower(), set()).add(mid)

    for e in entries:
        _claim(e.id, e.id)
        for a in e.aliases:
            _claim(a, e.id)
        if "/" in e.id:
            bare = e.id.lstrip("~").split("/", 1)[1]
            _claim(bare, e.id)
            dash = _dash_alias(bare)
            if dash:
                _claim(dash, e.id)
    resolved: dict[str, str] = {}
    for alias, ids in sorted(claims.items()):
        if len(ids) == 1:
            resolved[alias] = next(iter(ids))
            continue
        # A base id and its ``:variant`` ids (``:batch``, ``:free``) share a
        # canonical slug; the base id owns it. Anything else is ambiguous.
        bases = {i.split(":", 1)[0] for i in ids}
        if len(bases) == 1 and (base := next(iter(bases))) in ids:
            resolved[alias] = base
    return resolved


@cache
def curated_catalog() -> ModelCatalog:
    """Curated-only catalog (no disk, no network). Stable for the process."""
    return build_catalog(all_models())


# ---------------------------------------------------------------------------
# Load with cache / refresh / offline fallback.
# ---------------------------------------------------------------------------


def _cache_age_seconds(synced_at: str | None, now: float) -> float | None:
    if not synced_at:
        return None
    try:
        ts = datetime.fromisoformat(synced_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return max(0.0, now - ts)


def load_catalog(
    *,
    refresh: str = "never",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    cache_path: Path | None = None,
    fetcher: Callable[[], list[dict]] | None = None,
    now: float | None = None,
    curated: Iterable[ModelEntry] | None = None,
) -> ModelCatalog:
    """Return the catalog from the local cache, refreshing only when asked.

    *refresh*:
      ``"never"`` - cache only (run path; no network, ever).
      ``"auto"``  - refresh when the cache is missing or older than *ttl_seconds*.
      ``"force"`` - always attempt a refresh.

    A failed refresh never raises: the stale cache is used (``stale=True``,
    ``error`` set), else the catalog is curated-only. A corrupt cache is
    treated as missing. Never raises.
    """
    from openshard.models import openrouter_fetcher as orf

    if refresh not in ("never", "auto", "force"):
        raise ValueError(f"refresh must be never|auto|force, got {refresh!r}")
    now = time.time() if now is None else now
    curated_list = list(all_models() if curated is None else curated)

    try:
        cached = orf.load_openrouter_cache(cache_path)
    except orf.OpenRouterCacheError:
        cached = None
    if cached is not None and not isinstance(cached.get("models"), list):
        cached = None

    age = _cache_age_seconds(cached.get("synced_at") if cached else None, now)
    is_stale = cached is None or age is None or age > ttl_seconds
    want_refresh = refresh == "force" or (refresh == "auto" and is_stale)

    error: str | None = None
    if want_refresh:
        fetch = fetcher or orf.fetch_openrouter_models
        try:
            raw_models = fetch()
            normalized = [orf.normalize_model(m) for m in raw_models if isinstance(m, dict)]
            synced_at = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                orf.save_openrouter_cache(normalized, cache_path, synced_at=synced_at)
            except OSError:
                pass  # an unwritable cache must not lose the fresh data
            return build_catalog(
                curated_list, normalized, synced_at=synced_at, origin=ORIGIN_REFRESHED,
                static_prices=static_price_table(),
            )
        except Exception as exc:  # network, JSON, shape - all degrade the same way
            error = str(exc) or exc.__class__.__name__

    if cached is not None:
        origin = ORIGIN_STALE_CACHE if (is_stale and (want_refresh or refresh == "auto")) else ORIGIN_CACHE
        return build_catalog(
            curated_list,
            cached.get("models") or [],
            synced_at=cached.get("synced_at"),
            origin=origin,
            stale=is_stale,
            error=error,
            static_prices=static_price_table(),
        )
    return build_catalog(
        curated_list, (), origin=ORIGIN_CURATED_ONLY, error=error,
        static_prices=static_price_table(),
    )


def catalog_entry_to_dict(entry: CatalogEntry) -> dict:
    """JSON-friendly projection (``openshard models catalog --json``)."""
    return {
        "id": entry.id,
        "provider": entry.provider,
        "display_name": entry.display_name,
        "family": entry.family,
        "aliases": list(entry.aliases),
        "status": entry.status,
        "release_date": entry.release_date,
        "expiration_date": entry.expiration_date,
        "context_length": entry.context_length,
        "max_output_tokens": entry.max_output_tokens,
        "input_modalities": list(entry.input_modalities),
        "output_modalities": list(entry.output_modalities),
        "supports_tools": entry.supports_tools,
        "supports_structured_outputs": entry.supports_structured_outputs,
        "supports_reasoning": entry.supports_reasoning,
        "pricing": {
            "input_per_mtok": entry.pricing.input_per_mtok,
            "output_per_mtok": entry.pricing.output_per_mtok,
            "source": entry.pricing.source,
            "as_of": entry.pricing.as_of,
        },
        "capability_tags": list(entry.capability_tags),
        "discovery_source": entry.discovery_source,
        "lifecycle": entry.lifecycle,
        "routing_eligibility": entry.routing_eligibility,
        "cost_class": entry.cost_class,
    }


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogEntry",
    "CatalogPricing",
    "CatalogSnapshotInfo",
    "DISCOVERED_LIFECYCLE",
    "ModelCatalog",
    "build_catalog",
    "capability_tags_for_model_entry",
    "catalog_entry_to_dict",
    "cost_class_from_price",
    "curated_catalog",
    "derive_family",
    "load_catalog",
    "static_price_table",
]
