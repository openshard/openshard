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
from openshard.adapters.cursor_hooks_install import (
    HOOK_EVENTS as CURSOR_HOOK_EVENTS,
)
from openshard.adapters.cursor_hooks_install import (
    HOOKS_RELPATH as CURSOR_HOOKS_RELPATH,
)
from openshard.adapters.cursor_hooks_install import (
    install_cursor_hooks,
    installed_cursor_events,
    load_cursor_hooks,
    uninstall_cursor_hooks,
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
AGENT_CURSOR = "cursor"
SUPPORTED_AGENTS: tuple[str, ...] = (AGENT_CODEX, AGENT_OPENCODE, AGENT_CURSOR)

# Executables that mean "this agent is installed", first found wins. Cursor
# is an IDE: its ``cursor`` shell command is added to PATH by the Windows
# installer but only on request on macOS/Linux, and ``cursor-agent`` is its
# CLI agent. Detection is PATH-only on purpose (deterministic, the same
# rule as every other agent); ``openshard capture install cursor`` works
# whether or not detection found it.
_CLI_NAMES: dict[str, tuple[str, ...]] = {
    AGENT_CODEX: ("codex",),
    AGENT_OPENCODE: ("opencode",),
    AGENT_CURSOR: ("cursor", "cursor-agent"),
}
_LABELS: dict[str, str] = {AGENT_CODEX: "Codex", AGENT_OPENCODE: "OpenCode", AGENT_CURSOR: "Cursor"}
_INSTALL_GUIDANCE: dict[str, str] = {
    AGENT_CODEX: "npm install -g @openai/codex",
    AGENT_OPENCODE: "npm install -g opencode-ai",
    AGENT_CURSOR: "install Cursor and enable its `cursor` shell command",
}
# Agents whose "not found" message is not "install it": Cursor may well be
# installed without its shell command on PATH.
_SKIPPED_MESSAGES: dict[str, str] = {
    AGENT_CURSOR: (
        "Cursor not found on PATH (`cursor` / `cursor-agent`); skipped. If you use Cursor, run "
        "`openshard capture install cursor` in this repository (or enable Cursor's `cursor` shell "
        "command and re-run `openshard setup`)."
    ),
}


def agent_label(agent: str) -> str:
    return _LABELS.get(agent, agent)


# The capture record fields that mark a run as OpenCode-originated (see
# opencode_plugin / history.event). Either is sufficient evidence that the
# OpenCode plugin actually delivered events into this repository.
_OPENCODE_RECORD_MARKERS: tuple[tuple[str, str], ...] = (
    ("executor", "opencode_plugin"),
    ("import_source", "opencode"),
)
_RUNS_RELPATH = Path(".openshard") / "runs.jsonl"
_MAX_RUNS_SCAN_BYTES = 8 * 1024 * 1024


def opencode_capture_observed(repo_root: Path | None) -> bool | None:
    """Whether OpenShard has an actual OpenCode capture recorded for *repo_root*.

    This is the difference between "the plugin file is installed" (structural)
    and "OpenCode has really delivered events here" (proven). It reads the
    repository's own ``.openshard/runs.jsonl`` -- persistent, per-repository,
    and impossible to fake without a real captured session -- and returns
    ``True`` if any recorded run is OpenCode-originated, ``False`` if the file
    exists but holds no such run, and ``None`` when it cannot tell (no history
    file yet, or it could not be read). Never raises.

    A ``False`` here next to an installed plugin is exactly the reported
    failure: OpenCode ran and edited the repo, but nothing was captured
    because the plugin never loaded (``--pure``, a desktop build that skips
    project plugins, a stalled dependency wait, or a stale plugin).
    """
    if repo_root is None:
        return None
    path = Path(repo_root) / _RUNS_RELPATH
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_RUNS_SCAN_BYTES:
            # Unusually large history: don't block doctor scanning it; treat as
            # "cannot cheaply prove" rather than risk a slow/again-unbounded read.
            return None
        import json as _json

        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and any(
                    entry.get(k) == v for k, v in _OPENCODE_RECORD_MARKERS
                ):
                    return True
        return False
    except OSError:
        return None


def detect_agent_cli(agent: str) -> tuple[bool, str | None]:
    """``(available, path)`` for the agent's CLI on PATH. Never raises."""
    names = _CLI_NAMES.get(agent)
    if not names:
        return False, None
    for name in names:
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found:
            return True, found
    return False, None


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
    # v0.4.5: whether an actual capture from this agent has been recorded for
    # this repository (proof of delivery, not just of a config file). True /
    # False / None (unknown -- not applicable or could not tell). Only the
    # OpenCode plugin, which runs inside OpenCode's own runtime and can silently
    # fail to load, sets this today; hook-based agents run `openshard` directly.
    capture_observed: bool | None = None

    @property
    def configured(self) -> bool:
        return self.state == "openshard" and not self.capture_port_mismatch

    @property
    def capture_verified(self) -> bool:
        """Configured *and* proven to deliver (a real capture exists)."""
        return self.configured and self.capture_observed is True

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "cli_available": self.cli_available,
            "cli_path": self.cli_path,
            "configured": self.configured,
            "capture_observed": self.capture_observed,
            "capture_verified": self.capture_verified,
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


def detect_opencode_integration(
    repo_root: Path | None,
    *,
    service_port: int | None = None,
    capture_observed: bool | None = None,
) -> AgentIntegrationStatus:
    """Read-only snapshot of the OpenCode plugin integration for *repo_root*.

    *capture_observed* is the evidence (from ``opencode_capture_observed``)
    that OpenCode has actually delivered events here; when the plugin is
    installed but nothing has been captured, the detail says so rather than
    implying the integration works. It is looked up here when not supplied.
    """
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
    if capture_observed is None:
        capture_observed = opencode_capture_observed(repo_root)
    state = str(found.get("state"))
    port = found.get("port")
    version = found.get("version")
    mismatch = False
    capability_state = str(found.get("capability_state") or "n/a")
    if state == "openshard":
        if version != PLUGIN_VERSION:
            state, detail = "partial", "older plugin version; run `openshard setup` to update it"
        elif capability_state in ("missing", "stale"):
            state, detail = "partial", (
                "plugin carries no valid capture credential (its events are refused by the service); "
                "run `openshard setup` to rewrite it"
            )
        elif capture_observed is True:
            detail = f"configured ({rel}); OpenCode capture verified in this repository"
        elif capture_observed is False:
            # The reported failure: the plugin file is present and valid but no
            # OpenCode session has ever been captured here. Say what is proven
            # and what is not, and name the likely reason -- OpenShard cannot
            # make OpenCode load the plugin, so "the file exists" is not "it works".
            detail = (
                f"plugin installed ({rel}) but no OpenCode capture recorded here yet. Run an OpenCode "
                "session in this repository to verify. If a completed session still records nothing, "
                "OpenCode is not loading the plugin (e.g. `--pure`, a desktop build that skips project "
                "plugins, or a stale plugin) -- re-run `openshard setup`."
            )
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
        capture_observed=capture_observed if state in ("openshard", "partial") else None,
    )


