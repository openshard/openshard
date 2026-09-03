"""Tests for the local Claude Code capture service and its client (PR9.5).

Every test runs its own service in-process on an ephemeral port with
``OPENSHARD_HOME`` pointed at a temp dir (the autouse conftest fixture), so
nothing here can touch the developer's real service, state file or default
port. ``OPENSHARD_CAPTURE_DISABLE`` -- set globally by conftest so the rest
of the suite stays in-process -- is removed for these tests only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks import handle_claude_hook

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"
SID2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECRET = "sk-ant-api03-SECRETSECRET12345678901234567890"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "my repo")


@pytest.fixture
def capture_env(monkeypatch) -> dict:
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    return dict(os.environ)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Service:
    def __init__(self, env: dict, *, port: int = 0, idle_timeout: float = 0.0) -> None:
        self.env = env
        self.ready = threading.Event()
        self.box: list = []
        self.exit_code: int | None = None

        def _run() -> None:
            self.exit_code = svc.serve(port=port, idle_timeout=idle_timeout, env=env,
                                       ready=self.ready, server_box=self.box)

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10), "service did not become ready"

    @property
    def server(self) -> svc.CaptureServer:
        return self.box[0]

    @property
    def port(self) -> int:
        return self.server.port

    def stop(self) -> None:
        if self.box:
            self.box[0].begin_shutdown("test")
        self.thread.join(60)


@pytest.fixture
def service(capture_env):
    running = _Service(capture_env)
    # Clients resolve the port from the state file the service wrote.
    assert client.resolve_port(capture_env) == running.port
    yield running
    running.stop()


def _payload(event: str, repo: Path, session_id: str = SID, **fields) -> bytes:
    base: dict = {
        "session_id": session_id,
        "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": event,
    }
    base.update(fields)
    return json.dumps(base).encode("utf-8")


def _status_payload(repo: Path, session_id: str = SID, *, model_id="claude-sonnet-5", cost=0.25, tokens=1200) -> bytes:
    return json.dumps({
        "session_id": session_id, "cwd": str(repo),
        "model": {"id": model_id, "display_name": "Claude Sonnet 5"},
        "cost": {"total_cost_usd": cost},
        "context_window": {"current_usage": {"input_tokens": tokens, "output_tokens": tokens // 4}},
    }).encode("utf-8")


def _post(port: int, raw: bytes, *, project_dir: str | None = None, event_override: str | None = None) -> bool:
    return client.post_hook(port, raw, project_dir=project_dir, event_override=event_override)


def _lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        # Windows: the worker's atomic temp+replace briefly denies readers.
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _first_line(repo: Path) -> dict | None:
    """The first record, or None -- a single read, unlike ``_lines(repo)[0]``.

    A ``_wait_for`` predicate written as ``bool(_lines(repo)) and
    _lines(repo)[0][...]`` calls ``_lines`` twice; between the two calls the
    file can transiently deny a read (the ``PermissionError`` case above),
    making the second call return ``[]`` even though the first call just
    proved a record exists -- an uncaught ``IndexError`` on ``[][0]``, which
    crashes the test instead of being treated as "not ready yet, poll
    again". Reading once and reusing the result closes that window.
    """
    lines = _lines(repo)
    return lines[0] if lines else None


def _wait_for(predicate, timeout: float = 30.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _session_dir(repo: Path) -> Path:
    return repo / ".openshard" / "claude_sessions"


def _drive_session_http(port: int, repo: Path, session_id: str = SID) -> None:
    assert _post(port, _payload("SessionStart", repo, session_id, source="startup"), project_dir=str(repo))
    assert _post(port, _payload("UserPromptSubmit", repo, session_id, prompt="Add a calculator module"), project_dir=str(repo))
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert _post(port, _payload("PostToolUse", repo, session_id, tool_name="Write",
                                tool_input={"file_path": str(repo / "calc.py"), "content": "RAW"},
                                tool_response={"filePath": str(repo / "calc.py")}), project_dir=str(repo))
    assert _post(port, _payload("PostToolUse", repo, session_id, tool_name="Bash",
                                tool_input={"command": "python -m pytest -q"},
                                tool_response={"stdout": "3 passed"}), project_dir=str(repo))
    assert _post(port, _payload("Stop", repo, session_id, last_assistant_message="done"), project_dir=str(repo))
    assert _post(port, _payload("SessionEnd", repo, session_id, reason="prompt_input_exit"), project_dir=str(repo))


def _drive_session_inline(repo: Path, session_id: str = SID) -> None:
    env = {"CLAUDE_PROJECT_DIR": str(repo)}

    def run(event: str, **fields):
        return handle_claude_hook(json.loads(_payload(event, repo, session_id, **fields)), env=env)

    run("SessionStart", source="startup")
    run("UserPromptSubmit", prompt="Add a calculator module")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    run("PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "calc.py"), "content": "RAW"})
    run("PostToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"})
    run("Stop", last_assistant_message="done")
    run("SessionEnd", reason="prompt_input_exit")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_health_and_state_file(self, service, capture_env):
        doc = client.health(service.port)
        assert doc is not None
        assert doc["service"] == client.SERVICE_NAME
        assert doc["pid"] == os.getpid()
        assert doc["port"] == service.port
        assert isinstance(doc["instance_id"], str)
        state = json.loads(Path(client.state_path(capture_env)).read_text(encoding="utf-8"))
        assert state["port"] == service.port
        assert state["pid"] == os.getpid()
        assert state["instance_id"] == doc["instance_id"]
        # No paths leak through the health document.
        assert str(Path.home()) not in json.dumps(doc)

    def test_shutdown_drains_queue_and_removes_state(self, service, capture_env, repo):
        service.server.recorder.pause_processing()
        _drive_session_http(service.port, repo)
        assert _lines(repo) == []  # nothing folded yet
        assert client.request_shutdown(capture_env, wait_seconds=60)
        service.thread.join(60)
        assert service.exit_code == 0
        assert not Path(client.state_path(capture_env)).exists()
        entries = _lines(repo)
        assert len(entries) == 1 and entries[0]["capture"]["session_end_observed"] is True
        assert not list(_session_dir(repo).glob("*.queue*.jsonl"))

    def test_shutdown_requires_matching_instance_id(self, service):
        status, _ = client._request("POST", service.port, client.SHUTDOWN_PATH,
                                    json.dumps({"instance_id": "nope"}).encode())
        assert status == 403
        assert client.health(service.port) is not None

    def test_second_instance_on_same_port_exits_zero_and_leaves_first(self, service, capture_env):
        second = _Service(capture_env, port=service.port)
        second.thread.join(30)
        assert second.exit_code == 0
        assert not second.box  # it never bound
        assert client.health(service.port)["instance_id"] == service.server.instance_id

    def test_foreign_listener_moves_service_to_next_port(self, capture_env, monkeypatch):
        base = _free_port()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", base))
        blocker.listen(1)
        try:
            monkeypatch.setattr(svc, "DEFAULT_PORT", base)
            monkeypatch.setattr(svc, "PORT_RANGE", 3)
            running = _Service(capture_env, port=None)
            try:
                assert running.port != base
                assert base < running.port < base + 3
                assert client.resolve_port(capture_env) == running.port
                assert client.health(base) is None  # the foreign socket is not mistaken for us
            finally:
                running.stop()
        finally:
            blocker.close()

    def test_pinned_port_env_is_used_by_service_and_client(self, capture_env, monkeypatch):
        port = _free_port()
        monkeypatch.setenv("OPENSHARD_CAPTURE_PORT", str(port))
        env = dict(os.environ)
        running = _Service(env, port=None)
        try:
            assert running.port == port
            assert client.resolve_port(env) == port
        finally:
            running.stop()

    def test_idle_timeout_exits_and_cleans_state(self, capture_env, monkeypatch):
        monkeypatch.setattr(svc, "_IDLE_CHECK_SECONDS", 0.05)
        running = _Service(capture_env, idle_timeout=0.2)
        running.thread.join(30)
        assert running.exit_code == 0
        assert not Path(client.state_path(capture_env)).exists()
        assert client.health(running.port) is None

    def test_ensure_service_states(self, service, capture_env, monkeypatch):
        assert client.ensure_service(capture_env) == (service.port, "running")
        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        assert client.ensure_service(dict(os.environ)) == (None, "disabled")
        monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE")
        service.stop()
        assert client.ensure_service(dict(os.environ)) == (None, "unavailable")  # NO_SPAWN is set

    def test_service_status_and_stop_service(self, service, capture_env):
        status = svc.service_status(capture_env)
        assert status["running"] is True and status["port"] == service.port
        result = svc.stop_service(capture_env, wait_seconds=60)
        assert result == {"was_running": True, "stopped": True, "port": service.port}
        service.thread.join(60)
        after = svc.service_status(capture_env)
        assert after["running"] is False
        assert svc.stop_service(capture_env)["was_running"] is False

    def test_stale_state_file_is_reported_and_cleaned(self, capture_env):
        Path(client.state_path(capture_env)).parent.mkdir(parents=True, exist_ok=True)
        Path(client.state_path(capture_env)).write_text(
            json.dumps({"port": _free_port(), "pid": 999999999, "instance_id": "dead"}), encoding="utf-8")
        status = svc.service_status(capture_env)
        assert status["running"] is False and status["stale_state"] is True
        svc.stop_service(capture_env)
        assert not Path(client.state_path(capture_env)).exists()

    def test_detached_spawn_starts_a_real_service_that_outlives_the_spawner(self, capture_env, monkeypatch):
        port = _free_port()
        monkeypatch.setenv("OPENSHARD_CAPTURE_PORT", str(port))
        monkeypatch.delenv("OPENSHARD_CAPTURE_NO_SPAWN")
        env = dict(os.environ)
        # Spawn from a *child* Python that exits immediately, like a hook
        # process would, so we know the service is not tied to its parent.
        code = (
            "import os,sys,json; from openshard.adapters import claude_capture_client as c; "
            "print(json.dumps(c.ensure_service(dict(os.environ))))"
        )
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        got_port, state = json.loads(result.stdout.strip())
        assert (got_port, state) == (port, "started"), result.stdout
        try:
            doc = client.health(port)
            assert doc is not None and doc["pid"] != os.getpid()
            assert svc._pid_alive(doc["pid"])
            log = Path(client.log_path(env)).read_text(encoding="utf-8")
            assert f"listening on 127.0.0.1:{port}" in log
        finally:
            assert client.request_shutdown(env, wait_seconds=15)
        assert _wait_for(lambda: not svc._pid_alive(doc["pid"]), timeout=15)
        assert not Path(client.state_path(env)).exists()


# ---------------------------------------------------------------------------
# Blocking path
# ---------------------------------------------------------------------------


class TestBlockingPath:
    def test_hook_is_durably_queued_before_ack(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="Fix the login bug"), project_dir=str(repo))
        queue_file = _session_dir(repo) / f"{SID}{svc.QUEUE_SUFFIX}"
        assert queue_file.exists()
        lines = [json.loads(ln) for ln in queue_file.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        line = lines[0]
        assert line["kind"] == "hook"
        assert line["id"].startswith(service.server.instance_id)
        assert line["data"]["event"] == "UserPromptSubmit"
        assert line["data"]["task_excerpt"] == "Fix the login bug"
        assert _lines(repo) == []
        service.server.recorder.resume_processing()
        assert _wait_for(lambda: len(_lines(repo)) == 1)
        assert _lines(repo)[0]["task"] == "Fix the login bug"
        assert not queue_file.exists()

    def test_blocking_path_stays_within_budget(self, service, repo):
        # A regression guard for the whole point of the service: the part
        # Claude Code waits on must be a few milliseconds even while the
        # worker is folding in the background. Bounds are deliberately loose
        # for CI; scripts/bench_claude_capture.py reports the real numbers.
        assert _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="warm"), project_dir=str(repo))
        roundtrips: list[float] = []
        for i in range(40):
            raw = _payload("PostToolUse", repo, tool_name="Bash", tool_input={"command": f"echo {i}"})
            t0 = time.perf_counter()
            assert _post(service.port, raw, project_dir=str(repo))
            roundtrips.append(time.perf_counter() - t0)
        assert service.server.recorder.wait_idle(20)
        timing = client.health(service.port)["blocking_ms"]
        assert timing["n"] >= 42
        assert timing["p50_ms"] < 25, timing
        assert timing["p95_ms"] < 50, timing
        roundtrips.sort()
        assert roundtrips[len(roundtrips) // 2] < 0.05, roundtrips

    def test_malformed_or_unsupported_input_never_errors(self, service):
        for body in (b"", b"not json", b"[1,2]", b"42", b'{"hook_event_name":"Stop"}',
                     b'{"hook_event_name":"Nope","session_id":"' + SID.encode() + b'"}',
                     b'{"hook_event_name":"Stop","session_id":"../../etc"}'):
            status, reply = client._request("POST", service.port, client.HOOK_PATH, body)
            assert status == 200 and reply == b"{}", body
        status, _ = client._request("POST", service.port, "/nope", b"{}")
        assert status == 404
        status, _ = client._request("GET", service.port, "/nope")
        assert status == 404
        stats = client.health(service.port)["stats"]
        assert stats["queued"] == 0

    def test_oversized_body_is_rejected(self, service, monkeypatch):
        monkeypatch.setattr(svc, "MAX_BODY_BYTES", 1024)
        status, _ = client._request("POST", service.port, client.HOOK_PATH, b"{" + b" " * 2048 + b"}")
        assert status == 413

    def test_large_tool_response_is_accepted_but_never_persisted(self, service, repo):
        big = "RAW OUTPUT " * 200_000  # ~2MB
        service.server.recorder.pause_processing()
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="t"), project_dir=str(repo))
        assert _post(service.port, _payload("PostToolUse", repo, tool_name="Bash",
                                            tool_input={"command": "cat big.log"},
                                            tool_response={"stdout": big}), project_dir=str(repo))
        queue_file = _session_dir(repo) / f"{SID}{svc.QUEUE_SUFFIX}"
        assert queue_file.stat().st_size < 4096
        assert "RAW OUTPUT" not in queue_file.read_text(encoding="utf-8")
        service.server.recorder.resume_processing()
        assert _post(service.port, _payload("Stop", repo), project_dir=str(repo))
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1)
        assert "RAW OUTPUT" not in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Eventual consistency: same records as the synchronous path
# ---------------------------------------------------------------------------


_STABLE_EVENT_KEYS = ("event_type", "action", "status", "evidence", "target", "actor", "source")


def _stable_view(entry: dict) -> dict:
    return {
        "task": entry["task"],
        "executor": entry["executor"],
        "files_source": entry["files_source"],
        "files_detail": entry["files_detail"],
        "summary": entry["summary"],
        "capture": {k: v for k, v in entry["capture"].items()
                    if k not in ("started_at", "last_activity_at", "first_prompt_at",
                                 "last_turn_completed_at", "last_status_ping_at",
                                 # Legitimately differs by capture path: the HTTP/service
                                 # path assigns real dedup ids (CaptureRecorder._next_id);
                                 # the inline path never does (dedup_id=None throughout),
                                 # so its applied_event_ids stays empty. Not a correctness
                                 # difference -- see build_hook_entry/apply_reduced_hook.
                                 "applied_event_ids")},
        "events": [{k: e.get(k) for k in _STABLE_EVENT_KEYS} for e in entry["events"]],
        "verification_attempted": entry["verification_attempted"],
        "verification_passed": entry["verification_passed"],
    }


class TestEventualConsistency:
    def test_http_session_produces_the_same_record_as_the_inline_path(self, service, tmp_path):
        via_http = _make_repo(tmp_path / "http repo")
        via_inline = _make_repo(tmp_path / "inline repo")
        _drive_session_http(service.port, via_http)
        _drive_session_inline(via_inline)
        assert _wait_for(lambda: (_e := _first_line(via_http)) is not None and _e["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        http_entry, inline_entry = _lines(via_http)[0], _lines(via_inline)[0]
        assert _stable_view(http_entry) == _stable_view(inline_entry)
        assert http_entry["capture"]["session_id"] == SID
        assert not list(_session_dir(via_http).glob("*"))  # buffer + queue gone after SessionEnd

    def test_stop_is_folded_promptly_after_the_hook_returns(self, service, repo):
        assert _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="quick"), project_dir=str(repo))
        assert service.server.recorder.wait_idle(20)
        t0 = time.perf_counter()
        assert _post(service.port, _payload("Stop", repo), project_dir=str(repo))
        # Bound wide enough to absorb the replay-retry design (a transient
        # replay failure -- e.g. a Windows PermissionError from antivirus
        # briefly holding runs.jsonl open right after the atomic replace --
        # is retried after _RETRY_BACKOFF_SECONDS rather than discarding the
        # evidence; see TestReplayRetryNeverLosesEvidence). Several
        # consecutive transient failures are the realistic worst case this
        # test should tolerate without flaking; a genuine hang would still
        # fail this. The dedicated benchmark (scripts/bench_claude_capture.py)
        # is what asserts the normal-case latency target, not this test.
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1, timeout=15)
        elapsed = time.perf_counter() - t0
        assert elapsed < 15.0, elapsed

    def test_status_ping_via_service_feeds_the_next_fold(self, service, repo):
        assert _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="model please"), project_dir=str(repo))
        assert client.post_status(service.port, _status_payload(repo, cost=0.10), project_dir=str(repo))
        assert client.post_status(service.port, _status_payload(repo, cost=0.35), project_dir=str(repo))
        assert _post(service.port, _payload("Stop", repo), project_dir=str(repo))
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1)
        entry = _lines(repo)[0]
        assert entry["execution_model"] == "claude-sonnet-5"
        assert entry["cost_provenance"] == "provider_reported"
        assert entry["estimated_cost"] == pytest.approx(0.25)
        assert entry["tokens_provenance"] == "provider_reported"

    def test_project_dir_header_anchors_the_repo_when_cwd_is_elsewhere(self, service, repo, tmp_path):
        elsewhere = tmp_path / "somewhere else"
        elsewhere.mkdir()
        raw = _payload("UserPromptSubmit", elsewhere, prompt="anchored")
        assert _post(service.port, raw, project_dir=str(repo))
        assert _wait_for(lambda: len(_lines(repo)) == 1)
        assert not (elsewhere / ".openshard").exists()

    def test_repositories_are_isolated(self, service, tmp_path):
        a = _make_repo(tmp_path / "a")
        b = _make_repo(tmp_path / "b")
        assert _post(service.port, _payload("UserPromptSubmit", a, SID, prompt="task a"), project_dir=str(a))
        assert _post(service.port, _payload("UserPromptSubmit", b, SID2, prompt="task b"), project_dir=str(b))
        assert _wait_for(lambda: len(_lines(a)) == 1 and len(_lines(b)) == 1)
        assert _lines(a)[0]["task"] == "task a" and _lines(b)[0]["task"] == "task b"
        assert not (a / ".openshard" / "claude_sessions" / f"{SID2}.json").exists()

    def test_event_override_query_parameter(self, service, repo):
        raw = json.dumps({"session_id": SID, "cwd": str(repo), "prompt": "no event name"}).encode()
        assert _post(service.port, raw, project_dir=str(repo), event_override="UserPromptSubmit")
        assert _wait_for(lambda: len(_lines(repo)) == 1)
        assert _lines(repo)[0]["capture"]["prompt_count"] == 1


# ---------------------------------------------------------------------------
# Recovery and idempotence
# ---------------------------------------------------------------------------


def _queue_line(event_id: str, event: str, **data) -> str:
    base = {"event": event, "session_id": SID}
    base.update(data)
    return json.dumps({"id": event_id, "kind": "hook", "at": "2026-09-03T10:00:00Z", "data": base})


class TestReplayRetryNeverLosesEvidence:
    """Regression test for a real bug found while investigating an
    intermittent test flake: a transient error during replay (observed
    cause on this machine: a Windows PermissionError on runs.jsonl, almost
    certainly antivirus briefly holding the file open right after the
    atomic replace inside upsert_jsonl) used to be swallowed into an
    "error" outcome and the queue file was then unlinked unconditionally
    regardless -- silently and permanently discarding already-durably-
    queued evidence. The fix (_drain_session/_replay_file) only removes a
    queue file once every line in it has actually been applied, and
    schedules a short, bounded retry for a session that had a failure
    instead. Slow folding is acceptable; losing evidence is not.
    """

    def test_a_transient_replay_error_is_retried_not_lost(self, service, repo, monkeypatch):
        monkeypatch.setattr(svc, "_RETRY_BACKOFF_SECONDS", 0.1)
        real_apply = svc.apply_reduced_hook
        calls = {"n": 0}

        def flaky(reduced, root, *, dedup_id=None, at=None):
            if reduced.event == "Stop" and calls["n"] == 0:
                calls["n"] += 1
                return ch.HookOutcome(event=reduced.event, action="error",
                                      session_id=reduced.session_id, detail="PermissionError")
            return real_apply(reduced, root, dedup_id=dedup_id, at=at)

        with patch.object(svc, "apply_reduced_hook", side_effect=flaky):
            assert _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
            assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="t"), project_dir=str(repo))
            assert service.server.recorder.wait_idle(20)
            assert _post(service.port, _payload("Stop", repo), project_dir=str(repo))
            # Must eventually succeed via the scheduled retry -- not lost.
            assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1, timeout=10)

        assert calls["n"] == 1, "the flaky wrapper should have been hit exactly once before succeeding"
        stats = client.health(service.port)["stats"]
        assert stats["replay_errors"] >= 1
        assert stats["replayed"] >= 1

    def test_queue_file_is_kept_not_deleted_while_a_retry_is_pending(self, service, repo, monkeypatch):
        # A much longer backoff than the test needs, so we can inspect the
        # on-disk state *during* the retry window rather than racing it.
        monkeypatch.setattr(svc, "_RETRY_BACKOFF_SECONDS", 5.0)

        def always_fails(reduced, root, *, dedup_id=None, at=None):
            return ch.HookOutcome(event=reduced.event, action="error",
                                  session_id=reduced.session_id, detail="PermissionError")

        with patch.object(svc, "apply_reduced_hook", side_effect=always_fails):
            assert _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
            assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="t"), project_dir=str(repo))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if client.health(service.port)["stats"]["replay_errors"] >= 1:
                    break
                time.sleep(0.02)
            # Two events (SessionStart, UserPromptSubmit) can each be rotated
            # into their own file if a drain attempt runs between the two
            # posts -- that is fine (both get retried); the property under
            # test is that failed evidence is never discarded, not that
            # exactly one file happens to exist.
            queued = list(_session_dir(repo).glob("*.queue.*.jsonl"))
            assert len(queued) >= 1, "the failed line(s) must not be discarded while a retry is pending"
        # Patch lifted: the next retry (well within the 5s backoff having
        # already been in flight) uses the real function and succeeds.
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["prompt_count"] == 1, timeout=10)


class TestRecovery:
    def test_duplicate_ids_are_applied_once(self, service, repo):
        directory = _session_dir(repo)
        directory.mkdir(parents=True)
        (directory / f"{SID}{svc.QUEUE_SUFFIX}").write_text(
            "\n".join([
                _queue_line("x-1", "UserPromptSubmit", task_excerpt="once"),
                _queue_line("x-1", "UserPromptSubmit", task_excerpt="once"),
                _queue_line("x-2", "Stop"),
                _queue_line("x-2", "Stop"),
            ]) + "\n", encoding="utf-8")
        assert service.server.recorder.recover(repo) == 1
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] >= 1)
        assert service.server.recorder.wait_idle(20)
        entry = _lines(repo)[0]
        assert entry["capture"]["prompt_count"] == 1
        assert entry["capture"]["turn_count"] == 1
        assert client.health(service.port)["stats"]["duplicates"] == 2

    def test_leftover_queue_is_replayed_when_the_service_starts(self, capture_env, repo):
        directory = _session_dir(repo)
        directory.mkdir(parents=True)
        (directory / f"{SID}{svc.QUEUE_SUFFIX}").write_text(
            _queue_line("old-1", "UserPromptSubmit", task_excerpt="left behind") + "\n"
            + _queue_line("old-2", "Stop") + "\n", encoding="utf-8")
        state = Path(client.state_path(capture_env))
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"port": _free_port(), "pid": 0, "instance_id": "gone",
                                     "recent_repos": [str(repo)]}), encoding="utf-8")
        running = _Service(capture_env)
        try:
            assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1)
            assert _lines(repo)[0]["task"] == "left behind"
            assert _lines(repo)[0]["capture"]["first_prompt_at"] == "2026-09-03T10:00:00Z"
            # The recovered repo stays in the new instance's state file.
            new_state = json.loads(state.read_text(encoding="utf-8"))
            assert new_state["instance_id"] == running.server.instance_id
            assert str(repo) in new_state["recent_repos"]
        finally:
            running.stop()

    def test_leftover_queue_is_replayed_on_session_start_for_that_repo(self, service, repo):
        directory = _session_dir(repo)
        directory.mkdir(parents=True)
        (directory / f"{SID2}{svc.QUEUE_SUFFIX}").write_text(
            json.dumps({"id": "z-1", "kind": "hook", "at": "2026-09-03T10:00:00Z",
                        "data": {"event": "UserPromptSubmit", "session_id": SID2, "task_excerpt": "orphan"}}) + "\n",
            encoding="utf-8")
        assert _post(service.port, _payload("SessionStart", repo, SID, source="startup"), project_dir=str(repo))
        assert _wait_for(lambda: any(e["task"] == "orphan" for e in _lines(repo)))

    def test_no_event_is_lost_when_the_service_dies_mid_queue(self, capture_env, repo):
        # Build a recorder + server by hand so the "crash" skips the drain.
        recorder = svc.CaptureRecorder(instance_id="crashy")
        recorder.pause_processing()
        recorder.start()
        server = svc.CaptureServer(0, recorder, instance_id="crashy", started_at="now")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            for raw in (_payload("UserPromptSubmit", repo, prompt="survive"),
                        _payload("PostToolUse", repo, tool_name="Bash", tool_input={"command": "ls"}),
                        _payload("Stop", repo)):
                assert _post(server.port, raw, project_dir=str(repo))
        finally:
            server.shutdown()
            server.server_close()
        assert _lines(repo) == []
        queued = list(_session_dir(repo).glob("*.queue*.jsonl"))
        assert len(queued) == 1
        state = Path(client.state_path(capture_env))
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"recent_repos": [str(repo)]}), encoding="utf-8")
        running = _Service(capture_env)
        try:
            assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1)
            entry = _lines(repo)[0]
            assert entry["task"] == "survive"
            assert entry["capture"]["tool_call_count"] == 1
            assert running.server.recorder.wait_idle(20)
            assert not list(_session_dir(repo).glob("*.queue*.jsonl"))
        finally:
            running.stop()

    def test_corrupt_queue_lines_are_skipped(self, service, repo):
        directory = _session_dir(repo)
        directory.mkdir(parents=True)
        (directory / f"{SID}{svc.QUEUE_SUFFIX}").write_text(
            "garbage\n{\"id\": 1}\n" + _queue_line("ok-1", "UserPromptSubmit", task_excerpt="fine") + "\n"
            + json.dumps({"id": "bad-2", "kind": "hook", "at": "x", "data": {"event": "Nope", "session_id": SID}}) + "\n",
            encoding="utf-8")
        service.server.recorder.recover(repo)
        assert _wait_for(lambda: len(_lines(repo)) == 1)
        assert _lines(repo)[0]["task"] == "fine"
        assert client.health(service.port)["stats"]["replay_errors"] == 0


# ---------------------------------------------------------------------------
# Privacy and paths
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_queue_file_never_holds_raw_input(self, service, repo):
        service.server.recorder.pause_processing()
        outside = str(Path(repo).parent / "outside.txt")
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt=f"Use key {SECRET} to deploy"),
                     project_dir=str(repo))
        assert _post(service.port, _payload("PostToolUse", repo, tool_name="Write",
                                            tool_input={"file_path": str(repo / "src" / "app.py"), "content": "FILE BODY"},
                                            tool_response={"content": "FILE BODY"}), project_dir=str(repo))
        assert _post(service.port, _payload("PostToolUse", repo, tool_name="Edit",
                                            tool_input={"file_path": outside}), project_dir=str(repo))
        assert _post(service.port, _payload("PostToolUse", repo, tool_name="Bash",
                                            tool_input={"command": f"curl -H 'Authorization: Bearer {SECRET}' x"},
                                            tool_response={"stdout": "SECRET OUTPUT"}), project_dir=str(repo))
        assert _post(service.port, _payload("Stop", repo, last_assistant_message="ASSISTANT TEXT"), project_dir=str(repo))
        text = (_session_dir(repo) / f"{SID}{svc.QUEUE_SUFFIX}").read_text(encoding="utf-8")
        for forbidden in (SECRET, "FILE BODY", "SECRET OUTPUT", "ASSISTANT TEXT", "transcript", str(repo), outside):
            assert forbidden not in text, forbidden
        lines = [json.loads(ln)["data"] for ln in text.splitlines()]
        assert lines[1]["file_target"] == "src/app.py"
        assert lines[2]["file_target"] is None and lines[2]["file_dropped"] is True
        # A command carrying a secret is scrubbed; when the scrubbed text is
        # still judged unsafe the whole command is replaced by a neutral label.
        assert lines[3]["command_action"] in ("Bash command (redacted)",) or lines[3]["command_action"].startswith("Bash:")
        service.server.recorder.resume_processing()
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1)
        stored = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        for forbidden in (SECRET, "FILE BODY", "SECRET OUTPUT", "ASSISTANT TEXT", str(repo), outside):
            assert forbidden not in stored, forbidden

    def test_windows_style_paths(self, service, repo):
        win_cwd = str(repo).replace("/", "\\") + "\\"
        raw = json.dumps({
            "session_id": SID, "cwd": win_cwd, "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": str(repo).replace("/", "\\") + "\\pkg\\mod.py"},
        }).encode()
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="win"), project_dir=win_cwd)
        (repo / "pkg").mkdir()
        (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        assert _post(service.port, raw, project_dir=win_cwd)
        assert _post(service.port, _payload("Stop", repo), project_dir=win_cwd)
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["turn_count"] == 1)
        entry = _lines(repo)[0]
        assert any(f["path"] == "pkg/mod.py" for f in entry["files_detail"])
        assert "\\" not in json.dumps(entry["files_detail"])


# ---------------------------------------------------------------------------
# Client (the command-form entrypoints)
# ---------------------------------------------------------------------------


class TestClient:
    def test_hook_command_forwards_when_the_service_is_up(self, service, repo):
        import io

        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        label = client.run_hook_via_service(io.BytesIO(_payload("UserPromptSubmit", repo, prompt="fwd")), env=env)
        assert label == "forwarded"
        assert _wait_for(lambda: len(_lines(repo)) == 1)
        assert client.health(service.port)["stats"]["queued"] == 1

    def test_hook_command_falls_back_inline_when_unavailable(self, capture_env, repo):
        import io

        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}  # NO_SPAWN set, nothing running
        label = client.run_hook_via_service(io.BytesIO(_payload("UserPromptSubmit", repo, prompt="inline")), env=env)
        assert label == "record_created"
        assert _lines(repo)[0]["task"] == "inline"

    def test_disable_env_never_contacts_a_running_service(self, service, repo, monkeypatch):
        import io

        monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        label = client.run_hook_via_service(io.BytesIO(_payload("UserPromptSubmit", repo, prompt="direct")), env=env)
        assert label == "record_created"
        monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE")
        assert client.health(service.port)["stats"]["received"] == 0

    def test_status_command_returns_text_and_records_via_service(self, service, repo):
        import io

        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="s"), project_dir=str(repo))
        text = client.run_status_via_service(io.BytesIO(_status_payload(repo)), env=env)
        assert text == "my repo · Claude Sonnet 5"
        assert _post(service.port, _payload("Stop", repo), project_dir=str(repo))
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e.get("execution_model") == "claude-sonnet-5")

    def test_status_command_falls_back_inline(self, capture_env, repo):
        import io

        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        handle_claude_hook(json.loads(_payload("UserPromptSubmit", repo, prompt="s")), env=env)
        text = client.run_status_via_service(io.BytesIO(_status_payload(repo)), env=env)
        assert text == "my repo · Claude Sonnet 5"
        buf = json.loads((_session_dir(repo) / f"{SID}.json").read_text(encoding="utf-8"))
        assert buf["model_current"] == "claude-sonnet-5"

    def test_status_command_handles_bad_input(self, capture_env):
        import io

        assert client.run_status_via_service(io.BytesIO(b""), env=dict(os.environ)) == ""
        assert client.run_status_via_service(io.BytesIO(b"nope"), env=dict(os.environ)) == ""
        assert client.run_hook_via_service(io.BytesIO(b""), env=dict(os.environ)) == "ignored"

    @pytest.mark.parametrize("cwd", [
        "C:\\Users\\dev\\proj", "C:\\Users\\dev\\proj\\", "/home/dev/proj", "/home/dev/proj/", "", "C:\\",
    ])
    def test_fallback_status_text_matches_inline_renderer(self, cwd):
        data = {"cwd": cwd, "model": {"display_name": "Claude Sonnet 5"}}
        assert client._fallback_status_text(json.dumps(data).encode()) == ch._status_line_text(data)

    def test_client_imports_stay_minimal(self):
        # The command-form entrypoints pay Python start-up already; the
        # client must not add the hook module's import cost on top.
        code = (
            "import sys; import openshard.adapters.claude_capture_client; "
            "print(sorted(m for m in sys.modules if m.startswith('openshard')))"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert "openshard.adapters.claude_hooks" not in result.stdout
        assert "openshard.history.jsonl_store" not in result.stdout


# ---------------------------------------------------------------------------
# Installer / setup integration
# ---------------------------------------------------------------------------


class TestSetupIntegration:
    def test_setup_targets_the_running_service_port(self, service, repo):
        from openshard.adapters.claude_hooks_install import installed_hook_port, load_settings
        from openshard.adapters.claude_setup import run_setup

        def _run(argv, **kwargs):
            if "check-ignore" in argv or "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=".git/info/exclude\n", stderr="")
            if "get" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout='No MCP server named "openshard".', stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="Added", stderr="")

        with patch("openshard.adapters.claude_mcp_install.subprocess.run", side_effect=_run), \
             patch("openshard.adapters.claude_mcp_install.shutil.which",
                   side_effect=lambda n: f"/usr/local/bin/{n}"):
            result = run_setup(repo_path=repo)
        assert result.readiness == "ready"
        assert result.capture_service == {"state": "running", "port": service.port}
        settings, _ = load_settings(repo)
        assert installed_hook_port(settings) == service.port
        assert result.to_dict()["capture_service"]["port"] == service.port

    def test_detect_reports_port_mismatch_and_legacy_hooks(self, service, repo):
        from openshard.adapters.claude_hooks_install import HOOK_COMMAND, install_claude_hooks
        from openshard.adapters.claude_setup import detect_claude_integration

        with patch("openshard.adapters.claude_hooks_install.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            install_claude_hooks(repo_root=repo, port=service.port + 1)
        with patch("openshard.adapters.claude_mcp_install.shutil.which", return_value=None):
            status = detect_claude_integration(repo)
        assert status.capture_service["running"] is True
        assert status.hooks_port == service.port + 1
        assert status.capture_port_mismatch is True
        assert status.hooks_need_upgrade is False
        settings_path = repo / ".claude" / "settings.local.json"
        settings_path.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND}]}]}}), encoding="utf-8")
        with patch("openshard.adapters.claude_mcp_install.shutil.which", return_value=None):
            legacy = detect_claude_integration(repo)
        assert legacy.hooks_need_upgrade is True
        assert legacy.hooks_port is None
        assert legacy.to_dict()["hooks_need_upgrade"] is True


# ---------------------------------------------------------------------------
# Windows console safety (regression: FreeConsole must never be a
# serve()-embedded side effect -- see the PR9.5 console-storm incident)
# ---------------------------------------------------------------------------


class TestConsoleSafety:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only console-detach regression")
    def test_serve_never_frees_the_caller_console(self, capture_env):
        """serve() embedded in-process (tests, benchmarks, foreground `capture serve`)
        must never detach this process's own console. A prior version called
        FreeConsole() unconditionally, which broke every git subprocess
        spawned afterwards by *this same process* (each one then allocated
        its own new, visible console) -- this is a direct regression test
        for that incident, not just an indirect behavioral check.
        """
        import ctypes

        with patch.object(ctypes.windll.kernel32, "FreeConsole") as mock_free:
            running = _Service(capture_env)
            try:
                assert client.health(running.port) is not None
            finally:
                running.stop()
        mock_free.assert_not_called()

    def test_serve_has_no_console_detach_helper(self):
        # Belt-and-braces: the mechanism itself must be gone, not merely
        # unreached on this code path.
        assert not hasattr(svc, "_detach_console")


# ---------------------------------------------------------------------------
# Spawn coordination: lock + cooldown must prevent spawn storms
# ---------------------------------------------------------------------------


def _spawnable_env(tmp_path: Path) -> dict:
    """An env with no service running yet and spawning genuinely allowed
    (unlike every other fixture in this file, which sets NO_SPAWN)."""
    env = {**os.environ, "OPENSHARD_HOME": str(tmp_path / "home")}
    env.pop("OPENSHARD_CAPTURE_DISABLE", None)
    env.pop("OPENSHARD_CAPTURE_NO_SPAWN", None)
    return env


class TestSpawnCoordination:
    def test_ensure_service_spawns_at_most_once_under_concurrency(self, tmp_path):
        env = _spawnable_env(tmp_path)
        calls: list[int] = []

        def fake_spawn(e):
            calls.append(1)
            return None  # simulate Popen itself failing -- fast, deterministic

        results: list[tuple] = []

        def worker():
            results.append(client.ensure_service(dict(env), wait_seconds=0.2))

        with patch.object(client, "spawn_service", side_effect=fake_spawn):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(15)

        assert len(calls) == 1, "8 concurrent hooks must converge on exactly one real spawn attempt"
        assert all(state == "unavailable" for _port, state in results)
        assert len(results) == 8

    def test_cooldown_prevents_immediate_retry_but_allows_after_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(client, "_SPAWN_COOLDOWN_SECONDS", 0.2)
        env = _spawnable_env(tmp_path)
        calls: list[int] = []
        with patch.object(client, "spawn_service", side_effect=lambda e: calls.append(1) or None):
            assert client.ensure_service(dict(env), wait_seconds=0.1) == (None, "unavailable")
            assert len(calls) == 1
            # Immediately again: still in cooldown, must not spawn a second time.
            assert client.ensure_service(dict(env), wait_seconds=0.1) == (None, "unavailable")
            assert len(calls) == 1
            time.sleep(0.25)
            # Cooldown window has passed: a new attempt is allowed.
            assert client.ensure_service(dict(env), wait_seconds=0.1) == (None, "unavailable")
            assert len(calls) == 2

    def test_maybe_spawn_service_never_blocks_long_and_spawns_at_most_once(self, tmp_path, monkeypatch):
        # health() is mocked here (unlike the other tests in this class):
        # a real socket connect attempt's variance (up to _CONNECT_TIMEOUT)
        # multiplied across 6 threads can, on a loaded box, exceed even a
        # generous cooldown window and legitimately allow a second attempt
        # (the cooldown is a bounded window, not a permanent latch -- that
        # is correct behavior, not a bug). Removing that source of jitter
        # keeps this test a tight, deterministic check of the lock itself.
        monkeypatch.setattr(client, "health", lambda *a, **k: None)
        monkeypatch.setattr(client, "_SPAWN_COOLDOWN_SECONDS", 5.0)
        env = _spawnable_env(tmp_path)
        calls: list[int] = []

        def worker():
            client.maybe_spawn_service(dict(env), wait_seconds=0.2, lock_wait_seconds=2.0)

        with patch.object(client, "spawn_service", side_effect=lambda e: calls.append(1) or None):
            t0 = time.monotonic()
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            elapsed = time.monotonic() - t0
        assert len(calls) == 1, "the status line must not spawn once per concurrent ping"
        assert elapsed < 3.0, f"maybe_spawn_service must stay bounded, took {elapsed:.2f}s"

    def test_maybe_spawn_service_is_a_noop_when_disabled_or_no_spawn(self, tmp_path):
        env = _spawnable_env(tmp_path)
        with patch.object(client, "spawn_service") as mock_spawn:
            client.maybe_spawn_service({**env, "OPENSHARD_CAPTURE_DISABLE": "1"})
            client.maybe_spawn_service({**env, "OPENSHARD_CAPTURE_NO_SPAWN": "1"})
        mock_spawn.assert_not_called()

    def test_already_healthy_short_circuits_without_spawning(self, service, capture_env):
        # The common case (a service is already up) must never touch the
        # lock or call spawn_service at all -- zero added overhead.
        with patch.object(client, "spawn_service") as mock_spawn:
            assert client.ensure_service(dict(capture_env)) == (service.port, "running")
            client.maybe_spawn_service(dict(capture_env))
        mock_spawn.assert_not_called()


class TestPortRaceGrace:
    def test_wait_for_owner_health_is_bounded_when_nothing_ever_answers(self):
        port = _free_port()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            t0 = time.monotonic()
            result = svc._wait_for_owner_health(port, timeout=0.4)
            elapsed = time.monotonic() - t0
        finally:
            blocker.close()
        assert result is False
        assert 0.35 <= elapsed <= 3.0, elapsed

    def test_wait_for_owner_health_recognizes_a_real_service_promptly(self, service):
        # The success path already exercised implicitly by every other test
        # in this file (siblings never duplicate a live service); this pins
        # down the specific helper _bind relies on to make that call.
        assert svc._wait_for_owner_health(service.port, timeout=2.0) is True


# ---------------------------------------------------------------------------
# Durability: dedup must survive a session ending (buffer deletion)
# ---------------------------------------------------------------------------


class TestDedupAcrossSessionEnd:
    def test_a_late_duplicate_after_session_end_does_not_double_count(self, service, repo):
        _drive_session_http(service.port, repo)
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        entry_before = _lines(repo)[0]
        assert entry_before["capture"]["applied_event_ids"], "ended record must persist its dedup ids"
        turns_before = entry_before["capture"]["turn_count"]
        tools_before = entry_before["capture"]["tool_call_count"]
        replayed_id = entry_before["capture"]["applied_event_ids"][-1]

        # Simulate a leftover queue file surviving past SessionEnd (e.g. a
        # crash mid-drain) that gets replayed again later, re-using an id
        # that was already applied and persisted into the finalized record.
        directory = _session_dir(repo)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{SID}.queue.late.jsonl").write_text(
            json.dumps({"id": replayed_id, "kind": "hook", "at": "2026-09-03T10:00:00Z",
                        "data": {"event": "Stop", "session_id": SID}}) + "\n",
            encoding="utf-8",
        )
        service.server.recorder.enqueue(repo, SID)
        assert service.server.recorder.wait_idle(20)

        entry_after = _lines(repo)[0]
        assert entry_after["capture"]["turn_count"] == turns_before
        assert entry_after["capture"]["tool_call_count"] == tools_before
        assert client.health(service.port)["stats"]["duplicates"] >= 1
