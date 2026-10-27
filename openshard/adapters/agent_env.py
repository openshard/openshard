"""Where a coding agent runs: provider, surface and billing basis, read from its hook environment.

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

from openshard.adapters.billing_basis import (
    UNKNOWN,
    billing_basis_or_none,
    claude_billing_basis,
    codex_billing_basis,
)

PROVIDER_SOURCE_AGENT_ENV = "agent_env"
_CLAUDE_PROVIDER_FLAGS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_CODE_USE_BEDROCK", "amazon_bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "google_vertex"),
    ("CLAUDE_CODE_USE_FOUNDRY", "microsoft_foundry"),
)
_CLAUDE_PROVIDERS = frozenset({"anthropic", *(p for _, p in _CLAUDE_PROVIDER_FLAGS)})
_SURFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
# Claude Code on the web sets CLAUDE_CODE_REMOTE_SESSION_ID (``cse_`` form) in
# cloud sessions; the same session appears as ``session_`` in its claude.ai
# URL and in the ``Claude-Session`` trailer on commits Claude creates there.
_CLOUD_SESSION_ENV_RE = re.compile(r"^cse_([A-Za-z0-9]{8,128})$")
_CLOUD_SESSION_RE = re.compile(r"^session_[A-Za-z0-9]{8,128}$")


def agent_provider_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value in _CLAUDE_PROVIDERS else None


def agent_surface_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SURFACE_RE.match(value) else None


def cloud_session_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _CLOUD_SESSION_RE.match(value) else None


def _env_flag(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() not in ("", "0", "false", "no", "off")


def claude_agent_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """``{"provider", "surface", "cloud_session", "billing_basis"}`` (each only when known) from Claude Code's hook environment.

    Exactly one ``CLAUDE_CODE_USE_*`` flag names its cloud provider; none
    set means the Anthropic API -- unless ``ANTHROPIC_BASE_URL`` points
    Claude Code at a custom gateway, whose provider OpenShard cannot know
    (left unknown, as are conflicting flags). ``surface`` is the raw,
    validated ``CLAUDE_CODE_ENTRYPOINT`` (``cli``, ``sdk-cli``,
    ``claude-vscode`` ...). ``cloud_session`` is the claude.ai cloud session
    id in ``session_`` form, only inside a cloud session (``CLAUDE_CODE_REMOTE``).
    ``billing_basis`` is ``api_billed`` or ``subscription`` when
    ``adapters/billing_basis`` can tell from presence checks alone; absent
    (unknown) otherwise. Never raises.
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
        match = _CLOUD_SESSION_ENV_RE.match(str(env.get("CLAUDE_CODE_REMOTE_SESSION_ID") or "").strip())
        if match and _env_flag(env.get("CLAUDE_CODE_REMOTE")):
            out["cloud_session"] = f"session_{match.group(1)}"
        host = claude_host(env)
        if host:
            out["host"] = host
    except Exception:
        return {}
    billing = claude_billing_basis(env)
    if billing != UNKNOWN:
        out["billing_basis"] = billing
    return out


# Where the agent ran, from its own environment: (variable, value or "" for any, ran_on token).
_CI_VARS: tuple[tuple[str, str], ...] = (
    ("GITHUB_ACTIONS", "ci:github_actions"), ("GITLAB_CI", "ci:gitlab"), ("BUILDKITE", "ci:buildkite"),
    ("CIRCLECI", "ci:circleci"), ("JENKINS_URL", "ci:jenkins"), ("CI", "ci:other"),
)
_CLOUD_DEV_VARS: tuple[tuple[str, str], ...] = (
    ("CODESPACES", "cloud_dev:codespaces"), ("GITPOD_WORKSPACE_ID", "cloud_dev:gitpod"),
    ("REPLICAS_WORKSPACE_ID", "cloud_dev:replicas"),  # the marker repo_setup.CLOUD_SANDBOX_MARKERS reads
)
# A hosted agent VM's own marker, per agent (the markers connected.config reads for its surface).
_CLOUD_AGENT_VARS: dict[str, tuple[str, str]] = {
    "claude_code": ("CLAUDE_CODE_REMOTE", "cloud_agent:claude_code"),
    "cursor": ("CURSOR_CODE_REMOTE", "cloud_agent:cursor"),
}
# The app Claude Code ran in: its entrypoint first, then the terminal that launched it.
_ENTRYPOINT_APPS = {"claude-vscode": "vscode", "claude-desktop": "claude_desktop", "remote_desktop": "claude_desktop",
                    "remote_mobile": "claude_mobile", "remote": "claude_web", "remote_web": "claude_web"}
_TERM_APPS = {"vscode": "vscode", "iTerm.app": "iterm2", "Apple_Terminal": "macos_terminal", "WezTerm": "wezterm",
              "ghostty": "ghostty", "WarpTerminal": "warp", "tmux": "tmux", "Hyper": "hyper", "kitty": "kitty"}
_HOST_RE = re.compile(r"^[a-z][a-z0-9_:]{0,40}\|(?:[a-z][a-z0-9_]{0,30})?$")


def agent_host_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _HOST_RE.match(value) else None


def claude_host(env: Mapping[str, str] | None) -> str | None:
    """``"<ran_on>|<app>"`` for a Claude Code session, from its hook environment; None when not known.

    ran_on: ``cloud_agent:claude_code`` (``CLAUDE_CODE_REMOTE``), ``ci:<name>``
    (a CI runner's own variable), ``cloud_dev:<name>`` (Codespaces, Gitpod,
    Replicas), else ``local`` (this machine: a laptop or a server the person
    runs, not a known cloud agent or CI). app (may be empty): from
    ``CLAUDE_CODE_ENTRYPOINT`` (``claude-vscode`` is VS Code), else
    ``TERMINAL_EMULATOR`` (JetBrains) or ``TERM_PROGRAM`` (iTerm2, VS Code's
    terminal...), else ``terminal`` for the plain ``cli`` entrypoint and ``sdk``
    for headless ``sdk-*``. Only these tokens are recorded, never an
    environment value. Never raises.
    """
    return agent_host("claude_code", env)


