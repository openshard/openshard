"""Zero-friction onboarding for Claude Code (PR8): detect, configure, report.

This module is the orchestration layer behind ``openshard setup`` and the
Claude Code section of ``openshard doctor``. It does not implement any new
installation logic -- it only calls the existing, independently-tested
adapters (``claude_mcp_install``, ``claude_hooks_install``) and turns their
results into one readiness judgement plus a short, actionable list of next
steps, so a new user never needs to know that ``mcp install claude`` exists.

Two entry points:

- ``detect_claude_integration()`` -- read-only. Never writes, never installs
  anything, never shells out to ``claude`` except a single ``mcp get`` query.
  Used by ``openshard doctor`` and ``openshard setup --agent``.
- ``run_setup()`` -- configures what it safely can, by calling the same
  ``install_claude_mcp`` / ``install_claude_hooks`` / ``install_claude_statusline``
  functions ``openshard mcp install claude`` uses, and is therefore just as
  idempotent and just as conservative about existing configuration (a custom
  status line is never replaced; unrelated hooks and settings are untouched).

Readiness has three tiers, not two, because a skipped status line is a real
(if minor) limitation that must never be reported as full success:

- ``"ready"``: MCP, hooks, and the status line are all configured. Receipts
  show model/cost/token data.
- ``"ready_partial"``: MCP and hooks are configured (capture works, Shards
  are recorded) but the status line was not -- almost always because the
  project already has its own ``statusLine``. Receipts stay Unknown/Not
  recorded for model/cost/token fields until that is resolved.
- ``"not_ready"``: capture itself is not working (no git repository, Claude
  Code CLI not found, or the MCP/hooks install failed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from openshard.adapters.claude_capture_client import ensure_service
from openshard.adapters.claude_capture_service import service_status
from openshard.adapters.claude_hooks_install import (
    HOOK_EVENTS,
    HTTP_EVENTS,
    SETTINGS_RELPATH,
    ClaudeHooksInstallResult,
    install_claude_hooks,
    install_claude_statusline,
    installed_events,
    installed_hook_port,
    is_openshard_hook,
    is_openshard_statusline,
    load_settings,
)
from openshard.adapters.claude_mcp_install import (
    ClaudeCliAvailability,
    ClaudeMcpInstallResult,
    _extract_repo_path,
    _same_repo,
    detect_claude_cli,
    find_repo_root,
    get_existing_entry,
    install_claude_mcp,
)

HISTORY_RELPATH = Path(".openshard") / "runs.jsonl"


def history_writable(base_dir: Path) -> bool:
    """Best-effort, read-only check that ``.openshard/`` can be written under *base_dir*.

    Never creates anything: if the directory already exists this checks it
    directly, otherwise it checks the parent -- matching what actually
    happens the first time a run or hook writes ``runs.jsonl``.
    """
    history_dir = base_dir / ".openshard"
    target = history_dir if history_dir.exists() else base_dir
    try:
        return os.access(target, os.W_OK)
    except OSError:
        return False


@dataclass
class ClaudeIntegrationStatus:
    claude_cli: ClaudeCliAvailability
    repo_root: Path | None
    mcp_configured: bool
    mcp_detail: str
    hook_events_installed: list[str]
    hook_events_missing: list[str]
    hooks_settings_error: str | None
    statusline_state: str  # "openshard" | "custom" | "absent"
    # PR9.5: the warm capture service the HTTP hooks talk to.
    capture_service: dict = field(default_factory=dict)  # service_status() snapshot
    hooks_port: int | None = None  # port the installed HTTP hooks target
    hooks_need_upgrade: bool = False  # pre-PR9.5 command-form hooks still installed
    capture_port_mismatch: bool = False  # hooks target a port the service is not on

    def to_dict(self) -> dict:
        return {
            "claude_cli_available": self.claude_cli.available,
            "claude_cli_path": self.claude_cli.path,
            "repo_root": str(self.repo_root) if self.repo_root else None,
            "mcp_configured": self.mcp_configured,
            "mcp_detail": self.mcp_detail,
            "hooks_configured": bool(self.hook_events_installed) and not self.hook_events_missing,
            "hook_events_installed": self.hook_events_installed,
            "hook_events_missing": self.hook_events_missing,
            "hooks_settings_error": self.hooks_settings_error,
            "statusline_state": self.statusline_state,
            "capture_service_running": bool(self.capture_service.get("running")),
            "capture_service_port": self.capture_service.get("port"),
            "hooks_port": self.hooks_port,
            "hooks_need_upgrade": self.hooks_need_upgrade,
            "capture_port_mismatch": self.capture_port_mismatch,
        }


def _http_events_installed(settings: dict) -> list[str]:
    """Events (of HTTP_EVENTS) whose installed OpenShard hook is the HTTP form."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    found: list[str] = []
    for event in HTTP_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if any(isinstance(h, dict) and h.get("type") == "http" and is_openshard_hook(h) for h in entries or []):
                found.append(event)
                break
    return found


