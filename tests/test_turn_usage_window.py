"""A sealed turn Receipt's transcript window ends at its Stop, however late the fold runs."""
from __future__ import annotations

import json
from pathlib import Path

from openshard.adapters.claude_cross_checkout import usage_until
from openshard.adapters.claude_transcript_usage import (
    _read_transcript_increment,
    _summarise_transcript_state,
    _transcript_state_for,
)


def _reply(path: Path, mid: str, at: str, output: int) -> None:
    line = {"type": "assistant", "timestamp": at, "message": {"id": mid, "model": "claude-opus-5-5", "usage": {
        "input_tokens": 1, "output_tokens": output, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _output(state: dict) -> int:
    return (_summarise_transcript_state(state) or {}).get("totals", {}).get("output", 0)


def test_only_a_sealed_turn_has_an_end():
    assert usage_until({"receipt_scope": "turn", "turn_sealed_at": "2026-02-01T00:00:05Z"}) == "2026-02-01T00:00:06Z"
    assert usage_until({"receipt_scope": "turn"}) is None
    assert usage_until({"receipt_scope": "session", "turn_sealed_at": "2026-02-01T00:00:05Z"}) is None
    assert usage_until({"receipt_scope": "turn", "turn_sealed_at": "garbled"}) is None


def test_a_late_seal_drops_the_next_turns_replies(tmp_path):
    transcript = tmp_path / "t.jsonl"
    since = "2026-02-01T00:00:00Z"
    _reply(transcript, "m1", "2026-02-01T00:00:05.400Z", 10)  # this turn, before its Stop at :05
    _reply(transcript, "m2", "2026-02-01T00:00:07.000Z", 99)  # the next turn, already written
    state = _transcript_state_for(None, since, None)
    assert _read_transcript_increment(state, [transcript]) and _output(state) == 109  # an in-progress fold
    until = usage_until({"receipt_scope": "turn", "turn_sealed_at": "2026-02-01T00:00:05Z"})
    sealed = _transcript_state_for(state, since, until)
    assert sealed is not state  # it had counted past the end: read again within the window
    assert _read_transcript_increment(sealed, [transcript]) and _output(sealed) == 10


def test_a_state_within_the_window_keeps_reading_incrementally(tmp_path):
    transcript = tmp_path / "t.jsonl"
    since, until = "2026-02-01T00:00:00Z", "2026-02-01T00:00:06Z"
    _reply(transcript, "m1", "2026-02-01T00:00:05.400Z", 10)
    state = _transcript_state_for(None, since, None)
    assert _read_transcript_increment(state, [transcript])
    assert _transcript_state_for(state, since, until) is state and state["until"] == until
