"""Qualify a guard: prove it catches the mistake before it is trusted.

A guard that passes on the code it was written against proves nothing. This
runs the guard three ways, never on the live checkout's history:

1. on an isolated copy of the **bad** commit (the agent's mistake), with the
   guard's own test files copied in from the working tree -- it must fail;
2. on an isolated copy of the **good** commit (the correction), same copy-in
   -- it must pass;
3. on the working tree, ``runs`` times -- it must pass every time (stable,
   so it will not block unrelated changes at random).

Verdicts:

* ``qualified`` -- failed on bad, passed on good, passed every stability run;
* ``passes_on_bad`` -- it did not catch the mistake;
* ``cannot_load_on_bad`` -- pytest could not collect or import it on the bad
  commit (exit code 2, 4 or 5): that shows the code is new, not that the guard
  tests the fix;
* ``fails_on_good`` -- it fails on the correction too;
* ``flaky`` -- it failed at least one stability run;
* ``could_not_run`` -- a run was blocked, needs approval, timed out, or a
  copy of a commit could not be made.

Only the exit codes OpenShard observed decide the verdict. Nothing is
installed; JavaScript dependencies are linked offline the same way as
``openshard verify --commit``.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from openshard.guards.model import Guard
from openshard.guards.plan import _referenced_paths

DEFAULT_RUNS = 3
_PYTEST_LOAD_FAILURES = frozenset({2, 3, 4, 5})

QUALIFIED = "qualified"
PASSES_ON_BAD = "passes_on_bad"
CANNOT_LOAD_ON_BAD = "cannot_load_on_bad"
FAILS_ON_GOOD = "fails_on_good"
FLAKY = "flaky"
COULD_NOT_RUN = "could_not_run"


@dataclass
class RunOutcome:
    where: str  # bad | good | stable-<n>
    status: str  # passed | failed | skipped | unknown
    exit_code: int | None
    seconds: float | None
    note: str = ""


@dataclass
class Qualification:
    guard_id: str
    verdict: str
    reason: str
    bad: str | None
    good: str | None
    runs: list[RunOutcome] = field(default_factory=list)

    @property
    def stable_runs(self) -> int:
        return sum(1 for r in self.runs if r.where.startswith("stable") and r.status == "passed")

    @property
    def runtime_s(self) -> float | None:
        times = [r.seconds for r in self.runs if r.seconds is not None]
        return round(max(times), 2) if times else None

    def to_record(self, *, at: str) -> dict[str, Any]:
        """The ``qualification`` block written into the guard file."""
        return {
            "verdict": self.verdict,
            "failed_on": self.bad if self.verdict == QUALIFIED else None,
            "passed_on": self.good if self.verdict == QUALIFIED else None,
            "stable_runs": self.stable_runs,
            "runtime_s": self.runtime_s,
            "qualified_at": at,
            "method": "openshard guard qualify v1",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "guard": self.guard_id, "verdict": self.verdict, "reason": self.reason,
            "bad": self.bad, "good": self.good, "stable_runs": self.stable_runs, "runtime_s": self.runtime_s,
            "runs": [r.__dict__ for r in self.runs],
        }


def _is_pytest(argv: list[str]) -> bool:
    return any(PurePosixPath(t).name in ("pytest", "py.test") for t in argv[:3])


def _overlay(tree: Path, source: Path, paths: set[str]) -> list[str]:
    """Copy the guard's own files from the working tree into *tree*. Returns what was copied."""
    copied: list[str] = []
    for rel in sorted(paths):
        src, dst = source / rel, tree / rel
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(rel)
    return copied


def _one(check: Any, tree: Path, *, where: str, approve: bool, timeout: float, env: dict | None,
         runner: Any) -> RunOutcome:
    from openshard.verification.post_session import run_checks

    started = time.monotonic()
    [result] = run_checks([check], tree, approve=approve, timeout=timeout, stream=False, env=env, runner=runner)
    seconds = result.duration_seconds if result.duration_seconds is not None else round(time.monotonic() - started, 2)
    return RunOutcome(where=where, status=result.status, exit_code=result.exit_code, seconds=seconds,
                      note=result.note)


