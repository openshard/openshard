"""``openshard setup --repo``: committable, repository-level config for cloud coding agents.

Cloud agents (Claude Code on the web, Cursor cloud agents, the GitHub Copilot
cloud agent, OpenHands, a devcontainer/Codespace) start from a fresh clone and
never see user-level config (``~/.claude``, ``~/.cursor`` ...), and the local
installers write per-user, git-excluded files. This module writes the files a
team commits so those agents pick OpenShard up from the repository alone.

Rules
-----
* Merge, never clobber: other hooks, keys, steps and lines are kept; a file
  that cannot be parsed is reported and left untouched. Only entries carrying
  ``REPO_HOOK_MARKER`` / ``SETUP_MARKER`` (or OpenShard's own local hook
  entries, which the repository form replaces) are ever changed.
* Idempotent: a second run reports ``unchanged`` and writes nothing.
  ``dry_run`` plans everything and writes nothing.
* No credentials: the files name environment variables, never values. A
  file whose new content looks like it holds an OpenShard credential is not
  written. Remote capture links only when ``OPENSHARD_REMOTE_CAPTURE_URL`` /
  ``OPENSHARD_REMOTE_TOKEN`` are set in the environment (``openshard remote
  attach``, or the hook path's own environment fallback); otherwise capture
  stays local to wherever the agent runs, and the setup step says so.
* Fail open: every hook command exits 0 and never blocks the agent. When
  OpenShard is not installed the event is skipped with a note on stderr (a
  gap, not a failure). Only in a recognised cloud sandbox
  (``CLOUD_SANDBOX_MARKERS``: Claude Code on the web, Replicas, E2B, GitHub
  Codespaces, Gitpod) or with ``OPENSHARD_REPO_BOOTSTRAP=1`` does the Claude
  SessionStart hook start a background install; a teammate's machine never
  gets one silently. ``OPENSHARD_REPO_BOOTSTRAP=0`` turns it off anywhere.

Copilot and OpenHands have no OpenShard hook adapter in Core: their hooks only
deliver what OpenShard already recorded in that environment (for example an
``openshard verify`` attestation). Their sessions are not captured event by
event, and the output says so.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from openshard.adapters.claude_hooks_install import (
    HOOK_SPECS as CLAUDE_HOOK_SPECS,
)
from openshard.adapters.claude_hooks_install import (
    SETTINGS_RELPATH as CLAUDE_LOCAL_SETTINGS,
)
from openshard.adapters.claude_hooks_install import (
    TRANSPORT_COMMAND,
    HookSpec,
    is_openshard_hook,
    merge_openshard_hooks,
)
from openshard.adapters.cursor_hooks_install import (
    HOOK_SPECS as CURSOR_HOOK_SPECS,
)
from openshard.adapters.cursor_hooks_install import (
    HOOKS_FILE_VERSION as CURSOR_HOOKS_VERSION,
)
from openshard.adapters.cursor_hooks_install import (
    REPO_HOOK_MARKER,
    is_openshard_cursor_hook,
)

FILE_AGENTS: tuple[str, ...] = ("claude", "cursor", "copilot", "openhands", "devcontainer")
SNIPPET_AGENTS: tuple[str, ...] = ("devin", "amp", "replicas", "boat")
SETUP_MARKER = "OPENSHARD_REPO_SETUP=1"
BLOCK_BEGIN = "# >>> openshard repo setup >>>"
BLOCK_END = "# <<< openshard repo setup <<<"
COPILOT_JOB = "copilot-setup-steps"
COPILOT_STEP_NAME = "Install OpenShard"

CLAUDE_SETTINGS = Path(".claude") / "settings.json"
CURSOR_HOOKS = Path(".cursor") / "hooks.json"
CURSOR_ENV = Path(".cursor") / "environment.json"
COPILOT_WORKFLOW = Path(".github") / "workflows" / "copilot-setup-steps.yml"
COPILOT_HOOKS = Path(".github") / "hooks" / "openshard.json"
OPENHANDS_SETUP = Path(".openhands") / "setup.sh"
OPENHANDS_HOOKS = Path(".openhands") / "hooks.json"
DEVCONTAINER_FILES = (Path(".devcontainer") / "devcontainer.json", Path(".devcontainer.json"))

# Anything that looks like an OpenShard credential: remote (osr_), connected
# (osc_), API key (osk_), or a repository capture capability (r2.).
_CREDENTIAL_RE = re.compile(r"(?<![A-Za-z0-9])(?:os[rck]_[A-Za-z0-9_-]{8,}|r2\.[A-Za-z0-9_-]{16,})")
_GIT_TIMEOUT = 5.0

# --- shell -----------------------------------------------------------------
# No single quotes anywhere below: hook commands are wrapped in sh -c '...'.

_RESOLVE = 'OS=$(command -v openshard 2>/dev/null || echo "$HOME/.local/bin/openshard")'
_INSTALL_CHAIN = (
    "pipx install openshard >/dev/null 2>&1"
    " || python3 -m pip install --quiet openshard >/dev/null 2>&1"
    " || python3 -m pip install --quiet --user openshard >/dev/null 2>&1"
    " || python3 -m pip install --quiet --user --break-system-packages openshard >/dev/null 2>&1"
)
_INSTALL = (
    'command -v openshard >/dev/null 2>&1 || [ -x "$HOME/.local/bin/openshard" ] || '
    + _INSTALL_CHAIN
    + ' || echo "openshard: install failed; nothing is captured in this environment" >&2'
)
_LINK = (
    'if [ -n "${OPENSHARD_REMOTE_CAPTURE_URL:-}" ] && [ -n "${OPENSHARD_REMOTE_TOKEN:-}" ]; then '
    '"$OS" remote attach >&2 || echo "openshard: remote attach failed; evidence stays in this environment" >&2; '
    'elif [ -n "${OPENSHARD_CONNECTED_TOKEN:-}" ]; then '
    'echo "openshard: connected capture is configured from the environment (not checked here)" >&2; '
    'else echo "openshard: remote capture not linked (OPENSHARD_REMOTE_CAPTURE_URL / OPENSHARD_REMOTE_TOKEN unset);'
    ' evidence stays in this environment" >&2; fi'
)
SETUP_LINES: tuple[str, ...] = (SETUP_MARKER, _INSTALL, _RESOLVE, _LINK)
SETUP_ONE_LINE = "(" + "; ".join(SETUP_LINES) + ") || true"

# Environment variables that only a cloud sandbox sets: (name, required value or None for "any
# non-empty value", sandbox). Each is the provider's own documented or observed marker; an
# unknown sandbox can opt in with OPENSHARD_REPO_BOOTSTRAP=1.
CLOUD_SANDBOX_MARKERS: tuple[tuple[str, str | None, str], ...] = (
    ("CLAUDE_CODE_REMOTE", "true", "Claude Code on the web"),
    ("REPLICAS_WORKSPACE_ID", None, "Replicas"),
    ("E2B_SANDBOX", "true", "E2B"),
    ("CODESPACES", "true", "GitHub Codespaces"),
    ("GITPOD_WORKSPACE_ID", None, "Gitpod"),
)


def cloud_sandbox(env: dict[str, str] | None = None) -> str | None:
    """The cloud sandbox this process runs in, by its own marker variable, or None."""
    env = dict(os.environ) if env is None else env
    for name, value, label in CLOUD_SANDBOX_MARKERS:
        got = env.get(name) or ""  # exactly the shell test the hook runs: non-empty, or this exact value
        if got if value is None else got == value:
            return label
    return None


def _shell_test(name: str, value: str | None) -> str:
    return f'[ -n "${{{name}:-}}" ]' if value is None else f'[ "${{{name}:-}}" = "{value}" ]'


_IN_CLOUD = " || ".join([_shell_test(n, v) for n, v, _ in CLOUD_SANDBOX_MARKERS]
                        + ['[ "${OPENSHARD_REPO_BOOTSTRAP:-}" = "1" ]'])
_BOOTSTRAP = (
    "if { " + _IN_CLOUD + "; }"
    ' && [ "${OPENSHARD_REPO_BOOTSTRAP:-}" != "0" ] && mkdir "${TMPDIR:-/tmp}/openshard-bootstrap" 2>/dev/null; then '
    "( " + _INSTALL_CHAIN + " ) </dev/null >/dev/null 2>&1 & "
    'echo "openshard: installing in the background; events before it finishes are not captured" >&2; '
    'if [ -z "${OPENSHARD_REMOTE_TOKEN:-}" ] && [ -z "${OPENSHARD_CONNECTED_TOKEN:-}" ]; then '
    'echo "openshard: remote capture not linked; evidence stays in this environment" >&2; fi; exit 0; fi'
)


def hook_command(
    invocation: str,
    *,
    defer_if: tuple[str, str] | None = None,
    bootstrap: bool = False,
    missing_reply: str | None = None,
    stdout_to_stderr: bool = False,
) -> str:
    """A fail-open ``sh -c`` command that runs ``openshard <invocation>`` when installed.

    *defer_if* ``(needle, file)``: exit 0 when *file* contains *needle* (this
    clone's local install already delivers the event). *missing_reply*: the
    stdout reply the agent expects when OpenShard is missing.
    """
    parts = [REPO_HOOK_MARKER]
    if defer_if:
        parts.append(f'grep -qs "{defer_if[0]}" "{defer_if[1]}" && exit 0')
    parts.append(_RESOLVE)
    redirect = " >&2" if stdout_to_stderr else ""
    parts.append(f'if [ -x "$OS" ]; then exec "$OS" {invocation}{redirect}; fi')
    if bootstrap:
        parts.append(_BOOTSTRAP)
    parts.append('echo "openshard: not installed; this event was not captured" >&2')
    if missing_reply is not None:
        parts.append("echo " + json.dumps(missing_reply))
    parts.append("exit 0")
    script = "; ".join(parts)
    assert "'" not in script
    return f"sh -c '{script}'"


def is_repo_hook(hook: object) -> bool:
    """True for a hook entry written by ``setup --repo`` (any agent's layout)."""
    if not isinstance(hook, dict):
        return False
    return any(isinstance(hook.get(k), str) and REPO_HOOK_MARKER in hook[k] for k in ("command", "bash"))


# --- results ---------------------------------------------------------------


@dataclass
class FileChange:
    agent: str
    path: str  # repository-relative, POSIX
    status: str  # created | updated | unchanged | skipped | manual | error
    detail: str = ""
    content: str | None = None
    executable: bool = False


@dataclass
class RepoSetupResult:
    root: Path
    dry_run: bool
    changes: list[FileChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == "error" for c in self.changes)


def parse_agents(value: str | None) -> list[str]:
    """``--agents`` -> ordered, de-duplicated names. Raises ``ValueError`` for an unknown one."""
    if not value:
        return list(FILE_AGENTS)
    names: list[str] = []
    known = FILE_AGENTS + SNIPPET_AGENTS
    for raw in value.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        name = {"claude-code": "claude", "github-copilot": "copilot"}.get(name, name)
        if name not in known:
            raise ValueError(f"unknown agent {raw.strip()!r}; choose from {', '.join(known)}")
        if name not in names:
            names.append(name)
    return names


# --- JSON helpers ----------------------------------------------------------


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    """``({}, None)`` for a missing/empty file; ``(None, why)`` when it cannot be merged safely."""
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read it ({type(exc).__name__})"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "it is not plain JSON (comments or a syntax error); left untouched"
    if not isinstance(data, dict):
        return None, "it does not hold a JSON object; left untouched"
    return data, None


def _json_change(agent: str, root: Path, rel: Path, before: dict, after: dict, detail: str) -> FileChange:
    path = root / rel
    if path.exists() and before == after:
        return FileChange(agent, rel.as_posix(), "unchanged", "already configured")
    status = "updated" if path.exists() else "created"
    return FileChange(agent, rel.as_posix(), status, detail, json.dumps(after, indent=2) + "\n")


def _events_detail(changes: dict[str, str]) -> str:
    touched = [f"{event} {state}" for event, state in changes.items() if state != "unchanged"]
    return "hooks: " + ", ".join(touched) if touched else "already configured"


def _merge_flat(
    config: dict, desired: dict[str, dict], is_ours: Callable[[object], bool], *, version: int = 1,
) -> tuple[dict, dict[str, str]]:
    """Merge into the flat ``{"version": 1, "hooks": {event: [entry, ...]}}`` layout (Cursor, Copilot)."""
    new = copy.deepcopy(config)
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not a JSON object")
    new.setdefault("version", version)
    changes: dict[str, str] = {}
    for event, entry in desired.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"'hooks.{event}' is not a JSON array")
        ours = [h for h in entries if is_ours(h)]
        if len(ours) == 1 and ours[0] == entry:
            changes[event] = "unchanged"
            continue
        changes[event] = "updated" if ours else "added"
        entries[:] = [h for h in entries if not is_ours(h)] + [entry]
    return new, changes


def _merge_command(existing: object, key: str) -> tuple[object, str]:
    """Prepend the setup line to a shell-command field. Returns ``(value, status)``.

    The setup line always exits 0, so ``ours; theirs`` keeps the user's own
    command and its exit status. A list (exec form, no shell) cannot be
    prepended to: reported as ``manual``.
    """
    if existing is None or (isinstance(existing, str) and not existing.strip()):
        return SETUP_ONE_LINE, "added"
    if isinstance(existing, str):
        if SETUP_MARKER in existing:
            return existing, "unchanged" if SETUP_ONE_LINE in existing else "present"
        return f"{SETUP_ONE_LINE}; {existing}", "added"
    if isinstance(existing, dict):
        if any(isinstance(v, str) and SETUP_MARKER in v for v in existing.values()):
            return existing, "unchanged" if existing.get("openshard") == SETUP_ONE_LINE else "present"
        return {**existing, "openshard": SETUP_ONE_LINE}, "added"
    return existing, "manual"


# --- per-agent planners ----------------------------------------------------


def _claude_entry(spec: HookSpec, _port: int) -> dict:
    return {
        "type": "command",
        "command": hook_command(
            "hooks claude",
            defer_if=("openshard hooks claude", "${CLAUDE_PROJECT_DIR:-.}/" + CLAUDE_LOCAL_SETTINGS.as_posix()),
            bootstrap=spec.event == "SessionStart",
        ),
        "timeout": spec.timeout,
    }


def _plan_claude(root: Path) -> list[FileChange]:
    settings, err = _read_json(root / CLAUDE_SETTINGS)
    if settings is None:
        return [FileChange("claude", CLAUDE_SETTINGS.as_posix(), "error", err or "")]
    specs = tuple(HookSpec(s.event, s.matcher, s.timeout, TRANSPORT_COMMAND) for s in CLAUDE_HOOK_SPECS)
    try:
        merged, changes = merge_openshard_hooks(
            settings, specs=specs, build_entry=_claude_entry,
            is_ours=lambda h: is_openshard_hook(h) or is_repo_hook(h),
        )
    except ValueError as exc:
        return [FileChange("claude", CLAUDE_SETTINGS.as_posix(), "error", f"unexpected hooks layout ({exc})")]
    return [_json_change("claude", root, CLAUDE_SETTINGS, settings, merged, _events_detail(changes))]


def _cursor_entry(spec: HookSpec) -> dict:
    no_spawn = " --no-spawn" if spec.event == "sessionEnd" else ""
    reply = {"continue": True} if spec.event == "beforeSubmitPrompt" else {}
    return {
        "type": "command",
        "command": hook_command(f"hooks cursor{no_spawn}", missing_reply=json.dumps(reply)),
        "timeout": spec.timeout,
        "failClosed": False,
    }


def _plan_cursor(root: Path) -> list[FileChange]:
    out: list[FileChange] = []
    hooks, err = _read_json(root / CURSOR_HOOKS)
    if hooks is None:
        out.append(FileChange("cursor", CURSOR_HOOKS.as_posix(), "error", err or ""))
    else:
        desired = {spec.event: _cursor_entry(spec) for spec in CURSOR_HOOK_SPECS}
        try:
            merged, changes = _merge_flat(hooks, desired, is_openshard_cursor_hook, version=CURSOR_HOOKS_VERSION)
            out.append(_json_change("cursor", root, CURSOR_HOOKS, hooks, merged, _events_detail(changes)))
        except ValueError as exc:
            out.append(FileChange("cursor", CURSOR_HOOKS.as_posix(), "error", f"unexpected hooks layout ({exc})"))
    out.append(_plan_command_field("cursor", root, CURSOR_ENV, "install", create=True))
    return out


def _plan_command_field(agent: str, root: Path, rel: Path, key: str, *, create: bool) -> FileChange:
    path = root / rel
    if not path.exists() and not create:
        return FileChange(agent, rel.as_posix(), "skipped", "not present (not created)")
    data, err = _read_json(path)
    if data is None:
        return FileChange(agent, rel.as_posix(), "manual", f"{err}; add to {key}: {SETUP_ONE_LINE}")
    value, state = _merge_command(data.get(key), key)
    if state == "manual":
        return FileChange(agent, rel.as_posix(), "manual", f"{key} is not a shell string; add: {SETUP_ONE_LINE}")
    if state == "present":
        return FileChange(agent, rel.as_posix(), "unchanged", f"an older OpenShard step is in {key}; left as is")
    merged = {**data, key: value}
    return _json_change(agent, root, rel, data, merged, f"{key}: OpenShard install step added")


def _flush_hook(timeout: int) -> dict:
    return {"command": hook_command("remote flush", stdout_to_stderr=True), "timeout": timeout}


def _copilot_entry() -> dict:
    return {
        "type": "command",
        "bash": hook_command("remote flush", stdout_to_stderr=True),
        "powershell": (
            "if (Get-Command openshard -ErrorAction SilentlyContinue) { openshard remote flush *> $null }; exit 0"
        ),
        "timeoutSec": 30,
    }


def _plan_copilot(root: Path) -> list[FileChange]:
    out = [_plan_copilot_workflow(root)]
    hooks, err = _read_json(root / COPILOT_HOOKS)
    if hooks is None:
        out.append(FileChange("copilot", COPILOT_HOOKS.as_posix(), "error", err or ""))
        return out
    desired = {"sessionEnd": _copilot_entry(), "agentStop": _copilot_entry()}
    try:
        merged, changes = _merge_flat(hooks, desired, is_repo_hook)
    except ValueError as exc:
        out.append(FileChange("copilot", COPILOT_HOOKS.as_posix(), "error", f"unexpected hooks layout ({exc})"))
        return out
    out.append(_json_change("copilot", root, COPILOT_HOOKS, hooks, merged, _events_detail(changes)))
    return out


def _copilot_step_lines(indent: str) -> list[str]:
    lines = [
        f"{indent}- name: {COPILOT_STEP_NAME}",
        f"{indent}  # OPENSHARD_REMOTE_CAPTURE_URL / OPENSHARD_REMOTE_TOKEN come from the repository's",
        f"{indent}  # Agents secrets, which Copilot exposes as environment variables. None are stored here.",
        f"{indent}  run: |",
        f"{indent}    (",
    ]
    lines += [f"{indent}    {line}" for line in SETUP_LINES]
    lines.append(f"{indent}    ) || true")
    return lines


def _desired_copilot_step() -> dict:
    return yaml.safe_load("\n".join(_copilot_step_lines("")) + "\n")[0]


_COPILOT_TEMPLATE = """\
name: "Copilot Setup Steps"

on:
  workflow_dispatch:
  push:
    paths:
      - .github/workflows/copilot-setup-steps.yml
  pull_request:
    paths:
      - .github/workflows/copilot-setup-steps.yml

jobs:
  copilot-setup-steps:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
"""


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _insert_copilot_step(text: str) -> str | None:
    """Append the step to the ``copilot-setup-steps`` job's block-style ``steps``; None when not found."""
    lines = text.splitlines()
    job = next((i for i, ln in enumerate(lines) if re.match(rf"^\s+{COPILOT_JOB}:\s*(#.*)?$", ln)), None)
    if job is None:
        return None
    job_indent = _indent_of(lines[job])
    steps = None
    for i in range(job + 1, len(lines)):
        if _content(lines[i]) and _indent_of(lines[i]) <= job_indent:
            break
        if re.match(r"^\s+steps:\s*(#.*)?$", lines[i]):
            steps = i
            break
    if steps is None:
        return None
    first = next((i for i in range(steps + 1, len(lines)) if _content(lines[i])), None)
    if first is None or not lines[first].lstrip().startswith("- "):
        return None
    item_indent = _indent_of(lines[first])
    end = len(lines)
    for i in range(first + 1, len(lines)):
        if not _content(lines[i]):
            continue
        ind = _indent_of(lines[i])
        if ind < item_indent or (ind == item_indent and not lines[i].lstrip().startswith("-")):
            end = i
            break
    while end > first + 1 and not lines[end - 1].strip():
        end -= 1
    new_lines = lines[:end] + _copilot_step_lines(" " * item_indent) + lines[end:]
    return "\n".join(new_lines) + "\n"


def _copilot_job(data: object) -> dict | None:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    job = jobs.get(COPILOT_JOB) if isinstance(jobs, dict) else None
    return job if isinstance(job, dict) else None


def _plan_copilot_workflow(root: Path) -> FileChange:
    rel = COPILOT_WORKFLOW.as_posix()
    path = root / COPILOT_WORKFLOW
    desired = _desired_copilot_step()
    manual = f"add a step to the {COPILOT_JOB} job:\n" + "\n".join(_copilot_step_lines("      "))
    if not path.exists():
        text = _COPILOT_TEMPLATE + "\n".join(_copilot_step_lines("      ")) + "\n"
        return FileChange("copilot", rel, "created", f"{COPILOT_JOB} job with an OpenShard install step", text)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError):
        return FileChange("copilot", rel, "error", "not valid YAML; left untouched")
    job = _copilot_job(data)
    steps = job.get("steps") if job else None
    if not isinstance(steps, list):
        return FileChange("copilot", rel, "manual", f"no {COPILOT_JOB} job with a steps list; {manual}")
    ours = [s for s in steps if isinstance(s, dict) and s.get("name") == COPILOT_STEP_NAME]
    if ours:
        state = "already configured" if ours[0] == desired else "a different OpenShard step is present; left as is"
        return FileChange("copilot", rel, "unchanged", state)
    new_text = _insert_copilot_step(text)
    new_job = _copilot_job(yaml.safe_load(new_text)) if new_text else None
    new_steps = new_job.get("steps") if new_job else None
    if not isinstance(new_steps, list) or new_steps[-1] != desired or new_steps[:-1] != steps:
        return FileChange("copilot", rel, "manual", f"could not place the step safely; {manual}")
    return FileChange("copilot", rel, "updated", f"OpenShard install step appended to {COPILOT_JOB}", new_text)


