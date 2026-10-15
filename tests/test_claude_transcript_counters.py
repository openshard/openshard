"""Missing runtime counters are unavailable, never invented zero usage."""
from __future__ import annotations

import json


def test_claude_missing_cache_counter_is_not_zero(tmp_path):
    from openshard.adapters.claude_hooks import read_transcript_usage
    from tests.test_claude_hooks import _assistant_line as _assistant
    path = tmp_path / "transcript.jsonl"
    row = _assistant("message-1", input_tokens=100, output_tokens=20)
    del row["message"]["usage"]["cache_read_input_tokens"]
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert read_transcript_usage(path) is None


def test_claude_incomplete_stream_does_not_price_older_counter(tmp_path):
    from openshard.adapters.claude_hooks import _transcript_cost, read_transcript_usage
    from tests.test_claude_hooks import _assistant_line as _assistant
    path = tmp_path / "transcript.jsonl"
    valid = _assistant("message-1", input_tokens=100, output_tokens=20)
    bad = _assistant("message-1", input_tokens=100, output_tokens=40)
    bad["message"]["usage"]["cache_read_input_tokens"] = True
    path.write_text(json.dumps(valid) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    usage = read_transcript_usage(path)
    assert usage is not None and not usage["complete"]
    assert usage["totals"]["output"] == 20
    assert _transcript_cost(usage) is None
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_assistant("message-1", input_tokens=100, output_tokens=50)) + "\n")
    usage = read_transcript_usage(path)
    assert usage is not None and usage["complete"] and usage["totals"]["output"] == 50


def test_haiku_tiers_are_per_message_and_survive_incremental_closure(tmp_path):
    import pytest

    from openshard.adapters import claude_transcript_usage as ctu
    from openshard.adapters.claude_hooks import _transcript_cost
    from tests.test_claude_hooks import _assistant_line as assistant

    path = tmp_path / "transcript.jsonl"
    rows = [
        assistant("boundary", model="claude-haiku-5.5", input_tokens=10_000,
                  cache_read=80_000, cache_5m=5_000, cache_1h=5_000, output_tokens=100),
        assistant("long", model="claude-haiku-5.5", input_tokens=10_002,
                  cache_read=80_000, cache_5m=5_000, cache_1h=5_000, output_tokens=100),
    ]
    # Each of these is short, even though their aggregate prompt exceeds 100k.
    rows += [assistant(f"short-{i}", model="claude-haiku-5.5", input_tokens=60_000,
                       output_tokens=10) for i in range(20)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    state = ctu._new_transcript_state(None)
    assert ctu._read_transcript_increment(state, [path])
    usage = ctu._summarise_transcript_state(state)
    bucket = usage["by_model"]["claude-haiku-5.5"]
    assert bucket["messages"] == 22
    assert bucket["long_context"]["messages"] == 1
    assert bucket["long_context"]["input"] == 10_002
    short_cost = (10_000 * .10 + 80_000 * .01 + 5_000 * .125 + 5_000 * .20 + 100 * .50) / 1e6
    long_cost = (10_002 * .50 + 80_000 * .05 + 5_000 * .625 + 5_000 * 1 + 100 * 2.50) / 1e6
    expected = short_cost + long_cost + 20 * (60_000 * .10 + 10 * .50) / 1e6
    assert _transcript_cost(usage) == pytest.approx(round(expected, 6))
    assert ctu._read_transcript_increment(state, [path])
    assert ctu._summarise_transcript_state(state) == usage


def test_haiku_stream_replacement_moves_tier_without_double_counting(tmp_path):
    from openshard.adapters.claude_hooks import _transcript_cost, read_transcript_usage
    from tests.test_claude_hooks import _assistant_line as assistant
    path = tmp_path / "transcript.jsonl"
    rows = [assistant("same", model="claude-haiku-5-5", input_tokens=100_001),
            assistant("same", model="claude-haiku-5-5", input_tokens=100_000)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    usage = read_transcript_usage(path)
    assert usage["messages"] == 1
    assert usage["by_model"]["claude-haiku-5-5"]["long_context"]["messages"] == 0
    assert _transcript_cost(usage) == .01
    bad = assistant("same", model="claude-haiku-5-5", input_tokens=100_001)
    del bad["message"]["usage"]["cache_read_input_tokens"]
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(bad) + "\n")
    usage = read_transcript_usage(path)
    assert not usage["complete"]
    assert _transcript_cost(usage) is None


def test_haiku_persisted_aggregate_without_tier_is_not_priced():
    from openshard.adapters.claude_hooks import _transcript_cost
    assert _transcript_cost({"complete": True, "by_model": {
        "claude-haiku-5-5": {"input": 100_001, "output": 10}
    }}) is None
