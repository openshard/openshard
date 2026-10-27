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

# Whose exit status the captured shell tool reports (see classify_check_commands).
SHELL_POSIX = "posix"
SHELL_POWERSHELL = "powershell"

_MAX_COMMAND_CHARS = 20_000

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_PS_HERESTRING_RE = re.compile(r"@(['\"])\r?\n.*?\r?\n\1@", re.DOTALL)
# Characters a POSIX shell backslash is used to escape in practice. Before
# anything else (``\t`` in ``C:\tmp``) the backslash is kept, as PowerShell
# and cmd read it.
_ESCAPABLE = frozenset(" \t\\'\"$`&|;<>()*?[]{}#~!")
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
    # ``"<family>:full"`` / ``"<family>:narrow"`` for a test-runner run whose
    # scope can be read off its words (see ``_suite``); None otherwise.
    suite: str | None = None
    # Set when the line itself makes this check unable to fail (``|| true``,
    # a final ``exit 0``, ``set +e`` before it): one of EXIT_MASKS. An
    # observation about the command text, never a verdict.
    exit_masked: str | None = None
    # True for a pytest or vitest run whose exit status is hidden only by a
    # pipe or a later statement, on a line where every other command is a
    # setup step or a line filter that cannot write a summary line of its own
    # (see ``_summary_readable``): the runner's own summary in the output is
    # then its outcome (``runner_summary_outcome``).
    summary_readable: bool = False
    # ``"pytest"`` / ``"vitest"`` for a ``summary_readable`` check: which
    # runner's summary format stands for its outcome.
    summary_runner: str | None = None

    @property
    def is_check(self) -> bool:
        return self.kind in CHECK_KINDS


_OTHER = CheckCommand(KIND_OTHER)


@dataclass
class _Simple:
    words: list[str]
    text: str
    op_after: str | None  # the operator that ended it: && || | ; & or None at the end
    start: int = 0  # offset of its text in the parsed command
    in_block: bool = False  # led by a control keyword (``if pytest; then``)


def classify_check_command(command: str | None, *, shell: str = SHELL_POSIX) -> CheckCommand:
    """The last check *command* runs (``kind`` "other" when it runs none)."""
    checks = classify_check_commands(command, shell=shell)
    return checks[-1] if checks else _OTHER


def classify_check_commands(command: str | None, *, shell: str = SHELL_POSIX) -> list[CheckCommand]:
    """Every check *command* runs, in order, each with whether the exit status is its own.

    POSIX shells (Claude Code's Bash tool): the exit status is the last
    pipeline element's of the final statement -- unless ``set -o pipefail``
    is on, or the line ends ``exit $?`` / ``exit ${PIPESTATUS[k]}``. Every
    member of that statement's ``&&`` chain had to succeed for it to be
    zero, so a success of the whole line is each such check's success. A
    failure is pinned on a check only when everything else in the chain is
    a setup step.

    PowerShell (Claude Code's PowerShell tool): the reported exit code is
    ``$LASTEXITCODE``, the last *native program's*. Cmdlets never change it
    (``npx vitest run | Select-Object -Last 20`` reports vitest's code), so
    a check is attributable when no native program runs after it.

    Either shell: a check inside ``if``/loops/blocks is recorded as run but
    its outcome is never attributed (whether it ran, and which command set
    the status, depends on a condition).
    """
    return _classify(command, depth=0, shell=shell)


def shell_for_tool(tool_name: str | None) -> str:
    """The exit-status rules of a captured shell tool (Claude Code's ``PowerShell`` tool, else POSIX)."""
    return SHELL_POWERSHELL if isinstance(tool_name, str) and tool_name.lower() == "powershell" else SHELL_POSIX


def _classify(command: str | None, *, depth: int, shell: str = SHELL_POSIX) -> list[CheckCommand]:
    if not isinstance(command, str) or not command.strip():
        return []
    text = _strip_inline_documents(command[:_MAX_COMMAND_CHARS])
    statements = _parse(text)
    for statement in statements:
        for simple in statement:
            simple.words, simple.in_block = _strip_keywords(simple.words, shell)
    flat = [(si, ci) for si, st in enumerate(statements) for ci in range(len(st))]
    # A command line that changes directory may be running another package's
    # tests: none of its checks gets a suite (never superseding, never superseded).
    moves_dir = any(s.words and _program(s.words[0]) in _DIR_CHANGE for st in statements for s in st)
    out: list[CheckCommand] = []
    for si, statement in enumerate(statements):
        for ci, simple in enumerate(statement):
            if shell == SHELL_POWERSHELL:
                outer, outer_failure = _position_powershell(statements, flat, si, ci)
            else:
                outer, outer_failure = _position(statements, si, ci)
            blocked = outer is None and (simple.in_block or _block_depth(text[:simple.start], shell) > 0)
            if blocked:
                outer, outer_failure = UNATTRIBUTED_NOT_LAST, None
            script = _shell_script(simple.words) if depth < 2 else None
            if script is not None:
                # ``bash -lc "pytest -q"``: the inner script's exit status is the shell's.
                mask = _exit_mask(statements, si, ci, shell)
                for inner in _classify(script, depth=depth + 1):
                    out.append(CheckCommand(inner.kind, inner.segment, inner.unattributed or outer,
                                            inner.failure_unattributed or outer_failure,
                                            None if moves_dir else inner.suite, inner.exit_masked or mask))
                continue
            kind = _kind_of(simple.words)
            if kind != KIND_OTHER:
                suite = _suite(simple.words) if kind == KIND_TEST and not moves_dir else None
                readable = not blocked and _summary_readable(statements, si, ci, outer, depth)
                out.append(CheckCommand(kind, _display(simple.words), outer, outer_failure, suite,
                                        _exit_mask(statements, si, ci, shell), readable,
                                        _summary_runner(simple.words) if readable else None))
    return out


