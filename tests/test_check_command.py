"""Which captured commands are checks, and when the command's exit status is the check's.

The cases marked "seen" are command shapes that earlier Receipts recorded
as tests or lint checks although they ran neither.
"""
from __future__ import annotations

import pytest

from openshard.verification import check_command as cc


@pytest.mark.parametrize("command", [
    # seen: commit messages, search patterns and edit scripts that mention a tool
    'git add -A && git commit -q -m "Ran ruff and pytest before committing"',
    "grep -n pytest pyproject.toml",
    "grep -rn \"vitest\" apps/web/test",
    "sed -i 's|expect(ids).toContain(\">03029378<\");|x|' apps/web/test/interactions.test.tsx",
    "python3 - <<'PY'\np='test/interactions.test.tsx'\nimport subprocess; subprocess.run(['npx','vitest'])\nPY",
    "@'\nfrom pathlib import Path\nprint('pytest')\n'@ | python -",
    "uv venv --system-site-packages .venv; uv pip install --python .venv\\Scripts\\python.exe -e '.[dev]'",
    "cat tox.ini",
    "echo tox",
    "ls tests/",
    "python scripts/smoke_release.py",
    "python -c \"import pytest\"",
    "pip install pytest",
    "pnpm install",
    "black .",
    "ruff format .",
    "prettier --write src",
    "tsc --watch",
    "gh pr checks 162 --repo openshard/platform",
    "git log --oneline -5 -- tests/test_ruff.py",
    "bash scripts/run_pytest.sh",
    "bash -lc 'echo pytest'",
])
def test_commands_that_run_no_check_are_not_checks(command):
    assert cc.classify_check_commands(command) == []
    assert cc.classify_check_command(command).kind == cc.KIND_OTHER


@pytest.mark.parametrize("command,kind,segment", [
    ("pytest -q", cc.KIND_TEST, "pytest -q"),
    ("python -m pytest tests/test_x.py -q", cc.KIND_TEST, "python -m pytest tests/test_x.py -q"),
    ("/c/tmp/osvenv/Scripts/python.exe -m pytest -q tests/a.py", cc.KIND_TEST, "python -m pytest -q tests/a.py"),
    ('TEMP="C:/tmp/pyt" TMP="C:/tmp/pyt" /c/tmp/osvenv/Scripts/python.exe -m pytest tests/x.py',
     cc.KIND_TEST, "python -m pytest tests/x.py"),
    ("py -3.12 -m unittest discover", cc.KIND_TEST, "py -3.12 -m unittest discover"),
    ("uv run pytest", cc.KIND_TEST, "pytest"),
    ("npx vitest run apps/web/test", cc.KIND_TEST, "npx vitest run apps/web/test"),
    ("corepack pnpm --filter @openshard/web test", cc.KIND_TEST, "pnpm --filter @openshard/web test"),
    ("pnpm -r run typecheck", cc.KIND_TYPECHECK, "pnpm -r run typecheck"),
    ("pnpm typecheck", cc.KIND_TYPECHECK, "pnpm typecheck"),
    ("npm run lint", cc.KIND_LINT, "npm run lint"),
    ("npm test", cc.KIND_TEST, "npm test"),
    ("pnpm build", cc.KIND_BUILD, "pnpm build"),
    ("python -m ruff check openshard tests", cc.KIND_LINT, "python -m ruff check openshard tests"),
    ("ruff format --check .", cc.KIND_LINT, "ruff format --check ."),
    ("mypy openshard", cc.KIND_TYPECHECK, "mypy openshard"),
    ("npx tsc --noEmit", cc.KIND_TYPECHECK, "npx tsc --noEmit"),
    ("go test ./...", cc.KIND_TEST, "go test ./..."),
    ("cargo clippy -- -D warnings", cc.KIND_LINT, "cargo clippy -- -D warnings"),
    ("black --check .", cc.KIND_LINT, "black --check ."),
    ("& 'C:/Python312/python.exe' -m pytest -q", cc.KIND_TEST, "python -m pytest -q"),
    # Codex sends argv; it arrives joined: the inner script is what runs.
    ("bash -lc python -m pytest -q", cc.KIND_TEST, "python -m pytest -q"),
    ("bash -lc 'cd app && npm test'", cc.KIND_TEST, "npm test"),
    ("pwsh -NoProfile -Command \"npx vitest run\"", cc.KIND_TEST, "npx vitest run"),
])
def test_check_programs_are_recognised_with_a_readable_name(command, kind, segment):
    check = cc.classify_check_command(command)
    assert check.kind == kind
    assert check.segment == segment


