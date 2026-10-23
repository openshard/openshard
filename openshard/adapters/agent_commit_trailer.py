"""The ``Openshard-Agent`` commit trailer on commits an observed agent session makes.

So "agent or human?" can be answered from git itself, ``openshard setup``
installs a ``prepare-commit-msg`` hook that adds one trailer, the same key
the cloud-receipts workflow and the merge gate already read::

    Openshard-Agent: Claude Code

Truth rules
-----------
* Evidence, not a guess. A commit is marked only when the process making it
  names an agent session in its environment (Claude Code exports
  ``CLAUDE_CODE_SESSION_ID`` to the commands it runs) *and* Openshard's
  capture service holds that same session, still open, for this repository
  (its staging buffer or queue under ``.openshard/claude_sessions/``).
  Either one alone adds nothing: a human commit carries no session id, and a
  session id Openshard never observed is only a claim.
* Unknown adds nothing: no session state, unreadable state, an agent whose
  commands get no session id (every agent but Claude Code today), a rebase
  or cherry-pick replaying someone else's commits, or any error.
* Cloud sessions are left to Claude: Claude Code on the web
  (``CLAUDE_CODE_REMOTE``) already adds its own ``Claude-Session`` trailer,
  which the merge gate reads and the cloud-receipts workflow corroborates;
  an ``Openshard-Agent`` line there would turn that corroboration into a
  declaration, so none is added.
* Never duplicated: a message that already has an ``Openshard-Agent``
  trailer (an amend, a rebased agent commit, a cloud agent's own trailer) is
  left alone.
* Fail open: the hook exits 0 whatever happens here, so a broken Openshard
  install never blocks a commit. The user's own hook, when chained, keeps
  its exit status.

Coexistence
-----------
Openshard writes only into the repository's own ``.git/hooks``. A
``core.hooksPath`` (husky, a shared or global hooks directory) or a
``prepare-commit-msg`` owned by lefthook or pre-commit is never rewritten:
setup prints the one line to add instead. An existing hand-written hook is
kept as ``prepare-commit-msg.pre-openshard`` and runs first.

Opt out with ``openshard setup --no-commit-trailers`` (writes
``git config openshard.commitTrailers false``), the same git setting by
hand (per repository or ``--global``), or ``OPENSHARD_COMMIT_TRAILERS=off``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from openshard.util.git import run_git

TRAILER_KEY = "Openshard-Agent"
HOOK_NAME = "prepare-commit-msg"
CHAINED_SUFFIX = ".pre-openshard"
HOOK_MARKER = "# openshard-agent-commit-trailer v1"
CONFIG_KEY = "openshard.commitTrailers"
ENV_OPT_OUT = "OPENSHARD_COMMIT_TRAILERS"
HOOK_COMMAND = "openshard hooks git-commit-msg"
MANUAL_LINE = f'{HOOK_COMMAND} "$1" "$2" "$3" || true'
OPT_OUT_COMMAND = "openshard setup --no-commit-trailers"

# (capture agent key, environment variable naming its session in the commands it runs).
# Only agents that export their session id to commands can be matched to an
# observed session; others get no trailer (unknown, not human).
AGENT_SESSION_ENV: tuple[tuple[str, str], ...] = (("claude_code", "CLAUDE_CODE_SESSION_ID"),)

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_FALSE = ("0", "false", "no", "off")
_MAX_WORKTREES = 32
_TRAILER_RE = re.compile(r"^" + re.escape(TRAILER_KEY) + r":[ \t]*\S", re.IGNORECASE | re.MULTILINE)
# Reflog actions that replay commits someone else may have made.
_REPLAY_ACTIONS = ("rebase", "cherry-pick")
_REPLAY_STATE = ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------


def opted_out(repo: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether the user turned the trailer off (environment, or git config in *repo* / globally). Never raises."""
    env = os.environ if env is None else env
    if str(env.get(ENV_OPT_OUT) or "").strip().lower() in _FALSE:
        return True
    value = run_git(repo, ["config", "--get", CONFIG_KEY])
    return value is not None and value.strip().lower() in _FALSE


def set_opt_out(repo: Path) -> bool:
    """Persist the opt-out for *repo* (``git config openshard.commitTrailers false``). Never raises."""
    return run_git(repo, ["config", CONFIG_KEY, "false"]) is not None