# Line filters that may sit after pytest in its own pipeline: they pass on
# some of pytest's stdout lines and cannot write a summary line pytest did
# not print. Their arguments are checked too: ``tail -c`` could cut a line
# to a passing-looking tail and ``tail old.log`` would read a stale file;
# grep (``-o``), sed and awk can rewrite a line and are never filters here.
_LINE_FILTERS = frozenset({"tail", "head", "tee", "select-object", "select", "out-string"})
_LINE_COUNT_RE = re.compile(r"^(?:-\d+|-n[+-]?\d+|--lines=[+-]?\d+|-[qs]|--quiet|--silent)$")
_SELECT_ARG_RE = re.compile(r"^(?:-(?:last|first|skip)|\d+)$", re.IGNORECASE)
# Steps that may follow pytest's statement without printing anything.
_SILENT_AFTER = frozenset({"cd", "pushd", "popd", "set-location", "sl", "true", ":"})


def _summary_readable(
    statements: list[list[_Simple]], si: int, ci: int, outer: str | None, depth: int,
) -> bool:
    """Whether the test runner's own summary in the output may stand for its hidden exit status.

    Only for a pytest or vitest run (``_summary_runner``) hidden by a pipe or a later statement (never
    ``||``, a background ``&`` or a block), on a line where every command
    before pytest is a setup step, every command after it in its pipeline is
    a line filter of its output (``_filter_ok``), and every later statement
    is a silent directory change.
    """
    if depth or outer not in (UNATTRIBUTED_PIPED, UNATTRIBUTED_NOT_LAST):
        return False
    statement = statements[si]
    if _summary_runner(statement[ci].words) is None or any(s.op_after in ("||", "&") for s in statement):
        return False
    for sj, other in enumerate(statements):
        for cj, simple in enumerate(other):
            if (sj, cj) == (si, ci):
                continue
            if simple.in_block:
                return False
            if (sj, cj) < (si, ci):
                if not _is_setup(simple.words):
                    return False
            elif sj == si:
                if statement[cj - 1].op_after != "|" or not _filter_ok(simple.words):
                    return False
            elif _program(simple.words[0]) not in _SILENT_AFTER:
                return False
    return True


def _filter_ok(words: list[str]) -> bool:
    head, args = _program(words[0]), words[1:]
    if head in ("tail", "head"):
        i = 0
        while i < len(args):
            if args[i] in ("-n", "--lines") and i + 1 < len(args) and re.match(r"^[+-]?\d+$", args[i + 1]):
                i += 2
                continue
            if not _LINE_COUNT_RE.match(args[i]):
                return False
            i += 1
        return True
    if head == "tee":
        return True  # writes copies to its files; passes every line on unchanged
    if head in ("select-object", "select"):
        return all(_SELECT_ARG_RE.match(a) for a in args)
    if head == "out-string":
        return not args
    return False


def _is_pytest(words: list[str]) -> bool:
    if not words:
        return False
    head, rest = _program(words[0]), words[1:]
    if head in ("pytest", "py.test"):
        return True
    return head in _PYTHONS and len(rest) >= 2 and rest[0] == "-m" and rest[1].lower() == "pytest"


def _is_vitest(words: list[str]) -> bool:
    """A direct vitest run: ``vitest``, ``npx vitest``, ``pnpm vitest``, ``pnpm exec vitest``.

    Only these exact shapes: a package-manager flag before ``vitest``
    (``pnpm -r``, ``--filter``) may run it in several packages, each printing
    its own summary, so the last one in the output would not speak for all.
    ``pnpm test`` is excluded for the same reason: the script may run anything.
    """
    if not words:
        return False
    head, rest = _program(words[0]), words[1:]
    if head == "vitest":
        return True
    if head in _NODE_EXEC:
        rest = rest[1:] if rest and rest[0] in ("-y", "--yes") else rest
        return bool(rest) and _program(rest[0]) == "vitest"
    if head in _PACKAGE_MANAGERS:
        if rest and rest[0] in _PM_SUBCOMMANDS_EXEC:
            rest = rest[1:]
        return bool(rest) and rest[0] == "vitest"
    return False


