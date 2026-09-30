from __future__ import annotations

import pytest

from openshard.history.shard_contract import build_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.models.pricing import (
    COST_PROVENANCE_OFFICIAL_RATE,
    estimate_usage_cost,
    official_rate,
    single_pricing_model,
)


def test_openai_sol_prices_input_output_and_cache() -> None:
    estimate = estimate_usage_cost(
        "gpt-5.6-sol",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert estimate is not None
    assert estimate.usd == pytest.approx(29.4)
    assert estimate.input_usd == pytest.approx(4.0)
    assert estimate.cache_read_usd == pytest.approx(0.4)
    assert estimate.cache_write_usd == pytest.approx(5.0)
    assert estimate.output_usd == pytest.approx(20.0)


def test_anthropic_current_models_use_official_cache_rates() -> None:
    opus = estimate_usage_cost(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    fable = estimate_usage_cost(
        "claude-fable-5.1",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert opus is not None and opus.usd == pytest.approx(29.2)
    assert fable is not None and fable.usd == pytest.approx(72.75)


def test_unknown_model_or_unpriceable_cache_stays_unknown() -> None:
    assert estimate_usage_cost("some-new-model", input_tokens=10_000) is None
    # Google publishes token read pricing but cache storage has a duration fee,
    # so aggregate cache-write tokens alone are not enough to price it honestly.
    assert estimate_usage_cost("gemini-3.7-flash", cache_write_tokens=10_000) is None


def test_pricing_aliases_are_explicit_not_fuzzy() -> None:
    assert official_rate("anthropic/claude-opus-5-5") is not None
    assert official_rate("claude-opus-5.5") is not None
    assert official_rate("claude-opus-5-5-extra") is None


def test_multi_model_aggregate_usage_is_not_mispriced() -> None:
    entry = {
        "stage_runs": [
            {"stage_type": "planning", "model": "gpt-5.6-luna"},
            {"stage_type": "implementation", "model": "gpt-5.6-sol"},
        ]
    }
    assert single_pricing_model(entry) is None


def test_receipt_derives_cost_when_usage_is_known_but_dollars_are_not() -> None:
    entry = {
        "timestamp": "2026-10-01T00:00:00Z",
        "task": "Make cost visible",
        "execution_model": "gpt-5.6-sol",
        "tokens_provenance": "provider_reported",
        "prompt_tokens": 100_000,
        "completion_tokens": 10_000,
        "cache_read_tokens": 50_000,
        "cache_creation_tokens": 20_000,
    }
    receipt = build_shard_receipt(entry)
    expected = (100_000 * 4 + 10_000 * 20 + 50_000 * 0.4 + 20_000 * 5) / 1_000_000
    assert receipt.cost_raw == pytest.approx(expected)
    assert receipt.cost_provenance == COST_PROVENANCE_OFFICIAL_RATE
    assert receipt.cost_display.endswith(" est.")

    wire = receipt_to_dict(receipt, extended=True)
    assert wire["cost_usd"] == pytest.approx(expected)
    assert wire["cost_provenance"] == COST_PROVENANCE_OFFICIAL_RATE
    assert wire["cost_is_estimate"] is True


def test_provider_reported_cost_wins_over_rate_estimate() -> None:
    entry = {
        "timestamp": "2026-10-01T00:00:00Z",
        "task": "Keep provider cost",
        "execution_model": "gpt-5.6-sol",
        "estimated_cost": 1.23,
        "cost_provenance": "provider_reported",
        "tokens_provenance": "provider_reported",
        "prompt_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
    }
    receipt = build_shard_receipt(entry)
    assert receipt.cost_raw == pytest.approx(1.23)
    assert receipt.cost_provenance == "provider_reported"
