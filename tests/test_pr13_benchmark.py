"""Deterministic tests for the PR13 benchmark runner (evals/pr13). No live model calls.

The coding agent is replaced by ``tests/pr13_fake_claude.py``, a script
that speaks Claude Code's ``stream-json`` protocol and, for burn-in,
delivers real hook payloads to OpenShard's production hook adapter. The
benchmark code under test is otherwise exercised end to end: seed
repository build, exact-commit enforcement, A/B workspace isolation,
.openshard preservation across the code reset, verification parsing, the
known-failed-approach criterion, result serialisation, and every loud
failure path.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evals.pr13 import placebo_mcp as placebo
from evals.pr13.benchmark import capture, workspace
from evals.pr13.benchmark.config import load_scenario
from evals.pr13.benchmark.errors import BenchmarkError
from evals.pr13.benchmark.harness import (
    HarnessConfig,
    build_argv,
    parse_stream,
    run_agent,
    scrubbed_env,
    write_mcp_config,
)
from evals.pr13.benchmark.mcp_probe import (
    probe_stdio_server,
    require_expected_surface,
    require_same_surface,
    surface_fingerprint,
)
from evals.pr13.benchmark.results import RunResult, retrieval_observed
from evals.pr13.benchmark.runner import BenchmarkOptions, run_benchmark
from evals.pr13.benchmark.verify import evaluate_known_failed_approach, run_verification
from evals.pr13.benchmark.workspace import SourceRepo
from openshard.adapters.claude_mcp_install import MCP_TOOLS, build_server_argv
from tests.pr13_fake_claude import correct_edit, naive_edit

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = REPO_ROOT / "evals" / "pr13" / "scenarios" / "1_previously_failed_approach"
PLACEBO_PATH = REPO_ROOT / "evals" / "pr13" / "placebo_mcp.py"
FAKE_CLAUDE = Path(__file__).resolve().parent / "pr13_fake_claude.py"
FAKE_ARGV = (sys.executable, str(FAKE_CLAUDE))
FORBIDDEN = "MUST NEVER BE RECORDED"


@pytest.fixture(scope="module")
def scenario():
    return load_scenario(SCENARIO_DIR)


@pytest.fixture(scope="module")
def source(scenario, tmp_path_factory) -> SourceRepo:
    root = tmp_path_factory.mktemp("pr13-source")
    return workspace.materialize_source(
        "seed", scenario.repository.base_commit, seed_dir=scenario.repository.seed_path, url=None, bench_root=root,
    )


def _fake_env(**modes: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("PR13_FAKE"):
            del env[key]
    env.update(modes)
    return env


def _options(tmp_path: Path, env: dict[str, str], **overrides) -> BenchmarkOptions:
    kwargs = dict(
        scenario_dir=SCENARIO_DIR, out_dir=tmp_path / "results", model="fake-model-1", claude_argv=FAKE_ARGV,
        start_capture_service=False, history_wait_seconds=5.0, run_id="run", env_base=env,
    )
    kwargs.update(overrides)
    return BenchmarkOptions(**kwargs)


# ---------------------------------------------------------------------------
# Scenario configuration
# ---------------------------------------------------------------------------


class TestScenarioConfig:
    def test_scenario_1_loads_with_full_commit_id(self, scenario):
        assert scenario.id == "1_previously_failed_approach"
        assert scenario.repository.kind == "seed"
        assert len(scenario.repository.base_commit) == 40
        assert [s.name for s in scenario.verification] == ["generated_schema_in_sync", "hidden_tests"]
        assert scenario.known_failed_approach.mode == "all"
        assert "priority" in scenario.burn_in.prompt_text()
        # The evaluation prompt must not reveal the correct mechanism.
        evaluation = scenario.evaluation.prompt_text()
        for leak in ("gen_schema", "jobs.json", "_schema.py", "regenerate", "generated"):
            assert leak not in evaluation
        # Nor may the burn-in prompt: it must force the known-bad approach by
        # closing the agent's file scope, never by naming the correct
        # mechanism it should avoid (that text becomes the burn-in Shard's
        # captured task, which the treatment arm can read via
        # relevant_context -- naming schema/jobs.json or the generator there
        # would leak the golden solution downstream).
        burn_in = scenario.burn_in.prompt_text()
        for leak in ("gen_schema", "jobs.json", "regenerate", "generated", "CONTRIBUTING", "schema/jobs"):
            assert leak not in burn_in
        # It must instead explicitly restrict the touched files (see revision 2).
        assert "relay/_schema.py" in burn_in
        assert "no other files" in burn_in.lower()

    def test_burn_in_revision_documented_before_any_arm_could_have_run(self, scenario):
        """Requirement: the scenario must record that Pilot 0 (Sonnet 5) avoided the
        known-bad approach with zero A/B arms, and that the burn-in prompt was made
        deterministic before any control/treatment outcome existed to react to."""
        meta = json.loads((SCENARIO_DIR / "metadata.json").read_text(encoding="utf-8"))
        revision = meta.get("revision")
        assert revision is not None and revision["version"] >= 2
        reason = revision["reason"].lower()
        for term in ("pilot 0", "sonnet", "zero", "before any"):
            assert term in reason, term
        assert any("pilot 0" in n.lower() or "revision" in n.lower() for n in meta["notes"])
        # The documentation lives only in scenario metadata/README -- it must
        # never leak into either prompt actually sent to a model.
        for text in (scenario.burn_in.prompt_text(), scenario.evaluation.prompt_text()):
            assert "sonnet" not in text.lower() and "pilot 0" not in text.lower()

    def test_rejects_short_or_missing_commit(self, tmp_path):
        copy = tmp_path / "1_previously_failed_approach"
        shutil.copytree(SCENARIO_DIR, copy)
        meta = json.loads((copy / "metadata.json").read_text(encoding="utf-8"))
        meta["repository"]["base_commit"] = "d8c94df"
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert exc.value.code == "scenario_invalid"
        assert "40-hex" in exc.value.message

    def test_rejects_unknown_criterion_step(self, tmp_path):
        copy = tmp_path / "1_previously_failed_approach"
        shutil.copytree(SCENARIO_DIR, copy)
        meta = json.loads((copy / "metadata.json").read_text(encoding="utf-8"))
        meta["known_failed_approach"]["criteria"][0]["step"] = "nope"
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert exc.value.code == "scenario_invalid"


# ---------------------------------------------------------------------------
# Workspaces: exact commit, isolation, history preservation
# ---------------------------------------------------------------------------


class TestWorkspaces:
    def test_seed_builds_to_the_pinned_commit(self, scenario, source):
        assert source.base_commit == scenario.repository.base_commit
        assert workspace.head_commit(Path(source.location)) == scenario.repository.base_commit

    def test_seed_commit_mismatch_is_loud_and_substitutes_nothing(self, scenario, tmp_path):
        wrong = "0" * 40
        with pytest.raises(BenchmarkError) as exc:
            workspace.materialize_source(
                "seed", wrong, seed_dir=scenario.repository.seed_path, url=None, bench_root=tmp_path,
            )
        assert exc.value.code == "seed_commit_mismatch"
        assert exc.value.details["built"] == scenario.repository.base_commit

    def test_git_source_with_unavailable_commit_fails(self, source, tmp_path):
        bogus = SourceRepo(kind="git", location=source.location, base_commit="f" * 40)
        with pytest.raises(BenchmarkError) as exc:
            workspace.create_workspace(bogus, tmp_path / "ws", label="X")
        assert exc.value.code == "commit_unavailable"

    def test_clone_failure_is_loud(self, tmp_path):
        bogus = SourceRepo(kind="git", location=str(tmp_path / "does-not-exist"), base_commit="a" * 40)
        with pytest.raises(BenchmarkError) as exc:
            workspace.create_workspace(bogus, tmp_path / "ws", label="X")
        assert exc.value.code == "clone_failed"

    def test_arms_are_independent_clones_at_the_same_commit(self, source, tmp_path):
        a = workspace.create_workspace(source, tmp_path / "A", label="A")
        b = workspace.create_workspace(source, tmp_path / "B", label="B")
        workspace.assert_isolated(a, b)
        assert workspace.head_commit(a) == workspace.head_commit(b) == source.base_commit
        (a / "relay" / "queue.py").write_text("changed in A", encoding="utf-8")
        (a / ".openshard").mkdir()
        (a / ".openshard" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
        assert "changed in A" not in (b / "relay" / "queue.py").read_text(encoding="utf-8")
        assert not (b / ".openshard").exists()
        assert workspace.status_lines(b, ignored=True) == []

    def test_nested_or_identical_workspaces_are_rejected(self, source, tmp_path):
        a = workspace.create_workspace(source, tmp_path / "A", label="A")
        with pytest.raises(BenchmarkError) as exc:
            workspace.assert_isolated(a, a)
        assert exc.value.code == "isolation_violated"
        nested = a / "inner"
        nested.mkdir()
        with pytest.raises(BenchmarkError):
            workspace.assert_isolated(a, nested)

    def test_reset_keeps_openshard_and_removes_everything_else(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "B", label="B")
        history = ws / ".openshard" / "runs.jsonl"
        history.parent.mkdir()
        history.write_text('{"task": "prior attempt"}\n', encoding="utf-8")
        (ws / ".openshard" / "claude_sessions").mkdir()
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        (ws / "relay" / "queue.py").write_text("hand edited", encoding="utf-8")
        (ws / "relay" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
        (ws / "relay" / "__pycache__").mkdir()
        (ws / "relay" / "__pycache__" / "queue.cpython-311.pyc").write_bytes(b"\x00")
        before = workspace.file_sha256(history)

        report = workspace.reset_code_preserving_history(ws, source.base_commit)

        assert report.to_dict()["history_preserved"] is True
        assert workspace.file_sha256(history) == before
        assert history.read_text(encoding="utf-8") == '{"task": "prior attempt"}\n'
        assert workspace.head_commit(ws) == source.base_commit
        assert not (ws / ".claude").exists()
        assert not (ws / "relay" / "new_module.py").exists()
        assert not (ws / "relay" / "__pycache__").exists()
        assert "hand edited" not in (ws / "relay" / "queue.py").read_text(encoding="utf-8")
        assert all(line[3:].startswith(".openshard/") for line in workspace.status_lines(ws, ignored=True))
        assert ".claude" in report.removed_local_state

    def test_reset_without_history_refuses(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "B", label="B")
        with pytest.raises(BenchmarkError) as exc:
            workspace.reset_code_preserving_history(ws, source.base_commit)
        assert exc.value.code == "history_missing"

    def test_changed_paths_ignore_local_state_dirs(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "A", label="A")
        (ws / "relay" / "queue.py").write_text("edited", encoding="utf-8")
        (ws / "docs.md").write_text("new", encoding="utf-8")
        (ws / ".openshard").mkdir()
        (ws / ".openshard" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        changed = workspace.changed_paths(ws, source.base_commit)
        assert changed.modified == ["relay/queue.py"]
        assert changed.added == ["docs.md"]
        assert changed.deleted == []


# ---------------------------------------------------------------------------
# Harness: argv, environment, stream parsing, timeouts
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> HarnessConfig:
    kwargs = dict(claude_argv=FAKE_ARGV, model="fake-model-1", max_turns=7, timeout_seconds=60.0)
    kwargs.update(overrides)
    return HarnessConfig(**kwargs)


class TestHarness:
    def test_argv_carries_every_isolation_flag(self, tmp_path):
        argv = build_argv(_cfg(max_budget_usd=2.5), tmp_path / "mcp.json")
        joined = " ".join(argv)
        assert "-p" in argv and "--output-format stream-json" in joined and "--verbose" in argv
        assert "--model fake-model-1" in joined
        assert "--strict-mcp-config" in argv and f"--mcp-config {tmp_path / 'mcp.json'}" in joined
        assert "--dangerously-skip-permissions" in argv
        assert "--max-turns 7" in joined and "--max-budget-usd 2.5" in joined
        assert "--setting-sources project,local" in joined
        assert "--disable-slash-commands" in argv and "--no-session-persistence" in argv

    def test_scrubbed_env_drops_session_and_openshard_vars(self):
        base = {
            "PATH": "/usr/bin", "HOME": "/home/x", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PROJECT_DIR": "/elsewhere", "OPENSHARD_CAPTURE_DISABLE": "1", "OPENSHARD_HOME": "/h",
            "CLAUDE_CODE_OAUTH_TOKEN": "keep-me", "ANTHROPIC_API_KEY": "keep-too",
        }
        env, removed = scrubbed_env(base, path_prepend=["/bench/bin"], overrides={"OPENSHARD_HOME": "/private"})
        assert removed == ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR", "OPENSHARD_CAPTURE_DISABLE",
                           "OPENSHARD_HOME"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep-me" and env["ANTHROPIC_API_KEY"] == "keep-too"
        assert env["PATH"].startswith("/bench/bin" + os.pathsep)
        assert env["OPENSHARD_HOME"] == "/private"
        assert "CLAUDECODE" not in env

    def test_parse_stream_extracts_facts_and_never_keeps_thinking(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x", "tools": ["Bash"],
                        "mcp_servers": [{"name": "openshard", "status": "connected"}], "claude_code_version": "2.1.0",
                        "permissionMode": "bypassPermissions"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-x-20260101", "content": [
                {"type": "thinking", "thinking": FORBIDDEN},
                {"type": "tool_use", "id": "t1", "name": "mcp__openshard__relevant_context", "input": {"task": "add priority"}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "python -m unittest"}},
            ]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "{\"matches\": [{\"shard_id\": \"shard-1\"}]}"}]},
                {"type": "tool_result", "tool_use_id": "t2", "content": "OK", "is_error": False},
            ]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "duration_ms": 1234,
                        "total_cost_usd": 0.05, "usage": {"input_tokens": 10, "output_tokens": 5},
                        "modelUsage": {"claude-x-20260101": {"costUSD": 0.05}}, "result": "done",
                        "permission_denials": []}),
        ]
        parsed = parse_stream(lines)
        assert parsed.session_id == "s1" and parsed.model_init == "claude-x"
        assert parsed.models_observed == ["claude-x-20260101"]
        assert parsed.mcp_servers == [{"name": "openshard", "status": "connected"}]
        assert parsed.tool_counts() == {"Bash": 1, "mcp__openshard__relevant_context": 1}
        assert parsed.openshard_calls[0].result_text is not None and "shard-1" in parsed.openshard_calls[0].result_text
        assert parsed.tool_calls[1].result_is_error is False and parsed.tool_calls[1].result_text is None
        assert parsed.result_subtype == "success" and parsed.num_turns == 4 and parsed.total_cost_usd == 0.05
        assert parsed.saw_result and parsed.lines_unparsed == 0
        assert FORBIDDEN not in json.dumps(parsed.to_dict())

    def test_parse_stream_counts_garbage_and_reports_no_result(self):
        parsed = parse_stream(["not json", "[1,2]", json.dumps({"type": "system", "subtype": "init", "session_id": "s"})])
        assert parsed.lines_unparsed == 2 and parsed.saw_result is False and parsed.session_id == "s"
        assert parsed.result_subtype is None and parsed.total_cost_usd is None

    def test_timeout_kills_the_agent_and_is_recorded(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        env, removed = scrubbed_env(_fake_env(PR13_FAKE_MODE="sleep", PR13_FAKE_SLEEP="30"))
        mcp = write_mcp_config(tmp_path / "mcp.json", {})
        run = run_agent(_cfg(timeout_seconds=2.0), prompt="hi", cwd=ws, env=env, env_removed=removed,
                        mcp_config_path=mcp, out_dir=tmp_path / "out")
        assert run.timed_out is True and run.exit_status == "timeout"
        assert run.agent_reported_completion is None
        assert run.parsed.session_id is not None and run.parsed.saw_result is False
        assert Path(run.stdout_path).exists() and (tmp_path / "out" / "prompt.txt").read_text(encoding="utf-8") == "hi"

    def test_crash_exit_code_is_recorded_not_repaired(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        env, removed = scrubbed_env(_fake_env(PR13_FAKE_MODE="crash"))
        mcp = write_mcp_config(tmp_path / "mcp.json", {})
        run = run_agent(_cfg(), prompt="hi", cwd=ws, env=env, env_removed=removed, mcp_config_path=mcp,
                        out_dir=tmp_path / "out")
        assert run.exit_code == 3 and run.exit_status == "exited_3" and run.timed_out is False
        assert run.parsed.saw_result is False and run.agent_reported_completion is None
        assert "crashing" in Path(run.stderr_path).read_text(encoding="utf-8")

    def test_missing_binary_raises_instead_of_falling_back(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        env, removed = scrubbed_env(_fake_env())
        mcp = write_mcp_config(tmp_path / "mcp.json", {})
        with pytest.raises(BenchmarkError) as exc:
            run_agent(_cfg(claude_argv=("definitely-not-a-real-claude-binary-zz",)), prompt="hi", cwd=ws, env=env,
                      env_removed=removed, mcp_config_path=mcp, out_dir=tmp_path / "out")
        assert exc.value.code == "claude_launch_failed"


# ---------------------------------------------------------------------------
# Verification and the known-failed-approach criterion
# ---------------------------------------------------------------------------


class TestVerification:
    def test_base_commit_fails_feature_tests_but_passes_generator_check(self, scenario, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        result = run_verification(scenario.verification, workspace=ws, scenario_dir=scenario.scenario_dir,
                                  out_dir=tmp_path / "v")
        assert result.passed is False
        assert result.step("generated_schema_in_sync").passed is True
        assert result.step("hidden_tests").passed is False
        assert (tmp_path / "v" / "hidden_tests.stderr.txt").exists()
        changed = workspace.changed_paths(ws, source.base_commit)
        verdict = evaluate_known_failed_approach(scenario.known_failed_approach, result, changed)
        assert verdict["matched"] is False  # failed, but not via the known approach

    def test_naive_edit_is_the_known_failed_approach(self, scenario, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        naive_edit(ws)
        result = run_verification(scenario.verification, workspace=ws, scenario_dir=scenario.scenario_dir,
                                  out_dir=tmp_path / "v")
        assert result.passed is False
        assert result.step("generated_schema_in_sync").passed is False
        assert result.step("generated_schema_in_sync").returncode == 1
        changed = workspace.changed_paths(ws, source.base_commit)
        assert "relay/_schema.py" in changed.modified and "schema/jobs.json" not in changed.all
        verdict = evaluate_known_failed_approach(scenario.known_failed_approach, result, changed)
        assert verdict["matched"] is True
        assert all(c["matched"] for c in verdict["criteria"])

    def test_correct_edit_passes_and_is_not_the_failed_approach(self, scenario, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        correct_edit(ws)
        result = run_verification(scenario.verification, workspace=ws, scenario_dir=scenario.scenario_dir,
                                  out_dir=tmp_path / "v")
        assert result.passed is True, result.to_dict()["steps"]
        changed = workspace.changed_paths(ws, source.base_commit)
        verdict = evaluate_known_failed_approach(scenario.known_failed_approach, result, changed)
        assert verdict["matched"] is False

    def test_step_timeout_and_missing_command_are_recorded(self, scenario, source, tmp_path):
        from evals.pr13.benchmark.config import VerificationStep

        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        steps = [
            VerificationStep(name="slow", argv=("{python}", "-c", "import time; time.sleep(30)"), timeout_seconds=1.0),
            VerificationStep(name="missing", argv=("no-such-verifier-binary-zz",)),
        ]
        result = run_verification(steps, workspace=ws, scenario_dir=scenario.scenario_dir, out_dir=tmp_path / "v")
        assert result.passed is False
        assert result.step("slow").timed_out is True and result.step("slow").passed is False
        assert result.step("missing").error is not None and result.step("missing").returncode is None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class TestResults:
    def test_run_result_serialises_unknowns_as_null(self, tmp_path):
        result = RunResult(
            scenario="s", arm="A", repeat=1, base_commit="a" * 40, workspace="w", run_dir="r", harness={},
            model_requested="m", model_reported_init=None, models_observed=[], started_at="t0", ended_at="t1",
            wall_clock_seconds=1.5, agent_exit_status="timeout", agent_exit_code=None, agent_timed_out=True,
            agent_reported_completion=None, agent_result_subtype=None, agent_num_turns=None, agent_final_text=None,
            activity={"tool_calls_total": None}, verification={"passed": False, "failed_steps": ["x"]},
            repeated_known_failure={"matched": None}, openshard={"history_present": False, "retrieval_observed": "unknown"},
            usage={"total_cost_usd": None}, artifacts={}, errors=["boom"],
        )
        result.write(tmp_path / "run.json")
        data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
        assert data["schema_version"] == 1 and data["arm"] == "A" and data["base_commit"] == "a" * 40
        assert data["agent_exit"]["timed_out"] is True and data["agent_exit"]["exit_code"] is None
        assert data["verified_success"] is False and data["repeated_known_failure"]["matched"] is None
        assert data["usage"]["total_cost_usd"] is None and data["errors"] == ["boom"]
        assert result.verified_success is False and result.repeated_known_failure_matched is None

    def test_retrieval_observed_states(self):
        no_run = retrieval_observed(None, mcp_configured=True)
        assert no_run == "unknown"


# ---------------------------------------------------------------------------
# Whole pipeline with the fake agent
# ---------------------------------------------------------------------------


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestBenchmarkPipeline:
    def test_full_run_control_repeats_failure_treatment_uses_history(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_BURN_IN="naive", PR13_FAKE_SIMULATE_HOOKS="1",
                        PR13_FAKE_MODE_CONTROL="naive", PR13_FAKE_MODE_TREATMENT="mcp")
        outcome = run_benchmark(_options(tmp_path, env))
        assert outcome.error is None, outcome.error
        assert outcome.status == "completed", outcome.validity_errors
        run_dir = outcome.run_dir
        bench = _read(run_dir / "benchmark.json")
        assert bench["status"] == "completed" and bench["error"] is None
        assert bench["source"]["base_commit"] == bench["scenario"]["repository"]["base_commit"]

        # Burn-in: a real failed attempt, captured by the production hook path.
        burn = _read(run_dir / "burn_in" / "run.json")
        assert burn["arm"] == "burn_in"
        assert burn["verified_success"] is False
        assert burn["repeated_known_failure"]["matched"] is True
        assert burn["openshard"]["history_present"] is True
        assert burn["openshard"]["captured_session_end_observed"] is True
        evidence = burn["openshard"]["expected_evidence"]
        assert evidence["present"] is True, evidence["checks"]
        shard = burn["openshard"]["history"]["shards"][0]
        assert shard["agent"] == "Claude Code (external)" and shard["verification_status"] in (None, "Not recorded")
        assert "relay/_schema.py" in shard["files"] and "schema/jobs.json" not in shard["files"]
        assert "priority" in shard["task_short"].lower()
        assert bench["burn_in"]["retrievable"] is True
        assert bench["burn_in"]["reset"]["history_preserved"] is True
        assert (run_dir / "burn_in" / "history_snapshot" / "runs.jsonl").exists()

        arms = {r.arm: r for r in outcome.arms}
        control, treatment = arms["A"], arms["B"]

        # Both arms: one openshard-shaped MCP server, connected, identical tool surface.
        expected_tools = sorted(MCP_TOOLS)
        for arm in (control, treatment):
            assert arm.openshard["mcp_configured"] is True
            assert arm.openshard["mcp_server_status"] == "connected"
            assert arm.openshard["mcp_servers_reported"] == [{"name": "openshard", "status": "connected"}]
            assert arm.openshard["mcp_surface"]["tool_names"] == expected_tools
            assert arm.openshard["mcp_surface"]["matches_expected"] is True
            assert arm.openshard["mcp_surface"]["init_event_tools_match"] is True
            assert arm.openshard["mcp_surface"]["server_name"] == "openshard"
        assert control.openshard["mcp_surface"]["fingerprint"] == treatment.openshard["mcp_surface"]["fingerprint"]
        assert bench["mcp_surface"][0]["identical"] is True

        # Control: placebo server, no history anywhere, repeats the failed approach.
        assert control.openshard["mcp_server_kind"] == "placebo"
        assert control.openshard["mcp_config"]["openshard"]["args"][0].endswith("placebo_mcp.py")
        assert control.openshard["history_present"] is False
        assert control.openshard["history_present_after_run"] is False
        assert control.openshard["retrieval_observed"] == "no"
        assert control.verified_success is False and control.repeated_known_failure_matched is True
        assert not (Path(control.workspace) / ".openshard").exists()
        assert control.errors == []

        # Treatment: production server over the preserved history, retrieval observed, verified success.
        assert treatment.openshard["mcp_server_kind"] == "production"
        assert treatment.openshard["history_present"] is True
        assert treatment.openshard["history_source"] == "burn_in_workspace_reset_in_place"
        assert treatment.openshard["mcp_config"]["openshard"]["args"][-2:] == ["--repo-path", treatment.workspace]
        assert treatment.openshard["retrieval_observed"] == "yes"
        assert treatment.openshard["tools_called"] == ["mcp__openshard__relevant_context"]
        assert treatment.openshard["burn_in_shard_surfaced_in_tool_results"] is True
        assert treatment.verified_success is True and treatment.repeated_known_failure_matched is False
        assert treatment.errors == []
        # The treatment run added nothing to the history (no hooks at evaluation time).
        assert treatment.openshard["history"]["entries"] == 1
        assert treatment.openshard["history"]["executors"] == ["claude_code_hooks"]

        # Both arms: same commit, same model request, same flags, no .claude/ config.
        for arm in (control, treatment):
            assert arm.base_commit == burn.get("base_commit")
            assert arm.model_requested == "fake-model-1" and arm.models_observed == ["fake-model-1"]
            assert arm.agent_reported_completion is True and arm.agent_exit_status == "exited_0"
            assert arm.usage["total_cost_usd"] == 0.0123 and arm.usage["cost_provenance"] == "claude_code_result_event"
            assert not (Path(arm.workspace) / ".claude").exists()
            assert arm.harness["setting_sources"] == "project,local"
        assert Path(control.workspace) != Path(treatment.workspace)
        workspace.assert_isolated(Path(control.workspace), Path(treatment.workspace))

        # Machine-readable outputs and the human comparison exist and never carry thinking.
        comparison = _read(run_dir / "comparison.json")
        assert comparison["control"]["verified_success"] == 0 and comparison["treatment"]["verified_success"] == 1
        assert comparison["control"]["repeated_known_failure"] == 1 and comparison["treatment"]["openshard_retrieval_yes"] == 1
        md = (run_dir / "comparison.md").read_text(encoding="utf-8")
        assert "Treatment B" in md and "Caveats" in md
        for path in (run_dir / "burn_in" / "run.json", run_dir / "arm_A_1" / "run.json", run_dir / "arm_B_1" / "run.json",
                     run_dir / "comparison.json", run_dir / "comparison.md", run_dir / "benchmark.json"):
            assert FORBIDDEN not in path.read_text(encoding="utf-8"), path
        # No environment values are persisted.
        assert "PR13_FAKE_MODE_TREATMENT" not in (run_dir / "benchmark.json").read_text(encoding="utf-8")

    def test_burn_in_that_passes_aborts_loudly(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_BURN_IN="correct", PR13_FAKE_SIMULATE_HOOKS="1", PR13_FAKE_MODE="correct")
        outcome = run_benchmark(_options(tmp_path, env))
        assert outcome.status == "aborted"
        assert outcome.error["code"] == "burn_in_did_not_fail"
        assert outcome.arms == []
        bench = _read(outcome.run_dir / "benchmark.json")
        assert bench["status"] == "aborted" and bench["error"]["code"] == "burn_in_did_not_fail"
        assert bench["arms"] == [] and not (outcome.run_dir / "workspaces" / "A1").exists()
        # The burn-in run itself was still recorded faithfully.
        assert _read(outcome.run_dir / "burn_in" / "run.json")["verified_success"] is True

    def test_burn_in_without_captured_history_aborts(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_BURN_IN="naive")  # hooks not delivered -> no Shard
        outcome = run_benchmark(_options(tmp_path, env, history_wait_seconds=1.0))
        assert outcome.status == "aborted" and outcome.error["code"] == "burn_in_not_captured"

    def test_burn_in_failing_differently_aborts(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_BURN_IN="noop", PR13_FAKE_SIMULATE_HOOKS="1")
        outcome = run_benchmark(_options(tmp_path, env))
        assert outcome.status == "aborted" and outcome.error["code"] == "burn_in_failed_differently"

    def test_missing_claude_cli_aborts_before_any_workspace(self, tmp_path):
        outcome = run_benchmark(_options(tmp_path, _fake_env(), claude_argv=("no-such-claude-binary-zz",)))
        assert outcome.status == "aborted" and outcome.error["code"] == "claude_cli_missing"
        assert not (outcome.run_dir / "workspaces").exists()

    def test_repeats_replicate_history_and_honour_arm_order(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_BURN_IN="naive", PR13_FAKE_SIMULATE_HOOKS="1",
                        PR13_FAKE_MODE_CONTROL="mcp", PR13_FAKE_MODE_TREATMENT="mcp")
        outcome = run_benchmark(_options(tmp_path, env, repeats=2, arm_order="BA"))
        assert outcome.status == "completed", outcome.error
        assert [(r.arm, r.repeat) for r in outcome.arms] == [("B", 1), ("A", 1), ("B", 2), ("A", 2)]
        b2 = next(r for r in outcome.arms if r.arm == "B" and r.repeat == 2)
        assert b2.openshard["history_source"] == "replicated_byte_for_byte_from_burn_in_snapshot"
        assert b2.openshard["history"]["entries"] == 1
        snapshot = workspace.file_sha256(outcome.run_dir / "burn_in" / "history_snapshot" / "runs.jsonl")
        assert workspace.file_sha256(Path(b2.workspace) / ".openshard" / "runs.jsonl") == snapshot
        burn_shard = _read(outcome.run_dir / "burn_in" / "run.json")["openshard"]["captured_shard_id"]
        for r in outcome.arms:
            # Both arms called the same tool through their own server; only the answer differs.
            assert r.openshard["tools_called"] == ["mcp__openshard__relevant_context"]
            answer = r.openshard["tool_calls"][0]["result_excerpt"] or ""
            if r.arm == "A":
                assert r.openshard["history_present"] is False and not (Path(r.workspace) / ".openshard").exists()
                assert r.openshard["mcp_server_kind"] == "placebo"
                assert '"matches": []' in answer and "No relevant prior OpenShard history" in answer
                assert burn_shard not in answer
                assert r.openshard["burn_in_shard_surfaced_in_tool_results"] is False
            else:
                assert r.openshard["mcp_server_kind"] == "production"
                assert burn_shard in answer
                assert r.openshard["burn_in_shard_surfaced_in_tool_results"] is True
            assert r.verified_success is True
        comparison = _read(outcome.run_dir / "comparison.json")
        assert comparison["control"]["runs"] == 2 and comparison["treatment"]["runs"] == 2


class TestPlaceboMcp:
    """The control arm's placebo server: same surface as production, empty answers, no history access."""

    @staticmethod
    def _tools(server) -> dict[str, dict]:
        listed = asyncio.run(server.list_tools())
        return {t.name: {"description": t.description, "input_schema": t.input_schema} for t in listed}

    @staticmethod
    def _call(server, name: str, arguments: dict):
        result = asyncio.run(server.call_tool(name, arguments))
        content, structured = result.content, result.structured_content
        text = "\n".join(getattr(c, "text", "") for c in content)
        if text.strip():
            return json.loads(text)
        # MCPServer sends an empty list as structured output only.
        return structured.get("result", structured) if isinstance(structured, dict) else structured

    def test_placebo_and_production_expose_the_same_tool_surface(self):
        from openshard.mcp import server as production

        placebo_server = placebo.build_server()
        production_server = production.build_server()
        placebo_tools = self._tools(placebo_server)
        production_tools = self._tools(production_server)
        assert sorted(placebo_tools) == sorted(production_tools) == sorted(MCP_TOOLS)
        for name in production_tools:
            assert placebo_tools[name]["description"] == production_tools[name]["description"], name
            assert placebo_tools[name]["input_schema"] == production_tools[name]["input_schema"], name
        assert placebo_server.name == production_server.name == "openshard"
        assert placebo.SERVER_INSTRUCTIONS == production.SERVER_INSTRUCTIONS
        as_probe = [{"name": n, **v} for n, v in placebo_tools.items()]
        as_probe_prod = [{"name": n, **v} for n, v in production_tools.items()]
        assert surface_fingerprint(as_probe) == surface_fingerprint(as_probe_prod)

    def test_placebo_returns_no_useful_evidence(self):
        server = placebo.build_server()
        ctx = self._call(server, "relevant_context", {"task": "Add an optional integer priority to job records"})
        assert ctx["matches"] == []
        assert "No relevant prior OpenShard history found for this task." in ctx["context_text"]
        assert ctx["task"] == "Add an optional integer priority to job records"
        blank = self._call(server, "relevant_context", {"task": "   "})
        assert blank["matches"] == [] and "No task given" in blank["context_text"]
        assert self._call(server, "recent_shards", {"limit": 5}) == []
        assert self._call(server, "search_history", {"query": "priority"}) == []
        for name, args, expected in (("get_shard", {"shard_id": "shard-20260904-0001"}, "No existing Shard found"),
                                     ("get_receipt", {"shard_id": "shard-20260904-0001"}, "No existing Shard found"),
                                     ("get_receipt", {"run_id": "run-1"}, "No run found"),
                                     ("get_receipt", {}, "requires shard_id and/or run_id")):
            with pytest.raises(Exception) as exc:
                asyncio.run(server.call_tool(name, args))
            assert expected in str(exc.value)

    def test_placebo_ignores_history_that_the_production_server_finds(self, scenario, source, tmp_path, monkeypatch):
        from openshard.history import query as history_query
        from tests.pr13_fake_claude import deliver_hooks

        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        naive_edit(ws)
        deliver_hooks(ws, "placebo-test-session", scenario.burn_in.prompt_text(), ["relay/_schema.py"])
        assert (ws / ".openshard" / "runs.jsonl").exists()
        prompt = scenario.evaluation.prompt_text()
        assert history_query.relevant_context(prompt, repo_path=ws).matches, "production must see the history"
        monkeypatch.chdir(ws)
        ctx = self._call(placebo.build_server(), "relevant_context", {"task": prompt})
        assert ctx["matches"] == []
        assert self._call(placebo.build_server(), "recent_shards", {}) == []

    def test_placebo_never_imports_openshard_or_names_history(self):
        source_text = PLACEBO_PATH.read_text(encoding="utf-8")
        body = source_text.split('"""', 2)[2]  # strip the module docstring, which describes the rules
        assert "import openshard" not in body and "from openshard" not in body
        assert "openshard.history" not in body
        # No file, process or environment access of any kind. (".openshard/runs.jsonl"
        # appears only inside the server-instructions text, which must match production.)
        for forbidden in ("open(", "Path(", "os.", "pathlib", "json.load", "subprocess", "socket", "environ"):
            assert forbidden not in body, forbidden
        check = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('placebo', r'{PLACEBO_PATH}')\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "mod.build_server()\n"
            "loaded = sorted(m for m in sys.modules if m == 'openshard' or m.startswith('openshard.'))\n"
            "print(loaded)\n"
            "raise SystemExit(1 if loaded else 0)\n"
        )
        result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "[]"

    def test_probe_sees_expected_surface_on_both_kinds(self, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        env, _ = scrubbed_env(_fake_env(), overrides={"OPENSHARD_CAPTURE_DISABLE": "1"})
        placebo_probe = probe_stdio_server(sys.executable, [str(PLACEBO_PATH)], cwd=ws, env=env)
        assert placebo_probe["tool_names"] == sorted(MCP_TOOLS) and placebo_probe["matches_expected"]
        assert placebo_probe["server_name"] == "openshard"
        openshard_exe = shutil.which("openshard", path=str(Path(sys.executable).parent))
        assert openshard_exe, "openshard console script must sit next to the test interpreter"
        production_probe = probe_stdio_server(openshard_exe, build_server_argv(ws)[1:], cwd=ws, env=env)
        assert production_probe["tool_names"] == sorted(MCP_TOOLS)
        assert production_probe["fingerprint"] == placebo_probe["fingerprint"]
        assert production_probe["instructions"] == placebo_probe["instructions"]
        require_same_surface(placebo_probe, production_probe)

    def test_surface_mismatch_is_loud(self):
        good = {"tools": [{"name": "relevant_context", "description": "d", "input_schema": {}}],
                "tool_names": ["relevant_context"], "matches_expected": False, "server_name": "openshard"}
        with pytest.raises(BenchmarkError) as exc:
            require_expected_surface(good, label="x")
        assert exc.value.code == "mcp_surface_mismatch"
        a = {"fingerprint": "1", "tools": [], "instructions": "i"}
        b = {"fingerprint": "2", "tools": [], "instructions": "i"}
        with pytest.raises(BenchmarkError):
            require_same_surface(a, b)


class TestCaptureHelpers:
    def test_expected_evidence_reports_missing_history_honestly(self, scenario, source, tmp_path):
        ws = workspace.create_workspace(source, tmp_path / "ws", label="ws")
        report = capture.evaluate_expected_evidence(scenario.expected_evidence, ws, "add priority to jobs")
        assert report["present"] is False
        assert report["checks"][0]["actual"] == 0
        assert "No relevant prior OpenShard history" in report["relevant_context_text"]
