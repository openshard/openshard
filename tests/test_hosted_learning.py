"""Hosted hook boundaries against a fake HTTP transport; no providers are called."""
from __future__ import annotations

import copy
import io
import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from openshard.history.receipt_evidence import learning_block
from openshard.learning import external, hosted
from openshard.learning.external_install import PATHS, configure
from openshard.sync.config import PlatformLink

REPOSITORY = "github.com/acme/shop"
RECEIPTS = ["rcpt_" + "a" * 32, "rcpt_" + "b" * 32]
LINK = PlatformLink("https://platform.example", "11111111-1111-1111-1111-111111111111", "osk_fake_test_key", None, "env")


def packet():
    return {
        "schema_version": "openshard.learning-context.v1", "repository": REPOSITORY,
        "source": "hosted_receipts", "status": "used",
        "generated_at": datetime.now(UTC).isoformat(), "snapshot_id": "hl_" + "a" * 24,
        "scope": {"loaded_receipts": 2, "matched_receipts": 2, "truncated": False, "freshness_days": 90},
        "signals_considered": 1,
        "signals": [{"signal_id": "ls_" + "a" * 12, "kind": "verified_check_history", "check": "pytest tests/test_layout.py", "passed": 1, "failed": 1, "samples": 2, "receipt_ids": RECEIPTS}],
    }


@pytest.fixture
def linked(tmp_path, monkeypatch):
    from openshard.history import repo_identity
    from openshard.sync import config

    monkeypatch.setattr(repo_identity, "capture_repo_identity", lambda root: REPOSITORY)
    monkeypatch.setattr(config, "resolve_link", lambda env: LINK)
    return tmp_path


def test_fetch_is_read_only_and_never_sends_prompt_or_follows_redirect():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=packet())

    result = hosted._fetch(LINK, REPOSITORY, ["responsive", "layout"], transport=httpx.MockTransport(respond))
    assert result["signals"][0]["receipt_ids"] == RECEIPTS
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == LINK.endpoint + "/v1/orgs/" + LINK.organisation_id + "/learning/context"
    assert json.loads(seen[0].content) == {"repository": REPOSITORY, "task_terms": ["responsive", "layout"]}
    assert seen[0].headers["authorization"] == "Bearer " + LINK.api_key


def test_delivery_and_receipt_keep_supporting_ids_without_model_consumption(linked, monkeypatch):
    good = hosted._checked(packet(), REPOSITORY)
    monkeypatch.setattr(hosted, "_fetch", lambda *a, **kw: good)
    response, record = hosted.retrieve(linked, "Fix responsive layout", env={})
    context = response["hookSpecificOutput"]["additionalContext"]
    assert RECEIPTS[0] in context and "1 passed, 1 failed" in context
    assert record["used"] is True and record["context_supplied"] is False
    assert record["context_delivery"] == "hook_response_emitted"
    assert record["routing"]["influenced"] is False
    assert record["verification"]["influenced"] is False
    assert record["snapshot"]["source"] == "hosted_context"
    output = io.StringIO()
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "cloud-1", "cwd": str(linked), "prompt": "Fix responsive layout"}
    external.emit_hook(io.StringIO(json.dumps(prompt)), output, "codex", env={}, hosted=True)
    saved = external.delivery_path(linked, "codex", "cloud-1").read_text()
    assert "Fix responsive layout" not in saved and LINK.api_key not in saved
    learned = external.captured_learning(linked, "codex", "cloud-1", "2026-01-01T00:00:00Z")
    projected = learning_block({"learning": learned})
    assert projected["supporting_receipt_ids"] == RECEIPTS
    assert projected["snapshot"]["source"] == "hosted_context"
    assert projected["context_supplied"] is False


def test_missing_read_link_and_disabled_sync_never_fetch(linked, monkeypatch):
    from openshard.sync import config

    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected fetch")

    monkeypatch.setattr(hosted, "_fetch", unexpected)
    monkeypatch.setattr(config, "resolve_link", lambda env: None)
    assert hosted.retrieve(linked, "Fix responsive layout", env={})[0] == {}
    monkeypatch.setattr(config, "resolve_link", lambda env: LINK)
    assert hosted.retrieve(linked, "Fix responsive layout", env={"OPENSHARD_PLATFORM_SYNC": "off"})[0] == {}


def test_missing_canonical_repo_never_fetch(linked, monkeypatch):
    from openshard.history import repo_identity

    monkeypatch.setattr(repo_identity, "capture_repo_identity", lambda root: None)
    called = []
    monkeypatch.setattr(hosted, "_fetch", lambda *a, **kw: called.append(True))
    assert hosted.retrieve(linked, "Fix responsive layout", env={})[0] == {}
    assert called == []


def test_only_privacy_filtered_terms_leave_checkout(linked, monkeypatch):
    seen = []
    monkeypatch.setattr(hosted, "_fetch", lambda link, repo, terms, **kw: seen.append(terms))
    task = "Fix responsive layout sk-" + "a" * 48 + " /home/private/file.py"
    assert hosted.retrieve(linked, task, env={})[0] == {}
    assert len(seen) == 1
    assert all("sk-" not in term and "private" not in term and "home" not in term for term in seen[0])
    assert len(seen[0]) <= 16


