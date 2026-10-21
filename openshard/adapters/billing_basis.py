"""How a coding agent's usage is paid for, from facts its hook process can observe.

``api_billed`` (metered against an API key), ``subscription`` (a Claude or
ChatGPT plan login) or ``unknown``. Only presence is ever checked: no
credential value is read into a result, logged or stored. Where a signal is
missing, contradicts another one, or the agent's documented credential
precedence does not settle which credential it used, the answer is
``unknown`` -- never a guess.

Claude Code (``claude_billing_basis``), following its documented
authentication precedence:

1. ``CLAUDE_CODE_USE_BEDROCK`` / ``_VERTEX`` / ``_FOUNDRY``, or an
   ``ANTHROPIC_BASE_URL`` other than ``https://api.anthropic.com``:
   ``unknown``. The cloud or gateway account bills it, on terms (on-demand,
   provisioned, committed) the hook cannot see.
2. A cloud session (``CLAUDE_CODE_REMOTE``): ``subscription``. Cloud sessions
   always use the account's subscription; API keys set there do not override it.
3. ``ANTHROPIC_AUTH_TOKEN``: ``unknown`` (a gateway's bearer token).
4. ``ANTHROPIC_API_KEY``: ``api_billed`` in a non-interactive session
   (``CLAUDE_CODE_ENTRYPOINT`` ``sdk-cli`` / ``sdk-ts`` / ``sdk-py``), where the
   key is always used. Interactive sessions ask once whether to use it, and
   that answer is not observable; but declining it leaves a subscription
   login as the only way to run, so ``api_billed`` when none exists: no
   ``CLAUDE_CODE_OAUTH_TOKEN``, no ``claudeAiOauth`` entry in
   ``.credentials.json``, and a readable global config (``.claude.json`` in
   ``CLAUDE_CONFIG_DIR`` or the home directory) without ``oauthAccount``.
   Any login signal, or a config that cannot be read: ``unknown``.
5. ``apiKeyHelper`` in the user, project or managed settings file:
   ``api_billed``; a gateway sign-in required there: ``unknown``.
6. ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``): ``subscription``.
7. An Anthropic profile or federation credential (``ANTHROPIC_PROFILE``,
   ``ANTHROPIC_FEDERATION_RULE_ID`` + ``ANTHROPIC_ORGANIZATION_ID``, or a
   profile in the Anthropic configuration directory): ``unknown``.
8. ``.credentials.json`` in the Claude configuration directory holding a
   claude.ai login with a ``subscriptionType``: ``subscription``. On macOS the
   login normally lives in the Keychain, which is not read: ``unknown``.

Codex (``codex_billing_basis``): ``auth.json`` in ``CODEX_HOME`` (default
``~/.codex``), its ``auth_mode`` field only: ``apikey`` -> ``api_billed``,
``chatgpt`` -> ``subscription`` unless ``OPENAI_API_KEY`` / ``CODEX_API_KEY``
is also set (conflict). A non-OpenAI ``model_provider`` in ``config.toml`` or
a non-OpenAI ``OPENAI_BASE_URL``: ``unknown``.

Every other agent: ``unknown``. Stdlib only and never raises: the
command-hook client calls it on every hook.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

API_BILLED = "api_billed"
SUBSCRIPTION = "subscription"
UNKNOWN = "unknown"
BILLING_BASES = frozenset({API_BILLED, SUBSCRIPTION, UNKNOWN})

_CLAUDE_CLOUD_FLAGS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
_NON_INTERACTIVE_ENTRYPOINTS = frozenset({"sdk-cli", "sdk-ts", "sdk-py"})
_CLAUDE_SUBSCRIPTION_TYPES = frozenset({"pro", "max", "team", "enterprise"})
_MANAGED_SETTINGS = {
    "darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
    "linux": "/etc/claude-code/managed-settings.json",
    "win32": r"C:\Program Files\ClaudeCode\managed-settings.json",
}
_MAX_FILE_BYTES = 1_000_000
_MAX_GLOBAL_CONFIG_BYTES = 32_000_000  # ~/.claude.json keeps per-project history and grows


def billing_basis_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value in BILLING_BASES else None


def _present(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    return isinstance(value, str) and bool(value.strip())


def _flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    return isinstance(value, str) and value.strip().lower() not in ("", "0", "false", "no", "off")


def _is_endpoint(url: object, host: str) -> bool:
    """True only for exactly ``https://<host>`` (an optional ``/`` or ``/v1``)."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == host and port in (None, 443)
            and parts.path.rstrip("/") in ("", "/v1") and not parts.query and not parts.username)


