"""`openshard login`: the RFC 8628-shaped device flow against a local fake Platform.

The fake implements the Platform contract (``packages/contracts/src/device-auth.ts``
and ``services/api/src/routes/device.ts``): ``POST /v1/auth/device/code``,
``POST /v1/auth/device/token`` answering 400 ``{"error": ...}`` until a
scripted outcome, and the capabilities read `connect` verifies the link
with. Sleeping runs on a fake clock, so no test waits in real time.
Agent setup is replaced with the canned results `test_connect_cmd` uses.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.sync import config, device_auth
from tests.capture_fixtures import _make_repo
from tests.test_connect_cmd import NOT_READY, READY, _closed_port

ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
DEVICE_CODE = "dc_SECRET_device_code_value_0123456789"
USER_CODE = "ABCD-EFGH"


class _Platform(BaseHTTPRequestHandler):
    token_script: list[tuple[int, dict]] = []
    code_response: tuple[int, dict] | None = None
    caps_status = 200
    seen: list[tuple[str, dict]] = []

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        cls = type(self)
        cls.seen.append((self.path, body))
        if self.path == "/v1/auth/device/code":
            web = f"http://127.0.0.1:{self.server.server_address[1]}"
            self._send(*(cls.code_response or (200, {
                "device_code": DEVICE_CODE, "user_code": USER_CODE,
                "verification_uri": f"{web}/device",
                "verification_uri_complete": f"{web}/device?code={USER_CODE}",
                "expires_in": 600, "interval": 5,
            })))
        elif self.path == "/v1/auth/device/token":
            self._send(*(cls.token_script.pop(0) if cls.token_script else (400, {"error": "authorization_pending"})))
        else:
            self._send(404, {"error": {"code": "not_found", "message": "no"}})

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, {}))
        self._send(type(self).caps_status, {"organisation_id": ORG, "capabilities": []})

    def log_message(self, *_args):
        return


@pytest.fixture
def platform():
    _Platform.token_script, _Platform.code_response, _Platform.caps_status, _Platform.seen = [], None, 200, []
    httpd = HTTPServer(("127.0.0.1", 0), _Platform)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(device_auth, "_sleep", c.sleep)
    monkeypatch.setattr(device_auth, "_clock", c)
    return c


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home dir with spaces" / ".openshard"  # Windows-style profile paths have spaces
    monkeypatch.setenv("OPENSHARD_HOME", str(home))
    for var in (config.ENDPOINT_ENV, config.ORG_ENV, config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "widget")


def _approved() -> tuple[int, dict]:
    return 200, {"api_key": KEY, "org_id": ORG, "org_name": "Acme"}


def _login(args, repo: Path, monkeypatch, *, setup=READY, opened: list | None = None):
    from openshard.cli.main import cli

    monkeypatch.chdir(repo)
    sink = opened if opened is not None else []
    with patch("openshard.adapters.claude_setup.run_setup", return_value=setup), \
         patch("openshard.cli.login_cmd.webbrowser.open", side_effect=lambda url, new=0: sink.append(url) or True):
        return CliRunner().invoke(cli, ["login", *args], catch_exceptions=False)


def _token_polls() -> int:
    return sum(1 for path, _ in _Platform.seen if path == "/v1/auth/device/token")


class TestLoginHappyPath:
    def test_approve_stores_the_link_verifies_and_sets_up_capture(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [(400, {"error": "authorization_pending"}), _approved()]
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 0, out.output
        assert f"Contacting Openshard Platform at {platform}" in out.output
        assert USER_CODE in out.output and f"/device?code={USER_CODE}" in out.output
        assert "Approved for Acme" in out.output and "verified" in out.output
        assert "Connected and ready" in out.output
        # Secrets never reach the terminal.
        assert KEY not in out.output and DEVICE_CODE not in out.output
        # Stored with the same write as `connect`: user-global file, never in the repository.
        link = config.load_link()
        assert link is not None and link.api_key == KEY and link.organisation_id == ORG and link.endpoint == platform
        path = home / config.CONFIG_FILENAME
        assert path.exists() and not list(repo.rglob(config.CONFIG_FILENAME))
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        # The contract's request bodies, and RFC 8628 pacing: wait `interval` before every poll.
        code_req = next(body for p, body in _Platform.seen if p == "/v1/auth/device/code")
        assert code_req["client_name"] == "Openshard CLI" and "client_version" in code_req
        assert all(body == {"device_code": DEVICE_CODE} for p, body in _Platform.seen if p.endswith("/token"))
        assert clock.sleeps == [5, 5]
        assert any(p.endswith(f"/v1/orgs/{ORG}/capabilities") for p, _ in _Platform.seen)

    def test_opens_the_browser_only_when_allowed(self, platform, clock, home, repo, monkeypatch):
        monkeypatch.setattr("openshard.cli.login_cmd._has_display", lambda: True)
        opened: list[str] = []
        _Platform.token_script = [_approved()]
        out = _login(["--endpoint", platform], repo, monkeypatch, opened=opened)
        assert out.exit_code == 0, out.output
        assert opened == [f"{platform}/device?code={USER_CODE}"] and "Opened your browser" in out.output

        opened.clear()
        _Platform.token_script = [_approved()]
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch, opened=opened)
        assert opened == [] and f"/device?code={USER_CODE}" in out.output

        monkeypatch.setattr("openshard.cli.login_cmd._has_display", lambda: False)
        _Platform.token_script = [_approved()]
        out = _login(["--endpoint", platform], repo, monkeypatch, opened=opened)
        assert opened == [] and "Open the URL above" in out.output

    def test_json_output_is_one_document_without_secrets(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [_approved()]
        out = _login(["--endpoint", platform, "--no-browser", "--json"], repo, monkeypatch)
        assert out.exit_code == 0
        doc = json.loads(out.stdout)  # the human prompt went to stderr
        assert doc["command"] == "login" and doc["status"] == "ok" and doc["connected"] is True
        assert doc["organisation_name"] == "Acme" and doc["device"]["user_code"] == USER_CODE
        assert doc["link"]["organisation_id"] == ORG and doc["link"]["stored_now"] is True
        assert USER_CODE in out.stderr
        assert KEY not in out.output and DEVICE_CODE not in out.output and "api_key" not in doc["link"]

    def test_unverified_link_after_login_is_reported_not_hidden(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [_approved()]
        _Platform.caps_status = 401
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch, setup=NOT_READY)
        assert out.exit_code == 1 and "NOT verified" in out.output and KEY not in out.output


class TestLoginPacingAndFailures:
    def test_slow_down_adds_five_seconds_to_every_later_wait(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [
            (400, {"error": "slow_down"}), (400, {"error": "authorization_pending"}), _approved(),
        ]
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 0, out.output
        assert clock.sleeps == [5, 10, 10]

    def test_expired_token_stops_with_the_next_command(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [(400, {"error": "expired_token"})]
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 1 and "expired" in out.output and "Run `openshard login` again" in out.output
        assert config.load_link() is None

    def test_code_lifetime_ends_the_wait(self, platform, clock, home, repo, monkeypatch):
        web = platform
        _Platform.code_response = (200, {
            "device_code": DEVICE_CODE, "user_code": USER_CODE, "verification_uri": f"{web}/device",
            "verification_uri_complete": f"{web}/device?code={USER_CODE}", "expires_in": 30, "interval": 5,
        })
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)  # pending forever
        assert out.exit_code == 1 and "expired before it was approved" in out.output
        assert sum(clock.sleeps) == pytest.approx(30) and _token_polls() == 6
        assert config.load_link() is None

    def test_denied_stops_and_stores_nothing(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [(400, {"error": "access_denied"})]
        out = _login(["--endpoint", platform, "--no-browser", "--json"], repo, monkeypatch)
        assert out.exit_code == 1
        doc = json.loads(out.stdout)
        assert doc["command"] == "login" and doc["status"] == "denied" and doc["connected"] is False
        assert "openshard login" in doc["next_steps"][0]
        assert config.load_link() is None and not (home / config.CONFIG_FILENAME).exists()

    def test_unreachable_platform_is_a_plain_message(self, clock, home, repo, monkeypatch):
        endpoint = f"http://127.0.0.1:{_closed_port()}"
        out = _login(["--endpoint", endpoint, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 1 and f"Could not reach the Platform at {endpoint}" in out.output
        assert f"Check that {endpoint} is reachable" in out.output and "Traceback" not in out.output

    def test_server_errors_while_waiting_back_off_then_recover(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [(503, {"error": {"code": "service_unavailable", "message": "busy"}}), _approved()]
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 0, out.output
        assert clock.sleeps == [5, 10]

    def test_persistent_server_errors_give_up_with_a_network_message(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [(502, {})] * device_auth.MAX_CONSECUTIVE_NETWORK_FAILURES
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 1 and "Lost contact with the Platform" in out.output

    def test_code_request_refusal_shows_the_platform_message(self, platform, clock, home, repo, monkeypatch):
        _Platform.code_response = (503, {"error": {"code": "service_unavailable",
                                                   "message": "Too many sign-in requests are waiting."}})
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 1 and "Too many sign-in requests are waiting." in out.output

    def test_ctrl_c_cancels_cleanly(self, platform, home, repo, monkeypatch):
        def interrupt(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(device_auth, "_sleep", interrupt)
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 130 and "Cancelled" in out.output and "Traceback" not in out.output
        assert config.load_link() is None

    def test_plain_http_to_a_remote_host_is_refused_before_contact(self, home, repo, monkeypatch):
        out = _login(["--endpoint", "http://platform.example.test"], repo, monkeypatch)
        assert out.exit_code == 2 and "https://" in out.output

    def test_an_approval_without_a_usable_key_stores_nothing(self, platform, clock, home, repo, monkeypatch):
        _Platform.token_script = [(200, {"api_key": "not-a-key", "org_id": ORG, "org_name": "Acme"})]
        out = _login(["--endpoint", platform, "--no-browser"], repo, monkeypatch)
        assert out.exit_code == 1 and "no usable key" in out.output and config.load_link() is None


def test_default_endpoint_is_the_linked_one_then_the_hosted_platform(home):
    from openshard.cli import login_cmd

    assert login_cmd._default_endpoint() == "https://api.openshard.dev"
    config.save_link(endpoint="https://platform.example.test", organisation_id=ORG, api_key=KEY)
    assert login_cmd._default_endpoint() == "https://platform.example.test"


def test_help_lists_login_first_under_getting_started(repo, monkeypatch):
    from openshard.cli.main import cli

    out = CliRunner().invoke(cli, ["--help"])
    section = out.output.split("Getting Started:", 1)[1].split("Receipts:", 1)[0]
    assert section.split()[0] == "login" and "connect" in section
