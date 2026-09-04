"""Deterministic, benchmark-owned verification of a workspace.

Verification never runs inside the coding agent's session and never
touches OpenShard's history: it is a list of commands the scenario
declares, run after the agent has exited, with stdout/stderr captured to
files. The benchmark's verdict (``passed``) is one field of the run
result, recorded next to -- never merged into -- what the agent reported
and what OpenShard captured.

The *known failed approach* check is machine-evaluated from the same
verification outcome plus git's view of what the agent changed, using the
criteria the scenario spells out.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.config import (
    Criterion,
    KnownFailedApproach,
    VerificationStep,
    substitute,
)
from evals.pr13.benchmark.workspace import ChangedPaths

_TAIL_CAP = 4000


@dataclass
class StepResult:
    name: str
    argv: list[str]
    cwd: str
    returncode: int | None
    passed: bool
    timed_out: bool
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    stdout_tail: str
    stderr_tail: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "argv": list(self.argv), "cwd": self.cwd, "returncode": self.returncode,
            "passed": self.passed, "timed_out": self.timed_out, "duration_seconds": self.duration_seconds,
            "stdout_path": self.stdout_path, "stderr_path": self.stderr_path,
            "stdout_tail": self.stdout_tail, "stderr_tail": self.stderr_tail, "error": self.error,
        }


@dataclass
class VerificationResult:
    passed: bool
    steps: list[StepResult] = field(default_factory=list)
    python: str = sys.executable

    def step(self, name: str) -> StepResult | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "python": self.python,
            "steps": [s.to_dict() for s in self.steps],
            "failed_steps": [s.name for s in self.steps if not s.passed],
        }


def _tail(text: str) -> str:
    return text if len(text) <= _TAIL_CAP else "…" + text[-(_TAIL_CAP - 1):]


def run_verification(
    steps: tuple[VerificationStep, ...] | list[VerificationStep],
    *,
    workspace: Path,
    scenario_dir: Path,
    out_dir: Path,
    python: str | None = None,
    env: dict[str, str] | None = None,
) -> VerificationResult:
    """Run every step in order (all of them, even after a failure) and record each outcome."""
    out_dir.mkdir(parents=True, exist_ok=True)
    py = python or sys.executable
    mapping = {"python": py, "workspace": str(workspace), "scenario": str(scenario_dir)}
    results: list[StepResult] = []
    for step in steps:
        argv = [substitute(a, mapping) for a in step.argv]
        cwd = substitute(step.cwd, mapping)
        step_env: dict[str, str] | None = dict(env) if env is not None else None
        if step.env:
            step_env = step_env if step_env is not None else dict(os.environ)
            step_env.update({k: substitute(v, mapping) for k, v in step.env.items()})
        stdout_path = out_dir / f"{step.name}.stdout.txt"
        stderr_path = out_dir / f"{step.name}.stderr.txt"
        t0 = time.monotonic()
        returncode: int | None = None
        timed_out = False
        error: str | None = None
        stdout = stderr = ""
        try:
            completed = subprocess.run(
                argv, cwd=cwd, env=step_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=step.timeout_seconds,
            )
            returncode = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            error = f"timed out after {step.timeout_seconds}s"
        except (FileNotFoundError, PermissionError, OSError) as exc:
            error = f"could not run: {exc}"
        duration = round(time.monotonic() - t0, 3)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        results.append(StepResult(
            name=step.name, argv=argv, cwd=cwd, returncode=returncode,
            passed=(returncode == 0 and not timed_out and error is None), timed_out=timed_out,
            duration_seconds=duration, stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            stdout_tail=_tail(stdout), stderr_tail=_tail(stderr), error=error,
        ))
    return VerificationResult(passed=bool(results) and all(r.passed for r in results), steps=results, python=py)


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()


def _evaluate_criterion(c: Criterion, verification: VerificationResult, changed: ChangedPaths) -> dict[str, Any]:
    changed_set = {_norm(p) for p in changed.all}
    if c.kind == "verification_failed":
        return {"kind": c.kind, "matched": not verification.passed}
    if c.kind == "verification_step_failed":
        step = verification.step(str(c.params.get("step")))
        return {"kind": c.kind, "step": c.params.get("step"),
                "matched": step is not None and not step.passed, "step_found": step is not None}
    any_of = [_norm(p) for p in c.params.get("any_of", [])]
    all_of = [_norm(p) for p in c.params.get("all_of", [])]
    if c.kind == "paths_changed":
        ok_any = any(p in changed_set for p in any_of) if any_of else True
        ok_all = all(p in changed_set for p in all_of) if all_of else True
        return {"kind": c.kind, "any_of": any_of, "all_of": all_of, "matched": ok_any and ok_all}
    if c.kind == "paths_unchanged":
        ok_any = any(p not in changed_set for p in any_of) if any_of else True
        ok_all = all(p not in changed_set for p in all_of) if all_of else True
        return {"kind": c.kind, "any_of": any_of, "all_of": all_of, "matched": ok_any and ok_all}
    return {"kind": c.kind, "matched": False, "error": "unknown criterion kind"}


def evaluate_known_failed_approach(
    spec: KnownFailedApproach, verification: VerificationResult, changed: ChangedPaths,
) -> dict[str, Any]:
    """Machine-checkable verdict: did this workspace end up in the known failed approach?"""
    details = [_evaluate_criterion(c, verification, changed) for c in spec.criteria]
    flags = [bool(d.get("matched")) for d in details]
    matched = all(flags) if spec.mode == "all" else any(flags)
    return {
        "id": spec.id,
        "description": spec.description,
        "mode": spec.mode,
        "matched": matched,
        "criteria": details,
        "changed_paths": changed.to_dict(),
    }
