from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_SHELL_METACHAR = re.compile(r"&&|\|(?!\|)|;|\$\(|`")

_SAFE_PREFIXES: list[tuple[str, ...]] = [
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    (sys.executable, "-m", "pytest"),
    ("pytest",),
    ("npm", "run", "test"),
    ("npm", "run", "lint"),
    ("npm", "run", "typecheck"),
    ("npm", "test"),
    ("cargo", "test"),
    ("go", "test"),
    ("bundle", "exec", "rspec"),
    ("mvn", "test"),
    ("git", "status"),
    ("git", "diff"),
    ("git", "rev-parse"),
    ("grep",),
    ("rg",),
]

_BLOCKED_COMMANDS = {"curl", "wget", "rm", "sudo", "chmod", "chown", "printenv"}

# Multi-token argv-prefix patterns that are always blocked.
_BLOCKED_ARGV_PREFIXES: list[tuple[str, ...]] = [
    ("git", "push"),
    ("git", "clean"),
    ("npm", "publish"),
    ("docker", "push"),
    # IaC destructive/deploying ops
    ("terraform", "apply"),
    ("terraform", "destroy"),
    ("terraform", "import"),
    ("terraform", "state"),
    # kubectl destructive ops
    ("kubectl", "apply"),
    ("kubectl", "delete"),
    ("kubectl", "patch"),
    ("kubectl", "scale"),
    # helm deploy ops
    ("helm", "install"),
    ("helm", "upgrade"),
    ("helm", "uninstall"),
    ("ansible",),
    ("ansible-playbook",),
]

# Flags that make `git reset` destructive.
_BLOCKED_GIT_RESET_FLAGS: frozenset[str] = frozenset({"--hard", "--mixed", "--keep"})

# Medium-risk commands — classified needs_approval with an informative reason.
_NEEDS_APPROVAL_PREFIXES: list[tuple[str, ...]] = [
    ("npm", "install"),
    ("npm", "ci"),
    ("npm", "run", "build"),
    ("pip", "install"),
    ("pip3", "install"),
    ("yarn", "install"),
    ("yarn", "add"),
    ("git", "checkout"),
    ("git", "switch"),
    ("git", "branch"),
    ("git", "merge"),
    ("git", "rebase"),
    ("make",),
    ("cargo", "build"),
    ("go", "build"),
    ("terraform", "plan"),
    ("terraform", "init"),
]

_TEST_KINDS: list[tuple[str, ...]] = [
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    (sys.executable, "-m", "pytest"),
    ("npm", "test"),
    ("cargo", "test"),
    ("go", "test"),
    ("bundle", "exec", "rspec"),
    ("mvn", "test"),
]


# Flags that make an otherwise read-only "safe" tool run other programs or write files.
_UNSAFE_FLAGS_BY_TOOL: dict[str, frozenset[str]] = {
    "rg": frozenset({"--pre", "--pre-glob"}),
    "git": frozenset({"--output", "--ext-diff", "--textconv", "--no-index"}),
    "go": frozenset({"-exec", "-toolexec"}),
    "cargo": frozenset({"--config"}),
    "pytest": frozenset({"--basetemp"}),
}

# Tools whose blocked subcommand may follow global options we cannot enumerate
# (`terraform -chdir=x apply`, `kubectl -n p delete`). For these, a blocked
# subcommand word anywhere before `--` is treated as blocked (over-blocks safely).
_SUBCOMMAND_SCAN_TOOLS = frozenset({"git", "terraform", "kubectl", "helm", "npm", "docker"})

_EXE_SUFFIXES = (".exe", ".com", ".bat", ".cmd")

# git global options that take a value in the next token.
_GIT_VALUE_OPTS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"})


