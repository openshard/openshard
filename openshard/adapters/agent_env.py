"""Where a coding agent runs: provider and surface, read from its hook environment.

Claude Code hands its hook processes its own environment, which is where
its provider selection lives (``CLAUDE_CODE_USE_BEDROCK`` / ``_VERTEX`` /
``_FOUNDRY``; otherwise the Anthropic API unless ``ANTHROPIC_BASE_URL``
routes it elsewhere) and how it was launched (``CLAUDE_CODE_ENTRYPOINT``).
Only these derived tokens are recorded, never an environment value beyond
the entrypoint name. Stdlib only: the command-hook client imports it on
every hook (see ``claude_capture_client``), so it must stay cheap.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

PROVIDER_SOURCE_AGENT_ENV = "agent_env"
_CLAUDE_PROVIDER_FLAGS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_CODE_USE_BEDROCK", "amazon_bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "google_vertex"),
    ("CLAUDE_CODE_USE_FOUNDRY", "microsoft_foundry"),
)
_CLAUDE_PROVIDERS = frozenset({"anthropic", *(p for _, p in _CLAUDE_PROVIDER_FLAGS)})
_SURFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")


def agent_provider_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value in _CLAUDE_PROVIDERS else None


def agent_surface_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SURFACE_RE.match(value) else None


def _env_flag(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() not in ("", "0", "false", "no", "off")


def claude_agent_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """``{"provider", "surface"}`` (each only when known) from Claude Code's hook environment.

    Exactly one ``CLAUDE_CODE_USE_*`` flag names its cloud provider; none
    set means the Anthropic API -- unless ``ANTHROPIC_BASE_URL`` points
    Claude Code at a custom gateway, whose provider OpenShard cannot know
    (left unknown, as are conflicting flags). ``surface`` is the raw,
    validated ``CLAUDE_CODE_ENTRYPOINT`` (``cli``, ``sdk-cli``,
    ``claude-vscode`` ...). Never raises.
    """
    out: dict[str, str] = {}
    if not isinstance(env, Mapping):
        return out
    try:
        flagged = [provider for var, provider in _CLAUDE_PROVIDER_FLAGS if _env_flag(env.get(var))]
        if len(flagged) == 1:
            out["provider"] = flagged[0]
        elif not flagged and not str(env.get("ANTHROPIC_BASE_URL") or "").strip():
            out["provider"] = "anthropic"
        surface = agent_surface_or_none(env.get("CLAUDE_CODE_ENTRYPOINT"))
        if surface:
            out["surface"] = surface
    except Exception:
        return {}
    return out


def format_agent_env(agent_env: Mapping[str, str] | None) -> str | None:
    """Header form of :func:`claude_agent_env` (``provider=anthropic;surface=cli``), or None when empty."""
    if not agent_env:
        return None
    parts = []
    provider = agent_provider_or_none(agent_env.get("provider"))
    surface = agent_surface_or_none(agent_env.get("surface"))
    if provider:
        parts.append(f"provider={provider}")
    if surface:
        parts.append(f"surface={surface}")
    return ";".join(parts) or None


def parse_agent_env(value: object) -> dict[str, str]:
    """Inverse of :func:`format_agent_env`; anything malformed is dropped, never repaired."""
    out: dict[str, str] = {}
    if not isinstance(value, str) or len(value) > 200:
        return out
    for part in value.split(";"):
        key, _, raw = part.partition("=")
        key = key.strip()
        if key == "provider":
            provider = agent_provider_or_none(raw.strip())
            if provider:
                out["provider"] = provider
        elif key == "surface":
            surface = agent_surface_or_none(raw)
            if surface:
                out["surface"] = surface
    return out
