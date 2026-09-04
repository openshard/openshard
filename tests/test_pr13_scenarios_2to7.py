"""Deterministic tests for PR13 scenarios 2-7 and the two new burn-in
mechanisms they required (``claude_wrap_chain``, ``opencode_hooks``). No
live model calls and no live OpenCode session -- see
``tests/pr13_fake_claude.py`` / ``tests/pr13_fake_opencode.py``.

``tests/test_pr13_benchmark.py`` (Scenario 1, the placebo MCP server, and
every scenario-agnostic harness module) is unchanged and still exercises
the default ``claude_hooks`` path end to end; this file covers only what
is new for scenarios 2-7.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evals.pr13.benchmark import capture, workspace
from evals.pr13.benchmark.config import BURN_IN_CAPTURE_KINDS, load_scenario
from evals.pr13.benchmark.errors import BenchmarkError
from evals.pr13.benchmark.runner import BenchmarkOptions, run_benchmark
from openshard.history import query as history_query

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO_ROOT / "evals" / "pr13" / "scenarios"
FAKE_CLAUDE = Path(__file__).resolve().parent / "pr13_fake_claude.py"
FAKE_OPENCODE = Path(__file__).resolve().parent / "pr13_fake_opencode.py"
FAKE_ARGV = (sys.executable, str(FAKE_CLAUDE))

SCENARIO_2 = SCENARIOS_DIR / "2_multi_attempt_chronology"
SCENARIO_3 = SCENARIOS_DIR / "3_prior_engineering_constraint"
SCENARIO_4 = SCENARIOS_DIR / "4_stale_historical_evidence"
SCENARIO_5 = SCENARIOS_DIR / "5_irrelevant_nearby_history"
SCENARIO_6 = SCENARIOS_DIR / "6_genuinely_novel_no_history"
SCENARIO_7 = SCENARIOS_DIR / "7_cross_agent_handoff"
ALL_NEW_SCENARIOS = (SCENARIO_2, SCENARIO_3, SCENARIO_4, SCENARIO_5, SCENARIO_6, SCENARIO_7)

GOLDEN_LEAK_TERMS = ("gen_schema", "jobs.json", "_schema.py", "regenerate", "generated", "CONTRIBUTING")


def _fake_env(**modes: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("PR13_FAKE"):
            del env[key]
    env.update(modes)
    return env


def _options(scenario_dir: Path, tmp_path: Path, env: dict[str, str], **overrides) -> BenchmarkOptions:
    kwargs = dict(
        scenario_dir=scenario_dir, out_dir=tmp_path / "results", model="fake-model-1", claude_argv=FAKE_ARGV,
        start_capture_service=False, history_wait_seconds=8.0, run_id="run", env_base=env,
    )
    kwargs.update(overrides)
    return BenchmarkOptions(**kwargs)


# ---------------------------------------------------------------------------
# Config: every new scenario loads and leaks nothing
# ---------------------------------------------------------------------------


class TestScenarioConfigs:
    @pytest.mark.parametrize("scenario_dir", ALL_NEW_SCENARIOS, ids=lambda p: p.name)
    def test_loads_and_evaluation_prompt_never_leaks(self, scenario_dir):
        sc = load_scenario(scenario_dir)
        assert sc.id == scenario_dir.name
        assert len(sc.repository.base_commit) == 40
        assert sc.burn_in.capture in BURN_IN_CAPTURE_KINDS
        evaluation = sc.evaluation.prompt_text()
        for leak in GOLDEN_LEAK_TERMS:
            assert leak not in evaluation, (scenario_dir.name, leak)

    def test_scenario_2_is_a_wrap_chain_with_two_stages(self):
        sc = load_scenario(SCENARIO_2)
        assert sc.burn_in.capture == "claude_wrap_chain"
        assert len(sc.burn_in.wrap_stages) == 2
        # Stage 1 (the known-bad attempt) must not already know the fix.
        stage1 = sc.burn_in.wrap_stages[0].prompt_text()
        for leak in GOLDEN_LEAK_TERMS:
            assert leak not in stage1
        # Stage 2 (the real correction, attempt 2 of the same Shard) is
        # allowed to name the fix -- that IS the chronology evidence; only
        # the *evaluation* prompt (checked above for every scenario) must
        # never see it. Note: this scenario does not test PR11
        # RecoveryObservation (see its own metadata/README) -- this test
        # only checks the wrap-chain config shape and non-leakage.

    def test_scenario_7_is_an_opencode_burn_in(self):
        sc = load_scenario(SCENARIO_7)
        assert sc.burn_in.capture == "opencode_hooks"
        assert sc.burn_in.agent == "opencode"
        burn_in = sc.burn_in.prompt_text()
        # "_schema.py" is the deliberately-named known-bad file (same as
        # Scenario 1); only the correct mechanism must stay unnamed.
        for leak in ("gen_schema", "jobs.json", "regenerate", "generated", "CONTRIBUTING"):
            assert leak not in burn_in
        assert "relay/_schema.py" in burn_in

    def test_harm_test_scenarios_do_not_gate_on_burn_in_correctness(self):
        for scenario_dir in (SCENARIO_4, SCENARIO_5, SCENARIO_6):
            sc = load_scenario(scenario_dir)
            assert sc.burn_in.require_verification_failed is False
            assert sc.burn_in.require_known_failed_approach is False


# ---------------------------------------------------------------------------
# config.py: the new fields are additive and validated
# ---------------------------------------------------------------------------


def _copy_scenario_with_working_seed(scenario_dir: Path, dest: Path) -> Path:
    """Copy one scenario dir, repointing its (relative) shared-seed path to
    the real shared seed's absolute location, so the copy alone still loads."""
    shutil.copytree(scenario_dir, dest)
    meta_path = dest / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["id"] = dest.name
    if meta["repository"]["kind"] == "seed":
        meta["repository"]["path"] = str((SCENARIOS_DIR / "_shared" / "relay_seed").resolve())
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return dest


