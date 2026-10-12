"""Native ``openshard run`` usage reaches the synced Receipt with its provenance.

Tokens a provider reported are kept (they were previously dropped because the
native record carried no ``tokens_provenance``); a 0/0 fill from a response
without usage stays unknown. Cost provenance follows each call's own source,
and only the provider's own figure is marked as not an estimate.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openshard.history.run_cost import RetryUsage
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.usage import COST_SOURCE_LIST_RATE, COST_SOURCE_PROVIDER, UsageStats
from openshard.history.views import receipt_to_dict


def _logged(tmp_path: Path, usage: UsageStats | None, *, retry_usage=None, retry: bool = False) -> dict:
    from openshard.run._pipeline_helpers import _log_run

    gen = MagicMock()
    gen.model = "acme/model-1"
    gen.fixer_model = "acme/fixer-1"
    with patch("openshard.run._pipeline_helpers.Path.cwd", return_value=tmp_path):
        _log_run(start=time.time(), task="t", generator=gen, retry_triggered=retry, files=[],
                 verification_attempted=False, verification_passed=None, workspace=None,
                 usage=usage, retry_usage=retry_usage)
    line = (tmp_path / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    return json.loads(line)


def _wire(entry: dict) -> dict:
    return receipt_to_dict(build_shard_receipt(entry), extended=True)


def test_provider_reported_tokens_and_cost_survive_to_the_sync_projection(tmp_path):
    usage = UsageStats(1200, 300, 1500, estimated_cost=0.0042, cost_source=COST_SOURCE_PROVIDER, cache_read_tokens=800)
    entry = _logged(tmp_path, usage)
    assert entry["tokens_provenance"] == "provider_reported"
    assert entry["cost_provenance"] == "provider_reported"
    wire = _wire(entry)
    assert (wire["tokens_input"], wire["tokens_output"], wire["tokens_cache_read"]) == (1200, 300, 800)
    assert wire["tokens_provenance"] == "provider_reported"
    assert wire["cost_usd"] == pytest.approx(0.0042)
    assert wire["cost_provenance"] == "provider_reported"
    # The provider's own figure is actual, not an estimate.
    assert wire["cost_is_estimate"] is False
    assert wire["cost"] == "$0.0042"


def test_list_rate_cost_is_an_official_rate_estimate(tmp_path):
    entry = _logged(tmp_path, UsageStats(1000, 100, 1100, estimated_cost=0.01, cost_source=COST_SOURCE_LIST_RATE))
    wire = _wire(entry)
    assert wire["cost_provenance"] == "official_rate_estimate"
    assert wire["cost_is_estimate"] is True


def test_response_without_usage_keeps_tokens_unknown_not_zero(tmp_path):
    entry = _logged(tmp_path, UsageStats(0, 0, 0))
    assert "tokens_provenance" not in entry
    wire = _wire(entry)
    assert wire["tokens_input"] is None and wire["tokens_output"] is None
    assert wire["cost_usd"] is None


def test_cost_without_a_source_has_no_provenance(tmp_path):
    entry = _logged(tmp_path, UsageStats(10, 5, 15, estimated_cost=0.001))
    assert "cost_provenance" not in entry
    assert _wire(entry)["cost_is_estimate"] is True


def test_retry_with_unknown_cost_source_records_no_provenance(tmp_path):
    usage = UsageStats(10, 5, 15, estimated_cost=0.001, cost_source=COST_SOURCE_PROVIDER)
    retry_usage = RetryUsage(20, 5, 25, estimated_cost=0.002)
    entry = _logged(tmp_path, usage, retry_usage=retry_usage, retry=True)
    assert "cost_provenance" not in entry