def detect_cursor_integration(repo_root: Path | None) -> AgentIntegrationStatus:
    """Read-only snapshot of the Cursor hook integration for *repo_root*."""
    available, path = detect_agent_cli(AGENT_CURSOR)
    rel = CURSOR_HOOKS_RELPATH.as_posix()
    if repo_root is None:
        return AgentIntegrationStatus(
            AGENT_CURSOR, available, path, None, "absent", "Not checked (no repository).", rel,
            events_missing=list(CURSOR_HOOK_EVENTS),
        )
    config, err = load_cursor_hooks(repo_root)
    if err or config is None:
        return AgentIntegrationStatus(
            AGENT_CURSOR, available, path, repo_root, "error", err or "unreadable", rel,
            events_missing=list(CURSOR_HOOK_EVENTS), config_error=err,
        )
    installed = installed_cursor_events(config)
    missing = [e for e in CURSOR_HOOK_EVENTS if e not in installed]
    if not installed:
        state, detail = "absent", "not configured"
    elif missing:
        state, detail = "partial", f"hooks missing for {', '.join(missing)}; run `openshard capture install cursor`"
    else:
        state, detail = "openshard", f"configured ({rel})"
    return AgentIntegrationStatus(
        AGENT_CURSOR, available, path, repo_root, state, detail, rel,
        events_installed=installed, events_missing=missing,
    )


def detect_agent_integrations(repo_root: Path | None, *, service_port: int | None = None) -> dict[str, AgentIntegrationStatus]:
    return {
        AGENT_CODEX: detect_codex_integration(repo_root),
        AGENT_OPENCODE: detect_opencode_integration(repo_root, service_port=service_port),
        AGENT_CURSOR: detect_cursor_integration(repo_root),
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
    elif agent == AGENT_CURSOR:
        result = install_cursor_hooks(repo_root=repo_root)
        steps = []
        if result.status in ("installed", "updated"):
            steps.append("Cursor reloads .cursor/hooks.json automatically; no restart is needed.")
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
    elif agent == AGENT_CURSOR:
        result = uninstall_cursor_hooks(repo_root=repo_root)
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
            message = _SKIPPED_MESSAGES.get(agent) or (
                f"{agent_label(agent)} CLI not found on PATH; skipped (install it, e.g. "
                f"`{_INSTALL_GUIDANCE[agent]}`, then re-run `openshard setup`)."
            )
            results[agent] = AgentSetupResult(agent, False, None, "skipped", message)
            continue
        results[agent] = install_agent(agent, repo_root=repo_root, port=port)
    return results
