"""The attachment: which remote capture this runtime writes to, and with which token.

Two sources, in order:

1. ``OPENSHARD_REMOTE_CAPTURE_URL`` + ``OPENSHARD_REMOTE_TOKEN`` in the
   environment. For runtimes that inject variables into the agent phase.
2. ``<OPENSHARD_HOME>/remote-capture.json`` (mode 0600), written by
   ``openshard remote attach``. For runtimes whose secrets exist only while
   a setup script runs: attach there, and the hooks that fire later read the
   file.

The token is short-lived and scoped to the one capture (the Platform
enforces both); it is still a bearer secret, so it is never logged, never
put in a repository, and never part of anything that is sent as evidence.
Plain ``http://`` is refused except to loopback.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openshard.adapters.claude_capture_client import capture_home
from openshard.telemetry.transport import endpoint_allowed

ATTACHMENT_FILENAME = "remote-capture.json"
ATTACHMENT_SCHEMA_VERSION = 1

URL_ENV = "OPENSHARD_REMOTE_CAPTURE_URL"
TOKEN_ENV = "OPENSHARD_REMOTE_TOKEN"
DISABLE_ENV = "OPENSHARD_REMOTE_CAPTURE"  # "off"/"0"/"false" disables; nothing enables

TOKEN_PREFIX = "osr_"
_TOKEN_RE = re.compile(r"^osr_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{20,200}$")
_URL_RE = re.compile(
    r"^(?P<endpoint>https?://[^/\s?#]+)/v1/remote-captures/"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)
_FALSEY = frozenset({"0", "off", "false", "no", "disabled"})

SOURCE_ENV = "env"
SOURCE_FILE = "file"


@dataclass(frozen=True)
class Attachment:
    capture_url: str  # <endpoint>/v1/remote-captures/<id>, no trailing slash
    token: str
    source: str
    organisation_id: str | None = None
    agent: str | None = None
    expires_at: str | None = None
    attached_at: str | None = None

    @property
    def endpoint(self) -> str:
        match = _URL_RE.match(self.capture_url)
        return match.group("endpoint") if match else ""

    @property
    def capture_id(self) -> str:
        match = _URL_RE.match(self.capture_url)
        return match.group("id").lower() if match else ""

    @property
    def token_prefix(self) -> str:
        """The public, non-secret part of the token (``osr_`` + 8 chars), for display."""
        return self.token[:12]

    def to_public_dict(self) -> dict:
        """Everything but the secret."""
        return {
            "capture_url": self.capture_url,
            "capture_id": self.capture_id,
            "organisation_id": self.organisation_id,
            "agent": self.agent,
            "token_prefix": self.token_prefix,
            "expires_at": self.expires_at,
            "attached_at": self.attached_at,
            "source": self.source,
        }


def attachment_path(env: dict | os._Environ | None = None) -> Path:
    return Path(capture_home(env)) / ATTACHMENT_FILENAME


def normalize_capture_url(value: object) -> str:
    """The canonical capture URL, or ``ValueError`` naming what is wrong (never echoing a secret)."""
    text = value.strip().rstrip("/") if isinstance(value, str) else ""
    match = _URL_RE.match(text)
    if not match:
        raise ValueError("the capture URL must look like https://<platform>/v1/remote-captures/<capture id>")
    if not endpoint_allowed(match.group("endpoint")):
        raise ValueError("the capture URL must be https:// (http:// is allowed only for localhost)")
    return f"{match.group('endpoint')}/v1/remote-captures/{match.group('id').lower()}"


def valid_token(value: object) -> bool:
    return isinstance(value, str) and bool(_TOKEN_RE.match(value.strip()))


def disabled(env: dict | os._Environ | None = None) -> bool:
    env = os.environ if env is None else env
    raw = env.get(DISABLE_ENV)
    return isinstance(raw, str) and raw.strip().lower() in _FALSEY


def _from_env(env: dict | os._Environ) -> Attachment | None:
    url, token = env.get(URL_ENV), env.get(TOKEN_ENV)
    if not (isinstance(url, str) and url.strip() and valid_token(token)):
        return None
    try:
        return Attachment(capture_url=normalize_capture_url(url), token=str(token).strip(), source=SOURCE_ENV)
    except ValueError:
        return None


def load_attachment(env: dict | os._Environ | None = None) -> Attachment | None:
    """The stored attachment, or None when absent or unusable. Never raises."""
    try:
        data = json.loads(attachment_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not valid_token(data.get("token")):
        return None
    try:
        url = normalize_capture_url(data.get("capture_url"))
    except ValueError:
        return None

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    return Attachment(
        capture_url=url, token=data["token"].strip(), source=SOURCE_FILE,
        organisation_id=text("organisation_id"), agent=text("agent"),
        expires_at=text("expires_at"), attached_at=text("attached_at"),
    )


def attached_hint(env: dict | os._Environ | None = None) -> bool:
    """Cheap pre-check for the hook path: could this runtime be attached? (No file is parsed.)"""
    env = os.environ if env is None else env
    if disabled(env):
        return False
    if env.get(TOKEN_ENV) and env.get(URL_ENV):
        return True
    try:
        return attachment_path(env).exists()
    except OSError:
        return False


def resolve_attachment(env: dict | os._Environ | None = None) -> Attachment | None:
    """The attachment in effect: the stored one, else the environment's. None: not attached.

    The stored attachment wins because ``attach`` verified it against the
    Platform and recorded the organisation; the environment alone is the
    fallback for a runtime where ``attach`` never ran.
    """
    env = os.environ if env is None else env
    if disabled(env):
        return None
    return load_attachment(env) or _from_env(env)


def save_attachment(
    *,
    capture_url: str,
    token: str,
    organisation_id: str | None = None,
    agent: str | None = None,
    expires_at: str | None = None,
    env: dict | os._Environ | None = None,
) -> Attachment:
    """Write the attachment file (0600). Raises ``ValueError`` for an unusable URL or token."""
    url = normalize_capture_url(capture_url)
    if not valid_token(token):
        raise ValueError("the token does not look like a remote capture token (osr_...)")
    attached_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema_version": ATTACHMENT_SCHEMA_VERSION,
        "capture_url": url,
        "token": token.strip(),
        "organisation_id": organisation_id,
        "agent": agent,
        "expires_at": expires_at,
        "attached_at": attached_at,
    }
    path = attachment_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return Attachment(
        capture_url=url, token=token.strip(), source=SOURCE_FILE, organisation_id=organisation_id,
        agent=agent, expires_at=expires_at, attached_at=attached_at,
    )


def clear_attachment(env: dict | os._Environ | None = None) -> bool:
    try:
        attachment_path(env).unlink()
        return True
    except OSError:
        return False


__all__ = [
    "ATTACHMENT_FILENAME",
    "Attachment",
    "DISABLE_ENV",
    "TOKEN_ENV",
    "URL_ENV",
    "attached_hint",
    "attachment_path",
    "clear_attachment",
    "disabled",
    "load_attachment",
    "normalize_capture_url",
    "resolve_attachment",
    "save_attachment",
    "valid_token",
]