def _summary_runner(words: list[str]) -> str | None:
    if _is_pytest(words):
        return "pytest"
    if _is_vitest(words):
        return "vitest"
    return None


# pytest's final summary line: ``==== 3 failed, 507 passed in 12.31s ====``
# (or, with ``-q``, the same without the ``=`` padding). Only the counts are
# read; nothing else of the output is kept.
_PYTEST_SUMMARY_RE = re.compile(
    r"^=*\s*(?P<counts>(?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)"
    r"(?:, )?)+|no tests ran) in \d+(?:\.\d+)?s(?: \([0-9:.]+\))?\s*=*$"
)
_PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)")
_PYTEST_INTERRUPTED_RE = re.compile(r"KeyboardInterrupt|!!+ .*[Ii]nterrupted")
_SUMMARY_TAIL_CHARS = 8_000
_SUMMARY_TAIL_LINES = 40


def pytest_summary_outcome(output: object) -> str | None:
    """``passed`` / ``failed`` from pytest's last summary line in *output*, else None.

    Read the way pytest sets its exit status: any failed test or error ->
    ``failed``; ``no tests ran`` -> ``failed`` (pytest exits 5); otherwise a
    line of passed/skipped/xfailed counts -> ``passed``. No summary line, or
    an interrupted run (pytest prints a summary of what ran before the
    interrupt), -> None: unknown, never a guessed pass. Only the last
    ``_SUMMARY_TAIL_CHARS`` characters are scanned.
    """
    if not isinstance(output, str) or not output:
        return None
    lines = output[-_SUMMARY_TAIL_CHARS:].splitlines()[-_SUMMARY_TAIL_LINES:]
    if any(_PYTEST_INTERRUPTED_RE.search(line) for line in lines):
        return None
    for line in reversed(lines):
        match = _PYTEST_SUMMARY_RE.match(line.strip())
        if not match:
            continue
        counts = {word.rstrip("s") if word.startswith(("error", "warning")) else word: int(n)
                  for n, word in _PYTEST_COUNT_RE.findall(match.group("counts"))}
        if match.group("counts") == "no tests ran" or counts.get("failed") or counts.get("error"):
            return "failed"
        ran = sum(counts.get(k, 0) for k in ("passed", "skipped", "xfailed", "xpassed"))
        return "passed" if ran else "failed"  # only deselected: pytest exits 5
    return None


# vitest's closing summary block, e.g.
#    Test Files  1 failed | 12 passed (13)
#         Tests  2 failed | 140 passed | 3 skipped (145)
#        Errors  1 error
#      Start at  14:24:35
#      Duration  3.21s (transform 42%, ...)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VITEST_ROW_RE = re.compile(r"^(Test Files|Tests|Errors|Type Errors|Snapshots|Start at|Duration)\s+(.*)$")
_VITEST_COUNT_RE = re.compile(r"(\d+) ([a-z][a-z ]*?)(?= \||\s*\(|$)")
_VITEST_FAILING_ROWS = ("Errors", "Type Errors")


def vitest_summary_outcome(output: object) -> str | None:
    """``passed`` / ``failed`` from vitest's closing summary block in *output*, else None.

    ``failed`` when the block reports a failed test file, test or snapshot,
    or any (type) error: vitest exits non-zero for each. ``passed`` only when
    the block reports passed test files and tests, nothing failed, and the
    block is the very end of the output (``Duration`` last), so nothing printed
    after it -- a coverage threshold, a reporter error -- can have failed the
    run unseen. Anything else, a cancelled run, or no block -> None: unknown,
    never a guessed pass. Only the last ``_SUMMARY_TAIL_CHARS`` are scanned.
    """
    if not isinstance(output, str) or not output:
        return None
    lines = [_ANSI_RE.sub("", line).strip() for line in output[-_SUMMARY_TAIL_CHARS:].splitlines()]
    lines = lines[-_SUMMARY_TAIL_LINES:]
    while lines and not lines[-1]:
        lines.pop()
    starts = [i for i, line in enumerate(lines) if line.startswith("Test Files ")]
    if not starts or any("cancel" in line.lower() for line in lines):
        return None
    rows: dict[str, dict[str, int]] = {}
    clean = True
    for line in lines[starts[-1]:]:
        match = _VITEST_ROW_RE.match(line)
        if not match:
            clean = False
            break
        rows[match.group(1)] = {word: int(n) for n, word in _VITEST_COUNT_RE.findall(match.group(2))}
    failing = any(rows.get(row, {}).get("failed") for row in ("Test Files", "Tests", "Snapshots"))
    if failing or any(sum(rows[row].values()) for row in _VITEST_FAILING_ROWS if row in rows):
        return "failed"
    if (clean and lines[-1].startswith("Duration") and rows.get("Test Files", {}).get("passed")
            and rows.get("Tests", {}).get("passed")):
        return "passed"
    return None