def detect_claude_integration(repo_root: Path | None) -> ClaudeIntegrationStatus:
    """Read-only snapshot of Claude Code integration for *repo_root* (or None if not a repo).

    Never installs or writes anything. The only subprocess call is a single
    ``claude mcp get openshard`` query (skipped entirely when the CLI is not
    on PATH or there is no repository to check against).
    """
    claude_avail = detect_claude_cli()

    mcp_configured = False
    mcp_detail = "Not checked (no repository)."
    if repo_root is not None:
        if not claude_avail.available:
            mcp_detail = "Claude Code CLI not found."
        else:
            existing = get_existing_entry(claude_avail.path or "claude")
            if existing is None:
                mcp_detail = "Not configured."
            else:
                already_local = bool(existing.scope) and "local" in (existing.scope or "").lower()
                same_target = existing.command == "openshard" and _same_repo(
                    _extract_repo_path(existing.args_raw), repo_root
                )
                mcp_configured = already_local and same_target
                mcp_detail = (
                    "Configured for this repository."
                    if mcp_configured
                    else "Configured, but for a different repository or scope."
                )

    hook_events_installed: list[str] = []
    hook_events_missing: list[str] = list(HOOK_EVENTS)
    hooks_settings_error: str | None = None
    statusline_state = "absent"
    hooks_port: int | None = None
    hooks_need_upgrade = False

    if repo_root is not None:
        settings, err = load_settings(repo_root)
        if err:
            hooks_settings_error = err
        elif settings is not None:
            hook_events_installed = installed_events(settings)
            hook_events_missing = [e for e in HOOK_EVENTS if e not in hook_events_installed]
            hooks_port = installed_hook_port(settings)
            http_installed = _http_events_installed(settings)
            hooks_need_upgrade = bool(hook_events_installed) and any(
                e in hook_events_installed and e not in http_installed for e in HTTP_EVENTS
            )
            status_line = settings.get("statusLine")
            if is_openshard_statusline(status_line):
                statusline_state = "openshard"
            elif status_line is not None:
                statusline_state = "custom"

    service = service_status()
    mismatch = bool(service.get("running")) and hooks_port is not None and hooks_port != service.get("port")

    return ClaudeIntegrationStatus(
        claude_cli=claude_avail,
        repo_root=repo_root,
        mcp_configured=mcp_configured,
        mcp_detail=mcp_detail,
        hook_events_installed=hook_events_installed,
        hook_events_missing=hook_events_missing,
        hooks_settings_error=hooks_settings_error,
        statusline_state=statusline_state,
        capture_service=service,
        hooks_port=hooks_port,
        hooks_need_upgrade=hooks_need_upgrade,
        capture_port_mismatch=mismatch,
    )


def _mcp_result_dict(result: ClaudeMcpInstallResult | None) -> dict:
    if result is None:
        return {"status": "skipped"}
    return {
        "status": result.status,
        "message": result.message,
        "warnings": result.warnings,
    }


def _hooks_result_dict(result: ClaudeHooksInstallResult | None) -> dict:
    if result is None:
        return {"status": "skipped"}
    return {
        "status": result.status,
        "message": result.message,
        "warnings": result.warnings,
        "events": result.events,
    }


