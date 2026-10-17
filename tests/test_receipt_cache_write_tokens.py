"""Cache-write tokens are itemised on the Receipt, so its cost line adds up.

Claude Code transcripts report ``cache_creation_input_tokens`` (with a
5-minute / 1-hour TTL split) beside cache reads. Both are priced at their own
rates, so a Tokens line that shows reads but not writes leaves part of the
cost unexplained. A record that never reported cache writes shows none,
never "+0 cache write".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openshard.history.receipt_render import render_full_shard_receipt, tokens_line
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.models.pricing import COST_PROVENANCE_OFFICIAL_RATE, official_rate
from openshard.sync.envelope import receipt_payload
from tests.test_claude_hooks import _assistant_line, _run, _runs_lines, _transcript
from tests.test_claude_hooks import _claude_config_dir as _claude_config_dir  # noqa: F401  (autouse)
from tests.test_claude_hooks import repo as repo  # noqa: F401  (fixture)

_PLATFORM_SCHEMA = Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json"


def _session(repo: Path, records: list[dict]) -> dict:
    transcript = _transcript(repo, records)
    _run(repo, "UserPromptSubmit", prompt="task", transcript_path=str(transcript))
    _run(repo, "Stop", transcript_path=str(transcript))
    return _runs_lines(repo)[0]


def test_cache_writes_are_recorded_and_the_cost_reconciles(repo: Path):
    # The reported session: 18 input, 4k output, 505.3k cache read and 35.2k
    # (1-hour) cache write on Claude Opus 5.5 -- about $0.46.
    entry = _session(repo, [
        _assistant_line("msg_1", input_tokens=10, output_tokens=1_500, cache_read=200_000, cache_1h=30_000),
        _assistant_line("msg_2", input_tokens=8, output_tokens=2_500, cache_read=305_300, cache_1h=5_200),
    ])
    assert (entry["prompt_tokens"], entry["completion_tokens"]) == (18, 4_000)
    assert (entry["cache_read_tokens"], entry["cache_creation_tokens"]) == (505_300, 35_200)

    rate = official_rate("claude-opus-5-5")
    assert rate is not None and rate.cache_write_1h_per_mtok is not None
    itemised = (18 * rate.input_per_mtok + 4_000 * rate.output_per_mtok
                + 505_300 * rate.cached_input_per_mtok + 35_200 * rate.cache_write_1h_per_mtok) / 1e6
    assert entry["cost_provenance"] == COST_PROVENANCE_OFFICIAL_RATE
    assert entry["estimated_cost"] == pytest.approx(round(itemised, 6))
    assert entry["estimated_cost"] == pytest.approx(0.462732)
    # Without the cache writes the itemised lines would only explain ~$0.18.
    assert itemised - 35_200 * rate.cache_write_1h_per_mtok / 1e6 == pytest.approx(0.181132)

    receipt = build_shard_receipt(entry)
    assert receipt.tokens_cache_creation == 35_200
    expected = "18 input / 4k output (+505.3k cache read, +35.2k cache write)"
    assert expected in render_compact_shard_receipt(receipt)
    assert expected in render_full_shard_receipt(receipt)


def test_mixed_ttl_cache_writes_are_priced_at_their_own_rates(repo: Path):
    entry = _session(repo, [
        _assistant_line("msg_1", input_tokens=100, output_tokens=1_000, cache_read=50_000,
                        cache_5m=20_000, cache_1h=10_000),
    ])
    assert entry["cache_creation_tokens"] == 30_000
    rate = official_rate("claude-opus-5-5")
    assert rate is not None and rate.cache_write_per_mtok is not None and rate.cache_write_1h_per_mtok is not None
    itemised = (100 * rate.input_per_mtok + 1_000 * rate.output_per_mtok + 50_000 * rate.cached_input_per_mtok
                + 20_000 * rate.cache_write_per_mtok + 10_000 * rate.cache_write_1h_per_mtok) / 1e6
    assert entry["estimated_cost"] == pytest.approx(round(itemised, 6))
    assert "(+50k cache read, +30k cache write)" in render_compact_shard_receipt(build_shard_receipt(entry))


def test_transcript_without_cache_creation_counter_records_no_cache_write(repo: Path):
    # A message that omits the counter is unavailable usage, never zero: no
    # token totals or cost are claimed from it, and no cache write is shown.
    row = _assistant_line("msg_1", input_tokens=100, output_tokens=20, cache_read=500)
    del row["message"]["usage"]["cache_creation_input_tokens"]
    entry = _session(repo, [row])
    assert "cache_creation_tokens" not in entry
    assert "estimated_cost" not in entry
    receipt = build_shard_receipt(entry)
    assert receipt.tokens_cache_creation is None
    assert "cache write" not in render_compact_shard_receipt(receipt)


def test_older_record_without_cache_write_field_renders_without_it():
    entry = {
        "timestamp": "2026-10-01T00:00:00Z", "task": "t", "executor": "claude_code",
        "prompt_tokens": 18, "completion_tokens": 4_000, "cache_read_tokens": 505_300,
        "tokens_provenance": "provider_reported",
    }
    receipt = build_shard_receipt(entry)
    assert receipt.tokens_cache_creation is None
    out = render_compact_shard_receipt(receipt)
    assert "18 input / 4k output (+505.3k cache read)" in out
    assert "cache write" not in out


def test_tokens_line_never_shows_an_unknown_or_zero_cache_counter():
    assert tokens_line(18, 4_000) == "18 input / 4k output"
    assert tokens_line(18, 4_000, None, None) == "18 input / 4k output"
    assert tokens_line(18, 4_000, 0, 0) == "18 input / 4k output"
    assert tokens_line(18, 4_000, None, 35_200) == "18 input / 4k output (+35.2k cache write)"


def test_cache_write_is_already_in_the_platform_sync_contract(repo: Path):
    # tokens_cache_creation was already synced and the Platform schema accepts
    # it: no new field reaches the strict envelope.
    entry = _session(repo, [_assistant_line("msg_1", output_tokens=10, cache_1h=1_000)])
    payload = receipt_payload(entry, 0)
    assert payload["tokens_cache_creation"] == 1_000
    schema = json.loads(_PLATFORM_SCHEMA.read_text(encoding="utf-8"))
    assert "tokens_cache_creation" in json.dumps(schema)


def test_stats_sum_cache_writes_and_keep_unknown_as_null(tmp_path: Path):
    from tests.test_cli_visibility import HOOKS_FULL, _invoke, _ok, _repo

    with_writes = {**HOOKS_FULL, "cache_creation_tokens": 35_200}
    without = {k: v for k, v in HOOKS_FULL.items() if k != "cache_creation_tokens"}
    repo = _repo(tmp_path, [with_writes])
    assert "(+30k cache read, +35.2k cache write)" in _ok(_invoke(["stats"], repo))
    assert json.loads(_ok(_invoke(["stats", "--json"], repo)))["tokens"]["cache_write"] == 35_200
    unknown = _repo(tmp_path, [without], name="other")
    assert json.loads(_ok(_invoke(["stats", "--json"], unknown)))["tokens"]["cache_write"] is None
    assert "cache write" not in _ok(_invoke(["stats"], unknown))
