#!/usr/bin/env python
"""Latency benchmarks for OpenShard's Claude Code capture path (PR7, PR9.5).

Run from the repo root:

    python scripts/bench_claude_capture.py
    python scripts/bench_claude_capture.py --iterations 30 --json
    python scripts/bench_claude_capture.py --service-only     # just the PR9.5 hot path

No live Claude API calls, no network access beyond 127.0.0.1, no new
dependency (stdlib ``time``/``statistics``/``subprocess`` only). Safe to run
repeatedly on a laptop; each run works in its own temp directory, starts its
own capture service on an ephemeral port under a temp ``OPENSHARD_HOME``,
and cleans up after itself -- the developer's real service is never touched.

Layers measured, because they answer different questions:

* **service (blocking path)** -- what Claude Code actually waits on since
  PR9.5: an HTTP POST of the hook payload to the warm local capture
  service, timed end to end from a client (Python, and ``node`` ``fetch``
  when a ``node`` binary is on PATH, which is the closest stand-in for
  Claude Code's own HTTP client). Also reports the service's own
  server-side timing and the time from a ``Stop`` POST returning to the
  folded receipt being visible in ``runs.jsonl``.
* **subprocess** -- the command-form entrypoints exactly as Claude Code
  invokes them: ``openshard hooks claude`` (now only ``SessionStart``,
  forwarding to the service) and the status line, plus the *fallback*
  in-process path (``OPENSHARD_CAPTURE_DISABLE=1``) that is what every
  hook cost before PR9.5. Dominated by interpreter start-up.
* **in-process** -- the fold/staging functions and the JSONL store called
  directly, isolating OpenShard's own logic cost (this is now the
  *background* worker's cost, not the user's).

Do not compare these numbers across machines/CI runners as a universal
performance claim -- they are a regression signal for maintainers on their
own box, not a benchmark of OpenShard in the abstract.

Safety
------
This script starts real (in-process, ephemeral-port) services and spawns
real ``git``/``openshard`` subprocesses, so it is written defensively:

* Every scenario's iteration count is clamped to ``_MAX_ITERATIONS``
  regardless of what ``--iterations``/``--service-iterations``/
  ``--in-process-iterations`` request.
* A single wall-clock budget (``--max-seconds``, default
  ``_DEFAULT_BUDGET_SECONDS``) is checked between scenarios and inside the
  git-repo-creating loops; once exceeded, the current scenario stops early
  (partial samples are kept, not discarded) instead of continuing.
* Every ``git`` and ``openshard`` subprocess call has an explicit timeout;
  none can hang the script indefinitely.
* Every capture service this script starts runs in a background *thread* of
  this same process (``OPENSHARD_CAPTURE_NO_SPAWN=1`` in its env, and the
  in-process fallback scenarios additionally set
  ``OPENSHARD_CAPTURE_DISABLE=1``), never as a detached child process --
  when the script exits, for any reason, no service or listener survives it
  (daemon threads die with the interpreter; there is nothing to leak).
* ``service.stop()`` is always called from a ``finally``, with its own
  bounded join.
* Every git subprocess call passes ``CREATE_NO_WINDOW`` on Windows (the
  parent already has a console here, so this is pure defense-in-depth,
  matching the same flag now used on the capture path's own git calls).
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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SID_PREFIX = "bench0000-0000-4000-8000-"

_MAX_ITERATIONS = 200  # hard cap regardless of CLI input
_DEFAULT_BUDGET_SECONDS = 240.0
_GIT_TIMEOUT_SECONDS = 15.0
_SUBPROCESS_TIMEOUT_SECONDS = 10.0

# See adapters/claude_code_import.py's matching comment: harmless here (this
# script's own process already has a console) but keeps every git.exe
# invocation in this file consistent with the capture path's own fix.
_NO_WINDOW_KW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class _Budget:
    """A single wall-clock deadline shared across every scenario in one run."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + max(0.0, seconds)

    def exceeded(self) -> bool:
        return time.monotonic() >= self.deadline

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


def _clamp_iterations(n: int) -> int:
    return max(1, min(int(n), _MAX_ITERATIONS))


def _sid(n: int) -> str:
    return f"{SID_PREFIX}{n:012d}"


