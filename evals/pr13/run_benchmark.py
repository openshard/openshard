"""Command line for the PR13 benchmark.

    python -m evals.pr13.run_benchmark --scenario 1_previously_failed_approach \
        --model <claude model id or alias> [--out evals/pr13/results]

Runs: preflight -> build/clone source at the pinned commit -> burn-in in the
treatment workspace (real Claude Code, real OpenShard hooks) -> external
verification -> reset code keeping .openshard/ -> control arm A and
treatment arm B -> comparison.json / comparison.md.

Exit codes: 0 completed, 1 completed with validity errors, 2 aborted
(``benchmark.json`` names the reason), 3 bad arguments.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.pr13.benchmark.errors import BenchmarkError
from evals.pr13.benchmark.runner import BenchmarkOptions, run_benchmark

PR13_ROOT = Path(__file__).resolve().parent
SCENARIOS_DIR = PR13_ROOT / "scenarios"
DEFAULT_OUT = PR13_ROOT / "results"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals.pr13.run_benchmark", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", required=True, help="scenario id (directory under evals/pr13/scenarios) or a path")
    p.add_argument("--model", required=True, help="Claude model id or alias passed to `claude --model` (recorded verbatim)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"results root (default: {DEFAULT_OUT})")
    p.add_argument("--run-id", default=None, help="name of the run directory (default: timestamp + scenario)")
    p.add_argument("--claude-bin", default=None, help="path to the Claude Code CLI (default: `claude` on PATH)")
    p.add_argument("--max-turns", type=int, default=None, help="override the scenario's --max-turns for every stage")
    p.add_argument("--timeout", type=float, default=None, help="override the scenario's per-stage wall-clock timeout (s)")
    p.add_argument("--max-budget-usd", type=float, default=None, help="pass --max-budget-usd to every Claude session")
    p.add_argument("--arm-order", choices=("AB", "BA"), default="AB", help="which evaluation arm runs first")
    p.add_argument("--repeats", type=int, default=1, help="number of A/B pairs to run from the same burn-in")
    p.add_argument("--history-wait", type=float, default=45.0, help="seconds to wait for the burn-in Shard to fold")
    p.add_argument("--setting-sources", default="project,local",
                   help="Claude Code --setting-sources (default excludes the machine's user settings)")
    p.add_argument("--keep-skills", action="store_true", help="do not pass --disable-slash-commands")
    p.add_argument("--permission-flag", default="--dangerously-skip-permissions",
                   help="permission flag for non-interactive runs ('' to pass none)")
    p.add_argument("--extra-arg", action="append", default=[], help="extra argument for every claude invocation")
    p.add_argument("--no-capture-service", action="store_true",
                   help="do not start a private capture service for burn-in (only for harness testing)")
    return p


def resolve_scenario(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_dir():
        return candidate.resolve()
    inside = SCENARIOS_DIR / value
    if inside.is_dir():
        return inside.resolve()
    raise BenchmarkError("scenario_missing", f"no scenario directory named {value!r} (looked in {SCENARIOS_DIR})")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenario_dir = resolve_scenario(args.scenario)
    except BenchmarkError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 3
    if args.repeats < 1:
        print("error: --repeats must be >= 1", file=sys.stderr)
        return 3
    options = BenchmarkOptions(
        scenario_dir=scenario_dir, out_dir=Path(args.out).resolve(), model=args.model,
        claude_argv=(args.claude_bin,) if args.claude_bin else None,
        max_turns=args.max_turns, timeout_seconds=args.timeout, max_budget_usd=args.max_budget_usd,
        arm_order=args.arm_order, repeats=args.repeats, run_id=args.run_id,
        start_capture_service=not args.no_capture_service, history_wait_seconds=args.history_wait,
        permission_flag=args.permission_flag, setting_sources=args.setting_sources or None,
        disable_skills=not args.keep_skills, extra_args=tuple(args.extra_arg),
    )
    print(f"PR13 benchmark: scenario={scenario_dir.name} model={args.model}")
    outcome = run_benchmark(options)
    print(f"status: {outcome.status}")
    print(f"run dir: {outcome.run_dir}")
    if outcome.error:
        print(f"error [{outcome.error['code']}]: {outcome.error['message']}", file=sys.stderr)
        return 2
    for v in outcome.validity_errors:
        print(f"validity error: {v}", file=sys.stderr)
    md = outcome.run_dir / "comparison.md"
    if md.exists():
        print(md.read_text(encoding="utf-8"))
    return 1 if outcome.validity_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
