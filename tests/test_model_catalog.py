"""Dynamic model catalog + routing classes.

Product rule under test: model DISCOVERY is dynamic, routing ELIGIBILITY stays
controlled. A newly released model is recognised, displayed and explicitly
selectable without a code release, but never becomes a routing default on its
own.
"""
from __future__ import annotations

import json
import random
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.models import openrouter_fetcher as orf
from openshard.models.catalog import (
    DISCOVERED_LIFECYCLE,
    ORIGIN_CACHE,
    ORIGIN_CURATED_ONLY,
    ORIGIN_REFRESHED,
    ORIGIN_STALE_CACHE,
    build_catalog,
    cost_class_from_price,
    curated_catalog,
    derive_family,
    load_catalog,
)
from openshard.models.registry import all_models
from openshard.routing.model_policy import (
    ModelPolicyConfig,
    explicit_selection_ids,
    model_policy_from_config,
)
from openshard.routing.model_resolver import (
    MODEL_CHEAP,
    MODEL_ESCALATE,
    MODEL_MAIN,
    MODEL_VISUAL,
    resolve_routing_model_for_context,
)
from openshard.routing.provider_availability import (
    ProviderAvailability,
    build_routable_pool,
)
from openshard.routing.routing_classes import (
    CLASS_NAMES,
    ROLE_TO_CLASS,
    select_all_classes,
    select_for_class,
)

SYNCED_AT = "2026-09-24T00:00:00Z"
SYNCED_TS = 1790208000.0  # 2026-09-24T00:00:00Z
NEW_FLASH = "deepseek/deepseek-v4.1-flash"
OLD_FLASH = "deepseek/deepseek-v4-flash"


def _raw(
    mid: str,
    name: str,
    *,
    created: int,
    prompt: str = "0.0000001",
    completion: str = "0.0000003",
    params=("tools", "structured_outputs", "reasoning"),
    inputs=("text",),
    context: int = 1_048_576,
    slug: str | None = None,
    expiration: str | None = None,
) -> dict:
    return orf.normalize_model({
        "id": mid,
        "name": name,
        "created": created,
        "canonical_slug": slug or mid,
        "context_length": context,
        "architecture": {"input_modalities": list(inputs), "output_modalities": ["text"]},
        "pricing": {"prompt": prompt, "completion": completion},
        "top_provider": {"context_length": context, "max_completion_tokens": 131_072},
        "supported_parameters": list(params),
        "expiration_date": expiration,
    })


def _snapshot() -> list[dict]:
    """A small OpenRouter-shaped snapshot around the DeepSeek Flash family."""
    return [
        # Curated model, as listed upstream (2026-04-24).
        _raw(OLD_FLASH, "DeepSeek: DeepSeek V4 Flash 0423", created=1777000000,
             prompt="0.000000088606", completion="0.000000177212",
             slug="deepseek/deepseek-v4-flash-20260423"),
        # Newly released successor (2026-09-10): cheaper-class, tools, vision.
        _raw(NEW_FLASH, "DeepSeek: DeepSeek V4.1 Flash", created=1789021285,
             prompt="0.00000014", completion="0.00000042", inputs=("text", "image"),
             slug="deepseek/deepseek-v4.1-flash-20260910"),
        # Pricing variant sharing the canonical slug.
        _raw(NEW_FLASH + ":batch", "DeepSeek: DeepSeek V4.1 Flash (batch)", created=1789021285,
             prompt="0.000000112", completion="0.000000336",
             slug="deepseek/deepseek-v4.1-flash-20260910"),
        # Floating alias.
        _raw("~deepseek/deepseek-flash-latest", "DeepSeek: DeepSeek Flash Latest",
             created=1789400000, prompt="0.00000004", completion="0.000001"),
        # Expired upstream (expiration before the snapshot date).
        _raw("deepseek/deepseek-v3.2", "DeepSeek: DeepSeek V3.2", created=1764547200,
             expiration="2026-09-01"),
        # Expiring soon (within 90 days of the snapshot).
        _raw("deepseek/deepseek-chat-v3.1", "DeepSeek: DeepSeek V3.1", created=1755734400,
             expiration="2026-10-15"),
        # Curated GLM, listed with its live price.
        _raw("z-ai/glm-5.1", "Z.ai: GLM 5.1", created=1775520000,
             prompt="0.000000966", completion="0.000003036"),
    ]