class TestWrapChainConfigValidation:
    def test_capture_rejects_unknown_value(self, tmp_path):
        copy = _copy_scenario_with_working_seed(SCENARIO_2, tmp_path / "2_multi_attempt_chronology")
        meta = json.loads((copy / "metadata.json").read_text(encoding="utf-8"))
        meta["burn_in"]["capture"] = "not_a_real_mechanism"
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert exc.value.code == "scenario_invalid"

    def test_wrap_chain_requires_at_least_two_stages(self, tmp_path):
        copy = _copy_scenario_with_working_seed(SCENARIO_2, tmp_path / "2_multi_attempt_chronology")
        meta = json.loads((copy / "metadata.json").read_text(encoding="utf-8"))
        meta["burn_in"]["wrap_stages"] = meta["burn_in"]["wrap_stages"][:1]
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert exc.value.code == "scenario_invalid"
        assert "at least 2" in exc.value.message

    def test_opencode_hooks_requires_a_provider_model_agent_model(self, tmp_path):
        """Regression for the live Scenario 7 abort: the Claude --model must never
        reach OpenCode, so the scenario must carry OpenCode's own model id."""
        copy = _copy_scenario_with_working_seed(SCENARIO_7, tmp_path / "7_cross_agent_handoff")
        meta = json.loads((copy / "metadata.json").read_text(encoding="utf-8"))
        del meta["burn_in"]["agent_model"]
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert exc.value.code == "scenario_invalid" and "agent_model" in exc.value.message
        meta["burn_in"]["agent_model"] = "sonnet"  # a Claude Code alias, not provider/model
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert "provider/model" in exc.value.message
        sc = load_scenario(SCENARIO_7)
        assert "/" in sc.burn_in.agent_model

    def test_opencode_hooks_requires_an_agent(self, tmp_path):
        copy = _copy_scenario_with_working_seed(SCENARIO_7, tmp_path / "7_cross_agent_handoff")
        meta = json.loads((copy / "metadata.json").read_text(encoding="utf-8"))
        del meta["burn_in"]["agent"]
        (copy / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(BenchmarkError) as exc:
            load_scenario(copy)
        assert exc.value.code == "scenario_invalid"


# ---------------------------------------------------------------------------
# Scenario 2: claude_wrap_chain, end to end
# ---------------------------------------------------------------------------


class TestScenario2WrapChain:
    def test_full_run_two_real_wrap_linked_attempts_then_correction(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_CONTROL="reset_retries", PR13_FAKE_MODE_TREATMENT="reset_retries")
        outcome = run_benchmark(_options(SCENARIO_2, tmp_path, env))
        assert outcome.error is None, outcome.error
        assert outcome.status == "completed", outcome.validity_errors

        burn = outcome.burn_in
        assert burn is not None
        # Stage 1 (the known failed approach) gates known_failed_approach;
        # the FINAL (stage 2, corrected) state is what verified_success
        # reports. This proves same-Shard multi-attempt chronology, NOT a
        # PR11 RecoveryObservation (neither attempt carries a
        # verification_passed value -- see the scenario's own README).
        assert burn.repeated_known_failure_matched is True
        assert burn.verified_success is True
        assert burn.openshard["history_source"] == "claude_wrap_chain_this_session"
        assert burn.openshard["shard_attempt_count"] == 2
        stage_verifications = burn.openshard["stage_verifications"]
        assert len(stage_verifications) == 2
        assert stage_verifications[0]["verification"]["passed"] is False
        assert stage_verifications[1]["verification"]["passed"] is True
        assert burn.openshard["wrap_stages"][0]["attempt_number"] == 1
        assert burn.openshard["wrap_stages"][1]["attempt_number"] == 2
        assert burn.openshard["wrap_stages"][0]["shard_id"] == burn.openshard["wrap_stages"][1]["shard_id"]
        # No hooks, no capture service, no MCP server for this mechanism.
        assert burn.openshard.get("hooks") is None
        assert not (Path(burn.workspace) / ".claude").exists()

        history = capture.history_summary(Path(burn.workspace))
        assert history["executors"] == ["claude_code_wrap"]
        shard = history["shards"][0]
        assert shard["agent"] == "Claude Code (external)"
        assert "purge" in shard["task_short"].lower()

        # The documented product gap, proven rather than only asserted in
        # prose: this same multi-attempt Shard never produces a PR11
        # RecoveryObservation, because wrap-captured attempts never carry a
        # verification_passed value.
        ctx = history_query.relevant_context("purge queue jobs", repo_path=Path(burn.workspace))
        matched = next((m for m in ctx.matches if m.shard.shard_id == burn.openshard["shard_id"]), None)
        assert matched is not None, "the multi-attempt shard must still be retrievable"
        assert matched.recovery is None
        assert len(matched.attempts) == 2  # the chronology this scenario DOES verify

        arms = {r.arm: r for r in outcome.arms}
        # Reset removed the burn-in code entirely; both arms solve a
        # different task (reset-retries) and this scenario forces no trap.
        assert arms["A"].verified_success is True
        assert arms["B"].verified_success is True
        assert not (Path(arms["A"].workspace) / ".openshard").exists()
        assert workspace.file_sha256(Path(arms["B"].workspace) / ".openshard" / "runs.jsonl") is not None

    def test_final_stage_must_pass_or_the_run_aborts(self, tmp_path):
        """If stage 2 (the intended fix) never actually fixes it, nothing is
        substituted -- the whole run aborts rather than reporting a false correction."""
        env = _fake_env(PR13_FAKE_WRAP_STAGE2_NOOP="1")
        outcome = run_benchmark(_options(SCENARIO_2, tmp_path, env, run_id="run2"))
        assert outcome.status == "aborted"
        assert outcome.error["code"] == "burn_in_correction_not_confirmed"
        assert outcome.arms == []

    def test_stage_1_not_matching_known_failed_approach_aborts(self, tmp_path):
        """If stage 1 never actually reproduces the known-bad approach (it
        implements purge correctly from the start), the precondition this
        scenario is built on was never met, so the run aborts rather than
        proceeding as if a failure had been observed."""
        env = _fake_env(PR13_FAKE_WRAP_STAGE1_CORRECT="1")
        outcome = run_benchmark(_options(SCENARIO_2, tmp_path, env, run_id="run3"))
        assert outcome.status == "aborted"
        assert outcome.error["code"] == "burn_in_did_not_fail"
        assert outcome.arms == []


# ---------------------------------------------------------------------------
# Scenario 7: opencode_hooks, end to end
# ---------------------------------------------------------------------------


def _install_fake_opencode_on_path(monkeypatch, tmp_path) -> None:
    """Point `shutil.which("opencode")` at a shim that runs our fake, exactly
    the way a real OpenCode install would sit on PATH. Windows needs a
    .cmd shim since subprocess resolves extensions via PATHEXT."""
    shim_dir = tmp_path / "opencode_shim"
    shim_dir.mkdir(exist_ok=True)
    if os.name == "nt":
        shim = shim_dir / "opencode.cmd"
        shim.write_text(f'@"{sys.executable}" "{FAKE_OPENCODE}" %*\n', encoding="utf-8")
    else:
        shim = shim_dir / "opencode"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_OPENCODE}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))


