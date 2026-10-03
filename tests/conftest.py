"""Shared pytest fixtures for the OpenShard test suite."""
from __future__ import annotations

import os
import socket
from unittest.mock import patch

import pytest

# Capture-service fixtures (``repo``, ``capture_env``, ``service``) shared by
# the capture, hook and v0.4.4 test modules.
pytest_plugins = ["tests.capture_fixtures"]


@pytest.fixture(autouse=True)
def _isolate_learning_worker(monkeypatch):
    """History writes in tests never launch a background learning worker.

    The production switch, so it also holds for OpenShard subprocesses a test
    starts. Tests that need a published learning snapshot ask for one
    explicitly (``learning_fixtures.publish_learning`` or ``inline_learning``);
    tests of the scheduler itself use ``real_learning_scheduler``.
    """
    monkeypatch.setenv("OPENSHARD_LEARNING_WORKER", "0")


class LearningLaunches(list):
    """Recorded ``(argv, kwargs)`` worker launches. ``pid`` is what each fake process
    reports: a live one by default (a worker still starting), or ``NO_SUCH_PID``."""

    def __init__(self) -> None:
        super().__init__()
        self.pid = os.getpid()


@pytest.fixture
def real_learning_scheduler(monkeypatch):
    """The real scheduler and ``nudge``, with process launches recorded instead of run."""
    from openshard.learning import worker

    monkeypatch.delenv("OPENSHARD_LEARNING_WORKER", raising=False)
    launches = LearningLaunches()

    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return type("Launched", (), {"pid": launches.pid})()

    monkeypatch.setattr(worker, "_spawn", spawn)
    return launches


NO_SUCH_PID = 2**31 - 4  # a valid pid value that is never allocated in practice


@pytest.fixture
def inline_learning(monkeypatch):
    """History writes refresh the learning snapshot synchronously, in-process.

    Stands in for the background worker having caught up, for end-to-end tests
    where one OSN run must learn from the previous one.
    """
    from openshard.learning.worker import refresh_snapshot

    monkeypatch.setattr(
        "openshard.learning.worker.schedule_update",
        lambda path, *a, **kw: refresh_snapshot(path) if path.name == "runs.jsonl" else None,
    )


GENEROUS_LEARNING_BUDGET_MS = 5000.0


@pytest.fixture
def generous_learning_budget(monkeypatch):
    """Keep tests that assert on learning data independent of CPU load.

    OSN's real lookup budget (25 ms, at most 100) is deliberately small and
    fails open to ``timeout``, which is correct but nondeterministic on a busy
    test machine. This raises the default and the ceiling for the test only;
    tests of the budget itself pass explicit budgets and are unaffected.
    """
    monkeypatch.setattr("openshard.learning.snapshot.DEFAULT_BUDGET_MS", GENEROUS_LEARNING_BUDGET_MS)
    monkeypatch.setattr("openshard.learning.snapshot.MAX_BUDGET_MS", GENEROUS_LEARNING_BUDGET_MS)


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(autouse=True)
def _isolate_capture_service(tmp_path_factory, monkeypatch):
    """Keep every test away from the developer's real Claude capture service.

    The Claude Code hook / status-line entrypoints forward to a per-user
    local service (PR9.5) whose state file lives under ``~/.openshard``.
    With ``OPENSHARD_CAPTURE_DISABLE`` set they handle everything
    in-process (the pre-PR9.5 behaviour most CLI tests assert), and never
    open a socket to the real default port. ``OPENSHARD_HOME`` is pointed
    at a throw-away directory so no test can read or write the real state
    file. Tests that exercise the service itself delete the DISABLE knob
    again (see tests/test_claude_capture_service.py).
    """
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path_factory.mktemp("openshard-home")))
    monkeypatch.setenv("OPENSHARD_CAPTURE_DISABLE", "1")
    monkeypatch.delenv("OPENSHARD_CAPTURE_PORT", raising=False)
    # v0.4.4: the *default* port is repointed at a free ephemeral port too.
    # Tests that re-enable the service (no state file yet, or a state file
    # for a port that has since closed) fall back to DEFAULT_PORT, and with
    # the real value that is the developer's own live capture service --
    # `ensure_service` would report it "running", `stop_service` would stop
    # it. No test may depend on whether a real OpenShard service is up.
    from openshard.adapters import claude_capture_client as _client
    from openshard.adapters import claude_capture_service as _service

    _isolated_default = _free_loopback_port()
    monkeypatch.setattr(_client, "DEFAULT_PORT", _isolated_default)
    monkeypatch.setattr(_service, "DEFAULT_PORT", _isolated_default)
    # Telemetry (0.4.2) is off for the whole suite and never flushes on a
    # background thread, so no test can send anything or leak a late flush
    # into another test's RecordingTransport. tests/test_telemetry*.py turn
    # it back on for themselves with an injected transport.
    monkeypatch.setenv("OPENSHARD_TELEMETRY", "off")
    monkeypatch.setenv("OPENSHARD_TELEMETRY_NO_BACKGROUND", "1")
    # The other kill-switches are cleared so that "off" above is the *only*
    # reason telemetry is off: a test that turns it on must then see it on,
    # whether the suite runs on a laptop or on a CI runner (which sets CI /
    # GITHUB_ACTIONS). Tests about CI detection set those variables themselves.
    for var in ("DO_NOT_TRACK", "CI", "GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(var, raising=False)
    # Capture must not depend on the agent this suite happens to run under
    # (provider/surface come from these), and the session-end pull-request
    # lookup must never call a real ``gh``; tests that need it opt back in.
    for var in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "ANTHROPIC_BASE_URL", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENSHARD_PR_LOOKUP", "off")
    yield


@pytest.fixture(autouse=True)
def _default_pipeline_provider():
    """Patch detect_provider at the pipeline import site for every test.

    Pipeline integration tests mock ExecutionGenerator to avoid real API
    calls, but they don't set any API key env var.  Without this fixture,
    detect_provider() raises ValueError and the pipeline exits before the
    mocked generator is ever reached, breaking those tests.

    Tests that exercise detect_provider() directly import it from
    openshard.config.settings and are unaffected by this patch.
    """
    with patch("openshard.run.pipeline.detect_provider", return_value="openrouter"):
        yield
