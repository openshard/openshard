"""Run a small set of representative coding tasks through `openshard osn run` and summarise what happened.

Usage::

    python -m evals.osn_acceptance.run_acceptance --repo C:/path/to/fixture [--tasks tasks.json] [--only id,id]

Each task runs in the given repository (reset between tasks with
``git checkout -- . && git clean -fd``, keeping ``.openshard/`` so the
Receipts stay), as ``openshard osn run <task> --json [args]``. The result
object OSN prints is kept whole under ``results/<timestamp>/<task id>.json``
and summarised in one table: status, attempts, turns, actions, writes,
verification, cost (as the provider reported it, or unknown), wall time,
Receipt id, and whether the task's expectation held.

This is a benchmark, not a product feature: nothing in the ``openshard``
package imports it, it only drives the CLI. One run is an anecdote; repeat
runs before concluding anything (see README.md).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_TASKS = HERE / "tasks.json"


def load_tasks(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        raise SystemExit(f"{path}: expected a list of tasks")
    return [t for t in tasks if isinstance(t, dict) and t.get("id") and t.get("task")]


def reset_repo(repo: Path) -> None:
    """Back to the committed state, keeping .openshard/ (Receipts, checkpoints) and untracked ignored files."""
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fd", "-e", ".openshard"], cwd=repo, check=True, capture_output=True)


def osn_run_command(task: dict[str, Any]) -> list[str]:
    argv = [sys.executable, "-c", "from openshard.cli.main import cli; cli()", "osn", "run", str(task["task"]), "--json"]
    verify = task.get("verify_cmd")
    if isinstance(verify, str) and verify.strip():
        argv += ["--verify-cmd", verify]
    argv += [str(a) for a in (task.get("args") or [])]
    return argv


def run_task(repo: Path, task: dict[str, Any], *, timeout: float = 1800.0) -> dict[str, Any]:
    reset_repo(repo)
    t0 = time.monotonic()
    proc = subprocess.run(osn_run_command(task), cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    wall = round(time.monotonic() - t0, 1)
    final: dict[str, Any] | None = None
    text = proc.stdout.strip()
    if text:
        start = text.find("{")
        if start >= 0:
            try:
                final = json.loads(text[start:])
            except ValueError:
                final = None
    return {"task": task, "exit_code": proc.returncode, "wall_seconds": wall, "result": final,
            "stderr_tail": proc.stderr[-2000:] if proc.stderr else ""}


def _cost(result: dict[str, Any] | None) -> tuple[float | None, str]:
    """Sum of the model calls' reported costs; unknown when any call has none."""
    calls = (result or {}).get("model_calls") or []
    if not isinstance(calls, list) or not calls:
        return None, "unknown"
    costs: list[float] = []
    for c in calls:
        value = c.get("cost_usd") if isinstance(c, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None, "unknown"
        costs.append(float(value))
    if not costs:
        return None, "unknown"
    sources = {str(c.get("cost_source") or "") for c in calls if isinstance(c, dict)}
    return round(float(sum(costs)), 4), (next(iter(sources)) if len(sources) == 1 else "mixed")


def check_expectation(expect: dict[str, Any] | None, result: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether the run met the task's stated expectation, with the first reason it did not."""
    if not expect:
        return True, "no expectation"
    if result is None:
        return False, "no result object"
    summary_raw = result.get("action_summary")
    summary: dict[str, Any] = summary_raw if isinstance(summary_raw, dict) else {}
    checks: list[tuple[bool, str]] = []
    if "status" in expect:
        checks.append((result.get("status") == expect["status"], f"status {result.get('status')!r} != {expect['status']!r}"))
    if "status_in" in expect:
        allowed = [str(s) for s in expect["status_in"]]
        checks.append((result.get("status") in allowed, f"status {result.get('status')!r} not in {allowed}"))
    if "max_attempts" in expect:
        checks.append((int(result.get("attempts") or 0) <= int(expect["max_attempts"]),
                       f"attempts {result.get('attempts')} > {expect['max_attempts']}"))
    if "changed_at_least" in expect:
        n = len(result.get("changed_files") or [])
        checks.append((n >= int(expect["changed_at_least"]), f"changed {n} < {expect['changed_at_least']}"))
    if "writes_applied" in expect:
        n = int(summary.get("writes_applied") or 0)
        checks.append((n == int(expect["writes_applied"]), f"writes_applied {n} != {expect['writes_applied']}"))
    for ok, why in checks:
        if not ok:
            return False, why
    return True, "met"


def summarise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        result_raw = row.get("result")
        result: dict[str, Any] = result_raw if isinstance(result_raw, dict) else {}
        task = row["task"]
        summary_raw = result.get("action_summary")
        summary: dict[str, Any] = summary_raw if isinstance(summary_raw, dict) else {}
        cost, cost_source = _cost(result or None)
        met, why = check_expectation(task.get("expect"), result or None)
        out.append({
            "id": task["id"], "kind": task.get("kind"),
            "status": result.get("status") or f"no result (exit {row.get('exit_code')})",
            "verification": result.get("verification_state"),
            "attempts": result.get("attempts"), "turns": result.get("turns"),
            "actions": summary.get("actions"), "writes": summary.get("writes_applied"),
            "cost_usd": cost, "cost_source": cost_source, "wall_seconds": row.get("wall_seconds"),
            "receipt_id": result.get("receipt_id"), "expectation": why if met else f"NOT met: {why}",
        })
    return out


def render_table(summary: list[dict[str, Any]]) -> str:
    head = "| task | status | verification | attempts | turns | actions | writes | cost | wall s | expectation |"
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    lines = [head, sep]
    for s in summary:
        cost = f"${s['cost_usd']:.4f} ({s['cost_source']})" if isinstance(s.get("cost_usd"), float) else "unknown"
        lines.append(f"| {s['id']} | {s['status']} | {s['verification']} | {s['attempts']} | {s['turns']} | "
                     f"{s['actions']} | {s['writes']} | {cost} | {s['wall_seconds']} | {s['expectation']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="Fixture repository to run the tasks in (a git repository).")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--only", default="", help="Comma-separated task ids to run.")
    parser.add_argument("--results", default=str(HERE / "results"))
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} is not a git repository")
    tasks = load_tasks(Path(args.tasks))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        tasks = [t for t in tasks if t["id"] in wanted]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.results) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for task in tasks:
        print(f"== {task['id']} ({task.get('kind')})", flush=True)
        row = run_task(repo, task, timeout=args.timeout)
        rows.append(row)
        (out_dir / f"{task['id']}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
        s = summarise([row])[0]
        print(f"   {s['status']} · verification {s['verification']} · {s['attempts']} attempt(s) · {s['turns']} turns · "
              f"cost {s['cost_usd']} ({s['cost_source']}) · {s['wall_seconds']}s · {s['expectation']}", flush=True)
    summary = summarise(rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    table = render_table(summary)
    (out_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    print()
    print(table)
    print(f"\nResults: {out_dir}")
    return 0 if not any(str(s["expectation"]).startswith("NOT met") for s in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