class TestScenario7CrossAgentHandoff:
    def test_missing_opencode_cli_aborts_before_any_workspace(self, tmp_path, monkeypatch):
        # Keep git/claude(fake)/openshard reachable; only hide the real
        # `opencode` this machine has installed, by dropping the PATH entry
        # that resolves it.
        real_opencode = shutil.which("opencode")
        assert real_opencode, "this test assumes opencode is normally on PATH"
        opencode_dir = str(Path(real_opencode).parent)
        kept = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != opencode_dir]
        monkeypatch.setenv("PATH", os.pathsep.join(kept))
        assert shutil.which("opencode") is None, "test setup failed to hide opencode"
        env = _fake_env()
        outcome = run_benchmark(_options(SCENARIO_7, tmp_path, env))
        assert outcome.status == "aborted"
        assert outcome.error["code"] == "opencode_cli_missing"

    def test_agent_process_failure_is_not_mislabelled_as_failed_differently(self, tmp_path, monkeypatch):
        """Reproduces the live Scenario 7 abort with the fake in its faithful
        failure mode (unknown model -> two error events, idle-not-deleted
        capture, zero edits, exit 1). The captured task-only Shard must not
        reach the known-failed-approach check; the run aborts as an agent
        failure, naming OpenCode's own error."""
        _install_fake_opencode_on_path(monkeypatch, tmp_path)
        env = _fake_env(PR13_FAKE_OPENCODE_MODE="fail_model")
        outcome = run_benchmark(_options(SCENARIO_7, tmp_path, env))
        assert outcome.status == "aborted"
        assert outcome.error["code"] == "burn_in_agent_failed"
        assert outcome.error["code"] != "burn_in_failed_differently"
        assert "Model not found" in outcome.error["message"]
        assert outcome.error["details"]["agent_model"] == "openrouter/anthropic/claude-sonnet-5"
        assert outcome.arms == []

    def test_scenario_7_is_marked_inconclusive_with_factual_history(self):
        """PR13 closure: Scenario 7 recorded no valid result and must say so,
        with the three live attempts' actual causes, without ceasing to load."""
        sc = load_scenario(SCENARIO_7)  # the extra status keys must not break loading
        meta = json.loads((SCENARIO_7 / "metadata.json").read_text(encoding="utf-8"))
        assert meta["status"] == "inconclusive"
        reason = meta["status_reason"].lower()
        for term in ("v1", "invalid model", "v2", "insufficient credits", "v3", "passed verification",
                     "no valid cross-agent", "unmeasured"):
            assert term in reason, term
        assert sc.id == "7_cross_agent_handoff"

    def test_agent_exit_0_but_no_attempt_is_not_mislabelled(self, tmp_path, monkeypatch):
        """Reproduces the v2 live abort: OpenCode exits 0 but its model call
        failed (402 insufficient credits), so it made no tool calls and no
        edits. The captured task-only Shard must not reach the
        known-failed-approach check; the run aborts as an agent failure that
        names OpenCode's own error, never burn_in_failed_differently."""
        _install_fake_opencode_on_path(monkeypatch, tmp_path)
        env = _fake_env(PR13_FAKE_OPENCODE_MODE="api_error")
        outcome = run_benchmark(_options(SCENARIO_7, tmp_path, env))
        assert outcome.status == "aborted"
        assert outcome.error["code"] == "burn_in_agent_failed"
        assert outcome.error["code"] != "burn_in_failed_differently"
        assert "no attempt" in outcome.error["message"]
        assert "Insufficient credits" in outcome.error["message"]
        assert outcome.error["details"]["opencode_exit_code"] == 0
        assert outcome.arms == []

    def test_full_run_opencode_burn_in_claude_evaluates(self, tmp_path, monkeypatch):
        _install_fake_opencode_on_path(monkeypatch, tmp_path)
        env = _fake_env(PR13_FAKE_OPENCODE_MODE="naive", PR13_FAKE_MODE_CONTROL="tags_naive",
                        PR13_FAKE_MODE_TREATMENT="tags_correct")
        outcome = run_benchmark(_options(SCENARIO_7, tmp_path, env))
        assert outcome.error is None, outcome.error
        assert outcome.status == "completed", outcome.validity_errors

        burn = outcome.burn_in
        assert burn is not None
        # OpenCode got its own provider/model id, never the Claude --model.
        assert burn.model_requested == "openrouter/anthropic/claude-sonnet-5"
        argv = burn.openshard["opencode_run"]["argv"]
        assert argv[argv.index("--model") + 1] == "openrouter/anthropic/claude-sonnet-5"
        assert "fake-model-1" not in argv
        assert burn.repeated_known_failure_matched is True
        assert burn.verified_success is False
        assert burn.openshard["burn_in_agent"] == "opencode"
        assert burn.openshard["history_source"] == "opencode_plugin_this_session"
        history = capture.history_summary(Path(burn.workspace))
        assert history["executors"] == ["opencode_plugin"]
        shard = history["shards"][0]
        assert shard["agent"] == "OpenCode (external)"
        assert "tags" in shard["task_short"].lower()
        assert "relay/_schema.py" in shard["files"]
        assert "schema/jobs.json" not in shard["files"]

        arms = {r.arm: r for r in outcome.arms}
        # Both arms are evaluated by (fake) Claude Code, not OpenCode.
        for r in outcome.arms:
            assert r.model_requested == "fake-model-1"
        assert arms["A"].verified_success is False and arms["A"].repeated_known_failure_matched is True
        assert arms["B"].verified_success is True and arms["B"].repeated_known_failure_matched is False

    def test_treatment_can_retrieve_the_opencode_captured_shard(self, tmp_path, monkeypatch):
        """relevant_context is agent-agnostic: a Shard captured by OpenCode
        must be just as retrievable to a Claude Code evaluation session as
        one captured by Claude Code's own hooks."""
        _install_fake_opencode_on_path(monkeypatch, tmp_path)
        env = _fake_env(PR13_FAKE_OPENCODE_MODE="naive", PR13_FAKE_MODE_CONTROL="tags_naive",
                        PR13_FAKE_MODE_TREATMENT="mcp")
        outcome = run_benchmark(_options(SCENARIO_7, tmp_path, env))
        assert outcome.error is None, outcome.error
        treatment = next(r for r in outcome.arms if r.arm == "B")
        assert treatment.openshard["retrieval_observed"] == "yes"
        assert treatment.openshard["burn_in_shard_surfaced_in_tool_results"] is True


