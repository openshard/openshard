"""Codex and OpenCode integration detection and setup (PR12).

The Codex/OpenCode counterpart of ``claude_setup``: read-only detection
for ``openshard doctor`` / ``openshard setup --agent``, and the install
orchestration ``openshard setup`` runs after the Claude Code step. No
installation logic lives here -- it calls ``codex_hooks_install`` and
``opencode_plugin_install`` and turns their results into one readiness
judgement per agent.

Readiness is judged *per agent*, independently: a developer with only
Codex installed is fully ready for Codex capture; a missing Claude Code
CLI is a fact about Claude Code, not about OpenShard. Every agent talks to
the same capture service, so the service is checked once and shared.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from openshard.adapters.codex_hooks_install import (
    HOOK_EVENTS as CODEX_HOOK_EVENTS,
)
from openshard.adapters.codex_hooks_install import (
    HOOKS_RELPATH as CODEX_HOOKS_RELPATH,
)
from openshard.adapters.codex_hooks_install import (
    install_codex_hooks,
    installed_codex_events,
    load_codex_hooks,
    uninstall_codex_hooks,
)
from openshard.adapters.opencode_plugin_install import (
    PLUGIN_RELPATH as OPENCODE_PLUGIN_RELPATH,
)
from openshard.adapters.opencode_plugin_install import (
    PLUGIN_VERSION,
    detect_plugin,
    install_opencode_plugin,
    uninstall_opencode_plugin,
)

AGENT_CODEX = "codex"
AGENT_OPENCODE = "opencode"
SUPPORTED_AGENTS: tuple[str, ...] = (AGENT_CODEX, AGENT_OPENCODE)

_CLI_NAMES: dict[str, str] = {AGENT_CODEX: "codex", AGENT_OPENCODE: "opencode"}
_LABELS: dict[str, str] = {AGENT_CODEX: "Codex", AGENT_OPENCODE: "OpenCode"}
_INSTALL_GUIDANCE: dict[str, str] = {
    AGENT_CODEX: "npm install -g @openai/codex",
    AGENT_OPENCODE: "npm install -g opencode-ai",
}


def agent_label(agent: str) -> str:
    return _LABELS.get(agent, agent)


def detect_agent_cli(agent: str) -> tuple[bool, str | None]:
    """``(available, path)`` for the agent's CLI on PATH. Never raises."""
    name = _CLI_NAMES.get(agent)
    if not name:
        return False, None
    try:
        found = shutil.which(name)
    except Exception:
        found = None
    return bool(found), found


@dataclass
class AgentIntegrationStatus:
    agent: str
    cli_available: bool
    cli_path: str | None
    repo_root: Path | None
    # "openshard" (configured), "partial" (some events / stale port),
    # "custom" (a user-owned file blocks install), "absent", "error"
    state: str
    detail: str
    config_relpath: str
    events_installed: list[str] = field(default_factory=list)
    events_missing: list[str] = field(default_factory=list)
    config_error: str | None = None
    port: int | None = None  # port an installed OpenCode plugin targets
    capture_port_mismatch: bool = False

    @property
    def configured(self) -> bool:
        return self.state == "openshard" and not self.capture_port_mismatch

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "cli_available": self.cli_available,
            "cli_path": self.cli_path,
            "configured": self.configured,
            "state": self.state,
            "detail": self.detail,
            "config_path": self.config_relpath,
            "events_installed": self.events_installed,
            "events_missing": self.events_missing,
            "config_error": self.config_error,
            "port": self.port,
            "capture_port_mismatch": self.capture_port_mismatch,
        }


def detect_codex_integration(repo_root: Path | None) -> AgentIntegrationStatus:
    """Read-only snapshot of the Codex hook integration for *repo_root*."""
    available, path = detect_agent_cli(AGENT_CODEX)
    rel = CODEX_HOOKS_RELPATH.as_posix()
    if repo_root is None:
        return AgentIntegrationStatus(
            AGENT_CODEX, available, path, None, "absent", "Not checked (no repository).", rel,
            events_missing=list(CODEX_HOOK_EVENTS),
        )
    config, err = load_codex_hooks(repo_root)
    if err or config is None:
        return AgentIntegrationStatus(
            AGENT_CODEX, available, path, repo_root, "error", err or "unreadable", rel,
            events_missing=list(CODEX_HOOK_EVENTS), config_error=err,
        )
    installed = installed_codex_events(config)
    missing = [e for e in CODEX_HOOK_EVENTS if e not in installed]
    if not installed:
        state, detail = "absent", "not configured"
    elif missing:
        state, detail = "partial", f"hooks missing for {', '.join(missing)}; run `openshard setup`"
    else:
        state, detail = "openshard", f"configured ({rel})"
    return AgentIntegrationStatus(
        AGENT_CODEX, available, path, repo_root, state, detail, rel,
        events_installed=installed, events_missing=missing,
    )


