"""Launch context survives capture, replay and conflicting later declarations."""
import json

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters.claude_hooks import (
    HookPayload,
    ReducedHookPayload,
    handle_hook,
    reduce_hook_payload,
)
from openshard.history.correlation import CONTEXT_ENV
from openshard.sync.envelope import receipt_payload
from tests.capture_fixtures import _auth_headers, _lines, _wait_for
from tests.test_task_context_capture import SID1, TASK_A, _claude_docs, _codex_docs
from tests.test_workflow_correlation import CONTEXT


@pytest.mark.parametrize("agent", ["claude_code", "codex", "cursor"])
def test_capture_freezes_context_and_ignores_payload_spoofing(repo, agent):
    if agent == "cursor":
        from tests.test_cursor_capture import _doc
        docs = [_doc("sessionStart", repo, SID1, session_id=SID1),
                _doc("beforeSubmitPrompt", repo, SID1, prompt="Fix a thing"),
                _doc("stop", repo, SID1, status="completed"),
                _doc("sessionEnd", repo, SID1, session_id=SID1)]
    else:
        docs = (_claude_docs if agent == "claude_code" else _codex_docs)(repo, SID1)
    for i, doc in enumerate(docs):
        result = handle_hook({**doc, "correlation": {"source": "spoofed"}}, env={}, agent=agent,
                             task_id=TASK_A, correlation=CONTEXT if i == 0 else {"source": "changed"})
        assert result.action != "error", result.detail
    entry = _lines(repo)[0]
    assert entry["correlation"] == {"evidence": "declared", **CONTEXT}
    assert entry["capture"]["correlation_conflicts"] > 0
    assert receipt_payload(entry, 1)["correlation"] == entry["correlation"]


def test_reduced_queue_roundtrip(repo):
    payload = HookPayload(event="SessionStart", session_id=SID1, cwd=str(repo), correlation=CONTEXT)
    reduced = reduce_hook_payload(payload, repo)
    assert reduced is not None
    decoded = ReducedHookPayload.from_dict(json.loads(json.dumps(reduced.to_dict())))
    assert decoded is not None
    assert decoded.correlation == {"evidence": "declared", **CONTEXT}


def test_raw_payload_cannot_bind_context(repo):
    for doc in _claude_docs(repo, SID1):
        handle_hook({**doc, "correlation": CONTEXT}, env={})
    assert "correlation" not in _lines(repo)[0]


def test_inline_client_uses_launcher_context(repo):
    env = {"OPENSHARD_CAPTURE_DISABLED": "1", "CLAUDE_PROJECT_DIR": str(repo),
           CONTEXT_ENV: json.dumps(CONTEXT)}
    for doc in _claude_docs(repo, SID1):
        client._inline_hook(json.dumps(doc).encode(), env, None)
    assert _lines(repo)[0]["correlation"] == {"evidence": "declared", **CONTEXT}


def test_http_header_transport_and_persisted_replay(service, repo):
    headers = {**_auth_headers(str(repo)), client.CORRELATION_HEADER: json.dumps(CONTEXT)}
    for doc in _claude_docs(repo, SID1):
        response = client._request("POST", service.port, client.HOOK_PATH,
                                   json.dumps(doc).encode(), headers)
        assert response == (200, b"{}")
    assert _wait_for(lambda: bool(_lines(repo)) and _lines(repo)[0]["capture"]["session_end_observed"])
    assert service.server.recorder.wait_idle(60)
    assert _lines(repo)[0]["correlation"] == {"evidence": "declared", **CONTEXT}


def test_invalid_header_keeps_receipt_uncorrelated(service, repo):
    headers = {**_auth_headers(str(repo)), client.CORRELATION_HEADER: "{invalid"}
    for doc in _claude_docs(repo, SID1):
        assert client._request("POST", service.port, client.HOOK_PATH,
                               json.dumps(doc).encode(), headers) == (200, b"{}")
    assert _wait_for(lambda: bool(_lines(repo)) and _lines(repo)[0]["capture"]["session_end_observed"])
    assert "correlation" not in _lines(repo)[0]
