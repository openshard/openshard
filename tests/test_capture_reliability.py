"""Regression coverage for Claude capture self-healing.

The direct HTTP hook remains the fast path.  The async command watchdog is
only a safety net: it must do nothing when the HTTP path is healthy, and it
must recover the event when the service/config cannot be trusted.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from openshard.adapters import capture_auth
from openshard.adapters import claude_capture_client as client
from openshard.adapters.claude_hooks_install import (
    HTTP_EVENTS,
    WATCHDOG_COMMAND,
    build_hook_config,
    capability_state,
    installed_hook_port,
)
from tests.capture_fixtures import _payload


def _write_claude_settings(repo: Path, port: int, env: dict, *, capability: str | None = None) -> Path:
    token = capture_auth.load_token(env) or capture_auth.ensure_token(env)
    assert token is not None
    if capability is None:
        capability = capture_auth.repo_capability(token, repo, "claude_code")
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hooks": build_hook_config(port, capability=capability)}),
        encoding="utf-8",
    )
    return path


def test_watchdog_is_noop_when_http_path_is_healthy(service, capture_env, repo):
    env = {**capture_env, "CLAUDE_PROJECT_DIR": str(repo)}
    _write_claude_settings(repo, service.port, env)

    with patch.object(client, "_run_hook_raw", side_effect=AssertionError("must not duplicate a healthy HTTP event")):
        label = client.run_claude_watchdog(
            io.BytesIO(_payload("PostToolUse", repo, tool_name="Bash", tool_input={"command": "echo ok"})),
            env=env,
        )

    assert label == "healthy"


def test_watchdog_recovers_when_service_is_unavailable(capture_env, repo):
    env = {**capture_env, "CLAUDE_PROJECT_DIR": str(repo)}
    _write_claude_settings(repo, client.resolve_port(env), env)

    with patch.object(client, "health", return_value=None), \
         patch.object(client, "_run_hook_raw", return_value="record_created") as recover:
        label = client.run_claude_watchdog(
            io.BytesIO(_payload("UserPromptSubmit", repo, prompt="keep this event")),
            env=env,
        )

    assert label == "record_created"
    recover.assert_called_once()


def test_watchdog_recovers_when_http_hook_points_at_stale_port(service, capture_env, repo):
    env = {**capture_env, "CLAUDE_PROJECT_DIR": str(repo)}
    stale_port = service.port + 1 if service.port < 65535 else service.port - 1
    _write_claude_settings(repo, stale_port, env)

    with patch.object(client, "_run_hook_raw", return_value="forwarded") as recover, \
         patch.object(client, "_heal_claude_hook_config") as heal:
        label = client.run_claude_watchdog(
            io.BytesIO(_payload("Stop", repo)),
            env=env,
        )

    assert label == "forwarded"
    recover.assert_called_once()
    heal.assert_called_once()


def test_watchdog_recovers_when_http_capability_is_stale(service, capture_env, repo):
    env = {**capture_env, "CLAUDE_PROJECT_DIR": str(repo)}
    _write_claude_settings(repo, service.port, env, capability="stale-capability")

    with patch.object(client, "_run_hook_raw", return_value="forwarded") as recover:
        label = client.run_claude_watchdog(
            io.BytesIO(_payload("PostToolUseFailure", repo, tool_name="Bash", tool_input={"command": "false"})),
            env=env,
        )

    assert label == "forwarded"
    recover.assert_called_once()


def test_session_start_heals_stale_port_for_next_session(service, capture_env, repo):
    env = {**capture_env, "CLAUDE_PROJECT_DIR": str(repo)}
    stale_port = service.port + 1 if service.port < 65535 else service.port - 1
    path = _write_claude_settings(repo, stale_port, env)

    client._heal_claude_hook_config(
        _payload("SessionStart", repo, source="startup"),
        env,
        desired_port=service.port,
    )

    settings = json.loads(path.read_text(encoding="utf-8"))
    assert installed_hook_port(settings) == service.port
    assert capability_state(settings, repo, env=env) == "ok"
    for event in HTTP_EVENTS:
        hooks = settings["hooks"][event][0]["hooks"]
        assert hooks[0]["type"] == "http"
        assert hooks[1]["command"] == WATCHDOG_COMMAND
        assert hooks[1]["async"] is True
