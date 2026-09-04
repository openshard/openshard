"""Two additional real-agent burn-in mechanisms beyond the default hook capture.

``harness.py``/``capture.py`` are otherwise untouched by these; every
scenario using the default ``claude_hooks`` burn-in (Scenario 1 and most
of the rest) is completely unaffected. Only scenarios that explicitly opt
in via ``burn_in.capture`` in their ``metadata.json`` reach this module.

wrap-chain (``capture: "claude_wrap_chain"``)
----------------------------------------------
Claude Code's own hook capture never records a verification outcome for
any attempt, so a hook-captured Shard can never carry the
``verification_passed`` field OpenShard's PR11 recovery-observation
machinery (``history.query.RecoveryObservation``) reads -- see the
"Multi-attempt chronology" scenario's own README for the full,
explicit product-gap statement: external-agent capture (hooks, the
OpenCode plugin, and ``openshard wrap`` alike) does not persist that
field today, so PR13 cannot honestly claim to exercise
``RecoveryObservation`` for externally-captured history, only the
weaker (but real) same-Shard multi-attempt chronology below. The one
real, non-fabricated OpenShard capability that DOES let a real external
agent's run be explicitly linked to a prior attempt of the same
persistent Shard is ``openshard wrap claude --shard <id>`` (Migration 2's
Run/Attempt linkage). Each stage here is therefore a real ``claude -p``
session, launched through ``openshard wrap claude`` rather than directly,
so OpenShard captures it as another real attempt of one Shard -- still
exactly "the real Claude Code CLI", just invoked one layer further out.

OpenCode burn-in (``capture: "opencode_hooks"``)
-------------------------------------------------
Runs the burn-in through the real, installed OpenCode CLI instead of
Claude Code, using OpenCode's own production plugin capture
(``openshard.adapters.opencode_plugin_install``) against the same private
capture service the Claude Code path uses. OpenCode's ``run`` subcommand
takes the prompt as a positional argument (not stdin) and is not
protocol-compatible with ``harness.run_agent``'s Claude-Code-specific
argv/stdin handling, so it gets its own minimal subprocess wrapper here
rather than being forced through that module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.errors import BenchmarkError
from evals.pr13.benchmark.harness import utc_now
from openshard.history.metrics import load_runs

# ---------------------------------------------------------------------------
# wrap-chain
# ---------------------------------------------------------------------------


@dataclass
class WrapStageResult:
    stage_index: int
    task: str
    argv: list[str]
    started_at: str
    ended_at: str
    wall_clock_seconds: float
    exit_code: int | None
    timed_out: bool
    launch_error: str | None
    stdout_path: str
    stderr_path: str
    shard_id: str | None
    attempt_number: int | None
    run_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_index": self.stage_index, "task": self.task, "argv": list(self.argv),
            "started_at": self.started_at, "ended_at": self.ended_at,
            "wall_clock_seconds": self.wall_clock_seconds, "exit_code": self.exit_code,
            "timed_out": self.timed_out, "launch_error": self.launch_error,
            "stdout_path": self.stdout_path, "stderr_path": self.stderr_path,
            "shard_id": self.shard_id, "attempt_number": self.attempt_number, "run_id": self.run_id,
        }


def _last_entry(workspace: Path) -> dict | None:
    """The most recently appended run entry. ``wrap`` writes synchronously, so
    no polling/lock-retry is needed here (unlike the async hook fold)."""
    entries = load_runs(workspace)
    return entries[-1] if entries else None


def run_wrap_stage(
    *, openshard_exe: str, task: str, workspace: Path, claude_inner_argv: list[str], prompt: str,
    env: dict[str, str], shard_id: str | None, stage_index: int, timeout_seconds: float, out_dir: Path,
) -> WrapStageResult:
    """Run one real ``openshard wrap claude [--shard <id>] -- <claude argv>`` stage.

    ``claude_inner_argv`` is the full real Claude Code CLI command (the
    same shape ``harness.build_argv`` produces); the prompt is written to
    the *wrapped* Claude process's stdin, exactly as a direct invocation
    would. ``wrap`` writes the resulting Shard entry synchronously before
    it exits -- no polling or capture service is needed here, unlike hook
    capture. Never raises for the wrapped agent's own failures; raises
    ``BenchmarkError("claude_launch_failed")`` only if ``openshard`` itself
    could not be started.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [openshard_exe, "wrap", "claude", "--task", task, "--repo-path", str(workspace)]
    if shard_id:
        argv += ["--shard", shard_id]
    argv += ["--", *claude_inner_argv]
    (out_dir / f"stage{stage_index}_argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
    (out_dir / f"stage{stage_index}_prompt.txt").write_text(prompt, encoding="utf-8")
    stdout_path = out_dir / f"stage{stage_index}_stdout.txt"
    stderr_path = out_dir / f"stage{stage_index}_stderr.txt"

    started_at = utc_now()
    t0 = time.monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=str(workspace), env=env, input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_seconds,
        )
        exit_code: int | None = completed.returncode
        timed_out = False
        launch_error = None
        stdout_path.write_bytes(completed.stdout)
        stderr_path.write_bytes(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code, timed_out, launch_error = None, True, None
        stdout_path.write_bytes(exc.stdout or b"")
        stderr_path.write_bytes(exc.stderr or b"")
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise BenchmarkError(
            "claude_launch_failed", f"could not launch 'openshard wrap claude': {exc}", details={"argv": argv},
        ) from exc
    ended_at = utc_now()
    wall = round(time.monotonic() - t0, 3)

    entry = _last_entry(workspace) if not timed_out else None
    return WrapStageResult(
        stage_index=stage_index, task=task, argv=argv, started_at=started_at, ended_at=ended_at,
        wall_clock_seconds=wall, exit_code=exit_code, timed_out=timed_out, launch_error=launch_error,
        stdout_path=str(stdout_path), stderr_path=str(stderr_path),
        shard_id=(entry.get("shard_id") if entry else None),
        attempt_number=(entry.get("attempt_number") if entry else None),
        run_id=(entry.get("run_id") if entry else None),
    )


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


def detect_opencode_cli() -> str | None:
    return shutil.which("opencode")


@dataclass
class OpenCodeRun:
    argv: list[str]
    started_at: str
    ended_at: str
    wall_clock_seconds: float
    exit_code: int | None
    timed_out: bool
    stdout_path: str
    stderr_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv), "started_at": self.started_at, "ended_at": self.ended_at,
            "wall_clock_seconds": self.wall_clock_seconds, "exit_code": self.exit_code,
            "timed_out": self.timed_out, "stdout_path": self.stdout_path, "stderr_path": self.stderr_path,
        }


