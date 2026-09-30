"""Authenticated transport for the Platform's account-connected capture route."""

from __future__ import annotations

import json
from typing import Any

from openshard.sync.config import PlatformLink
from openshard.sync.transport import KIND_UNAVAILABLE, SendResult, classify_status

TOTAL_TIMEOUT_SECONDS = 10.0
_MAX_BODY_BYTES = 64 * 1024


class ConnectedCaptureClient:
    """Send one external host session through an already-linked Platform account."""

    name = "connected-capture"

    def __init__(
        self,
        link: PlatformLink,
        *,
        surface: str,
        external_session_id: str,
        agent: str,
        provider: str | None,
        repo_identity: str | None,
        repo: str | None,
        branch: str | None,
        user_agent: str,
        timeout: float = TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        self.link = link
        self.surface = surface
        self.external_session_id = external_session_id
        self.agent = agent
        self.provider = provider
        self.repo_identity = repo_identity
        self.repo = repo
        self.branch = branch
        self.user_agent = user_agent
        self.timeout = timeout

    def send_events(self, batch: dict[str, Any]) -> SendResult:
        import urllib.error
        import urllib.request

        body = {
            "surface": self.surface,
            "external_session_id": self.external_session_id,
            "agent": self.agent,
            "provider": self.provider,
            "repo_identity": self.repo_identity,
            "repo": self.repo,
            "branch": self.branch,
            "source": batch.get("source"),
            "collector_id": batch.get("collector_id"),
            "events": batch.get("events") or [],
            "links": batch.get("links") or [],
        }
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.link.connected_capture_url(),
            data=data,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.link.api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - link is validated https/loopback
                response.read(_MAX_BODY_BYTES)
                return classify_status(int(response.status))
        except urllib.error.HTTPError as exc:
            try:
                exc.read(_MAX_BODY_BYTES)
            except Exception:
                pass
            return classify_status(int(exc.code))
        except Exception:
            return SendResult(KIND_UNAVAILABLE, None)


__all__ = ["ConnectedCaptureClient", "TOTAL_TIMEOUT_SECONDS"]