@pytest.mark.parametrize("command,reason", [
    # seen: a passing tail made the tests look green
    ("cd C:/tmp/plat-receipt-ui && npx vitest run apps/web/test 2>&1 | tail -60", cc.UNATTRIBUTED_PIPED),
    ("python -m pytest -q | tee out.txt", cc.UNATTRIBUTED_PIPED),
    ("pytest -q; echo done", cc.UNATTRIBUTED_NOT_LAST),
    ("pytest -q\ngit status", cc.UNATTRIBUTED_NOT_LAST),
    ("pytest -q || true", cc.UNATTRIBUTED_ALTERNATIVE),
    ("pytest -q || echo failed", cc.UNATTRIBUTED_ALTERNATIVE),
    ("pytest -q &", cc.UNATTRIBUTED_BACKGROUND),
    ("bash -lc 'pytest -q | tail -3'", cc.UNATTRIBUTED_PIPED),
    ("bash -lc 'pytest -q' | tail -3", cc.UNATTRIBUTED_PIPED),
])
def test_an_exit_status_that_belongs_to_another_program_is_not_the_checks(command, reason):
    check = cc.classify_check_command(command)
    assert check.is_check
    assert check.unattributed == reason
    assert cc.check_outcome(check, "passed") == "unknown"
    assert cc.check_outcome(check, "failed") == "unknown"
    assert cc.unattributed_reason(check, "passed") == reason


@pytest.mark.parametrize("command", [
    "pytest -q",
    "cd /c/tmp/os && python -m pytest -q 2>&1",
    "source .venv/bin/activate && pytest",
    "export CI=1 && pnpm test",
    "Set-Location C:/tmp/os; python -m pytest -q",
])
def test_a_check_after_setup_steps_owns_both_outcomes(command):
    check = cc.classify_check_command(command)
    assert check.is_check and check.unattributed is None and check.failure_unattributed is None
    assert cc.check_outcome(check, "passed") == "passed"
    assert cc.check_outcome(check, "failed") == "failed"


def test_a_chain_of_checks_passes_together_but_a_failure_is_not_pinned_on_either():
    checks = cc.classify_check_commands("cd /c/tmp/p && pnpm typecheck && npx vitest run 2>&1")
    assert [c.kind for c in checks] == [cc.KIND_TYPECHECK, cc.KIND_TEST]
    for check in checks:
        assert cc.check_outcome(check, "passed") == "passed"
        assert cc.check_outcome(check, "failed") == "unknown"
        assert cc.unattributed_reason(check, "failed") == cc.UNATTRIBUTED_SHARED_FAILURE


def test_a_check_followed_by_a_commit_failure_is_not_a_failed_check():
    # seen shape: the commit (not the lint) is what fails most often here
    (check,) = cc.classify_check_commands('ruff check . && git add -A && git commit -m "x"')
    assert check.kind == cc.KIND_LINT
    assert cc.check_outcome(check, "passed") == "passed"
    assert cc.check_outcome(check, "failed") == "unknown"


def test_no_outcome_reported_stays_unknown():
    check = cc.classify_check_command("pytest")
    assert cc.check_outcome(check, "unknown") == "unknown"
    assert cc.unattributed_reason(check, "unknown") is None


def test_windows_paths_keep_their_backslashes():
    # Seen in acceptance: PowerShell's `C:\tmp\venv\...\python.exe -m ruff check` was not a check.
    (check,) = cc.classify_check_commands(
        r"Set-Location C:\tmp\acc-core; C:\tmp\osvenv\Scripts\python.exe -m ruff check openshard/x.py"
    )
    assert check.kind == cc.KIND_LINT and check.segment == "python -m ruff check openshard/x.py"
    assert cc.check_outcome(check, "failed") == "failed"
    (cmd,) = cc.classify_check_commands(r"cmd /c C:\venv\Scripts\pytest.exe -q")
    assert cmd.kind == cc.KIND_TEST and cmd.segment == "pytest -q"


def test_posix_escapes_still_apply():
    assert cc.classify_check_commands(r"echo a\ b\;c") == []
    assert cc.classify_check_command(r"pytest\ -q").kind == cc.KIND_OTHER  # one escaped word, not pytest


def test_check_names_are_scrubbed_word_by_word():
    from openshard.safety.sanitize import sanitize_words

    assert sanitize_words("npx vitest run packages/contracts/test/receipt-sync.test.ts", 100) == (
        "npx vitest run packages/contracts/test/receipt-sync.test.ts")
    assert sanitize_words("pytest --key AKIAIOSFODNN7EXAMPLEKEY12345 tests/x.py", 100) == "pytest --key <redacted> tests/x.py"
    assert sanitize_words("AKIAIOSFODNN7EXAMPLEKEY12345", 100) is None


# -- exit-status rules per shell ------------------------------------------------


@pytest.mark.parametrize("command", [
    "set -o pipefail; npx vitest run 2>&1 | tail -5",
    "set -euo pipefail\nnpx vitest run | tail -20",
])
def test_pipefail_makes_a_piped_success_the_checks(command):
    check = cc.classify_check_command(command)
    assert check.unattributed is None
    assert cc.check_outcome(check, "passed") == "passed"
    assert cc.check_outcome(check, "failed") == "unknown"  # tail may be the one that failed