@pytest.mark.parametrize("status", ["no_history", "no_relevant_signals"])
def test_empty_history_emits_no_context(linked, monkeypatch, status):
    data = packet()
    data.update(status=status, signals=[], signals_considered=0)
    if status == "no_history":
        data["scope"].update(loaded_receipts=0, matched_receipts=0)
    monkeypatch.setattr(hosted, "_fetch", lambda *a, **kw: hosted._checked(data, REPOSITORY))
    response, record = hosted.retrieve(linked, "Fix responsive layout", env={})
    assert response == {} and record["used"] is False
    assert record["context_delivery"] == "not_emitted"
    assert record["snapshot"]["status"] == "available"


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(repository="github.com/other/shop"),
    lambda p: p.update(schema_version="unknown"),
    lambda p: p.update(source="agent_reported"),
    lambda p: p.update(status="invented"),
    lambda p: p.update(generated_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat()),
    lambda p: p.update(generated_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat()),
    lambda p: p.update(snapshot_id="../../local"),
    lambda p: p["scope"].update(loaded_receipts=201),
    lambda p: p["scope"].update(matched_receipts=3),
    lambda p: p["scope"].update(truncated="yes"),
    lambda p: p["scope"].update(freshness_days=91),
    lambda p: p.update(signals=copy.deepcopy(p["signals"]) * 6),
    lambda p: p["signals"][0].update(samples=1),
    lambda p: p["signals"][0].update(passed=True),
    lambda p: p["signals"][0].update(check="/home/private/run.py"),
    lambda p: p["signals"][0].update(check="sk-" + "a" * 48),
    lambda p: p["signals"][0].update(receipt_ids=[RECEIPTS[0], RECEIPTS[0]]),
    lambda p: p["signals"][0].update(receipt_ids=["https://outside.example", RECEIPTS[1]]),
    lambda p: p["signals"][0].update(signal_id="unsafe"),
    lambda p: p.update(signals_considered=0),
])
def test_malformed_context_fails_open(linked, mutation):
    data = packet()
    mutation(data)
    response, record = hosted.retrieve(linked, "Fix responsive layout", env={}, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
    assert response == {}
    assert record["status"] == "unavailable"
    assert record["used"] is False and record["context_delivery"] == "not_emitted"


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://outside.example"}),
    httpx.Response(403, text="secret diagnostic"),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, content=b"x" * (hosted.MAX_RESPONSE + 1)),
])
def test_errors_redirects_and_oversized_responses_are_not_persisted(linked, response):
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return response
    output, record = hosted.retrieve(linked, "Fix responsive layout", env={}, transport=httpx.MockTransport(respond))
    assert output == {} and len(calls) == 1
    assert "secret diagnostic" not in json.dumps(record)


def test_deadline_returns_before_delayed_fetch_and_does_not_emit_later(linked, monkeypatch):
    release = threading.Event()
    completed = threading.Event()

    def slow(*args, **kwargs):
        release.wait(1)
        completed.set()
        return hosted._checked(packet(), REPOSITORY)

    monkeypatch.setattr(hosted, "_fetch", slow)
    monkeypatch.setattr(hosted, "BUDGET_SECONDS", 0.01)
    try:
        response, record = hosted.retrieve(linked, "Fix responsive layout", env={})
        assert response == {} and record["status"] == "timeout"
        assert record["context_delivery"] == "not_emitted"
    finally:
        release.set()
        assert completed.wait(1)
    assert record["used"] is False


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_installer_changes_only_own_hook_and_uninstalls_both_modes(linked, agent):
    path = linked / PATHS[agent]
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {"permissions": {"allow": ["Read"]}, "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}}
    path.write_text(json.dumps(original))
    configure(linked, agent)
    configure(linked, agent, hosted=True)
    content = path.read_text()
    hooks = [h for g in json.loads(content)["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert [h["command"] for h in hooks] == ["other-hook", f"openshard learn hook {agent} --hosted"]
    assert hooks[-1]["timeout"] == 3
    assert configure(linked, agent, hosted=True)["change"] == "unchanged"
    assert path.read_text() == content
    configure(linked, agent)
    configure(linked, agent, remove=True)
    assert json.loads(path.read_text()) == original


def test_local_mode_does_not_call_hosted_retrieval(linked, monkeypatch):
    calls = []
    monkeypatch.setattr(hosted, "retrieve", lambda *a, **kw: calls.append(True))
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "cloud-2", "cwd": str(linked), "prompt": "Fix responsive layout"}
    external.prepare_hook(prompt, "claude", env={})
    assert calls == []


def test_old_learning_projection_does_not_gain_hosted_fields():
    raw = {"status": "used", "used": True, "snapshot": {"status": "available"}, "supporting_receipt_ids": RECEIPTS}
    projected = learning_block({"learning": raw})
    assert "supporting_receipt_ids" not in projected
    assert "source" not in projected["snapshot"]
