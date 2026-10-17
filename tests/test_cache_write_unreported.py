"""Cache-write tokens a source never reported stay unknown (None), never 0.

Codex's cumulative ``token_count`` has no cache-write counter, and an OpenCode
message may omit ``cache.write``. Missing does not mean zero: the record keeps
None, a saved record with None reloads, an older record with 0 still loads,
cost is priced from the counters that were reported, the Tokens line and
stats omit the unknown, and the Platform payload carries null.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from openshard.adapters.claude_hooks import _stored_transcript_usage, handle_hook
from openshard.adapters.codex_transcript import read_codex_transcript_usage
from openshard.history.receipt_render import render_full_shard_receipt
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.history.store import load_history
from openshard.models.pricing import COST_PROVENANCE_OFFICIAL_RATE, official_rate
from openshard.sync.envelope import receipt_payload
from tests.capture_fixtures import _make_repo

SID = "56565656-7878-4909-8a8a-bcbcbcbcbcbc"
OC_SID = "ses_0a1b2c3d4e5f60718293a4b5c6d7e8f0"
_PLATFORM_SCHEMA = Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json"


def _token(inp: int, out: int, cached: int) -> dict:
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": inp, "output_tokens": out, "cached_input_tokens": cached,
    }}}}


def _codex_transcript(root: Path) -> Path:
    path = root / ".codex" / "rollout.jsonl"
    path.parent.mkdir()
    records = [{"type": "session_meta", "payload": {"id": SID, "model_provider": "openai"}},
               {"type": "turn_context", "payload": {"model": "gpt-6-sol"}}, _token(1_000, 200, 400)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _codex_session(root: Path, path: Path) -> dict:
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        handle_hook({"session_id": SID, "cwd": str(root), "hook_event_name": event, "prompt": "task",
                     "reason": "other", "transcript_path": str(path), "model": "gpt-6-sol"}, agent="codex")
    return load_history(root / ".openshard" / "runs.jsonl", coerce=False)[0]


def test_codex_counter_reports_no_cache_writes(tmp_path):
    usage = read_codex_transcript_usage(_codex_transcript(_make_repo(tmp_path / "repo")), SID)
    assert usage is not None
    assert usage["totals"]["cache_creation"] is None
    assert usage["totals"]["cache_read"] == 400 and usage["totals"]["input"] == 600


def test_codex_record_leaves_cache_writes_unknown_and_prices_reported_counters(tmp_path):
    root = _make_repo(tmp_path / "repo")
    entry = _codex_session(root, _codex_transcript(root))
    assert (entry["prompt_tokens"], entry["completion_tokens"], entry["cache_read_tokens"]) == (600, 200, 400)
    assert entry["cache_creation_tokens"] is None
    rate = official_rate("gpt-6-sol")
    assert rate is not None and rate.cached_input_per_mtok is not None
    priced = (600 * rate.input_per_mtok + 200 * rate.output_per_mtok + 400 * rate.cached_input_per_mtok) / 1e6
    assert entry["cost_provenance"] == COST_PROVENANCE_OFFICIAL_RATE
    assert entry["estimated_cost"] == pytest.approx(round(priced, 6))

    receipt = build_shard_receipt(entry)
    assert receipt.tokens_cache_creation is None
    for out in (render_compact_shard_receipt(receipt), render_full_shard_receipt(receipt)):
        assert "600 input / 200 output (+400 cache read)" in out
        assert "cache write" not in out

    payload = receipt_payload(entry, 0)
    assert payload["tokens_cache_creation"] is None
    schema = json.loads(_PLATFORM_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(None, schema["properties"]["receipt"]["properties"]["tokens_cache_creation"])


def test_saved_codex_record_with_unknown_cache_writes_reloads(tmp_path):
    root = _make_repo(tmp_path / "repo")
    entry = _codex_session(root, _codex_transcript(root))
    stored = _stored_transcript_usage(entry, entry["capture"])
    assert stored is not None
    assert stored["totals"] == {"input": 600, "output": 200, "cache_read": 400, "cache_creation": None}


def test_older_codex_record_with_zero_cache_writes_still_loads():
    entry = {"prompt_tokens": 600, "completion_tokens": 200, "cache_read_tokens": 400,
             "cache_creation_tokens": 0}
    stored = _stored_transcript_usage(entry, {"tokens_source": "transcript"})
    assert stored is not None and stored["totals"]["cache_creation"] == 0
    assert _stored_transcript_usage({**entry, "cache_read_tokens": None}, {"tokens_source": "transcript"}) is None


def test_resumed_codex_segment_keeps_cache_writes_unknown(tmp_path):
    root = _make_repo(tmp_path / "repo")
    path = _codex_transcript(root)
    _codex_session(root, path)
    for event in ("SessionStart", "UserPromptSubmit"):
        handle_hook({"session_id": SID, "cwd": str(root), "hook_event_name": event, "source": "resume",
                     "prompt": "again", "transcript_path": str(path), "model": "gpt-6-sol"}, agent="codex")
    with path.open("a") as f:
        f.write(json.dumps(_token(1_500, 260, 500)) + "\n")
    for event in ("Stop", "SessionEnd"):
        handle_hook({"session_id": SID, "cwd": str(root), "hook_event_name": event, "reason": "other",
                     "transcript_path": str(path), "model": "gpt-6-sol"}, agent="codex")
    new = load_history(root / ".openshard" / "runs.jsonl", coerce=False)[-1]
    assert (new["prompt_tokens"], new["cache_read_tokens"]) == (400, 100)
    assert new["cache_creation_tokens"] is None


def _oc(repo: Path, event: str, **fields) -> None:
    handle_hook({"agent": "opencode", "event": event, "session_id": OC_SID, "directory": str(repo),
                 "worktree": str(repo), **fields}, env={}, agent="opencode")


def _opencode_session(repo: Path, caches: list[dict]) -> dict:
    _oc(repo, "session.created", parent_id=None)
    _oc(repo, "chat.message", prompt="task", provider_id="anthropic", model_id="claude-sonnet-4-5")
    for n, cache in enumerate(caches):
        _oc(repo, "message.updated", message_id=f"msg_{n}", provider_id="anthropic",
            model_id="claude-sonnet-4-5", cost=0.01, tokens={"input": 100, "output": 20, "cache": cache})
    _oc(repo, "session.deleted")
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)[0]


@pytest.mark.parametrize(("caches", "expected"), [
    ([{"read": 10}, {"read": 10}], None),  # never reported
    ([{"read": 10, "write": 5}, {"read": 10}], None),  # one message unreported: total unknown
    ([{"read": 10, "write": 0}, {"read": 10, "write": 0}], 0),  # an explicit 0 stays 0
    ([{"read": 10, "write": 5}, {"read": 10, "write": 7}], 12),
])
def test_opencode_cache_writes_are_summed_only_when_reported(tmp_path, caches, expected):
    entry = _opencode_session(_make_repo(tmp_path / "repo"), caches)
    assert (entry["prompt_tokens"], entry["cache_read_tokens"]) == (200, 20)
    assert entry["cache_creation_tokens"] == expected
    receipt = build_shard_receipt(entry)
    assert receipt.tokens_cache_creation == expected
    assert ("cache write" in render_compact_shard_receipt(receipt)) is bool(expected)
    assert receipt_payload(entry, 0)["tokens_cache_creation"] == expected


def test_stats_keep_cache_writes_null_when_nobody_reported_them(tmp_path):
    from tests.test_cli_visibility import HOOKS_FULL, _invoke, _ok, _repo

    unreported = {**HOOKS_FULL, "cache_creation_tokens": None}
    repo = _repo(tmp_path, [unreported, unreported])
    assert json.loads(_ok(_invoke(["stats", "--json"], repo)))["tokens"]["cache_write"] is None
    assert "cache write" not in _ok(_invoke(["stats"], repo))
