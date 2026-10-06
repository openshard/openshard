from openshard.insights.economics import (
    COST_ALLOCATED, COST_EQUIVALENT, COST_PROVIDER,
    allocate_subscription_cost, canonical_surface, cost_evidence, strongest_cost,
)


def test_openai_surfaces_group_without_losing_surface():
    work = canonical_surface(agent="codex", surface="chatgpt-work", provider="openai")
    codex = canonical_surface(agent="codex", surface="codex-cloud", provider="openai")
    assert work == {"provider_family": "openai", "product_family": "chatgpt", "surface": "work", "raw_surface": "chatgpt-work"}
    assert codex["provider_family"] == "openai"
    assert codex["product_family"] == "codex"
    assert codex["surface"] == "codex_cloud"


def test_provider_cost_wins_without_erasing_alternatives():
    allocated = cost_evidence(COST_ALLOCATED, 0.37, source="subscription")
    equivalent = cost_evidence(COST_EQUIVALENT, 0.82, source="official_rate_estimate")
    billed = cost_evidence(COST_PROVIDER, 0.91, source="provider_reported")
    assert strongest_cost([allocated, equivalent, billed]) == billed


def test_subscription_allocation_is_explicit_not_billed():
    item = allocate_subscription_cost(20, 3, 100)
    assert item is not None
    assert item["kind"] == COST_ALLOCATED
    assert item["usd"] == 0.6
    assert item["source"] == "subscription_allocation"


def test_invalid_allocation_fails_closed():
    assert allocate_subscription_cost(20, 101, 100) is None
    assert allocate_subscription_cost(20, 1, 0) is None
