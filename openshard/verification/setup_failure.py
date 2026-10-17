"""Recognise a verifier that could not run in this environment.

A verification command that exits non-zero is not necessarily a verdict on the
change: ``python -m pytest`` in an interpreter without pytest exits 1 with
``No module named pytest``, and a command that is not installed exits 127.
Neither says anything about the model's work, so neither should be answered
by spending more model calls.

Detection is deliberately narrow. It matches the interpreter's or shell's own
"could not start the tool" line, anchored to the start of a line, and never a
test framework's report: a failing test that itself raises
``ModuleNotFoundError`` prints that inside the framework's output
(``E   ModuleNotFoundError: ...``), which is a real failure of the change and is
not matched here. The output is only inspected, never stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KIND_MISSING_MODULE = "missing_module"
KIND_COMMAND_NOT_FOUND = "command_not_found"

# Shell convention: 127 = command not found, 126 = found but not executable.
_TOOLING_EXIT_CODES = frozenset({126, 127})

_MAX_SCAN_CHARS = 20_000

# `<path>/python3.12: No module named pytest`  /  `C:\...\python.exe: No module named pytest`
_MISSING_MODULE = re.compile(
    r"^\s*(?:.*[\\/])?python[\w.\-]*(?:\.exe)?: No module named ['\"]?[\w.\-]+['\"]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# `bash: pytest: command not found`, `sh: 1: foo: not found`
_SHELL_NOT_FOUND = re.compile(
    r"^\s*(?:[\w.\-]+: )?(?:ba|z|da)?sh(?:\.exe)?: (?:line \d+: |\d+: )?[^\n]*: (?:command )?not found\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Windows cmd.exe: `'pytest' is not recognized as an internal or external command`
_WINDOWS_NOT_RECOGNIZED = re.compile(
    r"is not recognized as an internal or external command", re.IGNORECASE
)
# OpenShard's own runner when the executable could not be started at all.
_RUNNER_NOT_FOUND = re.compile(r"^\s*\[[^\]\n]*\] not found\s*$", re.IGNORECASE | re.MULTILINE)


def detect_setup_failure(exit_code: int | None, output: str | None) -> str | None:
    """The kind of setup failure that explains a failed verification, or ``None``.

    ``None`` means the failure is not shown to be environmental and must be
    treated as an ordinary failed check. A zero exit code is never a failure.
    """
    if exit_code == 0:
        return None
    if exit_code in _TOOLING_EXIT_CODES:
        return KIND_COMMAND_NOT_FOUND
    text = (output or "")[:_MAX_SCAN_CHARS]
    if _MISSING_MODULE.search(text):
        return KIND_MISSING_MODULE
    if _SHELL_NOT_FOUND.search(text) or _WINDOWS_NOT_RECOGNIZED.search(text) or _RUNNER_NOT_FOUND.search(text):
        return KIND_COMMAND_NOT_FOUND
    return None


def setup_failure_reason(kind: str) -> str:
    """A short, path-free reason for the verification block."""
    if kind == KIND_MISSING_MODULE:
        return "verification could not run: a required Python module is missing in this environment"
    return "verification could not run: the check command was not found in this environment"


def setup_failure_metadata(
    kind: str, exit_code: int | None, *, model: str | None = None, attempt: int = 1
) -> dict:
    """The record fields for a run whose verifier could not run.

    ``verification`` is an unknown, incomplete block (attempted, no outcome
    observed: never a pass and never a failed check) and ``outcome_classification``
    attributes the outcome to the environment, so it is not model-quality
    evidence. Nothing here carries command output.
    """
    from openshard.history.outcome_classification import ObservedFacts, classify
    from openshard.history.verification import (
        MODE_OPENSHARD_EXECUTED,
        SOURCE_DIRECTLY_OBSERVED,
        build_verification,
    )

    code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
    block = build_verification(
        source=SOURCE_DIRECTLY_OBSERVED,
        observation_mode=MODE_OPENSHARD_EXECUTED,
        status="unknown",
        exit_code=code,
        reason=setup_failure_reason(kind),
        incomplete_reasons=["verifier_setup_failed"],
    )
    facts = ObservedFacts(
        verification_status="failed",
        verification_source="directly_observed",
        check_exit_codes=(code,) if code is not None else (),
        verifier_preflight="failed",
    )
    return {
        "verification": block,
        "outcome_classification": classify(facts, attempt=attempt, model=model).to_dict(),
    }


# ---------------------------------------------------------------------------
# OpenShard's own re-run of a check on an exact commit (post_session.run_checks)
# ---------------------------------------------------------------------------
#
# A re-run often happens in an isolated copy of the commit whose JavaScript
# dependencies were installed offline with install scripts disabled. A suite
# that cannot start there (Electron's binary is never downloaded, a native
# addon is never built, a package is not in the offline store) exits non-zero
# without saying anything about the change. Those runs are ``unknown`` with a
# reason, never ``failed``.
#
# Two tiers, both conservative:
#
# * an *incomplete install* (Electron or a native addon missing its install
#   step) is the environment whatever else the output says;
# * every other "could not start" signature counts only when the output shows
#   no test results at all. Once a runner has reported results, a non-zero
#   exit stays ``failed``: a test that raises ``Cannot find module`` is a real
#   failure of the change.

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

_INCOMPLETE_INSTALL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Electron failed to install correctly", re.IGNORECASE),
     "Electron's binary is not installed (its install script did not run)"),
    (re.compile(r"Could not locate the bindings file", re.IGNORECASE),
     "a native module is not built (its install script did not run)"),
    (re.compile(r"was compiled against a different Node\.js version", re.IGNORECASE),
     "a native module was built for a different Node.js or Electron version"),
    (re.compile(r"Cannot find module '[^'\n]*\.node'"),
     "a native module is not built (its install script did not run)"),
)

# Evidence that a test runner started and reported results.
_TEST_RESULTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^=+ .*test session starts", re.MULTILINE),  # pytest
    re.compile(r"^=+ .*\b\d+ (?:passed|failed|errors?|skipped|deselected)\b", re.MULTILINE),  # pytest
    re.compile(r"^\s*Test Suites:\s+.*\d+ total", re.MULTILINE),  # jest
    re.compile(r"^\s*Tests:\s+.*\d+ total", re.MULTILINE),  # jest
    re.compile(r"^\s*Test Files\s+.*\d+ (?:passed|failed|skipped)", re.MULTILINE),  # vitest
    re.compile(r"^\s*Tests\s+.*\d+ (?:passed|failed|skipped)", re.MULTILINE),  # vitest
    re.compile(r"^\s*\d+ (?:passing|failing|pending)\b", re.MULTILINE),  # mocha
    re.compile(r"^#\s*(?:tests|pass|fail)\s+\d+", re.MULTILINE),  # node --test / TAP
    re.compile(r"^(?:not )?ok \d+\b", re.MULTILINE),  # TAP
    re.compile(r"^\s*\d+ (?:passed|failed|flaky)\b", re.MULTILINE),  # playwright
    re.compile(r"^test result: ", re.MULTILINE),  # cargo
    re.compile(r"^(?:--- FAIL|FAIL\s+\S+|ok\s+\S+\s+[\d.]+s)", re.MULTILINE),  # go test
    re.compile(r"^Ran \d+ tests? in ", re.MULTILINE),  # unittest
)

_BARE_PACKAGE = r"(?P<pkg>(?:@[\w.\-]+/)?[\w][\w.\-]*)(?:/[^'\"\s]*)?"
_NODE_MISSING_PACKAGE = re.compile(
    r"^\s*(?:Uncaught )?(?:\w*Error)?(?: \[ERR_MODULE_NOT_FOUND\])?:? ?Cannot find (?:module|package) "
    r"['\"]" + _BARE_PACKAGE + r"['\"]",
    re.MULTILINE,
)
_NPM_MISSING_SCRIPT = re.compile(r"^npm (?:ERR!|error) Missing script", re.MULTILINE | re.IGNORECASE)
_NPM_ENOENT = re.compile(r"^npm (?:ERR!|error) (?:code ENOENT|enoent )", re.MULTILINE | re.IGNORECASE)
_SPAWN_ENOENT = re.compile(r"\bspawn \S+ ENOENT\b")

_SCAN_HEAD = 16_000
_SCAN_TAIL = 48_000


def _scan_text(output: str | None) -> str:
    text = output or ""
    if len(text) > _SCAN_HEAD + _SCAN_TAIL:
        text = text[:_SCAN_HEAD] + "\n" + text[-_SCAN_TAIL:]
    return _ANSI.sub("", text)


def reports_test_results(output: str | None) -> bool:
    """Whether *output* shows a test runner that started and reported results."""
    from openshard.verification.failed_tests import failing_test_ids

    text = _scan_text(output)
    return any(p.search(text) for p in _TEST_RESULTS) or bool(failing_test_ids(text))


def rerun_setup_failure(exit_code: int | None, output: str | None) -> str | None:
    """Why a re-run check that exited non-zero could not run here, or ``None`` when it is a real failure.

    The reason is short and path-free (at most a package name). A zero or
    missing exit code is never a setup failure: a timeout or a check that
    could not start is already ``unknown`` before this is asked.
    """
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0:
        return None
    text = _scan_text(output)
    for pattern, reason in _INCOMPLETE_INSTALL:
        if pattern.search(text):
            return reason
    if reports_test_results(text):
        return None
    kind = detect_setup_failure(exit_code, text)
    if kind == KIND_MISSING_MODULE:
        return "a required Python module is not installed"
    if kind == KIND_COMMAND_NOT_FOUND:
        return "a command the check needs was not found"
    if _NPM_MISSING_SCRIPT.search(text):
        return "the package script the check runs does not exist at this commit"
    if _NPM_ENOENT.search(text):
        return "npm could not find a file it needs (ENOENT)"
    missing = _NODE_MISSING_PACKAGE.search(text)
    if missing:
        return f"the dependency {missing.group('pkg')[:60]} is not installed"
    if _SPAWN_ENOENT.search(text):
        return "a program the tests start was not found (ENOENT)"
    if exit_code < 0:
        return f"the test runner was stopped by signal {-exit_code} before it reported any results"
    return None


def rerun_setup_reason(reason: str) -> str:
    """The receipt/CLI sentence for a re-run that could not set the project up."""
    return f"OpenShard couldn't set up the project to re-run the check: {reason}"


# ---------------------------------------------------------------------------
# Test results a re-run reported before its setup failed
# ---------------------------------------------------------------------------
#
# ``vitest run && electron verify.cjs`` can report every test passing and then
# stop because Electron's binary was never installed. The check stays
# ``unknown`` (part of it never ran), but the runner's own summary is real
# evidence and is kept beside it as counts. Only the existing output is read;
# the command is never split or run differently. Counts are kept only when
# exactly one summary is found, it reports at least one pass and nothing
# failed: a failed test, suite, file, error or cancellation means no partial
# result, and several summaries are never added up.


@dataclass(frozen=True)
class ReportedTests:
    """What one test runner's summary said: counts only, never output."""

    runner: str
    passed: int
    failed: int = 0
    skipped: int = 0