def runner_summary_outcome(command: str | None, output: object, *, shell: str = SHELL_POSIX) -> str | None:
    """The outcome of *command*'s summary-readable test run, read off its runner's summary in *output*.

    None unless exactly one check on the line is ``summary_readable``.
    """
    runners = {c.summary_runner for c in classify_check_commands(command, shell=shell) if c.summary_readable}
    if len(runners) != 1:
        return None
    runner = runners.pop()
    if runner == "pytest":
        return pytest_summary_outcome(output)
    if runner == "vitest":
        return vitest_summary_outcome(output)
    return None


# How a command line can make its own check unable to fail (see _exit_mask).
MASK_OR_TRUE = "or_true"  # pytest || true
MASK_OR_NOOP = "or_noop"  # pytest || :
MASK_OR_EXIT_ZERO = "or_exit_zero"  # pytest || exit 0
MASK_EXIT_ZERO = "exit_zero"  # pytest; exit 0
MASK_SET_PLUS_E = "set_plus_e"  # set +e; pytest
EXIT_MASKS = frozenset({MASK_OR_TRUE, MASK_OR_NOOP, MASK_OR_EXIT_ZERO, MASK_EXIT_ZERO, MASK_SET_PLUS_E})
_NO_FAIL_FALLBACKS = {"true": MASK_OR_TRUE, ":": MASK_OR_NOOP, "exit 0": MASK_OR_EXIT_ZERO}


def _exit_mask(statements: list[list[_Simple]], si: int, ci: int, shell: str) -> str | None:
    """Whether the POSIX line around simple command *ci* of statement *si* can never report its failure.

    Only the explicit, unambiguous forms: a ``|| true`` / ``|| :`` /
    ``|| exit 0`` after it in its own statement, a later top-level
    ``exit 0``, or ``set +e`` earlier in the line. A pipe or a later
    ``echo`` also hides the status but is too common in ordinary use to
    call out, so it is left to the ``unattributed`` reasons above.
    """
    if shell != SHELL_POSIX:
        return None
    statement = statements[si]
    for j in range(ci + 1, len(statement)):
        fallback = _NO_FAIL_FALLBACKS.get(" ".join(statement[j].words))
        if statement[j - 1].op_after == "||" and fallback:
            return fallback
    if any(st and st[0].words == ["exit", "0"] for st in statements[si + 1:]):
        return MASK_EXIT_ZERO
    earlier = [s for st in statements[:si] for s in st] + statement[:ci]
    if any(s.words[:1] == ["set"] and "+e" in s.words[1:] for s in earlier):
        return MASK_SET_PLUS_E
    return None


_EXIT_STATUS_WORD = "$?"
_PIPESTATUS_RE = re.compile(r"^\$\{PIPESTATUS\[(\d{1,2})\]\}$")


def _deciding(statements: list[list[_Simple]]) -> tuple[int, int | None]:
    """``(statement index, pipeline element)`` whose status the POSIX line exits with.

    The final statement, unless it is ``exit $?`` (then the one before it) or
    ``exit ${PIPESTATUS[k]}`` (element *k* of the last pipeline before it).
    ``None`` means the usual rule: the last element (or pipefail).
    """
    last = len(statements) - 1
    final = statements[last]
    if last > 0 and len(final) == 1 and len(final[0].words) == 2 and final[0].words[0] == "exit":
        arg = final[0].words[1]
        if arg == _EXIT_STATUS_WORD:
            return last - 1, None
        match = _PIPESTATUS_RE.match(arg)
        if match:
            return last - 1, int(match.group(1))
    return last, None


def _pipefail_before(statements: list[list[_Simple]], si: int, ci: int) -> bool:
    """Whether ``set -o pipefail`` (or ``set -euo pipefail``...) ran earlier in the line."""
    for sj, statement in enumerate(statements):
        for cj, simple in enumerate(statement):
            if (sj, cj) >= (si, ci):
                return False
            w = simple.words
            if w and w[0] == "set" and "pipefail" in w:
                at = w.index("pipefail")
                if at > 0 and w[at - 1].startswith("-") and w[at - 1].endswith("o"):
                    return True
    return False


def _last_pipeline(statement: list[_Simple]) -> list[int]:
    """Indices of the statement's last pipeline (simple commands joined by ``|``)."""
    indices = [len(statement) - 1]
    for j in range(len(statement) - 2, -1, -1):
        if statement[j].op_after != "|":
            break
        indices.insert(0, j)
    return indices