def _home(env: Mapping[str, str]) -> Path | None:
    """The agent's home directory as its own environment states it; None (read nothing) otherwise."""
    for name in (("USERPROFILE", "HOME") if os.name == "nt" else ("HOME",)):
        if _present(env, name):
            return Path(env[name].strip())
    return None


def _json_object(path: Path, limit: int = _MAX_FILE_BYTES) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _claude_settings_basis(env: Mapping[str, str], config_dir: Path | None) -> str | None:
    """``api_billed`` for an ``apiKeyHelper``, ``unknown`` for a required gateway sign-in, else None."""
    paths = [config_dir / "settings.json"] if config_dir is not None else []
    project = env.get("CLAUDE_PROJECT_DIR")
    if isinstance(project, str) and project.strip():
        root = Path(project.strip()) / ".claude"
        paths += [root / "settings.json", root / "settings.local.json"]
    managed = _MANAGED_SETTINGS.get("win32" if os.name == "nt" else sys.platform)
    if managed:
        paths.append(Path(managed))
    found = None
    for path in paths:
        settings = _json_object(path) or {}
        if settings.get("forceLoginMethod") == "gateway" or settings.get("forceLoginGatewayUrl"):
            return UNKNOWN
        if settings.get("apiKeyHelper"):
            found = API_BILLED
    return found


def _anthropic_profile_configured(env: Mapping[str, str], home: Path | None) -> bool:
    if _present(env, "ANTHROPIC_PROFILE") or (
        _present(env, "ANTHROPIC_FEDERATION_RULE_ID") and _present(env, "ANTHROPIC_ORGANIZATION_ID")
    ):
        return True
    if os.name == "nt":
        appdata = env.get("APPDATA")
        base = Path(appdata.strip()) / "Anthropic" if isinstance(appdata, str) and appdata.strip() else None
    else:
        base = home / ".config" / "anthropic" if home is not None else None
    if base is None:
        return False
    try:
        return (base / "active_config").is_file() or any((base / "configs").iterdir())
    except OSError:
        return False


def _claude_login_subscription(config_dir: Path | None) -> bool:
    """A claude.ai login in ``.credentials.json``: only ``subscriptionType`` is looked at."""
    if config_dir is None:
        return False
    oauth = (_json_object(config_dir / ".credentials.json") or {}).get("claudeAiOauth")
    return isinstance(oauth, dict) and oauth.get("subscriptionType") in _CLAUDE_SUBSCRIPTION_TYPES


def _no_claude_login(env: Mapping[str, str], config_dir: Path | None, global_config: Path | None) -> bool:
    """True only when no subscription login can exist for this session; any doubt is False.

    No ``CLAUDE_CODE_OAUTH_TOKEN``; no ``claudeAiOauth`` entry (any plan) in
    ``.credentials.json``; and Claude Code's global config (``.claude.json``)
    present, readable and without the ``oauthAccount`` it writes at a
    claude.ai login. That last check is what makes the answer hold on macOS,
    whose login token sits in the Keychain (never read). Key presence only.
    """
    if _present(env, "CLAUDE_CODE_OAUTH_TOKEN") or config_dir is None or global_config is None:
        return False
    credentials = config_dir / ".credentials.json"
    try:
        if credentials.exists():
            stored = _json_object(credentials)
            if stored is None or "claudeAiOauth" in stored:  # unreadable is not absent
                return False
    except OSError:
        return False
    config = _json_object(global_config, _MAX_GLOBAL_CONFIG_BYTES)
    return config is not None and "oauthAccount" not in config


