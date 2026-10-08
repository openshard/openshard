"""First-run onboarding constants and state builders.

This module holds the *data* and *logic* for the onboarding surface
(``openshard init`` / ``doctor`` / ``config show``) so the CLI layer stays
thin. Nothing here makes provider/model calls or stores secrets — API keys are
read from the environment only and never written to config or printed.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# (key, label, description). Keys are stable; labels/descriptions are for humans.
MODES: list[tuple[str, str, str]] = [
    (
        "local_only",
        "Local-only",
        "Local-only mode: no API key required, limited to local help, planning, "
        "dry-run, receipt inspection, and PR comment generation.",
    ),
    (
        "native",
        "OpenShard Native",
        "OpenShard Native mode: drives real repo work through a provider; "
        "requires a provider API key in the environment.",
    ),
    (
        "external_planned",
        "External agent (planned)",
        "External agent mode: planned integration with external coding agents; "
        "not yet wired for execution.",
    ),
]

PROVIDERS: list[tuple[str, str, str]] = [
    (
        "openrouter",
        "OpenRouter",
        "OpenRouter: broad model access through one key. Recommended for trying "
        "many models, including free-cost ones. Not forced.",
    ),
    ("anthropic", "Anthropic", "Anthropic: direct API access using an Anthropic key."),
    ("openai", "OpenAI", "OpenAI: direct API access using an OpenAI key."),
    (
        "google",
        "Google",
        "Google: reached via OpenRouter today; direct provider not yet wired.",
    ),
    (
        "deepseek",
        "DeepSeek",
        "DeepSeek: reached via OpenRouter today; direct provider not yet wired.",
    ),
    (
        "moonshot",
        "Moonshot / Kimi",
        "Moonshot / Kimi: reached via OpenRouter today; direct provider not yet wired.",
    ),
    (
        "glm",
        "GLM",
        "GLM: reached via OpenRouter today; direct provider not yet wired.",
    ),
    ("other", "Other", "Other: record a provider preference without a wired backend."),
    ("skip", "Skip for now", "Skip for now: do not record a provider yet."),
]

MODEL_MODES: list[tuple[str, str, str]] = [
    (
        "free",
        "Free / trial",
        "OpenRouter free-model mode: requires OpenRouter setup, uses free-cost "
        "models where available, low limits, not recommended for serious work.",
    ),
    (
        "balanced",
        "Balanced default",
        "Balanced mode: sensible default models for everyday tasks.",
    ),
    (
        "serious",
        "Serious work",
        "Serious mode: use paid provider credits or direct API keys for real "
        "repo work.",
    ),
]

OUTPUT_MODES: list[tuple[str, str, str]] = [
    ("human", "Human", "Human-readable output by default."),
    ("agent_json", "Agent JSON", "Machine-readable JSON output for agents."),
    ("both", "Both", "Human output with machine-readable JSON available via --json."),
]

SAFETY_NOTES: list[str] = [
    "OpenShard may inspect repo files during runs.",
    "Receipts are stored locally by default (under .openshard/).",
    "Never commit secrets. API keys are read from the environment, not config.",
]

# Providers with a real, wired execution backend today.
IMPLEMENTED_PROVIDERS = frozenset({"openrouter", "anthropic", "openai"})

# Provider -> direct API-key env var. Providers reached via OpenRouter (or with
# no wired backend) map to None.
PROVIDER_ENV_VARS: dict[str, str | None] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": None,
    "deepseek": None,
    "moonshot": None,
    "glm": None,
    "other": None,
    "skip": None,
}

# The env vars `doctor` reports presence for (booleans only, never values).
ALL_API_KEY_ENV_VARS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_SECRET_GLOBS = ("*key*", "*token*", "*secret*", "*password*")
_REDACTED = "***REDACTED***"


def is_secret_key(key: str) -> bool:
    """Return True if *key* looks like it names a secret value."""
    lowered = key.lower()
    return any(fnmatch.fnmatch(lowered, pat) for pat in _SECRET_GLOBS)


def redact(obj: Any) -> Any:
    """Recursively mask values whose key looks secret.

    Only scalar values under a secret-looking key are masked; structure is
    preserved so the redacted config is still informative.
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and is_secret_key(k) and not isinstance(v, (dict, list)):
                out[k] = _REDACTED if v not in (None, "") else v
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def options_catalog() -> dict[str, Any]:
    """Return the supported onboarding choices for agent discovery."""

    def _opts(items: list[tuple[str, str, str]]) -> list[dict[str, str]]:
        return [{"key": k, "label": label, "description": desc} for k, label, desc in items]

    return {
        "modes": _opts(MODES),
        "providers": _opts(PROVIDERS),
        "model_modes": _opts(MODEL_MODES),
        "output_modes": _opts(OUTPUT_MODES),
        "implemented_providers": sorted(IMPLEMENTED_PROVIDERS),
        "safety_notes": list(SAFETY_NOTES),
    }


def api_key_present() -> dict[str, bool]:
    """Return ``{provider: bool}`` for each reported env var. Booleans only."""
    return {
        provider: bool(os.environ.get(env_var, "").strip())
        for provider, env_var in ALL_API_KEY_ENV_VARS.items()
    }


def any_api_key_present() -> bool:
    """Return True if any supported provider key is set in the environment."""
    return any(api_key_present().values())


