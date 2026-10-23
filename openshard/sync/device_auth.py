"""Device authorisation client for ``openshard login`` (RFC 8628 shape).

The Platform contract (``packages/contracts/src/device-auth.ts``):

* ``POST <endpoint>/v1/auth/device/code`` with ``client_name`` and optional
  ``client_version`` / ``hostname`` returns ``device_code``, ``user_code``,
  ``verification_uri``, ``verification_uri_complete``, ``expires_in`` and
  ``interval`` (seconds).
* ``POST <endpoint>/v1/auth/device/token`` with ``device_code`` answers 400
  ``{"error": "authorization_pending" | "slow_down" | "expired_token" |
  "access_denied"}`` until a person approves the code in the browser, then
  200 ``{"api_key", "org_id", "org_name"}`` exactly once. Polling early
  makes the server add 5 seconds to the interval and answer ``slow_down``.

The organisation API key is minted only when an approved code is polled, so
a flow abandoned before that leaves no credential behind. The ``device_code``
and the key are secrets: neither is ever logged or returned in a public dict.
Standard library only; redirects are refused so nothing travels to a host
the person did not choose.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from openshard.sync.config import is_api_key, is_organisation_id

CODE_PATH = "/v1/auth/device/code"
TOKEN_PATH = "/v1/auth/device/token"
DEFAULT_INTERVAL_SECONDS = 5
SLOW_DOWN_STEP_SECONDS = 5  # RFC 8628 section 3.5, and what the Platform adds
MAX_NETWORK_BACKOFF_SECONDS = 30
MAX_CONSECUTIVE_NETWORK_FAILURES = 6
REQUEST_TIMEOUT_SECONDS = 15.0
_MAX_BODY_BYTES = 64 * 1024
_sleep = time.sleep  # module attributes so tests can run the flow on a fake clock
_clock = time.monotonic

# Final outcomes of a flow that did not produce a credential.
DENIED = "denied"
EXPIRED = "expired"
NETWORK = "network"
REJECTED = "rejected"  # the Platform answered with something this client cannot use


class DeviceAuthError(Exception):
    """The flow ended without a credential. ``kind`` is one of the outcome constants above."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class DeviceCode:
    device_code: str  # secret: whoever holds it can collect the key
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int

    def to_public_dict(self) -> dict:
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "verification_uri_complete": self.verification_uri_complete,
            "expires_in": self.expires_in,
        }


@dataclass(frozen=True)
class DeviceToken:
    api_key: str  # secret
    org_id: str
    org_name: str
    # Who approved the sign-in (Platform ``approved_by``): the person this key
    # syncs as. None from a Platform that does not say.
    approver_email: str | None = None
    approver_name: str | None = None


