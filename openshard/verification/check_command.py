"""Which shell commands are checks, and whether a command's exit status is the check's.

A captured agent command is a *check* only when one of its simple commands
runs a test, lint, type-check or build tool as the program -- never because a
tool's name appears somewhere in the text. ``git commit -m "ran ruff"``,
``grep -n pytest pyproject.toml``, a ``sed`` edit of ``foo.test.ts`` and a
``python - <<'PY'`` script that mentions vitest are not checks.

The agent reports one outcome for the whole command line. That outcome is
the check's only when the shell would have returned the check's own exit
status: the check is the last pipeline element of the final statement, and
that statement has no ``||``. ``pytest | tail -5`` succeeds whenever ``tail``
does, and ``pytest; echo done`` whenever ``echo`` does, so neither
success says anything about the tests. A failure of ``build && pytest`` may
be the build's; only cheap setup steps (``cd``, ``export``, ``source``...)
may precede a check whose failure is attributed to it.

Pure text analysis: nothing is executed, and the raw command is never kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KIND_TEST = "test"
KIND_LINT = "lint"
KIND_TYPECHECK = "typecheck"
KIND_BUILD = "build"
KIND_OTHER = "other"
CHECK_KINDS: frozenset[str] = frozenset({KIND_TEST, KIND_LINT, KIND_TYPECHECK, KIND_BUILD})

# Why a check's outcome cannot be read off the command's exit status.
UNATTRIBUTED_PIPED = "output_piped_to_another_program"
UNATTRIBUTED_NOT_LAST = "later_command_decides_exit_status"
UNATTRIBUTED_ALTERNATIVE = "or_operator_masks_failure"
UNATTRIBUTED_BACKGROUND = "run_in_background"
UNATTRIBUTED_SHARED_FAILURE = "failure_may_belong_to_another_command"

_MAX_COMMAND_CHARS = 20_000

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_PS_HERESTRING_RE = re.compile(r"@(['\"])\r?\n.*?\r?\n\1@", re.DOTALL)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Setup steps that may precede a check without making its failure ambiguous.
_SETUP_PROGRAMS = frozenset({
    "cd", "pushd", "popd", "set-location", "sl", "export", "set", "unset", "source", ".",
    "activate", "true", ":", "set-strictmode",
})
# Wrappers whose next word is the program that actually runs.
_WRAPPERS = frozenset({"env", "time", "command", "exec", "nice", "nohup", "sudo", "xvfb-run"})
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "hatch": "run", "pdm": "run", "rye": "run"}
_PYTHONS = frozenset({"python", "python3", "py", "pypy", "pypy3"}) | {f"python3.{n}" for n in range(8, 16)}
_NODE_EXEC = frozenset({"npx", "bunx", "pnpx"})
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
# Package-manager flags that take a value (``pnpm --filter api test``).
_PM_VALUE_FLAGS = frozenset({
    "--filter", "-f", "-c", "--dir", "--prefix", "--workspace", "-w", "--cwd", "--config",
})
_PM_SUBCOMMANDS_EXEC = frozenset({"exec", "dlx", "x"})

_BINARIES: dict[str, str] = {
    "pytest": KIND_TEST, "py.test": KIND_TEST, "jest": KIND_TEST, "vitest": KIND_TEST,
    "mocha": KIND_TEST, "rspec": KIND_TEST, "tox": KIND_TEST, "nox": KIND_TEST, "ctest": KIND_TEST,
    "ava": KIND_TEST, "phpunit": KIND_TEST,
    "ruff": KIND_LINT, "flake8": KIND_LINT, "pylint": KIND_LINT, "eslint": KIND_LINT,
    "golangci-lint": KIND_LINT, "shellcheck": KIND_LINT, "tflint": KIND_LINT, "stylelint": KIND_LINT,
    "biome": KIND_LINT, "markdownlint": KIND_LINT, "actionlint": KIND_LINT,
    "mypy": KIND_TYPECHECK, "pyright": KIND_TYPECHECK, "tsc": KIND_TYPECHECK, "vue-tsc": KIND_TYPECHECK,
    "basedpyright": KIND_TYPECHECK,
}
# Formatters are checks only in their check-only mode; otherwise they rewrite files.
_FORMATTER_CHECK_FLAGS: dict[str, frozenset[str]] = {
    "black": frozenset({"--check", "--diff"}),
    "isort": frozenset({"--check", "--check-only", "-c", "--diff"}),
    "prettier": frozenset({"--check", "-c", "--list-different", "-l"}),
    "gofmt": frozenset({"-l", "-d"}),
}
_PYTHON_MODULES: dict[str, str] = {
    "pytest": KIND_TEST, "unittest": KIND_TEST, "tox": KIND_TEST, "nox": KIND_TEST,
    "ruff": KIND_LINT, "flake8": KIND_LINT, "pylint": KIND_LINT,
    "mypy": KIND_TYPECHECK, "pyright": KIND_TYPECHECK,
    "build": KIND_BUILD, "compileall": KIND_BUILD,
}
# Subcommand tools: ``go test``, ``cargo clippy``...
_SUBCOMMANDS: dict[str, dict[str, str]] = {
    "go": {"test": KIND_TEST, "vet": KIND_LINT, "build": KIND_BUILD},
    "cargo": {"test": KIND_TEST, "nextest": KIND_TEST, "clippy": KIND_LINT, "check": KIND_TYPECHECK,
              "build": KIND_BUILD},
    "dotnet": {"test": KIND_TEST, "build": KIND_BUILD},
    "mvn": {"test": KIND_TEST, "verify": KIND_TEST, "package": KIND_BUILD},
    "gradle": {"test": KIND_TEST, "check": KIND_TEST, "build": KIND_BUILD},
    "gradlew": {"test": KIND_TEST, "check": KIND_TEST, "build": KIND_BUILD},
    "make": {"test": KIND_TEST, "check": KIND_TEST, "lint": KIND_LINT, "build": KIND_BUILD},
    "deno": {"test": KIND_TEST, "lint": KIND_LINT, "check": KIND_TYPECHECK},
    "terraform": {"validate": KIND_LINT},
    "playwright": {"test": KIND_TEST},
    "turbo": {"test": KIND_TEST, "lint": KIND_LINT, "typecheck": KIND_TYPECHECK, "build": KIND_BUILD},
    "nx": {"test": KIND_TEST, "lint": KIND_LINT, "build": KIND_BUILD},
}


@dataclass(frozen=True)
class CheckCommand:
    """How one command line relates to verification."""

    kind: str  # one of CHECK_KINDS, or "other"
    # The check's own simple command (setup steps and pipes removed), for a
    # readable name; still raw text, so the caller must scrub it.
    segment: str | None = None
    # None when the command's exit status is the check's; otherwise one of the
    # UNATTRIBUTED_* reasons, which hold for a success and a failure alike.
    unattributed: str | None = None
    # Set when only a *failure* cannot be pinned on the check (another
    # non-setup command ran before it in the same ``&&`` chain); a success of
    # the whole chain still means the check passed.
    failure_unattributed: str | None = None

    @property
    def is_check(self) -> bool:
        return self.kind in CHECK_KINDS


_OTHER = CheckCommand(KIND_OTHER)


@dataclass
class _Simple:
    words: list[str]
    text: str
    op_after: str | None  # the operator that ended it: && || | ; & or None at the end


def classify_check_command(command: str | None) -> CheckCommand:
    """The last check *command* runs (``kind`` "other" when it runs none)."""
    checks = classify_check_commands(command)
    return checks[-1] if checks else _OTHER


def classify_check_commands(command: str | None) -> list[CheckCommand]:
    """Every check *command* runs, in order, each with whether the exit status is its own.

    The shell's exit status is the last pipeline element's of the final
    statement. Every member of that statement's ``&&`` chain had to succeed
    for it to be zero, so a success of the whole line is each such check's
    success. A failure is pinned on a check only when everything else in the
    chain is a setup step.
    """
    if not isinstance(command, str) or not command.strip():
        return []
    statements = _parse(_strip_inline_documents(command[:_MAX_COMMAND_CHARS]))
    out: list[CheckCommand] = []
    for si, statement in enumerate(statements):
        for ci, simple in enumerate(statement):
            kind = _kind_of(simple.words)
            if kind == KIND_OTHER:
                continue
            segment = _display(simple.words)
            if any(s.op_after == "&" for s in statement):
                out.append(CheckCommand(kind, segment, UNATTRIBUTED_BACKGROUND))
            elif si != len(statements) - 1:
                out.append(CheckCommand(kind, segment, UNATTRIBUTED_NOT_LAST))
            elif any(s.op_after == "||" for s in statement):
                out.append(CheckCommand(kind, segment, UNATTRIBUTED_ALTERNATIVE))
            elif simple.op_after == "|":
                out.append(CheckCommand(kind, segment, UNATTRIBUTED_PIPED))
            elif any(not _is_setup(s.words) for j, s in enumerate(statement) if j != ci):
                out.append(CheckCommand(kind, segment, None, UNATTRIBUTED_SHARED_FAILURE))
            else:
                out.append(CheckCommand(kind, segment))
    return out


def check_outcome(check: CheckCommand, command_outcome: str) -> str:
    """The check's own outcome given the whole command's ``passed``/``failed``/``unknown``."""
    if command_outcome not in ("passed", "failed") or check.unattributed:
        return "unknown"
    if command_outcome == "failed" and check.failure_unattributed:
        return "unknown"
    return command_outcome


def unattributed_reason(check: CheckCommand, command_outcome: str) -> str | None:
    """Why ``check_outcome`` is unknown although the command reported *command_outcome*."""
    if command_outcome not in ("passed", "failed"):
        return None
    if check.unattributed:
        return check.unattributed
    if command_outcome == "failed":
        return check.failure_unattributed
    return None


def _display(words: list[str]) -> str | None:
    """The check as a reader would type it: program basename, absolute paths shortened to their name."""
    if not words:
        return None
    shown = [_program(words[0])]
    skip_target = False
    for w in words[1:]:
        if skip_target:
            skip_target = False
            continue
        redirect = _REDIRECT_RE.match(w)
        if redirect:
            skip_target = not redirect.group(1)  # ``> out.txt``: the target is the next word
            continue
        shown.append(re.split(r"[\\/]", w.rstrip("\\/"))[-1] or w if _looks_absolute(w) else w)
    return " ".join(shown)


# ``2>&1``, ``>&2``, ``&>log``, ``>out.txt``, ``2>`` (target in the next word)...
_REDIRECT_RE = re.compile(r"^(?:\d|&)?>>?(&\d|.+)?$")


def _looks_absolute(word: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|/|~[\\/])", word))


# -- lexing --------------------------------------------------------------------


def _strip_inline_documents(command: str) -> str:
    """Drop heredoc bodies and PowerShell here-strings: their text is data, never commands."""
    command = _PS_HERESTRING_RE.sub("''", command)
    lines = command.splitlines()
    out: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        out.append(line)
        match = None
        for match in _HEREDOC_RE.finditer(line):
            pass
        if match is not None:
            terminator = match.group(2)
    return "\n".join(out)


def _parse(command: str) -> list[list[_Simple]]:
    """Statements (split on ``;``, ``&`` and newlines) of simple commands (split on ``&&`` ``||`` ``|``)."""
    statements: list[list[_Simple]] = []
    current: list[_Simple] = []
    words: list[str] = []
    word: list[str] = []
    in_word = False
    text_start = 0
    i = 0
    n = len(command)

    def end_word() -> None:
        nonlocal word, in_word
        if in_word:
            words.append("".join(word))
        word = []
        in_word = False

    def end_simple(op: str | None, at: int) -> None:
        nonlocal words, text_start
        end_word()
        if words:
            current.append(_Simple(_unwrap(words), command[text_start:at].strip(), op))
        elif current and op is not None and current[-1].op_after is None:
            current[-1].op_after = op
        words = []

    def end_statement(op: str | None, at: int) -> None:
        nonlocal current
        end_simple(op, at)
        if current:
            statements.append(current)
        current = []

    while i < n:
        ch = command[i]
        if ch == "'":
            close = command.find("'", i + 1)
            close = n if close < 0 else close
            word.append(command[i + 1:close])
            in_word = True
            i = close + 1
            continue
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and command[j] != '"':
                if command[j] in "\\`" and j + 1 < n:
                    j += 1
                buf.append(command[j])
                j += 1
            word.append("".join(buf))
            in_word = True
            i = j + 1
            continue
        if ch == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2  # line continuation
                continue
            word.append(command[i + 1])
            in_word = True
            i += 2
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            end_simple(two, i)
            i += 2
            text_start = i
            continue
        if ch == "|":
            end_simple("|", i)
            i += 1
            text_start = i
            continue
        if ch == "&" and not _is_redirect_amp(command, i):
            end_statement("&", i)
            i += 1
            text_start = i
            continue
        if ch in ";\n":
            end_statement(";" if ch == ";" else None, i)
            i += 1
            text_start = i
            continue
        if ch in " \t\r":
            end_word()
            i += 1
            continue
        word.append(ch)
        in_word = True
        i += 1
    end_statement(None, n)
    # The final simple command of a statement ended by ``;``/newline has no
    # operator after it within the statement.
    for statement in statements:
        if statement and statement[-1].op_after in (";", None):
            statement[-1].op_after = None
    return statements


def _is_redirect_amp(command: str, i: int) -> bool:
    """``2>&1``, ``>&2``, ``&>file`` and PowerShell's ``& 'prog'`` call operator are not separators."""
    prev = command[i - 1] if i > 0 else ""
    nxt = command[i + 1] if i + 1 < len(command) else ""
    if prev == ">" or nxt == ">":
        return True
    # PowerShell call operator at the start of a command: ``& "C:/py.exe" -m pytest``.
    before = command[:i].rstrip()
    return (not before or before[-1] in ";|&(\n") and nxt in " \t'\""


