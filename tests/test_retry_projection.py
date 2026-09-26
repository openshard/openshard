"""The receipt's cost and ``retry`` block for retried runs (history/run_cost.py through the sync projection)."""

from __future__ import annotations

import pytest

from openshard.history.metrics import load_runs  # noqa: F401  (import side: metrics module loads)
from openshard.history.receipt_evidence import retry_block
from openshard.history.run_cost import retry_attempt_record, run_cost_usd
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.providers.base import UsageStats
from openshard.sync.envelope import build_envelope

SONNET = "anthropic/claude-sonnet-4.6"
OPUS = "anthropic/claude-opus-4.7"


def _attempt(model, cost, tokens=150):
    u = UsageStats(prompt_tokens=tokens - 50, completion_tokens=50, total_tokens=tokens, estimated_cost=cost)
    return retry_attempt_record(model, u)


def _entry(**extra) -> dict:
    base = {
        "schema_version": "1.2",
        "run_id": "2026-09-26T13:30:42Z",
        "timestamp": "2026-09-26T13:30:42Z",
        "shard_id": "shard-20260926-0001",
        "receipt_id": "rcpt_" + "a1" * 16,
        "executor": "native",
        "execution_model": "deepseek/deepseek-v4-pro",
        "estimated_cost": 0.001,
        "verification_attempted": True,
        "verification_passed": False,
        "retry_triggered": True,
    }
    base.update(extra)
    return base


def _receipt(entry, *, extended=True):
    return receipt_to_dict(build_shard_receipt(entry, index=0), extended=extended)


def _complete(**extra):
    attempts = [_attempt(SONNET, 0.002, 150), _attempt(OPUS, 0.0146, 300)]
    return _entry(
        retry_attempts=attempts, fixer_model=OPUS, retry_estimated_cost=0.0166, retry_total_tokens=450, **extra
    )


class TestCompleteRecord:
    def test_receipt_cost_is_the_true_run_total(self):
        r = _receipt(_complete())
        assert r["cost_usd"] == pytest.approx(0.0176)  # 0.001 + 0.002 + 0.0146
        assert r["cost"] == "$0.0176"

    def test_retry_block_lists_every_escalation_and_says_the_cost_is_included(self):
        retry = _receipt(_complete())["retry"]
        assert retry["attempts"] == [
            {"model": SONNET, "total_tokens": 150, "cost_usd": 0.002},
            {"model": OPUS, "total_tokens": 300, "cost_usd": 0.0146},
        ]
        assert retry["cost_included"] is True
        assert retry["fixer_model"] == OPUS
        assert retry["cost_usd"] == pytest.approx(0.0166) and retry["total_tokens"] == 450

    def test_local_summaries_use_the_same_total(self):
        assert run_cost_usd(_complete()) == pytest.approx(0.0176)


class TestUnknownAttemptCost:
    def test_attempts_are_listed_but_the_total_is_not_claimed(self):
        e = _entry(retry_attempts=[_attempt(SONNET, 0.002), _attempt(OPUS, None)], fixer_model=OPUS,
                   retry_estimated_cost=None, retry_total_tokens=300)
        r = _receipt(e)
        assert r["cost_usd"] == 0.001  # first attempt only, as recorded
        assert r["retry"]["cost_included"] is False
        assert [a["cost_usd"] for a in r["retry"]["attempts"]] == [0.002, None]


class TestHistoricalRecord:
    """Written before retry_attempts existed: nothing is completed or reinterpreted."""

    def _old(self):
        return _entry(fixer_model=SONNET, retry_estimated_cost=0.0146, retry_total_tokens=1953)

    def test_cost_stays_the_first_attempt(self):
        r = _receipt(self._old())
        assert r["cost_usd"] == 0.001

    def test_retry_block_keeps_only_what_was_recorded(self):
        retry = _receipt(self._old())["retry"]
        assert retry["attempts"] is None and retry["cost_included"] is None
        assert retry["fixer_model"] == SONNET and retry["cost_usd"] == 0.0146 and retry["triggered"] is True

    def test_record_without_any_retry_fields_has_no_retry_claims(self):
        r = _receipt(_entry(retry_triggered=False))
        assert r["cost_usd"] == 0.001
        assert r["retry"]["attempts"] is None and r["retry"]["cost_included"] is None


class TestMalformed:
    @pytest.mark.parametrize("bad", ["x", [{"model": ""}], [{"model": 5}], [42], [{"model": SONNET, "estimated_cost": -1}]])
    def test_bad_attempts_never_produce_a_total(self, bad):
        r = _receipt(_entry(retry_attempts=bad, retry_estimated_cost=0.0146))
        assert r["cost_usd"] == 0.001
        assert r["retry"]["cost_included"] in (None, False)

    def test_an_attempt_model_that_looks_like_a_path_is_dropped_not_sent(self):
        e = _entry(retry_attempts=[_attempt("C:/Users/x/model", 0.002)], retry_estimated_cost=0.002)
        attempts = retry_block(e)["attempts"]
        assert attempts == [{"model": None, "total_tokens": 150, "cost_usd": 0.002}]

    def test_too_many_attempts_are_ignored(self):
        e = _entry(retry_attempts=[_attempt(SONNET, 0.001)] * 6, retry_estimated_cost=0.006)
        assert retry_block(e)["attempts"] is None and _receipt(e)["cost_usd"] == 0.001


class TestScope:
    def test_default_projection_is_unchanged(self):
        r = _receipt(_complete(), extended=False)
        assert "retry" not in r and "cost_usd" not in r

    def test_envelope_carries_the_new_keys_and_old_records_still_build(self):
        env = build_envelope(_complete(), 0, core_version="0.4.8")
        assert env["receipt"]["retry"]["cost_included"] is True
        old = build_envelope(_entry(fixer_model=SONNET, retry_estimated_cost=0.0146), 0, core_version="0.4.8")
        assert old["receipt"]["retry"]["cost_included"] is None