def default_provider() -> str:
    """Pick a default provider from detected env keys (OpenRouter > Anthropic > OpenAI)."""
    present = api_key_present()
    for provider in ("openrouter", "anthropic", "openai"):
        if present.get(provider):
            return provider
    return "skip"


def detect_git_repo(cwd: Path | None = None) -> bool:
    """Return True if *cwd* (or its ancestors) contains a ``.git`` entry."""
    start = cwd if cwd is not None else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        return False
    for parent in (start, *start.parents):
        if (parent / ".git").exists():
            return True
    return False


def _display_path(config_path: Path | None, cwd: Path | None) -> str | None:
    """Return a safe, non-absolute display string for *config_path*."""
    if config_path is None:
        return None
    base = cwd if cwd is not None else Path.cwd()
    try:
        return config_path.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError):
        # Outside cwd (e.g. OPENSHARD_CONFIG elsewhere): expose only the filename
        # so we never leak an absolute local path.
        return config_path.name


def is_first_run() -> bool:
    """Return True if the user has never completed openshard init or onboarding.

    A first run means there is no real on-disk user config carrying an
    onboarding completion marker. The bundled default config and any API keys
    injected from the environment must never count as completed onboarding — so
    when no user config file exists, this is always a first run regardless of
    what ``load_config`` would synthesise from defaults/env.
    """
    from openshard.config.settings import find_config_path, load_config_safe

    # No on-disk user config -> definitely a first run. Guards against the bundled
    # default config (and env-injected provider keys) being treated as completed.
    if find_config_path() is None:
        return True

    config, is_valid, _ = load_config_safe()
    if not is_valid:
        return True
    ob = config.get("onboarding", {})
    return not bool(ob.get("completed_at"))


def build_state(
    *,
    version: str,
    config_found: bool,
    config_path: Path | None,
    config_valid: bool,
    onboarding: dict[str, Any],
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Build the shared onboarding state dict (the documented JSON shape).

    This is the single source of truth for warnings and next steps so ``init``,
    ``doctor`` and ``config show`` stay consistent.
    """
    mode = onboarding.get("mode")
    provider = onboarding.get("provider")
    model_mode = onboarding.get("model_mode")
    output_mode = onboarding.get("output_mode")
    # First-run onboarding fields (additive; absent in older configs).
    user_type = onboarding.get("user_type")
    executor = onboarding.get("executor")
    provider_route = onboarding.get("provider_route")
    safety_profile = onboarding.get("safety_profile")

    keys_present = api_key_present()
    any_key = any(keys_present.values())

    # api_key_present reflects the selected provider when it has a wired env var;
    # otherwise it reflects "any supported key set" so agents get a useful signal.
    if provider in IMPLEMENTED_PROVIDERS:
        selected_key_present = keys_present.get(provider, False)
    else:
        selected_key_present = any_key

    warnings: list[str] = []
    next_steps: list[str] = []

    if not config_valid:
        warnings.append(
            "Config file could not be parsed; using safe defaults. "
            "Run `openshard init` to recreate it."
        )

    if not config_found:
        # Receipt capture (`openshard setup` + a coding agent) needs no
        # configuration; the config file is for OpenShard's own executor.
        next_steps.append(
            "Receipt capture needs no configuration. To run tasks with OpenShard's own "
            "executor (`openshard run`, `openshard osn run`), run `openshard init` to create "
            "your configuration."
        )

    # Provider selected but its direct key is missing.
    if provider in IMPLEMENTED_PROVIDERS and not keys_present.get(provider, False):
        env_var = PROVIDER_ENV_VARS.get(provider)
        warnings.append(f"Provider '{provider}' selected but {env_var} is not set.")
        next_steps.append(f"export {env_var}=your_key_here")

    # Providers reached via OpenRouter today.
    if provider in ("google", "deepseek", "moonshot", "glm") and not keys_present.get(
        "openrouter", False
    ):
        warnings.append(
            f"Provider '{provider}' is reached via OpenRouter today; "
            "OPENROUTER_API_KEY is not set."
        )
        next_steps.append("export OPENROUTER_API_KEY=your_key_here")

    # Free-model mode is an OpenRouter feature.
    if model_mode == "free" and provider not in (None, "openrouter", "skip"):
        warnings.append(
            "OpenRouter free-model mode: requires OpenRouter setup, uses free-cost "
            "models where available, low limits, not recommended for serious work."
        )

    # No supported key at all -> local-only.
    if not any_key:
        next_steps.append(
            "Local-only mode (no API key): receipt capture, inspection and history work fully; "
            "OpenShard's own executor is limited to local help, planning, dry-run and PR comment "
            "generation."
        )
        if mode not in (None, "local_only"):
            warnings.append(
                "No supported API key detected; OpenShard is limited to local-only "
                "mode until a provider key is set."
            )

    return {
        "openshard_version": version,
        "config_found": config_found,
        "config_path_display": _display_path(config_path, cwd),
        "config_valid": config_valid,
        "mode": mode,
        "provider": provider,
        "model_mode": model_mode,
        "api_key_present": selected_key_present,
        "output_mode": output_mode,
        "user_type": user_type,
        "executor": executor,
        "provider_route": provider_route,
        "safety_profile": safety_profile,
        "warnings": warnings,
        "next_steps": next_steps,
    }