@dataclass
class Sample:
    """One scenario's collected round-trip times.

    ``seconds`` holds ``None`` for an individual attempt that never
    completed within its own wait bound (currently only ``_wait_for``'s
    timeout, used by the fold-wait scenario) -- never ``float("nan")``,
    which previously corrupted this sample's whole statistics block (NaN
    compares false against everything, so sorting a list containing it
    silently scrambles order, and once one NaN enters a min/median/p95/p99/
    max computation the result is NaN too -- exactly what produced the
    ``"p99_ms": NaN`` seen in a real run). ``partial`` is set by the calling
    loop when it stopped early (wall-clock budget exceeded), independent of
    whether every collected sample is individually valid.
    """

    name: str
    seconds: list[float | None] = field(default_factory=list)
    partial: bool = False

    def stats(self) -> dict[str, float | int | bool | None]:
        attempted = len(self.seconds)
        valid = [x for x in self.seconds if isinstance(x, (int, float)) and math.isfinite(x)]
        n = len(valid)
        timed_out = attempted - n
        result: dict[str, float | int | bool | None] = {
            "n": n,
            "attempted": attempted,
            "partial": bool(self.partial or timed_out),
        }
        if timed_out:
            result["timed_out"] = timed_out
        if n == 0:
            result.update({"min_ms": None, "median_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None})
            return result

        xs = sorted(valid)

        def pct(p: float) -> float:
            idx = min(n - 1, max(0, int(round(p * (n - 1)))))
            return xs[idx]

        result.update({
            "min_ms": xs[0] * 1000,
            "median_ms": statistics.median(xs) * 1000,
            "p95_ms": pct(0.95) * 1000,
            "p99_ms": pct(0.99) * 1000,
            "max_ms": xs[-1] * 1000,
        })
        return result


def _git(repo: Path, *args: str) -> None:
    # This machine occasionally has a git.exe invocation crash under disk/AV
    # contention (STATUS_IN_PAGE_ERROR) when many repos are created back to
    # back -- unrelated to the capture path being measured. One retry after
    # a short pause is enough to ride out the transient failure. An explicit
    # timeout means a hung (not crashed) git can never hang this script.
    argv = ["git", "-c", "user.email=bench@example.com", "-c", "user.name=bench", *args]
    for attempt in range(2):
        try:
            subprocess.run(
                argv, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_GIT_TIMEOUT_SECONDS, **_NO_WINDOW_KW,
            )
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
    exe = shutil.which("openshard")
    if exe:
        return [exe]
    return [sys.executable, "-m", "openshard.cli.entrypoint"]


def _assert_safe_env(env: dict[str, str]) -> None:
    """Refuse to spawn a real ``openshard`` subprocess unless it cannot start a real service.

    Defense-in-depth for this script only: every subprocess invocation here
    must carry ``OPENSHARD_CAPTURE_NO_SPAWN`` (this run's in-thread service
    is the only one that may ever run) or ``OPENSHARD_CAPTURE_DISABLE``
    (the in-process fallback scenarios). If neither is set, that is a bug in
    this script, not a real Claude Code invocation -- fail loudly rather
    than risk spawning a detached service.
    """
    if not (env.get("OPENSHARD_CAPTURE_NO_SPAWN") or env.get("OPENSHARD_CAPTURE_DISABLE")):
        raise AssertionError("refusing to spawn: env lacks OPENSHARD_CAPTURE_NO_SPAWN/DISABLE")


def _run_subprocess(argv: list[str], stdin_text: str, env: dict[str, str]) -> float:
    _assert_safe_env(env)
    t0 = time.perf_counter()
    subprocess.run(
        argv, input=stdin_text, text=True, capture_output=True, env=env,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# PR9.5: the warm service's blocking path
# ---------------------------------------------------------------------------


class _BenchService:
    """An in-process capture service on an ephemeral port under a temp OPENSHARD_HOME."""

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
    """Seconds until *predicate* is true, or ``None`` on timeout -- never NaN."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if predicate():
            return time.perf_counter() - t0
        time.sleep(0.002)
    return None


def _record(repo: Path, session_id: str) -> dict | None:
    path = repo / ".openshard" / "runs.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (entry.get("capture") or {}).get("session_id") == session_id:
            return entry
    return None


def bench_service(iterations: int, budget: _Budget) -> tuple[list[Sample], dict]:
    from openshard.adapters import claude_capture_client as client

    iterations = _clamp_iterations(iterations)
    samples: list[Sample] = []
    with tempfile.TemporaryDirectory(prefix="openshard-bench-svc-", ignore_cleanup_errors=True) as tmp_s:
        tmp = Path(tmp_s)
        service = _BenchService(tmp / "home")
        port = service.port
        try:
            repo = _make_repo(tmp, "svc")
            pd = str(repo)

            def post(name: str, raw: str) -> float:
                t0 = time.perf_counter()
                ok = client.post_hook(port, raw.encode("utf-8"), project_dir=pd)
                dt = time.perf_counter() - t0
                if not ok:
                    raise RuntimeError(f"service rejected {name}")
                return dt

            # Warm up: one full session so caches (repo root, sessions dir) are hot.
            for ev, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "warm"}),
                              ("Stop", {}), ("SessionEnd", {"reason": "other"})):
                post(ev, _payload(ev, repo, _sid(0), **extra))
            service.server.recorder.wait_idle(20)

            sid = _sid(1)
            post("SessionStart", _payload("SessionStart", repo, sid, source="startup"))

            s = Sample("service: POST UserPromptSubmit (blocking, python client)")
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                s.seconds.append(post("UserPromptSubmit", _payload("UserPromptSubmit", repo, sid, prompt=f"task {i}")))
            samples.append(s)

            s = Sample("service: POST PostToolUse Bash (blocking, python client)")
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                s.seconds.append(post("PostToolUse", _payload(
                    "PostToolUse", repo, sid, tool_name="Bash", tool_input={"command": f"echo {i}"},
                    tool_response={"stdout": f"{i}\n", "stderr": ""})))
            samples.append(s)

            s = Sample("service: POST PostToolUse Write (blocking, python client)")
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                (repo / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
                s.seconds.append(post("PostToolUse", _payload(
                    "PostToolUse", repo, sid, tool_name="Write", tool_input={"file_path": str(repo / f"f{i}.py")})))
            samples.append(s)

            s = Sample("service: POST PostToolUse with 1MB tool_response (blocking)")
            big = "x" * (1024 * 1024)
            for i in range(max(3, iterations // 4)):
                if budget.exceeded():
                    s.partial = True
                    break
                s.seconds.append(post("PostToolUse", _payload(
                    "PostToolUse", repo, sid, tool_name="Bash", tool_input={"command": "cat big"},
                    tool_response={"stdout": big})))
            samples.append(s)

            s = Sample("service: POST Stop (blocking, python client; folds in background)")
            fold = Sample("service: Stop POST returned -> receipt folded in runs.jsonl")
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    fold.partial = True
                    break
                service.server.recorder.wait_idle(20)
                before = (_record(repo, sid) or {}).get("capture", {}).get("turn_count", 0)
                s.seconds.append(post("Stop", _payload("Stop", repo, sid)))
                fold.seconds.append(_wait_for(
                    lambda: ((_record(repo, sid) or {}).get("capture", {}).get("turn_count", 0)) > before))
            samples.append(s)
            samples.append(fold)

            s = Sample("service: POST status line (blocking, python client)")
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                t0 = time.perf_counter()
                client.post_status(port, _status_payload(repo, sid, 1000 + i).encode("utf-8"), project_dir=pd)
                s.seconds.append(time.perf_counter() - t0)
            samples.append(s)

            # Reuses one repo (fresh session ids) rather than making a new git
            # repo per sample -- SessionEnd's cost is the service round trip,
            # not repeated `git init`, and creating dozens of throw-away repos
            # is itself slow/flaky on a loaded Windows box (antivirus, disk).
            s = Sample("service: POST SessionEnd (blocking, python client)")
            end_iterations = min(iterations, 15)
            for i in range(end_iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                sid = _sid(500 + i)
                client.post_hook(port, _payload("UserPromptSubmit", repo, sid, prompt="t").encode(), project_dir=pd)
                service.server.recorder.wait_idle(20)
                t0 = time.perf_counter()
                client.post_hook(port, _payload("SessionEnd", repo, sid, reason="other").encode(), project_dir=pd)
                s.seconds.append(time.perf_counter() - t0)
            samples.append(s)

            node = shutil.which("node")
            if node and not budget.exceeded():
                s = Sample("service: POST Stop via node fetch (Claude Code's HTTP client stand-in)")
                script = f"""
const body = {json.dumps(_payload("Stop", repo, sid))};
(async () => {{
  const out = [];
  for (let i = 0; i < {iterations}; i++) {{
    const t0 = process.hrtime.bigint();
    const r = await fetch("http://127.0.0.1:{port}{client.HOOK_PATH}", {{
      method: "POST", headers: {{"content-type": "application/json", "{client.PROJECT_DIR_HEADER}": {json.dumps(pd)}}}, body }});
    await r.text();
    out.push(Number(process.hrtime.bigint() - t0) / 1e9);
  }}
  console.log(JSON.stringify(out));
}})();
"""
                try:
                    result = subprocess.run(
                        [node, "-e", script], capture_output=True, text=True,
                        timeout=min(60.0, max(5.0, budget.remaining())),
                    )
                    s.seconds = json.loads(result.stdout.strip())
                except (subprocess.TimeoutExpired, ValueError):
                    s.seconds = []
                samples.append(s)

            service.server.recorder.wait_idle(30)
            timing = service.server.recorder.timings.summary()
            print(f"service server-side blocking (last {timing.get('window')} of {timing.get('n')} requests): "
                  f"p50 {timing.get('p50_ms')}ms p95 {timing.get('p95_ms')}ms max {timing.get('max_ms')}ms",
                  file=sys.stderr)
        finally:
            service.stop()
    # Not a Sample: this is the *service's own* running aggregate over every
    # /hooks and /status request across all scenarios above (see
    # CaptureRecorder.timings), not one client-side measurement. Shoehorning
    # it into a Sample previously reported "n=1" (one synthetic data point --
    # the p50 value duplicated into min/median/p95/p99/max), which looked
    # like a single request's timing rather than what it actually is: a
    # summary already computed server-side over many requests. Returned
    # separately so callers can label and print it honestly instead of
    # mixing its differently-shaped, already-aggregated fields into the
    # per-scenario table.
    server_side_summary = {
        "description": (
            "computed server-side (CaptureRecorder.timings) across every /hooks and "
            "/status request in the scenarios above; 'n' is the true total request "
            "count, 'window' is how many of the most recent requests the ring "
            "buffer retains for the percentile calc below"
        ),
        **timing,
    }
    return samples, server_side_summary


# ---------------------------------------------------------------------------
# Command-form entrypoints (real console script)
# ---------------------------------------------------------------------------


def _warmup(argv: list[str], stdin_text: str, env: dict[str, str]) -> None:
    _assert_safe_env(env)
    subprocess.run(argv, input=stdin_text, text=True, capture_output=True, env=env,
                    timeout=_SUBPROCESS_TIMEOUT_SECONDS)


def bench_subprocess(iterations: int, budget: _Budget) -> list[Sample]:
    iterations = _clamp_iterations(iterations)
    base_argv = _openshard_argv()
    samples: list[Sample] = []

    with tempfile.TemporaryDirectory(prefix="openshard-bench-", ignore_cleanup_errors=True) as tmp_s:
        tmp = Path(tmp_s)
        service = _BenchService(tmp / "home")
        try:
            # 1) SessionStart -- the only remaining command hook, forwarding to a running service.
            s = Sample("subprocess: SessionStart command hook -> service (sync, once per session)")
            repo = _make_repo(tmp, "start")
            env = {**service.env, "CLAUDE_PROJECT_DIR": str(repo)}
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                payload = _payload("SessionStart", repo, _sid(i), source="startup")
                s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
            samples.append(s)

            # 2) status line command -> service
            s = Sample("subprocess: claude-status command -> service (status line render)")
            repo = _make_repo(tmp, "status")
            env = {**service.env, "CLAUDE_PROJECT_DIR": str(repo)}
            _warmup([*base_argv, "hooks", "claude"], _payload("UserPromptSubmit", repo, _sid(9002), prompt="task"), env)
            for i in range(iterations):
                if budget.exceeded():
                    s.partial = True
                    break
                payload = _status_payload(repo, _sid(9002), tokens=1000 + i)
                s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude-status"], payload, env))
            samples.append(s)
        finally:
            service.stop()

        # 3) The in-process fallback (what every hook cost before PR9.5).
        disabled = {**os.environ, "OPENSHARD_CAPTURE_DISABLE": "1", "OPENSHARD_HOME": str(tmp / "home2")}

        s = Sample("subprocess fallback: UserPromptSubmit in-process (OPENSHARD_CAPTURE_DISABLE)")
        repo = _make_repo(tmp, "prompt")
        env = {**disabled, "CLAUDE_PROJECT_DIR": str(repo)}
        _warmup([*base_argv, "hooks", "claude"], _payload("SessionStart", repo, _sid(9000), source="startup"), env)
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            payload = _payload("UserPromptSubmit", repo, _sid(9000), prompt=f"task {i}")
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        # Reuses one repo (fresh session ids per iteration) rather than a new
        # git repo per sample -- see the matching note on the service
        # scenarios above; the same disk/AV-contention risk applies here.
        s = Sample("subprocess fallback: Stop in-process (OPENSHARD_CAPTURE_DISABLE)")
        repo = _make_repo(tmp, "stop-fallback")
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            env = {**disabled, "CLAUDE_PROJECT_DIR": str(repo)}
            _warmup([*base_argv, "hooks", "claude"], _payload("UserPromptSubmit", repo, _sid(i), prompt="task"), env)
            payload = _payload("Stop", repo, _sid(i))
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude"], payload, env))
        samples.append(s)

        s = Sample("subprocess fallback: claude-status in-process (OPENSHARD_CAPTURE_DISABLE)")
        repo = _make_repo(tmp, "status2")
        env = {**disabled, "CLAUDE_PROJECT_DIR": str(repo)}
        _warmup([*base_argv, "hooks", "claude"], _payload("UserPromptSubmit", repo, _sid(9003), prompt="task"), env)
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            payload = _status_payload(repo, _sid(9003), tokens=1000 + i)
            s.seconds.append(_run_subprocess([*base_argv, "hooks", "claude-status"], payload, env))
        samples.append(s)

    return samples


# ---------------------------------------------------------------------------
# In-process logic (now the background worker's cost)
# ---------------------------------------------------------------------------


def bench_in_process(iterations: int, budget: _Budget) -> list[Sample]:
    from openshard.adapters.claude_hooks import handle_claude_hook, handle_claude_status

    iterations = _clamp_iterations(iterations)
    samples: list[Sample] = []

    with tempfile.TemporaryDirectory(prefix="openshard-bench-inproc-", ignore_cleanup_errors=True) as tmp_s:
        tmp = Path(tmp_s)
        repo = _make_repo(tmp, "inproc")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
        env.setdefault("OPENSHARD_CAPTURE_DISABLE", "1")  # never contact a real service from this scenario

        s = Sample("in-process: handle_claude_hook(UserPromptSubmit)")
        handle_claude_hook(json.loads(_payload("SessionStart", repo, _sid(1), source="startup")), env=env)
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            data = json.loads(_payload("UserPromptSubmit", repo, _sid(1), prompt=f"t{i}"))
            t0 = time.perf_counter()
            handle_claude_hook(data, env=env)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: handle_claude_hook(PostToolUse, no fold)")
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            data = json.loads(_payload("PostToolUse", repo, _sid(1), tool_name="Bash", tool_input={"command": "ls"}))
            t0 = time.perf_counter()
            handle_claude_hook(data, env=env)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: handle_claude_status (steady state, no fold)")
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            data = json.loads(_status_payload(repo, _sid(1), tokens=2000 + i))
            t0 = time.perf_counter()
            handle_claude_status(data, env=env)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        # One repo, fresh session ids -- each Stop still folds (a real git
        # diff against that session's own snapshotted HEAD), but this no
        # longer creates a new git repository per iteration.
        s = Sample("in-process: handle_claude_hook(Stop, folds) [background worker cost]")
        repo_stop = _make_repo(tmp, "inproc-stop")
        env_stop = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo_stop)}
        env_stop.setdefault("OPENSHARD_CAPTURE_DISABLE", "1")
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            handle_claude_hook(json.loads(_payload("UserPromptSubmit", repo_stop, _sid(100 + i), prompt="t")), env=env_stop)
            data = json.loads(_payload("Stop", repo_stop, _sid(100 + i)))
            t0 = time.perf_counter()
            handle_claude_hook(data, env=env_stop)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

    return samples


def bench_jsonl_store(iterations: int, budget: _Budget) -> list[Sample]:
    from openshard.history.jsonl_store import append_jsonl, upsert_jsonl

    iterations = _clamp_iterations(iterations)
    samples: list[Sample] = []
    with tempfile.TemporaryDirectory(prefix="openshard-bench-jsonl-", ignore_cleanup_errors=True) as tmp_s:
        path = Path(tmp_s) / "runs.jsonl"

        s = Sample("in-process: append_jsonl")
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            t0 = time.perf_counter()
            append_jsonl(path, {"i": i, "task": f"task {i}"})
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

        s = Sample("in-process: upsert_jsonl (replace, growing file)")
        for i in range(iterations):
            if budget.exceeded():
                s.partial = True
                break
            t0 = time.perf_counter()
            upsert_jsonl(path, {"i": 0, "task": f"updated {i}"}, lambda e: e.get("i") == 0)
            s.seconds.append(time.perf_counter() - t0)
        samples.append(s)

    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iterations", type=int, default=20,
                         help=f"samples per subprocess scenario (default: 20, hard cap {_MAX_ITERATIONS})")
    parser.add_argument("--service-iterations", type=int, default=100,
                         help=f"samples per service scenario (default: 100, hard cap {_MAX_ITERATIONS})")
    parser.add_argument("--in-process-iterations", type=int, default=200,
                         help=f"samples per in-process scenario (default: 200, hard cap {_MAX_ITERATIONS})")
    parser.add_argument("--skip-subprocess", action="store_true", help="skip the (slower) real subprocess scenarios")
    parser.add_argument("--service-only", action="store_true", help="only the PR9.5 service blocking-path scenarios")
    parser.add_argument("--max-seconds", type=float, default=_DEFAULT_BUDGET_SECONDS,
                         help=f"overall wall-clock budget; scenarios stop early past it (default: {_DEFAULT_BUDGET_SECONDS:.0f}s)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    args = parser.parse_args()

    budget = _Budget(args.max_seconds)
    all_samples: list[Sample] = []
    service_samples, server_side_summary = bench_service(_clamp_iterations(args.service_iterations), budget)
    all_samples += service_samples
    if not args.service_only and not budget.exceeded():
        if not args.skip_subprocess:
            all_samples += bench_subprocess(_clamp_iterations(args.iterations), budget)
        if not budget.exceeded():
            all_samples += bench_in_process(_clamp_iterations(args.in_process_iterations), budget)
        if not budget.exceeded():
            all_samples += bench_jsonl_store(_clamp_iterations(args.in_process_iterations), budget)
    if budget.exceeded():
        print("(wall-clock budget exceeded; remaining scenarios skipped, samples above are partial)", file=sys.stderr)

    server_side_key = "service: server-side blocking time per request (aggregate across every scenario above)"

    if args.json:
        payload = {s.name: s.stats() for s in all_samples}
        payload[server_side_key] = server_side_summary
        # allow_nan=False turns any future accidental NaN/Infinity into an
        # immediate, loud ValueError here rather than silently emitting
        # invalid JSON tokens (Python's json module allows them by default;
        # RFC 8259 does not) -- defense-in-depth on top of Sample.stats()
        # and _wait_for() never producing one in the first place.
        print(json.dumps(payload, indent=2, allow_nan=False))
        return

    rows = []
    for s in all_samples:
        st = s.stats()
        name = s.name + (" [partial]" if st.get("partial") else "")
        rows.append((name, st))

    name_w = max(len(name) for name, _ in rows) + 2
    header = f"{'scenario':<{name_w}}{'n':>5}{'attempted':>10}{'median':>10}{'p95':>10}{'p99':>10}{'max':>10}"
    print(header)
    print("-" * len(header))

    def _fmt_ms(value: float | None) -> str:
        return f"{value:>9.1f}m" if value is not None else f"{'-':>10}"

    for name, st in rows:
        print(
            f"{name:<{name_w}}{st['n']:>5}{st['attempted']:>10}"
            f"{_fmt_ms(st['median_ms'])}{_fmt_ms(st['p95_ms'])}{_fmt_ms(st['p99_ms'])}{_fmt_ms(st['max_ms'])}"
        )

    print()
    print(f"{server_side_key}:")
    print(
        f"  n={server_side_summary.get('n')} total requests, window={server_side_summary.get('window')} most recent: "
        f"p50={server_side_summary.get('p50_ms')}ms p95={server_side_summary.get('p95_ms')}ms "
        f"max={server_side_summary.get('max_ms')}ms"
    )


if __name__ == "__main__":
    main()
