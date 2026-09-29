from __future__ import annotations

import pytest

from openshard.history.receipt_evidence import organisation_policy_block
from openshard.osn.budget import BudgetLimits
from openshard.sync.policies import (
    OrganisationPolicyState,
    combine_budget_limits,
    combine_model_policy,
    effective_policy_hash,
)

SONNET = "anthropic/claude-sonnet-4.6"
GPT = "openai/gpt-5.6-sol"
HASH_A = "sha256:" + "a" * 64


def _document(*, models: dict | None = None, budgets: dict | None = None) -> dict:
    base_models = {
        "allowed_models": [],
        "blocked_models": [],
        "allowed_providers": [],
        "blocked_providers": [],
        "max_cost_class": None,
        "allow_specialist": False,
        "allow_experimental": False,
        "allow_watchlist": False,
        "allow_deprecated": False,
        "allow_open_weight": False,
        "allow_fallback": False,
        "allow_openrouter_wide": True,
    }
    if models:
        base_models.update(models)
    base_budgets = {
        "max_spend_usd": None,
        "max_attempts": None,
        "max_commands": None,
        "max_writes": None,
    }
    if budgets:
        base_budgets.update(budgets)
    return {"schema_version": 1, "models": base_models, "budgets": base_budgets}


def _state(document: dict | None = None) -> OrganisationPolicyState:
    if document is None:
        return OrganisationPolicyState("org", None, None, None, "none", "no_organisation_policy")
    return OrganisationPolicyState("org", 7, HASH_A, document, "fresh")


def test_model_policy_combines_stricter_rules() -> None:
    repo = {
        "models": {
            "allowed_models": [SONNET, GPT],
            "blocked_providers": ["xai"],
            "max_cost_class": "expensive",
            "allow_specialist": True,
            "allow_openrouter_wide": True,
        }
    }
    org = _state(_document(models={
        "allowed_models": [SONNET],
        "blocked_models": [GPT],
        "blocked_providers": ["anthropic"],
        "max_cost_class": "mid",
        "allow_specialist": False,
        "allow_openrouter_wide": False,
    }))
    policy = combine_model_policy(repo, org)
    assert policy.allowed_models == frozenset({SONNET})
    assert GPT in policy.blocked_models
    assert policy.blocked_providers == frozenset({"xai", "anthropic"})
    assert policy.max_cost_class == "mid"
    assert policy.allow_specialist is False
    assert policy.allow_openrouter_wide is False


def test_disjoint_allowlists_fail_closed() -> None:
    repo = {"models": {"allowed_models": [SONNET]}}
    org = _state(_document(models={"allowed_models": [GPT]}))
    with pytest.raises(ValueError, match="allowlists do not overlap"):
        combine_model_policy(repo, org)


def test_budget_uses_strictest_limit_and_accepts_org_zero() -> None:
    repo = {"agent_budgets": {"max_spend_usd": 2.0, "max_attempts": 4, "max_writes": 20}}
    org = _state(_document(budgets={"max_spend_usd": 1.0, "max_attempts": 0, "max_commands": 3}))
    effective, local, hosted = combine_budget_limits(repo, org)
    assert local == BudgetLimits(2.0, 4, None, 20)
    assert hosted == BudgetLimits(1.0, 0, 3, None)
    assert effective == BudgetLimits(1.0, 0, 3, 20)


def test_effective_hash_changes_when_policy_changes() -> None:
    a = combine_model_policy({}, _state(_document()))
    b = combine_model_policy({}, _state(_document(models={"blocked_providers": ["anthropic"]})))
    budget = BudgetLimits()
    assert effective_policy_hash(a, budget).startswith("sha256:")
    assert effective_policy_hash(a, budget) != effective_policy_hash(b, budget)


def test_receipt_record_contains_identity_not_document() -> None:
    state = _state(_document(models={"blocked_models": [GPT]}))
    record = state.receipt_record(
        effective_policy_hash="sha256:" + "b" * 64,
        repository_override_applied=True,
    )
    assert record["organisation_policy_version"] == 7
    assert record["organisation_policy_hash"] == HASH_A
    assert record["applied"] is True
    assert record["repository_override_applied"] is True
    assert "document" not in record and "policy" not in record


def test_receipt_projector_rejects_malformed_hash_but_keeps_safe_identity() -> None:
    raw = {
        "organisation_policy": {
            "schema_version": 1,
            "organisation_policy_version": 3,
            "organisation_policy_hash": "bad",
            "source": "fresh",
            "applied": True,
            "repository_override_applied": False,
            "effective_policy_hash": "sha256:" + "c" * 64,
            "refreshed_at_run_start": True,
            "reason": None,
        }
    }
    block = organisation_policy_block(raw)
    assert block is not None
    assert block["organisation_policy_hash"] is None
    assert block["effective_policy_hash"] == "sha256:" + "c" * 64