def _setup_block() -> str:
    return "\n".join([BLOCK_BEGIN, "(", *SETUP_LINES, ") || true", BLOCK_END]) + "\n"


def _plan_openhands(root: Path) -> list[FileChange]:
    out: list[FileChange] = []
    path = root / OPENHANDS_SETUP
    rel = OPENHANDS_SETUP.as_posix()
    block = _setup_block()
    if not path.exists():
        out.append(FileChange("openhands", rel, "created", "OpenShard install block", "#!/bin/bash\n" + block, True))
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            text = None
            out.append(FileChange("openhands", rel, "error", f"could not read it ({type(exc).__name__})"))
        if text is not None:
            start, stop = text.find(BLOCK_BEGIN), text.find(BLOCK_END)
            if start >= 0 and stop > start:
                current = text[start:stop + len(BLOCK_END)] + "\n"
                if current == block:
                    out.append(FileChange("openhands", rel, "unchanged", "already configured"))
                else:
                    new = text[:start] + block + text[stop + len(BLOCK_END):].lstrip("\n")
                    out.append(FileChange("openhands", rel, "updated", "OpenShard block refreshed", new))
            else:
                sep = "" if text.endswith("\n") or not text else "\n"
                out.append(FileChange("openhands", rel, "updated", "OpenShard install block appended", text + sep + block))
    hooks, err = _read_json(root / OPENHANDS_HOOKS)
    if hooks is None:
        out.append(FileChange("openhands", OPENHANDS_HOOKS.as_posix(), "error", err or ""))
        return out
    spec = HookSpec("stop", "*", 30, TRANSPORT_COMMAND)
    try:
        # OpenHands keeps the event keys at the top level (no "hooks" wrapper).
        wrapped, changes = merge_openshard_hooks(
            {"hooks": hooks}, specs=(spec,), build_entry=lambda s, _p: _flush_hook(s.timeout), is_ours=is_repo_hook,
        )
        merged = wrapped["hooks"]
    except ValueError as exc:
        out.append(FileChange("openhands", OPENHANDS_HOOKS.as_posix(), "error", f"unexpected hooks layout ({exc})"))
        return out
    out.append(_json_change("openhands", root, OPENHANDS_HOOKS, hooks, merged, _events_detail(changes)))
    return out


