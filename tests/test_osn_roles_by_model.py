"""A role whose calls went to several models names each one, never the last one for all of them.

An escalation ladder or a supervisor re-route gives the executor role two
models across attempts. The role record carried one ``model`` (the last)
with every call's tokens and cost, so the Receipt read "Executor Grok ·
20 turns · $0.50" for a run whose first ten turns and most of the money
were GPT's. The record now carries each model's share in order of first
use; the projection keeps counts and costs; the ROLES section names the
models in order and lists each share.
"""
from __future__ import annotations

from openshard.history.receipt_evidence import role_models_block, roles_block
from openshard.history.shard_contract import _role_line, _role_model_lines
from openshard.osn.model_provider import AttemptUsage
from openshard.osn.roles import ROLE_EXECUTOR, RoleRun


def _call(attempt: int, model: str, cost: float, prompt: int = 100, completion: int = 20) -> AttemptUsage:
    return AttemptUsage(attempt=attempt, model=model, requested_model=model, prompt_tokens=prompt,
                        completion_tokens=completion, cost_usd=cost, cost_source="provider_reported",
                        role=ROLE_EXECUTOR)


def test_one_model_means_no_breakdown():
    run = RoleRun.from_usage(ROLE_EXECUTOR, [_call(1, "a/m", 0.01), _call(1, "a/m", 0.02)], choice=None, provider=None)
    assert run.model == "a/m" and run.by_model == [] and run.to_record()["by_model"] == []


def test_two_models_across_attempts_are_recorded_in_order_with_their_own_share():
    usage = [_call(1, "a/m", 0.40, prompt=1000), _call(1, "a/m", 0.05), _call(2, "b/m", 0.03), _call(2, "b/m", 0.02)]
    run = RoleRun.from_usage(ROLE_EXECUTOR, usage, choice=None, provider=None)
    assert run.model == "b/m" and run.calls == 4 and run.cost_usd == 0.5
    assert run.by_model == [
        {"model": "a/m", "attempts": [1], "calls": 2, "prompt_tokens": 1100, "completion_tokens": 40, "cost_usd": 0.45},
        {"model": "b/m", "attempts": [2], "calls": 2, "prompt_tokens": 200, "completion_tokens": 40, "cost_usd": 0.05},
    ]
    record = run.to_record()
    assert [m["model"] for m in record["by_model"]] == ["a/m", "b/m"]

    projected = roles_block({"executor": record})["executor"]
    assert projected["model"] == "b/m" and [m["model"] for m in projected["by_model"]] == ["a/m", "b/m"]
    assert projected["by_model"][0]["cost_usd"] == 0.45 and projected["by_model"][0]["attempts"] == [1]
    assert role_models_block(record["by_model"][:1]) is None  # one entry is not a breakdown

    line = _role_line("executor", projected)
    assert "→" in line and line.index("→") < line.index("4 calls")  # both models named, in order, then the totals
    shares = _role_model_lines(projected)
    assert len(shares) == 2
    assert shares[0].strip().startswith("↳") and "attempt 1" in shares[0] and "$0.4500" in shares[0]
    assert "attempt 2" in shares[1] and "$0.0500" in shares[1]


def test_an_unknown_cost_in_one_share_stays_unknown():
    usage = [_call(1, "a/m", 0.1), _call(2, "b/m", None)]  # type: ignore[arg-type]
    run = RoleRun.from_usage(ROLE_EXECUTOR, usage, choice=None, provider=None)
    assert run.cost_usd is None and run.by_model[1]["cost_usd"] is None and run.by_model[0]["cost_usd"] == 0.1
    shares = _role_model_lines(run.to_record())
    assert "cost unknown" in shares[1] and "$0.1000" in shares[0]