# ---------------------------------------------------------------------------
# Evidence: an observed agent session for this commit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedSession:
    agent: str
    label: str
    session_id: str
    repo_root: str


def _candidate_roots(repo: Path) -> list[Path]:
    """This checkout first, then the repository's other worktrees (a session may own a sibling)."""
    roots: list[Path] = []
    top = run_git(repo, ["rev-parse", "--show-toplevel"])
    if top and top.strip():
        roots.append(Path(top.strip()))
    listing = run_git(repo, ["worktree", "list", "--porcelain"]) or ""
    for line in listing.splitlines():
        if line.startswith("worktree ") and len(roots) < _MAX_WORKTREES:
            roots.append(Path(line[len("worktree "):].strip()))
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        try:
            key = os.path.normcase(str(root.resolve()))
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _session_open(root: Path, session_id: str, agent: str) -> bool:
    """Whether the capture service holds *session_id* of *agent*, not ended, under *root*. Never raises."""
    try:
        from openshard.adapters.claude_capture_service import QUEUE_SUFFIX, queue_key
        from openshard.adapters.claude_hooks import buffer_path, sessions_dir

        state_dir = root / ".openshard"
        directory = sessions_dir(root)
        # Symlinked or missing state is not evidence (see the merge gate's
        # rule on committed/symlinked .openshard history).
        if state_dir.is_symlink() or directory.is_symlink() or not directory.is_dir():
            return False
        buffer = buffer_path(root, session_id, agent)
        if buffer.is_file() and not buffer.is_symlink():
            data = json.loads(buffer.read_text(encoding="utf-8"))
            return (
                isinstance(data, dict) and data.get("session_id") == session_id
                and data.get("agent", agent) == agent and not data.get("ended")
            )
        key = queue_key(session_id, agent)
        queue = directory / f"{key}{QUEUE_SUFFIX}"
        if queue.is_file() and not queue.is_symlink():
            return True
        return any(p.is_file() and not p.is_symlink() for p in directory.glob(f"{key}.queue.*.jsonl"))
    except Exception:
        return False


def observed_session(repo: Path, env: Mapping[str, str] | None = None) -> ObservedSession | None:
    """The open, Openshard-observed agent session this commit is being made in, or None. Never raises."""
    env = os.environ if env is None else env
    try:
        from openshard.adapters.capture_agents import profile_for

        for agent, var in AGENT_SESSION_ENV:
            session_id = str(env.get(var) or "").strip()
            if not _SESSION_RE.match(session_id):
                continue
            for root in _candidate_roots(repo):
                if _session_open(root, session_id, agent):
                    return ObservedSession(agent, profile_for(agent).label, session_id, str(root))
    except Exception:
        return None
    return None


