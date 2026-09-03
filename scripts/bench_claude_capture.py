#!/usr/bin/env python
"""Latency benchmarks for OpenShard's Claude Code capture path (PR7).

Run from the repo root:

    python scripts/bench_claude_capture.py
    python scripts/bench_claude_capture.py --iterations 30 --json

No live Claude API calls, no network access, no new dependency (stdlib
``time``/``statistics``/``subprocess`` only). Safe to run repeatedly on a
laptop; each run works in its own temp directory and cleans up after itself.

Two layers are measured, because they answer different questions:

* **subprocess** -- the real ``openshard hooks claude`` / ``claude-status``
  command exactly as Claude Code invokes it (interpreter start-up + module
  import + hook logic). This is the number that determines whether a
  developer actually notices OpenShard. It is dominated by Python process
  start-up on most machines, which OpenShard cannot eliminate -- only avoid
  making worse by importing things the hook does not need.
* **in-process** -- the hook/status handler functions (and the JSONL
  store) called directly in this already-running interpreter, isolating
  capture *logic* cost from interpreter start-up. This is the number that
  actually reflects work done by OpenShard's own code.

Do not compare these numbers across machines/CI runners as a universal
performance claim -- they are a regression signal for maintainers on their
own box, not a benchmark of OpenShard in the abstract.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SID_PREFIX = "bench0000-0000-4000-8000-"


def _sid(n: int) -> str:
    return f"{SID_PREFIX}{n:012d}"


@dataclass
class Sample:
    name: str
    seconds: list[float] = field(default_factory=list)

    def stats(self) -> dict[str, float]:
        xs = sorted(self.seconds)
        n = len(xs)
        if n == 0:
            return {}
        def pct(p: float) -> float:
            idx = min(n - 1, max(0, int(round(p * (n - 1)))))
            return xs[idx]
        return {
            "n": n,
            "min_ms": xs[0] * 1000,
            "median_ms": statistics.median(xs) * 1000,
            "p95_ms": pct(0.95) * 1000,
            "p99_ms": pct(0.99) * 1000,
            "max_ms": xs[-1] * 1000,
        }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=bench@example.com", "-c", "user.name=bench", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(tmp: Path, name: str) -> Path:
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _payload(event: str, repo: Path, session_id: str, **fields: object) -> str:
    data: dict[str, object] = {
        "session_id": session_id, "cwd": str(repo), "hook_event_name": event,
        "transcript_path": str(repo / "transcript.jsonl"), "permission_mode": "default",
    }
    data.update(fields)
    return json.dumps(data)


def _status_payload(repo: Path, session_id: str, tokens: int) -> str:
    return json.dumps({
        "session_id": session_id, "cwd": str(repo),
        "model": {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
        "cost": {"total_cost_usd": round(tokens * 0.00001, 6)},
        "context_window": {"current_usage": {"input_tokens": tokens, "output_tokens": tokens // 8}},
    })


def _openshard_argv() -> list[str]:
    """Invoke the installed console script the same way Claude Code does."""
    from shutil import which
    exe = which("openshard")
    if exe:
        return [exe]
    return [sys.executable, "-m", "openshard.cli.entrypoint"]


def _run_subprocess(argv: list[str], stdin_text: str, env: dict[str, str]) -> float:
    t0 = time.perf_counter()
    subprocess.run(argv, input=stdin_text, text=True, capture_output=True, env=env, timeout=30)
    return time.perf_counter() - t0


def bench_subprocess(iterations: int) -> list[Sample]:
    import os
    import tempfile

    base_argv = _openshard_argv()
    samples: list[Sample] = []

    with tempfile.TemporaryDirectory(prefix="openshard-bench-") as tmp_s:
        tmp = Path(tmp_s)

        # 1) lightweight lifecycle hook: SessionStart (one fresh session per sample)
        s = Sample("subprocess: SessionStart (lifecycle hook)")
        for i in range(iterations):
            repo = _make_repo(tmp, f"start-{i}")
            env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
            payload = _payload("SessionStart", repo, _sid(i), source="startup")
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # 2) UserPromptSubmit on an already-started session (steady state)
        s = Sample("subprocess: UserPromptSubmit")
        repo = _make_repo(tmp, "prompt")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        subprocess.run([*base_argv, "hooks", "claude"], input=_payload("SessionStart", repo, _sid(9000), source="startup"),
                        text=True, capture_output=True, env=env)
        for i in range(iterations):
            payload = _payload("UserPromptSubmit", repo, _sid(9000), prompt=f"task {i}")
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # 3) PostToolUse, no file change (Bash), steady state (does not trigger a fold)
        s = Sample("subprocess: PostToolUse (no file change)")
        repo = _make_repo(tmp, "tool-nofile")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        subprocess.run([*base_argv, "hooks", "claude"], input=_payload("UserPromptSubmit", repo, _sid(9001), prompt="task"),
                        text=True, capture_output=True, env=env)
        for i in range(iterations):
            payload = _payload("PostToolUse", repo, _sid(9001), tool_name="Bash",
                                tool_input={"command": f"echo {i}"})
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # 4) PostToolUse that lands on a fold boundary with a real repo file change
        s = Sample("subprocess: PostToolUse (fold boundary, repo file changed)")
        for i in range(iterations):
            repo = _make_repo(tmp, f"tool-file-{i}")
            env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
            subprocess.run([*base_argv, "hooks", "claude"], input=_payload("UserPromptSubmit", repo, _sid(i), prompt="task"),
                            text=True, capture_output=True, env=env)
            (repo / "changed.py").write_text("x = 1\n", encoding="utf-8")
            payload = _payload("PostToolUse", repo, _sid(i), tool_name="Write",
                                tool_input={"file_path": str(repo / "changed.py")})
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # 5) Stop -- always folds, and is a SYNCHRONOUS hook Claude Code waits on every turn
        s = Sample("subprocess: Stop (sync hook, every turn)")
        for i in range(iterations):
            repo = _make_repo(tmp, f"stop-{i}")
            env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
            subprocess.run([*base_argv, "hooks", "claude"], input=_payload("UserPromptSubmit", repo, _sid(i), prompt="task"),
                            text=True, capture_output=True, env=env)
            payload = _payload("Stop", repo, _sid(i))
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # 6) SessionEnd -- also synchronous
        s = Sample("subprocess: SessionEnd (sync hook)")
        for i in range(iterations):
            repo = _make_repo(tmp, f"end-{i}")
            env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
            subprocess.run([*base_argv, "hooks", "claude"], input=_payload("UserPromptSubmit", repo, _sid(i), prompt="task"),
                            text=True, capture_output=True, env=env)
            payload = _payload("SessionEnd", repo, _sid(i), reason="other")
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # 7) status-line capture -- synchronous by construction, may fire very often
        s = Sample("subprocess: claude-status (status line)")
        repo = _make_repo(tmp, "status")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        subprocess.run([*base_argv, "hooks", "claude"], input=_payload("UserPromptSubmit", repo, _sid(9002), prompt="task"),
                        text=True, capture_output=True, env=env)
        for i in range(iterations):
            payload = _status_payload(repo, _sid(9002), tokens=1000 + i)
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude-status"], payload, env))
        samples.append(s)

    return samples


def bench_in_process(iterations: int) -> list[Sample]:
    import os
    import tempfile

    from openshard.adapters.claude_hooks import handle_claude_hook, handle_claude_status

    samples: list[Sample] = []

    with tempfile.TemporaryDirectory(prefix="openshard-bench-inproc-") as tmp_s:
        tmp = Path(tmp_s)
        repo = _make_repo(tmp, "inproc")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}

        s = Sample("in-process: handle_claude_hook(UserPromptSubmit)")
        handle_claude_hook(json.loads(_payload("SessionStart", repo, _sid(1), source="startup")), env=env)
        for i in range(iterations):
            data = json.loads(_payload("UserPromptSubmit", repo, _sid(1), prompt=f"t{i}"))
            t0 = time.perf_counter()
            handle_claude_hook(data, env=env)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: handle_claude_hook(PostToolUse, no fold)")
        for i in range(iterations):
            data = json.loads(_payload("PostToolUse", repo, _sid(1), tool_name="Bash", tool_input={"command": "ls"}))
            t0 = time.perf_counter()
            handle_claude_hook(data, env=env)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: handle_claude_status (steady state, no fold)")
        for i in range(iterations):
            data = json.loads(_status_payload(repo, _sid(1), tokens=2000 + i))
            t0 = time.perf_counter()
            handle_claude_status(data, env=env)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: handle_claude_hook(Stop, folds)")
        for i in range(iterations):
            repo_i = _make_repo(tmp, f"inproc-stop-{i}")
            env_i = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo_i)}
            handle_claude_hook(json.loads(_payload("UserPromptSubmit", repo_i, _sid(100 + i), prompt="t")), env=env_i)
            data = json.loads(_payload("Stop", repo_i, _sid(100 + i)))
            t0 = time.perf_counter()
            handle_claude_hook(data, env=env_i)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

    return samples


def bench_jsonl_store(iterations: int) -> list[Sample]:
    import tempfile

    from openshard.history.jsonl_store import append_jsonl, upsert_jsonl

    samples: list[Sample] = []
    with tempfile.TemporaryDirectory(prefix="openshard-bench-jsonl-") as tmp_s:
        path = Path(tmp_s) / "runs.jsonl"

        s = Sample("in-process: append_jsonl")
        for i in range(iterations):
            t0 = time.perf_counter()
            append_jsonl(path, {"i": i, "task": f"task {i}"})
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: upsert_jsonl (replace, growing file)")
        for i in range(iterations):
            t0 = time.perf_counter()
            upsert_jsonl(path, {"i": 0, "task": f"updated {i}"}, lambda e: e.get("i") == 0)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iterations", type=int, default=20, help="samples per subprocess scenario (default: 20)")
    parser.add_argument("--in-process-iterations", type=int, default=200, help="samples per in-process scenario (default: 200)")
    parser.add_argument("--skip-subprocess", action="store_true", help="skip the (slower) real subprocess scenarios")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    args = parser.parse_args()

    all_samples: list[Sample] = []
    if not args.skip_subprocess:
        all_samples += bench_subprocess(args.iterations)
    all_samples += bench_in_process(args.in_process_iterations)
    all_samples += bench_jsonl_store(args.in_process_iterations)

    if args.json:
        print(json.dumps({s.name: s.stats() for s in all_samples}, indent=2))
        return

    name_w = max(len(s.name) for s in all_samples) + 2
    header = f"{'scenario':<{name_w}}{'n':>5}{'median':>10}{'p95':>10}{'p99':>10}{'max':>10}"
    print(header)
    print("-" * len(header))
    for s in all_samples:
        st = s.stats()
        if not st:
            continue
        print(
            f"{s.name:<{name_w}}{st['n']:>5}{st['median_ms']:>9.1f}m{st['p95_ms']:>9.1f}m"
            f"{st['p99_ms']:>9.1f}m{st['max_ms']:>9.1f}m"
        )


if __name__ == "__main__":
    main()
