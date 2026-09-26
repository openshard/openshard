"""Run-level cost and retry accounting (history/run_cost.py) and its capture in the run record."""

from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from openshard.cli.run_output import _print_summary
from openshard.execution.generator import ChangedFile
from openshard.history.run_cost import (
    MAX_RETRY_ATTEMPTS,
    RetryUsage,
    aggregate_retry_usage,
    retry_attempt_record,
    run_total_cost,
    run_total_from_usage,
    stored_retry_attempts,
)
from openshard.providers.base import UsageStats
from openshard.run.pipeline import _log_run

SONNET = "anthropic/claude-sonnet-4.6"
OPUS = "anthropic/claude-opus-4.7"


def _u(p=100, c=50, cost: float | None = 0.002) -> UsageStats:
    return UsageStats(prompt_tokens=p, completion_tokens=c, total_tokens=p + c, estimated_cost=cost)


class TestAttemptRecord:
    def test_keeps_recorded_values(self):
        r = retry_attempt_record(SONNET, _u(100, 50, 0.002))
        assert r == {
            "model": SONNET, "prompt_tokens": 100, "completion_tokens": 50,
            "total_tokens": 150, "estimated_cost": 0.002,
        }

    def test_missing_usage_is_none_never_zero(self):
        r = retry_attempt_record(OPUS, None)
        assert r["estimated_cost"] is None and r["total_tokens"] is None

    @pytest.mark.parametrize("bad", [-0.5, float("nan"), float("inf"), True, "0.1"])
    def test_unusable_cost_is_dropped(self, bad):
        assert retry_attempt_record(OPUS, _u(cost=bad))["estimated_cost"] is None


class TestAggregate:
    def test_sums_tokens_and_cost_across_every_attempt(self):
        agg = aggregate_retry_usage([
            retry_attempt_record(SONNET, _u(100, 50, 0.002)),
            retry_attempt_record(OPUS, _u(200, 100, 0.0146)),
        ])
        assert isinstance(agg, RetryUsage) and isinstance(agg, UsageStats)
        assert (agg.prompt_tokens, agg.completion_tokens, agg.total_tokens) == (300, 150, 450)
        assert agg.estimated_cost == pytest.approx(0.0166)
        assert [a["model"] for a in agg.attempts] == [SONNET, OPUS]

    def test_cost_is_unknown_when_any_attempt_cost_is_unknown(self):
        agg = aggregate_retry_usage([
            retry_attempt_record(SONNET, _u(cost=0.002)),
            retry_attempt_record(OPUS, _u(cost=None)),
        ])
        assert agg is not None and agg.estimated_cost is None

    def test_no_attempts_no_usage(self):
        assert aggregate_retry_usage([]) is None


class TestStoredAttempts:
    def _entry(self, attempts):
        return {"retry_attempts": attempts}

    def test_valid(self):
        rec = [retry_attempt_record(SONNET, _u())]
        assert stored_retry_attempts(self._entry(rec)) == rec

    @pytest.mark.parametrize("bad", [None, [], "x", [{"model": ""}], [{"model": 3}], [42]])
    def test_malformed_is_none(self, bad):
        assert stored_retry_attempts(self._entry(bad)) is None

    def test_too_many_is_none(self):
        rec = [retry_attempt_record(SONNET, _u())] * (MAX_RETRY_ATTEMPTS + 1)
        assert stored_retry_attempts(self._entry(rec)) is None


class TestRunTotal:
    def _retried(self, first, costs):
        return {
            "estimated_cost": first, "retry_triggered": True,
            "retry_attempts": [retry_attempt_record(m, _u(cost=c)) for m, c in zip((SONNET, OPUS), costs)],
        }

    def test_no_retry_first_generation_is_the_run(self):
        assert run_total_cost({"estimated_cost": 0.001, "retry_triggered": False}) == (0.001, True)
        assert run_total_cost({"estimated_cost": 0.001}) == (0.001, True)

    def test_retried_with_every_attempt_stored_is_the_true_total(self):
        total, complete = run_total_cost(self._retried(0.001, [0.002, 0.0146]))
        assert complete is True and total == pytest.approx(0.0176)

    def test_historical_retried_record_is_not_completed_by_guessing(self):
        # Older Core: first attempt in estimated_cost, at most the last escalation in
        # retry_estimated_cost, no attempts. Nothing is added.
        old = {"estimated_cost": 0.001, "retry_triggered": True, "retry_estimated_cost": 0.0146,
               "fixer_model": SONNET}
        assert run_total_cost(old) == (0.001, False)

    def test_an_attempt_without_a_cost_makes_the_total_unknowable(self):
        assert run_total_cost(self._retried(0.001, [0.002, None])) == (0.001, False)

    def test_unknown_first_cost_is_never_a_total(self):
        assert run_total_cost(self._retried(None, [0.002, 0.003])) == (None, False)

    def test_live_objects_follow_the_same_rule(self):
        agg = aggregate_retry_usage([retry_attempt_record(SONNET, _u(cost=0.002))])
        assert run_total_from_usage(_u(cost=0.001), agg, True) == (pytest.approx(0.003), True)
        assert run_total_from_usage(_u(cost=0.001), _u(cost=0.002), True) == (0.001, False)  # plain usage: no attempts
        assert run_total_from_usage(_u(cost=0.001), None, False) == (0.001, True)


