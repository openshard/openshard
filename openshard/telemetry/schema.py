"""The telemetry event schema: the complete list of what can leave a machine.

Versioned and closed. Every event type has an allowlist of properties, and
every property has a validator that admits only a bounded shape -- an enum
member, a bounded integer, a bool, a 2-decimal float, or a *token*
(``^[A-Za-z0-9._-]{1,64}$``, additionally rejected by the secret scrubber).
The token grammar cannot express a filesystem path, an email address, a
URL, a repository name or a shell command, so the schema excludes them by
construction rather than by policy. Unknown event types and unknown
properties are dropped, and a single invalid property drops the whole
event (fail closed), never a partial one.

Adding an event or property is a deliberate schema change: bump
``SCHEMA_VERSION`` when the envelope changes, and document the addition in
``docs/telemetry.md``. The reserved names in ``RESERVED_EVENT_TYPES`` are
the future "richer development data" layer; they are not accepted in v1.
"""

from __future__ import annotations

import platform as _platform
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from openshard.safety.sanitize import looks_like_secret

SCHEMA_VERSION = 1

CONSENT_IMPROVE = "improve"  # the "Help improve OpenShard" control (v1)
CONSENT_RICHER = "richer"  # reserved: "Share richer development data" (not implemented)
CONSENT_LEVELS: frozenset[str] = frozenset({CONSENT_IMPROVE, CONSENT_RICHER})

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_INSTALLATION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_MAX_INT = 10_000_000

Validator = Callable[[object], Any]  # returns the cleaned value, or raises ValueError


def _enum(*values: str) -> Validator:
    allowed = frozenset(values)

    def check(value: object) -> str:
        if isinstance(value, str) and value in allowed:
            return value
        raise ValueError("not an allowed value")

    return check


