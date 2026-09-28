"""Core -> Platform capability read (``openshard.sync.capabilities``).

Every test runs against a temporary ``OPENSHARD_HOME`` with an injected
fetcher or a throw-away loopback HTTP server. The invariant under test:
a capability is on only when the Platform said so for *this* link, and
everything else -- no link, refusal, outage, bad body, foreign or stale
cache -- is off.
"""

from __future__ import annotations

import json
import socket
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from openshard.sync import capabilities as caps
from openshard.sync import config

ENDPOINT = "https://platform.example.test"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
ORG_B = "11111111-2222-4333-8444-555555555555"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
KEY_B = "osk_zzzzZZZZ_0123456789abcdefghijklmnopqrstuvwxyz"
NOW = 1_800_000_000.0


def _body(org: str, keys: list[str], *, enabled: bool = True) -> bytes:
    return json.dumps({
        "organisation_id": org,
        "capabilities": [
            {"key": k, "name": k, "description": "", "stage": "internal", "enabled": enabled,
             "enabled_at": "2026-09-28T10:00:00.000Z"}
            for k in keys
        ],
    }).encode("utf-8")


def _env(tmp_path: Path, org: str = ORG, key: str = KEY, endpoint: str = ENDPOINT) -> dict:
    return {
        "OPENSHARD_HOME": str(tmp_path / "home"),
        config.ENDPOINT_ENV: endpoint,
        config.ORG_ENV: org,
        config.API_KEY_ENV: key,
    }


def _link(org: str = ORG, key: str = KEY, endpoint: str = ENDPOINT) -> config.PlatformLink:
    return config.PlatformLink(endpoint=endpoint, organisation_id=org, api_key=key, linked_at=None, source="env")


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    seen: list[dict] = []
    redirect_to: str | None = None

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append({"path": self.path, "headers": dict(self.headers)})
        status, body = type(self).routes.get(self.path, (404, b'{"error":{"code":"not_found"}}'))
        self.send_response(status)
        if status in (301, 302, 307, 308) and type(self).redirect_to:
            self.send_header("Location", type(self).redirect_to)
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
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
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


class TestFetch:
    def test_reads_own_org_route_with_bearer_key_and_parses_the_contract(self, server):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _body(ORG, ["agent_budgets", "adaptive_routing"]))
        keys = caps.fetch_enabled_capabilities(_link(endpoint=_endpoint(server)), user_agent="openshard/test", timeout=5.0)
        assert keys == frozenset({"agent_budgets", "adaptive_routing"})
        req = _Handler.seen[-1]
        assert req["path"] == f"/v1/orgs/{ORG}/capabilities"
        assert req["headers"]["Authorization"] == f"Bearer {KEY}"
        assert req["headers"]["Accept"] == "application/json"
        assert req["headers"]["User-Agent"] == "openshard/test"

    def test_empty_list_is_a_normal_answer_meaning_nothing_is_on(self, server):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _body(ORG, []))
        assert caps.fetch_enabled_capabilities(_link(endpoint=_endpoint(server)), timeout=5.0) == frozenset()

    @pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
    def test_any_refusal_or_outage_is_unknown(self, server, status):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (status, b'{"error":{"code":"x"}}')
        assert caps.fetch_enabled_capabilities(_link(endpoint=_endpoint(server)), timeout=5.0) is None

    def test_offline_endpoint_is_unknown_quickly(self):
        link = _link(endpoint=f"http://127.0.0.1:{_closed_port()}")
        assert caps.fetch_enabled_capabilities(link, timeout=1.0) is None

    def test_a_body_for_another_organisation_is_refused(self, server):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _body(ORG_B, ["agent_budgets"]))
        assert caps.fetch_enabled_capabilities(_link(endpoint=_endpoint(server)), timeout=5.0) is None

    @pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"capabilities": "x"}', b'{"organisation_id": 1}'])
    def test_malformed_bodies_are_unknown(self, server, body):
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, body)
        assert caps.fetch_enabled_capabilities(_link(endpoint=_endpoint(server)), timeout=5.0) is None

    def test_disabled_and_malformed_items_are_dropped(self):
        body = json.dumps({"organisation_id": ORG.upper(), "capabilities": [
            {"key": "agent_budgets", "enabled": True},
            {"key": "adaptive_routing", "enabled": False},
            {"key": "supervisor_routing"},  # no flag: not proven on
            {"key": "Not-A-Key", "enabled": True},
            {"key": "x" * 65, "enabled": True},
            "junk", {"enabled": True},
        ]}).encode()
        assert caps._parse_keys(body, organisation_id=ORG) == frozenset({"agent_budgets"})