def run_opencode(
    *, opencode_exe: str, workspace: Path, model: str, prompt: str, env: dict[str, str],
    timeout_seconds: float, out_dir: Path,
) -> OpenCodeRun:
    """Run one real, non-interactive ``opencode run`` session. Never raises for the agent's own failures.

    OpenCode's ``run [message..]`` takes the prompt as a positional
    argument (verified via ``opencode run --help``), not stdin, so this is
    a plain subprocess call rather than a reuse of ``harness.run_agent``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [opencode_exe, "run", "--dir", str(workspace), "--format", "json", "--model", model, prompt]
    (out_dir / "opencode_argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
    stdout_path = out_dir / "opencode_stdout.txt"
    stderr_path = out_dir / "opencode_stderr.txt"

    started_at = utc_now()
    t0 = time.monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=str(workspace), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_seconds,
        )
        exit_code: int | None = completed.returncode
        timed_out = False
        stdout_path.write_bytes(completed.stdout)
        stderr_path.write_bytes(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code, timed_out = None, True
        stdout_path.write_bytes(exc.stdout or b"")
        stderr_path.write_bytes(exc.stderr or b"")
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise BenchmarkError(
            "opencode_launch_failed", f"could not launch 'opencode run': {exc}", details={"argv": argv},
        ) from exc
    ended_at = utc_now()
    return OpenCodeRun(
        argv=argv, started_at=started_at, ended_at=ended_at, wall_clock_seconds=round(time.monotonic() - t0, 3),
        exit_code=exit_code, timed_out=timed_out, stdout_path=str(stdout_path), stderr_path=str(stderr_path),
    )