def _env_flag(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() not in ("", *_FALSE)


def _replaying(repo: Path, env: Mapping[str, str]) -> bool:
    """A rebase or cherry-pick is replaying commits (which may be someone else's)."""
    action = str(env.get("GIT_REFLOG_ACTION") or "").strip().lower()
    if action.startswith(_REPLAY_ACTIONS):
        return True
    for name in _REPLAY_STATE:
        path = run_git(repo, ["rev-parse", "--git-path", name])
        if path and path.strip():
            p = Path(path.strip())
            if not p.is_absolute():
                p = repo / p
            if p.exists():
                return True
    return False


def mark_commit_message(msg_file: Path, repo: Path | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """Add ``Openshard-Agent: <agent>`` to *msg_file* when the evidence holds; the line added, or None.

    Never raises: any failure adds nothing.
    """
    env = os.environ if env is None else env
    repo = repo if repo is not None else Path.cwd()
    try:
        if _env_flag(env.get("CLAUDE_CODE_REMOTE")) or opted_out(repo, env) or _replaying(repo, env):
            return None
        session = observed_session(repo, env)
        if session is None:
            return None
        if _TRAILER_RE.search(msg_file.read_text(encoding="utf-8", errors="replace")):
            return None
        line = f"{TRAILER_KEY}: {session.label}"
        out = subprocess.run(
            ["git", "interpret-trailers", "--in-place", "--if-exists", "doNothing", "--trailer", line, str(msg_file)],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
        return line if out.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The prepare-commit-msg hook: status, install, uninstall
# ---------------------------------------------------------------------------


def hook_script() -> str:
    """The hook Openshard writes (POSIX sh: git runs it with its own sh on Windows too). LF line endings."""
    env_test = " || ".join(f'[ -n "${{{var}:-}}" ]' for _, var in AGENT_SESSION_ENV)
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        "# Installed by `openshard setup`. Adds an Openshard-Agent trailer only to commits\n"
        "# made inside an agent session Openshard observed in this repository.\n"
        f"# Opt out: `{OPT_OUT_COMMAND}`. Never blocks a commit.\n"
        'hook_dir=$(dirname "$0")\n'
        f'if [ -x "$hook_dir/{HOOK_NAME}{CHAINED_SUFFIX}" ]; then\n'
        f'  "$hook_dir/{HOOK_NAME}{CHAINED_SUFFIX}" "$@" || exit $?\n'
        "fi\n"
        f"if {{ {env_test}; }} && command -v openshard >/dev/null 2>&1; then\n"
        f'  {HOOK_COMMAND} "$@" </dev/null >/dev/null 2>&1 || true\n'
        "fi\n"
        "exit 0\n"
    )


@dataclass
class CommitHookStatus:
    # installed | missing | opted_out | manual | not_git (status)
    # installed | updated | already_installed | chained | uninstalled | opted_out | manual | error (actions)
    state: str
    detail: str
    hook_path: str | None = None
    instruction: str | None = None  # the exact line to add when Openshard cannot install safely

    def to_dict(self) -> dict:
        return asdict(self)


def _git_path(repo: Path, args: list[str]) -> Path | None:
    out = run_git(repo, args)
    if not out or not out.strip():
        return None
    p = Path(out.strip())
    return p if p.is_absolute() else (repo / p)


def _plan(repo: Path) -> tuple[Path | None, CommitHookStatus | None]:
    """``(hook path, None)`` when Openshard may manage the hook, else ``(path?, manual/not_git status)``."""
    common = _git_path(repo, ["rev-parse", "--git-common-dir"])
    if common is None:
        return None, CommitHookStatus("not_git", "not a git repository")
    hooks_path = (run_git(repo, ["config", "--get", "core.hooksPath"]) or "").strip()
    if hooks_path:
        if ".husky" in hooks_path.replace("\\", "/"):
            return None, CommitHookStatus(
                "manual", f"husky manages git hooks (core.hooksPath={hooks_path})",
                instruction=f"echo '{MANUAL_LINE}' >> .husky/{HOOK_NAME}",
            )
        return None, CommitHookStatus(
            "manual", f"core.hooksPath is set ({hooks_path}); Openshard does not write there",
            instruction=f"Add this line to {hooks_path.rstrip('/')}/{HOOK_NAME}: {MANUAL_LINE}",
        )
    path = common / "hooks" / HOOK_NAME
    try:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        existing = ""
    if HOOK_MARKER in existing:
        return path, None
    if "lefthook" in existing:
        return path, CommitHookStatus(
            "manual", f"lefthook owns {HOOK_NAME}", str(path),
            instruction=(f'Add to lefthook.yml: {HOOK_NAME}: {{commands: {{openshard: '
                         f'{{run: "{HOOK_COMMAND} {{1}} {{2}} {{3}} || true"}}}}}}'),
        )
    if "pre-commit" in existing and "generated by pre-commit" in existing.lower():
        return path, CommitHookStatus(
            "manual", f"pre-commit owns {HOOK_NAME}", str(path),
            instruction=("Add to .pre-commit-config.yaml under a `repo: local` entry: "
                         f"{{id: openshard-agent-trailer, name: Openshard agent trailer, entry: {HOOK_COMMAND}, "
                         f"language: system, stages: [{HOOK_NAME}], always_run: true}}"),
        )
    if existing and path.with_name(HOOK_NAME + CHAINED_SUFFIX).exists():
        return path, CommitHookStatus(
            "manual", f"{HOOK_NAME} and {HOOK_NAME}{CHAINED_SUFFIX} both exist; not touching either", str(path),
            instruction=f"Add this line to {path}: {MANUAL_LINE}",
        )
    return path, None


def commit_hook_status(repo: Path | None, env: Mapping[str, str] | None = None) -> CommitHookStatus:
    """Read-only: is the trailer hook in place for *repo*? Never raises."""
    if repo is None:
        return CommitHookStatus("not_git", "not a git repository")
    try:
        path, blocked = _plan(repo)
        if blocked is not None and blocked.state == "not_git":
            return blocked
        if opted_out(repo, env):
            return CommitHookStatus("opted_out", f"off ({CONFIG_KEY}=false or {ENV_OPT_OUT}=off)")
        if blocked is not None:
            return blocked
        assert path is not None
        try:
            ours = path.is_file() and HOOK_MARKER in path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ours = False
        if ours:
            return CommitHookStatus("installed", "agent commits get an Openshard-Agent trailer", str(path))
        return CommitHookStatus(
            "missing", f"not installed; run `openshard setup` to enable, or `{OPT_OUT_COMMAND}` to turn off",
            str(path),
        )
    except (OSError, ValueError) as exc:
        return CommitHookStatus("error", f"could not read the hook state: {type(exc).__name__}")


def _write_hook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".openshard-tmp")
    tmp.write_bytes(hook_script().encode("utf-8"))
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, path)


