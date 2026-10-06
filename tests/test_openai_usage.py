"""OpenAI usage reconciliation: exact Codex thread binding and honest cost."""
from __future__ import annotations

from pathlib import Path

import pytest

from openshard.adapters.openai_usage import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_MATCH,
    OUTCOME_UNAVAILABLE,
    parse_app_server_notification,
    reconcile_app_server_usage,
)
from openshard.history.usage_evidence import (
    OUTCOME_RECORDED,
    SOURCE_OPENSHARD,
    SOURCE_RUNTIME,
    STATUS_ESTIMATED,
    STATUS_RECONCILED,
    SURFACE_OPENAI_CODEX_APP_SERVER,
    effective_usage,
    load_usage_attestations,
)

THREAD = "019a4f3c-6c1e-7d2b-9c3e-2f4a5b6c7d8e"
RID = "rcpt_" + "a" * 32


def entry(receipt_id: str = RID, thread_id: str = THREAD) -> dict:
    return {
        "receipt_id": receipt_id,
        "shard_id": "shard-20261006-0001",
        "run_id": "run-openai-1",
        "executor": "codex_hooks",
        "execution_model": "gpt-5.6-sol",
        "capture": {
            "source": "codex_hooks",
            "agent": "codex",
            "session_id": thread_id,
            "models_seen": ["gpt-5.6-sol"],
            "model_source": "codex_hook",
        },
    }


def notification(thread_id: str = THREAD, *, total: int = 5_300) -> dict:
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread_id,
            "turnId": "turn_0001",
            "tokenUsage": {
                "last": {
                    "inputTokens": 100,
                    "cachedInputTokens": 0,
                    "outputTokens": 20,
                    "reasoningOutputTokens": 10,
                    "totalTokens": 120,
                },
                "total": {
                    "inputTokens": 5_000,
                    "cachedInputTokens": 4_000,
                    "outputTokens": 300,
                    "reasoningOutputTokens": 100,
                    "totalTokens": total,
                },
            },
        },
    }


def test_parser_normalises_cached_input_and_does_not_double_count_reasoning() -> None:
    parsed = parse_app_server_notification(notification())
    assert parsed.outcome == OUTCOME_RECORDED
    assert parsed.thread_id == THREAD
    assert parsed.input == 1_000
    assert parsed.cache_read == 4_000
    assert parsed.output == 300
    assert parsed.total == 5_300


def test_parser_fails_closed_on_wrong_total_or_wrong_method() -> None:
    assert parse_app_server_notification(notification(total=999)).outcome == OUTCOME_UNAVAILABLE
    assert parse_app_server_notification({"method": "thread/started", "params": {}}).outcome == OUTCOME_UNAVAILABLE


def test_reconciles_exact_thread_onto_existing_receipt_and_prices_known_model(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    current = entry()

    result = reconcile_app_server_usage(
        root,
        [current],
        notification(),
        created_at="2026-10-06T12:00:00.000Z",
    )
    assert result.outcome == OUTCOME_RECORDED
    assert result.receipt_id == RID

    attestations = load_usage_attestations(root / ".openshard")
    assert len(attestations) == 1
    att = attestations[0]
    assert att["correlation"] == {
        "surface": SURFACE_OPENAI_CODEX_APP_SERVER,
        "key": "codex_thread_id",
        "key_value": THREAD,
        "scope": "thread_total",
    }

    usage = effective_usage(current, attestations)
    assert usage["tokens"]["status"] == STATUS_RECONCILED
    assert usage["tokens"]["source"] == SOURCE_RUNTIME
    assert usage["tokens"]["surface"] == SURFACE_OPENAI_CODEX_APP_SERVER
    assert usage["tokens"]["input"] == 1_000
    assert usage["tokens"]["cache_read"] == 4_000
    assert usage["tokens"]["output"] == 300
    assert usage["tokens"]["total"] == 5_300

    assert usage["cost"]["status"] == STATUS_ESTIMATED
    assert usage["cost"]["source"] == SOURCE_OPENSHARD
    assert usage["cost"]["surface"] == SURFACE_OPENAI_CODEX_APP_SERVER
    assert usage["cost"]["usd"] == pytest.approx(0.0116)
    assert usage["cost"]["rate"]["model_id"] == "gpt-5.6-sol"


def test_no_exact_thread_match_records_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    result = reconcile_app_server_usage(root, [entry()], notification("different-thread"))
    assert result.outcome == OUTCOME_NO_MATCH
    assert load_usage_attestations(root / ".openshard") == []


def test_duplicate_thread_is_ambiguous_not_split(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    first = entry()
    second = entry("rcpt_" + "b" * 32)
    result = reconcile_app_server_usage(root, [first, second], notification())
    assert result.outcome == OUTCOME_AMBIGUOUS
    assert load_usage_attestations(root / ".openshard") == []


def test_regular_chat_or_non_codex_receipt_is_never_promoted_to_codex(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    chat = entry()
    chat["executor"] = "github_cloud"
    chat["capture"]["source"] = "github_observed"
    result = reconcile_app_server_usage(root, [chat], notification())
    assert result.outcome == OUTCOME_NO_MATCH
    assert load_usage_attestations(root / ".openshard") == []
