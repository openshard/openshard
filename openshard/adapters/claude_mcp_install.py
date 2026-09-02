"""Claude Code MCP installation for OpenShard.

Registers the local OpenShard MCP server (``openshard mcp serve``) with
Claude Code's own CLI, so Claude Code can call OpenShard's read-only
history tools for one specific repository:

    openshard mcp install claude
        -> `claude mcp add` (local scope, this repo only)
        -> Claude Code launches `openshard mcp serve --repo-path <repo>`

Design constraints:
- Never edits Claude Code's config file directly; always goes through the
  `claude` CLI's own `mcp add` / `mcp get` / `mcp remove` subcommands, since
  that is Claude Code's supported configuration mechanism.
- Local scope only (see ``SCOPE``): private to this user, bound to this one
  repository directory, and never committed to the repository's git history.
  Project scope (`.mcp.json`, checked into git) would bake this machine's
  absolute repo path into a file every teammate's checkout shares -- wrong
  since that path differs per clone. User scope would make the server
  available in every project on this machine, not just this repository.
- Idempotent: re-running never creates a duplicate `openshard` entry --
  an unchanged existing entry is left alone; a stale one (different repo
  path) is replaced in place.
- Never raises from the public functions here; the CLI layer decides how
  to surface a failure. All subprocess calls to `claude` are argv lists
  (no shell), so paths containing spaces need no special handling.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SERVER_NAME = "openshard"
SCOPE = "local"
MCP_TOOLS: tuple[str, ...] = (
    "recent_shards",
    "get_shard",
    "get_receipt",
    "search_history",
    "relevant_context",
)

_CLAUDE_TIMEOUT_SECONDS = 15.0

_INSTALL_GUIDANCE_WIN32: list[str] = [
    "irm https://claude.ai/install.ps1 | iex",
    "winget install Anthropic.ClaudeCode",
]
_INSTALL_GUIDANCE_DARWIN: list[str] = [
    "curl -fsSL https://claude.ai/install.sh | bash",
    "brew install --cask claude-code",
]
_INSTALL_GUIDANCE_LINUX: list[str] = [
    "curl -fsSL https://claude.ai/install.sh | bash",
]
_INSTALL_GUIDANCE_ALL: list[str] = [
    "curl -fsSL https://claude.ai/install.sh | bash",
    "irm https://claude.ai/install.ps1 | iex",
]


def get_claude_install_guidance(platform: str | None = None) -> list[str]:
    """Return platform-appropriate install options for the Claude Code CLI."""
    plat = platform if platform is not None else sys.platform
    if plat == "win32":
        return list(_INSTALL_GUIDANCE_WIN32)
    if plat == "darwin":
        return list(_INSTALL_GUIDANCE_DARWIN)
    if plat.startswith("linux"):
        return list(_INSTALL_GUIDANCE_LINUX)
    return list(_INSTALL_GUIDANCE_ALL)


@dataclass
class ClaudeCliAvailability:
    available: bool
    path: str | None
    reason: str | None
    install_guidance: list[str] = field(default_factory=list)


def detect_claude_cli() -> ClaudeCliAvailability:
    """Detect whether the Claude Code CLI (`claude`) is available. Never raises."""
    found = shutil.which("claude")
    if found:
        return ClaudeCliAvailability(available=True, path=found, reason=None)
    return ClaudeCliAvailability(
        available=False,
        path=None,
        reason="Claude Code CLI ('claude') not found on PATH.",
        install_guidance=get_claude_install_guidance(),
    )


@dataclass
class OpenShardCliAvailability:
    available: bool
    path: str | None


def detect_openshard_cli() -> OpenShardCliAvailability:
    """Detect whether the installed `openshard` console script is on PATH. Never raises."""
    found = shutil.which("openshard")
    return OpenShardCliAvailability(available=bool(found), path=found)


def mcp_extra_installed() -> bool:
    """Return True when the optional `mcp` package (needed by `openshard mcp serve`) is importable."""
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default cwd) to the nearest directory containing `.git`.

    Returns None when no such directory is found. Never raises.
    """
    cur = start if start is not None else Path.cwd()
    try:
        cur = cur.resolve()
    except OSError:
        return None
    for parent in (cur, *cur.parents):
        try:
            if (parent / ".git").exists():
                return parent
        except OSError:
            continue
    return None


def build_server_argv(repo_root: Path) -> list[str]:
    """Return the argv Claude Code should launch to start the OpenShard MCP server for *repo_root*.

    Uses the bare `openshard` command (resolved via PATH at launch time) rather
    than an absolute interpreter/source path, so the configuration keeps
    working after a normal pip/pipx install regardless of which virtualenv
    ran `mcp install claude`.
    """
    return ["openshard", "mcp", "serve", "--repo-path", str(repo_root)]


@dataclass
class ClaudeMcpEntry:
    scope: str | None
    command: str | None
    # Raw, un-tokenized "Args:" line from `claude mcp get`. Claude Code's text
    # output never quotes a value containing spaces (a path with a space and
    # a genuine extra arg are indistinguishable once space-joined), so this
    # is parsed with knowledge of our own fixed args shape (see
    # _extract_repo_path) rather than a generic shell tokenizer -- shlex in
    # particular would also mis-parse Windows backslash paths as escapes.
    args_raw: str | None


