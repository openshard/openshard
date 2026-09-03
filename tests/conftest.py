"""Shared pytest fixtures for the OpenShard test suite."""
from __future__ import annotations

from unittest.mock import patch

import pytest


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