def _catalog(snapshot=None, synced_at=SYNCED_AT):
    return build_catalog(
        all_models(), _snapshot() if snapshot is None else snapshot,
        synced_at=synced_at, origin=ORIGIN_CACHE,
    )


def _write_cache(path: Path, models: list[dict], synced_at: str = SYNCED_AT) -> None:
    orf.save_openrouter_cache(models, path, synced_at=synced_at)


# ---------------------------------------------------------------------------
# Refresh + offline fallback
# ---------------------------------------------------------------------------


class TestCatalogRefresh:
    def test_auto_refreshes_missing_cache_and_persists_it(self, tmp_path):
        cache = tmp_path / "or.json"
        calls = []

        def fetcher():
            calls.append(1)
            return [{"id": NEW_FLASH, "name": "DeepSeek: DeepSeek V4.1 Flash",
                     "created": 1789021285, "pricing": {"prompt": "0.00000014", "completion": "0.00000042"}}]

        cat = load_catalog(refresh="auto", cache_path=cache, fetcher=fetcher, now=SYNCED_TS)
        assert calls == [1]
        assert cat.snapshot.origin == ORIGIN_REFRESHED
        assert cat.snapshot.synced_at == SYNCED_AT
        assert cat.is_recognised(NEW_FLASH)
        saved = json.loads(cache.read_text(encoding="utf-8"))
        assert saved["synced_at"] == SYNCED_AT
        assert [m["id"] for m in saved["models"]] == [NEW_FLASH]

    def test_auto_does_not_refresh_fresh_cache(self, tmp_path):
        cache = tmp_path / "or.json"
        _write_cache(cache, _snapshot())

        def fetcher():
            raise AssertionError("fresh cache must not trigger a fetch")

        cat = load_catalog(refresh="auto", cache_path=cache, fetcher=fetcher, now=SYNCED_TS + 3600)
        assert cat.snapshot.origin == ORIGIN_CACHE
        assert cat.snapshot.stale is False

    def test_auto_refreshes_cache_older_than_ttl(self, tmp_path):
        cache = tmp_path / "or.json"
        _write_cache(cache, _snapshot())
        calls = []

        def fetcher():
            calls.append(1)
            return []

        load_catalog(refresh="auto", cache_path=cache, fetcher=fetcher, now=SYNCED_TS + 2 * 86400)
        assert calls == [1]

    def test_force_always_fetches(self, tmp_path):
        cache = tmp_path / "or.json"
        _write_cache(cache, _snapshot())
        calls = []
        load_catalog(refresh="force", cache_path=cache, fetcher=lambda: calls.append(1) or [],
                     now=SYNCED_TS + 60)
        assert calls == [1]

    def test_never_mode_never_fetches(self, tmp_path):
        def fetcher():
            raise AssertionError("refresh=never must not touch the network")

        cat = load_catalog(refresh="never", cache_path=tmp_path / "missing.json", fetcher=fetcher)
        assert cat.snapshot.origin == ORIGIN_CURATED_ONLY

    def test_invalid_refresh_mode_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            load_catalog(refresh="sometimes", cache_path=tmp_path / "x.json")


class TestOfflineFallback:
    def test_failed_refresh_falls_back_to_stale_cache(self, tmp_path):
        cache = tmp_path / "or.json"
        _write_cache(cache, _snapshot())

        def offline():
            raise orf.OpenRouterFetchError("Network error — check your connection.")

        cat = load_catalog(refresh="auto", cache_path=cache, fetcher=offline, now=SYNCED_TS + 3 * 86400)
        assert cat.snapshot.origin == ORIGIN_STALE_CACHE
        assert cat.snapshot.stale is True
        assert "Network error" in (cat.snapshot.error or "")
        # Discovered models from the stale cache are still recognised.
        assert cat.is_recognised(NEW_FLASH)

    def test_failed_refresh_without_cache_is_curated_only(self, tmp_path):
        def offline():
            raise OSError("no route to host")

        cat = load_catalog(refresh="auto", cache_path=tmp_path / "none.json", fetcher=offline)
        assert cat.snapshot.origin == ORIGIN_CURATED_ONLY
        assert cat.snapshot.error == "no route to host"
        assert cat.is_recognised(OLD_FLASH)
        assert not cat.is_recognised(NEW_FLASH)

    def test_corrupt_cache_is_treated_as_missing(self, tmp_path):
        cache = tmp_path / "or.json"
        cache.write_text("{not json", encoding="utf-8")
        cat = load_catalog(refresh="never", cache_path=cache)
        assert cat.snapshot.origin == ORIGIN_CURATED_ONLY

    def test_offline_and_online_route_identically(self, tmp_path):
        offline = select_all_classes(curated_catalog())
        online = select_all_classes(_catalog())
        assert {k: v.model for k, v in offline.items()} == {k: v.model for k, v in online.items()}

    def test_default_cache_path_follows_openshard_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path))
        assert orf.default_cache_path() == tmp_path / "openrouter-models.json"


