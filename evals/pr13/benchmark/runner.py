"""Orchestration: preflight -> source -> burn-in -> reset -> arms -> comparison.

Read this module top to bottom to see the whole experiment. Every stage
writes its artifacts as it goes, so an aborted run still leaves a
``benchmark.json`` saying exactly which precondition failed.

Isolation, spelled out
----------------------
* Control (A) and treatment (B) are independent clones of the same source
  at the same base commit (``workspace.create_workspace``), in sibling
  directories that are proven not to nest (``assert_isolated``).
* Burn-in happens in B *before* A exists. B's code is then reset to the
  base commit with its ``.openshard/`` history kept byte-for-byte
  (``reset_code_preserving_history``), and the hook configuration is
  removed, so at evaluation time the only file-system difference between
  A and B is ``.openshard/``.
* Both arms run the same Claude Code binary, model, flags and scrubbed
  environment; both use ``--strict-mcp-config`` and both declare one MCP
  server named ``openshard`` with the same five read-only tools. A's is
  the benchmark-local placebo (``evals/pr13/placebo_mcp.py``: same tool
  names, descriptions and schemas; empty-history answers; never reads
  history). B's is the production server for B's own path, built from
  ``build_server_argv``. Both servers are probed over MCP before the arms
  run and must present identical tool surfaces (``mcp_probe``).
* So the arms differ in exactly one thing: control receives empty
  OpenShard history, treatment receives the preserved burn-in evidence.
* Verification and the known-failed-approach check run only after the
  agent has exited, from outside the workspace.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.pr13.benchmark import capture, cross_agent, workspace
from evals.pr13.benchmark.config import ScenarioConfig, load_scenario
from evals.pr13.benchmark.errors import BenchmarkError
from evals.pr13.benchmark.harness import (
    OPENSHARD_TOOL_PREFIX,
    AgentRun,
    HarnessConfig,
    ParsedStream,
    build_argv,
    parse_stream,
    run_agent,
    scrubbed_env,
    utc_now,
    write_mcp_config,
)
from evals.pr13.benchmark.mcp_probe import (
    EXPECTED_TOOLS,
    KIND_PLACEBO,
    KIND_PRODUCTION,
    probe_stdio_server,
    require_expected_surface,
    require_same_surface,
)
from evals.pr13.benchmark.results import (
    ARM_BURN_IN,
    ARM_CONTROL,
    ARM_TREATMENT,
    RunResult,
    activity_from_run,
    build_comparison,
    render_comparison_markdown,
    retrieval_observed,
    usage_from_run,
)
from evals.pr13.benchmark.verify import (
    VerificationResult,
    evaluate_known_failed_approach,
    run_verification,
)
from openshard.adapters.claude_mcp_install import build_server_argv

BENCHMARK_SCHEMA_VERSION = 1
PLACEBO_SERVER_PATH = Path(__file__).resolve().parent.parent / "placebo_mcp.py"


def mcp_server_config(arm: str, *, ws: Path, openshard_exe: str, python: str) -> dict[str, Any]:
    """The one ``openshard`` MCP server each arm declares; only its backing differs."""
    if arm == ARM_TREATMENT:
        server_argv = build_server_argv(ws)  # ["openshard", "mcp", "serve", "--repo-path", <ws>]
        return {"type": "stdio", "command": openshard_exe, "args": server_argv[1:]}
    if not PLACEBO_SERVER_PATH.is_file():
        raise BenchmarkError("placebo_missing", f"placebo MCP server not found at {PLACEBO_SERVER_PATH}")
    return {"type": "stdio", "command": python, "args": [str(PLACEBO_SERVER_PATH)]}


def arm_env(options: BenchmarkOptions, *, path_prepend: list[str], openshard_home: Path) -> tuple[dict[str, str], list[str]]:
    return scrubbed_env(
        options.env_base, path_prepend=path_prepend,
        overrides={"OPENSHARD_HOME": str(openshard_home), "OPENSHARD_CAPTURE_DISABLE": "1"},
    )


@dataclass(frozen=True)
class BenchmarkOptions:
    scenario_dir: Path
    out_dir: Path
    model: str
    claude_argv: tuple[str, ...] | None = None
    max_turns: int | None = None  # overrides the scenario's per-stage value when set
    timeout_seconds: float | None = None  # same
    max_budget_usd: float | None = None
    arm_order: str = "AB"
    repeats: int = 1
    run_id: str | None = None
    start_capture_service: bool = True
    history_wait_seconds: float = 45.0
    python: str = sys.executable
    permission_flag: str = "--dangerously-skip-permissions"
    setting_sources: str | None = "project,local"
    disable_skills: bool = True
    no_session_persistence: bool = True
    extra_args: tuple[str, ...] = ()
    env_base: dict[str, str] | None = None  # tests: a controlled environment instead of os.environ

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_dir": str(self.scenario_dir),
            "out_dir": str(self.out_dir),
            "model": self.model,
            "claude_argv": list(self.claude_argv) if self.claude_argv else None,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "max_budget_usd": self.max_budget_usd,
            "arm_order": self.arm_order,
            "repeats": self.repeats,
            "run_id": self.run_id,
            "start_capture_service": self.start_capture_service,
            "history_wait_seconds": self.history_wait_seconds,
            "python": self.python,
            "permission_flag": self.permission_flag,
            "setting_sources": self.setting_sources,
            "disable_skills": self.disable_skills,
            "no_session_persistence": self.no_session_persistence,
            "extra_args": list(self.extra_args),
        }


@dataclass
class BenchmarkOutcome:
    status: str  # "completed" | "completed_with_validity_errors" | "aborted"
    run_dir: Path
    error: dict[str, Any] | None = None
    burn_in: RunResult | None = None
    arms: list[RunResult] = field(default_factory=list)
    validity_errors: list[str] = field(default_factory=list)

    @property
    def benchmark_json(self) -> Path:
        return self.run_dir / "benchmark.json"


class _State:
    """Mutable progress the runner persists to ``benchmark.json`` after every stage."""

    def __init__(self, run_dir: Path, options: BenchmarkOptions) -> None:
        self.run_dir = run_dir
        # The burn-in agent environment, kept only so the private capture
        # service can be shut down on any exit path. Never persisted.
        self.capture_env: dict[str, str] | None = None
        self.data: dict[str, Any] = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "benchmark": "pr13",
            "status": "running",
            "started_at": utc_now(),
            "ended_at": None,
            "options": options.to_dict(),
            "preflight": {},
            "scenario": None,
            "source": None,
            "burn_in": None,
            "arms": [],
            "validity_errors": [],
            "error": None,
            "comparison": None,
        }

    def save(self) -> None:
        capture.dump_json(self.run_dir / "benchmark.json", self.data)


def _version_of(argv: list[str]) -> str | None:
    try:
        out = subprocess.run([*argv, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines()[0] if text else None


def preflight(options: BenchmarkOptions) -> tuple[dict[str, Any], list[str], str]:
    """Verify every external requirement. Returns ``(report, claude_argv, openshard_exe)``."""
    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": options.python,
        "cwd": str(Path.cwd()),
    }
    git_version = _version_of(["git"])
    if git_version is None:
        raise BenchmarkError("git_missing", "git is required and was not found on PATH")
    report["git"] = git_version

    if options.claude_argv:
        claude_argv = list(options.claude_argv)
        head = claude_argv[0]
        if not (Path(head).is_file() or shutil.which(head)):
            raise BenchmarkError("claude_cli_missing", f"configured Claude Code command not found: {head}")
    else:
        found = shutil.which("claude")
        if not found:
            raise BenchmarkError(
                "claude_cli_missing",
                "the Claude Code CLI ('claude') is not on PATH; install it or pass --claude-bin",
            )
        claude_argv = [found]
    claude_version = _version_of(claude_argv)
    if claude_version is None:
        raise BenchmarkError("claude_cli_unusable", f"could not run {claude_argv} --version")
    report["claude"] = {"argv": claude_argv, "version": claude_version}

    try:
        import openshard
        import openshard.mcp.server  # noqa: F401  -- proves the 'mcp' extra is importable here
    except ImportError as exc:
        raise BenchmarkError(
            "mcp_extra_missing",
            f"openshard.mcp.server cannot be imported in this interpreter ({exc}); install openshard[mcp]",
        ) from exc
    report["openshard"] = {"version": getattr(openshard, "__version__", None), "path": str(Path(openshard.__file__).parent)}

    scripts_dir = str(Path(options.python).parent)
    openshard_exe = shutil.which("openshard", path=scripts_dir)
    if not openshard_exe:
        raise BenchmarkError(
            "openshard_cli_missing",
            f"no 'openshard' console script next to the benchmark interpreter ({scripts_dir}); "
            "the Claude Code hooks and MCP server are launched by that name",
        )
    report["openshard_cli"] = openshard_exe
    report["agent_path_prepend"] = [scripts_dir]
    return report, claude_argv, openshard_exe


def _harness_config(options: BenchmarkOptions, claude_argv: list[str], *, max_turns: int | None,
                    timeout_seconds: float) -> HarnessConfig:
    return HarnessConfig(
        claude_argv=tuple(claude_argv), model=options.model,
        max_turns=options.max_turns if options.max_turns is not None else max_turns,
        timeout_seconds=options.timeout_seconds if options.timeout_seconds is not None else timeout_seconds,
        max_budget_usd=options.max_budget_usd, permission_flag=options.permission_flag,
        setting_sources=options.setting_sources, disable_skills=options.disable_skills,
        no_session_persistence=options.no_session_persistence, extra_args=tuple(options.extra_args),
    )


def _run_result(
    *, scenario: ScenarioConfig, arm: str, repeat: int, ws: Path, run_dir: Path, harness: HarnessConfig,
    run: AgentRun, verification: dict[str, Any], repeated: dict[str, Any], openshard_info: dict[str, Any],
    changed: dict[str, Any], errors: list[str], notes: list[str], extra_artifacts: dict[str, Any] | None = None,
) -> RunResult:
    stream = run.parsed
    artifacts = {
        "run_dir": str(run_dir),
        "workspace": str(ws),
        "agent_stdout": run.stdout_path,
        "agent_stderr": run.stderr_path,
        "prompt": str(run_dir / "prompt.txt"),
        "argv": str(run_dir / "argv.json"),
        "mcp_config": str(run_dir / "mcp_config.json"),
        "verification_dir": str(run_dir / "verification"),
    }
    if extra_artifacts:
        artifacts.update(extra_artifacts)
    return RunResult(
        scenario=scenario.id, arm=arm, repeat=repeat, base_commit=scenario.repository.base_commit,
        workspace=str(ws), run_dir=str(run_dir), harness=harness.to_dict(),
        model_requested=harness.model, model_reported_init=stream.model_init,
        models_observed=list(stream.models_observed),
        started_at=run.started_at, ended_at=run.ended_at, wall_clock_seconds=run.wall_clock_seconds,
        agent_exit_status=run.exit_status, agent_exit_code=run.exit_code, agent_timed_out=run.timed_out,
        agent_reported_completion=run.agent_reported_completion, agent_result_subtype=stream.result_subtype,
        agent_num_turns=stream.num_turns, agent_final_text=stream.result_text,
        activity=activity_from_run(run, changed), verification=verification, repeated_known_failure=repeated,
        openshard=openshard_info, usage=usage_from_run(run), artifacts=artifacts, errors=errors, notes=notes,
    )


def _agent_stderr_tail(run: AgentRun, cap: int = 1500) -> str:
    try:
        text = Path(run.stderr_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-cap:]


@dataclass
class _BurnInOutcome:
    """What every burn-in mechanism (hooks / wrap-chain / OpenCode) must produce.

    ``_run_stages`` consumes only this shape, so the mechanism-specific
    functions below are the *only* place that branches on how history was
    captured -- everything after burn-in (precondition checks, the code
    reset, the A/B arms, comparison) is completely mechanism-agnostic.
    """

    run_result: RunResult
    verification: VerificationResult
    repeated: dict[str, Any]
    evidence: dict[str, Any]
    history: dict[str, Any]
    snapshot: dict[str, Any]
    session_id: str | None
    hooks_removed: dict[str, Any]
    extra_state: dict[str, Any] = field(default_factory=dict)


def _run_hook_burn_in(
    *, options: BenchmarkOptions, scenario: ScenarioConfig, claude_argv: list[str], ws_b1: Path, burn_dir: Path,
    base: str, eval_prompt: str, path_prepend: list[str], openshard_home: Path, state: _State,
) -> _BurnInOutcome:
    """Default burn-in (Scenario 1 and most others): one real Claude Code session,
    captured through OpenShard's production Claude Code hooks. Unchanged from the
    original single-mechanism implementation."""
    port = capture.pick_free_port()
    hooks_info = capture.install_burn_in_capture(ws_b1, port)
    burn_env, removed = scrubbed_env(
        options.env_base, path_prepend=path_prepend,
        overrides=capture.capture_env_overrides(openshard_home, port),
    )
    service_info: dict[str, Any] = {"state": "not_started_by_benchmark"}
    if options.start_capture_service:
        service_info = capture.start_capture_service(burn_env)
        state.capture_env = dict(burn_env)
    mcp_burn = write_mcp_config(burn_dir / "mcp_config.json", {})
    harness_burn = _harness_config(
        options, claude_argv, max_turns=scenario.burn_in.max_turns, timeout_seconds=scenario.burn_in.timeout_seconds,
    )
    burn_run = run_agent(
        harness_burn, prompt=scenario.burn_in.prompt_text(), cwd=ws_b1, env=burn_env, env_removed=removed,
        mcp_config_path=mcp_burn, out_dir=burn_dir,
    )
    burn_errors: list[str] = []
    session_id = burn_run.parsed.session_id
    if session_id is None:
        raise BenchmarkError(
            "burn_in_agent_failed",
            "the burn-in Claude Code session produced no session id (it did not start, or authentication failed); "
            f"exit={burn_run.exit_status}; stderr tail: {_agent_stderr_tail(burn_run)!r}",
            details={"run_dir": str(burn_dir)},
        )
    captured = capture.wait_for_captured_session(ws_b1, session_id, timeout_seconds=options.history_wait_seconds)
    if options.start_capture_service:
        service_info["stopped"] = capture.stop_capture_service(burn_env)
        state.capture_env = None
        try:
            log = openshard_home / "claude-capture.log"
            if log.exists():
                shutil.copyfile(log, burn_dir / "claude-capture.log")
        except OSError:
            pass

    verification_b = run_verification(
        scenario.verification, workspace=ws_b1, scenario_dir=scenario.scenario_dir,
        out_dir=burn_dir / "verification", python=options.python,
    )
    changed_b = workspace.changed_paths(ws_b1, base)
    repeated_b = evaluate_known_failed_approach(scenario.known_failed_approach, verification_b, changed_b)
    evidence = capture.evaluate_expected_evidence(scenario.expected_evidence, ws_b1, eval_prompt)
    history_b = capture.history_summary(ws_b1)
    snapshot = capture.snapshot_history(ws_b1, burn_dir / "history_snapshot")
    burn_openshard: dict[str, Any] = {
        "history_present": True,
        "history_source": "hook_capture_this_session",
        "hooks": hooks_info,
        "capture_service": service_info,
        "captured_session_id": session_id,
        "captured_shard_id": captured.get("shard_id"),
        "captured_task_status": (captured.get("capture") or {}).get("task_status"),
        "captured_session_end_observed": (captured.get("capture") or {}).get("session_end_observed"),
        "history": history_b,
        "expected_evidence": evidence,
        "history_snapshot": snapshot,
        "mcp_configured": False,
        "mcp_server_status": None,
        "retrieval_observed": retrieval_observed(burn_run, mcp_configured=False),
        "tools_called": [],
    }
    burn_result = _run_result(
        scenario=scenario, arm=ARM_BURN_IN, repeat=0, ws=ws_b1, run_dir=burn_dir, harness=harness_burn,
        run=burn_run, verification=verification_b.to_dict(), repeated=repeated_b, openshard_info=burn_openshard,
        changed=changed_b.to_dict(), errors=burn_errors,
        notes=["Burn-in: hooks installed, no MCP server, private capture service; "
               "verification ran after the session ended and is not part of the captured Shard."],
        extra_artifacts={"history_snapshot": snapshot["path"], "hooks_settings": hooks_info["settings_path"]},
    )
    burn_result.write(burn_dir / "run.json")
    return _BurnInOutcome(
        run_result=burn_result, verification=verification_b, repeated=repeated_b, evidence=evidence,
        history=history_b, snapshot=snapshot, session_id=session_id,
        hooks_removed=capture.remove_burn_in_capture(ws_b1),
    )


def _run_wrap_chain_burn_in(
    *, options: BenchmarkOptions, scenario: ScenarioConfig, claude_argv: list[str], openshard_exe: str,
    ws_b1: Path, burn_dir: Path, base: str, eval_prompt: str, path_prepend: list[str],
) -> _BurnInOutcome:
    """"Multi-attempt chronology" burn-in: two or more real, separate Claude
    Code sessions, each launched through ``openshard wrap claude [--shard <id>]``
    so OpenShard links them as real attempts of one persistent Shard.

    Produces a genuine same-Shard failed-then-corrected attempt chronology
    -- not a verified-outcome PR11 ``RecoveryObservation`` (external
    capture never persists ``verification_passed``; see this module's
    docstring and the scenario's own README for the full statement).

    No hooks are installed and no capture service is used -- ``wrap`` writes
    its entry synchronously. Verification runs after *every* stage so the
    benchmark can honestly report the intermediate (expected-failing) state,
    but only the first stage's diff/verification feeds
    ``known_failed_approach`` (matching the "known failed approach" concept
    of *one* bad attempt) and only the *final* stage's verification is
    required to pass (see ``burn_in_correction_not_confirmed`` below) --
    ``require_verification_failed``/``require_known_failed_approach`` on the
    scenario apply to that first stage, not to the (by design, corrected)
    final state.
    """
    stages = scenario.burn_in.wrap_stages
    env, removed = scrubbed_env(options.env_base, path_prepend=path_prepend, overrides={"OPENSHARD_CAPTURE_DISABLE": "1"})
    harness_cfg = _harness_config(
        options, claude_argv, max_turns=scenario.burn_in.max_turns, timeout_seconds=scenario.burn_in.timeout_seconds,
    )
    mcp_burn = write_mcp_config(burn_dir / "mcp_config.json", {})
    inner_argv = build_argv(harness_cfg, mcp_burn)

    shard_id: str | None = None
    stage_results = []
    stage_verifications: list[dict[str, Any]] = []
    first_verification: VerificationResult | None = None
    first_changed: workspace.ChangedPaths | None = None
    last_verification: VerificationResult | None = None
    for i, stage in enumerate(stages, start=1):
        result = cross_agent.run_wrap_stage(
            openshard_exe=openshard_exe, task=stage.task, workspace=ws_b1, claude_inner_argv=inner_argv,
            prompt=stage.prompt_text(), env=env, shard_id=shard_id, stage_index=i,
            timeout_seconds=scenario.burn_in.timeout_seconds, out_dir=burn_dir,
        )
        stage_results.append(result)
        if result.shard_id is None:
            raise BenchmarkError(
                "burn_in_agent_failed",
                f"wrap-chain stage {i} ('{stage.task}') produced no Shard entry "
                f"(exit={result.exit_code}, timed_out={result.timed_out})",
                details={"run_dir": str(burn_dir)},
            )
        shard_id = result.shard_id
        stage_verification = run_verification(
            scenario.verification, workspace=ws_b1, scenario_dir=scenario.scenario_dir,
            out_dir=burn_dir / f"verification_stage{i}", python=options.python,
        )
        stage_changed = workspace.changed_paths(ws_b1, base)
        stage_verifications.append({
            "stage": i, "task": stage.task, "shard_id": result.shard_id, "attempt_number": result.attempt_number,
            "verification": stage_verification.to_dict(), "changed_paths": stage_changed.to_dict(),
        })
        if i == 1:
            first_verification, first_changed = stage_verification, stage_changed
        last_verification = stage_verification

    assert first_verification is not None and first_changed is not None and last_verification is not None
    repeated_first = evaluate_known_failed_approach(scenario.known_failed_approach, first_verification, first_changed)
    if not last_verification.passed:
        raise BenchmarkError(
            "burn_in_correction_not_confirmed",
            "the wrap-chain burn-in's final stage did not pass benchmark verification, so no recovery was "
            "genuinely observed; the scenario precondition is not met (nothing was substituted)",
            details={"stages": stage_verifications},
        )

    final_changed = workspace.changed_paths(ws_b1, base)
    evidence = capture.evaluate_expected_evidence(scenario.expected_evidence, ws_b1, eval_prompt)
    history_b = capture.history_summary(ws_b1)
    snapshot = capture.snapshot_history(ws_b1, burn_dir / "history_snapshot")
    shard_attempt_count = capture.count_shard_attempts(ws_b1, shard_id)
    burn_openshard: dict[str, Any] = {
        "history_present": True,
        "history_source": "claude_wrap_chain_this_session",
        "wrap_stages": [s.to_dict() for s in stage_results],
        "stage_verifications": stage_verifications,
        "shard_id": shard_id,
        "shard_attempt_count": shard_attempt_count,
        "history": history_b,
        "expected_evidence": evidence,
        "history_snapshot": snapshot,
        "mcp_configured": False,
        "mcp_server_status": None,
        "retrieval_observed": "unknown",
        "tools_called": [],
        "recovery_observation_note": (
            "No production real-agent capture path (Claude/Codex hooks, OpenCode plugin, or "
            "`openshard wrap`) ever records verification_passed/osn_verification_contract -- that field is "
            "written only by OpenShard's native run pipeline, which PR13 deliberately excludes as 'the "
            "coding agent'. So this Shard's multiple real, wrap-linked attempts are visible (attempt count, "
            "per-attempt files/status via relevant_context), but history.query's fail-then-pass "
            "RecoveryObservation structure will not populate from it. This is a disclosed architecture "
            "boundary, not a benchmark defect -- see the scenario README."
        ),
    }
    # A synthetic AgentRun-shaped result for the *last* stage so _run_result's
    # existing shape (built for one Claude session) still applies; per-stage
    # detail lives in wrap_stages/stage_verifications above.
    last_stage_run = _last_stage_as_agent_run(stage_results[-1], harness_cfg)
    burn_result = _run_result(
        scenario=scenario, arm=ARM_BURN_IN, repeat=0, ws=ws_b1, run_dir=burn_dir, harness=harness_cfg,
        run=last_stage_run, verification=last_verification.to_dict(), repeated=repeated_first,
        openshard_info=burn_openshard, changed=final_changed.to_dict(), errors=[],
        notes=[f"Burn-in: {len(stages)}-stage claude_wrap_chain; stage 1 is the known-failed-approach check, "
               "the final stage's verification must pass (the corrected attempt). No hooks, no MCP server, "
               "no capture service."],
        extra_artifacts={"history_snapshot": snapshot["path"]},
    )
    burn_result.write(burn_dir / "run.json")
    return _BurnInOutcome(
        run_result=burn_result, verification=first_verification, repeated=repeated_first, evidence=evidence,
        history=history_b, snapshot=snapshot, session_id=None, hooks_removed={"status": "not_installed"},
        extra_state={"final_verification_passed": last_verification.passed, "stages": len(stages),
                    "shard_id": shard_id, "shard_attempt_count": shard_attempt_count},
    )


def _last_stage_as_agent_run(stage: cross_agent.WrapStageResult, harness_cfg: HarnessConfig) -> AgentRun:
    """Adapt a wrap-chain stage into the ``AgentRun`` shape ``_run_result`` expects.

    Reparses that stage's own captured stdout (the wrapped Claude process's
    real stream-json, passed through by ``openshard wrap``) so token/cost/
    model accounting is real, not synthesized.
    """
    try:
        lines = Path(stage.stdout_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    parsed = parse_stream(lines)
    return AgentRun(
        argv=stage.argv, cwd="", started_at=stage.started_at, ended_at=stage.ended_at,
        wall_clock_seconds=stage.wall_clock_seconds, exit_code=stage.exit_code, timed_out=stage.timed_out,
        launch_error=stage.launch_error, stdout_path=stage.stdout_path, stderr_path=stage.stderr_path,
        env_removed=[], parsed=parsed,
    )


def _opencode_error_note(stdout_path: str) -> str:
    """The first OpenCode ``error`` event's message from its NDJSON stdout, for a
    diagnostic abort (e.g. 'Insufficient credits'). Never raises; '' when none."""
    try:
        for line in Path(stdout_path).read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "error":
                message = ((event.get("error") or {}).get("data") or {}).get("message")
                if isinstance(message, str) and message.strip():
                    return f"; OpenCode reported: {message.strip()[:300]!r}"
    except OSError:
        pass
    return ""


def _run_opencode_burn_in(
    *, options: BenchmarkOptions, scenario: ScenarioConfig, ws_b1: Path, burn_dir: Path, base: str,
    eval_prompt: str, path_prepend: list[str], openshard_home: Path, state: _State,
) -> _BurnInOutcome:
    """"Cross-Agent Handoff" burn-in: a real OpenCode session, captured through
    OpenShard's production OpenCode plugin, evaluated later by Claude Code/Sonnet."""
    opencode_exe = cross_agent.detect_opencode_cli()
    if not opencode_exe:
        raise BenchmarkError(
            "opencode_cli_missing",
            "the OpenCode CLI ('opencode') is not on PATH; this scenario's burn-in requires it",
        )
    port = capture.pick_free_port()
    plugin_info = capture.install_opencode_burn_in_capture(ws_b1, port)
    burn_env, removed = scrubbed_env(
        options.env_base, path_prepend=path_prepend,
        overrides=capture.capture_env_overrides(openshard_home, port),
    )
    service_info: dict[str, Any] = {"state": "not_started_by_benchmark"}
    if options.start_capture_service:
        service_info = capture.start_capture_service(burn_env)
        state.capture_env = dict(burn_env)

    baseline = capture.entry_count(ws_b1)
    agent_model = scenario.burn_in.agent_model
    if not agent_model:  # config.py already enforces this; never fall back to the Claude --model
        raise BenchmarkError(
            "scenario_invalid", "opencode_hooks burn-in has no agent_model; refusing to pass the Claude "
            "Code --model to OpenCode",
        )
    run = cross_agent.run_opencode(
        opencode_exe=opencode_exe, workspace=ws_b1, model=agent_model, prompt=scenario.burn_in.prompt_text(),
        env=burn_env, timeout_seconds=scenario.burn_in.timeout_seconds, out_dir=burn_dir,
    )
    if run.exit_code != 0:
        # The burn-in agent itself failed to run (e.g. an unknown model, no
        # provider credentials). Whatever the plugin still captured is not a
        # prior *attempt* at the task, so it must not reach the
        # known-failed-approach check and be mislabelled as "failed
        # differently" -- the exact mislabel a live Scenario 7 run produced.
        # Mirrors the Claude path's burn_in_agent_failed for a session that
        # never started.
        try:
            stdout_tail = Path(run.stdout_path).read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:
            stdout_tail = ""
        if options.start_capture_service:
            capture.stop_capture_service(burn_env)
            state.capture_env = None
        raise BenchmarkError(
            "burn_in_agent_failed",
            f"the OpenCode burn-in process exited {run.exit_code} (timed_out={run.timed_out}) before "
            f"attempting the task; stdout tail: {stdout_tail!r}",
            details={"run_dir": str(burn_dir), "agent_model": agent_model, "argv": run.argv},
        )
    captured = capture.wait_for_new_entry(
        ws_b1, baseline, timeout_seconds=options.history_wait_seconds, executor="opencode_plugin",
    )
    if options.start_capture_service:
        service_info["stopped"] = capture.stop_capture_service(burn_env)
        state.capture_env = None

    # "Exited 0 but never attempted the task" -- the v2 live failure mode
    # (OpenCode's model call 402'd on insufficient credits, so it exited 0
    # with no tool calls and no edits). The exit-code guard above cannot see
    # this; the captured task-only Shard is not a prior *attempt* and must
    # not reach the known-failed-approach check (it would mislabel as
    # burn_in_failed_differently). The known bad approach always edits
    # relay/_schema.py, so any genuine attempt changes at least one file --
    # zero tool calls AND zero changed files is unambiguously "no attempt".
    changed_b = workspace.changed_paths(ws_b1, base)
    tool_calls = int((captured.get("capture") or {}).get("tool_call_count") or 0)
    if tool_calls == 0 and not changed_b.all:
        raise BenchmarkError(
            "burn_in_agent_failed",
            "the OpenCode burn-in exited 0 but made no attempt at the task (0 tool calls, no file "
            f"changes){_opencode_error_note(run.stdout_path)}; the captured session is not a prior "
            "attempt at the known failed approach and must not reach the known-failed-approach check",
            details={"run_dir": str(burn_dir), "agent_model": agent_model,
                     "opencode_exit_code": run.exit_code, "changed_paths": changed_b.to_dict()},
        )

    verification_b = run_verification(
        scenario.verification, workspace=ws_b1, scenario_dir=scenario.scenario_dir,
        out_dir=burn_dir / "verification", python=options.python,
    )
    repeated_b = evaluate_known_failed_approach(scenario.known_failed_approach, verification_b, changed_b)
    evidence = capture.evaluate_expected_evidence(scenario.expected_evidence, ws_b1, eval_prompt)
    history_b = capture.history_summary(ws_b1)
    snapshot = capture.snapshot_history(ws_b1, burn_dir / "history_snapshot")
    session_id = (captured.get("capture") or {}).get("session_id")
    burn_openshard: dict[str, Any] = {
        "history_present": True,
        "history_source": "opencode_plugin_this_session",
        "burn_in_agent": "opencode",
        "opencode_run": run.to_dict(),
        "plugin": plugin_info,
        "capture_service": service_info,
        "captured_session_id": session_id,
        "captured_shard_id": captured.get("shard_id"),
        "history": history_b,
        "expected_evidence": evidence,
        "history_snapshot": snapshot,
        "mcp_configured": False,
        "mcp_server_status": None,
        "retrieval_observed": "unknown",
        "tools_called": [],
    }
    harness_burn = HarnessConfig(
        claude_argv=(opencode_exe, "run"), model=agent_model, max_turns=None,
        timeout_seconds=scenario.burn_in.timeout_seconds,
    )
    synthetic = AgentRun(
        argv=run.argv, cwd=str(ws_b1), started_at=run.started_at, ended_at=run.ended_at,
        wall_clock_seconds=run.wall_clock_seconds, exit_code=run.exit_code, timed_out=run.timed_out,
        launch_error=None, stdout_path=run.stdout_path, stderr_path=run.stderr_path, env_removed=removed,
        parsed=ParsedStream(session_id=session_id, model_init=options.model),
    )
    burn_result = _run_result(
        scenario=scenario, arm=ARM_BURN_IN, repeat=0, ws=ws_b1, run_dir=burn_dir, harness=harness_burn,
        run=synthetic, verification=verification_b.to_dict(), repeated=repeated_b, openshard_info=burn_openshard,
        changed=changed_b.to_dict(), errors=[],
        notes=["Burn-in: OpenCode plugin installed, no MCP server, private capture service; "
               "the evaluation arms still run Claude Code -- this is the cross-agent handoff."],
        extra_artifacts={"history_snapshot": snapshot["path"], "plugin_path": plugin_info["settings_path"]},
    )
    burn_result.write(burn_dir / "run.json")
    return _BurnInOutcome(
        run_result=burn_result, verification=verification_b, repeated=repeated_b, evidence=evidence,
        history=history_b, snapshot=snapshot, session_id=session_id,
        hooks_removed=capture.remove_opencode_burn_in_capture(ws_b1),
    )


