"""Post-session verification (verification v2): OpenShard re-runs approved checks itself.

An external agent's hooks can at best *report* a check's outcome
(``agent_reported``); most report none (``unknown`` / ``outcome_not_observed``).
``openshard verify`` closes that gap without trusting the agent:

    external agent completes
    -> OpenShard picks the checks to run: the repository's configured
       verification contract, else its detected test command, plus (opt-in)
       the check commands the agent was observed running
    -> each is classified by the same safety rules native runs use
       (verification/plan.py); ``blocked`` never runs, ``needs_approval``
       runs only with explicit approval, ``safe`` runs
    -> OpenShard executes them (argv, never a shell) and reads each exit code
    -> the outcome is recorded as ``directly_observed`` /
       ``openshard_executed``, bound to the commit git reports -- but only
       when the working tree was a clean commit before *and* after the run;
       otherwise it is recorded unbound (``artifact_not_bound``)

Where the result goes
---------------------
Receipts in ``runs.jsonl`` are content-hashed (history/shard_hash.py) and a
hook session's line is re-written on every fold, so a re-run is never
written into a receipt. Each run appends one *attestation* to
``.openshard/verifications.jsonl`` naming the receipt it verifies
(``receipt_id``, else ``run_id``). Readers join the latest attestation onto
the receipt at display time (``post_session_verification``); the receipt's
own ``verification`` block -- what was observed during the session -- is
never replaced, so both stay visible side by side.

Never manufactured
------------------
Exit code 0 -> ``passed``; non-zero -> ``failed``. A check that timed out or
could not start has no exit code: it is ``unknown`` with
``check_not_completed``, never ``failed`` or ``passed``. A non-zero exit whose
output shows the project could not be set up to run the check (a dependency
or binary missing, the command not found; ``setup_failure.rerun_setup_failure``)
is ``unknown`` too, with ``verifier_setup_failed`` and the reason. A skipped check
(blocked, or needing approval that was not given) is ``skipped``. Nothing run
at all -> ``not_run``. Command output is streamed to the terminal (or
discarded under ``--json``) and never stored; the attestation carries only a
path-free check label, the exit code and the duration.

This is evidence, not policy: nothing here gates, blocks or fails anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.verification import (
    CHECK_FAILED,
    CHECK_PASSED,
    CHECK_SKIPPED,
    CHECK_UNKNOWN,
    MODE_CI_REPORT,
    MODE_OPENSHARD_EXECUTED,
    REASON_ARTIFACT_NOT_BOUND,
    REASON_CHECK_NOT_COMPLETED,
    REASON_CHECKS_TRUNCATED,
    SOURCE_DIRECTLY_OBSERVED,
    SOURCE_INDEPENDENTLY_VERIFIED,
    STATUS_NOT_RUN,
    STATUS_PARTIAL,
    STATUS_PASSED,
    VerificationCheck,
    aggregate_status,
    build_verification,
    parse_verification_block,
)
from openshard.verification.plan import (
    CommandSafety,
    VerificationCommand,
    VerificationKind,
    VerificationSource,
    classify_command_safety,
    parse_command_to_argv,
    safe_check_label,
)

ATTESTATIONS_FILENAME = "verifications.jsonl"
ATTESTATION_VERSION = 1
# What an attestation line records. A re-run is OpenShard executing checks
# itself; a CI attestation is an independent CI system's verdict on one exact
# commit (verification/ci_evidence.py). Both are appended, never rewritten.
KIND_POST_SESSION = "post_session_verification"
KIND_CI = "ci_verification"
ATTESTATION_KINDS: frozenset[str] = frozenset({KIND_POST_SESSION, KIND_CI})
MAX_EVIDENCE = 20  # newest attestations joined onto one receipt
MAX_PLANNED_CHECKS = 8
DEFAULT_TIMEOUT_SECONDS = 600.0
# The project could not be set up to run a check (setup_failure.py uses the same token).
REASON_VERIFIER_SETUP_FAILED = "verifier_setup_failed"

ORIGIN_CONTRACT = "contract"  # the repository's configured verification contract
ORIGIN_DETECTED = "detected"  # the test command OpenShard detects for the repository
ORIGIN_OBSERVED = "observed"  # a check command the agent was observed running (opt-in)
ORIGIN_CHANGE = "change"  # selected from what the verified commit changed (verification/change_checks.py)

# Read-only checkers a contract may name without extra approval. The shared
# plan.py allowlist is left untouched (it also governs native runs); these
# are only honoured here, and never with a flag that rewrites files.
_READ_ONLY_CHECKERS: tuple[tuple[str, ...], ...] = (
    ("ruff", "check"),
    ("python", "-m", "ruff", "check"),
    ("ruff", "format", "--check"),
    ("mypy",),
    ("python", "-m", "mypy"),
    ("flake8",),
    ("pylint",),
    ("black", "--check"),
    ("tsc", "--noemit"),
    ("go", "vet"),
)
_WRITING_FLAGS = frozenset({"--fix", "--unsafe-fixes", "--write", "-w", "--fix-only"})

# ``<Tool>: <command>`` -- the shape hook capture stores for a shell command
# (adapters/claude_hooks.summarize_command). Longer names were capped there.
_OBSERVED_NAME_RE = re.compile(r"^[A-Za-z_][\w.-]{0,40}: (?P<cmd>.+)$")
_OBSERVED_COMMAND_CAP = 100  # claude_hooks._COMMAND_CAP: a command this long may be truncated
_TEST_RE = re.compile(
    r"(?:^|\s)(?:pytest|py\.test|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|go\s+test|cargo\s+test|jest|vitest|"
    r"mocha|unittest|rspec|dotnet\s+test|mvn\s+test|tox|nox)(?:\s|$)",
    re.IGNORECASE,
)
_TYPECHECK_RE = re.compile(r"(?:^|\s)(?:mypy|tsc|pyright)(?:\s|$)", re.IGNORECASE)
_LINT_RE = re.compile(r"(?:^|\s)(?:ruff|flake8|pylint|eslint|black|isort|gofmt|golangci-lint|clippy|vet)(?:\s|$)",
                      re.IGNORECASE)


@dataclass
class PlannedCheck:
    name: str  # path-free display label
    argv: list[str]
    kind: str  # history/verification.py CHECK_KINDS
    origin: str
    safety: str  # safe | needs_approval | blocked
    reason: str
    cwd: str = ""  # repo-relative directory the check runs in ("" = root)
    why: str = ""  # why change-aware selection chose it (ORIGIN_CHANGE only)


@dataclass
class CheckRun:
    check: PlannedCheck
    status: str  # passed | failed | skipped | unknown
    exit_code: int | None = None
    duration_seconds: float | None = None
    note: str = ""
    setup_failed: bool = False  # the project could not be set up to run it; ``note`` says why


@dataclass
class TreeState:
    head: str | None
    dirty: bool | None  # tracked or untracked changes; None: git could not tell
    tracked_dirty: bool | None = None  # tracked changes only


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _kind_for(argv: list[str]) -> str:
    text = " ".join(argv)
    if _TEST_RE.search(text):
        return "test"
    if _TYPECHECK_RE.search(text):
        return "typecheck"
    if _LINT_RE.search(text):
        return "lint"
    return "other"


def _classify(argv: list[str], source: VerificationSource) -> tuple[str, str]:
    safety, reason = classify_command_safety(argv, source)
    if safety == CommandSafety.needs_approval:
        lowered = [t.lower() for t in argv]
        writes = any(t in _WRITING_FLAGS or t.startswith("--fix=") for t in lowered)
        for prefix in _READ_ONLY_CHECKERS:
            if not writes and len(lowered) >= len(prefix) and tuple(lowered[: len(prefix)]) == prefix:
                return CommandSafety.safe.value, f"read-only checker: {' '.join(prefix)}"
    return safety.value, reason


def _planned(argv: list[str], origin: str, source: VerificationSource) -> PlannedCheck:
    safety, reason = _classify(argv, source)
    cmd = VerificationCommand(
        name="", argv=argv, kind=VerificationKind.unknown, source=source,
        safety=CommandSafety(safety), reason=reason,
    )
    return PlannedCheck(
        name=safe_check_label(cmd) or "check", argv=argv, kind=_kind_for(argv),
        origin=origin, safety=safety, reason=reason,
    )


def _contract_argvs(config: dict) -> list[list[str]]:
    """``verification_commands`` (a list of commands) or the older single ``verification_command``."""
    raw: Any = config.get("verification_commands")
    if raw is None:
        single = config.get("verification_command")
        raw = [single] if single else []
    if isinstance(raw, str):
        raw = [raw]
    out: list[list[str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(parse_command_to_argv(item.strip()))
        elif isinstance(item, list) and item and all(isinstance(t, str) for t in item):
            out.append(list(item))
    return out


def observed_check_commands(entry: dict) -> list[str]:
    """Check command texts a receipt says the agent ran, when they are safe to re-read.

    Only names in the ``<Tool>: <command>`` shape qualify; a redacted name, or
    one long enough that capture may have truncated it, is never re-run
    (running a truncated command would verify something the agent never ran).
    """
    from openshard.history.verification import derive_verification

    out: list[str] = []
    for check in derive_verification(entry).checks:
        match = _OBSERVED_NAME_RE.match(check.name)
        if not match or check.kind not in ("test", "lint"):
            continue
        command = match.group("cmd").strip()
        if not command or len(command) >= _OBSERVED_COMMAND_CAP or "redact" in command.lower():
            continue
        if command not in out:
            out.append(command)
    return out


def plan_checks(
    repo_root: Path, config: dict, entry: dict | None, *, include_observed: bool = False,
) -> list[PlannedCheck]:
    """The checks ``openshard verify`` would run, in order, de-duplicated by argv."""
    planned: list[PlannedCheck] = []
    seen: set[tuple[str, ...]] = set()

    def add(argv: list[str], origin: str, source: VerificationSource) -> None:
        key = tuple(argv)
        if argv and key not in seen and len(planned) < MAX_PLANNED_CHECKS:
            seen.add(key)
            planned.append(_planned(argv, origin, source))

    for argv in _contract_argvs(config):
        add(argv, ORIGIN_CONTRACT, VerificationSource.config)
    if not planned:
        try:
            from openshard.analysis.repo import analyze_repo

            detected = analyze_repo(repo_root).test_command
        except Exception:
            detected = None
        if detected:
            add(parse_command_to_argv(detected), ORIGIN_DETECTED, VerificationSource.detected)
    if include_observed and entry is not None:
        for command in observed_check_commands(entry):
            add(parse_command_to_argv(command), ORIGIN_OBSERVED, VerificationSource.user)
    return planned


def plan_commit_checks(
    repo_root: Path, config: dict, entry: dict | None, commit: str, base: str | None,
) -> list[PlannedCheck]:
    """The checks for verifying one exact *commit*.

    The repository's configured contract still wins: when it names commands,
    those are the required checks. Otherwise the checks come from what the
    change touched (``change_checks.select_checks``); when the change calls
    for none (a docs-only change, an unknown stack), the repository's detected
    test command. A change-selected check is OpenShard's own command built
    from a fixed script name, so it is ``safe`` unless the shared rules block
    it or the change rewrote that check's own definition (``needs_approval``).
    """
    return plan_commit(repo_root, config, entry, commit, base)[0]


_KEEP_FIRST = {"test": 0, "typecheck": 1, "build": 2, "lint": 3}


def plan_commit(
    repo_root: Path, config: dict, entry: dict | None, commit: str, base: str | None,
) -> tuple[list[PlannedCheck], bool]:
    """``plan_commit_checks`` plus whether selected checks were dropped to stay within ``MAX_PLANNED_CHECKS``.

    When the change calls for more checks than the cap, tests are kept first,
    then typecheck, build and lint, so a cap can never leave a change
    "verified" by lint alone; the caller records the run as incomplete.
    """
    if _contract_argvs(config):
        return plan_checks(repo_root, config, entry), len(_contract_argvs(config)) > MAX_PLANNED_CHECKS
    from openshard.verification.change_checks import select_checks

    try:
        selected = select_checks(repo_root, commit, base)
    except Exception:
        selected = []
    truncated = len(selected) > MAX_PLANNED_CHECKS
    if truncated:
        kept = sorted(range(len(selected)), key=lambda i: (_KEEP_FIRST.get(selected[i].kind, 4), i))[:MAX_PLANNED_CHECKS]
        selected = [selected[i] for i in sorted(kept)]
    planned: list[PlannedCheck] = []
    for check in selected:
        argv = list(check.argv)
        shared, shared_reason = classify_command_safety(argv, VerificationSource.detected)
        if shared == CommandSafety.blocked:
            safety, reason = CommandSafety.blocked.value, shared_reason
        elif check.safety_override:
            safety, reason = check.safety_override, check.override_reason
        else:
            safety, reason = CommandSafety.safe.value, "selected by OpenShard for this change"
        planned.append(PlannedCheck(
            name=check.label, argv=argv, kind=check.kind, origin=ORIGIN_CHANGE, safety=safety,
            reason=reason, cwd=check.cwd, why=check.why,
        ))
    return (planned, truncated) if planned else (plan_checks(repo_root, {}, entry), False)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


Runner = Callable[..., Any]


def tree_state(repo_root: Path) -> TreeState:
    from openshard.util.git import run_git

    head = run_git(repo_root, ["rev-parse", "HEAD"])
    status = run_git(repo_root, ["status", "--porcelain", "--untracked-files=normal"])
    head_sha = head.strip().lower() if head and re.fullmatch(r"[0-9a-fA-F]{40,64}", head.strip()) else None
    dirty: bool | None = None
    tracked_dirty: bool | None = None
    if status is not None:
        # OpenShard's own state never makes the tree "dirty" for binding purposes.
        lines = [ln for ln in status.splitlines() if ln.strip() and ".openshard/" not in ln.replace("\\", "/")]
        dirty = bool(lines)
        tracked_dirty = any(not ln.startswith("??") for ln in lines)
    return TreeState(head=head_sha, dirty=dirty, tracked_dirty=tracked_dirty)


def check_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a check runs in: OpenShard's own, plus *extra*, minus the Windows venv launcher hint.

    A venv's ``python.exe`` on Windows sets ``__PYVENV_LAUNCHER__``; a child
    interpreter that inherits it adopts *that* venv's site-packages. A
    project's own ``.venv`` Python would then test against OpenShard's
    packages instead of the project's -- so the hint never crosses over.
    """
    import os

    env = {k: v for k, v in os.environ.items() if k != "__PYVENV_LAUNCHER__"}
    env.update(extra or {})
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop *proc* and everything it started (a check is often ``pnpm.cmd`` -> node -> vitest workers)."""
    import os
    import signal
    import sys

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S603,S607 - fixed argv
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def run_process_tree(argv: list[str], *, cwd: Path, timeout: float, check: bool = False, **kwargs: Any) -> Any:
    """``subprocess.run`` for checks, except that a timeout stops the whole process tree, not just its head.

    Output is never captured here (it is streamed or discarded), so waiting
    cannot deadlock on a full pipe. Raises ``TimeoutExpired`` like ``run``.
    """
    import sys

    if sys.platform == "win32":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, cwd=cwd, **kwargs)  # noqa: S603 - argv list, no shell
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise
    return proc


OUTPUT_HEAD_BYTES = 16_000
OUTPUT_TAIL_BYTES = 48_000


def run_check_process(argv: list[str], *, cwd: Path, timeout: float, echo: bool,
                      env: dict[str, str] | None = None) -> tuple[Any, str]:
    """``run_process_tree`` with output read through a pipe: ``(process, bounded output)``.

    Output is echoed to the terminal as it arrives when *echo*, and the first
    ``OUTPUT_HEAD_BYTES`` and last ``OUTPUT_TAIL_BYTES`` are kept only so the
    caller can tell "could not run" from "failed"; it is never stored.
    Raises ``TimeoutExpired`` like ``run``.
    """
    import os
    import sys
    import threading

    kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "env": env}
    if not echo:
        kwargs["stdin"] = subprocess.DEVNULL
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    else:
        kwargs["start_new_session"] = True
    head = bytearray()
    tail = bytearray()
    proc = subprocess.Popen(argv, cwd=cwd, **kwargs)  # noqa: S603 - argv list, no shell

    def pump() -> None:
        out = getattr(sys.stdout, "buffer", None)
        if proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                if echo:
                    try:
                        if out is not None:
                            out.write(chunk)
                        else:
                            sys.stdout.write(chunk.decode("utf-8", "replace"))
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        pass
                room = OUTPUT_HEAD_BYTES - len(head)
                if room > 0:
                    head.extend(chunk[:room])
                    chunk = chunk[room:]
                tail.extend(chunk)
                if len(tail) > OUTPUT_TAIL_BYTES:
                    del tail[: len(tail) - OUTPUT_TAIL_BYTES]
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        reader.join(timeout=5)
        raise
    # A process the check left running may hold the pipe open; never wait on it for long.
    reader.join(timeout=10)
    if not reader.is_alive() and proc.stdout is not None:
        proc.stdout.close()
    return proc, (bytes(head) + bytes(tail)).decode("utf-8", "replace")


def run_checks(
    planned: list[PlannedCheck],
    repo_root: Path,
    *,
    approve: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stream: bool = True,
    runner: Runner | None = None,
    on_start: Callable[[PlannedCheck], None] | None = None,
    env: dict[str, str] | None = None,
) -> list[CheckRun]:
    """Run each planned check that its safety class allows. Never raises.

    *env* adds variables to the inherited environment (an isolated copy's
    ``PYTHONPATH``). A check runs in ``repo_root / check.cwd``. A custom
    *runner* may return an object with a ``stdout`` string, which is read the
    same way as a real check's output.
    """
    from openshard.verification.commit_checkout import resolve_tool
    from openshard.verification.setup_failure import rerun_setup_failure, rerun_setup_reason
    results: list[CheckRun] = []
    for check in planned:
        if check.safety == CommandSafety.blocked.value:
            results.append(CheckRun(check, CHECK_SKIPPED, note=f"blocked: {check.reason}"))
            continue
        if check.safety == CommandSafety.needs_approval.value and not approve:
            results.append(CheckRun(check, CHECK_SKIPPED, note="needs approval (re-run with --approve)"))
            continue
        argv = resolve_tool(check.argv) if runner is None else list(check.argv)
        if argv is None:
            results.append(CheckRun(check, CHECK_UNKNOWN, note="executable not found; not run"))
            continue
        cwd = repo_root / check.cwd if check.cwd else repo_root
        if not cwd.is_dir():
            results.append(CheckRun(check, CHECK_UNKNOWN, note="its directory does not exist at this commit"))
            continue
        if on_start is not None:
            on_start(check)
        io: dict[str, Any] = {} if stream else {
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        }
        io["env"] = check_environment(env)
        started = time.monotonic()
        output: str | None = None
        try:
            if runner is None:
                proc, output = run_check_process(argv, cwd=cwd, timeout=timeout, echo=stream, env=io["env"])
            else:
                proc = runner(argv, cwd=cwd, timeout=timeout, check=False, **io)
                raw_out = getattr(proc, "stdout", None)
                output = raw_out if isinstance(raw_out, str) else None
        except subprocess.TimeoutExpired:
            results.append(CheckRun(
                check, CHECK_UNKNOWN, duration_seconds=round(time.monotonic() - started, 2),
                note=f"timed out after {timeout:g}s; no exit code",
            ))
            continue
        except OSError as exc:
            results.append(CheckRun(check, CHECK_UNKNOWN, note=f"could not start ({type(exc).__name__})"))
            continue
        code = getattr(proc, "returncode", None)
        duration = round(time.monotonic() - started, 2)
        if not isinstance(code, int) or isinstance(code, bool):
            results.append(CheckRun(check, CHECK_UNKNOWN, duration_seconds=duration, note="no exit code"))
            continue
        setup = rerun_setup_failure(code, output) if code != 0 else None
        if setup is not None:
            # The project could not be set up to run it: no verdict on the change.
            results.append(CheckRun(check, CHECK_UNKNOWN, exit_code=code, duration_seconds=duration,
                                    note=rerun_setup_reason(setup), setup_failed=True))
            continue
        results.append(CheckRun(
            check, CHECK_PASSED if code == 0 else CHECK_FAILED, exit_code=code, duration_seconds=duration,
        ))
    return results


# ---------------------------------------------------------------------------
# Attestation
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_attestation(
    entry: dict | None,
    results: list[CheckRun],
    *,
    before: TreeState,
    after: TreeState,
    started_at: str,
    completed_at: str,
    commit: str | None = None,
    checkout: dict | None = None,
    truncated: bool = False,
) -> dict:
    """One ``verifications.jsonl`` line: what OpenShard ran, saw, and against which tree.

    With *commit* (``openshard verify --commit``) the result binds only when
    the tree that ran was exactly that commit; *checkout* says where it ran.
    """
    checks = [
        VerificationCheck(name=r.check.name, status=r.status, kind=r.check.kind, exit_code=r.exit_code)
        for r in results
    ]
    incomplete: list[str] = []
    # Bound only when what ran was exactly a commit: no change of any kind
    # before the run (an untracked file could be under test), and afterwards
    # the same HEAD with no *tracked* change. Untracked files a check itself
    # writes (caches, reports) do not change what was tested.
    after_tracked = after.tracked_dirty if after.tracked_dirty is not None else after.dirty
    bound = (
        before.head is not None
        and before.dirty is False
        and after_tracked is False
        and after.head == before.head
        and (commit is None or before.head.startswith(commit.lower()))
    )
    if not bound:
        incomplete.append(REASON_ARTIFACT_NOT_BOUND)
    if any(r.status == CHECK_UNKNOWN for r in results):
        incomplete.append(REASON_CHECK_NOT_COMPLETED)
    if any(r.setup_failed for r in results):
        incomplete.append(REASON_VERIFIER_SETUP_FAILED)
    status = _commit_verdict(aggregate_status(checks) if checks else STATUS_NOT_RUN, results,
                             commit=commit, truncated=truncated, incomplete=incomplete)
    durations = [r.duration_seconds for r in results if r.duration_seconds is not None]
    ran = [r for r in results if r.status != CHECK_SKIPPED]
    reason = _reason(results, bound, before)
    block = build_verification(
        source=SOURCE_DIRECTLY_OBSERVED,
        observation_mode=MODE_OPENSHARD_EXECUTED,
        checks=[c.to_dict() for c in checks],
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(sum(durations), 2) if durations else None,
        exit_code=ran[0].exit_code if len(ran) == 1 else None,
        artifact_sha=before.head if bound else None,
        reason=reason,
        incomplete_reasons=incomplete,
    )
    entry = entry or {}
    return {
        "version": ATTESTATION_VERSION,
        "attestation_id": f"vat_{uuid.uuid4().hex}",
        "kind": KIND_POST_SESSION,
        "created_at": completed_at,
        "receipt_id": entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None,
        "run_id": entry.get("run_id") if isinstance(entry.get("run_id"), str) else None,
        "shard_id": entry.get("shard_id") if isinstance(entry.get("shard_id"), str) else None,
        "executor": entry.get("executor") if isinstance(entry.get("executor"), str) else None,
        "tree": {
            "head": before.head, "dirty": before.dirty,
            "head_after": after.head, "dirty_after": after.dirty, "tracked_dirty_after": after.tracked_dirty,
        },
        "checks": [
            {"name": r.check.name, "origin": r.check.origin, "safety": r.check.safety, "note": r.note[:120],
             **({"why": r.check.why[:200]} if r.check.why else {}),
             **({"cwd": r.check.cwd} if r.check.cwd else {})}
            for r in results
        ],
        **({"commit": commit.lower()} if commit else {}),
        **({"checkout": checkout} if checkout else {}),
        "raw_output_stored": False,
        "verification": block,
    }


def _commit_verdict(status: str, results: list[CheckRun], *, commit: str | None, truncated: bool,
                    incomplete: list[str]) -> str:
    """A commit is verified only by every check its change called for.

    A check dropped by the cap, or skipped (blocked, or needing approval
    because the change rewrote it), leaves a would-be pass ``partial`` and the
    evidence incomplete. Appends to *incomplete*; returns the status.
    """
    skipped = commit is not None and any(r.status == CHECK_SKIPPED for r in results)
    if truncated:
        incomplete.append(REASON_CHECKS_TRUNCATED)
    if skipped:
        incomplete.append(REASON_CHECK_NOT_COMPLETED)
    return STATUS_PARTIAL if status == STATUS_PASSED and (truncated or skipped) else status


def _reason(results: list[CheckRun], bound: bool, before: TreeState) -> str:
    if not results:
        return "OpenShard found no approved check to run."
    ran = sum(1 for r in results if r.status in (CHECK_PASSED, CHECK_FAILED))
    setup = next((r for r in results if r.setup_failed), None)
    if setup is not None:
        # The first such reason leads: it is why the outcome is unknown.
        lead = setup.note.rstrip(".") + "."
        if ran == 0:
            return lead
        return f"{lead} OpenShard read the exit codes of {ran} of {len(results)} check(s)."
    parts = [f"OpenShard ran {ran} of {len(results)} check(s) and read their exit codes"]
    if bound:
        parts.append(f"on clean commit {before.head[:12] if before.head else ''}")
    elif before.dirty:
        parts.append("on a working tree with uncommitted changes (not bound to a commit)")
    else:
        parts.append("(not bound to a commit)")
    return " ".join(parts) + "."


def attestations_path(repo_root: Path) -> Path:
    return repo_root / ".openshard" / ATTESTATIONS_FILENAME


def record_attestation(repo_root: Path, attestation: dict) -> Path:
    from openshard.history.jsonl_store import append_jsonl

    path = attestations_path(repo_root)
    append_jsonl(path, attestation)
    return path


def load_attestations(history_dir: Path) -> list[dict]:
    """Every well-formed attestation next to ``runs.jsonl`` (*history_dir* is ``.openshard``). Never raises."""
    path = history_dir / ATTESTATIONS_FILENAME
    out: list[dict] = []
    try:
        if not path.is_file():
            return out
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(item, dict) and item.get("kind") in ATTESTATION_KINDS:
                    out.append(item)
    except OSError:
        return out
    return out


def commit_attested(attestations: list[dict], entry: dict, commit: str) -> bool:
    """Whether an OpenShard re-run of exactly *commit* is already recorded for *entry*."""
    sha = commit.lower()
    return any(
        isinstance(item, dict) and item.get("kind") == KIND_POST_SESSION
        and item.get("commit") == sha and _names_entry(entry, item)
        for item in attestations
    )


def _names_entry(entry: dict, item: dict) -> bool:
    rid = entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None
    run_id = entry.get("run_id") if isinstance(entry.get("run_id"), str) else None
    return bool((rid and item.get("receipt_id") == rid) or (not rid and run_id and item.get("run_id") == run_id))


def evidence_for_entry(entry: dict, attestations: list[dict]) -> list[dict]:
    """Every attestation naming *entry* (by ``receipt_id``, else ``run_id``), oldest first, validated.

    This is the Shard's later-evidence history: each ``openshard verify``
    re-run and each CI verdict, in the order they were recorded. Nothing is
    merged or dropped except beyond the newest ``MAX_EVIDENCE``.
    """
    matched = [item for item in attestations if isinstance(item, dict) and _names_entry(entry, item)]
    return [summarize_attestation(item) for item in matched[-MAX_EVIDENCE:]]


def latest_for_entry(entry: dict, attestations: list[dict]) -> dict | None:
    """The later evidence recorded for *entry*, or None when there is none.

    The top-level ``attestation_id`` / ``created_at`` / ``verification`` are
    the newest OpenShard re-run (None when only CI evidence exists);
    ``evidence`` is the full history from ``evidence_for_entry``.
    """
    evidence = evidence_for_entry(entry, attestations)
    if not evidence:
        return None
    reruns = [item for item in evidence if item.get("kind") == KIND_POST_SESSION]
    latest = reruns[-1] if reruns else {
        "attestation_id": None, "created_at": None, "kind": None, "verification": None,
    }
    return {**latest, "evidence": evidence}


_CI_TOKEN_RE = re.compile(r"^[a-z_]{1,48}$")


def summarize_attestation(item: dict) -> dict:
    """The receipt-facing projection of an attestation; its block is re-validated on read."""
    ev = parse_verification_block(item.get("verification"))
    kind = KIND_CI if item.get("kind") == KIND_CI else KIND_POST_SESSION
    if kind == KIND_CI:
        # A CI verdict counts only as an independent report bound to a commit.
        if (
            ev.source != SOURCE_INDEPENDENTLY_VERIFIED
            or ev.observation_mode != MODE_CI_REPORT
            or not ev.artifact_sha
        ):
            ev.status = "unknown"
            ev.mark_incomplete("malformed_verification_block")
    elif ev.source != SOURCE_DIRECTLY_OBSERVED or ev.observation_mode != MODE_OPENSHARD_EXECUTED:
        # Only an OpenShard-executed block can be surfaced as a re-run; anything
        # else in this file is unreadable, never promoted.
        ev.status = "unknown"
        ev.mark_incomplete("malformed_verification_block")
    summary: dict[str, Any] = {
        "attestation_id": item.get("attestation_id") if isinstance(item.get("attestation_id"), str) else None,
        "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
        "kind": kind,
        "verification": ev.to_dict(),
    }
    if kind == KIND_CI:
        raw_ci = item.get("ci")
        ci: dict = raw_ci if isinstance(raw_ci, dict) else {}
        summary["ci"] = {
            key: ci.get(key) if isinstance(ci.get(key), str) and _CI_TOKEN_RE.match(ci[key]) else None
            for key in ("provider", "binding", "outcome")
        }
    return summary


def result_detail(result: CheckRun) -> str:
    """The CLI's parenthetical for one check: its exit code, else its note; both when setup failed."""
    if result.setup_failed:
        return result.note + (f"; exit {result.exit_code}" if result.exit_code is not None else "")
    return f"exit {result.exit_code}" if result.exit_code is not None else result.note