def _int(lo: int = 0, hi: int = _MAX_INT) -> Validator:
    def check(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("not an int")
        if value < lo or value > hi:
            raise ValueError("out of range")
        return value

    return check


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("not a bool")


def _money(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    if value != value or value < 0 or value > 1_000_000:  # NaN / negative / absurd
        raise ValueError("out of range")
    return round(float(value), 2)


def _optional(inner: Validator) -> Validator:
    def check(value: object) -> Any:
        if value is None:
            return None
        return inner(value)

    return check


def token(value: object) -> str:
    """A bounded identifier-like string; never a path, email, URL or command."""
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        raise ValueError("not a token")
    if looks_like_secret(value):
        raise ValueError("secret-like")
    return value


def _token_list(*allowed: str, max_items: int = 8) -> Validator:
    allowed_set = frozenset(allowed)

    def check(value: object) -> list[str]:
        if not isinstance(value, list) or len(value) > max_items:
            raise ValueError("not a bounded list")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str) or item not in allowed_set:
                raise ValueError("list item not allowed")
            out.append(item)
        return out

    return check


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

AGENTS = ("claude_code", "codex", "opencode", "cursor", "antigravity", "native", "wrap", "import", "other")
ORIGINS = ("openshard_routed", "external_observed", "unknown")
CAPTURE_DEPTHS = ("full", "partial", "unknown")
FILES_SOURCES = ("git_diff", "hook_reported", "not_available", "other")
CHECKS = ("none", "attempted_unverified", "passed", "failed")
RESULTS = ("ok", "error")
ERROR_CATEGORIES = ("timeout", "permission", "io", "parse", "git_unavailable", "lock_timeout", "usage", "unknown")
COMPONENTS = ("cli", "hooks", "capture_service", "mcp", "native_run")
COMMANDS = (
    "setup", "doctor", "init", "last", "history", "context", "stats", "learned", "run", "demo",
    "capture.status", "capture.start", "capture.stop", "capture.install", "capture.uninstall",
    "mcp.install", "mcp.uninstall", "mcp.serve",
    "telemetry.status", "telemetry.on", "telemetry.off", "telemetry.reset", "telemetry.sample",
    "other",
)
HISTORY_COMMANDS = ("history", "context", "search", "relevant_context", "last", "stats")
MCP_TOOLS = ("recent_shards", "get_shard", "get_receipt", "search_history", "relevant_context")
SERVICE_STATES = ("started", "stopped", "idle_exit", "spawn_failed")
CONSENT_SOURCES = ("setup", "onboarding", "cli", "env", "config")
MODEL_FAMILIES = (
    "claude", "gpt", "o-series", "codex", "gemini", "llama", "mistral", "deepseek", "qwen", "grok",
    "unknown", "other",
)
OS_NAMES = ("windows", "linux", "darwin", "other")
ARCHES = ("x86_64", "arm64", "other")

_RECEIPT_PROPERTIES: dict[str, Validator] = {
    "agent": _enum(*AGENTS),
    "origin": _enum(*ORIGINS),
    "capture_depth": _enum(*CAPTURE_DEPTHS),
    "files_changed": _int(),
    "files_source": _enum(*FILES_SOURCES),
    "tool_calls": _int(),
    "tool_failures": _int(),
    "checks": _enum(*CHECKS),
    "attempt_number": _int(1, 1_000),
    "is_retry": _bool,
    "turn_count": _int(),
    "duration_s": _optional(_int()),
    "cost_usd": _optional(_money),
    "model_family": _enum(*MODEL_FAMILIES),
}

# event_type -> {property: validator}. This IS the contract.
EVENT_TYPES: dict[str, dict[str, Validator]] = {
    "install.seen": {"first_run": _bool},
    "setup.completed": {
        "agents": _token_list("claude_code", "codex", "opencode", "cursor", "antigravity"),
        "mcp": _bool,
        "capture_service": _enum("ok", "failed", "disabled"),
        "result": _enum(*RESULTS),
        "error_category": _optional(_enum(*ERROR_CATEGORIES)),
    },
    "command.invoked": {
        "command": _enum(*COMMANDS),
        "duration_ms": _int(),
        "result": _enum(*RESULTS),
        "error_category": _optional(_enum(*ERROR_CATEGORIES)),
    },
    "receipt.created": dict(_RECEIPT_PROPERTIES),
    "receipt.completed": dict(_RECEIPT_PROPERTIES),
    "history.queried": {
        "command": _enum(*HISTORY_COMMANDS),
        "results": _int(),
        "duration_ms": _int(),
    },
    "mcp.tool_called": {
        "tool": _enum(*MCP_TOOLS),
        "results": _int(),
        "duration_ms": _int(),
        "result": _enum(*RESULTS),
    },
    "capture.service": {
        "state": _enum(*SERVICE_STATES),
        "queued": _int(),
        "folded": _int(),
        "replay_errors": _int(),
        # v0.4.4: bounded counters only -- never the credential or any line content.
        "rejected": _int(),
        "corrupt_lines": _int(),
        "p50_ms": _int(),
        "p95_ms": _int(),
    },
    "error.occurred": {
        "component": _enum(*COMPONENTS),
        "category": _enum(*ERROR_CATEGORIES),
    },
    "telemetry.consent_changed": {
        "improve": _enum("on", "off"),
        "source": _enum(*CONSENT_SOURCES),
    },
}

# The future "richer development data" layer (a separate, off-by-default
# consent). Named now so it is added deliberately; rejected in v1.
RESERVED_EVENT_TYPES: frozenset[str] = frozenset({
    "attempt.outcome", "context.retrieval.outcome", "developer.correction", "agent.handoff",
})

ENVELOPE_KEYS: tuple[str, ...] = (
    "schema_version", "event_id", "event_type", "occurred_at", "installation_id",
    "consent_level", "openshard_version", "platform", "properties",
)


def platform_info() -> dict[str, str]:
    """Coarse platform facts: OS family, architecture family, Python major.minor."""
    system = sys.platform
    if system.startswith("win"):
        os_name = "windows"
    elif system.startswith("linux"):
        os_name = "linux"
    elif system == "darwin":
        os_name = "darwin"
    else:
        os_name = "other"
    try:
        machine = _platform.machine().lower()
    except Exception:
        machine = ""
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "other"
    return {"os": os_name, "arch": arch, "python": f"{sys.version_info[0]}.{sys.version_info[1]}"}


def model_family(model_id: object) -> str:
    """The public model family a model id belongs to; ``other`` for anything private/unknown.

    An allowlist rather than the raw slug: a custom provider's model name
    (``acme-internal-llm``) can identify an organisation.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        return "unknown"
    slug = model_id.lower()
    if slug == "unknown":
        return "unknown"
    if "codex" in slug:
        return "codex"
    if "claude" in slug:
        return "claude"
    if re.search(r"(^|[^a-z])o[1-9]([^a-z]|$)", slug):
        return "o-series"
    if "gpt" in slug:
        return "gpt"
    for family in ("gemini", "llama", "mistral", "deepseek", "qwen", "grok"):
        if family in slug:
            return family
    return "other"


def _iso_second(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(
    event_type: object,
    properties: Mapping[str, Any] | None,
    *,
    installation_id: str,
    openshard_version: str,
    consent_level: str = CONSENT_IMPROVE,
    platform: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[dict | None, str | None]:
    """Validate and envelope one event. Returns ``(event, None)`` or ``(None, reason)``.

    Pure. Every property is passed through its validator; an unknown
    property is dropped silently (it can never be sent), an invalid one
    drops the whole event. The envelope carries no hostname, username,
    locale, timezone or precise timestamp.
    """
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        return None, "unknown_event_type"
    if consent_level not in CONSENT_LEVELS:
        return None, "bad_consent_level"
    if not isinstance(installation_id, str) or not _INSTALLATION_ID_RE.match(installation_id):
        return None, "bad_installation_id"
    try:
        version = token(openshard_version)
    except ValueError:
        return None, "bad_version"
    spec = EVENT_TYPES[event_type]
    cleaned: dict[str, Any] = {}
    props = properties if isinstance(properties, Mapping) else {}
    for key, validator in spec.items():
        if key not in props:
            # Every listed property is required so the server never has to
            # guess a default; optional ones are declared _optional(...).
            try:
                cleaned[key] = validator(None)
            except ValueError:
                return None, f"missing:{key}"
            continue
        try:
            cleaned[key] = validator(props[key])
        except ValueError:
            return None, f"invalid:{key}"
    plat = dict(platform) if platform is not None else platform_info()
    try:
        plat = {
            "os": _enum(*OS_NAMES)(plat.get("os")),
            "arch": _enum(*ARCHES)(plat.get("arch")),
            "python": token(plat.get("python")),
        }
    except ValueError:
        return None, "bad_platform"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": _iso_second(now),
        "installation_id": installation_id,
        "consent_level": consent_level,
        "openshard_version": version,
        "platform": plat,
        "properties": cleaned,
    }, None


def validate_event(event: object) -> tuple[dict | None, str | None]:
    """Re-validate an already-built event (server side, or a queue line).

    Accepts exactly what :func:`build_event` produces; returns a fresh,
    cleaned copy so a stored event is never trusted beyond the schema.
    """
    if not isinstance(event, Mapping):
        return None, "not_an_object"
    if event.get("schema_version") != SCHEMA_VERSION:
        return None, "bad_schema_version"
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not _INSTALLATION_ID_RE.match(event_id):
        return None, "bad_event_id"
    occurred = event.get("occurred_at")
    if not isinstance(occurred, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", occurred):
        return None, "bad_occurred_at"
    consent = event.get("consent_level")
    if consent not in CONSENT_LEVELS:
        return None, "bad_consent_level"
    plat = event.get("platform")
    built, reason = build_event(
        event.get("event_type"),
        event.get("properties") if isinstance(event.get("properties"), Mapping) else {},
        installation_id=str(event.get("installation_id")),
        openshard_version=str(event.get("openshard_version")),
        consent_level=str(consent),
        platform=plat if isinstance(plat, Mapping) else {},
    )
    if built is None:
        return None, reason
    built["event_id"] = event_id
    built["occurred_at"] = occurred
    return built, None


def describe_schema() -> dict[str, list[str]]:
    """``{event_type: [property, ...]}`` -- for docs and ``telemetry status``."""
    return {name: sorted(spec) for name, spec in EVENT_TYPES.items()}