def _position(statements: list[list[_Simple]], si: int, ci: int) -> tuple[str | None, str | None]:
    """Whether the POSIX line's exit status is that of simple command *ci* of statement *si*."""
    statement = statements[si]
    if any(s.op_after == "&" for s in statement):
        return UNATTRIBUTED_BACKGROUND, None
    deciding, element = _deciding(statements)
    if si != deciding:
        return UNATTRIBUTED_NOT_LAST, None
    if any(s.op_after == "||" for s in statement):
        return UNATTRIBUTED_ALTERNATIVE, None
    pipeline = _last_pipeline(statement)
    others = [j for j in range(len(statement)) if j != ci]
    if element is not None:
        # ``exit ${PIPESTATUS[k]}``: element *k* of the last pipeline is the status.
        if ci not in pipeline or pipeline.index(ci) != element:
            return UNATTRIBUTED_PIPED, None
        others = [j for j in others if j not in pipeline]
    elif statement[ci].op_after == "|":
        if not _pipefail_before(statements, si, ci):
            return UNATTRIBUTED_PIPED, None
        # pipefail: a zero status means every element succeeded; a failure
        # may be any element's.
        return None, UNATTRIBUTED_SHARED_FAILURE
    if any(not _is_setup(statement[j].words) for j in others):
        return None, UNATTRIBUTED_SHARED_FAILURE
    return None, None


def _position_powershell(
    statements: list[list[_Simple]], flat: list[tuple[int, int]], si: int, ci: int,
) -> tuple[str | None, str | None]:
    """Whether ``$LASTEXITCODE`` at the end of the PowerShell line is simple command *ci*'s.

    It is the last *native program's* exit code: cmdlets, assignments and
    keywords after the check leave it alone; any later native program, an
    ``exit N`` or an assignment to ``$LASTEXITCODE`` replaces it.
    """
    statement = statements[si]
    if any(s.op_after == "&" for s in statement):
        return UNATTRIBUTED_BACKGROUND, None
    if any(s.op_after == "||" for s in statement):
        return UNATTRIBUTED_ALTERNATIVE, None
    after = flat[flat.index((si, ci)) + 1:]
    chained_failure: str | None = None
    for sj, cj in after:
        later = statements[sj][cj]
        if _PS_LASTEXITCODE_SET_RE.search(later.text) or _ps_exit_overrides(later.words):
            return UNATTRIBUTED_NOT_LAST, None
        if _ps_native(later):
            if sj == si and all(statement[k].op_after == "&&" for k in range(ci, cj)):
                # ``check && other`` (PowerShell 7): ``other`` only runs after
                # the check succeeded, so a success is still the check's.
                chained_failure = UNATTRIBUTED_SHARED_FAILURE
                continue
            same_pipeline = sj == si and all(statement[k].op_after == "|" for k in range(ci, cj))
            return (UNATTRIBUTED_PIPED if same_pipeline else UNATTRIBUTED_NOT_LAST), None
    if chained_failure:
        return None, chained_failure
    # ``a && check``: a failure may be ``a``'s (a success means both ran clean).
    if any(not _is_setup(s.words) and _ps_native(s) for s in statement[:ci]):
        return None, UNATTRIBUTED_SHARED_FAILURE
    return None, None


_PS_LASTEXITCODE_SET_RE = re.compile(r"\$(?:global:)?LASTEXITCODE\s*=(?!=)", re.IGNORECASE)
# Cmdlet verbs that never run a native program themselves (Invoke-*, Start-*
# and anything unknown count as native, conservatively).
_PS_SAFE_VERBS = frozenset({
    "get", "set", "select", "where", "foreach", "write", "out", "tee", "sort", "measure", "format", "test",
    "new", "remove", "copy", "move", "join", "split", "convertto", "convertfrom", "add", "clear", "resolve",
    "push", "pop", "group", "compare", "read", "import", "export", "rename", "wait",
})
_PS_CMDLET_RE = re.compile(r"^([a-z]+)-[a-z][a-z0-9]*$")
_PS_ALIASES = frozenset({
    "echo", "write", "cd", "chdir", "sl", "pwd", "ls", "dir", "gci", "cat", "gc", "type", "select", "where",
    "sort", "tee", "measure", "fl", "ft", "fw", "oh", "%", "?", "sleep", "cls", "clear", "rm", "del", "ri",
    "cp", "copy", "mv", "move", "mkdir", "md", "ni", "gi", "gp", "gl", "gm", "foreach", "exit", "return",
    "throw", "break", "continue", "param", "function",
})