REPORTED_RUNNERS: frozenset[str] = frozenset({"vitest", "jest", "pytest", "mocha", "node --test"})

_COUNT = re.compile(r"(\d+) (passed|failed|skipped|todo|errors?)\b")
_VITEST_TESTS = re.compile(
    r"^\s*Tests\s{2,}(?P<body>[^\n(]*\d+ (?:passed|failed|skipped)[^\n(]*)\(\d+\)\s*$", re.MULTILINE
)
_VITEST_BAD = re.compile(r"^\s*(?:Test Files\s{2,}[^\n]*\d+ failed|Errors\s{2,}\d+ errors?)", re.MULTILINE)
_JEST_TESTS = re.compile(r"^\s*Tests:\s+(?P<body>[^\n]*\d+ total)\s*$", re.MULTILINE)
_JEST_BAD = re.compile(r"^\s*Test Suites:\s+[^\n]*\d+ failed", re.MULTILINE)
_PYTEST_FINAL = re.compile(
    r"^(?:=+ )?(?P<body>\d+ (?:passed|failed|errors?|skipped)\b[^=\n]*?) in [\d.]+s(?: \([^)\n]*\))?(?: =+)?\s*$",
    re.MULTILINE,
)
_MOCHA = re.compile(r"^\s*(?P<n>\d+) (?P<word>passing|failing|pending)\b", re.MULTILINE)
_NODE_TEST = re.compile(r"^(?:#|ℹ)\s*(?P<word>pass|fail|cancelled|skipped)\s+(?P<n>\d+)\s*$", re.MULTILINE)