def _plan_devcontainer(root: Path) -> list[FileChange]:
    for rel in DEVCONTAINER_FILES:
        if (root / rel).exists():
            return [_plan_command_field("devcontainer", root, rel, "postCreateCommand", create=False)]
    return [FileChange("devcontainer", DEVCONTAINER_FILES[0].as_posix(), "skipped", "no devcontainer (not created)")]


_PLANNERS: dict[str, Callable[[Path], list[FileChange]]] = {
    "claude": _plan_claude,
    "cursor": _plan_cursor,
    "copilot": _plan_copilot,
    "openhands": _plan_openhands,
    "devcontainer": _plan_devcontainer,
}

_NOTES = {
    "claude": (
        "Claude Code: hooks run `openshard hooks claude` when OpenShard is installed and stand aside when this "
        "clone has local hooks (.claude/settings.local.json). In a recognised cloud sandbox (Claude Code on the "
        "web, Replicas, E2B, Codespaces, Gitpod) a missing install starts a background install; events before it "
        "finishes are not captured. Multi-repository cloud sessions load "
        "hooks from the parent directory only (docs/claude-cloud-prelaunch.md), so these do not fire there."
    ),
    "cursor": (
        "Cursor: cloud-agent lifecycle hooks (sessionStart/sessionEnd) may not fire; verify a first Remote "
        "event before relying on capture."
    ),
    "copilot": (
        "Copilot: OpenShard has no Copilot hook adapter. The hooks only deliver evidence OpenShard already "
        "recorded in that environment (e.g. `openshard verify`); Copilot sessions are not captured event by event. "
        "copilot-setup-steps.yml takes effect once it is on the default branch."
    ),
    "openhands": (
        "OpenHands: OpenShard has no OpenHands hook adapter. The Stop hook only delivers evidence OpenShard already recorded "
        "(e.g. `openshard verify`); OpenHands sessions are not captured event by event."
    ),
}


