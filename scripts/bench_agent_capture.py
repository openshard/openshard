#!/usr/bin/env python
"""Latency benchmarks for the Codex and OpenCode capture ingress paths (PR12).

Run from the repo root:

    python scripts/bench_agent_capture.py
    python scripts/bench_agent_capture.py --iterations 200 --json
    python scripts/bench_agent_capture.py --skip-subprocess

Companion to ``scripts/bench_claude_capture.py`` (same safety rules: an
in-thread capture service on an ephemeral port under a temp
``OPENSHARD_HOME``, ``OPENSHARD_CAPTURE_NO_SPAWN=1`` on every subprocess,
no network beyond 127.0.0.1, bounded iterations and wall clock). It
measures what each agent actually waits on:

* **Codex** -- Codex only has command hooks, so the blocking cost is the
  ``openshard hooks codex`` process: interpreter start-up plus one loopback
  POST. Reported both as the raw POST (what the service adds) and as the
  full subprocess (what Codex feels). ``PostToolUse`` is installed as an
  async hook, so Codex's tool loop never waits on it at all; the number is
  still reported for completeness.
* **OpenCode** -- the plugin POSTs from inside OpenCode's process, so the
  blocking cost is the POST alone. Measured with the Python client and,
  when ``node`` is on PATH, with ``fetch`` from node -- the same runtime
  family (Bun/Node) the plugin runs in.
* **fold-behind** -- for both agents, the time from a turn-ending POST
  returning to the folded receipt being visible in ``runs.jsonl``: the
  eventual-consistency lag, never on the caller's path.

Numbers are a regression signal for maintainers on their own machine, not
a universal performance claim.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_MAX_ITERATIONS = 500
_DEFAULT_BUDGET_SECONDS = 240.0
_SUBPROCESS_TIMEOUT_SECONDS = 60.0
_GIT_TIMEOUT_SECONDS = 30.0
_NO_WINDOW_KW: dict = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform == "win32" else {}
)
CODEX_SID = "019a0000-0000-7000-8000-00000000"
OPENCODE_SID = "ses_bench0000000000000000000000"


class _Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + max(0.0, seconds)

    def exceeded(self) -> bool:
        return time.monotonic() >= self.deadline

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


@dataclass
class Sample:
    name: str
    seconds: list[float | None] = field(default_factory=list)
    partial: bool = False

    def stats(self) -> dict[str, float | int | bool | None]:
        attempted = len(self.seconds)
        valid = [x for x in self.seconds if isinstance(x, (int, float)) and math.isfinite(x)]
        n = len(valid)
        result: dict[str, float | int | bool | None] = {
            "n": n, "attempted": attempted, "partial": bool(self.partial or attempted != n),
        }
        if n == 0:
            result.update({"min_ms": None, "median_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None})
            return result
        xs = sorted(valid)

        def pct(p: float) -> float:
            return xs[min(n - 1, max(0, int(round(p * (n - 1)))))]

        result.update({
            "min_ms": xs[0] * 1000, "median_ms": statistics.median(xs) * 1000,
            "p95_ms": pct(0.95) * 1000, "p99_ms": pct(0.99) * 1000, "max_ms": xs[-1] * 1000,
        })
        return result


def _git(repo: Path, *args: str) -> None:
    argv = ["git", "-c", "user.email=bench@example.com", "-c", "user.name=bench", *args]
    for attempt in range(2):
        try:
            subprocess.run(argv, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=_GIT_TIMEOUT_SECONDS, **_NO_WINDOW_KW)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == 1:
                raise
            time.sleep(0.5)


def _make_repo(tmp: Path, name: str) -> Path:
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _codex_doc(event: str, repo: Path, sid: str, **fields: object) -> str:
    data: dict[str, object] = {
        "session_id": sid, "turn_id": "turn_1", "hook_event_name": event, "cwd": str(repo),
        "model": "gpt-5-codex", "transcript_path": str(repo / "rollout.jsonl"), "permission_mode": "default",
    }
    data.update(fields)
    return json.dumps(data)


def _opencode_doc(event: str, repo: Path, sid: str, **fields: object) -> str:
    data: dict[str, object] = {"agent": "opencode", "event": event, "session_id": sid,
                               "directory": str(repo), "worktree": str(repo)}
    data.update(fields)
    return json.dumps(data)


class _BenchService:
    def __init__(self, home: Path) -> None:
        from openshard.adapters import claude_capture_service as svc

        self.env = {**os.environ, "OPENSHARD_HOME": str(home), "OPENSHARD_CAPTURE_NO_SPAWN": "1"}
        self.env.pop("OPENSHARD_CAPTURE_DISABLE", None)
        self.ready = threading.Event()
        self.box: list = []
        self.thread = threading.Thread(
            target=svc.serve, kwargs={"port": 0, "idle_timeout": 0, "env": self.env,
                                      "ready": self.ready, "server_box": self.box}, daemon=True)
        self.thread.start()
        if not self.ready.wait(10) or not self.box:
            raise RuntimeError("capture service did not start")
        self.server = self.box[0]
        self.port = self.server.port

    def stop(self) -> None:
        self.server.begin_shutdown("bench done")
        self.thread.join(15)


def _wait_for(predicate, timeout: float = 10.0) -> float | None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if predicate():
            return time.perf_counter() - t0
        time.sleep(0.002)
    return None


def _record(repo: Path, sid: str, executor: str) -> dict | None:
    path = repo / ".openshard" / "runs.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        try:
            entry = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if entry and entry.get("executor") == executor and (entry.get("capture") or {}).get("session_id") == sid:
            return entry
    return None


def _openshard_argv() -> list[str]:
    exe = shutil.which("openshard")
    return [exe] if exe else [sys.executable, "-m", "openshard.cli.entrypoint"]


def _timed_post(client, port: int, path: str, raw: str) -> float:
    t0 = time.perf_counter()
    ok = client.post_hook(port, raw.encode("utf-8"), hook_path=path)
    dt = time.perf_counter() - t0
    if not ok:
        raise RuntimeError(f"service rejected POST {path}")
    return dt


def _loop(sample: Sample, iterations: int, budget: _Budget, fn) -> Sample:
    for i in range(iterations):
        if budget.exceeded():
            sample.partial = True
            break
        sample.seconds.append(fn(i))
    return sample


def bench_service(iterations: int, budget: _Budget, *, skip_subprocess: bool) -> tuple[list[Sample], dict]:
    from openshard.adapters import claude_capture_client as client

    samples: list[Sample] = []
    with tempfile.TemporaryDirectory(prefix="openshard-bench-agents-", ignore_cleanup_errors=True) as tmp_s:
        tmp = Path(tmp_s)
        service = _BenchService(tmp / "home")
        port = service.port
        try:
            codex_repo = _make_repo(tmp, "codex")
            oc_repo = _make_repo(tmp, "opencode")
            cpath, opath = client.CODEX_HOOK_PATH, client.OPENCODE_HOOK_PATH

            # --- Codex: blocking POST (what the service adds) -------------------
            sid = CODEX_SID + "0001"
            _timed_post(client, port, cpath, _codex_doc("SessionStart", codex_repo, sid, source="startup"))
            _timed_post(client, port, cpath, _codex_doc("UserPromptSubmit", codex_repo, sid, prompt="warm"))
            service.server.recorder.wait_idle(20)
            samples.append(_loop(Sample("codex: POST UserPromptSubmit (python client)"), iterations, budget,
                                 lambda i: _timed_post(client, port, cpath, _codex_doc(
                                     "UserPromptSubmit", codex_repo, sid, prompt=f"task {i}"))))
            samples.append(_loop(Sample("codex: POST PostToolUse Bash (python client; async hook in Codex)"),
                                 iterations, budget,
                                 lambda i: _timed_post(client, port, cpath, _codex_doc(
                                     "PostToolUse", codex_repo, sid, tool_name="Bash",
                                     tool_input={"command": f"echo {i}"}, tool_response={"output": f"{i}\n"}))))
            patch = "*** Begin Patch\n*** Add File: f.py\n+x = 1\n*** End Patch\n"
            samples.append(_loop(Sample("codex: POST PostToolUse apply_patch (python client)"), iterations, budget,
                                 lambda i: _timed_post(client, port, cpath, _codex_doc(
                                     "PostToolUse", codex_repo, sid, tool_name="apply_patch",
                                     tool_input={"patch": patch}, tool_response={"output": "Done"}))))
            stop = Sample("codex: POST Stop (python client; folds in background)")
            fold = Sample("codex: Stop POST returned -> receipt folded in runs.jsonl")
            for i in range(iterations):
                if budget.exceeded():
                    stop.partial = fold.partial = True
                    break
                service.server.recorder.wait_idle(20)
                before = ((_record(codex_repo, sid, "codex_hooks") or {}).get("capture") or {}).get("turn_count", 0)
                stop.seconds.append(_timed_post(client, port, cpath, _codex_doc("Stop", codex_repo, sid)))
                fold.seconds.append(_wait_for(lambda: (((_record(codex_repo, sid, "codex_hooks") or {})
                                                        .get("capture") or {}).get("turn_count", 0)) > before))
            samples += [stop, fold]

            # --- OpenCode: blocking POST (the plugin's whole cost) --------------
            osid = OPENCODE_SID + "01"
            _timed_post(client, port, opath, _opencode_doc("session.created", oc_repo, osid))
            _timed_post(client, port, opath, _opencode_doc("chat.message", oc_repo, osid, prompt="warm",
                                                           provider_id="anthropic", model_id="claude-sonnet-4-5"))
            service.server.recorder.wait_idle(20)
            samples.append(_loop(Sample("opencode: POST chat.message (python client)"), iterations, budget,
                                 lambda i: _timed_post(client, port, opath, _opencode_doc(
                                     "chat.message", oc_repo, osid, prompt=f"task {i}",
                                     provider_id="anthropic", model_id="claude-sonnet-4-5"))))
            samples.append(_loop(Sample("opencode: POST tool.execute.after bash (python client)"), iterations, budget,
                                 lambda i: _timed_post(client, port, opath, _opencode_doc(
                                     "tool.execute.after", oc_repo, osid, tool="bash", command=f"echo {i}"))))
            samples.append(_loop(Sample("opencode: POST message.updated usage (python client)"), iterations, budget,
                                 lambda i: _timed_post(client, port, opath, _opencode_doc(
                                     "message.updated", oc_repo, osid, message_id=f"msg_{i}", cost=0.001,
                                     provider_id="anthropic", model_id="claude-sonnet-4-5",
                                     tokens={"input": 100, "output": 20, "cache": {"read": 0, "write": 0}}))))
            idle = Sample("opencode: POST session.idle (python client; folds in background)")
            ofold = Sample("opencode: session.idle POST returned -> receipt folded in runs.jsonl")
            for i in range(iterations):
                if budget.exceeded():
                    idle.partial = ofold.partial = True
                    break
                service.server.recorder.wait_idle(20)
                before = ((_record(oc_repo, osid, "opencode_plugin") or {}).get("capture") or {}).get("turn_count", 0)
                idle.seconds.append(_timed_post(client, port, opath, _opencode_doc("session.idle", oc_repo, osid)))
                ofold.seconds.append(_wait_for(lambda: (((_record(oc_repo, osid, "opencode_plugin") or {})
                                                         .get("capture") or {}).get("turn_count", 0)) > before))
            samples += [idle, ofold]

            node = shutil.which("node")
            if node and not budget.exceeded():
                s = Sample("opencode: POST tool.execute.after via node fetch (plugin runtime stand-in)")
                body = _opencode_doc("tool.execute.after", oc_repo, osid, tool="bash", command="echo node")
                script = f"""
