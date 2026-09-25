"""Task-scoped external-agent correlation v1: declared launch context (``OPENSHARD_TASK_ID``).

Product contract under test:

* Several external-agent sessions -- of one agent or of different agents --
  may share one *explicitly declared* ``task_id`` and still remain separate
  receipts, one per agent session. Nothing is merged, inferred or enforced.
* The declaration reaches capture only through the launch environment, and
  from there on a dedicated authenticated header (never the agent's own
  payload): client -> ``X-OpenShard-Task-Id`` -> ``HookPayload`` ->
  ``ReducedHookPayload`` -> session buffer -> receipt.
* The relationship is machine-readable as DECLARED launch context
  (``capture.task_context.evidence == "declared"``); hook/tool/session facts
  keep the evidence levels they always had.
* The first valid declaration binds immutably; a conflicting or late one is
  counted and never reassigns a receipt. Missing/malformed ids keep the
  legacy behaviour exactly.
"""

from __future__ import annotations

import copy
import io
import json
import socket
import uuid
from pathlib import Path

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks import (
    HookPayload,
    ReducedHookPayload,
    apply_reduced_hook,
    extract_agent_payload,
    handle_hook,
    reduce_hook_payload,
)
from openshard.adapters.claude_hooks_install import (
    HTTP_EVENTS,
    SETTINGS_RELPATH,
    build_hook_config,
    install_claude_hooks,
    merge_openshard_hooks,
)
from openshard.history.query import get_receipt, list_receipts_by_task
from openshard.history.shard import derive_shard_identity
from openshard.history.task_identity import (
    EVIDENCE_DECLARED,
    TASK_ID_ENV,
    launch_task_id,
    new_task_id,
)
from openshard.sync.envelope import build_envelope, eligibility, receipt_payload
from tests.capture_fixtures import (
    _auth_headers,
    _lines,
    _make_repo,
    _session_dir,
    _status_payload,
    _wait_for,
)

TASK_A = "task_018f4d2a-1c3e-7000-8b1a-0242ac120002"
TASK_B = "task_018f4d2a-1c3e-7000-9c2b-0242ac120003"
SID1 = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
SID3 = "33333333-3333-4333-8333-333333333333"
SID4 = "44444444-4444-4444-8444-444444444444"

MALFORMED_IDS = [
    "",  # what Claude Code interpolates for an unset $OPENSHARD_TASK_ID
    "$OPENSHARD_TASK_ID",  # a header that was never interpolated
    "task_not-a-task-id",
    TASK_A.upper(),
    f" {TASK_A}",
    f"{TASK_A} ",
    f"{TASK_A}\n",
    TASK_A[len("task_"):],  # no prefix
    f"task_{uuid.uuid4()}",  # UUIDv4, not v7
]

# What can still arrive as a *header value*: the HTTP layer itself trims a leading
# space and cannot carry a newline, so those padded shapes only exist in the
# environment (covered above) -- what remains is what a header can really hold.
HTTP_MALFORMED_IDS = [
    "",
    "$OPENSHARD_TASK_ID",
    "task_not-a-task-id",
    TASK_A.upper(),
    TASK_A[len("task_"):],
    f"task_{uuid.uuid4()}",
]

CLAUDE_EXECUTOR = "claude_code_hooks"
CODEX_EXECUTOR = "codex_hooks"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _claude_docs(repo: Path, sid: str, prompt: str = "Fix the login button alignment") -> list[dict]:
    base = {"session_id": sid, "cwd": str(repo), "permission_mode": "default"}
    return [
        {**base, "hook_event_name": "SessionStart", "source": "startup"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt},
        {**base, "hook_event_name": "Stop"},
        {**base, "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"},
    ]


def _codex_docs(repo: Path, sid: str, prompt: str = "Add a terraform verification step") -> list[dict]:
    base = {"session_id": sid, "cwd": str(repo), "model": "gpt-5-codex"}
    return [
        {**base, "hook_event_name": "SessionStart", "source": "startup"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt},
        {**base, "hook_event_name": "Stop"},
        {**base, "hook_event_name": "SessionEnd"},
    ]


def _claude_env(repo: Path) -> dict:
    return {"CLAUDE_PROJECT_DIR": str(repo)}


def _drive_claude(repo: Path, docs: list[dict], task_id: str | None = None) -> None:
    for doc in docs:
        handle_hook(doc, env=_claude_env(repo), task_id=task_id)


def _drive_codex(repo: Path, docs: list[dict], task_id: str | None = None) -> None:
    for doc in docs:
        handle_hook(doc, env={}, agent="codex", task_id=task_id)


def _entry(repo: Path, sid: str, executor: str = CLAUDE_EXECUTOR) -> dict:
    matches = [
        e for e in _lines(repo)
        if e.get("executor") == executor and (e.get("capture") or {}).get("session_id") == sid
    ]
    assert len(matches) == 1, f"expected exactly one {executor} record for {sid}, found {len(matches)}"
    return matches[0]


def _read_buffer(repo: Path, sid: str) -> dict:
    buf = ch._read_buffer(ch.buffer_path(repo, sid))
    assert buf is not None
    return buf