def _unwrap(words: list[str]) -> list[str]:
    """Drop subshell parentheses, assignments, the PowerShell call operator and wrapper programs."""
    out = [w for w in words if w not in ("(", ")", "{", "}", "&")]
    if out and out[0].startswith("("):
        out[0] = out[0].lstrip("(")
    if out and out[-1].endswith(")"):
        out[-1] = out[-1].rstrip(")")
    out = [w for w in out if w]
    while out:
        head = _program(out[0])
        if _ASSIGNMENT_RE.match(out[0]):
            out = out[1:]
        elif head in _WRAPPERS:
            out = out[1:]
        elif head == "timeout" and len(out) > 1:
            out = out[2:]
        elif head in _RUNNERS and len(out) > 1 and out[1] == _RUNNERS[head]:
            out = out[2:]
        elif head == "corepack" and len(out) > 1:
            out = out[1:]
        else:
            break
    return out


def _program(word: str) -> str:
    name = re.split(r"[\\/]", word)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _is_setup(words: list[str]) -> bool:
    if not words:
        return True
    head = _program(words[0])
    return head in _SETUP_PROGRAMS or (len(words) >= 2 and head == "git" and words[1] in ("status", "rev-parse"))


# -- classification ------------------------------------------------------------


def _kind_of(words: list[str]) -> str:
    if not words:
        return KIND_OTHER
    head = _program(words[0])
    rest = words[1:]
    if head in _PYTHONS:
        return _python_kind(rest)
    if head in _NODE_EXEC:
        target = _first_positional(rest)
        return _binary_kind(_program(target[0]), target[1:]) if target else KIND_OTHER
    if head in _PACKAGE_MANAGERS:
        return _package_manager_kind(head, rest)
    if head in _SUBCOMMANDS:
        sub = _first_positional(rest)
        return _SUBCOMMANDS[head].get(sub[0], KIND_OTHER) if sub else KIND_OTHER
    if head == "terraform" and rest and rest[0] == "fmt":
        return KIND_LINT if "-check" in rest else KIND_OTHER
    return _binary_kind(head, rest)