const body = {json.dumps(body)};
(async () => {{
  const out = [];
  for (let i = 0; i < {iterations}; i++) {{
    const t0 = process.hrtime.bigint();
    const r = await fetch("http://127.0.0.1:{port}{opath}", {{
      method: "POST", headers: {{"content-type": "application/json"}}, body }});
    await r.text();
    out.push(Number(process.hrtime.bigint() - t0) / 1e9);
  }}
  console.log(JSON.stringify(out));
}})();
"""
                try:
                    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                                            timeout=min(60.0, max(5.0, budget.remaining())))
                    s.seconds = json.loads(result.stdout.strip())
                except (subprocess.TimeoutExpired, ValueError):
                    s.seconds = []
                samples.append(s)

            # --- Codex: the real command hook as a subprocess -------------------
            if not skip_subprocess and not budget.exceeded():
                argv = _openshard_argv() + ["hooks", "codex"]
                env = dict(service.env)
                assert env.get("OPENSHARD_CAPTURE_NO_SPAWN")
                warm = _codex_doc("UserPromptSubmit", codex_repo, sid, prompt="warm")
                subprocess.run(argv, input=warm, text=True, capture_output=True, env=env,
                               timeout=_SUBPROCESS_TIMEOUT_SECONDS)

                def run_hook(raw: str) -> float:
                    t0 = time.perf_counter()
                    subprocess.run(argv, input=raw, text=True, capture_output=True, env=env,
                                   timeout=_SUBPROCESS_TIMEOUT_SECONDS)
                    return time.perf_counter() - t0

                sub_iters = max(3, min(iterations, 20))
                samples.append(_loop(Sample("codex: `openshard hooks codex` UserPromptSubmit (real subprocess)"),
                                     sub_iters, budget, lambda i: run_hook(_codex_doc(
                                         "UserPromptSubmit", codex_repo, sid, prompt=f"sub {i}"))))
                samples.append(_loop(Sample("codex: `openshard hooks codex` Stop (real subprocess, synchronous)"),
                                     sub_iters, budget, lambda i: run_hook(_codex_doc("Stop", codex_repo, sid))))
                samples.append(_loop(Sample("codex: `openshard hooks codex --no-spawn` SessionEnd (real subprocess)"),
                                     sub_iters, budget, lambda i: run_hook(_codex_doc(
                                         "SessionEnd", codex_repo, sid))))

            service.server.recorder.wait_idle(30)
            timing = service.server.recorder.timings.summary()
        finally:
            service.stop()
    server_side = {
        "description": "server-side blocking time per request (CaptureRecorder.timings) across every "
                       "/hooks/codex and /hooks/opencode request above",
        **timing,
    }
    return samples, server_side


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iterations", type=int, default=100, help=f"samples per scenario (cap {_MAX_ITERATIONS})")
    parser.add_argument("--skip-subprocess", action="store_true", help="skip the real `openshard hooks codex` runs")
    parser.add_argument("--max-seconds", type=float, default=_DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    budget = _Budget(args.max_seconds)
    iterations = max(1, min(int(args.iterations), _MAX_ITERATIONS))
    samples, server_side = bench_service(iterations, budget, skip_subprocess=args.skip_subprocess)
    if budget.exceeded():
        print("(wall-clock budget exceeded; samples above are partial)", file=sys.stderr)

    if args.json:
        payload = {s.name: s.stats() for s in samples}
        payload["server-side blocking (aggregate)"] = server_side
        print(json.dumps(payload, indent=2, allow_nan=False))
        return

    rows = [(s.name + (" [partial]" if s.stats().get("partial") else ""), s.stats()) for s in samples]
    name_w = max(len(name) for name, _ in rows) + 2
    header = f"{'scenario':<{name_w}}{'n':>5}{'median':>10}{'p95':>10}{'p99':>10}{'max':>10}"
    print(header)
    print("-" * len(header))

    def fmt(v: float | None) -> str:
        return f"{v:>9.1f}m" if v is not None else f"{'-':>10}"

    for name, st in rows:
        print(f"{name:<{name_w}}{st['n']:>5}{fmt(st['median_ms'])}{fmt(st['p95_ms'])}{fmt(st['p99_ms'])}{fmt(st['max_ms'])}")
    print()
    print(f"server-side blocking (aggregate): n={server_side.get('n')} p50={server_side.get('p50_ms')}ms "
          f"p95={server_side.get('p95_ms')}ms max={server_side.get('max_ms')}ms")


if __name__ == "__main__":
    main()