def qualify_guard(
    repo_root: Path, guard: Guard, bad: str, good: str, *, runs: int = DEFAULT_RUNS, approve: bool = False,
    timeout: float = 600.0, runner: Any = None,
) -> Qualification:
    """Run *guard* on *bad*, *good* and the working tree and return the verdict. Never raises."""
    from openshard.verification.commit_checkout import commit_checkout
    from openshard.verification.plan import VerificationSource, classify_command_safety
    from openshard.verification.post_session import ORIGIN_GUARD, PlannedCheck

    root = Path(repo_root)
    q = Qualification(guard_id=guard.id, verdict=COULD_NOT_RUN, reason="", bad=bad, good=good)
    if not guard.command or guard.kind != "test":
        q.reason = "only a 'test' guard with a command can be qualified"
        return q
    argv = list(guard.command)
    safety, why = classify_command_safety(argv, VerificationSource.config)
    check = PlannedCheck(name=guard.label, argv=argv, kind="test", origin=ORIGIN_GUARD, safety=safety.value,
                         reason=why, cwd=guard.cwd, why="qualification")
    own_files = _referenced_paths(guard)
    prepare_js = Path(root, guard.cwd or ".", "package.json").is_file()

    def at_commit(sha: str, where: str) -> RunOutcome:
        try:
            with commit_checkout(root, sha, prepare_js=prepare_js, force_isolated=True) as co:
                if not co.ready or co.path is None:
                    return RunOutcome(where=where, status="unknown", exit_code=None, seconds=None,
                                      note=co.problem or "could not make an isolated copy")
                _overlay(co.path, root, own_files)
                return _one(check, co.path, where=where, approve=approve, timeout=timeout, env=co.env or None,
                            runner=runner)
        except OSError as exc:
            return RunOutcome(where=where, status="unknown", exit_code=None, seconds=None,
                              note=f"isolated copy failed ({exc.__class__.__name__})")

    on_bad = at_commit(bad, "bad")
    q.runs.append(on_bad)
    if on_bad.status in ("skipped", "unknown"):
        q.reason = f"could not run on the bad commit: {on_bad.note or on_bad.status}"
        return q
    if on_bad.status == "passed":
        q.verdict, q.reason = PASSES_ON_BAD, "it passed on the bad commit, so it does not catch the mistake"
        return q
    if _is_pytest(argv) and on_bad.exit_code in _PYTEST_LOAD_FAILURES:
        q.verdict = CANNOT_LOAD_ON_BAD
        q.reason = (f"pytest exited {on_bad.exit_code} on the bad commit (collection or import error, or no "
                    "tests), which shows the code is new, not that the guard tests the fix")
        return q

    on_good = at_commit(good, "good")
    q.runs.append(on_good)
    if on_good.status in ("skipped", "unknown"):
        q.reason = f"could not run on the good commit: {on_good.note or on_good.status}"
        return q
    if on_good.status == "failed":
        q.verdict, q.reason = FAILS_ON_GOOD, "it fails on the correction too"
        return q

    for i in range(max(1, runs)):
        outcome = _one(check, root, where=f"stable-{i + 1}", approve=approve, timeout=timeout, env=None,
                       runner=runner)
        q.runs.append(outcome)
        if outcome.status in ("skipped", "unknown"):
            q.reason = f"could not run on the working tree: {outcome.note or outcome.status}"
            return q
        if outcome.status == "failed":
            q.verdict, q.reason = FLAKY, f"it failed stability run {i + 1} of {runs} on the working tree"
            return q

    q.verdict = QUALIFIED
    q.reason = f"failed on the bad commit, passed on the good commit and {runs} times on the working tree"
    return q


# ---------------------------------------------------------------------------
# Writing the result into the guard file
# ---------------------------------------------------------------------------


def write_qualification(path: Path, record: dict[str, Any], *, activate: bool) -> None:
    """Replace the file's top-level ``qualification:`` block (comments elsewhere are kept).

    With *activate* (only ever for a ``qualified`` verdict) a ``status:`` line
    is set to ``active``.
    """
    import yaml

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        top_level = bool(line) and not line[0].isspace() and not line.startswith("#")
        if top_level:
            skipping = line.startswith("qualification:")
            if skipping:
                continue
        elif skipping:
            continue
        if activate and line.startswith("status:"):
            line = "status: active"
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    block = yaml.safe_dump({"qualification": record}, sort_keys=False, default_flow_style=False).rstrip()
    path.write_text("\n".join([*out, block]) + "\n", encoding="utf-8")


__all__ = [
    "CANNOT_LOAD_ON_BAD", "COULD_NOT_RUN", "DEFAULT_RUNS", "FAILS_ON_GOOD", "FLAKY", "PASSES_ON_BAD",
    "QUALIFIED", "Qualification", "RunOutcome", "qualify_guard", "write_qualification",
]
