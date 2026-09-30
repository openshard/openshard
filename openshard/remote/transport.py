"""HTTPS calls for one remote capture, each answer classified.

Everything goes to ``<capture_url>/...`` with ``Authorization: Bearer
<osr_...>``: the capture's own short-lived token, which the Platform accepts
on these routes and nowhere else. stdlib ``urllib`` only, strict timeout.

=====================  ===========  ==========================================
call                   route        answers that matter
=====================  ===========  ==========================================
``status``             GET  ``/``   200 open; 401 token dead (expired, revoked)
``send_events``        POST events  200 stored or already held; 400/422 this
                                    batch is refused for good; 409 journal
                                    full; 401 token dead; 429/5xx/network try
                                    again later
``send``               POST receipts   the receipt-sync answers (201/200/409/4xx)
``send_evidence``      POST receipts/<id>/verification-evidence
=====================  ===========  ==========================================

``send`` and ``send_evidence`` make this a ``PlatformTransport``, so the
ordinary sync flush (``sync/client.py``) delivers the session's Receipt and
its later verification evidence through the capture, with the same outbox
and the same idempotency, and without an organisation API key in the runtime.
"""

from __future__ import annotations

import json
from typing import Any

from openshard.remote.config import Attachment
from openshard.sync.transport import (
    KIND_UNAVAILABLE,
    SendResult,
    classify_evidence_status,
    classify_status,
)

TOTAL_TIMEOUT_SECONDS = 10.0
_MAX_BODY_BYTES = 64 * 1024


class RemoteCaptureClient:
    name = "remote-capture"

    def __init__(self, attachment: Attachment, *, user_agent: str, timeout: float = TOTAL_TIMEOUT_SECONDS) -> None:
        self.base = attachment.capture_url
        self._token = attachment.token
        self.user_agent = user_agent
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None, classify: Any) -> tuple[SendResult, Any]:
        import urllib.error
        import urllib.parse
        import urllib.request

        data = None if body is None else json.dumps(
            body, separators=(",", ":"), ensure_ascii=True, default=str,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self.user_agent,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https/loopback only
                raw = response.read(_MAX_BODY_BYTES)
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace")) if raw else None
                except ValueError:
                    parsed = None
                return classify(int(response.status)), parsed
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(_MAX_BODY_BYTES)
            except Exception:
                raw = b""
            return classify(int(exc.code), raw), None
        except Exception:
            return SendResult(KIND_UNAVAILABLE, None), None

    def status(self) -> tuple[SendResult, dict | None]:
        """Whether the capture is open for this token, and what the Platform says about it."""
        result, body = self._request("GET", "", None, classify_status)
        return result, body if isinstance(body, dict) else None

    def send_events(self, batch: dict) -> SendResult:
        return self._request("POST", "/events", batch, classify_status)[0]

    # PlatformTransport / EvidenceTransport (sync/transport.py)

    def send(self, envelope: dict) -> SendResult:
        return self._request("POST", "/receipts", envelope, classify_status)[0]

    def send_evidence(self, receipt_id: str, envelope: dict) -> SendResult:
        import urllib.parse

        path = f"/receipts/{urllib.parse.quote(receipt_id, safe='')}/verification-evidence"
        return self._request("POST", path, envelope, classify_evidence_status)[0]


__all__ = ["RemoteCaptureClient", "TOTAL_TIMEOUT_SECONDS"]