def _strip_exe_suffix(name: str) -> str:
    name = name.rstrip(" .")  # Win32 normalises "x.bat." and "x.bat " to "x.bat"
    for suf in _EXE_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def _skip_git_global_opts(argv_lower: list[str]) -> list[str]:
    """Return argv with git's global options removed so the subcommand is at index 1."""
    if not argv_lower or _strip_exe_suffix(argv_lower[0].split("/")[-1].split("\\")[-1]) != "git":
        return argv_lower
    i = 1
    while i < len(argv_lower) and argv_lower[i].startswith("-"):
        i += 2 if argv_lower[i] in _GIT_VALUE_OPTS else 1
    return ["git", *argv_lower[i:]]


class VerificationSource(str, Enum):
    config = "config"
    detected = "detected"
    user = "user"
    eval = "eval"


class VerificationKind(str, Enum):
    test = "test"
    lint = "lint"
    typecheck = "typecheck"
    build = "build"
    format_check = "format_check"
    unknown = "unknown"


class CommandSafety(str, Enum):
    safe = "safe"
    needs_approval = "needs_approval"
    blocked = "blocked"


@dataclass
class VerificationCommand:
    name: str
    argv: list[str]
    kind: VerificationKind
    source: VerificationSource
    safety: CommandSafety
    reason: str


@dataclass
class VerificationPlan:
    commands: list[VerificationCommand] = field(default_factory=list)

    @property
    def has_commands(self) -> bool:
        return bool(self.commands)


def classify_command_safety(
    argv: list[str], source: VerificationSource  # noqa: ARG001
) -> tuple[CommandSafety, str]:
    if not argv:
        return CommandSafety.blocked, "empty argv"

    # Step 2: shell metacharacters — catches piped grep/rg and chained commands.
    for token in argv:
        if _SHELL_METACHAR.search(token):
            return CommandSafety.blocked, f"shell metacharacter in token: {token!r}"

    joined = " ".join(argv)
    if _SHELL_METACHAR.search(joined):
        return CommandSafety.blocked, "shell metacharacters detected"

    # Step 3: single-token blocked executables.
    executable = argv[0].lower()
    base = _strip_exe_suffix(executable.split("/")[-1].split("\\")[-1])
    if base in _BLOCKED_COMMANDS or executable in _BLOCKED_COMMANDS:
        return CommandSafety.blocked, f"blocked executable: {argv[0]!r}"

    # Step 4: multi-token blocked argv prefix.
    argv_lower = [t.lower() for t in argv]
    # Blocked-prefix matching sees through `git -C dir push` and `terraform.exe apply`.
    match_argv = _skip_git_global_opts(argv_lower)
    if match_argv:
        match_argv = [_strip_exe_suffix(match_argv[0].split("/")[-1].split("\\")[-1]), *match_argv[1:]]
    for prefix in _BLOCKED_ARGV_PREFIXES:
        if len(match_argv) >= len(prefix) and tuple(match_argv[: len(prefix)]) == prefix:
            return CommandSafety.blocked, f"blocked command: {' '.join(prefix)}"
    for prefix in _BLOCKED_ARGV_PREFIXES:
        if len(argv_lower) >= len(prefix) and tuple(argv_lower[: len(prefix)]) == prefix:
            return CommandSafety.blocked, f"blocked command: {' '.join(prefix)}"

    if match_argv and match_argv[0] in _SUBCOMMAND_SCAN_TOOLS:
        head = argv_lower[1:argv_lower.index("--")] if "--" in argv_lower else argv_lower[1:]
        for prefix in _BLOCKED_ARGV_PREFIXES:
            if len(prefix) == 2 and prefix[0] == match_argv[0] and prefix[1] in head:
                return CommandSafety.blocked, f"blocked command: {' '.join(prefix)}"

    # Step 5: git reset with destructive flags.
    if len(match_argv) >= 2 and match_argv[0] == "git" and match_argv[1] == "reset":
        flags_present = frozenset(t.lower() for t in argv[2:]) & _BLOCKED_GIT_RESET_FLAGS
        if flags_present:
            flag = next(iter(flags_present))
            return CommandSafety.blocked, f"destructive git reset flag: {flag}"

    # Step 5b: a safe-listed tool with a flag that executes/writes is not safe.
    tool = match_argv[0] if match_argv else ""
    unsafe = set(_UNSAFE_FLAGS_BY_TOOL.get(tool, frozenset()))
    if tool == "pytest" or argv_lower[1:3] == ["-m", "pytest"]:
        unsafe |= _UNSAFE_FLAGS_BY_TOOL["pytest"]
        for i, tok in enumerate(argv_lower):
            nxt = argv_lower[i + 1] if i + 1 < len(argv_lower) else ""
            if (tok == "-p" and not nxt.startswith("no:")) or (
                tok.startswith("-p") and len(tok) > 2 and not tok.startswith(("-pno:", "--"))
            ):
                return CommandSafety.needs_approval, "pytest -p loads an arbitrary plugin module"
    for tok in argv_lower[1:]:
        if tok.split("=", 1)[0] in unsafe:
            return CommandSafety.needs_approval, f"flag can execute or write outside read-only use: {tok.split('=', 1)[0]}"

    # Step 6: safe prefixes — checked before approval to protect e.g. npm run test.
    for prefix in _SAFE_PREFIXES:
        if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
            return CommandSafety.safe, f"matches safe prefix: {' '.join(prefix)}"

    # Step 7: explicit medium-risk prefixes (informative reason string).
    for prefix in _NEEDS_APPROVAL_PREFIXES:
        if len(argv_lower) >= len(prefix) and tuple(argv_lower[: len(prefix)]) == prefix:
            return CommandSafety.needs_approval, f"medium-risk command requires approval: {' '.join(prefix)}"

    # Step 8: default.
    return CommandSafety.needs_approval, "unrecognised command requires approval"


