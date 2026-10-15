"""Cloud sessions that start in a restored container, and in a parent of several checkouts."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_workspace as ws
from openshard.cli.main import cli
from tests.capture_fixtures import _make_repo


def _install(repo_or_workspace: Path, *extra: str) -> dict:
    result = CliRunner().invoke(
        cli, ["capture", "install", "claude", "--defer-service", "--json", *extra], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def parent(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    root = tmp_path / "user"
    root.mkdir()
    for name in ("core", "platform"):
        _make_repo(root / name)
    return root


def _is_workspace_warning(payload: dict) -> bool:
    return any("--workspace" in w for w in payload.get("warnings", []))


def test_per_checkout_install_warns_that_a_parent_session_will_not_fire_it(parent):
    payload = _install(parent / "core", "--repo-path", str(parent / "core"))
    assert payload["configured"]
    assert _is_workspace_warning(payload)
    assert str(parent.resolve()) in " ".join(payload["warnings"])


def test_per_checkout_install_is_quiet_once_the_parent_is_a_workspace(parent):
    assert ws.install_workspace(parent, port=47999)["status"] == "installed"
    payload = _install(parent / "core", "--repo-path", str(parent / "core"))
    assert not _is_workspace_warning(payload)


def test_per_checkout_install_is_quiet_for_a_single_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    repo = _make_repo(tmp_path / "only" / "core")
    assert not _is_workspace_warning(_install(repo, "--repo-path", str(repo)))


def test_workspace_install_does_not_warn(parent):
    payload = _install(parent, "--workspace", str(parent))
    assert payload["status"] == "installed" and not _is_workspace_warning(payload)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_restored_container_starts_the_service_from_the_first_hook(tmp_path, monkeypatch):
    """The container image keeps ~/.openshard (state naming a dead pid, a start lock) but not the process."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OPENSHARD_HOME", str(home))
    monkeypatch.delenv("OPENSHARD_CAPTURE_NO_SPAWN", raising=False)
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    port = _free_port()
    monkeypatch.setenv("OPENSHARD_CAPTURE_PORT", str(port))
    (home / "claude-capture.json").write_text(json.dumps({
        "service": "openshard-claude-capture", "port": port, "pid": 2 ** 22 - 3, "instance_id": "dead",
        "started_at": "2026-10-09T16:42:07Z", "version": "0.4.14",
    }), encoding="utf-8")
    (home / "claude-capture.startlock").write_bytes(b"")
    assert client.health(port) is None

    started: int | None = None
    try:
        started, how = client.ensure_service(wait_seconds=20)
        assert how in ("started", "running") and started == port
        deadline = time.monotonic() + 10
        while client.health(port) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert client.health(port) is not None
    finally:
        if started is not None:
            client.request_shutdown()
