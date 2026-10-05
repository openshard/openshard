"""`openshard connect`: one path from nothing to a verified link and configured capture.

The command orchestrates three existing primitives and must claim no more
than each of them established: `sync.config.save_link` (the same write as
`sync connect`), `sync.capabilities.check_link` (the same authenticated read
receipt sync uses) and `adapters.claude_setup.run_setup` (the same installers
as `openshard setup`). Setup is replaced with canned results here; the
installers have their own tests.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.sync import capabilities as caps
from openshard.sync import config, transport
from tests.capture_fixtures import _make_repo

ENDPOINT = "https://platform.example.test"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
ORG_B = "11111111-2222-4333-8444-555555555555"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"


def _caps_body(org: str) -> bytes:
    return json.dumps({"organisation_id": org, "capabilities": []}).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append({"path": self.path, "headers": dict(self.headers)})
        status, body = type(self).routes.get(self.path, (404, b'{"error":{"code":"not_found"}}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def server():
    _Handler.seen = []
    _Handler.routes = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _endpoint(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _link(endpoint: str, org: str = ORG, key: str = KEY) -> config.PlatformLink:
    return config.PlatformLink(endpoint=endpoint, organisation_id=org, api_key=key, linked_at=None, source="env")


# ---------------------------------------------------------------------------
# check_link: what one authenticated read establishes
# ---------------------------------------------------------------------------


class TestCheckLink:
    def test_ok_only_when_the_platform_answers_for_this_organisation(self, server):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        check = caps.check_link(_link(_endpoint(server)))
        assert check.ok and check.kind == caps.LINK_OK and check.status == 200
        assert _Handler.seen[0]["headers"]["Authorization"] == f"Bearer {KEY}"
        assert check.to_dict() == {"ok": True, "kind": "ok", "status": 200}

    @pytest.mark.parametrize("status,kind", [
        (401, caps.LINK_UNAUTHORIZED), (403, caps.LINK_FORBIDDEN), (404, caps.LINK_NOT_FOUND),
        (429, caps.LINK_UNAVAILABLE), (500, caps.LINK_UNAVAILABLE), (503, caps.LINK_UNAVAILABLE),
    ])
    def test_refusals_are_named_and_everything_else_is_unavailable(self, server, status, kind):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (status, b'{"error":{"code":"x"}}')
        check = caps.check_link(_link(_endpoint(server)))
        assert not check.ok and check.kind == kind and check.status == status

    def test_a_body_for_another_organisation_is_not_ok(self, server):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG_B))
        check = caps.check_link(_link(_endpoint(server)))
        assert not check.ok and check.kind == caps.LINK_UNAVAILABLE and check.status == 200

    def test_offline_is_unavailable_and_never_raises(self):
        check = caps.check_link(_link(f"http://127.0.0.1:{_closed_port()}"), timeout=2.0)
        assert check.kind == caps.LINK_UNAVAILABLE and check.status is None

    def test_fetch_enabled_capabilities_still_reads_the_same_route(self, server):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, json.dumps({
            "organisation_id": ORG,
            "capabilities": [{"key": "agent_budgets", "enabled": True}],
        }).encode("utf-8"))
        assert caps.fetch_enabled_capabilities(_link(_endpoint(server))) == frozenset({"agent_budgets"})
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (401, b"{}")
        assert caps.fetch_enabled_capabilities(_link(_endpoint(server))) is None


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@dataclass
class _Agent:
    agent: str
    status: str
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return self.status in ("installed", "updated", "already_installed")

    def to_dict(self) -> dict:
        return {"agent": self.agent, "status": self.status, "configured": self.configured}


@dataclass
class _Setup:
    """The slice of claude_setup.SetupResult the command and its renderer read."""

    readiness: str
    is_git: bool = True
    history_writable: bool = True
    next_steps: list[str] = field(default_factory=list)
    claude: bool = True
    agents: dict = field(default_factory=dict)
    mcp: object = None
    hooks: object = None
    statusline: object = None
    capture_service: dict | None = None

    @property
    def claude_cli(self):
        return type("Cli", (), {"available": self.claude})()

    def configured_agents(self) -> list[str]:
        out = ["claude_code"] if self.claude else []
        out.extend(k for k, r in self.agents.items() if r.configured)
        return out

    def to_dict(self) -> dict:
        return {
            "readiness": self.readiness, "is_git": self.is_git, "next_steps": self.next_steps,
            "configured_agents": self.configured_agents(),
            "agents": {k: r.to_dict() for k, r in self.agents.items()},
        }


READY = _Setup("ready", agents={"codex": _Agent("codex", "installed")})
READY_CLAUDE_ONLY = _Setup("ready", agents={"codex": _Agent("codex", "skipped", "Codex CLI not found")})
NOT_READY = _Setup("not_ready", claude=False, next_steps=["Install the Claude Code CLI, then re-run `openshard setup`."])
NO_REPO = _Setup("not_ready", is_git=False, claude=False,
                 next_steps=["Run `openshard setup` again from inside a git repository to enable coding-agent capture for a project."])


class TestConnectCommand:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        return _make_repo(tmp_path / "widget")

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch) -> Path:
        home = tmp_path / "home"
        monkeypatch.setenv("OPENSHARD_HOME", str(home))
        for var in (config.ENDPOINT_ENV, config.ORG_ENV, config.API_KEY_ENV):
            monkeypatch.delenv(var, raising=False)
        return home

    def _run(self, args, cwd: Path, monkeypatch, *, setup=READY, **kwargs):
        from openshard.cli.main import cli

        monkeypatch.chdir(cwd)
        with patch("openshard.adapters.claude_setup.run_setup", return_value=setup) as run_setup:
            out = CliRunner().invoke(cli, args, catch_exceptions=False, **kwargs)
        out.run_setup = run_setup  # type: ignore[attr-defined]
        return out

    def test_stores_verifies_and_configures_in_one_command(self, repo, home, server, monkeypatch):
        endpoint = _endpoint(server)
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        out = self._run(["connect", "--endpoint", endpoint, "--org", ORG, "--api-key", KEY], repo, monkeypatch)
        assert out.exit_code == 0, out.output
        assert KEY not in out.output
        assert "verified" in out.output and "NOT verified" not in out.output
        assert "Connected and ready. Use Claude Code, Codex normally" in out.output
        assert "openshard last" in out.output and "sync now" in out.output
        # The same write `sync connect` makes: user-global file, mode 0600, the key stored.
        stored = config.load_link()
        assert stored is not None and stored.api_key == KEY and stored.endpoint == endpoint
        assert (home / config.CONFIG_FILENAME).exists()
        assert out.run_setup.call_args.kwargs == {"repo_path": None}
        assert _Handler.seen[0]["headers"]["Authorization"] == f"Bearer {KEY}"

    def test_prompts_for_the_key_without_echo(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG], repo, monkeypatch, input=KEY + "\n")
        assert out.exit_code == 0, out.output
        assert KEY not in out.output and config.load_link().api_key == KEY

    def test_rerun_without_arguments_reuses_the_stored_link(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        config.save_link(endpoint=_endpoint(server), organisation_id=ORG, api_key=KEY)
        before = (home / config.CONFIG_FILENAME).read_text(encoding="utf-8")
        transport.record_link_failure("unauthorized")  # an earlier bad run paused the link
        out = self._run(["connect"], repo, monkeypatch)
        assert out.exit_code == 0, out.output
        assert "already stored" in out.output and KEY not in out.output
        assert (home / config.CONFIG_FILENAME).read_text(encoding="utf-8") == before
        assert transport.in_backoff() is None  # a verified link deserves a fresh attempt

    def test_environment_link_is_used_and_never_written(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        monkeypatch.setenv(config.ENDPOINT_ENV, _endpoint(server))
        monkeypatch.setenv(config.ORG_ENV, ORG)
        monkeypatch.setenv(config.API_KEY_ENV, KEY)
        out = self._run(["connect"], repo, monkeypatch)
        assert out.exit_code == 0, out.output
        assert "environment variables" in out.output and KEY not in out.output
        assert not (home / config.CONFIG_FILENAME).exists()

    def test_nothing_stored_and_no_arguments_says_what_to_run(self, repo, home, monkeypatch):
        out = self._run(["connect"], repo, monkeypatch)
        assert out.exit_code == 2 and "--endpoint <url> --org <uuid>" in out.output
        out.run_setup.assert_not_called()

    @pytest.mark.parametrize("args", [
        ["--endpoint", ENDPOINT], ["--org", ORG], ["--api-key", KEY],
    ])
    def test_partial_link_arguments_are_rejected_before_anything_is_written(self, repo, home, monkeypatch, args):
        out = self._run(["connect", *args], repo, monkeypatch)
        assert out.exit_code == 2 and KEY not in out.output
        assert config.load_link() is None
        out.run_setup.assert_not_called()

    def test_insecure_endpoint_is_refused_like_sync_connect(self, repo, home, monkeypatch):
        out = self._run(["connect", "--endpoint", "http://platform.example.test", "--org", ORG, "--api-key", KEY],
                        repo, monkeypatch)
        assert out.exit_code == 2 and "https" in out.output and config.load_link() is None

    @pytest.mark.parametrize("status,phrase", [
        (401, "rejected the API key"), (403, "may not access"), (404, "not found at this endpoint"),
        (503, "could not be reached"),
    ])
    def test_a_rejected_link_is_reported_kept_and_recoverable(self, repo, home, server, monkeypatch, status, phrase):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (status, b"{}")
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG, "--api-key", KEY], repo, monkeypatch)
        assert out.exit_code == 1, out.output
        assert "NOT verified" in out.output and phrase in out.output and KEY not in out.output
        assert "Not connected" in out.output and "Connected and ready" not in out.output
        # Local capture was still configured and said so; the link stays for a corrected re-run.
        assert "capture is configured and Receipts are recorded locally" in out.output
        assert "openshard connect" in out.output
        assert config.load_link() is not None and config.load_link().api_key == KEY

    def test_offline_platform_is_unverified_not_rejected(self, repo, home, monkeypatch):
        endpoint = f"http://127.0.0.1:{_closed_port()}"
        with patch("openshard.sync.capabilities.TOTAL_TIMEOUT_SECONDS", 2.0):
            out = self._run(["connect", "--endpoint", endpoint, "--org", ORG, "--api-key", KEY], repo, monkeypatch)
        assert out.exit_code == 1 and "could not be reached" in out.output
        assert "is reachable from this machine" in out.output

    def test_agents_are_only_claimed_when_the_installers_configured_them(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG, "--api-key", KEY],
                        repo, monkeypatch, setup=READY_CLAUDE_ONLY)
        assert out.exit_code == 0, out.output
        assert "Codex:         not found (skipped)" in out.output
        assert "Use Claude Code normally" in out.output and "Codex normally" not in out.output

    def test_no_agent_configured_is_connected_but_not_ready(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG, "--api-key", KEY],
                        repo, monkeypatch, setup=NOT_READY)
        assert out.exit_code == 1, out.output
        assert "verified" in out.output
        assert "Connected, but no coding agent is configured" in out.output
        assert "Install the Claude Code CLI" in out.output
        assert config.load_link() is not None  # the verified link is kept

    def test_outside_a_repository_connects_and_says_where_to_run_setup(self, tmp_path, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG, "--api-key", KEY],
                        tmp_path, monkeypatch, setup=NO_REPO)
        assert out.exit_code == 1, out.output
        assert "not a git repository" in out.output and "verified" in out.output
        assert "inside a git repository" in out.output

    def test_json_envelope_carries_each_stage_and_no_secret(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG))
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG, "--api-key", KEY, "--json"],
                        repo, monkeypatch)
        assert out.exit_code == 0, out.output
        doc = json.loads(out.output)
        assert doc["command"] == "connect" and doc["status"] == "ok" and doc["connected"] is True
        assert doc["link"]["api_key_prefix"].startswith("osk_") and doc["link"]["stored_now"] is True
        assert "api_key" not in doc["link"] and KEY not in out.output
        assert doc["verification"] == {"ok": True, "kind": "ok", "status": 200}
        assert doc["setup"]["readiness"] == "ready" and doc["setup"]["configured_agents"] == ["claude_code", "codex"]
        assert any("openshard last" in s for s in doc["next_steps"])

    def test_json_envelope_when_the_link_is_rejected(self, repo, home, server, monkeypatch):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (401, b"{}")
        out = self._run(["connect", "--endpoint", _endpoint(server), "--org", ORG, "--api-key", KEY, "--json"],
                        repo, monkeypatch)
        assert out.exit_code == 1
        doc = json.loads(out.output)
        assert doc["status"] == "incomplete" and doc["connected"] is False
        assert doc["verification"] == {"ok": False, "kind": "unauthorized", "status": 401}
        assert KEY not in out.output

    def test_sync_connect_is_unchanged_and_shares_the_write(self, repo, home, monkeypatch):
        out = self._run(["sync", "connect", "--endpoint", ENDPOINT, "--org", ORG, "--api-key", KEY], repo, monkeypatch)
        assert out.exit_code == 0 and "Connected" in out.output and KEY not in out.output
        assert config.load_link().api_key == KEY
        out.run_setup.assert_not_called()  # `sync connect` never touches agent configuration

    def test_help_lists_connect_under_getting_started(self, repo, monkeypatch):
        out = self._run(["--help"], repo, monkeypatch)
        section = out.output.split("Getting Started:", 1)[1].split("Receipts:", 1)[0]
        assert "connect" in section and "setup" in section