def install_commit_hook(repo: Path | None, env: Mapping[str, str] | None = None) -> CommitHookStatus:
    """Install (or refresh) the trailer hook in *repo*'s own ``.git/hooks``. Idempotent; never raises."""
    if repo is None:
        return CommitHookStatus("not_git", "not a git repository")
    try:
        path, blocked = _plan(repo)
        if blocked is not None and blocked.state == "not_git":
            return blocked
        if opted_out(repo, env):
            return CommitHookStatus("opted_out", f"off ({CONFIG_KEY}=false or {ENV_OPT_OUT}=off)")
        if blocked is not None:
            return blocked
        assert path is not None
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        if existing is not None and HOOK_MARKER in existing:
            if existing == hook_script():
                return CommitHookStatus("already_installed", "agent commits get an Openshard-Agent trailer", str(path))
            _write_hook(path)
            return CommitHookStatus("updated", "agent commits get an Openshard-Agent trailer", str(path))
        if existing is not None:
            chained = path.with_name(HOOK_NAME + CHAINED_SUFFIX)
            os.replace(path, chained)
            _write_hook(path)
            return CommitHookStatus(
                "chained", f"your existing hook is kept as {chained.name} and still runs first", str(path),
            )
        _write_hook(path)
        return CommitHookStatus("installed", "agent commits get an Openshard-Agent trailer", str(path))
    except (OSError, ValueError) as exc:
        return CommitHookStatus(
            "error", f"could not install the commit trailer hook: {type(exc).__name__}",
            instruction=f"Add this line to .git/hooks/{HOOK_NAME}: {MANUAL_LINE}",
        )


def uninstall_commit_hook(repo: Path | None) -> CommitHookStatus:
    """Remove Openshard's hook and restore a chained one. Never touches a hook that is not Openshard's."""
    if repo is None:
        return CommitHookStatus("not_git", "not a git repository")
    try:
        common = _git_path(repo, ["rev-parse", "--git-common-dir"])
        if common is None:
            return CommitHookStatus("not_git", "not a git repository")
        path = common / "hooks" / HOOK_NAME
        if not (path.is_file() and HOOK_MARKER in path.read_text(encoding="utf-8", errors="replace")):
            return CommitHookStatus("opted_out", "no Openshard hook installed", str(path))
        chained = path.with_name(HOOK_NAME + CHAINED_SUFFIX)
        if chained.exists():
            os.replace(chained, path)
        else:
            path.unlink()
        return CommitHookStatus("uninstalled", "Openshard's commit trailer hook removed", str(path))
    except (OSError, ValueError) as exc:
        return CommitHookStatus("error", f"could not remove the commit trailer hook: {type(exc).__name__}")