# ---------------------------------------------------------------------------
# Newly discovered model: recognised, not promoted
# ---------------------------------------------------------------------------


class TestDiscoveredNotPromoted:
    def test_new_model_is_recognised_with_normalized_metadata(self):
        e = _catalog().get(NEW_FLASH)
        assert e is not None
        assert e.lifecycle == DISCOVERED_LIFECYCLE
        assert e.routing_eligibility == "not_promoted"
        assert e.discovery_source == "openrouter"
        assert e.provider == "DeepSeek"
        assert e.family == "deepseek-flash"
        assert e.release_date == "2026-09-10"
        assert e.pricing.input_per_mtok == pytest.approx(0.14)
        assert e.pricing.output_per_mtok == pytest.approx(0.42)
        assert e.pricing.source == "openrouter" and e.pricing.as_of == SYNCED_AT
        assert e.cost_class == "cheap"
        assert {"tools", "vision", "reasoning", "long_context"} <= set(e.capability_tags)

    def test_new_model_never_selected_by_any_class(self):
        for sel in select_all_classes(_catalog()).values():
            assert sel.model != NEW_FLASH
            assert NEW_FLASH not in sel.considered

    def test_new_model_surfaces_as_cheap_coding_promotion_candidate(self):
        sel = select_for_class("cheap_coding", _catalog())
        assert sel.model == OLD_FLASH
        assert sel.promotion_candidates == (NEW_FLASH,)  # no :batch, no floating alias

    def test_curated_entry_keeps_curated_routing_fields(self):
        e = _catalog().get("z-ai/glm-5.1")
        assert e.discovery_source == "curated+openrouter"
        assert e.lifecycle == "active_default" and e.routing_eligibility == "default"
        # Discovered price is displayed with provenance ...
        assert e.pricing.output_per_mtok == pytest.approx(3.036)
        # ... but the curated cost class (a routing input) is not overridden.
        assert e.cost_class == "mid"

    def test_scored_inventory_gate_drops_uncurated_models(self):
        from openshard.providers.base import ModelInfo
        from openshard.providers.manager import InventoryEntry
        from openshard.scoring.filter import filter_unpromoted
        from openshard.scoring.shortlist import build_shortlist

        def inv(mid):
            return InventoryEntry(provider="openrouter", model=ModelInfo(id=mid, name=mid, pricing={}))

        entries = [inv(OLD_FLASH), inv("deepseek/deepseek-v4-pro"), inv(NEW_FLASH)]
        # Without the gate the shortlist's newest-version rule promotes 4.1.
        assert [e.model.id for e in build_shortlist(entries)] == [NEW_FLASH]
        gated = filter_unpromoted(entries)
        assert NEW_FLASH not in [e.model.id for e in gated]
        assert NEW_FLASH not in [e.model.id for e in build_shortlist(gated)]
        # Explicit selection passes the gate.
        assert NEW_FLASH in [e.model.id for e in filter_unpromoted(entries, allow=frozenset({NEW_FLASH}))]
        # Direct-provider ids resolve to curated models via aliases.
        direct = [inv("claude-sonnet-4-6"), inv("claude-opus-5-5")]
        assert [e.model.id for e in filter_unpromoted(direct)] == ["claude-sonnet-4-6"]
        # An inventory with nothing curated is left alone (no silent emptying).
        unknown = [inv("vendor/model-a"), inv("vendor/model-b")]
        assert filter_unpromoted(unknown) == unknown

    def test_default_pool_never_contains_discovered_model(self, tmp_path):
        _write_cache(orf.default_cache_path(), _snapshot())
        avail = ProviderAvailability(("openrouter",), True, False, False)
        pool = build_routable_pool(avail, policy=ModelPolicyConfig())
        assert NEW_FLASH not in {m.id for m in pool.routable}


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