@dataclass
class SetupResult:
    repo_root: Path | None
    is_git: bool
    claude_cli: ClaudeCliAvailability
    history_path: Path
    history_writable: bool
    mcp: ClaudeMcpInstallResult | None
    hooks: ClaudeHooksInstallResult | None
    statusline: ClaudeHooksInstallResult | None
    readiness: str  # "ready" | "ready_partial" | "not_ready"
    next_steps: list[str] = field(default_factory=list)
    # PR9.5: {"state": running|started|disabled|unavailable, "port": int|None}
    capture_service: dict | None = None
    # PR12: Codex / OpenCode capture integrations, keyed by agent
    # ("codex", "opencode"); each configured only when its CLI is installed.
    agents: dict = field(default_factory=dict)

    @property
    def claude_configured(self) -> bool:
        return (
            self.mcp is not None and self.mcp.status != "error"
            and self.hooks is not None and self.hooks.status != "error"
        )

    def configured_agents(self) -> list[str]:
        """Agent keys whose capture integration is configured after this setup."""
        out: list[str] = ["claude_code"] if self.claude_configured else []
        out.extend(k for k, r in self.agents.items() if getattr(r, "configured", False))
        return out

    def to_dict(self) -> dict:
        return {
            "repo_root": str(self.repo_root) if self.repo_root else None,
            "is_git": self.is_git,
            "claude_cli_available": self.claude_cli.available,
            "claude_cli_path": self.claude_cli.path,
            "history_path": str(self.history_path),
            "history_writable": self.history_writable,
            "mcp": _mcp_result_dict(self.mcp),
            "hooks": _hooks_result_dict(self.hooks),
            "statusline": _hooks_result_dict(self.statusline),
            "capture_service": self.capture_service if self.capture_service is not None else {"status": "skipped"},
            "agents": {k: r.to_dict() for k, r in self.agents.items()},
            "configured_agents": self.configured_agents(),
            "readiness": self.readiness,
            "ready": self.readiness == "ready",
            "next_steps": self.next_steps,
        }


def ensure_capture_service() -> dict:
    """Start (or find) the capture service; ``{"state", "port"}``. Never raises."""
    try:
        port, state = ensure_service()
    except Exception:
        port, state = None, "unavailable"
    return {"state": state, "port": port}


def _claude_install_step(claude_avail: ClaudeCliAvailability) -> str:
    guidance = claude_avail.install_guidance[0] if claude_avail.install_guidance else None
    step = "Install the Claude Code CLI"
    if guidance:
        step += f" (e.g. `{guidance}`)"
    step += ", then re-run `openshard setup`."
    return step


