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
