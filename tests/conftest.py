"""Shared pytest fixtures for the OpenShard test suite."""
from __future__ import annotations

import atexit
import os
import shutil
import socket
import tempfile
from unittest.mock import patch

import pytest

# Credentials that would let a test reach a real Platform organisation, a
# connected cloud capture or a remote capture. No test may inherit them.
_LIVE_CREDENTIAL_ENV = (
    "OPENSHARD_PLATFORM_ENDPOINT",
    "OPENSHARD_PLATFORM_ORG_ID",
    "OPENSHARD_PLATFORM_API_KEY",
    "OPENSHARD_CONNECTED_ENDPOINT",
    "OPENSHARD_CONNECTED_ORG_ID",
    "OPENSHARD_CONNECTED_TOKEN",
    "OPENSHARD_CONNECTED_CREDENTIAL_MODE",
    "OPENSHARD_CONNECTED_SURFACE",
    "OPENSHARD_REMOTE_CAPTURE_URL",
    "OPENSHARD_REMOTE_TOKEN",
)

def _isolate_user_home() -> tuple[str, str]:
    """Point every home-directory variable at a throw-away directory, for the whole run.

    ``OPENSHARD_HOME`` is isolated per test further down, but code that is
    handed its own ``env`` mapping (``handle_claude_hook(doc, env={...})``)
    and every subprocess started with a fresh environment fall back to
    ``~/.openshard`` -- the developer's real capture token, model cache and
    Platform link (``platform.json``), which holds a live organisation key.
    Changing the variables here, at import and before any ``openshard``
    module loads, covers in-process code, module-level caches and every
    subprocess, since they inherit ``os.environ``.

    Done once per run: xdist workers, and a second import of this module
    under another name, inherit the isolated home through the environment.
    Returns ``(real home, isolated home)``.
    """
    if os.environ.get("OPENSHARD_TEST_ISOLATED_HOME"):
        return os.environ["OPENSHARD_TEST_REAL_HOME"], os.environ["OPENSHARD_TEST_ISOLATED_HOME"]
    real = os.path.expanduser("~")
    home = tempfile.mkdtemp(prefix="openshard-test-home-")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    os.environ["OPENSHARD_TEST_REAL_HOME"] = real
    os.environ["OPENSHARD_TEST_ISOLATED_HOME"] = home
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home  # what ``expanduser`` reads on Windows
    drive, rest = os.path.splitdrive(home)
    if drive:
        os.environ["HOMEDRIVE"], os.environ["HOMEPATH"] = drive, rest
    for name in _LIVE_CREDENTIAL_ENV:
        os.environ.pop(name, None)
    return real, home


REAL_USER_HOME, ISOLATED_USER_HOME = _isolate_user_home()

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
    # Never let a developer's Herdr session (or its socket) reach a test.
    for _name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_SOCKET_PATH", "HERDR_BIN_PATH"):
        monkeypatch.delenv(_name, raising=False)
    from openshard.util import herdr as _herdr

    _herdr.clear_cache()
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
                "ANTHROPIC_BASE_URL", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENSHARD_PR_LOOKUP", "off")
    # Commit verification is on by default for captured sessions: no test may
    # start a real detached ``openshard verify``. Tests of the trigger pass
    # their own env and spawner (tests/test_commit_verification.py).
    monkeypatch.setenv("OPENSHARD_NO_AUTO_VERIFY", "1")
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "session_receipts(reason): pin a Claude Code capture test to the opt-out "
        "(OPENSHARD_TURN_RECEIPTS=off, one Receipt per session); give the reason.",
    )


@pytest.fixture(autouse=True)
def _turn_receipts_default(request, monkeypatch):
    """Claude Code capture tests see the default: per-turn Receipts (docs/per-turn-receipts.md).

    The developer's own ``OPENSHARD_TURN_RECEIPTS`` never leaks in. A test about
    the legacy session Receipt says so with ``@pytest.mark.session_receipts("why")``;
    a test that must hold in both modes requests ``receipt_scope``.
    """
    monkeypatch.delenv("OPENSHARD_TURN_RECEIPTS", raising=False)
    if request.node.get_closest_marker("session_receipts") is not None:
        monkeypatch.setenv("OPENSHARD_TURN_RECEIPTS", "off")


@pytest.fixture(params=["turn", "session"])
def receipt_scope(request, monkeypatch) -> str:
    """Run a test once with per-turn Receipts (the default) and once opted out."""
    monkeypatch.setenv("OPENSHARD_TURN_RECEIPTS", "on" if request.param == "turn" else "off")
    return str(request.param)


@pytest.fixture(autouse=True)
def _isolate_codex_home(tmp_path_factory, monkeypatch):
    """Codex hook-trust detection never reads the developer's real ~/.codex."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex_home")))


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


@pytest.fixture(autouse=True)
def _pr_lookup_off_by_default(monkeypatch):
    """Start every test as an in-process hook, not the capture service.

    ``enable_pr_lookup()`` flips a process-global when a test starts the
    service in-process; without this reset it leaks into later tests on the
    same xdist worker, which would then run service-only git/``gh`` work.
    Tests that need the service behaviour set it explicitly.
    """
    from openshard.adapters import claude_hooks

    monkeypatch.setattr(claude_hooks, "_PR_LOOKUP_ENABLED", False)