class TestAliases:
    def test_canonical_slug_resolves_to_base_not_variant(self):
        assert _catalog().resolve("deepseek/deepseek-v4.1-flash-20260910") == NEW_FLASH

    def test_dash_form_resolves(self):
        assert _catalog().resolve("anthropic/claude-opus-4-7") == "anthropic/claude-opus-4.7"

    def test_vendorless_slug_resolves(self):
        assert _catalog().resolve("glm-5.1") == "z-ai/glm-5.1"

    def test_alias_lookup_is_case_insensitive(self):
        assert _catalog().resolve("Z-AI/GLM-5.1") == "z-ai/glm-5.1"

    def test_exact_id_wins_and_variant_stays_distinct(self):
        cat = _catalog()
        assert cat.resolve(NEW_FLASH + ":batch") == NEW_FLASH + ":batch"
        assert cat.get(NEW_FLASH + ":batch").pricing.output_per_mtok == pytest.approx(0.336)

    def test_ambiguous_alias_is_dropped(self):
        snap = [
            _raw("a/shared-model", "A: Shared", created=1789000000),
            _raw("b/shared-model", "B: Shared", created=1789000000),
        ]
        cat = build_catalog([], snap, synced_at=SYNCED_AT)
        assert cat.resolve("shared-model") is None
        assert cat.resolve("a/shared-model") == "a/shared-model"

    def test_unknown_id_is_not_recognised(self):
        assert _catalog().resolve("missing/model") is None


# ---------------------------------------------------------------------------
# Stale / deprecated handling
# ---------------------------------------------------------------------------


class TestStaleAndDeprecated:
    def test_expired_model_is_deprecated_and_blocked(self):
        e = _catalog().get("deepseek/deepseek-v3.2")
        assert e.status == "deprecated"
        assert e.routing_eligibility == "blocked"

    def test_expiry_within_window_is_expiring(self):
        assert _catalog().get("deepseek/deepseek-chat-v3.1").status == "expiring"

    def test_floating_alias_status(self):
        assert _catalog().get("~deepseek/deepseek-flash-latest").status == "floating_alias"

    def test_curated_model_missing_upstream_is_unlisted_but_still_routes(self):
        cat = _catalog()
        # minimax/m2.7 is curated but absent from this (non-empty) snapshot.
        assert cat.get("minimax/m2.7").status == "unlisted"
        # Controlled: routing does not silently change because of it.
        assert select_for_class("cheap_coding", cat).model == OLD_FLASH

    def test_curated_only_catalog_never_marks_unlisted(self):
        assert all(e.status != "unlisted" for e in curated_catalog().entries)

    def test_curated_deprecated_lifecycle_is_blocked(self):
        from openshard.models.registry import ModelEntry

        dep = ModelEntry(id="x/old", display_name="Old", provider="X", tier="cheap",
                         lifecycle="deprecated", supports_tools=True, cost_class="cheap")
        cat = build_catalog([dep])
        assert cat.get("x/old").routing_eligibility == "blocked"
        assert select_for_class("cheap_coding", cat).model is None

    def test_deprecated_pin_is_rejected_and_class_default_used(self):
        cat = _catalog()
        sel = select_for_class("cheap_coding", cat, pins={"cheap_coding": "deepseek/deepseek-v3.2"})
        assert sel.model == OLD_FLASH and sel.source == "catalog"
        assert sel.rejected_pin == "deepseek/deepseek-v3.2"
        assert sel.rejected_pin_reason == "deprecated_or_blocked"

    def test_deprecated_pin_in_config_warns_instead_of_failing(self):
        _write_cache(orf.default_cache_path(), _snapshot())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            policy = model_policy_from_config(
                {"models": {"routing_classes": {"cheap_coding": "deepseek/deepseek-v3.2"}}}
            )
        assert policy.class_pins == ()
        assert any("deprecated" in str(w.message) for w in caught)

    def test_status_uses_snapshot_date_not_wall_clock(self):
        # Same snapshot, much later wall clock: status must not change.
        with patch("time.time", return_value=SYNCED_TS + 400 * 86400):
            later = _catalog()
        assert later.get("deepseek/deepseek-chat-v3.1").status == "expiring"


