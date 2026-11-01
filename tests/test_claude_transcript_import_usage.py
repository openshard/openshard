"""Full usage, cost and session end on a Receipt reconstructed from a Claude Code transcript.

A reconstruction must carry everything the transcript proves: every token
counter (cache reads and 5m/1h cache writes included), counted once per API
message across the main and ``subagents/`` transcripts, attributed per model;
a cost priced per model at official list rates and labelled an estimate, or
unknown -- never $0 -- when a model has no list rate; and a session that is
plainly over must be closed (end not observed) so it syncs.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_transcript_import as cti
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.sync.envelope import eligibility
from tests.capture_fixtures import _make_repo

SID = "7d0c2a51-1f3e-4d7a-9b6e-0c1d2e3f4a5b"
LAST = "2026-10-20T10:05:00.000Z"
LAST_DT = datetime(2026, 10, 20, 10, 5, tzinfo=UTC)
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def _usage(inp: int, out: int, read: int = 0, five: int = 0, hour: int = 0) -> dict:
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": five + hour,
            "cache_creation": {"ephemeral_5m_input_tokens": five, "ephemeral_1h_input_tokens": hour}}


def _assistant(at: str, msg_id: str, model: str, usage: dict, block: dict, *, sidechain: bool = False) -> dict:
    return {"type": "assistant", "timestamp": at, "cwd": "/w", "sessionId": SID, "isSidechain": sidechain,
            "requestId": f"req_{msg_id}", "uuid": f"u-{at}-{block.get('type')}",
            "message": {"id": msg_id, "role": "assistant", "model": model, "stop_reason": "tool_use",
                        "content": [block], "usage": usage}}


def _user(at: str, text: str) -> dict:
    return {"type": "user", "timestamp": at, "cwd": "/w", "sessionId": SID, "isSidechain": False,
            "message": {"role": "user", "content": text}}


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _transcript(tmp_path: Path, *, sub_model: str = SONNET) -> Path:
    """Two Opus API messages (the first written as two content-block lines), one subagent message."""
    main = tmp_path / "projects" / "p" / f"{SID}.jsonl"
    big = _usage(1000, 500, read=1_000_000, five=10_000, hour=20_000)
    _write(main, [
        _user("2026-10-20T10:00:00.000Z", "Price the receipts"),
        # One API response, two content blocks, two lines: counted once.
        _assistant("2026-10-20T10:00:05.000Z", "msg_1", OPUS, big, {"type": "text", "text": "looking"}),
        _assistant("2026-10-20T10:00:05.100Z", "msg_1", OPUS, big, {"type": "tool_use", "id": "t1", "name": "Task", "input": {}}),
        _assistant(LAST, "msg_2", OPUS, _usage(200, 100), {"type": "text", "text": "done"}),
    ])
    sub_usage = _usage(300, 60, read=5_000, five=1_000)
    _write(main.with_suffix("") / "subagents" / "agent-a1.jsonl", [
        _assistant("2026-10-20T10:01:00.000Z", "msg_s1", sub_model, sub_usage, {"type": "text", "text": "x"}, sidechain=True),
        _assistant("2026-10-20T10:01:00.100Z", "msg_s1", sub_model, sub_usage,
                   {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}, sidechain=True),
    ])
    return main


def _entry(tmp_path: Path, **kwargs) -> dict:
    repo = _make_repo(tmp_path / "repo")
    kwargs.setdefault("now", LAST_DT + timedelta(hours=2))
    report = cti.import_transcript(_transcript(tmp_path, sub_model=kwargs.pop("sub_model", SONNET)), repo_path=repo, **kwargs)
    assert report.status == "dry_run"
    return report.entry


def test_every_token_counter_is_filled_once_per_api_message_with_subagents_attributed_per_model(tmp_path):
    entry = _entry(tmp_path)
    capture = entry["capture"]
    assert entry["prompt_tokens"] == 1_500 and entry["completion_tokens"] == 660
    assert entry["cache_read_tokens"] == 1_005_000 and entry["cache_creation_tokens"] == 31_000
    assert capture["cache_creation_split"] == {"5m": 11_000, "1h": 20_000, "unsplit": 0}
    assert capture["usage_messages"] == 3 and capture["tokens_source"] == "transcript"
    by_model = capture["usage_by_model"]
    assert by_model[OPUS]["messages"] == 2 and by_model[OPUS]["cache_creation_1h"] == 20_000
    assert by_model[SONNET]["messages"] == 1 and by_model[SONNET]["input"] == 300
    assert capture["models_seen"] == [OPUS, SONNET]  # the subagent's model too
    receipt = build_shard_receipt(entry, index=0)
    assert (receipt.tokens_input, receipt.tokens_output) == (1_500, 660)
    assert (receipt.tokens_cache_read, receipt.tokens_cache_creation) == (1_005_000, 31_000)


def test_cost_is_priced_per_model_at_official_rates_and_labelled_an_estimate(tmp_path):
    entry = _entry(tmp_path)
    capture = entry["capture"]
    # Opus 5.5: 1200 in x $4 + 600 out x $20 + 1M read x $0.20 + 10k 5m x $5 + 20k 1h x $8 (per Mtok).
    assert capture["usage_by_model"][OPUS]["cost_usd"] == pytest.approx(0.4268)
    # Sonnet 5.5 subagent: 300 x $2 + 60 x $10 + 5k x $0.10 + 1k 5m x $2.50.
    assert capture["usage_by_model"][SONNET]["cost_usd"] == pytest.approx(0.0042)
    assert entry["estimated_cost"] == pytest.approx(0.431)
    assert entry["cost_provenance"] == "official_rate_estimate" and entry["cost_is_estimate"] is True
    assert "cost_not_recorded_reason" not in capture
    assert cti.REASON_NO_COST not in capture["completeness"]["reasons"]
    view = receipt_to_dict(build_shard_receipt(entry, index=0), extended=True)
    assert view["cost_usd"] == pytest.approx(0.431) and view["cost_is_estimate"] is True
    assert view["cost_provenance"] == "official_rate_estimate"


def test_a_model_without_a_list_rate_leaves_the_cost_unknown_never_zero(tmp_path):
    entry = _entry(tmp_path, sub_model="claude-mystery-9")
    capture = entry["capture"]
    assert "estimated_cost" not in entry and "cost_provenance" not in entry
    assert capture["cost_not_recorded_reason"] == cti.COST_REASON_NO_RATE
    assert capture["usage_by_model"][OPUS]["cost_usd"] == pytest.approx(0.4268)
    assert capture["usage_by_model"]["claude-mystery-9"]["cost_usd"] is None
    receipt = build_shard_receipt(entry, index=0)
    assert receipt.cost_display == "Not recorded" and receipt.cost_raw is None
    assert receipt.tokens_input == 1_500  # tokens stay known


def test_a_recent_session_stays_open_until_idle_or_marked_ended(tmp_path):
    recent = _entry(tmp_path / "a", now=LAST_DT + timedelta(minutes=10))
    assert cti.REASON_NO_END in recent["capture"]["completeness"]["reasons"]
    assert "ended_by" not in recent["capture"]["reconstructed"]
    assert eligibility(recent, now=LAST_DT + timedelta(minutes=10)).reason == "session_in_progress"

    flagged = _entry(tmp_path / "b", now=LAST_DT + timedelta(minutes=10), ended=True)
    assert flagged["capture"]["reconstructed"]["ended_by"] == cti.ENDED_BY_OPERATOR
    assert eligibility(flagged, now=LAST_DT + timedelta(minutes=10)).eligible

    idle = _entry(tmp_path / "c", now=LAST_DT + timedelta(minutes=31))
    assert idle["capture"]["reconstructed"]["ended_by"] == cti.ENDED_BY_IDLE
    assert eligibility(idle, now=LAST_DT + timedelta(minutes=31)).eligible
    # Honest labels: closed, not observed ending.
    for entry in (flagged, idle):
        capture = entry["capture"]
        assert capture["status"] == "reconstructed" and capture["session_end_observed"] is False
        (end,) = [r for r in capture["completeness"]["reasons"] if isinstance(r, dict)]
        assert end["kind"] == "session_end_not_observed" and "not observed" in end["detail"]


def test_a_capture_buffer_for_the_session_does_not_hold_back_a_closed_reconstruction(tmp_path):
    from openshard.adapters.claude_hooks import buffer_path
    from openshard.sync import client

    repo = _make_repo(tmp_path / "repo")
    entry = cti.import_transcript(_transcript(tmp_path), repo_path=repo, ended=True).entry
    stale = buffer_path(repo, SID)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"events": []}), encoding="utf-8")
    assert client._capture_buffer_open(repo, entry) is False


def test_cli_prints_agent_tokens_cost_per_model_and_session_state(tmp_path):
    from openshard.cli.main import cli

    repo = _make_repo(tmp_path / "repo")
    transcript = _transcript(tmp_path)
    result = CliRunner().invoke(cli, ["import", "claude-transcript", str(transcript), "--repo", str(repo), "--ended"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "agent: claude_code" in out
    assert "input 1,500  output 660  cache read 1,005,000  cache write 31,000 (1h: 20,000)  from 3 API message(s)" in out
    assert "cost: $0.4310 estimate at official list rates" in out
    assert f"{OPUS}: 2 message(s), $0.4268 est." in out and f"{SONNET}: 1 message(s), $0.0042 est." in out
    assert "session: ended (--ended; end not observed)" in out
    assert not (repo / ".openshard" / "runs.jsonl").exists()  # still a dry run
