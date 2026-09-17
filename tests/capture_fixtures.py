"""Shared helpers and fixtures for the Claude capture-service tests.

Registered as a pytest plugin from ``tests/conftest.py`` so the ``repo``,
``capture_env`` and ``service`` fixtures are available to every test module
without importing them from another test module. Plain helpers
(``_payload``, ``_post``, ``_lines`` ...) are imported explicitly.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"
SID2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


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


def _auth_headers(project_dir: str | None = None) -> dict[str, str]:
    """Headers a raw ``client._request`` needs to be accepted (v0.4.4 token)."""
    return client._auth_headers(None, project_dir)


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


def _queue_line(event_id: str, event: str, **data) -> str:
    base = {"event": event, "session_id": SID}
    base.update(data)
    return json.dumps({"id": event_id, "kind": "hook", "at": "2026-09-03T10:00:00Z", "data": base})