def _log(retry_usage, *, retry_triggered=True, fixer="configured/fixer"):
    """Run the real _log_run in a throwaway cwd and return the stored record."""
    gen = MagicMock()
    gen.model = "deepseek/deepseek-v4-pro"
    gen.fixer_model = fixer
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            os.makedirs(".openshard", exist_ok=True)
            _log_run(
                start=time.time(), task="fix it", generator=gen, retry_triggered=retry_triggered,
                files=[ChangedFile(path="a.py", change_type="update", content="", summary="")],
                verification_attempted=True, verification_passed=False, workspace=None,
                usage=_u(100, 20, 0.001), retry_usage=retry_usage, run_index=1,
            )
            with open(".openshard/runs.jsonl", encoding="utf-8") as fh:
                return json.loads(fh.readlines()[-1])
        finally:
            os.chdir(original)


class TestRecordCapture:
    def test_two_escalations_are_all_recorded_and_summed(self):
        agg = aggregate_retry_usage([
            retry_attempt_record(SONNET, _u(100, 50, 0.002)),
            retry_attempt_record(OPUS, _u(200, 100, 0.0146)),
        ])
        e = _log(agg)
        assert [a["model"] for a in e["retry_attempts"]] == [SONNET, OPUS]
        assert e["retry_estimated_cost"] == pytest.approx(0.0166)
        assert e["retry_total_tokens"] == 450
        assert e["estimated_cost"] == 0.001  # the first attempt, unchanged meaning
        assert run_total_cost(e) == (pytest.approx(0.0176), True)

    def test_fixer_model_is_the_model_that_made_the_final_attempt(self):
        agg = aggregate_retry_usage([retry_attempt_record(SONNET, _u()), retry_attempt_record(OPUS, _u())])
        assert _log(agg, fixer=SONNET)["fixer_model"] == OPUS

    def test_a_caller_without_attempts_keeps_the_old_shape(self):
        e = _log(_u(10, 5, 0.004), fixer="configured/fixer")
        assert e["fixer_model"] == "configured/fixer"
        assert "retry_attempts" not in e
        assert e["retry_estimated_cost"] == 0.004
        assert run_total_cost(e) == (0.001, False)  # cannot be shown as the total

    def test_no_retry_records_no_attempts(self):
        e = _log(None, retry_triggered=False)
        assert "retry_attempts" not in e and "fixer_model" not in e


class TestCliSummary:
    def _out(self, capsys, retry_usage, retry_triggered=True):
        gen = MagicMock()
        gen.fixer_model = "configured/fixer"
        _print_summary(time.time(), gen, retry_triggered, [], usage=_u(100, 20, 0.001),
                       retry_usage=retry_usage, detail="full")
        return capsys.readouterr().out

    def test_cost_line_is_the_total_with_retries(self, capsys):
        agg = aggregate_retry_usage([retry_attempt_record(SONNET, _u(cost=0.002)), retry_attempt_record(OPUS, _u(cost=0.0146))])
        out = self._out(capsys, agg)
        assert "Cost: $0.0176 (incl. retries)" in out
        assert "Escalated:" in out and "->" in out

    def test_cost_line_says_when_it_is_only_the_first_attempt(self, capsys):
        out = self._out(capsys, _u(cost=0.004))
        assert "Cost: $0.0010 (first attempt only)" in out
        assert "Fixer model:" in out

    def test_no_retry_line_is_unchanged(self, capsys):
        out = self._out(capsys, None, retry_triggered=False)
        assert "Cost: $0.0010" in out and "(" not in out.split("Cost:")[1]