def _raw_post(port: int, path: str, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    result = client._request("POST", port, path, body, headers)
    assert result is not None, "service did not answer"
    return result


def _post_declared(
    service, repo: Path, doc: dict, task_id: str | None, *, path: str = client.HOOK_PATH,
) -> tuple[int, bytes]:
    headers = dict(_auth_headers(str(repo)))
    if task_id is not None:
        headers[client.TASK_ID_HEADER] = task_id
    return _raw_post(service.port, path, json.dumps(doc).encode("utf-8"), headers)


def _drive_http(
    service, repo: Path, docs: list[dict], task_id: str | None, *, path: str = client.HOOK_PATH,
) -> None:
    for doc in docs:
        status, body = _post_declared(service, repo, doc, task_id, path=path)
        assert (status, body) == (200, b"{}")


def _wait_ended(service, repo: Path, count: int = 1) -> list[dict]:
    def _done() -> bool:
        lines = _lines(repo)
        return len(lines) == count and all(e["capture"]["session_end_observed"] for e in lines)

    assert _wait_for(_done, timeout=120)
    assert service.server.recorder.wait_idle(60)
    return _lines(repo)


class _RecordedRequests:
    """A stand-in for ``client._request`` that records what would have been sent."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, port, path, body=None, headers=None, *, timeout=5.0):
        self.calls.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})
        return 200, b"{}"


@pytest.fixture
def sent(monkeypatch) -> _RecordedRequests:
    recorded = _RecordedRequests()
    monkeypatch.setattr(client, "_request", recorded)
    return recorded


def _client_env(tmp_path: Path, **extra: str) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), **extra}


# ---------------------------------------------------------------------------
# launch environment validation
# ---------------------------------------------------------------------------


class TestLaunchTaskId:
    def test_env_var_name_and_header_are_the_documented_ones(self):
        assert TASK_ID_ENV == "OPENSHARD_TASK_ID"
        assert client.TASK_ID_HEADER == "X-OpenShard-Task-Id"

    def test_valid_declaration_is_returned_exactly_as_given(self):
        assert launch_task_id({TASK_ID_ENV: TASK_A}) == TASK_A
        minted = new_task_id()
        assert launch_task_id({TASK_ID_ENV: minted}) == minted

    def test_unset_is_no_declaration(self):
        assert launch_task_id({}) is None

    @pytest.mark.parametrize("value", MALFORMED_IDS)
    def test_malformed_is_no_declaration_and_never_repaired(self, value):
        assert launch_task_id({TASK_ID_ENV: value}) is None

    def test_defaults_to_the_process_environment(self, monkeypatch):
        monkeypatch.setenv(TASK_ID_ENV, TASK_A)
        assert launch_task_id() == TASK_A
        monkeypatch.delenv(TASK_ID_ENV)
        assert launch_task_id() is None


# ---------------------------------------------------------------------------
# command-hook client forwarding
# ---------------------------------------------------------------------------


class TestCommandClientForwarding:
    def test_declaration_is_forwarded_on_its_own_header_never_in_the_body(self, sent, tmp_path):
        raw = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": SID1, "prompt": "p"}).encode()
        env = _client_env(tmp_path, **{TASK_ID_ENV: TASK_A})
        assert client._run_hook_raw(raw, env, event_override=None, agent="claude_code", spawn=False) == "forwarded"
        (call,) = sent.calls
        assert call["headers"][client.TASK_ID_HEADER] == TASK_A
        assert call["body"] == raw  # the agent's payload is forwarded untouched
        assert TASK_A.encode() not in call["body"]
        # ...alongside (not instead of) the capture credential.
        assert "X-OpenShard-Capture-Token" in call["headers"]

    @pytest.mark.parametrize("agent", sorted(client.AGENT_HOOK_PATHS))
    def test_every_agent_receiver_gets_the_header(self, sent, tmp_path, agent):
        raw = b'{"hook_event_name": "Stop", "session_id": "s"}'
        env = _client_env(tmp_path, **{TASK_ID_ENV: TASK_A})
        assert client._run_hook_raw(raw, env, event_override=None, agent=agent, spawn=False) == "forwarded"
        (call,) = sent.calls
        assert call["path"] == client.AGENT_HOOK_PATHS[agent]
        assert call["headers"][client.TASK_ID_HEADER] == TASK_A

    def test_cursor_wrapper_forwards_it_and_still_answers_continue(self, sent, tmp_path):
        raw = b'{"hook_event_name": "beforeSubmitPrompt", "session_id": "s"}'
        env = _client_env(tmp_path, **{TASK_ID_ENV: TASK_A})
        label, reply = client.run_cursor_hook(io.BytesIO(raw), env=env, spawn=False)
        assert label == "forwarded" and reply == client.CURSOR_ALLOW_RESPONSE
        assert sent.calls[0]["headers"][client.TASK_ID_HEADER] == TASK_A

    @pytest.mark.parametrize("value", [None, *MALFORMED_IDS])
    def test_unset_or_malformed_env_sends_no_header(self, sent, tmp_path, value):
        extra = {} if value is None else {TASK_ID_ENV: value}
        raw = b'{"hook_event_name": "Stop", "session_id": "s"}'
        env = _client_env(tmp_path, **extra)
        assert client._run_hook_raw(raw, env, event_override=None, agent="claude_code", spawn=False) == "forwarded"
        assert client.TASK_ID_HEADER not in sent.calls[0]["headers"]

    def test_payload_fields_are_never_promoted_to_a_declaration(self, sent, tmp_path):
        raw = json.dumps({
            "hook_event_name": "Stop", "session_id": "s", "task_id": TASK_A,
            "env": {TASK_ID_ENV: TASK_A}, TASK_ID_ENV: TASK_A,
        }).encode()
        env = _client_env(tmp_path)  # nothing declared at launch
        client._run_hook_raw(raw, env, event_override=None, agent="claude_code", spawn=False)
        assert client.TASK_ID_HEADER not in sent.calls[0]["headers"]

    def test_status_line_ping_carries_no_declaration(self, sent, tmp_path):
        env = _client_env(tmp_path, **{TASK_ID_ENV: TASK_A})
        assert client.post_status(1, b"{}", env=env)
        assert client.TASK_ID_HEADER not in sent.calls[0]["headers"]

    def test_in_process_fallback_binds_from_the_environment(self, repo, tmp_path):
        env = _client_env(
            tmp_path, OPENSHARD_CAPTURE_DISABLE="1", CLAUDE_PROJECT_DIR=str(repo), **{TASK_ID_ENV: TASK_A},
        )
        for doc in _claude_docs(repo, SID1):
            label = client.run_hook_via_service(io.BytesIO(json.dumps(doc).encode()), env=env)
            assert label != "forwarded"
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A
        assert entry["capture"]["task_context"]["evidence"] == EVIDENCE_DECLARED

    def test_in_process_fallback_ignores_a_malformed_environment_value(self, repo, tmp_path):
        env = _client_env(
            tmp_path, OPENSHARD_CAPTURE_DISABLE="1", CLAUDE_PROJECT_DIR=str(repo), **{TASK_ID_ENV: "task_bogus"},
        )
        for doc in _claude_docs(repo, SID1):
            client.run_hook_via_service(io.BytesIO(json.dumps(doc).encode()), env=env)
        entry = _entry(repo, SID1)
        assert "task_id" not in entry and "task_context" not in entry["capture"]

    @pytest.mark.parametrize("entrypoint", ["handle", "stream"])
    @pytest.mark.parametrize(
        ("declared", "expected"),
        [(TASK_A, TASK_A), ("task_bogus", None), (None, None)],
    )
    def test_legacy_synchronous_entrypoints_read_launch_context(self, repo, entrypoint, declared, expected):
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        if declared is not None:
            env[TASK_ID_ENV] = declared
        for doc in _claude_docs(repo, SID1):
            if entrypoint == "handle":
                ch.handle_claude_hook(doc, env=env)
            else:
                ch.run_hook_from_stream(io.BytesIO(json.dumps(doc).encode()), env=env)
        entry = _entry(repo, SID1)
        assert entry.get("task_id") == expected
        assert ("task_context" in entry["capture"]) is (expected is not None)


# ---------------------------------------------------------------------------
# Claude HTTP hook installation
# ---------------------------------------------------------------------------


class TestClaudeHttpHookInstall:
    def test_every_http_hook_interpolates_the_task_id_header(self):
        config = build_hook_config(capability="r2." + "0" * 64)
        assert HTTP_EVENTS
        for event in HTTP_EVENTS:
            hook = config[event][0]["hooks"][0]
            assert hook["headers"][client.TASK_ID_HEADER] == "$OPENSHARD_TASK_ID"
            assert hook["headers"]["X-OpenShard-Project-Dir"] == "$CLAUDE_PROJECT_DIR"
            assert hook["headers"]["X-OpenShard-Capture-Token"].startswith("r2.")
            # Only the two variables OpenShard needs may be interpolated.
            assert hook["allowedEnvVars"] == ["CLAUDE_PROJECT_DIR", "OPENSHARD_TASK_ID"]

    def test_session_start_command_hook_is_unchanged(self):
        hook = build_hook_config()["SessionStart"][0]["hooks"][0]
        assert hook["type"] == "command" and "headers" not in hook and "allowedEnvVars" not in hook

    def test_installer_writes_the_header_into_settings_local_json(self, repo):
        result = install_claude_hooks(repo_root=repo)
        assert result.status in ("installed", "updated"), result
        settings = json.loads((repo / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        for event in HTTP_EVENTS:
            hook = settings["hooks"][event][0]["hooks"][0]
            assert hook["headers"][client.TASK_ID_HEADER] == "$OPENSHARD_TASK_ID"
            assert "OPENSHARD_TASK_ID" in hook["allowedEnvVars"]
        assert install_claude_hooks(repo_root=repo).status == "already_installed"

    def test_existing_installs_are_upgraded_in_place_once(self):
        legacy = copy.deepcopy(build_hook_config())
        for event in HTTP_EVENTS:
            hook = legacy[event][0]["hooks"][0]
            del hook["headers"][client.TASK_ID_HEADER]
            hook["allowedEnvVars"] = ["CLAUDE_PROJECT_DIR"]
        merged, changes = merge_openshard_hooks({"hooks": legacy})
        assert {changes[e] for e in HTTP_EVENTS} == {"updated"}
        assert changes["SessionStart"] == "unchanged"
        for event in HTTP_EVENTS:
            assert client.TASK_ID_HEADER in merged["hooks"][event][0]["hooks"][0]["headers"]
        _, again = merge_openshard_hooks(merged)
        assert set(again.values()) == {"unchanged"}


# ---------------------------------------------------------------------------
# direct HTTP -> service -> worker flow
# ---------------------------------------------------------------------------


class TestServiceFlow:
    def test_header_declaration_lands_on_the_receipt(self, service, repo):
        _drive_http(service, repo, _claude_docs(repo, SID1), TASK_A)
        (entry,) = _wait_ended(service, repo)
        assert entry["task_id"] == TASK_A
        ctx = entry["capture"]["task_context"]
        assert set(ctx) == {"source", "variable", "evidence", "bound_at", "bound_by"}
        assert ctx["source"] == "launch_environment" and ctx["variable"] == "OPENSHARD_TASK_ID"
        assert ctx["evidence"] == "declared" and ctx["bound_by"] == "SessionStart"
        assert "task_context_conflicts" not in entry["capture"]
        assert get_receipt(entry["shard_id"], repo_path=repo).task_id == TASK_A

    def test_client_post_hook_task_id_argument_uses_the_same_header(self, service, repo):
        for doc in _claude_docs(repo, SID1):
            assert client.post_hook(
                service.port, json.dumps(doc).encode(), project_dir=str(repo), task_id=TASK_A,
            )
        (entry,) = _wait_ended(service, repo)
        assert entry["task_id"] == TASK_A

    def test_no_header_keeps_the_legacy_record_shape(self, service, repo):
        _drive_http(service, repo, _claude_docs(repo, SID1), None)
        (entry,) = _wait_ended(service, repo)
        assert "task_id" not in entry
        assert "task_context" not in entry["capture"] and "task_context_conflicts" not in entry["capture"]

    def test_malformed_headers_never_bind_never_reject_and_still_record(self, service, repo):
        sids = [f"aaaaaaaa-0000-4000-8000-{i:012d}" for i in range(len(HTTP_MALFORMED_IDS))]
        for sid, value in zip(sids, HTTP_MALFORMED_IDS, strict=True):
            _drive_http(service, repo, _claude_docs(repo, sid), value)
        entries = _wait_ended(service, repo, count=len(sids))
        assert {e["capture"]["session_id"] for e in entries} == set(sids)
        for entry in entries:
            assert "task_id" not in entry and "task_context" not in entry["capture"]
            assert "task_context_conflicts" not in entry["capture"]

    def test_repeated_header_is_ambiguous_and_never_binds(self, service, repo):
        headers = dict(_auth_headers(str(repo)))
        lines = [f"{k}: {v}" for k, v in headers.items()]
        lines += [f"{client.TASK_ID_HEADER}: {TASK_A}", f"{client.TASK_ID_HEADER}: {TASK_A}"]
        for doc in _claude_docs(repo, SID1):
            body = json.dumps(doc).encode()
            head = "\r\n".join(
                ["POST /hooks/claude HTTP/1.0", "Host: 127.0.0.1", *lines,
                 "Content-Type: application/json", f"Content-Length: {len(body)}"]
            ) + "\r\n\r\n"
            with socket.create_connection(("127.0.0.1", service.port), timeout=10) as sock:
                sock.sendall(head.encode("ascii") + body)
                chunks = []
                while chunk := sock.recv(65536):
                    chunks.append(chunk)
            assert b" 200 " in b"".join(chunks).split(b"\r\n", 1)[0]
        (entry,) = _wait_ended(service, repo)
        assert "task_id" not in entry

    def test_unauthenticated_request_with_a_valid_header_records_nothing(self, service, repo):
        doc = _claude_docs(repo, SID1)[1]
        status, _ = _raw_post(
            service.port, client.HOOK_PATH, json.dumps(doc).encode(),
            {client.PROJECT_DIR_HEADER: str(repo), client.TASK_ID_HEADER: TASK_A},
        )
        assert status == 401
        assert not list(_session_dir(repo).glob("*.queue*.jsonl")) and not _lines(repo)

    def test_body_fields_are_never_task_context(self, service, repo):
        docs = [
            {**d, "task_id": TASK_A, TASK_ID_ENV: TASK_A, "metadata": {"task_id": TASK_A}}
            for d in _claude_docs(repo, SID1)
        ]
        _drive_http(service, repo, docs, None)
        (entry,) = _wait_ended(service, repo)
        assert "task_id" not in entry and "task_context" not in entry["capture"]
        assert TASK_A not in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")

    def test_conflicting_header_keeps_the_first_and_still_answers_200(self, service, repo):
        docs = _claude_docs(repo, SID1)
        _drive_http(service, repo, docs[:2], TASK_A)
        snapshot: list[dict] = []

        def _created() -> bool:
            snapshot[:] = _lines(repo)[:1]
            return bool(snapshot)

        assert _wait_for(_created, timeout=60)
        assert service.server.recorder.wait_idle(60)
        first = snapshot[0]
        _drive_http(service, repo, docs[2:], TASK_B)  # every reply is still a plain 200 {}
        (entry,) = _wait_ended(service, repo)
        assert entry["task_id"] == TASK_A
        assert entry["capture"]["task_context"] == first["capture"]["task_context"]
        assert entry["capture"]["task_context_conflicts"] == 2  # Stop + SessionEnd
        assert (entry["receipt_id"], entry["shard_id"], entry["run_id"]) == (
            first["receipt_id"], first["shard_id"], first["run_id"])

    def test_status_pings_are_not_a_declaration_channel(self, service, repo):
        _drive_http(service, repo, _claude_docs(repo, SID1), TASK_A)
        _wait_ended(service, repo)
        headers = {**_auth_headers(str(repo)), client.TASK_ID_HEADER: TASK_B}
        status, body = _raw_post(service.port, client.STATUS_PATH, _status_payload(repo, SID1), headers)
        assert (status, body) == (200, b"{}")
        assert service.server.recorder.wait_idle(60)
        (entry,) = _lines(repo)
        assert entry["task_id"] == TASK_A and "task_context_conflicts" not in entry["capture"]

    def test_queue_line_carries_only_the_validated_id_never_the_agent_payload_field(self, service, repo):
        recorder = service.server.recorder
        recorder.pause_processing()
        try:
            doc = {**_claude_docs(repo, SID1)[1], "task_id": TASK_B, "prompt": "Fix the login button"}
            assert _post_declared(service, repo, doc, TASK_A) == (200, b"{}")
            assert _post_declared(service, repo, {**doc, "prompt": "again"}, "task_junk") == (200, b"{}")
            assert _post_declared(service, repo, {**doc, "prompt": "third"}, None) == (200, b"{}")
            queue = _session_dir(repo) / f"{SID1}{svc.QUEUE_SUFFIX}"
            queued = [json.loads(ln) for ln in queue.read_text(encoding="utf-8").splitlines()]
        finally:
            recorder.resume_processing()
        assert [ln["data"].get("task_id") for ln in queued] == [TASK_A, None, None]
        assert all("task_id" not in ln["data"] for ln in queued[1:])  # legacy shape: key absent
        assert TASK_B not in json.dumps(queued)

    def test_sessions_of_two_agents_sharing_one_id_stay_separate_receipts(self, service, repo):
        _drive_http(service, repo, _claude_docs(repo, SID1), TASK_A)
        _drive_http(service, repo, _claude_docs(repo, SID2), TASK_A)
        _drive_http(service, repo, _codex_docs(repo, SID3), TASK_A, path=client.CODEX_HOOK_PATH)
        _drive_http(service, repo, _codex_docs(repo, SID4), None, path=client.CODEX_HOOK_PATH)
        entries = _wait_ended(service, repo, count=4)
        declared = [e for e in entries if e.get("task_id") == TASK_A]
        assert len(declared) == 3
        assert {e["executor"] for e in declared} == {CLAUDE_EXECUTOR, CODEX_EXECUTOR}
        assert len({e["receipt_id"] for e in entries}) == 4
        assert len({e["shard_id"] for e in entries}) == 4
        undeclared = next(e for e in entries if e["capture"]["session_id"] == SID4)
        assert "task_id" not in undeclared
        receipts = list_receipts_by_task(TASK_A, repo_path=repo)
        assert len(receipts) == 3
        assert {r.agent for r in receipts} == {"Claude Code (external)", "Codex (external)"}


# ---------------------------------------------------------------------------
# reduced payload / queue replay / idempotency
# ---------------------------------------------------------------------------


class TestReducedPayload:
    def test_round_trip_and_legacy_shape(self):
        declared = ReducedHookPayload(event="Stop", session_id=SID1, task_id=TASK_A)
        assert declared.to_dict()["task_id"] == TASK_A
        decoded = ReducedHookPayload.from_dict(declared.to_dict())
        assert decoded is not None and decoded.task_id == TASK_A
        legacy = ReducedHookPayload(event="Stop", session_id=SID1)
        assert "task_id" not in legacy.to_dict()
        decoded = ReducedHookPayload.from_dict(legacy.to_dict())
        assert decoded is not None and decoded.task_id is None

    @pytest.mark.parametrize("value", [*MALFORMED_IDS, 7, ["x"], {"task_id": TASK_A}])
    def test_malformed_queue_value_is_dropped_on_decode(self, value):
        line = {"event": "Stop", "session_id": SID1, "task_id": value}
        decoded = ReducedHookPayload.from_dict(line)
        assert decoded is not None and decoded.task_id is None

    def test_reduce_copies_only_a_valid_declaration(self, repo):
        valid = reduce_hook_payload(HookPayload(event="Stop", session_id=SID1, cwd=str(repo), task_id=TASK_A), repo)
        assert valid is not None and valid.task_id == TASK_A
        for value in ("task_bogus", "", None):
            bad = reduce_hook_payload(HookPayload(event="Stop", session_id=SID1, cwd=str(repo), task_id=value), repo)
            assert bad is not None and bad.task_id is None

    @pytest.mark.parametrize("agent", ["claude_code", "codex", "opencode"])
    def test_translators_never_read_task_context_from_the_agent_payload(self, repo, agent):
        forged = {"task_id": TASK_A, TASK_ID_ENV: TASK_A, "env": {TASK_ID_ENV: TASK_A}, "metadata": {"task_id": TASK_A}}
        if agent == "opencode":
            doc = {"agent": "opencode", "session_id": SID1, "directory": str(repo), "event": "session.created", **forged}
        else:
            doc = {**_claude_docs(repo, SID1)[1], **forged}
        payload = extract_agent_payload(doc, agent=agent)
        assert isinstance(payload, HookPayload) and payload.task_id is None
        reduced = reduce_hook_payload(payload, repo)
        assert reduced is not None and reduced.task_id is None

    def test_apply_ignores_a_malformed_declaration_that_bypassed_decode(self, repo):
        bogus = ReducedHookPayload(event="SessionStart", session_id=SID1, source="startup", task_id="task_bogus")
        apply_reduced_hook(bogus, repo, dedup_id="x-1")
        buf = _read_buffer(repo, SID1)
        assert buf["task_context"] is None and buf["task_context_conflicts"] == 0

    def test_replaying_an_applied_event_never_double_counts_a_conflict(self, repo):
        start = ReducedHookPayload(event="SessionStart", session_id=SID1, source="startup", task_id=TASK_A)
        apply_reduced_hook(start, repo, dedup_id="i-1")
        clash = ReducedHookPayload(event="UserPromptSubmit", session_id=SID1, task_excerpt="x", task_id=TASK_B)
        assert apply_reduced_hook(clash, repo, dedup_id="i-2").action != "error"
        replay = apply_reduced_hook(clash, repo, dedup_id="i-2")
        assert replay.detail == "duplicate event id"
        (entry,) = _lines(repo)
        assert entry["task_id"] == TASK_A and entry["capture"]["task_context_conflicts"] == 1

    def test_queue_replay_carries_the_declaration_and_survives_a_second_pass(self, repo):
        recorder = svc.CaptureRecorder(instance_id="t")  # worker never started: replay is driven by hand
        docs = _claude_docs(repo, SID1)
        for doc in docs:
            assert recorder.record_hook(doc, project_dir=str(repo), task_id=TASK_A)[0] == "queued"
        root = recorder.resolve_root(str(repo), None)
        assert root is not None
        live = ch.sessions_dir(root) / f"{svc.queue_key(SID1)}{svc.QUEUE_SUFFIX}"
        snapshot = live.read_text(encoding="utf-8")
        assert {json.loads(ln)["data"]["task_id"] for ln in snapshot.splitlines()} == {TASK_A}

        recorder._drain_session(root, svc.queue_key(SID1))
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A

        # A crash-style re-read of the very same lines changes nothing.
        live.write_text(snapshot, encoding="utf-8")
        recorder._drain_session(root, svc.queue_key(SID1))
        (again,) = _lines(repo)
        assert again["task_id"] == TASK_A and again["receipt_id"] == entry["receipt_id"]
        assert "task_context_conflicts" not in again["capture"]

    def test_recorder_drops_a_malformed_id_and_refuses_before_looking_at_one(self, repo):
        recorder = svc.CaptureRecorder(instance_id="t")
        doc = _claude_docs(repo, SID1)[1]
        assert recorder.record_hook(doc, project_dir=str(repo), task_id="task_junk")[0] == "queued"
        root = recorder.resolve_root(str(repo), None)
        assert root is not None
        live = ch.sessions_dir(root) / f"{svc.queue_key(SID1)}{svc.QUEUE_SUFFIX}"
        assert all("task_id" not in json.loads(ln)["data"] for ln in live.read_text(encoding="utf-8").splitlines())

        other = svc.CaptureRecorder(instance_id="u")
        action, detail = other.record_hook(
            _claude_docs(repo, SID2)[1], project_dir=str(repo), task_id=TASK_A, authorize=lambda _root: False,
        )
        assert (action, detail) == ("rejected", "unauthenticated")
        assert not (ch.sessions_dir(root) / f"{svc.queue_key(SID2)}{svc.QUEUE_SUFFIX}").exists()


# ---------------------------------------------------------------------------
# buffer binding and fold persistence
# ---------------------------------------------------------------------------


class TestBufferAndFold:
    def test_first_declaration_binds_in_the_buffer_then_persists_on_the_receipt(self, repo):
        docs = _claude_docs(repo, SID1)
        handle_hook(docs[0], env=_claude_env(repo), task_id=TASK_A)
        ctx = _read_buffer(repo, SID1)["task_context"]
        assert ctx["task_id"] == TASK_A and ctx["bound_by"] == "SessionStart"
        assert ctx["source"] == "launch_environment" and ctx["evidence"] == "declared"
        for doc in docs[1:]:
            handle_hook(doc, env=_claude_env(repo), task_id=TASK_A)
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A
        assert entry["capture"]["task_context"]["bound_at"] == ctx["bound_at"]
        assert "task_context_conflicts" not in entry["capture"]

    def test_conflicting_declaration_is_counted_and_never_reassigns(self, repo):
        docs = _claude_docs(repo, SID1)
        handle_hook(docs[0], env=_claude_env(repo), task_id=TASK_A)
        handle_hook(docs[1], env=_claude_env(repo), task_id=TASK_B)  # creates the record
        before = _entry(repo, SID1)
        assert before["task_id"] == TASK_A and before["capture"]["task_context_conflicts"] == 1
        outcome = handle_hook(docs[2], env=_claude_env(repo), task_id=TASK_B)
        assert outcome.action != "error"  # observational: a conflict is never a failure
        after = _entry(repo, SID1)
        assert after["task_id"] == TASK_A
        assert after["capture"]["task_context"] == before["capture"]["task_context"]
        assert after["capture"]["task_context_conflicts"] == 2
        for key in ("receipt_id", "shard_id", "run_id"):
            assert after[key] == before[key]

    def test_declaration_is_immutable_across_session_end_and_a_late_hook(self, repo):
        docs = _claude_docs(repo, SID1)
        _drive_claude(repo, docs, TASK_A)
        assert not ch.buffer_path(repo, SID1).exists()  # SessionEnd deleted the staging buffer
        ended = _entry(repo, SID1)
        late = {**docs[2]}  # a background Stop finishing after SessionEnd

        handle_hook(late, env=_claude_env(repo), task_id=TASK_B)
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A and entry["capture"]["task_context_conflicts"] == 1
        assert entry["receipt_id"] == ended["receipt_id"]

        handle_hook(late, env=_claude_env(repo), task_id=TASK_A)  # the same id again: no conflict
        handle_hook(late, env=_claude_env(repo), task_id=None)  # no declaration: kept, not dropped
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A and entry["capture"]["task_context_conflicts"] == 1
        assert entry["capture"]["task_context"] == ended["capture"]["task_context"]

    def test_late_declaration_is_never_retroactive(self, repo):
        docs = _claude_docs(repo, SID1)
        _drive_claude(repo, docs[:3])  # record exists, no declaration
        assert "task_id" not in _entry(repo, SID1)
        handle_hook(docs[2], env=_claude_env(repo), task_id=TASK_A)  # a late Stop that carries one
        entry = _entry(repo, SID1)
        assert "task_id" not in entry and "task_context" not in entry["capture"]
        assert entry["capture"]["task_context_conflicts"] == 1

    def test_first_valid_declaration_binds_after_a_malformed_one(self, repo):
        docs = _claude_docs(repo, SID1)
        handle_hook(docs[0], env=_claude_env(repo), task_id="task_bogus")
        buf = _read_buffer(repo, SID1)
        assert buf["task_context"] is None and buf["task_context_conflicts"] == 0
        for doc in docs[1:]:
            handle_hook(doc, env=_claude_env(repo), task_id=TASK_A)
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A and entry["capture"]["task_context"]["bound_by"] == "UserPromptSubmit"
        assert "task_context_conflicts" not in entry["capture"]

    def test_legacy_pre_task_buffer_still_loads(self, repo):
        docs = _claude_docs(repo, SID1)
        handle_hook(docs[0], env=_claude_env(repo))
        path = ch.buffer_path(repo, SID1)
        buf = json.loads(path.read_text(encoding="utf-8"))
        buf.pop("task_context")
        buf.pop("task_context_conflicts")
        path.write_text(json.dumps(buf), encoding="utf-8")  # a buffer written before this feature
        for doc in docs[1:]:
            handle_hook(doc, env=_claude_env(repo), task_id=TASK_A)
        assert _entry(repo, SID1)["task_id"] == TASK_A

    def test_existing_task_id_without_provenance_stays_without_provenance(self, repo):
        docs = _claude_docs(repo, SID1)
        _drive_claude(repo, docs, None)
        runs_path = repo / ".openshard" / "runs.jsonl"
        entry = _entry(repo, SID1)
        entry["task_id"] = TASK_A
        entry["capture"].pop("task_context", None)
        entry.pop("content_hash", None)
        runs_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        ch.handle_hook(docs[2], env=_claude_env(repo))
        rebuilt = _entry(repo, SID1)
        assert rebuilt["task_id"] == TASK_A
        assert "task_context" not in rebuilt["capture"]

    def test_undeclared_and_declared_records_differ_only_by_the_declaration(self, repo, tmp_path):
        other = _make_repo(tmp_path / "second repo")
        _drive_claude(repo, _claude_docs(repo, SID1), TASK_A)
        _drive_claude(other, _claude_docs(other, SID1), None)
        declared, legacy = _entry(repo, SID1), _entry(other, SID1)
        assert set(declared) - set(legacy) == {"task_id"}
        assert set(legacy) - set(declared) == set()
        assert set(declared["capture"]) - set(legacy["capture"]) == {"task_context"}
        assert derive_shard_identity(declared) == derive_shard_identity(legacy)


# ---------------------------------------------------------------------------
# evidence semantics, cross-agent sessions, no enforcement
# ---------------------------------------------------------------------------


class TestEvidenceAndCorrelation:
    def test_declaration_is_declared_and_observed_facts_keep_their_evidence(self, repo, tmp_path):
        other = _make_repo(tmp_path / "control repo")
        (repo / "auth.py").write_text("x = 1\n", encoding="utf-8")
        (other / "auth.py").write_text("x = 1\n", encoding="utf-8")

        def script(root: Path) -> list[dict]:
            base = {"session_id": SID1, "cwd": str(root), "permission_mode": "default"}
            edit = {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                    "tool_input": {"file_path": str(root / "auth.py")}}
            docs = _claude_docs(root, SID1)
            return [*docs[:2], edit, *docs[2:]]

        for doc in script(repo):
            handle_hook(doc, env=_claude_env(repo), task_id=TASK_A)
        for doc in script(other):
            handle_hook(doc, env=_claude_env(other))
        declared, control = _entry(repo, SID1), _entry(other, SID1)

        assert declared["capture"]["task_context"]["evidence"] == "declared"
        evidence = [ev["evidence"] for ev in declared["events"]]
        assert "declared" not in evidence  # the declaration is never dressed up as an observed event
        assert evidence == [ev["evidence"] for ev in control["events"]]
        started = next(ev for ev in declared["events"] if ev["event_type"] == "session.started")
        assert started["evidence"] == "directly_observed"
        assert any(ev["evidence"] == "agent_reported" for ev in declared["events"])
        assert declared["verification_passed"] is None and control["verification_passed"] is None
        receipt = get_receipt(declared["shard_id"], repo_path=repo)
        assert receipt.status != "passed"  # capture is observational; nothing was verified or enforced

    def test_one_task_many_sessions_across_agents_stay_separate_receipts(self, repo):
        _drive_claude(repo, _claude_docs(repo, SID1), TASK_A)
        _drive_claude(repo, _claude_docs(repo, SID2, prompt="Second Claude session"), TASK_A)
        _drive_codex(repo, _codex_docs(repo, SID1), TASK_A)  # the very same session id, another agent
        _drive_claude(repo, _claude_docs(repo, SID3, prompt="Unrelated work, same wording"), None)

        entries = _lines(repo)
        assert len(entries) == 4
        assert len({e["receipt_id"] for e in entries}) == 4
        assert len({e["shard_id"] for e in entries}) == 4
        declared = [e for e in entries if e.get("task_id") == TASK_A]
        assert len(declared) == 3
        assert sorted((e["executor"], e["capture"]["session_id"]) for e in declared) == sorted([
            (CLAUDE_EXECUTOR, SID1), (CLAUDE_EXECUTOR, SID2), (CODEX_EXECUTOR, SID1)])
        receipts = list_receipts_by_task(TASK_A, repo_path=repo)
        assert len(receipts) == 3
        assert {r.agent for r in receipts} == {"Claude Code (external)", "Codex (external)"}
        assert list_receipts_by_task(TASK_B, repo_path=repo) == []

    def test_a_second_task_id_in_the_same_repository_never_bleeds(self, repo):
        _drive_claude(repo, _claude_docs(repo, SID1), TASK_A)
        _drive_claude(repo, _claude_docs(repo, SID2), TASK_B)
        assert {r.run_id for r in list_receipts_by_task(TASK_A, repo_path=repo)} == {_entry(repo, SID1)["run_id"]}
        assert {r.run_id for r in list_receipts_by_task(TASK_B, repo_path=repo)} == {_entry(repo, SID2)["run_id"]}


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_only_the_opaque_id_and_concise_provenance_are_added(self, service, repo):
        secret = "sk-ant-api03-SECRETSECRET12345678901234567890"
        docs = [
            {**d, "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
             "task_id": TASK_B, "env": {TASK_ID_ENV: TASK_B}}
            for d in _claude_docs(repo, SID1, prompt=f"Fix login with key {secret}")
        ]
        recorder = service.server.recorder
        recorder.pause_processing()
        try:
            _drive_http(service, repo, docs, TASK_A)
            queue_text = (_session_dir(repo) / f"{SID1}{svc.QUEUE_SUFFIX}").read_text(encoding="utf-8")
        finally:
            recorder.resume_processing()
        (entry,) = _wait_ended(service, repo)
        runs_text = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        for text in (queue_text, runs_text):
            assert TASK_B not in text  # the agent payload's own claim is never carried
            assert secret not in text and "transcript.jsonl" not in text
        assert runs_text.count(TASK_A) == 1  # exactly one place: the top-level task_id
        assert entry["task_id"] == TASK_A
        assert set(entry["capture"]["task_context"]) == {"source", "variable", "evidence", "bound_at", "bound_by"}


# ---------------------------------------------------------------------------
# sync pass-through
# ---------------------------------------------------------------------------


class TestSyncCompatibility:
    def test_task_id_crosses_the_sync_projection_without_changing_the_contract(self, repo, tmp_path):
        other = _make_repo(tmp_path / "legacy repo")
        _drive_claude(repo, _claude_docs(repo, SID1), TASK_A)
        _drive_claude(other, _claude_docs(other, SID1), None)
        declared, legacy = _entry(repo, SID1), _entry(other, SID1)

        wire = receipt_payload(declared, 0)
        baseline = receipt_payload(legacy, 0)
        assert wire["task_id"] == TASK_A and baseline["task_id"] is None
        assert set(wire) == set(baseline)  # no key the Platform contract does not already define
        envelope = build_envelope(declared, 0, core_version="test")
        assert envelope["receipt"]["task_id"] == TASK_A
        assert envelope["source"]["receipt_schema_version"] == declared["schema_version"]
        assert eligibility(declared).eligible and eligibility(legacy).eligible

    def test_an_in_progress_declared_session_still_waits_for_quiescence(self, repo):
        docs = _claude_docs(repo, SID1)
        _drive_claude(repo, docs[:3], TASK_A)  # no SessionEnd yet
        entry = _entry(repo, SID1)
        assert entry["task_id"] == TASK_A
        assert not eligibility(entry).eligible  # correlation never bypasses the sync quiescence rule