def snippet_lines(agent: str) -> list[str]:
    """Documented, not written: setup-script lines for runtimes without a repository config file."""
    return [
        f"{agent}: add to the environment's setup/startup script (nothing written):",
        *(f"    {line}" for line in SETUP_LINES),
        "  No OpenShard hook adapter exists for it: only `openshard verify` and git/CI evidence are captured.",
    ]


# --- orchestration ---------------------------------------------------------


def _git_ignored(root: Path, rel: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", rel], cwd=str(root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _write(path: Path, text: str, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else (0o755 if executable else 0o644)
    tmp = path.with_name(path.name + ".openshard-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")  # LF everywhere: these run in Linux VMs
    os.chmod(tmp, mode & 0o7777)
    os.replace(tmp, path)


def run_repo_setup(repo_root: Path, agents: list[str], *, dry_run: bool = False) -> RepoSetupResult:
    """Plan (and unless *dry_run*, write) the repository-level files for *agents*."""
    root = Path(repo_root)
    result = RepoSetupResult(root=root, dry_run=dry_run)
    for agent in agents:
        if agent in _PLANNERS:
            result.changes.extend(_PLANNERS[agent](root))
            if agent in _NOTES:
                result.notes.append(_NOTES[agent])
        else:
            result.notes.append("\n".join(snippet_lines(agent)))
    for change in result.changes:
        if change.content is not None and _CREDENTIAL_RE.search(change.content):
            change.status, change.content = "error", None
            change.detail = "contains what looks like an OpenShard credential; not written (remove it first)"
    if dry_run:
        return result
    for change in result.changes:
        if change.content is None or change.status not in ("created", "updated"):
            continue
        _write(root / change.path, change.content, change.executable)
        if _git_ignored(root, change.path):
            result.warnings.append(
                f"{change.path} is git-ignored here (`git check-ignore -v {change.path}`); "
                "it will not be committed until that rule is removed."
            )
    return result


def render(result: RepoSetupResult) -> list[str]:
    head = "OpenShard repository setup" + (" (dry run: nothing written)" if result.dry_run else "")
    lines = [head]
    width = max((len(c.path) for c in result.changes), default=0)
    for c in result.changes:
        verb = {"created": "would create", "updated": "would update"}.get(c.status, c.status) if result.dry_run else c.status
        lines.append(f"  {c.agent:<12} {c.path:<{width}}  {verb}: {c.detail}")
    lines += [f"  warning: {w}" for w in result.warnings]
    lines += [""] + result.notes if result.notes else []
    lines += [
        "",
        "No credentials were written. Cloud capture links only when OPENSHARD_REMOTE_CAPTURE_URL and",
        "OPENSHARD_REMOTE_TOKEN are set in the cloud environment (store them as secrets, never in the repo);",
        "without them capture stays local to where the agent runs. Review the files, then commit them.",
    ]
    return lines


def run_cli(repo_path: Path | None, agents_value: str | None, dry_run: bool, echo: Callable[[str], None]) -> int:
    """``openshard setup --repo`` body. Returns the exit code."""
    from openshard.adapters.claude_mcp_install import find_repo_root

    try:
        agents = parse_agents(agents_value)
    except ValueError as exc:
        echo(f"Error: {exc}")
        return 2
    root = find_repo_root(repo_path)
    if root is None:
        echo("Error: not inside a git repository (use --repo-path).")
        return 2
    result = run_repo_setup(root, agents, dry_run=dry_run)
    for line in render(result):
        echo(line)
    return 0 if result.ok else 1