# ---------------------------------------------------------------------------
# Scenarios 5 & 6: relevance scoring, verified directly against real history
# ---------------------------------------------------------------------------


def _synthetic_history(tmp_path: Path, *, task: str, files: list[str], shard_id: str) -> Path:
    entry = {
        "schema_version": 6, "timestamp": "2026-09-04T10:00:00Z", "task": task[:300],
        "execution_model": "unknown", "executor": "claude_code_hooks",
        "shard_id": shard_id, "run_id": f"run-{shard_id}", "attempt_number": 1,
        "files_detail": [{"path": p, "change_type": "update", "summary": "hooks"} for p in files],
        "verification_attempted": False, "verification_passed": None,
        "summary": "Claude Code session.",
        "capture": {"source": "claude_code_hooks", "agent": "claude_code", "session_id": "s1",
                    "status": "ended", "session_end_observed": True, "task_status": "turn_completed"},
    }
    ws = tmp_path / "history_only"
    (ws / ".openshard").mkdir(parents=True)
    with open(ws / ".openshard" / "runs.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return ws


class TestRelevanceScoring:
    def test_scenario_5_burn_in_scores_the_minimum_nonzero_score(self, tmp_path):
        sc5 = load_scenario(SCENARIO_5)
        ws = _synthetic_history(tmp_path, task=sc5.burn_in.prompt_text(), files=["relay/cli.py"], shard_id="s5")
        ctx = history_query.relevant_context(sc5.evaluation.prompt_text(), repo_path=ws)
        assert len(ctx.matches) == 1
        assert ctx.matches[0].score == 2
        assert ctx.matches[0].signals == ["task overlap: relay"]

    def test_scenario_6_burn_in_is_not_retrieved_at_all(self, tmp_path):
        sc6 = load_scenario(SCENARIO_6)
        ws = _synthetic_history(tmp_path, task=sc6.burn_in.prompt_text(), files=["README.md"], shard_id="s6")
        ctx = history_query.relevant_context(sc6.evaluation.prompt_text(), repo_path=ws)
        assert ctx.matches == []
        assert "No relevant prior OpenShard history found" in ctx.context_text


# ---------------------------------------------------------------------------
# Scenarios 3 & 4: hidden-test verification, checked against real (hand-built)
# buggy/correct implementations rather than assumed
# ---------------------------------------------------------------------------


def _fresh_seed_ws(tmp_path: Path) -> Path:
    ws = tmp_path / f"ws-{len(list(tmp_path.glob('ws-*')))}"
    shutil.copytree(SCENARIOS_DIR / "_shared" / "relay_seed", ws)
    return ws


def _run_hidden(scenario_dir: Path, ws: Path) -> subprocess.CompletedProcess:
    hidden = scenario_dir / "verification" / "hidden_tests"
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(hidden), "-t", str(hidden), "-v"],
        cwd=ws, capture_output=True, text=True, timeout=60,
    )