# ---------------------------------------------------------------------------
# Explicit user selection
# ---------------------------------------------------------------------------


class TestExplicitSelection:
    @pytest.fixture(autouse=True)
    def _cache(self):
        _write_cache(orf.default_cache_path(), _snapshot())

    def test_class_pin_accepts_discovered_model_via_alias(self):
        policy = model_policy_from_config(
            {"models": {"routing_classes": {"cheap_coding": "deepseek/deepseek-v4.1-flash-20260910"}}}
        )
        assert policy.class_pin_map == {"cheap_coding": NEW_FLASH}
        assert NEW_FLASH in explicit_selection_ids(policy)

    def test_pinned_discovered_model_enters_pool_and_wins_its_role(self):
        policy = model_policy_from_config({"models": {"routing_classes": {"cheap_coding": NEW_FLASH}}})
        avail = ProviderAvailability(("openrouter",), True, False, False)
        pool = build_routable_pool(avail, policy=policy)
        assert NEW_FLASH in {m.id for m in pool.routable}
        res = resolve_routing_model_for_context("cheap", pool, class_pins=policy.class_pin_map)
        assert res.model == NEW_FLASH and res.source == "class_pin"
        # Other roles are unaffected by the cheap_coding pin.
        main = resolve_routing_model_for_context("main", pool, class_pins=policy.class_pin_map)
        assert main.model == MODEL_MAIN

    def test_roster_accepts_discovered_model(self):
        policy = model_policy_from_config({"models": {
            "mode": "custom_roster",
            "custom_roster": {"models": [NEW_FLASH, "anthropic/claude-sonnet-4.6"]},
        }})
        avail = ProviderAvailability(("openrouter",), True, False, False)
        pool = build_routable_pool(avail, policy=policy)
        assert {m.id for m in pool.routable} == {NEW_FLASH, "anthropic/claude-sonnet-4.6"}

    def test_roster_keeps_lifecycle_gate_for_curated_models(self):
        policy = model_policy_from_config({"models": {
            "mode": "custom_roster",
            "custom_roster": {"models": ["qwen/qwen3.6-flash"]},  # curated experimental
        }})
        assert "qwen/qwen3.6-flash" not in explicit_selection_ids(policy)

    def test_unknown_pin_model_raises(self):
        with pytest.raises(ValueError, match="unknown model ID"):
            model_policy_from_config({"models": {"routing_classes": {"cheap_coding": "missing/model"}}})

    def test_unknown_class_raises(self):
        with pytest.raises(ValueError, match="unknown class"):
            model_policy_from_config({"models": {"routing_classes": {"turbo": OLD_FLASH}}})

    def test_pin_missing_required_capability_raises(self):
        # vision requires image input; GLM 5.1 is text-only.
        with pytest.raises(ValueError, match="missing_capability:vision"):
            model_policy_from_config({"models": {"routing_classes": {"vision": "z-ai/glm-5.1"}}})

    def test_roster_add_cli_accepts_discovered_model(self, tmp_path, monkeypatch):
        from openshard.cli.main import cli

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["roster", "add", NEW_FLASH])
        assert result.exit_code == 0, result.output
        assert "not evaluated by OpenShard" in result.output

    def test_models_show_cli_displays_discovered_model(self):
        from openshard.cli.main import cli

        result = CliRunner().invoke(cli, ["models", "show", NEW_FLASH])
        assert result.exit_code == 0, result.output
        assert "not_promoted" in result.output and "deepseek-flash" in result.output


# ---------------------------------------------------------------------------
# Routing classes
# ---------------------------------------------------------------------------