class TestResolve:
    def test_no_link_is_off_and_never_fetches(self, tmp_path):
        def fetcher(_link):
            raise AssertionError("must not fetch without a link")

        state = caps.resolve_capabilities({"OPENSHARD_HOME": str(tmp_path / "home")}, fetcher=fetcher, now=NOW)
        assert state.source == "unavailable" and state.reason == "no_platform_link"
        assert state.enabled("agent_budgets") is False
        assert not caps.cache_path({"OPENSHARD_HOME": str(tmp_path / "home")}).exists()

    def test_fetch_failure_is_off_and_remembered_briefly(self, tmp_path):
        env = _env(tmp_path)
        calls: list[int] = []

        def failing(_link):
            calls.append(1)
            return None

        state = caps.resolve_capabilities(env, fetcher=failing, now=NOW)
        assert state.source == "unavailable" and state.reason == "platform_unreachable_or_refused"
        assert state.enabled("agent_budgets") is False
        # Inside the negative TTL the failure is served from the cache: no second timeout.
        again = caps.resolve_capabilities(env, fetcher=failing, now=NOW + 59.0)
        assert again.source == "unavailable" and calls == [1]
        assert json.loads(caps.cache_path(env).read_text())["keys"] is None
        # After it, the Platform is asked again and a good answer replaces the failure.
        ok = caps.resolve_capabilities(env, fetcher=lambda _l: frozenset({"agent_budgets"}), now=NOW + 60.0)
        assert ok.source == "fresh" and ok.enabled("agent_budgets")

    def test_fetcher_exception_is_off(self, tmp_path):
        def fetcher(_link):
            raise RuntimeError("boom")

        assert caps.resolve_capabilities(_env(tmp_path), fetcher=fetcher, now=NOW).enabled("agent_budgets") is False

    def test_fresh_answer_is_cached_privately_and_served_inside_the_ttl(self, tmp_path):
        env = _env(tmp_path)
        calls: list[str] = []

        def fetcher(link):
            calls.append(link.organisation_id)
            return frozenset({"agent_budgets"})

        first = caps.resolve_capabilities(env, fetcher=fetcher, now=NOW)
        assert first.source == "fresh" and first.enabled("agent_budgets")
        path = caps.cache_path(env)
        assert path.exists()
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        stored = json.loads(path.read_text())
        assert stored["organisation_id"] == ORG and stored["endpoint"] == ENDPOINT
        assert stored["key_prefix"] == _link().key_prefix and KEY not in path.read_text()

        second = caps.resolve_capabilities(env, fetcher=fetcher, now=NOW + 599.0)
        assert second.source == "cache" and second.enabled("agent_budgets")
        assert calls == [ORG]

        third = caps.resolve_capabilities(env, fetcher=fetcher, now=NOW + 600.0)
        assert third.source == "fresh" and calls == [ORG, ORG]

    def test_an_expired_cache_is_not_served_when_the_platform_is_down(self, tmp_path):
        env = _env(tmp_path)
        caps.resolve_capabilities(env, fetcher=lambda _l: frozenset({"agent_budgets"}), now=NOW)
        later = caps.resolve_capabilities(env, fetcher=lambda _l: None, now=NOW + 601.0)
        assert later.source == "unavailable" and later.enabled("agent_budgets") is False

    def test_a_cache_from_the_future_or_another_schema_is_ignored(self, tmp_path):
        env = _env(tmp_path)
        caps.resolve_capabilities(env, fetcher=lambda _l: frozenset({"agent_budgets"}), now=NOW)
        path = caps.cache_path(env)
        data = json.loads(path.read_text())
        data["fetched_at"] = NOW + 10.0
        path.write_text(json.dumps(data))
        assert caps.resolve_capabilities(env, fetcher=lambda _l: None, now=NOW).enabled("agent_budgets") is False
        data["fetched_at"], data["schema_version"] = NOW, 99
        path.write_text(json.dumps(data))
        assert caps.resolve_capabilities(env, fetcher=lambda _l: None, now=NOW).enabled("agent_budgets") is False

    def test_one_organisations_cache_never_answers_for_another(self, tmp_path):
        env_a = _env(tmp_path)
        assert caps.resolve_capabilities(env_a, fetcher=lambda _l: frozenset({"agent_budgets"}), now=NOW).enabled("agent_budgets")

        asked: list[str] = []

        def refusing_fetcher(link):
            asked.append(link.organisation_id)
            return None  # the Platform answers 403 for a key on another org's path

        env_b = _env(tmp_path, org=ORG_B, key=KEY_B)
        state_b = caps.resolve_capabilities(env_b, fetcher=refusing_fetcher, now=NOW)
        assert asked == [ORG_B]  # Core asked about B, never reused A
        assert state_b.source == "unavailable" and state_b.enabled("agent_budgets") is False

        # Same organisation, rotated key: the old answer is not trusted either.
        env_rotated = _env(tmp_path, key=KEY_B)
        state_r = caps.resolve_capabilities(env_rotated, fetcher=refusing_fetcher, now=NOW)
        assert asked == [ORG_B, ORG] and state_r.enabled("agent_budgets") is False

        # Same organisation, different endpoint: also a fresh question.
        env_ep = _env(tmp_path, endpoint="https://other.example.test")
        caps.resolve_capabilities(env_ep, fetcher=refusing_fetcher, now=NOW)
        assert asked == [ORG_B, ORG, ORG]

    def test_end_to_end_against_loopback_platform_is_org_scoped(self, server, tmp_path):
        ep = _endpoint(server)
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _body(ORG, ["agent_budgets"]))
        _Handler.routes[f"/v1/orgs/{ORG_B}/capabilities"] = (403, b'{"error":{"code":"forbidden"}}')

        state_a = caps.resolve_capabilities(_env(tmp_path, endpoint=ep), now=NOW)
        assert state_a.source == "fresh" and state_a.enabled("agent_budgets")

        state_b = caps.resolve_capabilities(_env(tmp_path, org=ORG_B, key=KEY_B, endpoint=ep), now=NOW)
        assert state_b.enabled("agent_budgets") is False
        assert _Handler.seen[-1]["path"] == f"/v1/orgs/{ORG_B}/capabilities"
        assert _Handler.seen[-1]["headers"]["Authorization"] == f"Bearer {KEY_B}"

    def test_an_edited_cache_is_ignored_because_it_is_signed_with_the_key(self, tmp_path):
        env = _env(tmp_path)
        caps.resolve_capabilities(env, fetcher=lambda _l: frozenset(), now=NOW)
        path = caps.cache_path(env)
        data = json.loads(path.read_text())
        assert "signature" in data and KEY not in path.read_text()
        data["keys"] = ["agent_budgets"]  # hand-edited grant
        path.write_text(json.dumps(data))
        state = caps.resolve_capabilities(env, fetcher=lambda _l: None, now=NOW + 1.0)
        assert state.enabled("agent_budgets") is False and state.source == "unavailable"

    def test_the_sync_kill_switch_stops_the_read(self, tmp_path):
        env = _env(tmp_path)
        env["OPENSHARD_PLATFORM_SYNC"] = "off"

        def fetcher(_link):
            raise AssertionError("must not fetch when Platform traffic is switched off")

        state = caps.resolve_capabilities(env, fetcher=fetcher, now=NOW)
        assert state.source == "unavailable" and state.reason == "platform_sync_disabled"
        assert state.enabled("agent_budgets") is False

    def test_a_redirect_is_not_followed_with_the_key(self, server):
        ep = _endpoint(server)
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (302, b"")
        _Handler.redirect_to = f"{ep}/elsewhere"
        _Handler.routes["/elsewhere"] = (200, _body(ORG, ["agent_budgets"]))
        try:
            assert caps.fetch_enabled_capabilities(_link(endpoint=ep), timeout=5.0) is None
            assert [r["path"] for r in _Handler.seen] == [f"/v1/orgs/{ORG}/capabilities"]
        finally:
            _Handler.redirect_to = None

    def test_capability_enabled_helper(self, tmp_path):
        env = _env(tmp_path)
        assert caps.capability_enabled("agent_budgets", env, fetcher=lambda _l: frozenset({"agent_budgets"}), now=NOW)
        assert not caps.capability_enabled("adaptive_routing", env, now=NOW)  # served from cache: not listed
