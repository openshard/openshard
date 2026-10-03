"""Catalog refresh 2026-10-03: new provider models are catalogued, not promoted.

Pipeline under test: catalog -> availability -> eligibility -> policy ->
routing. A newly catalogued model must be displayable and priceable, but must
not become a default, a role-group member, or a scored-routing candidate.
"""
from __future__ import annotations

import pytest

from openshard.cli.run_output import _model_label
from openshard.history.shard_contract import display_model_name
from openshard.models.pricing import estimate_usage_cost, official_rate
from openshard.models.registry import (
    ROLE_GROUPS,
    get_model,
    is_routing_default_eligible,
)
from openshard.providers.openrouter import MODEL_PRICING
from openshard.scoring.filter import filter_unpromoted

NEW_WATCHLIST = (
    "anthropic/claude-fable-5.1",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "google/gemini-3.8-flash",
    "x-ai/grok-4.7",
    "moonshotai/kimi-k3",
)


@pytest.mark.parametrize("model_id", NEW_WATCHLIST)
def test_new_models_are_catalogued_as_watchlist_only(model_id: str) -> None:
    entry = get_model(model_id)
    assert entry is not None
    assert entry.lifecycle == "watchlist"
    assert entry.roles == ()
    assert not is_routing_default_eligible(model_id)
    assert all(model_id not in ids for ids in ROLE_GROUPS.values())


def _inv(mid: str):
    from openshard.providers.base import ModelInfo
    from openshard.providers.manager import InventoryEntry

    return InventoryEntry(provider="openrouter", model=ModelInfo(id=mid, name=mid, pricing={}))


def test_watchlist_models_do_not_enter_scored_routing() -> None:
    entries = [
        _inv("anthropic/claude-opus-4.8"),
        _inv("anthropic/claude-opus-5.5"),
        _inv("anthropic/claude-sonnet-4.6"),
        _inv("anthropic/claude-sonnet-5.5"),
    ]
    kept = [e.model.id for e in filter_unpromoted(entries)]
    assert kept == ["anthropic/claude-opus-4.8", "anthropic/claude-sonnet-4.6"]


def test_watchlist_direct_provider_alias_does_not_enter_scored_routing() -> None:
    entries = [_inv("claude-sonnet-4-6"), _inv("claude-opus-5-5"), _inv("claude-fable-5-1")]
    assert [e.model.id for e in filter_unpromoted(entries)] == ["claude-sonnet-4-6"]


def test_explicit_selection_still_admits_a_watchlist_model() -> None:
    entries = [_inv("anthropic/claude-opus-4.8"), _inv("anthropic/claude-opus-5.5")]
    allow = frozenset({"anthropic/claude-opus-5.5"})
    kept = [e.model.id for e in filter_unpromoted(entries, allow=allow)]
    assert "anthropic/claude-opus-5.5" in kept


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-fable-5-1", "Claude Fable 5.1"),
        ("claude-opus-5-5", "Claude Opus 5.5"),
        ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
        ("claude-opus-4-8", "Claude Opus 4.8"),
        ("anthropic/claude-opus-5.5", "Claude Opus 5.5"),
    ],
)
def test_current_claude_ids_have_display_names(model_id: str, expected: str) -> None:
    assert display_model_name(model_id) == expected
    assert _model_label(model_id) in (expected, f"Anthropic: {expected}")


@pytest.mark.parametrize(
    ("model_id", "rates"),
    [
        # (input, output, cache read, 5m cache write) per MTok, official pricing page.
        ("claude-fable-5-1", (10.0, 50.0, 0.25, 12.50)),
        ("claude-opus-5-5", (4.0, 20.0, 0.20, 5.0)),
        ("claude-sonnet-5-5", (2.0, 10.0, 0.20, 2.50)),
        ("claude-haiku-4-5-20251001", (1.0, 5.0, 0.10, 1.25)),
        ("anthropic/claude-opus-4.8", (5.0, 25.0, 0.50, 6.25)),
        ("anthropic/claude-sonnet-4.6", (3.0, 15.0, 0.30, 3.75)),
        ("gpt-6-sol", (2.0, 10.0, 0.20, 2.50)),
        ("gpt-6.1-sol", (2.0, 10.0, 0.10, 2.50)),
        ("gemini-3.8-flash", (0.75, 3.75, 0.075, None)),
        ("minimax/minimax-m2.7", (0.30, 1.20, 0.06, 0.375)),
    ],
)
def test_official_rates(model_id: str, rates: tuple) -> None:
    rate = official_rate(model_id)
    assert rate is not None
    assert (
        rate.input_per_mtok,
        rate.output_per_mtok,
        rate.cached_input_per_mtok,
        rate.cache_write_per_mtok,
    ) == rates


@pytest.mark.parametrize("model_id", ["grok-4.7", "MiniMax-M3", "deepseek-v4-pro"])
def test_tiered_or_time_of_day_rates_stay_unpriced(model_id: str) -> None:
    assert estimate_usage_cost(model_id, input_tokens=1000) is None


def test_static_anthropic_pricing_matches_official_list_prices() -> None:
    assert MODEL_PRICING["anthropic/claude-haiku-4.5"] == (1.00, 5.00)
    assert MODEL_PRICING["anthropic/claude-opus-4.6"] == (5.00, 25.00)
    assert MODEL_PRICING["anthropic/claude-opus-4.7"] == (5.00, 25.00)
    assert MODEL_PRICING["anthropic/claude-opus-5.5"] == (4.00, 20.00)
    assert MODEL_PRICING["anthropic/claude-fable-5.1"] == (10.00, 50.00)


def test_policy_allow_watchlist_readmits_watchlist_models() -> None:
    from openshard.routing.model_policy import ModelPolicyConfig, eligible_lifecycles

    entries = [_inv("anthropic/claude-opus-4.8"), _inv("anthropic/claude-opus-5.5")]
    default = filter_unpromoted(entries, eligible=eligible_lifecycles(ModelPolicyConfig()))
    assert [e.model.id for e in default] == ["anthropic/claude-opus-4.8"]
    opted_in = filter_unpromoted(
        entries, eligible=eligible_lifecycles(ModelPolicyConfig(allow_watchlist=True))
    )
    assert [e.model.id for e in opted_in] == ["anthropic/claude-opus-4.8", "anthropic/claude-opus-5.5"]


def test_gated_only_inventory_does_not_bypass_the_gate() -> None:
    # Only watchlist curated entries plus an unknown one: the liveness fallback
    # keeps the uncurated entry but never the gated watchlist models.
    entries = [_inv("anthropic/claude-opus-5.5"), _inv("claude-sonnet-5-5"), _inv("vendor/model-a")]
    assert [e.model.id for e in filter_unpromoted(entries)] == ["vendor/model-a"]
    only_gated = [_inv("anthropic/claude-opus-5.5"), _inv("x-ai/grok-4.7")]
    assert filter_unpromoted(only_gated) == []


def test_uncurated_inventory_is_still_returned_unchanged() -> None:
    unknown = [_inv("vendor/model-a"), _inv("vendor/model-b")]
    assert filter_unpromoted(unknown) == unknown


def test_advisory_lists_watchlist_only_when_asked() -> None:
    from openshard.models.advisory import recommend_models

    default_ids = {a.model.id for a in recommend_models(limit=100)}
    with_watch = {a.model.id for a in recommend_models(include_watchlist=True, limit=100)}
    assert "anthropic/claude-opus-5.5" not in default_ids
    assert "anthropic/claude-opus-5.5" in with_watch
    assert "minimax/m2.7" not in with_watch  # deprecated is never recommended