def _binary_kind(head: str, rest: list[str]) -> str:
    if head == "ruff":
        # ``ruff format`` rewrites files unless asked only to check.
        if rest and rest[0] == "format":
            return KIND_LINT if "--check" in rest or "--diff" in rest else KIND_OTHER
        return KIND_LINT
    if head == "biome" and rest and rest[0] == "format":
        return KIND_OTHER if "--write" in rest else KIND_LINT
    if head in _FORMATTER_CHECK_FLAGS:
        return KIND_LINT if any(w in _FORMATTER_CHECK_FLAGS[head] for w in rest) else KIND_OTHER
    if head == "tsc" and any(w in ("--watch", "-w") for w in rest):
        return KIND_OTHER  # never exits: no outcome to observe
    if head == "eslint" and "--fix" in rest:
        return KIND_LINT  # still exits non-zero on remaining problems
    if head in ("vitest", "jest") and rest and rest[0] == "watch":
        return KIND_OTHER
    if head in _SUBCOMMANDS:
        sub = _first_positional(rest)
        return _SUBCOMMANDS[head].get(sub[0], KIND_OTHER) if sub else KIND_OTHER
    return _BINARIES.get(head, KIND_OTHER)


def _python_kind(rest: list[str]) -> str:
    i = 0
    while i < len(rest):
        w = rest[i]
        if w == "-m" and i + 1 < len(rest):
            module = rest[i + 1].lower()
            return _module_kind(module, rest[i + 2:])
        if w.startswith("-m") and len(w) > 2:
            return _module_kind(w[2:].lower(), rest[i + 1:])
        if w in ("-c", "-") or not w.startswith("-"):
            return KIND_OTHER  # an inline program, stdin or a script file
        i += 1
    return KIND_OTHER


