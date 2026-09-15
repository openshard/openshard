"""v0.4.4 Phase 9 -- the suite never touches a developer's real capture service.

Every capture-related code path that falls back to ``DEFAULT_PORT`` must see
the isolated per-test value from ``conftest``, never the real 47811, so a
live OpenShard service on the developer's machine cannot change a test's
outcome (or be stopped by one).
"""

from __future__ import annotations

import os

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc

REAL_DEFAULT_PORT = 47811


def test_default_port_is_isolated_for_every_test():
    assert client.DEFAULT_PORT != REAL_DEFAULT_PORT
    assert svc.DEFAULT_PORT == client.DEFAULT_PORT
    assert 0 < client.DEFAULT_PORT < 65536


def test_resolve_port_falls_back_to_the_isolated_default(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k != "OPENSHARD_CAPTURE_PORT"}
    assert client.read_state(env) is None  # fresh OPENSHARD_HOME: no state file
    assert client.resolve_port(env) == client.DEFAULT_PORT
    assert client.resolve_port(env) != REAL_DEFAULT_PORT


def test_candidate_ports_never_include_the_real_default(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k != "OPENSHARD_CAPTURE_PORT"}
    ports = svc._candidate_ports(env, None)
    assert REAL_DEFAULT_PORT not in ports
    assert ports[0] == client.DEFAULT_PORT


def test_ensure_service_with_no_spawn_never_reports_a_foreign_service(monkeypatch):
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    env = dict(os.environ)
    # Whatever is listening on the real default port on this machine, the
    # answer here depends only on the isolated port (nothing listens there).
    assert client.ensure_service(env) == (None, "unavailable")