def run_benchmark(options: BenchmarkOptions) -> BenchmarkOutcome:
    """Run the whole experiment. Never raises ``BenchmarkError``: aborts are persisted and returned."""
    if options.arm_order not in ("AB", "BA"):
        raise ValueError("arm_order must be 'AB' or 'BA'")
    if options.repeats < 1:
        raise ValueError("repeats must be >= 1")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = options.out_dir / (options.run_id or f"{stamp}-{Path(options.scenario_dir).name}")
    if run_dir.exists():
        raise ValueError(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    state = _State(run_dir, options)
    state.save()
    outcome = BenchmarkOutcome(status="running", run_dir=run_dir)
    try:
        _run_stages(options, state, outcome, run_dir)
    except BenchmarkError as exc:
        outcome.status = "aborted"
        outcome.error = exc.to_dict()
        state.data["error"] = exc.to_dict()
    except Exception as exc:  # unexpected: still leave a readable record, then propagate
        outcome.status = "aborted"
        outcome.error = {"code": "unexpected_exception", "message": f"{type(exc).__name__}: {exc}", "details": {}}
        state.data["error"] = outcome.error
        state.data["status"] = outcome.status
        state.data["ended_at"] = utc_now()
        state.save()
        raise
    finally:
        if state.capture_env is not None:
            capture.stop_capture_service(state.capture_env)
            state.capture_env = None
    if outcome.status == "running":
        outcome.status = "completed_with_validity_errors" if outcome.validity_errors else "completed"
    state.data["status"] = outcome.status
    state.data["validity_errors"] = list(outcome.validity_errors)
    state.data["ended_at"] = utc_now()
    state.save()
    return outcome


def _run_stages(options: BenchmarkOptions, state: _State, outcome: BenchmarkOutcome, run_dir: Path) -> None:
    scenario = load_scenario(options.scenario_dir)
    state.data["scenario"] = scenario.to_dict()
    report, claude_argv, openshard_exe = preflight(options)
    state.data["preflight"] = report
    state.save()

    source = workspace.materialize_source(
        scenario.repository.kind, scenario.repository.base_commit,
        seed_dir=scenario.repository.seed_path, url=scenario.repository.url,
        bench_root=run_dir / "workspaces", default_branch=scenario.repository.default_branch,
    )
    state.data["source"] = source.to_dict()
    state.save()

    base = scenario.repository.base_commit
    ws_root = run_dir / "workspaces"
    path_prepend = list(report["agent_path_prepend"])
    openshard_home = run_dir / "openshard-home"
    openshard_home.mkdir(exist_ok=True)
    eval_prompt = scenario.evaluation.prompt_text()

    # ------------------------------------------------------------------ burn-in
    ws_b1 = workspace.create_workspace(source, ws_root / "B1", label="B1")
    burn_dir = run_dir / "burn_in"
    burn_dir.mkdir()

    capture_kind = scenario.burn_in.capture
    if capture_kind == "claude_hooks":
        outcome_b = _run_hook_burn_in(
            options=options, scenario=scenario, claude_argv=claude_argv, ws_b1=ws_b1, burn_dir=burn_dir,
            base=base, eval_prompt=eval_prompt, path_prepend=path_prepend, openshard_home=openshard_home,
            state=state,
        )
    elif capture_kind == "claude_wrap_chain":
        outcome_b = _run_wrap_chain_burn_in(
            options=options, scenario=scenario, claude_argv=claude_argv, openshard_exe=openshard_exe,
            ws_b1=ws_b1, burn_dir=burn_dir, base=base, eval_prompt=eval_prompt, path_prepend=path_prepend,
        )
    elif capture_kind == "opencode_hooks":
        outcome_b = _run_opencode_burn_in(
            options=options, scenario=scenario, ws_b1=ws_b1, burn_dir=burn_dir, base=base,
            eval_prompt=eval_prompt, path_prepend=path_prepend, openshard_home=openshard_home, state=state,
        )
    else:  # pragma: no cover - config.py already rejects unknown values
        raise BenchmarkError("scenario_invalid", f"unknown burn_in.capture {capture_kind!r}")

    verification_b, repeated_b, evidence, history_b, snapshot = (
        outcome_b.verification, outcome_b.repeated, outcome_b.evidence, outcome_b.history, outcome_b.snapshot,
    )
    outcome.burn_in = outcome_b.run_result
    state.data["burn_in"] = {
        "run_json": str(burn_dir / "run.json"),
        "capture": capture_kind,
        "session_id": outcome_b.session_id,
        "verification_failed": not verification_b.passed,
        "known_failed_approach_matched": repeated_b["matched"],
        "expected_evidence_present": evidence["present"],
        "retrievable": next((c["ok"] for c in evidence["checks"] if c["check"] == "retrievable_via_relevant_context"), None),
        "shards": len(history_b["shards"]),
        "agent_exit_status": outcome_b.run_result.agent_exit_status,
        **outcome_b.extra_state,
    }
    state.save()

    policy = scenario.burn_in
    if policy.require_verification_failed and verification_b.passed:
        raise BenchmarkError(
            "burn_in_did_not_fail",
            "the burn-in attempt passed verification, so there is no failed prior attempt to learn from; "
            "the scenario precondition is not met (re-run the benchmark; nothing was substituted)",
            details={"run_json": str(burn_dir / "run.json")},
        )
    if policy.require_known_failed_approach and not repeated_b["matched"]:
        raise BenchmarkError(
            "burn_in_failed_differently",
            "the burn-in attempt failed verification but not through the scenario's known failed approach "
            f"({scenario.known_failed_approach.id}); the recorded evidence would not be the one under test",
            details={"criteria": repeated_b["criteria"], "failed_steps": verification_b.to_dict()["failed_steps"]},
        )
    if policy.require_expected_evidence and not evidence["present"]:
        raise BenchmarkError(
            "burn_in_evidence_missing",
            "the preserved OpenShard history does not contain the evidence the scenario expects",
            details={"checks": evidence["checks"]},
        )

    # ---------------------------------------------------------- reset B's code
    state.data["burn_in"]["hooks_removed"] = outcome_b.hooks_removed
    reset = workspace.reset_code_preserving_history(ws_b1, base)
    state.data["burn_in"]["reset"] = reset.to_dict()
    if (ws_b1 / ".claude").exists():
        raise BenchmarkError("reset_incomplete", "hook/plugin configuration survived the reset of the treatment workspace")
    state.save()
    snapshot_hash = workspace.file_sha256(Path(snapshot["path"]) / "runs.jsonl")

    # ------------------------------------------------------------------- arms
    for repeat in range(1, options.repeats + 1):
        ws_a = workspace.create_workspace(source, ws_root / f"A{repeat}", label=f"A{repeat}")
        if repeat == 1:
            ws_b = ws_b1
            history_source = "burn_in_workspace_reset_in_place"
        else:
            ws_b = workspace.create_workspace(source, ws_root / f"B{repeat}", label=f"B{repeat}")
            shutil.copytree(snapshot["path"], ws_b / ".openshard")
            history_source = "replicated_byte_for_byte_from_burn_in_snapshot"
        workspace.assert_isolated(ws_a, ws_b)
        # Prove the tool surfaces match before either arm runs: probe both
        # configured servers over MCP exactly as Claude will launch them.
        env_probe, _ = arm_env(options, path_prepend=path_prepend, openshard_home=openshard_home)
        probes: dict[str, dict[str, Any]] = {}
        for arm, ws in ((ARM_CONTROL, ws_a), (ARM_TREATMENT, ws_b)):
            cfg = mcp_server_config(arm, ws=ws, openshard_exe=openshard_exe, python=options.python)
            probe = probe_stdio_server(cfg["command"], cfg["args"], cwd=ws, env=env_probe)
            require_expected_surface(probe, label=f"arm {arm}{repeat}")
            probes[arm] = probe
        require_same_surface(probes[ARM_CONTROL], probes[ARM_TREATMENT])
        state.data.setdefault("mcp_surface", []).append({
            "repeat": repeat, "tools": probes[ARM_CONTROL]["tool_names"],
            "fingerprint": probes[ARM_CONTROL]["fingerprint"],
            "control_kind": KIND_PLACEBO, "treatment_kind": KIND_PRODUCTION, "identical": True,
        })
        state.save()
        arms = [(ARM_CONTROL, ws_a), (ARM_TREATMENT, ws_b)]
        if options.arm_order == "BA":
            arms.reverse()
        for arm, ws in arms:
            result = _run_arm(
                options, scenario, claude_argv, openshard_exe, arm=arm, repeat=repeat, ws=ws, run_dir=run_dir,
                path_prepend=path_prepend, openshard_home=openshard_home, eval_prompt=eval_prompt,
                snapshot_hash=snapshot_hash, history_source=history_source, outcome=outcome, probe=probes[arm],
            )
            outcome.arms.append(result)
            state.data["arms"].append({"arm": arm, "repeat": repeat, "run_json": str(Path(result.run_dir) / "run.json"),
                                       "verified_success": result.verified_success,
                                       "repeated_known_failure": result.repeated_known_failure_matched,
                                       "retrieval_observed": result.openshard.get("retrieval_observed"),
                                       "errors": list(result.errors)})
            state.save()

    comparison = build_comparison(
        state.data["burn_in"], [r.to_dict() for r in outcome.arms],
        options={"scenario": scenario.id, "model": options.model, "base_commit": base,
                 "arm_order": options.arm_order, "repeats": options.repeats, "claude": report.get("claude")},
    )
    capture.dump_json(run_dir / "comparison.json", comparison)
    (run_dir / "comparison.md").write_text(
        render_comparison_markdown(comparison, [r.to_dict() for r in outcome.arms]), encoding="utf-8",
    )
    state.data["comparison"] = {"json": str(run_dir / "comparison.json"), "markdown": str(run_dir / "comparison.md")}
    state.save()


def _run_arm(
    options: BenchmarkOptions, scenario: ScenarioConfig, claude_argv: list[str], openshard_exe: str, *,
    arm: str, repeat: int, ws: Path, run_dir: Path, path_prepend: list[str], openshard_home: Path,
    eval_prompt: str, snapshot_hash: str | None, history_source: str, outcome: BenchmarkOutcome,
    probe: dict[str, Any],
) -> RunResult:
    base = scenario.repository.base_commit
    arm_dir = run_dir / f"arm_{arm}_{repeat}"
    arm_dir.mkdir()
    errors: list[str] = []
    notes: list[str] = []

    # Pre-run isolation facts, asserted loudly.
    head = workspace.head_commit(ws)
    if head != base:
        raise BenchmarkError("checkout_mismatch", f"arm {arm}{repeat} workspace is at {head}, expected {base}")
    if (ws / ".claude").exists():
        raise BenchmarkError("isolation_violated", f"arm {arm}{repeat} workspace carries a .claude/ directory")
    has_history = workspace.history_present(ws)
    if arm == ARM_CONTROL and has_history:
        raise BenchmarkError("isolation_violated", "control workspace has OpenShard history before the run")
    if arm == ARM_TREATMENT:
        if not has_history:
            raise BenchmarkError("isolation_violated", "treatment workspace has no OpenShard history before the run")
        current = workspace.file_sha256(ws / ".openshard" / "runs.jsonl")
        if snapshot_hash is not None and current != snapshot_hash:
            raise BenchmarkError("history_lost", "treatment history differs from the preserved burn-in snapshot")
    dirty = [line for line in workspace.status_lines(ws, ignored=True)
             if not line[3:].replace("\\", "/").startswith(".openshard/")]
    if dirty:
        raise BenchmarkError("workspace_dirty", f"arm {arm}{repeat} workspace is not at a clean base state: {dirty[:5]}")

    server_kind = KIND_PRODUCTION if arm == ARM_TREATMENT else KIND_PLACEBO
    servers = {"openshard": mcp_server_config(arm, ws=ws, openshard_exe=openshard_exe, python=options.python)}
    mcp_path = write_mcp_config(arm_dir / "mcp_config.json", servers)
    env, removed = arm_env(options, path_prepend=path_prepend, openshard_home=openshard_home)
    harness = _harness_config(
        options, claude_argv, max_turns=scenario.evaluation.max_turns,
        timeout_seconds=scenario.evaluation.timeout_seconds,
    )
    run = run_agent(harness, prompt=eval_prompt, cwd=ws, env=env, env_removed=removed,
                    mcp_config_path=mcp_path, out_dir=arm_dir)

    stream = run.parsed
    statuses = {str(s.get("name")): s.get("status") for s in stream.mcp_servers}
    expected_tool_names = sorted(f"{OPENSHARD_TOOL_PREFIX}{name}" for name in EXPECTED_TOOLS)
    init_openshard_tools = sorted(t for t in stream.tools_available if t.startswith(OPENSHARD_TOOL_PREFIX))
    init_tools_match: bool | None = (init_openshard_tools == expected_tool_names) if stream.session_id else None
    if stream.session_id is not None:
        others = sorted(name for name in statuses if name != "openshard")
        if others:
            msg = f"arm saw MCP servers other than openshard: {others}"
            errors.append(msg)
            outcome.validity_errors.append(f"{arm}{repeat}: {msg}")
        if statuses.get("openshard") != "connected":
            msg = (f"{server_kind} openshard MCP server was not reported as connected "
                   f"(status={statuses.get('openshard')!r})")
            errors.append(msg)
            outcome.validity_errors.append(f"{arm}{repeat}: {msg}")
        if not init_tools_match:
            msg = f"Claude's init event listed OpenShard tools {init_openshard_tools}, expected {expected_tool_names}"
            errors.append(msg)
            outcome.validity_errors.append(f"{arm}{repeat}: {msg}")
    if stream.session_id is None:
        msg = f"agent produced no session (exit={run.exit_status}); stderr tail: {_agent_stderr_tail(run)!r}"
        errors.append(msg)

    verification = run_verification(
        scenario.verification, workspace=ws, scenario_dir=scenario.scenario_dir,
        out_dir=arm_dir / "verification", python=options.python,
    )
    changed = workspace.changed_paths(ws, base)
    repeated = evaluate_known_failed_approach(scenario.known_failed_approach, verification, changed)
    history_after = workspace.history_present(ws)
    if arm == ARM_CONTROL and history_after:
        errors.append("control workspace gained an .openshard/ directory during the run")
    openshard_calls = [
        {"tool": c.name, "input": c.input_summary, "result_is_error": c.result_is_error,
         "result_excerpt": (c.result_text[:600] if c.result_text else None)}
        for c in stream.tool_calls if c.name.startswith(OPENSHARD_TOOL_PREFIX)
    ]
    burn_in_shard_ids = []
    if outcome.burn_in is not None:
        sid = outcome.burn_in.openshard.get("captured_shard_id")
        if isinstance(sid, str):
            burn_in_shard_ids.append(sid)
    surfaced = any(sid in (c.result_text or "") for sid in burn_in_shard_ids
                   for c in stream.tool_calls if c.name.startswith(OPENSHARD_TOOL_PREFIX))
    openshard_info = {
        "history_present": has_history,
        "history_present_after_run": history_after,
        "history_source": history_source if arm == ARM_TREATMENT else "none_control_has_empty_history",
        "history": capture.history_summary(ws) if arm == ARM_TREATMENT else {"present": False, "entries": 0, "shards": []},
        "mcp_configured": True,
        "mcp_server_kind": server_kind,
        "mcp_config": servers,
        "mcp_surface": {
            "tool_names": probe["tool_names"], "fingerprint": probe["fingerprint"],
            "matches_expected": probe["matches_expected"], "server_name": probe["server_name"],
            "init_event_openshard_tools": init_openshard_tools, "init_event_tools_match": init_tools_match,
        },
        "mcp_servers_reported": stream.mcp_servers,
        "mcp_server_status": statuses.get("openshard"),
        "retrieval_observed": retrieval_observed(run, mcp_configured=True),
        "tools_called": [c["tool"] for c in openshard_calls],
        "tool_calls": openshard_calls,
        "burn_in_shard_surfaced_in_tool_results": surfaced,
    }
    result = _run_result(
        scenario=scenario, arm=arm, repeat=repeat, ws=ws, run_dir=arm_dir, harness=harness, run=run,
        verification=verification.to_dict(), repeated=repeated, openshard_info=openshard_info,
        changed=changed.to_dict(), errors=errors, notes=notes,
    )
    result.write(arm_dir / "run.json")
    return result


def load_run_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def seconds_since(t0: float) -> float:  # pragma: no cover - helper for CLI progress
    return round(time.monotonic() - t0, 1)
