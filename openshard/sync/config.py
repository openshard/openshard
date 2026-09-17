"""The Platform link: where receipts sync to, and as whom.

One user-global file, ``<OPENSHARD_HOME>/platform.json`` (mode 0600, never
inside a repository), holds the endpoint, the organisation id and the
organisation-scoped API key (``osk_...``) that ``openshard sync connect``
stored. Environment variables override it for CI and scripted use:

* ``OPENSHARD_PLATFORM_ENDPOINT``  base URL, ``https://`` (or ``http://`` to loopback)
* ``OPENSHARD_PLATFORM_ORG_ID``    organisation UUID
* ``OPENSHARD_PLATFORM_API_KEY``   ``osk_...``

All three must be present in the environment for the override to apply; a
partial set is ignored rather than mixed with the file. ``OPENSHARD_PLATFORM_SYNC=off``
and ``platform: {sync: false}`` in a repository's ``.openshard/config.yml``
turn sync off without forgetting the link.

The API key is never logged, never printed by a normal command (status shows
its public prefix only) and never written anywhere but this file.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openshard.adapters.claude_capture_client import capture_home
from openshard.telemetry.transport import endpoint_allowed

CONFIG_FILENAME = "platform.json"
CONFIG_SCHEMA_VERSION = 1

ENDPOINT_ENV = "OPENSHARD_PLATFORM_ENDPOINT"
ORG_ENV = "OPENSHARD_PLATFORM_ORG_ID"
API_KEY_ENV = "OPENSHARD_PLATFORM_API_KEY"
DISABLE_ENV = "OPENSHARD_PLATFORM_SYNC"  # "off"/"0"/"false" disables; nothing enables

API_KEY_PREFIX = "osk_"
_API_KEY_RE = re.compile(r"^osk_[A-Za-z0-9_-]{8,200}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_FALSEY = frozenset({"0", "off", "false", "no", "disabled"})

SOURCE_ENV = "env"
SOURCE_FILE = "file"


@dataclass(frozen=True)
class PlatformLink:
    endpoint: str
    organisation_id: str
    api_key: str
    linked_at: str | None
    source: str

    @property
    def key_prefix(self) -> str:
        """The public, non-secret part of the key (``osk_`` + 8 chars), for display."""
        return redact_api_key(self.api_key)

    def ingest_url(self) -> str:
        return f"{self.endpoint}/v1/orgs/{self.organisation_id}/receipts"

    def to_public_dict(self) -> dict:
        """Everything but the secret."""
        return {
            "endpoint": self.endpoint,
            "organisation_id": self.organisation_id,
            "api_key_prefix": self.key_prefix,
            "linked_at": self.linked_at,
            "source": self.source,
        }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def config_path(env: dict | os._Environ | None = None) -> Path:
    return Path(capture_home(env)) / CONFIG_FILENAME


def is_api_key(value: object) -> bool:
    return isinstance(value, str) and bool(_API_KEY_RE.match(value))


def is_organisation_id(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def normalise_endpoint(value: object) -> str | None:
    """A trimmed base URL without a trailing slash, or None when not allowed.

    HTTPS anywhere, or plain HTTP to loopback only (local development);
    anything else is refused so a key can never travel unencrypted.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip("/")
    return candidate if candidate and endpoint_allowed(candidate) else None


def redact_api_key(key: object) -> str:
    if not isinstance(key, str) or not key.startswith(API_KEY_PREFIX):
        return "(none)"
    return key[: len(API_KEY_PREFIX) + 8] + "…"


def _parse(data: object, *, source: str) -> PlatformLink | None:
    if not isinstance(data, dict):
        return None
    endpoint = normalise_endpoint(data.get("endpoint"))
    org = data.get("organisation_id")
    key = data.get("api_key")
    if endpoint is None or not is_organisation_id(org) or not is_api_key(key):
        return None
    linked_at = data.get("linked_at")
    return PlatformLink(
        endpoint=endpoint,
        organisation_id=str(org).lower(),
        api_key=str(key),
        linked_at=linked_at if isinstance(linked_at, str) else None,
        source=source,
    )


def load_link(env: dict | os._Environ | None = None) -> PlatformLink | None:
    """The link stored on disk, or None when absent or malformed. Never raises."""
    try:
        with config_path(env).open(encoding="utf-8") as fh:
            return _parse(json.load(fh), source=SOURCE_FILE)
    except Exception:
        return None


def _write_private(path: Path, payload: dict) -> None:
    """Atomic 0600 write: the file is complete and private, or absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_link(
    *,
    endpoint: str,
    organisation_id: str,
    api_key: str,
    env: dict | os._Environ | None = None,
) -> PlatformLink:
    """Validate and store the link. Raises ``ValueError`` on bad input, ``OSError`` on I/O."""
    normalised = normalise_endpoint(endpoint)
    if normalised is None:
        raise ValueError("endpoint must be an https:// URL (http:// is allowed for loopback only)")
    if not is_organisation_id(organisation_id):
        raise ValueError("organisation id must be a UUID")
    if not is_api_key(api_key):
        raise ValueError("API key must be an organisation API key starting with 'osk_'")
    link = PlatformLink(
        endpoint=normalised,
        organisation_id=organisation_id.strip().lower(),
        api_key=api_key.strip(),
        linked_at=_now(),
        source=SOURCE_FILE,
    )
    _write_private(config_path(env), {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "endpoint": link.endpoint,
        "organisation_id": link.organisation_id,
        "api_key": link.api_key,
        "linked_at": link.linked_at,
    })
    return link


def clear_link(env: dict | os._Environ | None = None) -> bool:
    """Forget the stored link. Returns True when a file was removed. Never raises."""
    try:
        config_path(env).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def link_from_env(env: dict | os._Environ | None = None) -> PlatformLink | None:
    """The link described by the three environment variables, or None unless all are valid."""
    env = os.environ if env is None else env
    return _parse(
        {"endpoint": env.get(ENDPOINT_ENV), "organisation_id": env.get(ORG_ENV), "api_key": env.get(API_KEY_ENV)},
        source=SOURCE_ENV,
    )


def resolve_link(env: dict | os._Environ | None = None) -> PlatformLink | None:
    """Environment first (all three variables), then the stored file. Never raises."""
    return link_from_env(env) or load_link(env)


def sync_disabled(env: dict | os._Environ | None = None, repo_config: dict | None = None) -> str | None:
    """The reason sync is switched off right now, or None when it may run.

    Each source can only turn sync *off*: ``OPENSHARD_PLATFORM_SYNC`` set to
    a false value, then ``platform: {sync: false}`` in the repository config.
    """
    env = os.environ if env is None else env
    value = env.get(DISABLE_ENV)
    if isinstance(value, str) and value.strip().lower() in _FALSEY:
        return f"disabled by {DISABLE_ENV}"
    block = repo_config.get("platform") if isinstance(repo_config, dict) else None
    if isinstance(block, dict) and block.get("sync") is False:
        return "disabled by this repository's .openshard/config.yml"
    return None
