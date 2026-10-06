"""Usage and cost evidence on the same Receipt: Cursor reconcile, honesty, sync.

Cursor hooks and Grok Bot rarely carry a cost, and Cursor hooks carry no
tokens. These tests pin that missing usage stays unknown (never $0), that
later Cursor-reported usage attaches to the existing Receipt (never a second
one), and that the closed receipt-sync payload does not grow a usage key.
Fixtures follow Cursor's Cloud Agents ``GET /v1/agents/{id}/usage`` and Admin
``POST /teams/filtered-usage-events`` shapes (docs checked 2026-10-06).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.adapters import grok_bot as gb
from openshard.adapters.claude_hooks import handle_hook
from openshard.adapters.cursor_usage import (
    KEY_CLOUD_AGENT,
    KEY_CONVERSATION,
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_MATCH,
    OUTCOME_PENDING,
    OUTCOME_RECORDED,
    OUTCOME_UNCHANGED,
    SCOPE_RUN,
    match_receipt,
    parse_agent_usage,
    parse_usage_events,
    receipt_keys,
    receipt_window_ms,
    reconcile_agent_usage,
    reconcile_usage_events,
    select_events,
    stored_cursor_run_id,
)
from openshard.adapters.otlp_logs import LogRecord
from openshard.cli.main import cli
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)
from openshard.history.store import load_history
from openshard.history.usage_evidence import (
    SOURCE_OPENSHARD,
    SOURCE_RUNTIME,
    SOURCE_VENDOR_TELEMETRY,
    STATUS_ESTIMATED,
    STATUS_OBSERVED,
    STATUS_RECONCILED,
    STATUS_UNKNOWN,
    SURFACE_CURSOR_ADMIN_EVENTS,
    SURFACE_CURSOR_AGENTS_API,
    effective_usage,
    load_usage_attestations,
    usage_attestations_for_entry,
    usage_from_record,
    usage_line,
)
from openshard.history.views import receipt_to_dict
from openshard.models.pricing import PRICING_SNAPSHOT_DATE, OfficialRate
from openshard.sync import client, config, envelope, outbox, transport
from openshard.sync import usage as sync_usage
from tests.capture_fixtures import _git, _make_repo

AGENT = "bc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RUN = "run-abc123"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
ENDPOINT = "https://platform.example.test"
STARTED = "2026-10-06T08:00:00.000Z"
ENDED = "2026-10-06T08:10:00.000Z"
EVENT_MS = int(datetime(2026, 10, 6, 8, 5, tzinfo=UTC).timestamp() * 1000)

ENVELOPE_KEYS = {"contract", "contract_version", "source", "receipt_id", "usage", "evidence"}
EVIDENCE_KEYS = {"attestation_id", "created_at", "kind", "correlation", "usage"}
USAGE_KEYS = {"version", "agent", "model", "tokens", "cost", "reconciled_by"}
TOKEN_KEYS = {
    "status", "source", "surface", "complete", "total",
    "input", "output", "cache_read", "cache_write", "reasoning", "other",
}
COST_KEYS = {
    "status", "source", "surface", "usd", "model_cost_usd", "platform_fee_usd", "complete", "rate",
}
CORR_KEYS = {"surface", "key", "key_value", "scope"}

_ZERO = {
    "inputTokens": 0, "outputTokens": 0, "cacheWriteTokens": 0, "cacheReadTokens": 0, "totalTokens": 0,
}


def _counts(inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> dict:
    return {
        "inputTokens": inp, "outputTokens": out,
        "cacheWriteTokens": cache_write, "cacheReadTokens": cache_read,
        "totalTokens": inp + out + cache_write + cache_read,
    }


def _agent_body(*runs: dict, total: dict | None = None) -> dict:
    body: dict = {"runs": list(runs)}
    if total is not None:
        body["totalUsage"] = total
    elif runs and all("usage" in r for r in runs):
        summed = {k: sum(int(r["usage"][k]) for r in runs) for k in _ZERO}
        body["totalUsage"] = summed
    return body


def _run(rid: str, usage: dict, *, uuid: str | None = "uuid-1") -> dict:
    item: dict = {"id": rid, "usage": usage}
    if uuid is not None:
        item["usageUuid"] = uuid
    return item


def _event(**fields: object) -> dict:
    base: dict = {
        "timestamp": EVENT_MS,
        "conversationId": AGENT,
        "cloudAgentId": AGENT,
        "model": "claude-sonnet-5-5",
        "isTokenBasedCall": True,
        "tokenUsage": {
            "inputTokens": 1000, "outputTokens": 200,
            "cacheWriteTokens": 10, "cacheReadTokens": 50, "totalCents": 12,
        },
        "chargedCents": 15,
        "cursorTokenFee": 3,
    }
    base.update(fields)
    return base


def _events(*events: dict) -> dict:
    return {"usageEvents": list(events), "pagination": {"hasNextPage": False}}


def _mid_ms(entry: dict) -> int:
    start, end = receipt_window_ms(entry)
    if start is not None and end is not None:
        return (start + end) // 2
    return EVENT_MS


def _outside_ms(entry: dict) -> int:
    start, _end = receipt_window_ms(entry)
    return (start or EVENT_MS) - 3 * 60 * 60 * 1000


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "widget")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


@pytest.fixture
def env(tmp_path: Path) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}


def _cursor_doc(event: str, repo: Path, sid: str, **fields: object) -> dict:
    base: dict = {
        "conversation_id": sid,
        "generation_id": RUN,
        "hook_event_name": event,
        "model": "claude-sonnet-5-5",
        "cursor_version": "1.7.0",
        "workspace_roots": [str(repo)],
    }
    base.update(fields)
    return base


def _cursor_session(repo: Path, sid: str = AGENT, *, generation_id: str = RUN, ended: bool = False) -> dict:
    """A Cloud Agent-shaped Cursor session: bc- conversation id, run- generation id."""
    def send(event: str, **fields: object) -> None:
        handle_hook(_cursor_doc(event, repo, sid, generation_id=generation_id, **fields), env={}, agent="cursor")

    send("beforeSubmitPrompt", prompt="implement the change")
    send("postToolUse", tool_name="Shell", tool_input={"command": "python -m pytest -q"})
    send("stop", status="completed")
    if ended:
        send("sessionEnd", session_id=sid, reason="completed", duration_ms=1000, final_status="completed")
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)[-1]


def _grok_otel(repo: Path, conversation_id: str = "gbconv-usage-1") -> dict:
    t0 = 1_790_000_000_000_000_000
    resource = {
        "service.name": "cursor", "cursor.team.id": 42, "cursor.surface": "grok_bot",
        "cursor.entrypoint": "web",
    }
    base = {"cursor.conversation.id": conversation_id, "cursor.grok_bot.provenance": "client"}
    recs = [
        LogRecord(
            resource=resource, body="grok_bot_shell_command",
            attributes={**base, "cursor.event.id": "e-shell",
                        "cursor.grok_bot.shell.command": "pytest -q",
                        "cursor.grok_bot.shell.allowed": True},
            time_unix_nano=t0, severity_number=9,
        ),
        LogRecord(
            resource=resource, body="api_request",
            attributes={**base, "cursor.event.id": "e-api",
                        "cursor.api.request.input_tokens": 1000,
                        "cursor.api.request.output_tokens": 200,
                        "cursor.api.request.cache_read_tokens": 50,
                        "cursor.api.request.cache_creation_tokens": 25,
                        "cursor.model.name": "grok-5"},
            time_unix_nano=t0 + 10**9, severity_number=9,
        ),
    ]
    gb.ingest_log_records(recs, repo)
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)[-1]


def _self_report(repo: Path) -> dict:
    gb.ingest_report(json.dumps({
        "schema": gb.REPORT_SCHEMA, "report_id": "rep-1",
        "conversation_id": AGENT, "task": "claimed work", "status": "completed",
    }), repo)
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)[-1]


# ---------------------------------------------------------------------------
# Cloud Agents /usage parser: pending vs recorded zero
# ---------------------------------------------------------------------------


class TestParseAgentUsage:
    def test_all_zero_without_usage_uuid_is_pending_not_recorded(self):
        parsed = parse_agent_usage({"runs": [_run(RUN, _ZERO, uuid=None)]})
        assert parsed.outcome == OUTCOME_PENDING and parsed.tokens == {}
        assert RUN in parsed.pending_runs

    def test_usage_uuid_with_zeros_is_recorded_zero(self):
        parsed = parse_agent_usage({"runs": [_run(RUN, _ZERO)]})
        assert parsed.outcome == OUTCOME_RECORDED
        assert parsed.tokens == {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}

    def test_recorded_tokens_include_cache_in_total(self):
        usage = _counts(100, 20, cache_write=10, cache_read=5)
        parsed = parse_agent_usage({"runs": [_run(RUN, usage)], "totalUsage": usage})
        assert parsed.outcome == OUTCOME_RECORDED and parsed.tokens["total"] == 135
        assert parsed.tokens["cache_read"] == 5 and parsed.tokens["cache_write"] == 10

    def test_total_must_equal_the_four_counters(self):
        bad = _counts(100, 20)
        bad["totalTokens"] = 999
        assert parse_agent_usage({"runs": [_run(RUN, bad)]}).outcome == "unavailable"

    def test_one_pending_run_refuses_the_whole_agent_total(self):
        body = _agent_body(_run("run-a", _counts(10, 1)), _run("run-b", _ZERO, uuid=None))
        parsed = parse_agent_usage(body)
        assert parsed.outcome == OUTCOME_PENDING
        assert "run-b" in parsed.pending_runs

    def test_run_id_scopes_to_that_run(self):
        body = _agent_body(_run("run-a", _counts(10, 1)), _run("run-b", _counts(100, 20)))
        parsed = parse_agent_usage(body, run_id="run-a")
        assert parsed.outcome == OUTCOME_RECORDED and parsed.scope == SCOPE_RUN
        assert parsed.tokens["total"] == 11


# ---------------------------------------------------------------------------
# Admin events and correlation
# ---------------------------------------------------------------------------


class TestAdminEvents:
    def test_charged_cents_are_cursor_reported_dollars(self):
        events = parse_usage_events(_events(_event()))
        assert events is not None and len(events) == 1
        assert events[0].charged_cents == 15 and events[0].model_cents == 12
        assert events[0].token_fee_cents == 3 and events[0].model == "claude-sonnet-5-5"

    def test_missing_cursor_token_fee_stays_unknown_not_zero(self):
        raw = _event()
        del raw["cursorTokenFee"]
        events = parse_usage_events(_events(raw))
        assert events is not None and events[0].token_fee_cents is None


class TestCorrelation:
    def test_cursor_hooks_conversation_is_a_key_and_bc_id_is_the_cloud_agent(self, repo):
        entry = _cursor_session(repo)
        keys = receipt_keys(entry)
        assert keys[KEY_CONVERSATION][0] == AGENT
        assert keys[KEY_CLOUD_AGENT][0] == AGENT
        assert stored_cursor_run_id(entry) == RUN
        assert entry["capture"]["cursor_generation_id"] == RUN
        assert entry["capture"]["cursor_run_id"] == RUN

    def test_grok_bot_otel_conversation_is_a_key(self, repo):
        entry = _grok_otel(repo)
        assert receipt_keys(entry)[KEY_CONVERSATION][0] == "gbconv-usage-1"
        assert KEY_CLOUD_AGENT not in receipt_keys(entry)

    def test_self_report_conversation_id_is_never_a_key(self, repo):
        entry = _self_report(repo)
        assert receipt_keys(entry) == {}
        assert entry["capture"].get("conversation_id") == AGENT

    def test_two_receipts_with_the_same_id_are_ambiguous(self, repo):
        first = _cursor_session(repo)
        second = dict(first)
        second["receipt_id"] = "rcpt_" + "b" * 32
        second["run_id"] = "run_other_segment"
        match = match_receipt([first, second], KEY_CLOUD_AGENT, AGENT)
        assert match.refusal == OUTCOME_AMBIGUOUS
        result = reconcile_agent_usage(
            repo, [first, second], AGENT, _agent_body(_run(RUN, _counts(8, 2))),
        )
        assert result.outcome == OUTCOME_AMBIGUOUS
        assert load_usage_attestations(repo / ".openshard") == []

    def test_event_outside_the_window_is_ambiguous(self, repo):
        entry = _cursor_session(repo)
        selected = select_events(
            parse_usage_events(_events(_event(timestamp=_outside_ms(entry)))) or [], entry,
        )
        assert selected.outcome == OUTCOME_AMBIGUOUS

    def test_event_naming_a_different_cloud_agent_is_ambiguous(self, repo):
        entry = _cursor_session(repo)
        other = "bc-ffffffff-ffff-ffff-ffff-ffffffffffff"
        selected = select_events(
            parse_usage_events(_events(_event(
                timestamp=_mid_ms(entry), conversationId=AGENT, cloudAgentId=other,
            ))) or [], entry,
        )
        assert selected.outcome == OUTCOME_AMBIGUOUS

    def test_no_matching_event_is_no_match(self, repo):
        entry = _cursor_session(repo)
        selected = select_events(
            parse_usage_events(_events(_event(
                timestamp=_mid_ms(entry), conversationId="other", cloudAgentId="other",
            ))) or [], entry,
        )
        assert selected.outcome == OUTCOME_NO_MATCH


# ---------------------------------------------------------------------------
# Record usage, merge, honesty of presentation
# ---------------------------------------------------------------------------


class TestUsageFromRecord:
    def test_cursor_hooks_usage_is_unknown_not_zero(self, repo):
        entry = _cursor_session(repo)
        block = usage_from_record(entry)
        assert block["tokens"]["status"] == STATUS_UNKNOWN and block["tokens"]["total"] is None
        assert block["cost"]["status"] == STATUS_UNKNOWN and block["cost"]["usd"] is None
        assert block["agent"] == "cursor"
        text = usage_line(block)
        assert "$0" not in text and "0 tokens" not in text
        assert "unavailable" in text.lower()
        compact = render_compact_shard_receipt(build_shard_receipt(entry))
        assert "Usage" in compact and "$0" not in compact

    def test_grok_otel_tokens_are_observed_vendor_telemetry_cost_unknown(self, repo):
        entry = _grok_otel(repo)
        block = usage_from_record(entry)
        assert block["tokens"]["status"] == STATUS_OBSERVED
        assert block["tokens"]["source"] == SOURCE_VENDOR_TELEMETRY
        assert block["tokens"]["input"] == 1000 and block["tokens"]["output"] == 200
        assert block["tokens"]["cache_read"] == 50 and block["tokens"]["cache_write"] == 25
        assert block["tokens"]["total"] == 1275  # cache included
        assert block["cost"]["status"] == STATUS_UNKNOWN and block["cost"]["usd"] is None
        assert block["model"]["id"] == "grok-5"
        assert block["agent"] == "grok_bot"
        # Agent is not a model: never invent a Grok model id from the agent name alone.
        assert block["model"]["id"] != "grok_bot"
        line = usage_line(block)
        assert "1,275 tokens" in line and "cost unknown" in line

    def test_no_token_provenance_is_unknown_even_if_counts_are_present(self):
        block = usage_from_record({
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "capture": {"agent": "cursor"},
        })
        assert block["tokens"]["status"] == STATUS_UNKNOWN and block["tokens"]["total"] is None

    def test_retry_attempts_are_added_when_complete_else_marked_incomplete(self):
        complete = usage_from_record({
            "tokens_provenance": "provider_reported",
            "prompt_tokens": 100, "completion_tokens": 20,
            "retry_triggered": True,
            "retry_attempts": [{"model": "claude-sonnet-5-5", "prompt_tokens": 50, "completion_tokens": 10}],
        })
        assert complete["tokens"]["input"] == 150 and complete["tokens"]["output"] == 30
        assert complete["tokens"]["complete"] is True
        incomplete = usage_from_record({
            "tokens_provenance": "provider_reported",
            "prompt_tokens": 100, "completion_tokens": 20,
            "retry_triggered": True,
        })
        assert incomplete["tokens"]["complete"] is False


class TestReconcileOntoSameReceipt:
    def test_cloud_agents_tokens_attach_to_the_existing_receipt(self, repo):
        entry = _cursor_session(repo)
        rid = entry["receipt_id"]
        body = _agent_body(_run(RUN, _counts(100, 20, cache_write=10, cache_read=5)))
        result = reconcile_agent_usage(repo, [entry], AGENT, body)
        assert result.outcome == OUTCOME_RECORDED and result.receipt_id == rid
        atts = usage_attestations_for_entry(entry, load_usage_attestations(repo / ".openshard"))
        assert len(atts) == 1 and atts[0]["receipt_id"] == rid
        block = effective_usage(entry, atts)
        assert block["tokens"]["status"] == STATUS_RECONCILED
        assert block["tokens"]["source"] == SOURCE_RUNTIME
        assert block["tokens"]["surface"] == SURFACE_CURSOR_AGENTS_API
        assert block["tokens"]["total"] == 135
        # Model is not invented from the agent. Cost is an official-rate estimate when the model is known.
        assert block["model"]["id"] == "claude-sonnet-5-5"  # from the record, kept
        assert block["cost"]["status"] == STATUS_ESTIMATED
        assert block["cost"]["source"] == SOURCE_OPENSHARD
        assert block["cost"]["rate"]["pricing_version"] == PRICING_SNAPSHOT_DATE
        assert "~" in usage_line(block) or "estimated" in usage_line(block)
        # The stored record itself is unchanged.
        stored = load_history(repo / ".openshard" / "runs.jsonl", coerce=False)[-1]
        assert stored["receipt_id"] == rid and "prompt_tokens" not in stored

    def test_pending_records_nothing(self, repo):
        entry = _cursor_session(repo)
        result = reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _ZERO, uuid=None)))
        assert result.outcome == OUTCOME_PENDING
        assert load_usage_attestations(repo / ".openshard") == []
        block = effective_usage(entry, [])
        assert block["cost"]["usd"] is None and block["tokens"]["total"] is None

    def test_auto_scopes_to_the_stored_run_id(self, repo):
        entry = _cursor_session(repo)
        body = _agent_body(
            _run(RUN, _counts(10, 1)),
            _run("run-later", _counts(9999, 9999)),
        )
        result = reconcile_agent_usage(repo, [entry], AGENT, body)
        assert result.outcome == OUTCOME_RECORDED
        tokens = result.attestation["usage"]["tokens"]
        assert tokens["total"] == 11
        assert result.attestation["correlation"]["scope"] == SCOPE_RUN
        assert result.attestation["correlation"]["run_id"] == RUN

    def test_admin_events_are_cursor_reported_cost_on_the_same_receipt(self, repo):
        entry = _cursor_session(repo)
        result = reconcile_usage_events(
            repo, [entry], entry, _events(_event(timestamp=_mid_ms(entry))),
        )
        assert result.outcome == OUTCOME_RECORDED and result.receipt_id == entry["receipt_id"]
        block = effective_usage(entry, usage_attestations_for_entry(
            entry, load_usage_attestations(repo / ".openshard"),
        ))
        assert block["tokens"]["status"] == STATUS_RECONCILED
        assert block["tokens"]["surface"] == SURFACE_CURSOR_ADMIN_EVENTS
        assert block["tokens"]["total"] == 1260  # 1000+200+10+50
        assert block["cost"]["status"] == STATUS_RECONCILED
        assert block["cost"]["source"] == SOURCE_RUNTIME
        assert block["cost"]["usd"] == 0.15
        assert block["cost"]["model_cost_usd"] == 0.12
        assert block["cost"]["platform_fee_usd"] == 0.03
        assert block["model"]["id"] == "claude-sonnet-5-5"
        line = usage_line(block)
        assert "Cursor-reported" in line and "(reconciled)" in line
        assert "$0.15" in line

    def test_delayed_reconcile_does_not_create_a_second_receipt(self, repo):
        entry = _cursor_session(repo)
        before = load_history(repo / ".openshard" / "runs.jsonl", coerce=False)
        reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(8, 2))))
        after = load_history(repo / ".openshard" / "runs.jsonl", coerce=False)
        assert [e["receipt_id"] for e in after] == [e["receipt_id"] for e in before]
        assert len(after) == 1

    def test_identical_reconcile_is_unchanged(self, repo):
        entry = _cursor_session(repo)
        body = _agent_body(_run(RUN, _counts(8, 2)))
        first = reconcile_agent_usage(repo, [entry], AGENT, body, created_at=STARTED)
        second = reconcile_agent_usage(repo, [entry], AGENT, body, created_at=ENDED)
        assert first.outcome == OUTCOME_RECORDED and second.outcome == OUTCOME_UNCHANGED
        assert len(load_usage_attestations(repo / ".openshard")) == 1

    def test_no_match_and_self_report_refuse_without_writing(self, repo):
        entry = _self_report(repo)
        result = reconcile_usage_events(repo, [entry], entry, _events(_event()))
        assert result.outcome == OUTCOME_NO_MATCH
        assert load_usage_attestations(repo / ".openshard") == []

    def test_usage_never_crosses_receipts(self, repo):
        target = _cursor_session(repo)
        other_root = _make_repo(repo.parent / "other")
        other = _cursor_session(other_root, sid="bc-bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")
        reconcile_agent_usage(repo, [target], AGENT, _agent_body(_run(RUN, _counts(8, 2))))
        atts = load_usage_attestations(repo / ".openshard")
        assert usage_attestations_for_entry(other, atts) == []
        assert effective_usage(other, atts)["tokens"]["total"] is None

    def test_historical_rate_is_kept_when_the_card_changes(self, repo):
        entry = _cursor_session(repo)
        reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(1_000_000, 0))))
        atts = load_usage_attestations(repo / ".openshard")
        stored_rate = atts[0]["usage"]["cost"]["rate"]["input_per_mtok"]
        fake = OfficialRate("anthropic", "claude-sonnet-5-5", 99.0, 99.0, 0.20, 2.50, as_of="2099-01-01")
        with patch("openshard.models.pricing.official_rate", return_value=fake):
            block = effective_usage(entry, atts)
        assert block["cost"]["rate"]["input_per_mtok"] == stored_rate
        assert block["cost"]["rate"]["pricing_version"] == PRICING_SNAPSHOT_DATE

    def test_unknown_model_does_not_invent_grok(self, repo):
        entry = _cursor_session(repo)
        entry["execution_model"] = "unknown"
        entry["capture"]["models_seen"] = []
        entry["capture"]["model_source"] = "not_captured"
        result = reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(10, 1))))
        usage = result.attestation["usage"]
        assert usage["model"]["id"] is None and usage["model"]["models"] == []
        assert usage["cost"]["usd"] is None  # no list rate without a model
        merged = effective_usage(entry, [result.attestation])
        assert merged["model"]["id"] is None


# ---------------------------------------------------------------------------
# last / views / closed sync envelope
# ---------------------------------------------------------------------------


class TestReceiptViewsAndSync:
    def test_last_json_has_usage_receipt_to_dict_does_not(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        entry = _cursor_session(repo)
        reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(100, 20))))
        payload = json.loads(CliRunner().invoke(cli, ["last", "--json"], catch_exceptions=False).output)
        assert payload["usage"]["tokens"]["total"] == 120
        assert payload["usage"]["tokens"]["status"] == STATUS_RECONCILED
        receipt = build_shard_receipt(
            entry, usage_attestations=load_usage_attestations(repo / ".openshard"),
        )
        local = receipt_to_dict(receipt, extended=True)
        assert "usage" not in local
        hosted = envelope.receipt_payload(entry, 0)
        assert "usage" not in hosted
        assert hosted == local or hosted["receipt_id"] == local["receipt_id"]
        compact = render_compact_shard_receipt(receipt)
        full = render_full_shard_receipt(receipt)
        assert "120 tokens" in compact and "120 tokens" in full

    def test_usage_evidence_envelope_has_closed_keys(self, repo):
        entry = _cursor_session(repo)
        reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(100, 20))))
        doc = sync_usage.build_usage_envelope(
            entry, load_usage_attestations(repo / ".openshard"), core_version="9.9.9",
        )
        assert doc is not None and set(doc) == ENVELOPE_KEYS
        assert doc["contract"] == "openshard.usage-evidence" and doc["contract_version"] == "1"
        assert doc["source"] == {"product": "openshard-core", "version": "9.9.9"}
        assert doc["receipt_id"] == entry["receipt_id"]
        assert set(doc["usage"]) == USAGE_KEYS
        assert set(doc["usage"]["tokens"]) == TOKEN_KEYS
        assert set(doc["usage"]["cost"]) == COST_KEYS
        assert set(doc["evidence"][0]) == EVIDENCE_KEYS
        assert set(doc["evidence"][0]["correlation"]) == CORR_KEYS
        blob = json.dumps(doc)
        assert str(repo) not in blob
        before = envelope.receipt_payload(entry, 0)
        after = envelope.receipt_payload(
            load_history(repo / ".openshard" / "runs.jsonl", coerce=True)[-1], 0,
        )
        assert after == before

    def test_nothing_to_send_without_attestations(self, repo):
        entry = _cursor_session(repo)
        assert sync_usage.build_usage_envelope(entry, [], core_version="9.9.9") is None


class TestUsageFlush:
    @pytest.fixture
    def link(self, env: dict) -> config.PlatformLink:
        return config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)

    @pytest.fixture
    def recording(self):
        rt = transport.RecordingPlatformTransport()
        client.configure(transport=rt, repo_config={})
        yield rt
        client.configure(transport=None, repo_config=None)

    def test_usage_follows_an_already_synced_receipt_and_is_sent_once(self, repo, env, link, recording):
        entry = _cursor_session(repo, ended=True)
        first = client.flush(repo, env=env)
        assert (first.created, first.usage_sent) == (1, 0)
        reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(8, 2))))
        second = client.flush(repo, env=env)
        assert (second.sent, second.usage_sent, second.usage_recorded) == (0, 1, 1)
        assert recording.usage_envelopes[0]["receipt_id"] == entry["receipt_id"]
        assert len(recording.envelopes) == 1  # the Receipt was never resent
        for _ in range(3):
            assert client.flush(repo, env=env).usage_sent == 0
        record = outbox.load_outbox(repo)[entry["receipt_id"]]
        assert record["state"] == "synced" and record["usage_state"] == "synced"
        assert record["usage_hash"] == sync_usage.usage_hash(recording.usage_envelopes[0])

    def test_a_platform_without_the_route_is_skipped_quietly(self, repo, env, link):
        entry = _cursor_session(repo, ended=True)
        reconcile_agent_usage(repo, [entry], AGENT, _agent_body(_run(RUN, _counts(8, 2))))
        rt = transport.RecordingPlatformTransport(
            usage_results=[transport.SendResult(transport.KIND_UNSUPPORTED, 404)],
        )
        client.configure(transport=rt, repo_config={})
        try:
            report = client.flush(repo, env=env)
            assert report.usage_unsupported is True
            assert report.created == 1
        finally:
            client.configure(transport=None, repo_config=None)

    def test_https_posts_to_the_usage_evidence_route(self):
        class Handler:
            seen: list[dict] = []
            status = 201
            body = b"{}"

        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                Handler.seen.append({
                    "path": self.path, "auth": self.headers.get("Authorization"),
                    "body": json.loads(raw.decode("utf-8")),
                })
                self.send_response(Handler.status)
                self.end_headers()
                self.wfile.write(Handler.body)

            def log_message(self, *_args: object) -> None:
                return

        httpd = HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            port = httpd.server_address[1]
            link = config.PlatformLink(
                endpoint=f"http://127.0.0.1:{port}", organisation_id=ORG, api_key=KEY,
                linked_at=None, source="file",
            )
            t = transport.HttpsPlatformTransport(link, user_agent="openshard/test", timeout=5.0)
            rid = "rcpt_" + "a" * 32
            assert t.send_usage(rid, {"contract": "openshard.usage-evidence"}).kind == "created"
            seen = Handler.seen[-1]
            assert seen["path"] == f"/v1/orgs/{ORG}/receipts/{rid}/usage-evidence"
            assert seen["auth"] == f"Bearer {KEY}"
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestUsageCli:
    def test_show_and_reconcile_from_file(self, repo, tmp_path, monkeypatch):
        monkeypatch.chdir(repo)
        entry = _cursor_session(repo)
        shown = CliRunner().invoke(cli, ["usage", "show"], catch_exceptions=False)
        assert shown.exit_code == 0
        assert "unavailable" in shown.output.lower() or "unknown" in shown.output.lower()
        assert "$0" not in shown.output

        fixture = tmp_path / "usage.json"
        fixture.write_text(json.dumps(_agent_body(_run(RUN, _ZERO, uuid=None))), encoding="utf-8")
        pending = CliRunner().invoke(
            cli, ["usage", "reconcile", "cursor-agent", AGENT, "--from-file", str(fixture)],
            catch_exceptions=False,
        )
        assert pending.exit_code == 0
        assert "Pending" in pending.output
        assert load_usage_attestations(repo / ".openshard") == []

        fixture.write_text(json.dumps(_agent_body(_run(RUN, _counts(40, 2)))), encoding="utf-8")
        recorded = CliRunner().invoke(
            cli, ["usage", "reconcile", "cursor-agent", AGENT, "--from-file", str(fixture), "--json"],
            catch_exceptions=False,
        )
        assert recorded.exit_code == 0
        payload = json.loads(recorded.output)
        assert payload["outcome"] == OUTCOME_RECORDED and payload["receipt_id"] == entry["receipt_id"]
        assert payload["usage"]["tokens"]["total"] == 42

        shown2 = json.loads(CliRunner().invoke(cli, ["usage", "show", "--json"], catch_exceptions=False).output)
        assert shown2["usage"]["tokens"]["total"] == 42
        assert shown2["receipt_id"] == entry["receipt_id"]

    def test_events_from_file_and_no_match_exits_one(self, repo, tmp_path, monkeypatch):
        monkeypatch.chdir(repo)
        entry = _cursor_session(repo)
        fixture = tmp_path / "events.json"
        fixture.write_text(json.dumps(_events(_event(
            timestamp=_mid_ms(entry), conversationId="nope", cloudAgentId="nope",
        ))), encoding="utf-8")
        refused = CliRunner().invoke(
            cli, ["usage", "reconcile", "cursor-events", "--from-file", str(fixture)],
            catch_exceptions=False,
        )
        assert refused.exit_code == 1
        assert "no_match" in refused.output or "Not recorded" in refused.output
        assert load_usage_attestations(repo / ".openshard") == []

        fixture.write_text(json.dumps(_events(_event(timestamp=_mid_ms(entry)))), encoding="utf-8")
        ok = CliRunner().invoke(
            cli, ["usage", "reconcile", "cursor-events", "--from-file", str(fixture)],
            catch_exceptions=False,
        )
        assert ok.exit_code == 0 and "Recorded" in ok.output
