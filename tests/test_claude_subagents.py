"""Each Claude Code subagent's own facts, read from the files Claude Code writes for it.

The fixtures follow what Claude Code 2.1 wrote in a real session with one
foreground ``Explore`` subagent and one background ``general-purpose`` one:
``subagents/agent-<id>.jsonl`` and ``.meta.json`` beside the session
transcript, the foreground outcome as ``toolUseResult.status`` and the
background one as a ``<task-notification>`` in the session transcript.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from openshard.adapters import claude_hooks as ch
from openshard.adapters import claude_subagents as cs
from openshard.history import task_edges
from openshard.sync import client, config, outbox, transport
from openshard.sync.envelope import build_envelope, rejected_for_subagents, subagents_block
from openshard.sync.transport import SendResult
from tests.capture_fixtures import _lines, _make_repo

SID = "61616161-6161-4161-8161-616161616161"
FG, BG, LOST = "a4515c6b384db9db8", "a27b7cfbeabdb7981", "a99999999999999990"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    return _make_repo(tmp_path / "factory")


def _assistant(mid: str, model: str, at: str, *, out: int, cache_5m: int = 0, tool: str | None = None) -> dict:
    content = [{"type": "tool_use", "id": tool, "name": "Bash", "input": {}}] if tool else [{"type": "text", "text": "x"}]
    return {"type": "assistant", "sessionId": SID, "timestamp": at, "message": {
        "id": mid, "model": model, "role": "assistant", "content": content,
        "usage": {"input_tokens": 2, "output_tokens": out, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": cache_5m, "service_tier": "standard", "speed": "standard",
                  "inference_geo": "global", "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                  "cache_creation": {"ephemeral_5m_input_tokens": cache_5m, "ephemeral_1h_input_tokens": 0}}}}


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _session_files(*, bg_status: str = "completed") -> Path:
    """Written mid-turn, stamped inside the turn: offsets are milliseconds after :data:`BASE`."""
    main = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "c--work-repo" / f"{SID}.jsonl"
    notification = (f"<task-notification>\n<task-id>{BG}</task-id>\n<status>{bg_status}</status>\n"
                    "<result>RAW SUBAGENT OUTPUT</result>\n<usage><subagent_tokens>9</subagent_tokens></usage>\n"
                    "</task-notification>")
    _jsonl(main, [
        _assistant("m-lead", "claude-opus-5-5", _at(0.0), out=50, tool="toolu_fg"),
        {"type": "user", "timestamp": _at(1.0), "toolUseResult": {
            "isAsync": True, "status": "async_launched", "agentId": BG, "resolvedModel": "claude-haiku-5-5"}},
        {"type": "user", "timestamp": _at(9.0), "toolUseResult": {
            "status": "completed", "agentId": FG, "agentType": "Explore", "totalTokens": 15193}},
        {"type": "user", "timestamp": _at(12.0), "message": {"role": "user", "content": notification}},
    ])
    sub = main.with_suffix("") / "subagents"
    _jsonl(sub / f"agent-{FG}.jsonl", [
        {"type": "user", "timestamp": _at(2.0), "agentId": FG, "isSidechain": True,
         "message": {"role": "user", "content": "RAW SUBAGENT PROMPT"}},
        _assistant("m-fg-1", "claude-sonnet-5-5", _at(3.0), out=90, cache_5m=12334, tool="toolu_a"),
        _assistant("m-fg-2", "claude-sonnet-5-5", _at(6.5), out=66, cache_5m=145),
    ])
    (sub / f"agent-{FG}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "toolUseId": "toolu_fg", "spawnDepth": 1, "requestShape": "foreground", "finished": True}))
    _jsonl(sub / f"agent-{BG}.jsonl", [
        {"type": "user", "timestamp": _at(4.0), "agentId": BG, "message": {"role": "user", "content": "p"}},
        _assistant("m-bg-1", "claude-haiku-5-5", _at(5.0), out=128, cache_5m=25071, tool="toolu_b"),
        _assistant("m-bg-1", "claude-haiku-5-5", _at(5.0), out=128, cache_5m=25071, tool="toolu_b"),
        _assistant("m-bg-2", "claude-haiku-5-5", _at(11.0), out=10),
    ])
    (sub / f"agent-{BG}.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "toolUseId": "toolu_bg", "spawnDepth": 1, "requestShape": "background"}))
    return main


BASE: list[datetime] = []


def _at(ms: float) -> str:
    return (BASE[-1] + timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _capture(repo: Path, bg_status: str = "completed") -> dict:
    """A turn whose subagents ran between the prompt and Stop, as the per-turn Receipt windows it."""
    main = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "c--work-repo" / f"{SID}.jsonl"
    base = {"session_id": SID, "cwd": str(repo), "transcript_path": str(main)}
    for doc in ({**base, "hook_event_name": "SessionStart", "source": "startup"},
                {**base, "hook_event_name": "UserPromptSubmit", "prompt": "split the work"}):
        ch.handle_claude_hook(doc, env={"CLAUDE_PROJECT_DIR": str(repo)})
    BASE.append(datetime.now(UTC) + timedelta(milliseconds=5))
    _session_files(bg_status=bg_status)
    while datetime.now(UTC) < BASE[-1] + timedelta(milliseconds=40):
        time.sleep(0.005)
    docs = [{**base, "hook_event_name": "SubagentStart", "agent_id": FG, "agent_type": "Explore"},
            {**base, "hook_event_name": "SubagentStart", "agent_id": BG, "agent_type": "general-purpose"},
            {**base, "hook_event_name": "SubagentStart", "agent_id": LOST, "agent_type": "Plan"},
            {**base, "hook_event_name": "SubagentStop", "agent_id": FG, "agent_type": "Explore",
             "agent_transcript_path": str(main.with_suffix("") / "subagents" / f"agent-{FG}.jsonl")},
            {**base, "hook_event_name": "SubagentStop", "agent_id": BG, "agent_type": "general-purpose"},
            {**base, "hook_event_name": "Stop"},
            {**base, "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}]
    for doc in docs:
        ch.handle_claude_hook(doc, env={"CLAUDE_PROJECT_DIR": str(repo)})
    [entry] = [e for e in _lines(repo) if (e.get("capture") or {}).get("session_id") == SID]
    return entry


def test_each_subagent_gets_its_own_model_tokens_cost_times_tool_calls_and_outcome(repo):
    entry = _capture(repo)
    runs = {r["agent_id"]: r for r in entry["capture"]["subagent_runs"]}
    fg, bg, lost = runs[FG], runs[BG], runs[LOST]

    assert fg["agent_type"] == "Explore" and fg["parent_agent"] == "lead" and fg["parent_source"] == "agent_reported"
    assert fg["models"] == ["claude-sonnet-5-5"] and fg["models_source"] == "provider_reported"
    assert fg["tokens"] == {"input_tokens": 4, "output_tokens": 156, "cache_read_tokens": 0, "cache_write_tokens": 12479, "total_tokens": 12639}
    assert fg["cost_estimate"] is True and fg["cost_source"] == "openshard_calculated"
    assert fg["cost_provenance"] == "official_rate_estimate" and fg["cost_usd"] == pytest.approx(
        (4 * 2.0 + 156 * 10.0 + 12479 * 2.5) / 1e6, abs=1e-6)
    assert (fg["started_at"], fg["ended_at"]) == (_at(2.0), _at(6.5))
    assert fg["tool_calls"] == 1 and fg["result"] == "finished" and fg["result_source"] == "agent_reported"

    # Background: outcome from the task notification; a re-reported streamed message counts once.
    assert bg["models"] == ["claude-haiku-5-5"] and bg["tokens"]["output_tokens"] == 138 and bg["result"] == "finished"
    assert bg["tool_calls"] == 1 and bg["ended_at"] == _at(11.0)

    # Reported by the hooks but no transcript: every fact unknown with its reason, never 0.
    assert lost["agent_type"] == "Plan" and lost["tokens"] is None and lost["cost_usd"] is None
    assert lost["tokens_not_recorded_reason"] == lost["cost_not_recorded_reason"] == "transcript_unavailable"
    assert lost["tool_calls"] is None and lost["started_at"] is None and lost["ended_at"] is None
    assert lost["parent_agent"] is None and lost["parent_not_recorded_reason"] == "parent_not_recorded"
    assert lost["result"] == "unknown" and lost["result_reason"] == "stop_not_observed" and lost["result_source"] is None

    cost = entry["capture"]["subagent_cost"]
    assert cost["status"] == "within_session_total" and cost["subagents_priced"] == 2 and cost["subagents"] == 3
    assert cost["subagents_usd"] == pytest.approx(fg["cost_usd"] + bg["cost_usd"]) and cost["subagents_usd"] < entry["estimated_cost"]
    assert cost["session_usd"] == entry["estimated_cost"]

    stored = json.dumps(entry)
    assert "RAW SUBAGENT" not in stored and "subagents/agent-" not in stored  # no content, no paths


def test_the_receipt_shows_each_subagent_and_unknowns_say_so(repo):
    entry = _capture(repo)
    rows = task_edges.task_edge_lines(task_edges.task_edges_block(entry))
    text = {value.split(" ", 1)[0]: value for label, value in rows if label == "Subagent"}
    assert "Explore · from lead · claude-sonnet-5-5 · 12,639 tokens · $0." in text[FG]
    assert text[FG].endswith("· 0s · 1 tool call · finished (agent-reported)")
    assert text[LOST].endswith("Plan · parent not recorded · model not recorded · tokens not recorded · "
                               "cost not recorded · tool calls not recorded · unknown (agent-reported)")
    assert not any(label == "Subagent cost" for label, _ in rows)  # reconciled: nothing to warn about


def test_a_failed_background_subagent_reads_failed(repo):
    entry = _capture(repo, "failed")
    assert {r["agent_id"]: r["result"] for r in entry["capture"]["subagent_runs"]}[BG] == "failed"


SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text("utf-8"))
OLD_PLATFORM = SendResult(transport.KIND_REJECTED, 400, "invalid_payload",
                          [{"path": "receipt", "message": 'Unrecognized key: "subagents"'}])


def test_the_envelope_carries_each_subagent_in_the_platform_contract(repo):
    entry = _capture(repo)
    envelope = build_envelope(entry, 0, core_version="0.0.0")
    jsonschema.validate(envelope, SCHEMA)
    block = envelope["receipt"]["subagents"]
    assert [a["agent_id"] for a in block["agents"]] == [FG, BG, LOST]
    assert block["agents"][0] == entry["capture"]["subagent_runs"][0]
    assert block["cost"]["status"] == "within_session_total"
    assert "RAW SUBAGENT" not in json.dumps(envelope)


def test_a_receipt_without_subagent_runs_sends_no_block():
    assert subagents_block({"subagent_edges": [{"agent_id": "x"}]}) is None and subagents_block(None) is None


def test_a_platform_that_predates_the_block_still_gets_the_receipt(repo, tmp_path):
    env = {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}
    config.save_link(endpoint="https://platform.example", organisation_id="0b6c1f7e-2a4d-4c8e-9f10-a1b2c3d4e5f6",
                     api_key="osk_" + "a" * 40, env=env)
    _capture(repo)
    rt = transport.RecordingPlatformTransport([OLD_PLATFORM, SendResult(transport.KIND_CREATED, 201)])
    report = client.flush(repo, env=env, transport=rt)
    assert (report.created, report.rejected) == (1, 0)
    first, second = rt.envelopes
    assert "subagents" in first["receipt"] and "subagents" not in second["receipt"]
    assert {r["state"] for r in outbox.load_outbox(repo).values()} == {"synced"}


def test_any_other_rejection_is_not_resent_without_subagents():
    other = SendResult(transport.KIND_REJECTED, 400, "invalid_payload", [{"path": "receipt.files", "message": "x"}])
    assert rejected_for_subagents(OLD_PLATFORM) is True and rejected_for_subagents(other) is False


def test_a_nested_subagent_names_the_subagent_that_launched_it(tmp_path):
    main = tmp_path / f"{SID}.jsonl"
    _jsonl(main, [])
    sub = main.with_suffix("") / "subagents"
    _jsonl(sub / "agent-outer.jsonl", [_assistant("o1", "claude-sonnet-5-5", "2099-01-01T00:00:01Z", out=1, tool="toolu_inner")])
    _jsonl(sub / "agent-inner.jsonl", [_assistant("i1", "claude-sonnet-5-5", "2099-01-01T00:00:02Z", out=1)])
    (sub / "agent-inner.meta.json").write_text(json.dumps({"toolUseId": "toolu_inner", "spawnDepth": 2}))
    (sub / "agent-orphan.meta.json").write_text(json.dumps({"toolUseId": "toolu_gone", "spawnDepth": 2}))
    edges = [{"agent_id": a, "stopped": True} for a in ("outer", "inner", "orphan")]
    runs = {r["agent_id"]: r for r in cs.subagent_runs(edges, main)}
    assert runs["inner"]["parent_agent"] == "outer"
    assert runs["outer"]["parent_agent"] is None  # no meta: depth unknown, never assumed to be the lead
    assert runs["orphan"]["parent_agent"] is None and runs["orphan"]["result_reason"] == "outcome_not_recorded"


@pytest.mark.parametrize(("subagents", "session", "status"), [
    ([0.02, 0.03], 0.10, "within_session_total"),
    ([0.08, 0.05], 0.10, "exceeds_session_total"),
    ([0.02, None], None, "unknown"),
    ([None], 0.10, "unknown"),
])
def test_subagent_costs_are_checked_against_the_receipt_cost(subagents, session, status):
    block = cs.cost_reconciliation([{"cost_usd": c} for c in subagents], session)
    assert block["status"] == status
    if status == "exceeds_session_total":
        rows = task_edges.task_edge_lines({"children": [], "subagent_cost": block})
        assert rows == [("Subagent cost", "$0.1300 est. is more than this Receipt's $0.1000: the figures do not reconcile")]


def test_usage_owned_by_another_checkout_is_not_counted_again(tmp_path):
    main = tmp_path / f"{SID}.jsonl"
    _jsonl(main, [])
    _jsonl(main.with_suffix("") / "subagents" / "agent-x.jsonl",
           [_assistant("x1", "claude-sonnet-5-5", "2099-01-01T00:00:01Z", out=5)])
    [run] = cs.subagent_runs([{"agent_id": "x", "stopped": True}], main,
                             usage_elsewhere=ch.USAGE_ELSEWHERE_REASON)
    assert run["tokens"] is None and run["cost_usd"] is None
    assert run["tokens_not_recorded_reason"] == ch.USAGE_ELSEWHERE_REASON
    assert run["started_at"] == "2099-01-01T00:00:01Z"  # times and tool calls are still its own


def test_an_old_platform_naming_several_new_keys_gets_the_receipt_without_each(repo, tmp_path):
    from openshard.sync.envelope import rejected_optional_key

    both = SendResult(transport.KIND_REJECTED, 400, "invalid_payload",
                      [{"path": "receipt", "message": 'Unrecognized keys: "subagents", "ran_on"'}])
    receipt = {"subagents": {}, "ran_on": {}, "agent_app": {}}
    assert rejected_optional_key(both, receipt) == "subagents"
    receipt.pop("subagents")
    assert rejected_optional_key(both, receipt) == "ran_on"
    receipt.pop("ran_on")
    assert rejected_optional_key(both, receipt) is None  # agent_app was not named: kept