def _counts(body: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for n, word in _COUNT.findall(body):
        key = "error" if word.startswith("error") else word
        out[key] = out.get(key, 0) + int(n)
    return out


def _summaries(text: str) -> list[ReportedTests | None]:
    """Every runner summary in *text*; ``None`` stands for a suite- or file-level failure."""
    found: list[ReportedTests | None] = []
    for runner, pattern in (("vitest", _VITEST_TESTS), ("jest", _JEST_TESTS)):
        for m in pattern.finditer(text):
            c = _counts(m.group("body"))
            found.append(ReportedTests(runner, c.get("passed", 0), c.get("failed", 0),
                                       c.get("skipped", 0) + c.get("todo", 0)))
    for m in _PYTEST_FINAL.finditer(text):
        c = _counts(m.group("body"))
        found.append(ReportedTests("pytest", c.get("passed", 0), c.get("failed", 0) + c.get("error", 0),
                                   c.get("skipped", 0)))
    mocha = {m.group("word"): int(m.group("n")) for m in _MOCHA.finditer(text)}
    if "passing" in mocha or "failing" in mocha:
        found.append(ReportedTests("mocha", mocha.get("passing", 0), mocha.get("failing", 0),
                                   mocha.get("pending", 0)))
    node = {m.group("word"): int(m.group("n")) for m in _NODE_TEST.finditer(text)}
    if "pass" in node or "fail" in node:
        found.append(ReportedTests("node --test", node.get("pass", 0),
                                   node.get("fail", 0) + node.get("cancelled", 0), node.get("skipped", 0)))
    if _VITEST_BAD.search(text) or _JEST_BAD.search(text):
        found.append(None)
    return found


def reported_test_results(output: str | None) -> ReportedTests | None:
    """The one all-passing runner summary in *output*, or ``None`` (none, several, or any failure)."""
    found = _summaries(_scan_text(output))
    if len(found) != 1 or found[0] is None:
        return None
    result = found[0]
    return result if result.failed == 0 and result.passed > 0 else None


def reported_tests_text(reported: ReportedTests) -> str:
    """``69 / 69 passed (vitest)``; skipped tests are named, never counted as passes."""
    skipped = f", {reported.skipped} skipped" if reported.skipped else ""
    return f"{reported.passed} / {reported.passed + reported.failed} passed{skipped} ({reported.runner})"


def rerun_partial_reason(reported: ReportedTests, reason: str) -> str:
    """The receipt/CLI sentence for a re-run whose tests passed before its setup failed."""
    return f"OpenShard re-ran the tests: {reported_tests_text(reported)}; then {reason}, so the full check couldn't run"


def blocked_step(reason: str | None) -> str:
    """The part of a check a setup failure stopped, for the one-line ``Re-verified:`` summary."""
    text = reason or ""
    if "Electron" in text:
        return "Electron step"
    if "native module" in text:
        return "native module step"
    return "rest of the check"