class TestScenario6ZeroMatchGate:
    """Regression for the live abort `burn_in_evidence_missing`: the generic
    expected-evidence check's retrievability sub-check is positive-only, so
    a by-design zero-match scenario must not gate on it."""

    def _captured_burn_in(self, tmp_path: Path):
        from tests.pr13_fake_claude import deliver_hooks, readme_license_edit

        sc = load_scenario(SCENARIO_6)
        src = workspace.materialize_source(
            "seed", sc.repository.base_commit, seed_dir=sc.repository.seed_path, url=None, bench_root=tmp_path,
        )
        ws = workspace.create_workspace(src, tmp_path / "B1", label="B1")
        readme_license_edit(ws)
        deliver_hooks(ws, "s6-session", sc.burn_in.prompt_text(), ["README.md"])
        return sc, ws

    def test_generic_check_reads_exactly_as_documented(self, tmp_path):
        sc, ws = self._captured_burn_in(tmp_path)
        evidence = capture.evaluate_expected_evidence(sc.expected_evidence, ws, sc.evaluation.prompt_text())
        by_name = {c["check"]: c for c in evidence["checks"]}
        # Real, correctly-shaped history was captured...
        assert by_name["min_shards"]["ok"] is True
        assert by_name["shard_matching_task_and_files"]["ok"] is True
        # ...and it is, by design, not retrievable for the evaluation task.
        assert by_name["retrievable_via_relevant_context"]["ok"] is False
        assert by_name["retrievable_via_relevant_context"]["matches"] == []
        assert evidence["present"] is False
        # Which is why the scenario must not gate on the generic flag.
        assert sc.burn_in.require_expected_evidence is False

    def test_real_captured_history_yields_zero_matches(self, tmp_path):
        sc, ws = self._captured_burn_in(tmp_path)
        assert capture.history_summary(ws)["entries"] == 1
        ctx = history_query.relevant_context(sc.evaluation.prompt_text(), repo_path=ws)
        assert ctx.matches == []
        assert "No relevant prior OpenShard history found" in ctx.context_text

    def test_full_run_completes_with_history_present_but_unretrievable(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_BURN_IN="readme_license", PR13_FAKE_SIMULATE_HOOKS="1",
                        PR13_FAKE_MODE_CONTROL="watch", PR13_FAKE_MODE_TREATMENT="watch")
        outcome = run_benchmark(_options(SCENARIO_6, tmp_path, env))
        assert outcome.error is None, outcome.error
        assert outcome.status == "completed", outcome.validity_errors

        burn = outcome.burn_in
        assert burn is not None
        assert burn.openshard["history"]["entries"] == 1
        assert burn.openshard["history"]["shards"][0]["files"] == ["README.md"]
        assert burn.openshard["expected_evidence"]["present"] is False  # documented read-out

        arms = {r.arm: r for r in outcome.arms}
        treatment = arms["B"]
        assert treatment.openshard["history_present"] is True
        assert treatment.openshard["history"]["entries"] == 1
        ctx = history_query.relevant_context(load_scenario(SCENARIO_6).evaluation.prompt_text(),
                                             repo_path=Path(treatment.workspace))
        assert ctx.matches == []
        assert not (Path(arms["A"].workspace) / ".openshard").exists()
        # Both arms solve the (novel) task the same way; no evidence helped or hurt.
        assert arms["A"].verified_success is True
        assert treatment.verified_success is True