def _version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _post(url: str, payload: dict, *, timeout: float) -> tuple[int | None, dict | None]:
    """One JSON POST: ``(http status or None when unreachable, parsed body or None)``. Never raises."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            return None

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"openshard/{_version()}",
        },
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - https/loopback only
            status = int(response.status)
            raw = response.read(_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            raw = exc.read(_MAX_BODY_BYTES + 1)
        except OSError:
            raw = b""
    except Exception:
        return None, None
    if len(raw) > _MAX_BODY_BYTES:
        return status, None
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        body = None
    return status, body if isinstance(body, dict) else None


def _server_message(body: dict | None) -> str | None:
    """The human message of a Platform ``{"error": {"code", "message"}}`` body, if any."""
    error = body.get("error") if body else None
    message = error.get("message") if isinstance(error, dict) else None
    return message.strip()[:300] if isinstance(message, str) and message.strip() else None


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def request_code(
    endpoint: str, *, client_name: str, hostname: str | None, timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> DeviceCode:
    """Ask the Platform for a sign-in code. Raises :class:`DeviceAuthError`."""
    payload: dict = {"client_name": client_name, "client_version": _version()[:64]}
    if hostname:
        payload["hostname"] = hostname[:120]
    status, body = _post(endpoint + CODE_PATH, payload, timeout=timeout)
    if status is None:
        raise DeviceAuthError(NETWORK, f"Could not reach the Platform at {endpoint}.")
    if status != 200 or body is None:
        detail = _server_message(body) or f"HTTP {status}"
        raise DeviceAuthError(REJECTED, f"The Platform at {endpoint} did not issue a sign-in code ({detail}).")
    fields = [body.get(k) for k in ("device_code", "user_code", "verification_uri", "verification_uri_complete")]
    if not all(isinstance(v, str) and v.strip() for v in fields):
        raise DeviceAuthError(REJECTED, f"The Platform at {endpoint} answered without a usable sign-in code.")
    device_code, user_code, uri, uri_complete = (str(v).strip() for v in fields)
    return DeviceCode(
        device_code=device_code,
        user_code=user_code,
        verification_uri=uri,
        verification_uri_complete=uri_complete,
        expires_in=_positive_int(body.get("expires_in"), 600),
        interval=_positive_int(body.get("interval"), DEFAULT_INTERVAL_SECONDS),
    )


_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def _token(endpoint: str, body: dict | None) -> DeviceToken:
    api_key = body.get("api_key") if body else None
    org_id = body.get("org_id") if body else None
    org_name = body.get("org_name") if body else None
    if not is_api_key(api_key) or not is_organisation_id(org_id):
        raise DeviceAuthError(REJECTED, f"The Platform at {endpoint} approved the request but sent no usable key.")
    approver = body.get("approved_by") if body else None
    email = approver.get("email") if isinstance(approver, dict) else None
    name = approver.get("name") if isinstance(approver, dict) else None
    email_ok = isinstance(email, str) and len(email) <= 254 and _EMAIL_RE.fullmatch(email) is not None
    return DeviceToken(
        api_key=str(api_key), org_id=str(org_id).lower(),
        org_name=org_name.strip()[:200] if isinstance(org_name, str) else "",
        approver_email=email if email_ok else None,
        approver_name=" ".join(name.split())[:80] or None if email_ok and isinstance(name, str) else None,
    )


def poll_for_token(
    endpoint: str,
    code: DeviceCode,
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> DeviceToken:
    """Wait for the person to approve ``code``; return the credential once. Raises :class:`DeviceAuthError`.

    Waits ``interval`` before every poll, adds 5 seconds on ``slow_down``,
    backs off on network failures and 5xx answers, and stops at
    ``expires_in``. ``KeyboardInterrupt`` propagates unchanged.
    """
    sleep = sleep or _sleep
    clock = clock or _clock
    deadline = clock() + code.expires_in
    interval = float(code.interval)
    wait = interval
    failures = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeviceAuthError(EXPIRED, "The sign-in code expired before it was approved.")
        sleep(min(wait, remaining))
        status, body = _post(endpoint + TOKEN_PATH, {"device_code": code.device_code}, timeout=timeout)
        if status == 200:
            return _token(endpoint, body)
        error = body.get("error") if body else None
        if status == 400 and error == "authorization_pending":
            failures, wait = 0, interval
            continue
        if status == 400 and error == "slow_down":
            interval += SLOW_DOWN_STEP_SECONDS
            failures, wait = 0, interval
            continue
        if status == 400 and error == "expired_token":
            raise DeviceAuthError(EXPIRED, "The sign-in code expired or was already used.")
        if status == 400 and error == "access_denied":
            raise DeviceAuthError(DENIED, "The sign-in request was denied in the browser.")
        if status is None or status >= 500 or status == 429:
            failures += 1
            if failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                raise DeviceAuthError(NETWORK, f"Lost contact with the Platform at {endpoint} while waiting.")
            wait = min(interval * (2 ** failures), MAX_NETWORK_BACKOFF_SECONDS)
            continue
        detail = _server_message(body) or f"HTTP {status}"
        raise DeviceAuthError(REJECTED, f"The Platform at {endpoint} refused the sign-in ({detail}).")