def _ps_native(simple: _Simple) -> bool:
    """Whether a PowerShell simple command may run a native program (and so set ``$LASTEXITCODE``)."""
    words = simple.words
    if not words:
        return False
    head = words[0]
    if head.startswith(("$", "@", "'", '"', "[")) or re.match(r"^-?\d", head):
        return False  # an expression, not a command
    if head.lower().endswith((".exe", ".cmd", ".bat", ".com")):
        return True  # ``where.exe`` is a program, not the ``where`` alias
    name = _program(head)
    match = _PS_CMDLET_RE.match(name)
    cmdlet = name in _PS_ALIASES or (match is not None and match.group(1) in _PS_SAFE_VERBS)
    # A cmdlet with a script block (``ForEach-Object { ... }``) may run a program in it.
    return not cmdlet or "{" in simple.text


def _ps_exit_overrides(words: list[str]) -> bool:
    """``exit`` with anything but ``$LASTEXITCODE`` sets the reported code itself."""
    if not words or words[0].lower() != "exit":
        return False
    return len(words) != 2 or words[1].lower() != "$lastexitcode"


_POSIX_KEYWORDS = frozenset({"if", "then", "elif", "else", "do", "while", "until", "!"})
_PS_KEYWORDS = frozenset({"if", "elseif", "else", "while", "for", "foreach", "do", "until", "try", "finally",
                          "switch"})
_PS_ASSIGN_RE = re.compile(r"^\$[A-Za-z_][\w:]*$")


def _strip_keywords(words: list[str], shell: str) -> tuple[list[str], bool]:
    """*words* without leading control keywords (and a PowerShell ``(condition)``), plus whether any were dropped.

    ``if pytest; then`` still runs pytest, so it is still a check -- but one
    whose outcome the line's status cannot be pinned on.
    """
    keywords = _PS_KEYWORDS if shell == SHELL_POWERSHELL else _POSIX_KEYWORDS
    out = list(words)
    dropped = False
    while out:
        head = out[0].lower()
        if head in keywords:
            out = out[1:]
            dropped = True
            if shell == SHELL_POWERSHELL and out and out[0].startswith("("):
                while out:
                    word = out.pop(0)
                    if word.endswith(")"):
                        break
            continue
        if shell == SHELL_POWERSHELL and len(out) > 2 and _PS_ASSIGN_RE.match(out[0]) and out[1] == "=":
            out = out[2:]  # ``$r = npx vitest run``: the program still runs
            continue
        break
    return (_unwrap(out) if dropped else out), dropped


_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\`]|[\\`].)*\"")
_EXPANSION_RE = re.compile(r"[$@]\{[^}]*\}")
_POSIX_TOKEN_RE = re.compile(r"[^\s;&|()]+|[;&|()\n]")
_POSIX_OPENERS = frozenset({"if", "while", "until", "for", "case", "select", "{"})
_POSIX_CLOSERS = frozenset({"fi", "done", "esac", "}"})
_POSIX_COMMAND_START = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "{"})


def _block_depth(prefix: str, shell: str) -> int:
    """How many compound commands / script blocks are open at the end of *prefix*."""
    text = _EXPANSION_RE.sub("", _QUOTED_RE.sub("''", prefix))
    if shell == SHELL_POWERSHELL:
        return max(0, text.count("{") - text.count("}"))
    depth = 0
    command_position = True
    for token in _POSIX_TOKEN_RE.findall(text):
        if token in (";", "&", "|", "(", ")", "\n"):
            command_position = True
            continue
        if command_position and token in _POSIX_OPENERS:
            depth += 1
        elif command_position and token in _POSIX_CLOSERS:
            depth = max(0, depth - 1)
        command_position = token in _POSIX_COMMAND_START
    return depth


_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"})
_SCRIPT_FLAGS = frozenset({"-c", "-lc", "-ic", "-lic", "-command", "/c", "/k"})


def _shell_script(words: list[str]) -> str | None:
    """The script of ``bash -lc SCRIPT`` / ``pwsh -Command SCRIPT`` / ``cmd /c SCRIPT``, else None."""
    if len(words) < 3 or _program(words[0]) not in _SHELLS:
        return None
    for i, word in enumerate(words[1:], start=1):
        if word.lower() in _SCRIPT_FLAGS:
            rest = words[i + 1:]
            return " ".join(rest) if rest else None
        if not word.startswith(("-", "/")):
            return None  # ``bash script.sh``: a script file, not inline commands
    return None


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
            current.append(_Simple(_unwrap(words), command[text_start:at].strip(), op, text_start))
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
                # POSIX: inside double quotes a backslash escapes only these;
                # elsewhere (``"C:\\My Proj\\python.exe"``) it is kept.
                if j + 1 < n and (command[j] == "`" or (command[j] == "\\" and command[j + 1] in '"\\$`\n')):
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
            if command[i + 1] in _ESCAPABLE:
                word.append(command[i + 1])
                in_word = True
                i += 2
                continue
            # A Windows path (``C:\tmp\venv\python.exe`` in PowerShell or cmd):
            # the backslash is a separator there, not an escape.
            word.append(ch)
            in_word = True
            i += 1
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