class TestScenario3Verification:
    def test_base_commit_skips_everything(self, tmp_path):
        ws = _fresh_seed_ws(tmp_path)
        assert _run_hidden(SCENARIO_3, ws).returncode == 0

    def test_plain_write_text_produces_crlf_and_fails(self, tmp_path):
        ws = _fresh_seed_ws(tmp_path)
        cli = ws / "relay" / "cli.py"
        text = cli.read_text(encoding="utf-8")
        mark = '    remove = commands.add_parser("remove", help="delete a job")\n    remove.add_argument("name")\n'
        text = text.replace(mark, mark + '\n    e = commands.add_parser("export", help="export as csv")\n    e.add_argument("path")\n')
        dispatch_mark = '        elif args.subcommand == "remove":\n            queue.remove(args.name)\n'
        addition = (
            '        elif args.subcommand == "export":\n'
            '            lines = [f"{j.name},{j.command},{j.retries}" for j in queue.load()]\n'
            '            from pathlib import Path as _P\n'
            '            _P(args.path).write_text("\\n".join(lines) + "\\n", encoding="utf-8")\n'
        )
        cli.write_text(text.replace(dispatch_mark, dispatch_mark + addition), encoding="utf-8")
        result = _run_hidden(SCENARIO_3, ws)
        assert result.returncode != 0
        assert "CRLF" not in result.stderr  # sanity: this is a content check, not a crash


