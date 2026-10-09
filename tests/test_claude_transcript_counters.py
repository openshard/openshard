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