class TestRoutingClasses:
    def test_class_backed_roles_match_legacy_constants(self):
        cat = curated_catalog()
        assert select_for_class(ROLE_TO_CLASS["cheap"], cat).model == MODEL_CHEAP == OLD_FLASH
        assert select_for_class(ROLE_TO_CLASS["main"], cat).model == MODEL_MAIN == "z-ai/glm-5.1"
        assert select_for_class(ROLE_TO_CLASS["escalate"], cat).model == MODEL_ESCALATE
        assert select_for_class(ROLE_TO_CLASS["visual"], cat).model == MODEL_VISUAL

    def test_every_class_selects_a_curated_model(self):
        for name, sel in select_all_classes(_catalog()).items():
            assert sel.model is not None, name
            assert _catalog().get(sel.model).curated

    def test_fast_class_requires_fast_tag(self):
        cat = curated_catalog()
        for mid in select_for_class("fast", cat).considered:
            assert "fast" in cat.get(mid).capability_tags

    def test_vision_class_requires_image_input(self):
        cat = curated_catalog()
        for mid in select_for_class("vision", cat).considered:
            assert "vision" in cat.get(mid).capability_tags

    def test_class_selection_follows_curation_not_hardcoded_id(self):
        # If curation promotes the successor (after an eval), cheap_coding moves
        # with it - no routing code references either version.
        from dataclasses import replace

        curated = [
            replace(m, lifecycle="deprecated") if m.id == OLD_FLASH else m for m in all_models()
        ]
        from openshard.models.registry import ModelEntry

        curated.append(ModelEntry(
            id=NEW_FLASH, display_name="DeepSeek: V4.1 Flash", provider="DeepSeek",
            tier="cheap", roles=("cheap_control", "boilerplate"), supports_tools=True,
            latency_class="fast", cost_class="cheap", lifecycle="active_default",
        ))
        cat = build_catalog(curated, _snapshot(), synced_at=SYNCED_AT)
        assert select_for_class("cheap_coding", cat).model == NEW_FLASH

    def test_unknown_class_raises_keyerror(self):
        with pytest.raises(KeyError):
            select_for_class("gigantic", curated_catalog())


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_snapshot_same_catalog_and_selections(self):
        a, b = _catalog(), _catalog()
        assert a.entries == b.entries
        assert a.snapshot.fingerprint == b.snapshot.fingerprint
        assert select_all_classes(a) == select_all_classes(b)

    def test_input_order_does_not_matter(self):
        snap = _snapshot()
        curated = all_models()
        rnd = random.Random(7)
        rnd.shuffle(snap)
        rnd.shuffle(curated)
        shuffled = build_catalog(curated, snap, synced_at=SYNCED_AT, origin=ORIGIN_CACHE)
        assert shuffled.entries == _catalog().entries
        assert shuffled.snapshot.fingerprint == _catalog().snapshot.fingerprint
        assert select_all_classes(shuffled) == select_all_classes(_catalog())

    def test_price_change_changes_fingerprint(self):
        snap = _snapshot()
        snap[1] = _raw(NEW_FLASH, "DeepSeek: DeepSeek V4.1 Flash", created=1789021285,
                       prompt="0.0000002", completion="0.0000005")
        assert build_catalog(all_models(), snap, synced_at=SYNCED_AT).snapshot.fingerprint != \
            _catalog().snapshot.fingerprint

    def test_every_class_is_selectable_from_curated_only(self):
        sels = select_all_classes(curated_catalog())
        assert set(sels) == set(CLASS_NAMES)


# ---------------------------------------------------------------------------
# Derivation helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("deepseek/deepseek-v4.1-flash", "deepseek-flash"),
        ("deepseek/deepseek-v4-flash-0731", "deepseek-flash"),
        ("deepseek/deepseek-v4.1-flash:batch", "deepseek-flash"),
        ("z-ai/glm-5.1", "glm"),
        ("z-ai/glm-5.3", "glm"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet"),
        ("qwen/qwen3.7-max", "qwen-max"),
        ("minimax/m2.7", "minimax"),
        ("~z-ai/glm-latest", "glm"),
    ],
)
def test_derive_family(model_id, family):
    assert derive_family(model_id) == family


@pytest.mark.parametrize(
    ("out_price", "cls"),
    [(None, "unknown"), (0.0, "free"), (0.42, "cheap"), (3.036, "mid"), (25.0, "expensive")],
)
def test_cost_class_from_price(out_price, cls):
    assert cost_class_from_price(out_price) == cls