def agent_host(agent: str, env: Mapping[str, str] | None) -> str | None:
    """``"<ran_on>|<app>"`` for any hook-captured agent (see :func:`claude_host`); None when not known.

    A command hook runs in the agent's own environment, so the same markers
    hold for every agent: its hosted-VM marker where it has one
    (``_CLOUD_AGENT_VARS``), a CI runner's or cloud workspace's own variable,
    else ``local``. The app comes from Claude Code's entrypoint, else the
    terminal / JetBrains variables the hook inherited; when none says, it is
    empty (unknown), never inferred from the agent's name. Never raises.
    """
    if not isinstance(env, Mapping):
        return None
    try:
        entry = str(env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip() if agent == "claude_code" else ""
        marker = _CLOUD_AGENT_VARS.get(agent)
        if marker is not None and _env_flag(env.get(marker[0])):
            ran_on = marker[1]
        else:
            ran_on = next((token for var, token in (*_CI_VARS, *_CLOUD_DEV_VARS) if _env_flag(env.get(var))), "local")
        app = _ENTRYPOINT_APPS.get(entry)
        if app is None and not ran_on.startswith("cloud_agent"):
            if str(env.get("TERMINAL_EMULATOR") or "").startswith("JetBrains"):
                app = "jetbrains"
            else:
                app = _TERM_APPS.get(str(env.get("TERM_PROGRAM") or "").strip())
        if app is None:
            app = "terminal" if entry == "cli" else "sdk" if entry.startswith("sdk-") else ""
        return agent_host_or_none(f"{ran_on}|{app}")
    except Exception:
        return None


def codex_agent_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """``{"billing_basis"}`` when Codex's own login state settles it (``adapters/billing_basis``), else ``{}``."""
    billing = codex_billing_basis(env)
    return {} if billing == UNKNOWN else {"billing_basis": billing}


def agent_environment(agent: str, env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The hook-environment facts recorded for *agent*: all of them for Claude Code; for the
    others where it ran (``host``), and Codex's billing basis. None when there are none."""
    if agent == "claude_code":
        return claude_agent_env(env)
    out = codex_agent_env(env) if agent == "codex" else {}
    host = agent_host(agent, env)
    if host and host != "local|":  # "local" with no app is the default, not an observation
        out["host"] = host
    return out or None


def apply_agent_env(payload, agent_env: Mapping[str, str] | None) -> None:
    """Attach a validated agent environment to a hook payload (moved from ``claude_hooks``):
    Claude Code takes all of it, Codex its billing basis, every agent where it ran."""
    if not agent_env:
        return
    payload.agent_host = agent_host_or_none(agent_env.get("host"))
    if payload.agent not in ("claude_code", "codex"):
        return
    payload.agent_billing_basis = billing_basis_or_none(agent_env.get("billing_basis"))
    if payload.agent == "claude_code":
        payload.agent_provider = agent_provider_or_none(agent_env.get("provider"))
        payload.agent_surface = agent_surface_or_none(agent_env.get("surface"))
        payload.agent_cloud_session = cloud_session_or_none(agent_env.get("cloud_session"))


def note_agent_env(buf: dict, payload) -> None:
    """Fold a hook payload's agent environment into the session buffer (moved from ``claude_hooks._apply``)."""
    if payload.agent_provider and not buf.get("provider_current"):
        # First observation wins: a session's environment does not change.
        buf["provider_current"] = payload.agent_provider
        buf["provider_source"] = PROVIDER_SOURCE_AGENT_ENV
    if payload.agent_surface and not buf.get("surface"):
        buf["surface"] = payload.agent_surface
    if payload.agent_host and not buf.get("agent_host"):
        buf["agent_host"] = payload.agent_host  # first observation wins, like the surface
    if payload.agent_cloud_session and not buf.get("cloud_session_id"):
        # First observation wins: one cloud session per hosted VM.
        buf["cloud_session_id"] = payload.agent_cloud_session
    if payload.agent_billing_basis:  # a fresh environment observation replaces a carried one
        buf["billing_basis"] = payload.agent_billing_basis


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
    cloud_session = cloud_session_or_none(agent_env.get("cloud_session"))
    if cloud_session:
        parts.append(f"cloud_session={cloud_session}")
    billing = billing_basis_or_none(agent_env.get("billing_basis"))
    if billing and billing != UNKNOWN:
        parts.append(f"billing={billing}")
    host = agent_host_or_none(agent_env.get("host"))
    if host:
        parts.append(f"host={host}")
    return ";".join(parts) or None


def parse_agent_env(value: object) -> dict[str, str]:
    """Inverse of :func:`format_agent_env`; anything malformed is dropped, never repaired."""
    out: dict[str, str] = {}
    if not isinstance(value, str) or len(value) > 400:
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
        elif key == "cloud_session":
            cloud_session = cloud_session_or_none(raw.strip())
            if cloud_session:
                out["cloud_session"] = cloud_session
        elif key == "billing":
            billing = billing_basis_or_none(raw.strip())
            if billing and billing != UNKNOWN:
                out["billing_basis"] = billing
        elif key == "host" and (host := agent_host_or_none(raw.strip())):
            out["host"] = host
    return out