def detect_opencode_integration(repo_root: Path | None, *, service_port: int | None = None) -> AgentIntegrationStatus:
    """Read-only snapshot of the OpenCode plugin integration for *repo_root*."""
    available, path = detect_agent_cli(AGENT_OPENCODE)
    rel = OPENCODE_PLUGIN_RELPATH.as_posix()
    if repo_root is None:
        return AgentIntegrationStatus(
            AGENT_OPENCODE, available, path, None, "absent", "Not checked (no repository).", rel,
        )
    found = detect_plugin(repo_root)
    if found.get("error"):
        return AgentIntegrationStatus(
            AGENT_OPENCODE, available, path, repo_root, "error", str(found["error"]), rel,
            config_error=str(found["error"]),
        )
    state = str(found.get("state"))
    port = found.get("port")
    version = found.get("version")
    mismatch = False
    if state == "openshard":
        if version != PLUGIN_VERSION:
            state, detail = "partial", "older plugin version; run `openshard setup` to update it"
        else:
            detail = f"configured ({rel})"
        mismatch = service_port is not None and port is not None and port != service_port
    elif state == "custom":
        detail = f"{rel} exists but is not OpenShard's plugin; move it aside to enable capture"
    else:
        detail = "not configured"
    return AgentIntegrationStatus(
        AGENT_OPENCODE, available, path, repo_root, state, detail, rel,
        port=port if isinstance(port, int) else None, capture_port_mismatch=mismatch,
    )


def detect_agent_integrations(repo_root: Path | None, *, service_port: int | None = None) -> dict[str, AgentIntegrationStatus]:
    return {
        AGENT_CODEX: detect_codex_integration(repo_root),
        AGENT_OPENCODE: detect_opencode_integration(repo_root, service_port=service_port),
    }


@dataclass
class AgentSetupResult:
    agent: str
    cli_available: bool
    cli_path: str | None
    # "installed" | "updated" | "already_installed" | "skipped_existing" | "error" | "skipped" (CLI absent)
    status: str
    message: str
    warnings: list[str] = field(default_factory=list)
    events: dict[str, str] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return self.status in ("installed", "updated", "already_installed")

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "cli_available": self.cli_available,
            "cli_path": self.cli_path,
            "status": self.status,
            "configured": self.configured,
            "message": self.message,
            "warnings": self.warnings,
            "events": self.events,
            "next_steps": self.next_steps,
        }


def install_agent(agent: str, *, repo_root: Path, port: int | None = None) -> AgentSetupResult:
    """Configure one agent's capture integration for *repo_root* (idempotent). Never raises."""
    available, path = detect_agent_cli(agent)
    if agent == AGENT_CODEX:
        result = install_codex_hooks(repo_root=repo_root)
        steps: list[str] = []
        if result.status in ("installed", "updated"):
            steps.append(
                "Codex reviews new hooks once before running them: open Codex in this repository "
                "and approve the `openshard hooks codex` hooks under /hooks (Codex must trust the project)."
            )
    elif agent == AGENT_OPENCODE:
        result = install_opencode_plugin(repo_root=repo_root, port=port)
        steps = []
        if result.status == "skipped_existing":
            steps.append(result.message)
    else:
        return AgentSetupResult(agent, available, path, "error", f"unknown agent {agent!r}")
    if result.status == "error":
        steps.append(result.message)
    return AgentSetupResult(
        agent, available, path, result.status, result.message,
        warnings=list(result.warnings), events=dict(result.events), next_steps=steps,
    )


def uninstall_agent(agent: str, *, repo_root: Path) -> AgentSetupResult:
    """Remove one agent's OpenShard-owned capture integration. Never raises."""
    available, path = detect_agent_cli(agent)
    if agent == AGENT_CODEX:
        result = uninstall_codex_hooks(repo_root=repo_root)
    elif agent == AGENT_OPENCODE:
        result = uninstall_opencode_plugin(repo_root=repo_root)
    else:
        return AgentSetupResult(agent, available, path, "error", f"unknown agent {agent!r}")
    return AgentSetupResult(
        agent, available, path, result.status, result.message,
        warnings=list(result.warnings), events=dict(result.events),
    )


def setup_detected_agents(*, repo_root: Path, port: int | None = None) -> dict[str, AgentSetupResult]:
    """Configure every supported agent whose CLI is on PATH; skip the rest.

    Only *installed* agents are configured: writing a Codex hooks file or
    an OpenCode plugin into a repository whose developer has neither tool
    would be clutter, not capture. Returns one result per supported agent
    (``status="skipped"`` when the CLI is absent).
    """
    results: dict[str, AgentSetupResult] = {}
    for agent in SUPPORTED_AGENTS:
        available, path = detect_agent_cli(agent)
        if not available:
            results[agent] = AgentSetupResult(
                agent, False, None, "skipped",
                f"{agent_label(agent)} CLI not found on PATH; skipped (install it, e.g. "
                f"`{_INSTALL_GUIDANCE[agent]}`, then re-run `openshard setup`).",
            )
            continue
        results[agent] = install_agent(agent, repo_root=repo_root, port=port)
    return results