def run_setup(*, repo_path: Path | None = None) -> SetupResult:
    """Configure capture for every supported agent installed on this machine.

    Claude Code (MCP + hooks + status line, exactly as `mcp install claude`
    does), then Codex hooks and the OpenCode plugin (PR12) for whichever of
    those CLIs is on PATH. Orchestrates the existing installers -- no
    duplicate installation logic lives here. Every step is idempotent, so
    calling this repeatedly (as ``openshard setup`` run twice will) never
    duplicates configuration and never re-writes a file whose content would
    not change.

    Readiness: ``ready`` when at least one detected agent is fully
    configured and nothing blocking was found; ``ready_partial`` when
    capture works but a limitation was recorded (a custom status line, a
    user-owned plugin file, a skipped agent); ``not_ready`` when no
    supported agent CLI is installed, the repository is not usable, or
    every attempted install failed.
    """
    from openshard.adapters.agent_setup import agent_label, detect_agent_cli, setup_detected_agents

    root = find_repo_root(repo_path)
    claude_avail = detect_claude_cli()
    base_dir = root if root is not None else (repo_path or Path.cwd())
    history_path = base_dir / HISTORY_RELPATH
    writable = history_writable(base_dir)

    next_steps: list[str] = []

    if root is None:
        return SetupResult(
            repo_root=None, is_git=False, claude_cli=claude_avail,
            history_path=history_path, history_writable=writable,
            mcp=None, hooks=None, statusline=None, readiness="not_ready",
            next_steps=["Run `openshard setup` again from inside a git repository "
                        "to enable coding-agent capture for a project."],
        )

    other_agents_present = [a for a in ("codex", "opencode") if detect_agent_cli(a)[0]]
    if not claude_avail.available and not other_agents_present:
        return SetupResult(
            repo_root=root, is_git=True, claude_cli=claude_avail,
            history_path=history_path, history_writable=writable,
            mcp=None, hooks=None, statusline=None, readiness="not_ready",
            next_steps=[
                _claude_install_step(claude_avail),
                "Or install Codex (`npm install -g @openai/codex`) or OpenCode "
                "(`npm install -g opencode-ai`); `openshard setup` configures whichever agents it finds.",
            ],
        )

    # The capture service first: the Claude HTTP hooks and the OpenCode
    # plugin written below must target the port it actually listens on
    # (normally the default; a different one only when the default is
    # taken by another program).
    service = ensure_capture_service()
    port = service.get("port") or None

    mcp_result: ClaudeMcpInstallResult | None = None
    hooks_result: ClaudeHooksInstallResult | None = None
    statusline_result: ClaudeHooksInstallResult | None = None
    claude_ok = False
    claude_limited = False
    if claude_avail.available:
        mcp_result = install_claude_mcp(repo_path=root)
        next_steps.extend(mcp_result.warnings)
        if mcp_result.status == "error":
            next_steps.append(mcp_result.message)
        else:
            hooks_result = install_claude_hooks(repo_root=root, port=port)
            next_steps.extend(hooks_result.warnings)
            if hooks_result.status == "error":
                next_steps.append(hooks_result.message)
            else:
                claude_ok = True
                statusline_result = install_claude_statusline(repo_root=root)
                if statusline_result.status == "skipped_existing":
                    claude_limited = True
                    next_steps.append(
                        f"Remove the custom statusLine entry in {SETTINGS_RELPATH.as_posix()} "
                        "(or leave it) to enable model/cost/token capture in receipts."
                    )
                elif statusline_result.status == "error":
                    claude_limited = True
                    next_steps.append(statusline_result.message)
    else:
        next_steps.append(
            "Claude Code CLI not found on PATH; Claude Code capture skipped ("
            + _claude_install_step(claude_avail).rstrip(".") + ")."
        )

    agents = setup_detected_agents(repo_root=root, port=port)
    agents_ok: list[str] = []
    agents_limited = False
    for key, result in agents.items():
        next_steps.extend(result.warnings)
        next_steps.extend(result.next_steps)
        if result.configured:
            agents_ok.append(key)
        elif result.status == "skipped":
            continue
        else:
            agents_limited = True
            if result.status == "error":
                next_steps.append(f"{agent_label(key)}: {result.message}")

    if not claude_ok and not agents_ok:
        readiness = "not_ready"
    elif claude_limited or agents_limited or (claude_avail.available and not claude_ok):
        readiness = "ready_partial"
    else:
        readiness = "ready"

    if readiness != "not_ready" and service.get("state") == "unavailable":
        # Hooks/plugin are installed and every session start retries the
        # service, but until it runs the HTTP hooks have nothing to talk
        # to -- capture would silently not happen, so this is not "ready".
        readiness = "not_ready"
        next_steps.append(
            "The local capture service could not be started (see ~/.openshard/claude-capture.log); "
            "run `openshard capture start`, then re-run `openshard setup`."
        )

    if not writable:
        # MCP/hooks/status line can all be configured correctly and captures
        # will still silently fail to record -- that is a blocking problem,
        # not a cosmetic one, so it overrides whatever tier the status line
        # alone would have produced.
        readiness = "not_ready"
        next_steps.append(
            f"{base_dir} is not writable; OpenShard cannot record local history here. "
            "Fix directory permissions, then re-run `openshard setup`."
        )

    return SetupResult(
        repo_root=root, is_git=True, claude_cli=claude_avail,
        history_path=history_path, history_writable=writable,
        mcp=mcp_result, hooks=hooks_result, statusline=statusline_result,
        readiness=readiness, next_steps=next_steps, capture_service=service, agents=agents,
    )