def add_cloud_usage_trailers(msg_file: Path, repo: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    """In a Claude Code cloud session, add the committing agent's ``Openshard-Model`` /
    ``Openshard-Tokens-*`` trailers (``adapters/commit_usage_trailers.py``); True when added.

    A cloud commit's Receipt is built by the GitHub workflow from the commit
    message alone, so without these it has no model and no cost. Only Claude
    Code's own transcript counters are written, each message claimed by one
    commit; when no true number exists nothing is added. Laptop sessions are
    left alone: their hook-captured Receipt already holds the usage. Never raises.
    """
    env = os.environ if env is None else env
    repo = repo if repo is not None else Path.cwd()
    try:
        if not _env_flag(env.get("CLAUDE_CODE_REMOTE")) or not env.get("CLAUDE_CODE_SESSION_ID"):
            return False
        if opted_out(repo, env) or _replaying(repo, env):
            return False
        from openshard.adapters.commit_usage_trailers import (
            UsageUnavailable,
            append_trailers,
            usage_trailers,
        )

        try:
            usage_trailers(repo, session_id=str(env["CLAUDE_CODE_SESSION_ID"]), hints=(str(msg_file),),
                           deliver=lambda lines: append_trailers(msg_file, lines))
        except UsageUnavailable:
            return False
        return True
    except Exception:
        return False


def run_git_hook(argv: list[str], env: Mapping[str, str] | None = None) -> None:
    """``openshard hooks git-commit-msg <file> [source] [sha]``: mark the message if the evidence holds,
    and in a cloud session add its usage trailers. Never raises."""
    if argv and argv[0]:
        mark_commit_message(Path(argv[0]), None, env)
        add_cloud_usage_trailers(Path(argv[0]), None, env)


# ---------------------------------------------------------------------------
# setup / doctor
# ---------------------------------------------------------------------------

_SETUP_LABELS = {
    "installed": "installed", "updated": "updated", "already_installed": "already configured",
    "chained": "installed (your existing hook still runs first)", "opted_out": "off (opted out)",
    "manual": "not installed (a hook manager owns it; see below)", "error": "NOT installed (see below)",
}


def opt_out_from_setup(result: object, opt_out: bool) -> None:
    """``openshard setup --no-commit-trailers``: persist the opt-out and remove Openshard's hook. Never raises."""
    root = getattr(result, "repo_root", None)
    if opt_out and isinstance(root, Path):
        set_opt_out(root)
        uninstall_commit_hook(root)


def apply_to_setup(result: object) -> None:
    """After ``openshard setup`` / ``connect``: install the hook unless opted out. Never raises.

    Records the outcome on the setup result (``commit_trailers``) and adds the
    exact line to add to its next steps when a hook manager blocks a safe install.
    """
    root = getattr(result, "repo_root", None)
    if not isinstance(root, Path):
        return
    status = install_commit_hook(root)
    try:
        result.commit_trailers = status.to_dict()  # type: ignore[attr-defined]
        if status.instruction:
            result.next_steps.append(f"Agent commit trailers: {status.detail}. {status.instruction}")  # type: ignore[attr-defined]
    except AttributeError:
        pass


def echo_setup_line(result: object, echo) -> None:
    """The ``Commit trailers:`` line of ``openshard setup``'s output, when setup recorded one."""
    state = (getattr(result, "commit_trailers", None) or {}).get("state")
    if state in _SETUP_LABELS:
        echo(f"  Commit trailers: {_SETUP_LABELS[state]}")


def doctor_rows(status: CommitHookStatus) -> list[tuple[str, bool, str]]:
    """``openshard doctor``'s check row for the hook (none outside a git repository)."""
    if status.state == "not_git":
        return []
    label = "Agent commit trailers" + (" (off: opted out)" if status.state == "opted_out" else "")
    detail = status.detail + (f"; {status.instruction}" if status.instruction else "")
    return [(label, status.state in ("installed", "opted_out"), detail)]


__all__ = [
    "AGENT_SESSION_ENV",
    "CONFIG_KEY",
    "ENV_OPT_OUT",
    "HOOK_MARKER",
    "HOOK_NAME",
    "TRAILER_KEY",
    "CommitHookStatus",
    "ObservedSession",
    "apply_to_setup",
    "opt_out_from_setup",
    "commit_hook_status",
    "doctor_rows",
    "echo_setup_line",
    "hook_script",
    "install_commit_hook",
    "mark_commit_message",
    "observed_session",
    "opted_out",
    "run_git_hook",
    "set_opt_out",
    "uninstall_commit_hook",
]