def claude_billing_basis(env: Mapping[str, str] | None) -> str:
    """How this Claude Code session's usage is paid for (see the module docstring). Never raises."""
    if not isinstance(env, Mapping):
        return UNKNOWN
    try:
        base_url = env.get("ANTHROPIC_BASE_URL")
        if any(_flag(env, name) for name in _CLAUDE_CLOUD_FLAGS) or (
            _present(env, "ANTHROPIC_BASE_URL") and not _is_endpoint(base_url, "api.anthropic.com")
        ):
            return UNKNOWN
        if _flag(env, "CLAUDE_CODE_REMOTE"):
            return SUBSCRIPTION
        if _present(env, "ANTHROPIC_AUTH_TOKEN"):
            return UNKNOWN
        home = _home(env)
        custom_dir = Path(env["CLAUDE_CONFIG_DIR"].strip()) if _present(env, "CLAUDE_CONFIG_DIR") else None
        config_dir = custom_dir or (home / ".claude" if home is not None else None)
        if _present(env, "ANTHROPIC_API_KEY"):
            entrypoint = str(env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip()
            if entrypoint in _NON_INTERACTIVE_ENTRYPOINTS:
                return API_BILLED
            # Interactive: the key is used once approved, and the only way to decline it and
            # still run is a subscription login. None on the machine: the key is what bills.
            config_root = custom_dir or home
            global_config = config_root / ".claude.json" if config_root is not None else None
            return API_BILLED if _no_claude_login(env, config_dir, global_config) else UNKNOWN
        from_settings = _claude_settings_basis(env, config_dir)
        if from_settings is not None:
            return from_settings
        if _present(env, "CLAUDE_CODE_OAUTH_TOKEN"):
            return SUBSCRIPTION
        if _anthropic_profile_configured(env, home):
            return UNKNOWN
        return SUBSCRIPTION if _claude_login_subscription(config_dir) else UNKNOWN
    except Exception:
        return UNKNOWN


def _codex_custom_provider(codex_home: Path) -> bool:
    """True when ``config.toml`` names a ``model_provider`` other than ``openai``, anywhere."""
    path = codex_home / "config.toml"
    try:
        if not path.is_file():
            return False
        import tomllib

        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return True  # unreadable configuration: which provider it names is unknown
    tables = [config, *[p for p in (config.get("profiles") or {}).values() if isinstance(p, dict)]]
    return any(t.get("model_provider", "openai") != "openai" for t in tables)


def codex_billing_basis(env: Mapping[str, str] | None) -> str:
    """How this Codex session's usage is paid for (see the module docstring). Never raises."""
    if not isinstance(env, Mapping):
        return UNKNOWN
    try:
        if _present(env, "OPENAI_BASE_URL") and not _is_endpoint(env.get("OPENAI_BASE_URL"), "api.openai.com"):
            return UNKNOWN
        home = _home(env)
        codex_home = (Path(env["CODEX_HOME"].strip()) if _present(env, "CODEX_HOME")
                      else home / ".codex" if home is not None else None)
        if codex_home is None or _codex_custom_provider(codex_home):
            return UNKNOWN
        mode = (_json_object(codex_home / "auth.json") or {}).get("auth_mode")
        if mode == "apikey":
            return API_BILLED
        env_key = _present(env, "OPENAI_API_KEY") or _present(env, "CODEX_API_KEY")
        return SUBSCRIPTION if mode == "chatgpt" and not env_key else UNKNOWN
    except Exception:
        return UNKNOWN


__all__ = [
    "API_BILLED",
    "BILLING_BASES",
    "SUBSCRIPTION",
    "UNKNOWN",
    "billing_basis_or_none",
    "claude_billing_basis",
    "codex_billing_basis",
]