def parse_command_to_argv(command: str) -> list[str]:
    try:
        posix = sys.platform != "win32"
        return shlex.split(command, posix=posix)
    except ValueError:
        return [command]


def _infer_kind(argv: list[str]) -> VerificationKind:
    for prefix in _TEST_KINDS:
        if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
            return VerificationKind.test
    return VerificationKind.unknown


def build_verification_plan(config: dict, repo_facts) -> VerificationPlan:
    argv: list[str] | None = None
    source: VerificationSource | None = None

    raw = config.get("verification_command")
    if isinstance(raw, list) and raw:
        argv = [str(t) for t in raw]
        source = VerificationSource.config
    elif isinstance(raw, str) and raw.strip():
        argv = parse_command_to_argv(raw.strip())
        source = VerificationSource.config
    elif repo_facts is not None and getattr(repo_facts, "test_command", None):
        argv = parse_command_to_argv(repo_facts.test_command)
        source = VerificationSource.detected

    if argv is None or source is None:
        return VerificationPlan()

    safety, reason = classify_command_safety(argv, source)
    kind = _infer_kind(argv)
    name = "tests" if (safety == CommandSafety.safe and kind == VerificationKind.test) else "verification"

    cmd = VerificationCommand(
        name=name,
        argv=argv,
        kind=kind,
        source=source,
        safety=safety,
        reason=reason,
    )
    return VerificationPlan(commands=[cmd])


def _safe_token_label(token: str) -> str:
    """Return the basename of a path token, handling both POSIX and Windows styles."""
    if "/" not in token and "\\" not in token:
        return token
    win_name = PureWindowsPath(token).name
    posix_name = PurePosixPath(token).name
    return min((win_name, posix_name), key=len) or token


def safe_check_label(cmd: VerificationCommand) -> str:
    """Return a path-free display label for a verification command (max 50 chars)."""
    safe = [
        "python" if t == sys.executable else _safe_token_label(t)
        for t in cmd.argv
    ]
    return " ".join(safe)[:50]


def render_verification_plan(plan: VerificationPlan) -> str:
    lines = ["Verification"]
    if not plan.has_commands:
        lines.append("  no verification command detected")
    else:
        for cmd in plan.commands:
            argv_str = " ".join(cmd.argv)
            lines.append(f"  {cmd.name}  {cmd.safety.value}  {cmd.source.value}  {argv_str}")
    return "\n".join(lines)
