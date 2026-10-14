"""Herdr multi-agent attribution: declared identifiers only, never authority.

Herdr (a terminal workspace manager) runs each agent CLI in a pane and
exports ``HERDR_*`` variables to the processes in it. Two facts are derived:

* the pane an agent runs in (launch context): ``herdr.terminal`` and
  ``herdr.pane`` external ids, plus ``source: "herdr"``;
* the agent sessions an orchestrator delegated to (a captured shell command
  ``herdr agent prompt|send-keys|start``): ``herdr.delegated_session`` ids of
  the form ``<agent>:<agent session id>``, recorded on the *parent* Receipt.

Everything is best-effort and bounded: one ``herdr agent get`` call with a
short timeout, never raising, never copying anything but opaque ids (no
prompt text, no cwd, no terminal output). Workspace/pane ids are reused after
a Herdr restart; ``terminal_id`` and the agent's own session id are the
strong identifiers.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping

TERMINAL_NAMESPACE = "herdr.terminal"
PANE_NAMESPACE = "herdr.pane"
DELEGATED_NAMESPACE = "herdr.delegated_session"
SOURCE = "herdr"
GET_TIMEOUT_SECONDS = 2.0
_MAX_OUTPUT = 256 * 1024
_MAX_COMMAND = 8_000
_MAX_DELEGATIONS = 16
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}\Z")
_DELEGATING = {"prompt", "send-keys", "start"}
_VALUE_OPTIONS = {"--until", "--timeout", "--kind", "--pane"}
_SHELLS = {"sh", "bash", "zsh", "dash", "pwsh", "powershell", "cmd"}
_SHELL_FLAGS = {"-c", "-lc", "-command", "/c", "/k"}
_cache: dict[tuple[str, str], dict | None] = {}


def _herdr_binary(env: Mapping[str, str]) -> str | None:
    configured = env.get("HERDR_BIN_PATH")
    if isinstance(configured, str) and configured.strip() and os.path.isfile(configured.strip()):
        return configured.strip()
    return shutil.which("herdr")


def agent_get(
    target: str, env: Mapping[str, str] | None = None, *, timeout: float = GET_TIMEOUT_SECONDS, use_cache: bool = False
) -> dict | None:
    """Resolve *target* with one bounded ``herdr agent get``; None on any failure.

    Returns ``{"agent", "session", "terminal_id"}`` where ``session`` and
    ``terminal_id`` may be None when Herdr does not report them.
    """
    env = os.environ if env is None else env
    if not isinstance(target, str) or not _TARGET.fullmatch(target):
        return None
    binary = _herdr_binary(env)
    if binary is None:
        return None
    key = (binary, target)
    if use_cache and key in _cache:
        return _cache[key]
    result = _run_get(binary, target, env, timeout)
    if use_cache:  # a delegation target's session can change; only the launch pane is cached
        _cache[key] = result
    return result


def _run_get(binary: str, target: str, env: Mapping[str, str], timeout: float) -> dict | None:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, validated target, no shell
            [binary, "agent", "get", target],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False,
            env=dict(env), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
        if proc.returncode != 0 or len(proc.stdout) > _MAX_OUTPUT:
            return None
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
        agent = data["result"]["agent"]
    except Exception:  # absent/slow/garbled Herdr must never affect capture
        return None
    if not isinstance(agent, dict):
        return None
    session = agent.get("agent_session")
    session_id = session.get("value") if isinstance(session, dict) else None
    name = agent.get("agent")
    terminal = agent.get("terminal_id")
    return {
        "agent": name if isinstance(name, str) else None,
        "session": session_id if isinstance(session_id, str) else None,
        "terminal_id": terminal if isinstance(terminal, str) else None,
    }


def clear_cache() -> None:
    _cache.clear()


def launch_context(env: Mapping[str, str]) -> dict | None:
    """Correlation fragment for the pane *env* runs in, or None outside Herdr."""
    if env.get("HERDR_ENV") != "1":
        return None
    pane = env.get("HERDR_PANE_ID")
    if not isinstance(pane, str) or not pane.strip():
        return None
    pane = pane.strip()
    links: list[dict] = []
    info = agent_get(pane, env, use_cache=True)
    if info and info.get("terminal_id"):
        links.append({"namespace": TERMINAL_NAMESPACE, "id": info["terminal_id"]})
    workspace = (env.get("HERDR_WORKSPACE_ID") or pane.split(":", 1)[0]).strip()
    local = pane.split(":", 1)[-1]
    links.append({"namespace": PANE_NAMESPACE, "id": f"{workspace}/{local}"})
    return {"source": SOURCE, "external_ids": links}


def merge_launch_context(explicit: dict | None, fragment: dict | None, cap: int) -> dict | None:
    """Explicit context wins; Herdr fills ``source`` and extra ids within *cap*."""
    if not fragment:
        return explicit
    merged = dict(explicit or {})
    merged.setdefault("source", fragment["source"])
    links = list(merged.get("external_ids") or [])
    seen = {(item.get("namespace"), item.get("id")) for item in links}
    for item in fragment["external_ids"]:
        if len(links) >= cap:
            break
        if (item["namespace"], item["id"]) not in seen:
            links.append(item)
    merged["external_ids"] = links
    return merged


# ---------------------------------------------------------------------------
# Delegation command parsing
# ---------------------------------------------------------------------------


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    out = []
    for token in lexer:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            token = token[1:-1]
        out.append(token)
    return out


# Herdr exports its own binary as HERDR_BIN_PATH, so agents commonly invoke it
# through the variable (bash `$HERDR_BIN_PATH`/`${HERDR_BIN_PATH}`, PowerShell
# `$env:HERDR_BIN_PATH`); the captured command carries the reference unexpanded.
_HERDR_BIN_REF = re.compile(r"\$(?:env:)?\{?HERDR_BIN_PATH\}?", re.IGNORECASE)


def _program(token: str) -> str:
    if _HERDR_BIN_REF.fullmatch(token):
        return "herdr"
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def delegation_targets(command: object, _depth: int = 0) -> list[str]:
    """Targets of ``herdr agent prompt|send-keys|start`` invocations in *command*.

    ``get/list/read/wait/...`` are ignored. Anything not parseable yields no
    targets: no guessing.
    """
    if not isinstance(command, str) or "herdr" not in command.lower() or len(command) > _MAX_COMMAND:
        return []
    try:
        tokens = _tokens(command)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(ch in ";&|()<>" for ch in token):
            segments.append([])
        else:
            segments[-1].append(token)
    targets: list[str] = []
    for segment in segments:
        while segment and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[0]):
            segment = segment[1:]  # leading VAR=value assignments
        if segment and _program(segment[0]) in ("env", "command", "exec"):
            segment = [t for t in segment[1:] if not t.startswith("-") and "=" not in t]
        if not segment:
            continue
        program = _program(segment[0])
        if program in _SHELLS and _depth < 2:
            for i, token in enumerate(segment[1:-1], start=1):
                if token.lower() in _SHELL_FLAGS:
                    targets.extend(delegation_targets(" ".join(segment[i + 1:]).strip("'\""), _depth + 1))
            continue
        if program != "herdr" or len(segment) < 3 or segment[1] != "agent" or segment[2] not in _DELEGATING:
            continue
        target = _target_of(segment[2], segment[3:])
        if target and _TARGET.fullmatch(target) and target not in targets:
            targets.append(target)
    return targets


def _target_of(subcommand: str, args: list[str]) -> str | None:
    if subcommand == "start":
        for i, arg in enumerate(args):
            if arg == "--pane" and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith("--pane="):
                return arg[len("--pane="):]
        return None
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _VALUE_OPTIONS:
            skip = True
        elif arg == "--":
            continue
        elif not arg.startswith("-"):
            return arg
    return None


# Herdr agent kind -> Core capture agent name (capture_agents.AGENT_*). Kinds
# without a Core capture adapter are not recorded: their Receipt cannot exist.
CAPTURE_AGENT = {
    "claude": "claude_code", "codex": "codex", "cursor": "cursor", "opencode": "opencode",
    "agy": "antigravity", "grok": "grok_build", "hermes": "hermes",
}


def delegated_agents(command: object, env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """``(capture agent, raw session id)`` for each delegation target Herdr resolves.

    Raw ids stay in memory: ``history.correlation.herdr_delegated_sessions``
    hashes them into ``session_key`` values before anything is stored.
    """
    found: list[tuple[str, str]] = []
    for target in delegation_targets(command)[:_MAX_DELEGATIONS]:
        info = agent_get(target, env)
        capture_agent = CAPTURE_AGENT.get(info["agent"]) if info and info.get("agent") else None
        session = info.get("session") if info else None
        if capture_agent and isinstance(session, str) and (capture_agent, session) not in found:
            found.append((capture_agent, session))
    return found
