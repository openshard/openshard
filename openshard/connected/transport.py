"""HTTPS transport for account-connected capture."""
from __future__ import annotations

import json
from typing import Any

from openshard.connected.config import ConnectedConnection, ConnectedSession
from openshard.sync.transport import (
    KIND_UNAVAILABLE,
    SendResult,
    classify_evidence_status,
    classify_status,
)

TOTAL_TIMEOUT_SECONDS = 10.0
_MAX_BODY_BYTES = 64 * 1024


class ConnectedCaptureClient:
    name = "connected-capture"

    def __init__(
        self,
        connection: ConnectedConnection,
        session: ConnectedSession,
        *,
        user_agent: str,
        timeout: float = TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        self.connection = connection
        self.session = session
        self.base = f"{connection.endpoint}/v1/orgs/{connection.organisation_id}/connected-captures"
        self.user_agent = user_agent
        self.timeout = timeout

    def _request(self, path: str, body: dict, classify: Any) -> tuple[SendResult, Any]:
        import urllib.error
        import urllib.request

        data = json.dumps(body, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.connection.token}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
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

    def _identity(self) -> dict[str, Any]:
        return {
            "surface": self.session.surface,
            "external_session_id": self.session.external_session_id,
        }

    def send_events(self, batch: dict) -> SendResult:
        s = self.session
        body = {
            **self._identity(),
            "agent": s.agent,
            "provider": s.provider,
            "repo_identity": s.repo_identity,
            "repo": s.repo,
            "branch": s.branch,
            "source": batch.get("source"),
            "collector_id": batch.get("collector_id"),
            "events": batch.get("events", []),
            "links": batch.get("links", []),
        }
        return self._request("/events", body, classify_status)[0]

    def send(self, envelope: dict) -> SendResult:
        return self._request("/receipts", {**self._identity(), "envelope": envelope}, classify_status)[0]

    def send_evidence(self, receipt_id: str, envelope: dict) -> SendResult:
        body = {**self._identity(), "receipt_id": receipt_id, "envelope": envelope}
        return self._request("/verification-evidence", body, classify_evidence_status)[0]


__all__ = ["ConnectedCaptureClient", "TOTAL_TIMEOUT_SECONDS"]