def _module_kind(module: str, rest: list[str]) -> str:
    if module in ("black", "isort"):
        return _binary_kind(module, rest)
    if module == "ruff":
        return _binary_kind("ruff", rest)
    if module == "pip":
        return KIND_OTHER
    return _PYTHON_MODULES.get(module, KIND_OTHER)


def _package_manager_kind(head: str, rest: list[str]) -> str:
    args = _first_positional(rest, value_flags=_PM_VALUE_FLAGS)
    if not args:
        return KIND_OTHER
    sub = args[0].lower()
    if sub in _PM_SUBCOMMANDS_EXEC:
        target = _first_positional(args[1:])
        return _binary_kind(_program(target[0]), target[1:]) if target else KIND_OTHER
    if sub in ("run", "run-script"):
        script = _first_positional(args[1:], value_flags=_PM_VALUE_FLAGS)
        return _script_kind(script[0].lower()) if script else KIND_OTHER
    if sub in ("test", "t", "tst"):
        return KIND_TEST
    if head in ("yarn", "pnpm", "bun") and sub not in ("install", "add", "remove", "i", "why", "list"):
        # ``pnpm lint`` / ``yarn vitest``: a script or a local binary by name.
        kind = _script_kind(sub)
        return kind if kind != KIND_OTHER else _binary_kind(_program(sub), args[1:])
    return KIND_OTHER


