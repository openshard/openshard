"""Captured cumulative counters must belong to one Receipt segment, not every resume."""
from __future__ import annotations

import json
from pathlib import Path

from openshard.adapters.claude_hooks import handle_hook
from openshard.adapters.codex_transcript import read_codex_transcript_usage
from openshard.history.store import load_history
from tests.capture_fixtures import _make_repo

SID = "12121212-3434-4565-8787-909090909090"


def _token(inp=100, out=20, cached=30):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": inp, "output_tokens": out, "cached_input_tokens": cached,
    }}}}


def _transcript(root: Path):
    path = root / ".codex" / "rollout.jsonl"
    path.parent.mkdir()
    records = [{"type": "session_meta", "payload": {"id": SID, "model_provider": "openai"}},
               {"type": "turn_context", "payload": {"model": "gpt-6"}}, _token()]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _hook(root, path, event, **extra):
    return handle_hook({"session_id": SID, "cwd": str(root), "hook_event_name": event,
                        "transcript_path": str(path), "model": "gpt-6", **extra}, agent="codex")


def test_resumed_codex_receipt_uses_counter_delta_and_preserves_previous_evidence(tmp_path):
    root = _make_repo(tmp_path / "repo")
    path = _transcript(root)
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        _hook(root, path, event, prompt="first", reason="other")
    first = load_history(root / ".openshard" / "runs.jsonl", coerce=False)[0]
    _hook(root, path, "SessionStart", source="resume")
    _hook(root, path, "UserPromptSubmit", prompt="second")
    with path.open("a") as f:
        f.write(json.dumps(_token(150, 40, 40)) + "\n")
    _hook(root, path, "Stop")
    _hook(root, path, "SessionEnd", reason="other")
    old, new = load_history(root / ".openshard" / "runs.jsonl", coerce=False)
    assert old == first
    assert new["prompt_tokens"] == 40  # (150 - 40) - (100 - 30)
    assert new["completion_tokens"] == 20
    assert new["cache_read_tokens"] == 10


def test_missing_cache_counter_is_unknown_not_zero(tmp_path):
    root = _make_repo(tmp_path / "repo")
    path = _transcript(root)
    record = _token()
    del record["payload"]["info"]["total_token_usage"]["cached_input_tokens"]
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n" + json.dumps(record) + "\n")
    assert read_codex_transcript_usage(path, SID) is None


def test_invalid_later_counter_does_not_claim_complete_coverage(tmp_path):
    root = _make_repo(tmp_path / "repo")
    path = _transcript(root)
    with path.open("a") as f:
        f.write(json.dumps(_token(1, 1, 5)) + "\n")
    assert read_codex_transcript_usage(path, SID)["complete"] is False


def test_price_requires_each_counter_and_labels_calculation():
    from openshard.history.usage_evidence import price_tokens
    tokens = {"status": "observed", "complete": True, "input": 100, "output": 20,
              "cache_read": 0, "cache_write": 0}
    result = price_tokens("gpt-6-sol", tokens)
    assert result is not None and result["kind"] == "calculated_estimate"
    assert price_tokens("gpt-6-sol", {**tokens, "cache_read": None}) is None