# -- test-suite scope (which later run supersedes an earlier failure) -------------
#
# The rule, deliberately narrow. An earlier *failed* run of a test runner is
# superseded by a later *passed* run only when all of these hold:
#
# 1. both ran in the same working directory (the hook's ``cwd``, compared as a
#    hash) and neither command line changed directory (``cd``/``pushd``...);
# 2. the earlier run was *narrow*: it named files, a filter or any argument
#    not on the small allowlist below, so it ran a subset of the suite;
# 3. the later run was *full*: the runner, or the package manager's ``test``
#    script, with no arguments beyond that allowlist (output/verbosity only);
# 4. the later run is of the same runner, or is the package manager's
#    ``test`` script (``npm test``, ``npm run test``, ``pnpm test``,
#    ``yarn test``) and the earlier run was a JS test runner or a narrowed
#    ``test`` script. The project's ``test`` script is taken to be its full JS
#    suite; a full ``vitest run`` does *not* supersede a narrowed ``npm test``
#    (the script may run more than vitest);
# 5. the later pass is attributable to the check itself (not piped etc.).
#
# Anything else -- another runner, another script (``test:unit``), a
# workspace/prefix flag, an unrecognised argument -- has no suite and never
# supersedes anything: the earlier failure stands. An earlier *full* run that
# failed is only ever superseded by re-running the same command.

SUITE_FULL = "full"
SUITE_NARROW = "narrow"
SUITE_PM_TEST = "pm_test"
_JS_TEST_RUNNERS = frozenset({"vitest", "jest", "mocha", "ava"})
SUITE_FAMILIES: frozenset[str] = _JS_TEST_RUNNERS | {"pytest", SUITE_PM_TEST}
_DIR_CHANGE = frozenset({"cd", "pushd", "popd", "chdir", "set-location", "sl", "push-location", "pop-location"})
# Arguments that change only output or stop-on-first-failure, never which tests run.
_SUITE_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset({"-q", "-qq", "-v", "-vv", "-vvv", "-x", "--exitfirst", "--quiet", "--verbose",
                         "--no-header", "--color=yes", "--color=no"}),
    "vitest": frozenset({"--run", "--silent", "--color", "--no-color", "--bail=1"}),
    "jest": frozenset({"--ci", "--silent", "--verbose", "--color", "--no-color", "--bail"}),
    "mocha": frozenset({"--color", "--no-color", "--bail"}),
    "ava": frozenset({"--verbose", "--color", "--no-color", "--fail-fast"}),
    SUITE_PM_TEST: frozenset({"--silent", "-s", "--quiet", "-q"}),
}
_SUITE_KEY_RE = re.compile(r"^[0-9a-f]{16}:(?P<family>[a-z_]+):(?P<scope>full|narrow)$")


def _suite(words: list[str]) -> str | None:
    """``"<family>:full"`` / ``"<family>:narrow"`` for a test-runner run, else None (see the rule above)."""
    if not words:
        return None
    head, rest = _program(words[0]), _without_redirects(words[1:])
    if head in _PYTHONS:
        if len(rest) >= 2 and rest[0] == "-m" and rest[1].lower() == "pytest":
            return _runner_suite("pytest", rest[2:])
        return None
    if head in ("pytest", "py.test"):
        return _runner_suite("pytest", rest)
    if head in _NODE_EXEC:
        return _js_runner_suite(rest)
    if head in _PACKAGE_MANAGERS:
        if not rest or rest[0].startswith("-"):
            return None  # ``--prefix``/``--filter``/``-w``: maybe another package
        sub = rest[0].lower()
        if sub in _PM_SUBCOMMANDS_EXEC:
            return _js_runner_suite(rest[1:])
        if sub in ("run", "run-script"):
            if len(rest) < 2 or rest[1] != "test":
                return None
            return _runner_suite(SUITE_PM_TEST, rest[2:])
        if sub in ("test", "t", "tst"):
            return None if head == "bun" else _runner_suite(SUITE_PM_TEST, rest[1:])
        if head in ("yarn", "pnpm") and sub in _JS_TEST_RUNNERS:
            return _js_runner_suite(rest)
        return None
    if head in _JS_TEST_RUNNERS:
        return _js_runner_suite(words[:1] + rest)
    return None


def _js_runner_suite(words: list[str]) -> str | None:
    if not words or words[0].startswith("-"):
        return None  # ``npx -p pkg vitest``: not read
    runner = _program(words[0])
    if runner not in _JS_TEST_RUNNERS:
        return None
    args = _without_redirects(words[1:])
    if runner == "vitest" and args and args[0] == "run":
        args = args[1:]
    return _runner_suite(runner, args)


