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
``check_not_completed``, never ``failed`` or ``passed``. A skipped check
(blocked, or needing approval that was not given) is ``skipped``. Nothing run
at all -> ``not_run``. Command output is streamed to the terminal (or
discarded under ``--json``) and never stored; the attestation carries only a
path-free check label, the exit code and the duration.

This is evidence, not policy: nothing here gates, blocks or fails anything.
"""

from __future__ import annotations

import json
import re
import shutil
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
    MODE_OPENSHARD_EXECUTED,
    REASON_ARTIFACT_NOT_BOUND,
    REASON_CHECK_NOT_COMPLETED,
    SOURCE_DIRECTLY_OBSERVED,
    STATUS_NOT_RUN,
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
MAX_PLANNED_CHECKS = 8
DEFAULT_TIMEOUT_SECONDS = 600.0

ORIGIN_CONTRACT = "contract"  # the repository's configured verification contract
ORIGIN_DETECTED = "detected"  # the test command OpenShard detects for the repository
ORIGIN_OBSERVED = "observed"  # a check command the agent was observed running (opt-in)

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


@dataclass
class CheckRun:
    check: PlannedCheck
    status: str  # passed | failed | skipped | unknown
    exit_code: int | None = None
    duration_seconds: float | None = None
    note: str = ""


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


def run_checks(
    planned: list[PlannedCheck],
    repo_root: Path,
    *,
    approve: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stream: bool = True,
    runner: Runner | None = None,
    on_start: Callable[[PlannedCheck], None] | None = None,
) -> list[CheckRun]:
    """Run each planned check that its safety class allows. Never raises."""
    run = runner or subprocess.run
    results: list[CheckRun] = []
    for check in planned:
        if check.safety == CommandSafety.blocked.value:
            results.append(CheckRun(check, CHECK_SKIPPED, note=f"blocked: {check.reason}"))
            continue
        if check.safety == CommandSafety.needs_approval.value and not approve:
            results.append(CheckRun(check, CHECK_SKIPPED, note="needs approval (re-run with --approve)"))
            continue
        exe = shutil.which(check.argv[0]) if runner is None else check.argv[0]
        if exe is None:
            results.append(CheckRun(check, CHECK_UNKNOWN, note="executable not found; not run"))
            continue
        if on_start is not None:
            on_start(check)
        io: dict[str, Any] = {} if stream else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        started = time.monotonic()
        try:
            proc = run([exe, *check.argv[1:]], cwd=repo_root, timeout=timeout, check=False, **io)
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
) -> dict:
    """One ``verifications.jsonl`` line: what OpenShard ran, saw, and against which tree."""
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
    )
    if not bound:
        incomplete.append(REASON_ARTIFACT_NOT_BOUND)
    if any(r.status == CHECK_UNKNOWN for r in results):
        incomplete.append(REASON_CHECK_NOT_COMPLETED)
    status = aggregate_status(checks) if checks else STATUS_NOT_RUN
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
        "kind": "post_session_verification",
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
            {"name": r.check.name, "origin": r.check.origin, "safety": r.check.safety, "note": r.note[:120]}
            for r in results
        ],
        "raw_output_stored": False,
        "verification": block,
    }


def _reason(results: list[CheckRun], bound: bool, before: TreeState) -> str:
    if not results:
        return "OpenShard found no approved check to run."
    ran = sum(1 for r in results if r.status in (CHECK_PASSED, CHECK_FAILED))
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
                if isinstance(item, dict) and item.get("kind") == "post_session_verification":
                    out.append(item)
    except OSError:
        return out
    return out


def latest_for_entry(entry: dict, attestations: list[dict]) -> dict | None:
    """The newest attestation naming *entry* (by ``receipt_id``, else ``run_id``), validated."""
    rid = entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None
    run_id = entry.get("run_id") if isinstance(entry.get("run_id"), str) else None
    if not rid and not run_id:
        return None
    for item in reversed(attestations):
        if (rid and item.get("receipt_id") == rid) or (not rid and run_id and item.get("run_id") == run_id):
            return summarize_attestation(item)
    return None


def summarize_attestation(item: dict) -> dict:
    """The receipt-facing projection of an attestation; its block is re-validated on read."""
    ev = parse_verification_block(item.get("verification"))
    if ev.source != SOURCE_DIRECTLY_OBSERVED or ev.observation_mode != MODE_OPENSHARD_EXECUTED:
        # Only an OpenShard-executed block can be surfaced as a re-run; anything
        # else in this file is unreadable, never promoted.
        ev.status = "unknown"
        ev.mark_incomplete("malformed_verification_block")
    return {
        "attestation_id": item.get("attestation_id") if isinstance(item.get("attestation_id"), str) else None,
        "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
        "verification": ev.to_dict(),
    }


def display_line(summary: dict) -> str:
    """``2/2 passed @ 1a2b3c4d5e6f (OpenShard re-run)`` -- one receipt row."""
    v = summary.get("verification") or {}
    status = v.get("status")
    attempted = v.get("checks_attempted") or 0
    passed = v.get("checks_passed") or 0
    if status == STATUS_NOT_RUN:
        text = "Nothing run"
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
    "CheckRun",
    "PlannedCheck",
    "TreeState",
    "attestations_path",
    "build_attestation",
    "display_line",
    "latest_for_entry",
    "load_attestations",
    "observed_check_commands",
    "plan_checks",
    "record_attestation",
    "run_checks",
    "summarize_attestation",
    "tree_state",
]
