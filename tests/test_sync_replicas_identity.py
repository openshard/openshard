"""Receipt sync from a Replicas workspace with its identity token: no API key anywhere."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from openshard.sync import config as cfg
from openshard.sync import transport as tr

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2FwaS5yZXBsaWNhcy5kZXYifQ.c2lnbmF0dXJl"
WORKSPACE = {"REPLICAS_WORKSPACE_ID": "5d0cde5e-3464-48f3-b2fd-58d0019eac25"}


def _replicas_cli(tmp_path: Path, output: str = TOKEN, code: int = 0) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "replicas.log"
    script = bindir / "replicas"
    script.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nprintf "%s" "{output}"\nexit {code}\n')
    script.chmod(0o755)
    return str(bindir)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", **extra}


def test_no_link_without_the_explicit_opt_in(tmp_path):
    assert cfg.resolve_link(_env(tmp_path, **WORKSPACE)) is None
    assert cfg.resolve_link(_env(tmp_path, OPENSHARD_PLATFORM_AUTH="replicas")) is None  # not in a Replicas workspace


def test_the_replicas_link_has_no_secret_and_targets_the_replicas_routes(tmp_path):
    link = cfg.resolve_link(_env(tmp_path, OPENSHARD_PLATFORM_AUTH="replicas", **WORKSPACE))
    assert link is not None and link.source == cfg.SOURCE_REPLICAS
    assert (link.api_key, link.organisation_id) == ("", "")
    assert link.ingest_url() == "https://api.openshard.dev/v1/cloud/replicas/receipts"
    local = cfg.resolve_link(_env(tmp_path, OPENSHARD_PLATFORM_AUTH="replicas",
                                  OPENSHARD_PLATFORM_ENDPOINT="http://127.0.0.1:8787", **WORKSPACE))
    assert local is not None and local.ingest_url() == "http://127.0.0.1:8787/v1/cloud/replicas/receipts"


def test_an_explicit_key_still_wins(tmp_path):
    env = _env(tmp_path, OPENSHARD_PLATFORM_AUTH="replicas", **WORKSPACE,
               OPENSHARD_PLATFORM_ENDPOINT="https://api.openshard.dev",
               OPENSHARD_PLATFORM_ORG_ID="0190d3a2-1c3e-7000-8b1a-0242ac120002",
               OPENSHARD_PLATFORM_API_KEY="osk_" + "a" * 32)
    link = cfg.resolve_link(env)
    assert link is not None and link.source == cfg.SOURCE_ENV


def test_the_token_comes_from_the_workspace_cli_for_the_openshard_audience(tmp_path):
    env = _env(tmp_path, PATH=_replicas_cli(tmp_path))
    assert tr.replicas_identity_token(env) == TOKEN
    assert (tmp_path / "replicas.log").read_text().strip() == "identity token --audience openshard-capture"


@pytest.mark.parametrize(("output", "code"), [("Not logged in", 1), ("not a jwt", 0), ("a.b.c d", 0), ("", 0)])
def test_no_usable_token_is_none_never_an_error_string(tmp_path, output, code):
    assert tr.replicas_identity_token(_env(tmp_path, PATH=_replicas_cli(tmp_path, output, code))) is None


def test_without_the_cli_there_is_no_token(tmp_path):
    assert tr.replicas_identity_token(_env(tmp_path)) is None


class _Recorder(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None, dict]] = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Recorder.seen.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(201)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Recorder.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_receipts_and_evidence_go_to_the_replicas_routes_with_a_fresh_token(tmp_path, monkeypatch, server):
    env = _env(tmp_path, OPENSHARD_PLATFORM_AUTH="replicas", OPENSHARD_PLATFORM_ENDPOINT=server, **WORKSPACE,
               PATH=_replicas_cli(tmp_path))
    monkeypatch.setenv("PATH", env["PATH"])
    link = cfg.resolve_link(env)
    assert link is not None
    sender = tr.HttpsPlatformTransport(link, user_agent="openshard-test")
    assert sender.send({"contract": "openshard.receipt-sync"}).accepted
    assert sender.send_evidence("rcpt_1", {"x": 1}).kind == tr.KIND_CREATED
    assert [(p, a) for p, a, _ in _Recorder.seen] == [
        ("/v1/cloud/replicas/receipts", f"Bearer {TOKEN}"),
        ("/v1/cloud/replicas/receipts/rcpt_1/verification-evidence", f"Bearer {TOKEN}"),
    ]
    assert len((tmp_path / "replicas.log").read_text().splitlines()) == 2  # one token per request
    home = tmp_path / "home"
    assert not home.exists() or all(TOKEN not in p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file())


def test_no_token_means_nothing_is_sent(tmp_path, monkeypatch, server):
    env = _env(tmp_path, OPENSHARD_PLATFORM_AUTH="replicas", OPENSHARD_PLATFORM_ENDPOINT=server, **WORKSPACE)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    link = cfg.resolve_link(env)
    assert link is not None
    result = tr.HttpsPlatformTransport(link, user_agent="openshard-test").send({"contract": "x"})
    assert result.kind == tr.KIND_UNAVAILABLE and _Recorder.seen == []
