from __future__ import annotations

import dataclasses
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openshard.evals.registry import EvalTask
from openshard.execution.generator import ExecutionGenerator
from openshard.history.jsonl_store import append_jsonl
from openshard.providers.openrouter import compute_cost
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path
from openshard.verification.executor import run_verification_plan
from openshard.verification.plan import build_verification_plan


@dataclass
class EvalResult:
    timestamp: str
    suite: str
    task_id: str
    model: str
    passed: bool
    duration_seconds: float
    verification_attempted: bool
    verification_passed: bool | None
    verification_returncode: int | None
    verification_output: str | None
    files_written: list[str] = field(default_factory=list)
    unsafe_files: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float | None = None
    error: str | None = None


def run_eval_task(
    task: EvalTask,
    model: str,
    suite: str,
    workspace_root: Path,
) -> EvalResult:
    start = time.monotonic()
    timestamp = datetime.now(UTC).isoformat()

    fixtures_dir = task.task_dir / "fixtures"
    if fixtures_dir.exists():
        shutil.copytree(fixtures_dir, workspace_root, dirs_exist_ok=True)

    files_written: list[str] = []
    unsafe_files: list[str] = []
    error: str | None = None
    verification_attempted = False
    verification_passed: bool | None = None
    verification_returncode: int | None = None
    verification_output: str | None = None
    prompt_tokens = completion_tokens = total_tokens = 0
    cost: float | None = None

    try:
        gen = ExecutionGenerator()
        result = gen.generate(task.prompt, model=model)

        if result.usage:
            prompt_tokens = getattr(result.usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(result.usage, "completion_tokens", 0) or 0
            total_tokens = getattr(result.usage, "total_tokens", 0) or 0
            cost = compute_cost(model, prompt_tokens, completion_tokens)

        root = workspace_root.resolve()
        for f in result.files:
            try:
                target = resolve_safe_repo_path(root, f.path)
            except UnsafePathError:
                unsafe_files.append(f.path)
                continue
            if f.change_type == "delete":
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f.content, encoding="utf-8")
                files_written.append(f.path)

        if task.verification_command:
            verification_attempted = True
            plan = build_verification_plan(
                {"verification_command": task.verification_command}, None
            )
            returncode, output = run_verification_plan(  # type: ignore[misc]  # capture=True always returns tuple; return type is int | tuple
                plan, cwd=workspace_root, gate=None, capture=True,
                pre_approved_by="eval_task_config",
            )
            verification_returncode = returncode
            verification_output = output
            verification_passed = returncode == 0
            passed = verification_passed
            if not passed and unsafe_files:
                error = f"verification failed; unsafe paths skipped: {unsafe_files}"
            elif not passed:
                error = f"verification failed (exit code {returncode})"
        else:
            passed = False
            error = "no verification command configured"

    except Exception as exc:  # noqa: BLE001
        passed = False
        error = str(exc)

    duration = time.monotonic() - start
    return EvalResult(
        timestamp=timestamp,
        suite=suite,
        task_id=task.id,
        model=model,
        passed=passed,
        duration_seconds=round(duration, 2),
        verification_attempted=verification_attempted,
        verification_passed=verification_passed,
        verification_returncode=verification_returncode,
        verification_output=verification_output,
        files_written=files_written,
        unsafe_files=unsafe_files,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost=cost,
        error=error,
    )


def append_eval_result(result: EvalResult, log_path: Path) -> None:
    record = dataclasses.asdict(result)
    append_jsonl(log_path, record)