def _script_kind(script: str) -> str:
    base = script.split(":", 1)[0]
    if base in ("test", "tests", "e2e", "vitest", "jest", "coverage"):
        return KIND_TEST
    if base in ("lint", "eslint", "stylelint"):
        return KIND_LINT
    if base in ("typecheck", "type-check", "types", "check-types", "tsc"):
        return KIND_TYPECHECK
    if base == "build":
        return KIND_BUILD
    if script in ("format:check", "fmt:check", "prettier:check"):
        return KIND_LINT
    return KIND_OTHER


def _first_positional(words: list[str], *, value_flags: frozenset[str] = frozenset()) -> list[str]:
    """*words* from the first non-flag argument on (skipping the value of a flag that takes one)."""
    i = 0
    while i < len(words):
        w = words[i]
        if not w.startswith("-"):
            return words[i:]
        if w.lower() in value_flags and "=" not in w:
            i += 2
            continue
        i += 1
    return []


# -- capture queue ---------------------------------------------------------------

_REASON_CODE_RE = re.compile(r"^[a-z_]{1,64}$")
MAX_CHECKS_PER_COMMAND = 10


def check_command_items(command: str | None, name_of) -> list[dict]:
    """The queue form of *command*'s checks: ``{"kind", "name", "unattributed", "failure_unattributed"}``.

    *name_of(segment, kind)* turns the raw check segment into a stored name
    (the caller's scrubber); the raw command never leaves this call.
    """
    return [
        {"kind": c.kind, "name": name_of(c.segment, c.kind), "unattributed": c.unattributed,
         "failure_unattributed": c.failure_unattributed}
        for c in classify_check_commands(command)[:MAX_CHECKS_PER_COMMAND]
    ]


def queued_check_commands(value: object) -> list[dict] | None:
    """Queued check items, keeping only well-formed ones (None when the line predates them)."""
    if not isinstance(value, list):
        return None
    out: list[dict] = []
    for item in value[:MAX_CHECKS_PER_COMMAND]:
        if not isinstance(item, dict) or item.get("kind") not in CHECK_KINDS:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean = {"kind": item["kind"], "name": name[:160]}
        for field_name in ("unattributed", "failure_unattributed"):
            raw = item.get(field_name)
            clean[field_name] = raw if isinstance(raw, str) and _REASON_CODE_RE.match(raw) else None
        out.append(clean)
    return out


def item_outcome(item: dict, command_outcome: str) -> tuple[str, str | None]:
    """``(status, reason)`` for one queued check item given the command's outcome."""
    check = CheckCommand(item["kind"], None, item.get("unattributed"), item.get("failure_unattributed"))
    return check_outcome(check, command_outcome), unattributed_reason(check, command_outcome)