@pytest.mark.parametrize("command", [
    "npx vitest run 2>&1 | tail -5; exit ${PIPESTATUS[0]}",
    "cd /c/tmp/p && pytest -q | tee out.txt; exit ${PIPESTATUS[0]}",
    "pytest -q > out.log 2>&1; exit $?",
])
def test_an_explicit_exit_of_the_checks_status_owns_both_outcomes(command):
    check = cc.classify_check_command(command)
    assert check.unattributed is None and check.failure_unattributed is None


@pytest.mark.parametrize("command,reason", [
    ("pytest -q | tail -5; exit ${PIPESTATUS[1]}", cc.UNATTRIBUTED_PIPED),  # tail's status
    ("pytest -q | tail -5; exit $?", cc.UNATTRIBUTED_PIPED),
    ("pytest -q; echo \"exit $?\"", cc.UNATTRIBUTED_NOT_LAST),  # an echoed status is not the exit status
    ("if pytest -q; then echo ok; fi", cc.UNATTRIBUTED_NOT_LAST),
    ("for f in a b; do pytest $f; done", cc.UNATTRIBUTED_NOT_LAST),
    ("if true; then\n  pytest -q\nfi", cc.UNATTRIBUTED_NOT_LAST),
])
def test_posix_statuses_that_are_not_the_checks(command, reason):
    check = cc.classify_check_command(command)
    assert check.is_check, "a check in a conditional is still a check that ran"
    assert check.unattributed == reason


@pytest.mark.parametrize("command", [
    "npx vitest run 2>&1 | Select-Object -Last 20",
    r"Set-Location C:\tmp\x; npx vitest run; Write-Output done",
    "npx vitest run *> out.log; exit $LASTEXITCODE",
    "$r = npx vitest run; $r | Select-Object -Last 5",
    r"& C:\tmp\venv\Scripts\python.exe -m pytest -q 2>&1 | Out-String",
    "npx vitest run | Tee-Object run.log",
])
def test_powershell_reports_the_last_native_programs_code(command):
    check = cc.classify_check_command(command, shell=cc.SHELL_POWERSHELL)
    assert check.is_check and check.unattributed is None and check.failure_unattributed is None
    # The same text under POSIX rules is never attributed that generously.
    posix = cc.classify_check_commands(command)
    assert all(c.unattributed or c.failure_unattributed for c in posix) or "|" not in command


@pytest.mark.parametrize("command,reason", [
    ("npx vitest run; git status", cc.UNATTRIBUTED_NOT_LAST),
    ("npx vitest run | findstr FAIL", cc.UNATTRIBUTED_PIPED),
    ("npx vitest run; exit 0", cc.UNATTRIBUTED_NOT_LAST),
    ("npx vitest run; $LASTEXITCODE = 0", cc.UNATTRIBUTED_NOT_LAST),
    ("npx vitest run || Write-Output failed", cc.UNATTRIBUTED_ALTERNATIVE),
    ("npx vitest run; Get-ChildItem | ForEach-Object { npx tsc }", cc.UNATTRIBUTED_NOT_LAST),
    ("npx vitest run &", cc.UNATTRIBUTED_BACKGROUND),
    ("npx vitest run; where.exe python", cc.UNATTRIBUTED_NOT_LAST),  # a program, not the where alias
])
def test_powershell_statuses_that_are_not_the_checks(command, reason):
    check = cc.classify_check_commands(command, shell=cc.SHELL_POWERSHELL)[0]
    assert check.unattributed == reason


def test_powershell_checks_in_blocks_are_recorded_but_never_attributed():
    for command in ("npx vitest run; if ($?) { npx tsc --noEmit }",
                    "npx vitest run\nif ($LASTEXITCODE -eq 0) {\n  npx tsc --noEmit\n}",
                    "try { npx tsc --noEmit } finally { Write-Output x }"):
        checks = cc.classify_check_commands(command, shell=cc.SHELL_POWERSHELL)
        assert cc.KIND_TYPECHECK in [c.kind for c in checks]
        assert all(c.unattributed for c in checks)


def test_powershell_and_chain_success_is_each_checks():
    checks = cc.classify_check_commands("pnpm typecheck && pnpm test", shell=cc.SHELL_POWERSHELL)
    assert [(c.unattributed, c.failure_unattributed) for c in checks] == [
        (None, cc.UNATTRIBUTED_SHARED_FAILURE), (None, cc.UNATTRIBUTED_SHARED_FAILURE)]


def test_the_shell_follows_the_captured_tool():
    assert cc.shell_for_tool("PowerShell") == cc.SHELL_POWERSHELL
    assert cc.shell_for_tool("Bash") == cc.SHELL_POSIX
    assert cc.shell_for_tool(None) == cc.SHELL_POSIX