def display_line(summary: dict) -> str:
    """``2/2 passed @ 1a2b3c4d5e6f (OpenShard re-run)`` -- one receipt row."""
    v = summary.get("verification") or {}
    status = v.get("status")
    attempted = v.get("checks_attempted") or 0
    passed = v.get("checks_passed") or 0
    if status == STATUS_NOT_RUN:
        text = "Nothing run"
    elif status == "unknown" and REASON_VERIFIER_SETUP_FAILED in (v.get("incomplete_reasons") or []):
        text = "Could not run (project setup failed)"
    elif status in ("passed", "failed", "partial") and attempted:
        text = f"{passed}/{attempted} passed"
        if status == "partial":
            text += ", rest not completed"
    else:
        text = "Outcome unknown"
    sha = v.get("artifact_sha")
    text += f" @ {sha[:12]}" if isinstance(sha, str) and sha else " (not bound to a commit)"
    return f"{text} (OpenShard re-run)"


__all__ = [
    "ATTESTATIONS_FILENAME",
    "ATTESTATION_KINDS",
    "KIND_CI",
    "KIND_POST_SESSION",
    "CheckRun",
    "PlannedCheck",
    "REASON_VERIFIER_SETUP_FAILED",
    "TreeState",
    "attestations_path",
    "build_attestation",
    "display_line",
    "evidence_for_entry",
    "latest_for_entry",
    "load_attestations",
    "observed_check_commands",
    "plan_checks",
    "record_attestation",
    "result_detail",
    "run_checks",
    "summarize_attestation",
    "tree_state",
]
