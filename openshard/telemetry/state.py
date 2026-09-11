"""Installation identity and consent state (``~/.openshard/telemetry.json``).

User-global, never repository-local: an installation id must never be
committed to a repository, and consent is a property of the person's
machine, not of one checkout. The file lives in the same directory as the
capture service's state (``claude_capture_client.capture_home``, so the
``OPENSHARD_HOME`` override applies to both).

The installation id is a random uuid4 minted locally. It is never derived
from a username, hostname, email, repository path, git remote, MAC
address or anything else that identifies a person or a machine, and
``openshard telemetry reset`` mints a new one.

Consent has three values, and events are emitted only when it is ``on``:

* ``unset`` -- nobody has seen the notice yet. An install that predates
  telemetry (and was promised "OpenShard does not send telemetry" at
  onboarding) stays here, silent, until the person next runs
  ``openshard setup`` and sees the new notice.
* ``on`` -- the notice was shown (default-on with an obvious opt-out), or
  ``openshard telemetry on`` was run.
* ``off`` -- ``openshard telemetry off`` was run.

Nothing in this module can enable telemetry from an environment variable
or a config file: those can only *disable* it (see ``effective_status``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openshard.adapters.claude_capture_client import capture_home

STATE_FILENAME = "telemetry.json"
STATE_SCHEMA_VERSION = 1

CONSENT_ON = "on"
CONSENT_OFF = "off"
CONSENT_UNSET = "unset"
_CONSENTS = (CONSENT_ON, CONSENT_OFF, CONSENT_UNSET)

DISABLE_ENV = "OPENSHARD_TELEMETRY"  # "off"/"0"/"false" disables; nothing enables
DO_NOT_TRACK_ENV = "DO_NOT_TRACK"  # the cross-tool convention: any truthy value disables
_CI_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI")
_FALSEY = frozenset({"0", "off", "false", "no", "disabled"})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(env: dict | os._Environ | None = None) -> Path:
    return Path(capture_home(env)) / STATE_FILENAME


@dataclass
class TelemetryState:
    installation_id: str
    created_at: str
    improve: str = CONSENT_UNSET
    improve_decided_at: str | None = None
    improve_source: str | None = None
    richer: str = CONSENT_OFF  # reserved; never anything but off in v1
    last_version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on write

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra)
        data.update({
            "schema_version": STATE_SCHEMA_VERSION,
            "installation_id": self.installation_id,
            "created_at": self.created_at,
            "improve": self.improve,
            "improve_decided_at": self.improve_decided_at,
            "improve_source": self.improve_source,
            "richer": CONSENT_OFF,
            "last_version": self.last_version,
        })
        return data


def _fresh() -> TelemetryState:
    return TelemetryState(installation_id=str(uuid4()), created_at=_now())


def _parse(data: object) -> TelemetryState | None:
    if not isinstance(data, dict):
        return None
    iid = data.get("installation_id")
    if not isinstance(iid, str) or len(iid) != 36:
        return None
    improve = data.get("improve")
    known = {
        "schema_version", "installation_id", "created_at", "improve", "improve_decided_at",
        "improve_source", "richer", "last_version",
    }
    created_at = data.get("created_at")
    return TelemetryState(
        installation_id=iid,
        created_at=created_at if isinstance(created_at, str) else _now(),
        improve=improve if improve in _CONSENTS else CONSENT_UNSET,
        improve_decided_at=data.get("improve_decided_at") if isinstance(data.get("improve_decided_at"), str) else None,
        improve_source=data.get("improve_source") if isinstance(data.get("improve_source"), str) else None,
        last_version=data.get("last_version") if isinstance(data.get("last_version"), str) else None,
        extra={k: v for k, v in data.items() if k not in known},
    )


def _write(path: Path, state: TelemetryState) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_state(env: dict | os._Environ | None = None) -> TelemetryState | None:
    """The persisted state, or None when there is none (or it is unreadable)."""
    try:
        with state_path(env).open(encoding="utf-8") as fh:
            return _parse(json.load(fh))
    except Exception:
        return None


def ensure_state(env: dict | os._Environ | None = None) -> tuple[TelemetryState, bool]:
    """Load the state, creating it (consent ``unset``) on first use.

    Returns ``(state, created)``. Never raises; when the file cannot be
    written, an in-memory state is returned so callers still get an id for
    this process (nothing is emitted for it anyway unless consent is on).
    """
    state = load_state(env)
    if state is not None:
        return state, False
    state = _fresh()
    _write(state_path(env), state)
    return state, True


def set_consent(improve: str, *, source: str, env: dict | os._Environ | None = None) -> TelemetryState:
    """Record the "Help improve OpenShard" decision. Never raises."""
    if improve not in (CONSENT_ON, CONSENT_OFF):
        raise ValueError("consent must be 'on' or 'off'")
    state, _ = ensure_state(env)
    previous = state.improve
    state.improve = improve
    state.improve_decided_at = _now()
    state.improve_source = source
    _write(state_path(env), state)
    if improve == CONSENT_ON and previous != CONSENT_ON:
        # Turning *off* is deliberately silent: once off, nothing is sent.
        try:
            from openshard.telemetry.client import emit

            emit("telemetry.consent_changed", env, improve=CONSENT_ON, source=source)
        except Exception:
            pass
    return state


def consent_after_notice(*, source: str, env: dict | os._Environ | None = None) -> TelemetryState:
    """The notice was shown: an ``unset`` consent becomes ``on`` (default-on).

    A decision already made (``on`` or ``off``) is never overridden by
    showing the notice again.
    """
    state, _ = ensure_state(env)
    if state.improve == CONSENT_UNSET:
        return set_consent(CONSENT_ON, source=source, env=env)
    return state


def note_version(version: str, env: dict | os._Environ | None = None) -> tuple[TelemetryState, bool, bool]:
    """Remember the running version. Returns ``(state, first_run, version_changed)``."""
    state, created = ensure_state(env)
    changed = state.last_version != version
    if changed:
        state.last_version = version
        _write(state_path(env), state)
    return state, created, changed


def reset_installation_id(env: dict | os._Environ | None = None) -> TelemetryState:
    """Mint a new installation id; consent is kept as it was."""
    state, _ = ensure_state(env)
    state.installation_id = str(uuid4())
    state.created_at = _now()
    _write(state_path(env), state)
    return state


def _truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip() != "" and value.strip().lower() not in _FALSEY


def _falsey(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _FALSEY


@dataclass(frozen=True)
class Effective:
    enabled: bool
    reason: str  # short, user-facing
    consent: str  # on | off | unset


def effective_status(
    *,
    env: dict | os._Environ | None = None,
    repo_config: dict | None = None,
    state: TelemetryState | None = None,
) -> Effective:
    """Whether events may be emitted right now, and why not when they may not.

    Precedence (each can only turn telemetry *off*): ``OPENSHARD_TELEMETRY``
    set to a false value, ``DO_NOT_TRACK`` set, a CI environment (``CI`` /
    ``GITHUB_ACTIONS`` / ``GITLAB_CI`` -- agent environments are *not* CI
    and are not suppressed), the repository's ``.openshard/config.yml``
    ``telemetry: {enabled: false}`` (how a team turns it off for everyone
    working in that repository), then the person's consent.
    """
    env = os.environ if env is None else env
    if _falsey(env.get(DISABLE_ENV)):
        return Effective(False, f"disabled by {DISABLE_ENV}", (state or load_state(env) or _fresh()).improve)
    if _truthy(env.get(DO_NOT_TRACK_ENV)):
        return Effective(False, f"disabled by {DO_NOT_TRACK_ENV}", (state or load_state(env) or _fresh()).improve)
    if any(_truthy(env.get(v)) for v in _CI_VARS):
        return Effective(False, "disabled in CI", (state or load_state(env) or _fresh()).improve)
    block = repo_config.get("telemetry") if isinstance(repo_config, dict) else None
    if isinstance(block, dict) and block.get("enabled") is False:
        return Effective(False, "disabled by this repository's .openshard/config.yml", CONSENT_OFF)
    current = state or load_state(env)
    consent = current.improve if current is not None else CONSENT_UNSET
    if consent == CONSENT_ON:
        return Effective(True, "on", consent)
    if consent == CONSENT_OFF:
        return Effective(False, "off (openshard telemetry on to enable)", consent)
    return Effective(False, "not yet asked (shown at the next openshard setup)", consent)