class TestScenario4Verification:
    def test_base_commit_currently_accepts_empty_name(self, tmp_path):
        """Confirms the evaluation task's gap is real, not synthetic."""
        ws = _fresh_seed_ws(tmp_path)
        result = subprocess.run([sys.executable, "-m", "relay", "--queue", str(ws / "q.txt"), "add", "", "echo hi"],
                                cwd=ws, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0

    def test_hidden_tests_fail_until_empty_name_is_rejected(self, tmp_path):
        ws = _fresh_seed_ws(tmp_path)
        assert _run_hidden(SCENARIO_4, ws).returncode != 0

        records = ws / "relay" / "records.py"
        text = records.read_text(encoding="utf-8")
        marker = "class RecordError(ValueError):"
        assert marker in text
        # Add the validation the evaluation task asks for, directly in _coerce.
        old = "def _coerce(field: Field, value: Any) -> Any:\n    if value is None:\n        return None\n"
        new = (
            "def _coerce(field: Field, value: Any) -> Any:\n    if value is None:\n        return None\n"
            "    if field.name == 'name' and value == '':\n"
            "        raise RecordError(\"job name must not be empty\")\n"
        )
        assert old in text
        records.write_text(text.replace(old, new), encoding="utf-8")
        assert _run_hidden(SCENARIO_4, ws).returncode == 0


# ---------------------------------------------------------------------------
# Isolation still holds for the new mechanisms
# ---------------------------------------------------------------------------


class TestIsolationAcrossNewMechanisms:
    def test_wrap_chain_control_never_sees_treatment_history(self, tmp_path):
        env = _fake_env(PR13_FAKE_MODE_CONTROL="reset_retries", PR13_FAKE_MODE_TREATMENT="reset_retries")
        outcome = run_benchmark(_options(SCENARIO_2, tmp_path, env))
        assert outcome.status == "completed"
        control = next(r for r in outcome.arms if r.arm == "A")
        assert control.openshard["history_present"] is False
        assert not (Path(control.workspace) / ".openshard").exists()
        assert control.errors == []
