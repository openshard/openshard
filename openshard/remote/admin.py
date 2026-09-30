"""The trusted side of remote capture: open, list, read and revoke captures.

These calls use the organisation's Platform link (``openshard sync
connect``), i.e. the long-lived organisation API key, and therefore run on a
machine the user trusts: a laptop, a CI secret store. They never run inside
the ephemeral agent runtime; that side holds only the capture's own
short-lived token (``remote/config.py``).

stdlib ``urllib`` only. Every function returns ``(ok, payload_or_error)`` and
never raises; an error is a short code plus the HTTP status, never a secret.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from openshard.sync.config import PlatformLink

TOTAL_TIMEOUT_SECONDS = 15.0
_MAX_BODY_BYTES = 2 * 1024 * 1024


def _call(link: PlatformLink, method: str, path: str, body: dict | None = None) -> tuple[bool, Any]:
    import urllib.error
    import urllib.request

    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {link.api_key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    url = f"{link.endpoint}/v1/orgs/{link.organisation_id}/remote-captures{path}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TOTAL_TIMEOUT_SECONDS) as response:  # noqa: S310 - https/loopback only
            return True, json.loads(response.read(_MAX_BODY_BYTES).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        code = None
        try:
            error = json.loads(exc.read(64 * 1024).decode("utf-8", "replace")).get("error")
            code = error.get("code") if isinstance(error, dict) else None
        except Exception:
            pass
        if exc.code == 404 and code in (None, "not_found") and path == "":
            return False, {"status": 404, "code": "remote_capture_unsupported"}
        return False, {"status": int(exc.code), "code": code if isinstance(code, str) else "error"}
    except Exception:
        return False, {"status": None, "code": "unavailable"}


def create_capture(
    link: PlatformLink,
    *,
    agent: str,
    provider: str | None = None,
    repo_identity: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    ttl_minutes: int | None = None,
) -> tuple[bool, Any]:
    """Open a remote capture. On success the payload carries the token, shown this once."""
    body: dict[str, Any] = {"agent": agent}
    for key, value in (("provider", provider), ("repo_identity", repo_identity), ("repo", repo), ("branch", branch)):
        if value:
            body[key] = value
    if ttl_minutes is not None:
        body["ttl_minutes"] = int(ttl_minutes)
    ok, payload = _call(link, "POST", "", body)
    if ok and isinstance(payload, dict) and isinstance(payload.get("capture_path"), str):
        payload["capture_url"] = link.endpoint + payload["capture_path"]
    return ok, payload


def list_captures(link: PlatformLink) -> tuple[bool, Any]:
    return _call(link, "GET", "")


def get_capture(link: PlatformLink, capture_id: str) -> tuple[bool, Any]:
    return _call(link, "GET", "/" + urllib.parse.quote(capture_id, safe=""))


def revoke_capture(link: PlatformLink, capture_id: str) -> tuple[bool, Any]:
    return _call(link, "POST", "/" + urllib.parse.quote(capture_id, safe="") + "/revoke")


__all__ = ["create_capture", "get_capture", "list_captures", "revoke_capture"]