def _parse_get_output(output: str) -> ClaudeMcpEntry:
    scope: str | None = None
    command: str | None = None
    args_raw: str | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Scope:"):
            scope = stripped[len("Scope:") :].strip()
        elif stripped.startswith("Command:"):
            command = stripped[len("Command:") :].strip()
        elif stripped.startswith("Args:"):
            args_raw = stripped[len("Args:") :].strip()
    return ClaudeMcpEntry(scope=scope, command=command, args_raw=args_raw)


def get_existing_entry(claude_bin: str, name: str = SERVER_NAME) -> ClaudeMcpEntry | None:
    """Return the currently configured `claude mcp` entry for *name*, or None. Never raises."""
    try:
        result = subprocess.run(
            [claude_bin, "mcp", "get", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CLAUDE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _parse_get_output(result.stdout)


_REPO_PATH_FLAG = "--repo-path"


def _extract_repo_path(args_raw: str | None) -> str | None:
    """Pull the ``--repo-path`` value out of a raw ``Args:`` line.

    Takes everything after the flag verbatim (no tokenizing) since our own
    launch command never appends anything after ``--repo-path`` -- this is
    the only way to correctly recover a repo path containing spaces from
    Claude Code's unquoted, space-joined ``Args:`` display.
    """
    if not args_raw:
        return None
    idx = args_raw.find(_REPO_PATH_FLAG)
    if idx == -1:
        return None
    value = args_raw[idx + len(_REPO_PATH_FLAG) :].strip()
    return value or None


def _same_repo(candidate: str | None, repo_root: Path) -> bool:
    if not candidate:
        return False
    try:
        return Path(candidate).resolve() == repo_root.resolve()
    except OSError:
        return False


@dataclass
class ClaudeMcpInstallResult:
    status: str  # "installed" | "updated" | "already_installed" | "error"
    repo_root: Path | None
    repo_identity: str | None
    command: list[str] | None
    message: str
    warnings: list[str] = field(default_factory=list)


def _error(message: str) -> ClaudeMcpInstallResult:
    return ClaudeMcpInstallResult(
        status="error", repo_root=None, repo_identity=None, command=None,
        message=message, warnings=[],
    )


def install_claude_mcp(*, repo_path: Path | None = None) -> ClaudeMcpInstallResult:
    """Configure Claude Code (local scope) to launch the OpenShard MCP server for this repo.

    Idempotent: a matching existing entry is left untouched; a stale one
    (e.g. pointing at a different repo path) is replaced. Never raises --
    every failure mode is reported via ``ClaudeMcpInstallResult(status="error", ...)``.
    """
    warnings: list[str] = []

    claude_avail = detect_claude_cli()
    if not claude_avail.available:
        guidance = "; ".join(claude_avail.install_guidance)
        return _error(
            f"{claude_avail.reason} Install it, e.g.: {guidance}"
            if guidance else claude_avail.reason or "Claude Code CLI not found."
        )

    openshard_avail = detect_openshard_cli()
    if not openshard_avail.available:
        return _error(
            "The 'openshard' executable was not found on PATH. Claude Code needs to "
            "be able to launch it by name -- install OpenShard so its console script "
            "is on PATH (e.g. `pip install openshard`), then retry."
        )

    root = find_repo_root(repo_path)
    if root is None:
        where = f" ({repo_path})" if repo_path is not None else ""
        return _error(f"Not inside a git repository{where}. Run this from within a repository.")

    if not mcp_extra_installed():
        warnings.append(
            "The 'mcp' extra is not installed; 'openshard mcp serve' will fail to start "
            "until you run: pip install 'openshard[mcp]'"
        )

    from openshard.history.repo_identity import capture_repo_identity

    repo_identity = capture_repo_identity(root)
    argv = build_server_argv(root)
    claude_bin = claude_avail.path or "claude"

    existing = get_existing_entry(claude_bin)
    is_update = False
    if existing is not None:
        already_local = bool(existing.scope) and "local" in (existing.scope or "").lower()
        same_target = existing.command == "openshard" and _same_repo(
            _extract_repo_path(existing.args_raw), root
        )
        if already_local and same_target:
            return ClaudeMcpInstallResult(
                status="already_installed", repo_root=root, repo_identity=repo_identity,
                command=argv, message="Already configured for this repository.",
                warnings=warnings,
            )
        if already_local:
            is_update = True
            try:
                subprocess.run(
                    [claude_bin, "mcp", "remove", SERVER_NAME, "-s", SCOPE],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=_CLAUDE_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                return _error(f"Failed to replace existing Claude Code MCP configuration: {exc}")

    try:
        add_result = subprocess.run(
            [claude_bin, "mcp", "add", "--scope", SCOPE, SERVER_NAME, "--", *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CLAUDE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return _error(f"Failed to run 'claude mcp add': {exc}")

    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout or "").strip()
        return _error(f"Claude Code rejected the MCP configuration: {detail or 'unknown error'}")

    return ClaudeMcpInstallResult(
        status="updated" if is_update else "installed",
        repo_root=root,
        repo_identity=repo_identity,
        command=argv,
        message="Configured.",
        warnings=warnings,
    )
