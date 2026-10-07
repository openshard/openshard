"""Where a model call's cost figure came from is recorded, never assumed.

``UsageStats.cost_source`` says whether ``estimated_cost`` is the provider's
own figure for the call (OpenRouter usage accounting) or OpenShard's
list-rate arithmetic from the token counts. A provider response without a
cost and without a known price leaves the cost unknown, not zero.
"""
from __future__ import annotations

from unittest.mock import patch

from openshard.providers.base import COST_SOURCE_LIST_RATE, COST_SOURCE_PROVIDER, UsageStats
from openshard.providers.openrouter import OpenRouterClient


def _client() -> OpenRouterClient:
    return OpenRouterClient("sk-or-test-key-not-real")


def _response(usage: dict, model: str = "acme/m") -> dict:
    return {"model": model, "choices": [{"message": {"content": "{}"}}], "usage": usage}


def test_usage_stats_defaults_keep_existing_callers_working():
    u = UsageStats(1, 2, 3)
    assert u.estimated_cost is None and u.cost_source is None and u.cache_read_tokens is None


def test_openrouter_requests_usage_accounting_and_records_provider_cost():
    client = _client()
    with patch.object(client, "_post", return_value=_response({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.00123,
        "prompt_tokens_details": {"cached_tokens": 40},
    })) as post:
        resp = client.execute("acme/m", "hello", system="sys", max_tokens=50)
    payload = post.call_args.args[1]
    assert payload["usage"] == {"include": True} and payload["max_tokens"] == 50
    assert resp.usage.estimated_cost == 0.00123
    assert resp.usage.cost_source == COST_SOURCE_PROVIDER
    assert resp.usage.cache_read_tokens == 40


def test_openrouter_fallback_arithmetic_is_labelled_as_an_estimate():
    client = _client()
    with patch.object(client, "_post", return_value=_response(
        {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000}, model="openai/gpt-6-sol",
    )), patch("openshard.providers.openrouter.compute_cost", return_value=2.0) as compute:
        resp = client.execute("openai/gpt-6-sol", "hello")
    assert compute.called
    assert resp.usage.estimated_cost == 2.0 and resp.usage.cost_source == COST_SOURCE_LIST_RATE


def test_openrouter_unknown_model_without_cost_stays_unknown():
    client = _client()
    with patch.object(client, "_post", return_value=_response(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, model="nobody/unpriced-model",
    )), patch("openshard.providers.openrouter.compute_cost", return_value=None):
        resp = client.execute("nobody/unpriced-model", "hello")
    assert resp.usage.estimated_cost is None and resp.usage.cost_source is None
    assert resp.usage.cache_read_tokens is None


def test_openrouter_ignores_a_non_numeric_cost():
    client = _client()
    with patch.object(client, "_post", return_value=_response(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": "free"},
    )), patch("openshard.providers.openrouter.compute_cost", return_value=None):
        resp = client.execute("acme/m", "hello")
    assert resp.usage.estimated_cost is None and resp.usage.cost_source is None