def _runner_suite(family: str, args: list[str]) -> str:
    full = all(a in _SUITE_FLAGS[family] for a in args)
    return f"{family}:{SUITE_FULL if full else SUITE_NARROW}"


def _without_redirects(words: list[str]) -> list[str]:
    out: list[str] = []
    skip_target = False
    for w in words:
        if skip_target:
            skip_target = False
            continue
        redirect = _REDIRECT_RE.match(w)
        if redirect:
            skip_target = not redirect.group(1)
            continue
        out.append(w)
    return out


def suite_key(suite: str | None, context: str | None) -> str | None:
    """The queued form of *suite*: ``"<16 hex of the context>:<family>:<scope>"``.

    *context* is the working directory the agent reported; only its hash is
    kept. None when either is missing -- an unknown directory never matches.
    """
    if not suite or not isinstance(context, str) or not context.strip():
        return None
    import hashlib

    digest = hashlib.sha256(context.strip().encode("utf-8", "replace")).hexdigest()[:16]
    key = f"{digest}:{suite}"
    return key if _valid_suite_key(key) else None


def _valid_suite_key(value: object) -> bool:
    match = _SUITE_KEY_RE.match(value) if isinstance(value, str) else None
    return match is not None and match.group("family") in SUITE_FAMILIES


def suite_supersedes(later: object, earlier: object) -> bool:
    """True when a *passed* run with suite key *later* supersedes an earlier *failed* run with *earlier*."""
    if not (_valid_suite_key(later) and _valid_suite_key(earlier)):
        return False
    l_dir, l_family, l_scope = str(later).split(":")
    e_dir, e_family, e_scope = str(earlier).split(":")
    if l_dir != e_dir or l_scope != SUITE_FULL or e_scope != SUITE_NARROW:
        return False
    if l_family == e_family:
        return True
    return l_family == SUITE_PM_TEST and e_family in _JS_TEST_RUNNERS


# -- capture queue ---------------------------------------------------------------

_REASON_CODE_RE = re.compile(r"^[a-z_]{1,64}$")
MAX_CHECKS_PER_COMMAND = 10


def check_command_items(
    command: str | None, name_of, *, shell: str = SHELL_POSIX, context: str | None = None,
    test_summary: str | None = None,
) -> list[dict]:
    """The queue form of *command*'s checks: ``{"kind", "name", "unattributed", "failure_unattributed"}``.

    *name_of(segment, kind)* turns the raw check segment into a stored name
    (the caller's scrubber); the raw command never leaves this call. With a
    *context* (the reported working directory) a test-runner check also
    carries ``suite`` (``suite_key``: a hash of the directory, never its text).
    *test_summary* is ``runner_summary_outcome`` of the command's reported
    output; a ``summary_readable`` check carries it as ``summary_outcome``.
    """
    items: list[dict] = []
    for c in classify_check_commands(command, shell=shell)[:MAX_CHECKS_PER_COMMAND]:
        item = {"kind": c.kind, "name": name_of(c.segment, c.kind), "unattributed": c.unattributed,
                "failure_unattributed": c.failure_unattributed}
        key = suite_key(c.suite, context)
        if key is not None:
            item["suite"] = key
        if c.exit_masked and c.kind == KIND_TEST:
            item["exit_masked"] = c.exit_masked
        if c.summary_readable and test_summary in ("passed", "failed"):
            item["summary_outcome"] = test_summary
        items.append(item)
    return items


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
        if _valid_suite_key(item.get("suite")):
            clean["suite"] = item["suite"]
        if item.get("exit_masked") in EXIT_MASKS:
            clean["exit_masked"] = item["exit_masked"]
        if item.get("summary_outcome") in ("passed", "failed"):
            clean["summary_outcome"] = item["summary_outcome"]
        out.append(clean)
    return out


def item_outcome(item: dict, command_outcome: str) -> tuple[str, str | None]:
    """``(status, reason)`` for one queued check item given the command's outcome.

    A pytest or vitest run whose exit status a pipe or a later statement hid
    takes its outcome from its own summary (``summary_outcome``) once the command
    completed: a failed summary always, a passed one only when the whole
    command also succeeded. Otherwise it stays unknown.
    """
    check = CheckCommand(item["kind"], None, item.get("unattributed"), item.get("failure_unattributed"))
    status, reason = check_outcome(check, command_outcome), unattributed_reason(check, command_outcome)
    summary = item.get("summary_outcome")
    if status == "unknown" and reason in (UNATTRIBUTED_PIPED, UNATTRIBUTED_NOT_LAST):
        if summary == "failed" or (summary == "passed" and command_outcome == "passed"):
            return summary, None
    return status, reason
