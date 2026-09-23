"""OTLP/HTTP receiver for Cursor's Grok Bot Action Recording export (Enterprise).

Cursor's OpenTelemetry Export pushes OTLP/HTTP protobuf to ``<base>/v1/logs``
and ``<base>/v1/metrics`` on an HTTPS endpoint reachable from the public
internet, authenticated by a static header the admin configures. OpenShard
does not terminate public TLS itself. The supported deployments are:

* Cursor -> the customer's OpenTelemetry Collector (public HTTPS) ->
  ``otlphttp`` exporter -> this receiver on a private address; or
* Cursor -> the customer's collector -> ``file`` exporter -> ``openshard
  grok-bot ingest <file>`` (no listener at all).

This receiver:

* requires ``Authorization: Bearer <token>`` (compared in constant time)
  on every request; the token comes from the environment, never argv;
* accepts ``POST /v1/logs`` (protobuf or OTLP/JSON, optional gzip) and
  folds ``cursor.surface=grok_bot`` records into the repository's history;
* answers ``POST /v1/metrics`` with success and ignores the body (token
  and cost *metrics* duplicate the ``api_request`` logs it already reads);
* refuses browser-originated requests (``Origin`` / ``Sec-Fetch-Site``);
* handles one request at a time, which serializes history writes.
"""

from __future__ import annotations

import hmac
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from openshard.adapters.otlp_logs import MAX_REQUEST_BYTES, OtlpDecodeError

TOKEN_ENV = "OPENSHARD_GROK_BOT_OTLP_TOKEN"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4318
MIN_TOKEN_LENGTH = 16


class ReceiverStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.rejected = 0
        self.decode_errors = 0
        self.accepted = 0
        self.duplicates = 0
        self.skipped: dict[str, int] = {}

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "requests": self.requests, "rejected": self.rejected, "decode_errors": self.decode_errors,
                "accepted": self.accepted, "duplicates": self.duplicates, "skipped": dict(self.skipped),
            }


def make_handler(
    repo_root: Path, token: str, *, team_id: int | None = None, stats: ReceiverStats | None = None,
    on_ingest: Callable[[dict], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()
    counters = stats or ReceiverStats()

    class Handler(BaseHTTPRequestHandler):
        server_version = "openshard-grok-bot-otlp"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return  # never log request lines (paths/headers) to stderr

        def _reply(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authorized(self) -> bool:
            given = (self.headers.get("Authorization") or "").encode()
            return hmac.compare_digest(given, expected)

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if self.path == "/health":
                self._reply(200, b'{"ok":true}')
            else:
                self._reply(404)

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            with counters.lock:
                counters.requests += 1
            if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
                self._reject(403)
                return
            if not self._authorized():
                self._reject(401)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._reject(413)
                return
            body = self.rfile.read(length) if length else b""
            ctype = (self.headers.get("Content-Type") or "application/x-protobuf").lower()
            is_json = "json" in ctype
            if self.path == "/v1/metrics":
                self._ok(is_json)
                return
            if self.path != "/v1/logs":
                self._reply(404)
                return
            from openshard.adapters.grok_bot import ingest_otlp_bytes

            try:
                result = ingest_otlp_bytes(body, repo_root, content_type=ctype, team_id=team_id)
            except (OtlpDecodeError, UnicodeDecodeError):
                with counters.lock:
                    counters.decode_errors += 1
                self._reply(400, b'{"error":"undecodable OTLP logs request"}')
                return
            with counters.lock:
                counters.accepted += result.accepted
                counters.duplicates += result.duplicates
                for k, v in result.skipped.items():
                    counters.skipped[k] = counters.skipped.get(k, 0) + v
            if on_ingest is not None:
                on_ingest(result.to_dict())
            self._ok(is_json)

        def _ok(self, is_json: bool) -> None:
            # An empty Export*ServiceResponse: full success, no partial_success.
            if is_json:
                self._reply(200, b"{}")
            else:
                self._reply(200, b"", "application/x-protobuf")

        def _reject(self, code: int) -> None:
            with counters.lock:
                counters.rejected += 1
            self._reply(code, json.dumps({"error": "unauthorized" if code in (401, 403) else "rejected"}).encode())

    return Handler


def serve(
    repo_root: Path, token: str, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
    team_id: int | None = None, stats: ReceiverStats | None = None,
    ready: Callable[[HTTPServer], None] | None = None, on_ingest: Callable[[dict], None] | None = None,
) -> None:
    """Run the receiver until interrupted (single-threaded by design)."""
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"a bearer token of at least {MIN_TOKEN_LENGTH} characters is required")
    handler = make_handler(repo_root, token, team_id=team_id, stats=stats, on_ingest=on_ingest)
    server = HTTPServer((host, port), handler)
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
