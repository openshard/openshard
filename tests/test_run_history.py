from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openshard.analysis.repo import RepoFacts
from openshard.cli.main import _log_run
from openshard.execution.stages import Stage, StageRun
from openshard.providers.base import ModelInfo
from openshard.providers.manager import InventoryEntry
from openshard.routing.engine import MODEL_MAIN, MODEL_STRONG
from openshard.scoring.requirements import TaskRequirements
from openshard.scoring.scorer import ScoredRoutingResult, select_with_info
from openshard.skills.discovery import SkillDef
from openshard.skills.matcher import MatchedSkill


def _make_generator(model="openrouter/fast-model", fixer_model="openrouter/strong-model"):
    g = MagicMock()
    g.model = model
    g.fixer_model = fixer_model
    return g


def _make_scored(
    candidates=None,
    scores=None,
    used_fallback=False,
    category="standard",
) -> ScoredRoutingResult:
    ids = candidates or []
    return ScoredRoutingResult(
        category=category,
        requirements=TaskRequirements(),
        candidate_count=len(ids),
        selected_model=ids[0] if ids else None,
        selected_provider="openrouter",
        used_fallback=used_fallback,
        candidates=ids,
        scores=scores or {},
    )


def _make_repo_facts(**kwargs) -> RepoFacts:
    defaults = dict(
        languages=["python"],
        package_files=["pyproject.toml"],
        framework="pytest",
        test_command="pytest",
        risky_paths=["auth/tokens.py", "config/secrets.py"],
        changed_files=["src/foo.py"],
    )
    defaults.update(kwargs)
    return RepoFacts(**defaults)


def _make_entry(model_id: str) -> InventoryEntry:
    return InventoryEntry(
        provider="openrouter",
        model=ModelInfo(
            id=model_id,
            name=model_id,
            pricing={},
            context_window=None,
            max_output_tokens=None,
            supports_vision=False,
            supports_tools=False,
        ),
    )


class TestLogRunHistory(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    # --- routing_candidates ---

    def test_routing_candidates_stored(self):
        scored = _make_scored(
            candidates=["openrouter/fast", "openrouter/strong"],
            scores={"openrouter/fast": 12.0, "openrouter/strong": 11.5},
        )
        self._call(_scored=scored)
        entry = self._read_entry()
        self.assertEqual(entry["routing_candidates"], ["openrouter/fast", "openrouter/strong"])

    def test_routing_candidates_order_preserved(self):
        ids = ["openrouter/a", "openrouter/b", "openrouter/c"]
        scored = _make_scored(candidates=ids, scores={i: 10.0 for i in ids})
        self._call(_scored=scored)
        entry = self._read_entry()
        self.assertEqual(entry["routing_candidates"], ids)

    def test_routing_candidates_absent_on_fallback(self):
        scored = _make_scored(used_fallback=True)
        self._call(_scored=scored)
        entry = self._read_entry()
        self.assertNotIn("routing_candidates", entry)
        self.assertNotIn("routing_scores", entry)

    def test_routing_candidates_absent_when_no_scored(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("routing_candidates", entry)

    # --- routing_scores ---

    def test_routing_scores_stored(self):
        scored = _make_scored(
            candidates=["openrouter/fast", "openrouter/strong"],
            scores={"openrouter/fast": 12.0, "openrouter/strong": 11.5},
        )
        self._call(_scored=scored)
        entry = self._read_entry()
        self.assertAlmostEqual(entry["routing_scores"]["openrouter/fast"], 12.0)
        self.assertAlmostEqual(entry["routing_scores"]["openrouter/strong"], 11.5)

    def test_routing_scores_absent_when_no_scored(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("routing_scores", entry)

    # --- repo_facts ---

    def test_repo_facts_stored(self):
        facts = _make_repo_facts()
        self._call(repo_facts=facts)
        entry = self._read_entry()
        rf = entry["repo_facts"]
        self.assertEqual(rf["languages"], ["python"])
        self.assertEqual(rf["package_files"], ["pyproject.toml"])
        self.assertEqual(rf["framework"], "pytest")
        self.assertEqual(rf["test_command"], "pytest")

    def test_repo_facts_counts_and_samples(self):
        facts = _make_repo_facts(
            risky_paths=["a.py", "b.py", "c.py", "d.py"],
            changed_files=["x.py", "y.py", "z.py", "w.py"],
        )
        self._call(repo_facts=facts)
        entry = self._read_entry()
        rf = entry["repo_facts"]
        self.assertEqual(rf["risky_paths_count"], 4)
        self.assertEqual(len(rf["risky_paths_sample"]), 3)
        self.assertEqual(rf["changed_files_count"], 4)
        self.assertEqual(len(rf["changed_files_sample"]), 3)

    def test_repo_facts_sample_does_not_exceed_three(self):
        facts = _make_repo_facts(risky_paths=["a", "b", "c", "d", "e"])
        self._call(repo_facts=facts)
        entry = self._read_entry()
        self.assertLessEqual(len(entry["repo_facts"]["risky_paths_sample"]), 3)

    def test_repo_facts_none_framework_stored_as_null(self):
        facts = _make_repo_facts(framework=None, test_command=None)
        self._call(repo_facts=facts)
        entry = self._read_entry()
        self.assertIsNone(entry["repo_facts"]["framework"])
        self.assertIsNone(entry["repo_facts"]["test_command"])

    def test_repo_facts_absent_when_none(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("repo_facts", entry)

    # --- backward compatibility ---

    def test_core_fields_always_present(self):
        self._call()
        entry = self._read_entry()
        for key in ("timestamp", "task", "execution_model", "retry_triggered",
                    "duration_seconds", "verification_attempted"):
            self.assertIn(key, entry)

    def test_no_new_fields_without_new_params(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("routing_candidates", entry)
        self.assertNotIn("routing_scores", entry)
        self.assertNotIn("repo_facts", entry)

    def test_multiple_entries_appended(self):
        self._call(task="first")
        self._call(task="second")
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["task"], "first")
        self.assertEqual(json.loads(lines[1])["task"], "second")

    # --- matched_skills ---

    def _make_skill(self, slug: str) -> SkillDef:
        return SkillDef(
            slug=slug, name=slug, description="", category="standard",
            keywords=[], languages=[], framework=None,
        )

    def test_matched_skills_logged(self):
        skills = [
            MatchedSkill(skill=self._make_skill("pytest-helper"), reasons=["keyword:pytest"]),
            MatchedSkill(skill=self._make_skill("django-views"), reasons=["framework:django", "category:standard"]),
        ]
        self._call(matched_skills=skills)
        entry = self._read_entry()
        self.assertEqual(entry["matched_skills"], ["pytest-helper", "django-views"])
        self.assertEqual(entry["matched_skill_reasons"]["pytest-helper"], ["keyword:pytest"])
        self.assertEqual(entry["matched_skill_reasons"]["django-views"], ["framework:django", "category:standard"])

    def test_no_skills_no_fields(self):
        self._call(matched_skills=[])
        entry = self._read_entry()
        self.assertNotIn("matched_skills", entry)
        self.assertNotIn("matched_skill_reasons", entry)

    # --- select_with_info integration ---

    def test_select_with_info_populates_candidates(self):
        entries = [_make_entry("openrouter/model-a"), _make_entry("openrouter/model-b")]
        result = select_with_info(entries, TaskRequirements(), "standard")
        self.assertGreater(len(result.candidates), 0)
        self.assertIn(result.selected_model, result.candidates)

    def test_select_with_info_populates_scores(self):
        entries = [_make_entry("openrouter/model-a"), _make_entry("openrouter/model-b")]
        result = select_with_info(entries, TaskRequirements(), "standard")
        self.assertGreater(len(result.scores), 0)
        self.assertIn(result.selected_model, result.scores)
        for score in result.scores.values():
            self.assertIsInstance(score, float)

    def test_select_with_info_fallback_has_empty_candidates_and_scores(self):
        entry = _make_entry("openrouter/no-vision")
        reqs = TaskRequirements(needs_vision=True)
        result = select_with_info([entry], reqs, "visual")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.scores, {})

    def test_select_with_info_scores_are_rounded(self):
        entries = [_make_entry("openrouter/model-a")]
        result = select_with_info(entries, TaskRequirements(), "standard")
        for score in result.scores.values():
            self.assertEqual(round(score, 3), score)

    # --- execution_profile ---

    def test_execution_profile_logged(self):
        from openshard.routing.profiles import ProfileDecision
        pd = ProfileDecision(profile="native_deep", reason="security category")
        self._call(profile_decision=pd)
        entry = self._read_entry()
        self.assertEqual(entry["execution_profile"], "native_deep")
        self.assertEqual(entry["execution_profile_reason"], "security category")

    def test_execution_profile_absent_when_none(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("execution_profile", entry)
        self.assertNotIn("execution_profile_reason", entry)

    def test_execution_profile_light_logged(self):
        from openshard.routing.profiles import ProfileDecision
        pd = ProfileDecision(profile="native_light", reason="simple/safe task")
        self._call(profile_decision=pd)
        entry = self._read_entry()
        self.assertEqual(entry["execution_profile"], "native_light")

    def test_execution_profile_swarm_logged(self):
        from openshard.routing.profiles import ProfileDecision
        pd = ProfileDecision(profile="native_swarm", reason="explicit override")
        self._call(profile_decision=pd)
        entry = self._read_entry()
        self.assertEqual(entry["execution_profile"], "native_swarm")
        self.assertEqual(entry["execution_profile_reason"], "explicit override")


class TestFeedbackScoringMetadata(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def test_feedback_scoring_used_recorded_via_extra_metadata(self):
        receipt = {
            "routing_feedback_scoring_used": True,
            "routing_feedback_adjustments": {"openrouter/fast-model": -0.2},
            "routing_feedback_reasons": {"openrouter/fast-model": "feedback: 3 rejected"},
        }
        self._call(extra_metadata=receipt)
        entry = self._read_entry()
        self.assertTrue(entry.get("routing_feedback_scoring_used"))
        self.assertIn("openrouter/fast-model", entry.get("routing_feedback_adjustments", {}))
        self.assertAlmostEqual(entry["routing_feedback_adjustments"]["openrouter/fast-model"], -0.2)

    def test_feedback_scoring_absent_by_default(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("routing_feedback_scoring_used", entry)
        self.assertNotIn("routing_feedback_adjustments", entry)

    def test_feedback_scoring_reasons_recorded(self):
        receipt = {
            "routing_feedback_scoring_used": True,
            "routing_feedback_adjustments": {"m": 0.15},
            "routing_feedback_reasons": {"m": "feedback: 4 accepted"},
        }
        self._call(extra_metadata=receipt)
        entry = self._read_entry()
        self.assertEqual(entry.get("routing_feedback_reasons", {}).get("m"), "feedback: 4 accepted")

    def test_old_run_without_feedback_scoring_keys_renders_safely(self):
        self._call()
        entry = self._read_entry()
        self.assertNotIn("routing_feedback_scoring_used", entry)
        self.assertNotIn("routing_feedback_adjustments", entry)
        self.assertNotIn("routing_feedback_reasons", entry)


class TestVerificationPlanLogging(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def _make_plan(self):
        from openshard.verification.plan import (
            CommandSafety,
            VerificationCommand,
            VerificationKind,
            VerificationPlan,
            VerificationSource,
        )
        cmd = VerificationCommand(
            name="tests",
            argv=["python", "-m", "pytest"],
            kind=VerificationKind.test,
            source=VerificationSource.detected,
            safety=CommandSafety.safe,
            reason="matches safe prefix: python -m pytest",
        )
        return VerificationPlan(commands=[cmd])

    def test_plan_with_commands_stored(self):
        self._call(verification_plan=self._make_plan())
        entry = self._read_entry()
        self.assertIn("verification_plan", entry)
        self.assertEqual(len(entry["verification_plan"]), 1)

    def test_stored_fields_match_command(self):
        self._call(verification_plan=self._make_plan())
        entry = self._read_entry()
        vc = entry["verification_plan"][0]
        self.assertEqual(vc["name"], "tests")
        self.assertEqual(vc["argv"], ["python", "-m", "pytest"])
        self.assertEqual(vc["kind"], "test")
        self.assertEqual(vc["source"], "detected")
        self.assertEqual(vc["safety"], "safe")
        self.assertIn("pytest", vc["reason"])

    def test_empty_plan_not_stored(self):
        from openshard.verification.plan import VerificationPlan
        self._call(verification_plan=VerificationPlan())
        entry = self._read_entry()
        self.assertNotIn("verification_plan", entry)

    def test_none_plan_not_stored(self):
        self._call(verification_plan=None)
        entry = self._read_entry()
        self.assertNotIn("verification_plan", entry)

    def test_existing_fields_unaffected(self):
        self._call(task="my task", verification_plan=self._make_plan())
        entry = self._read_entry()
        self.assertEqual(entry["task"], "my task")
        self.assertIn("timestamp", entry)
        self.assertIn("execution_model", entry)


class TestStageRunsDispatchLogging(unittest.TestCase):
    """Verify _log_run serialises stage_runs with dispatch models correctly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def _make_stage_run(self, stage_type: str, model: str) -> StageRun:
        return StageRun(
            stage=Stage(stage_type=stage_type, description="test stage"),
            model=model,
            duration=0.5,
            cost=0.0001,
            summary="done",
        )

    def test_planning_stage_model_logged(self):
        sr = self._make_stage_run("planning", MODEL_STRONG)
        self._call(stage_runs=[sr])
        entry = self._read_entry()
        self.assertIn("stage_runs", entry)
        self.assertEqual(entry["stage_runs"][0]["model"], MODEL_STRONG)
        self.assertEqual(entry["stage_runs"][0]["stage_type"], "planning")

    def test_implementation_stage_model_logged(self):
        sr = self._make_stage_run("implementation", MODEL_MAIN)
        self._call(stage_runs=[sr])
        entry = self._read_entry()
        self.assertIn("stage_runs", entry)
        self.assertEqual(entry["stage_runs"][0]["model"], MODEL_MAIN)
        self.assertEqual(entry["stage_runs"][0]["stage_type"], "implementation")

    def test_dispatch_models_both_stages(self):
        """Planning uses MODEL_STRONG and implementation uses MODEL_MAIN (dispatch scenario)."""
        stage_runs = [
            self._make_stage_run("planning", MODEL_STRONG),
            self._make_stage_run("implementation", MODEL_MAIN),
        ]
        self._call(stage_runs=stage_runs)
        entry = self._read_entry()
        self.assertEqual(len(entry["stage_runs"]), 2)
        plan_logged = next(sr for sr in entry["stage_runs"] if sr["stage_type"] == "planning")
        impl_logged = next(sr for sr in entry["stage_runs"] if sr["stage_type"] == "implementation")
        self.assertEqual(plan_logged["model"], MODEL_STRONG)
        self.assertEqual(impl_logged["model"], MODEL_MAIN)

    def test_stage_token_usage_logged_when_recorded(self):
        sr = StageRun(
            stage=Stage(stage_type="planning", description="test stage"),
            model=MODEL_STRONG,
            duration=0.5,
            cost=0.0001,
            summary="done",
            prompt_tokens=120,
            completion_tokens=30,
        )
        self._call(stage_runs=[sr])
        entry = self._read_entry()
        self.assertEqual(entry["stage_runs"][0]["prompt_tokens"], 120)
        self.assertEqual(entry["stage_runs"][0]["completion_tokens"], 30)

    def test_no_stage_runs_key_when_empty(self):
        self._call(stage_runs=[])
        entry = self._read_entry()
        self.assertNotIn("stage_runs", entry)


class TestToolSearchEventsHistory(unittest.TestCase):
    """tool_search_events serialization in run history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def _make_event_dict(self, **overrides) -> dict:
        base = {
            "tool_name": "search_repo",
            "selected_reason": "observe search trigger",
            "query": "auth",
            "result_count": 3,
            "result_quality": "useful",
            "retry_count": 0,
            "fallback_tool": None,
            "context_injected": True,
            "changed_plan": False,
            "warnings": [],
        }
        base.update(overrides)
        return base

    def test_tool_search_events_written_to_history(self):
        events = [self._make_event_dict()]
        self._call(extra_metadata={"tool_search_events": events})
        entry = self._read_entry()
        self.assertIn("tool_search_events", entry)
        self.assertEqual(len(entry["tool_search_events"]), 1)

    def test_event_fields_preserved_in_history(self):
        events = [self._make_event_dict(result_count=5, result_quality="weak")]
        self._call(extra_metadata={"tool_search_events": events})
        entry = self._read_entry()
        ev = entry["tool_search_events"][0]
        self.assertEqual(ev["tool_name"], "search_repo")
        self.assertEqual(ev["result_count"], 5)
        self.assertEqual(ev["result_quality"], "weak")
        self.assertIn("context_injected", ev)

    def test_empty_events_list_stored_as_empty_list(self):
        self._call(extra_metadata={"tool_search_events": []})
        entry = self._read_entry()
        self.assertIn("tool_search_events", entry)
        self.assertEqual(entry["tool_search_events"], [])

    def test_old_entry_without_events_key_is_valid(self):
        # Simulate an old run entry that has no tool_search_events
        self._call()
        entry = self._read_entry()
        # Old entries simply don't have the key — no error expected on read
        events = entry.get("tool_search_events", [])
        self.assertIsInstance(events, list)

    def test_no_raw_content_in_stored_event(self):
        events = [self._make_event_dict()]
        self._call(extra_metadata={"tool_search_events": events})
        entry = self._read_entry()
        ev = entry["tool_search_events"][0]
        for forbidden in ("output", "snippets", "diff", "stdout", "stderr"):
            self.assertNotIn(forbidden, ev)

    def test_multiple_events_stored_in_order(self):
        events = [
            self._make_event_dict(tool_name="list_files", result_quality="useful"),
            self._make_event_dict(tool_name="get_git_diff", result_quality="empty", result_count=0),
            self._make_event_dict(tool_name="search_repo", result_quality="weak"),
        ]
        self._call(extra_metadata={"tool_search_events": events})
        entry = self._read_entry()
        names = [e["tool_name"] for e in entry["tool_search_events"]]
        self.assertEqual(names, ["list_files", "get_git_diff", "search_repo"])

    def test_available_tools_serialized_in_event(self):
        events = [self._make_event_dict(available_tools=["list_files", "read_file", "search_repo"])]
        self._call(extra_metadata={"tool_search_events": events})
        entry = self._read_entry()
        ev = entry["tool_search_events"][0]
        self.assertIn("available_tools", ev)
        self.assertEqual(ev["available_tools"], ["list_files", "read_file", "search_repo"])

    def test_old_event_without_available_tools_is_valid(self):
        # Old JSONL records have no available_tools key — must load without error.
        events = [self._make_event_dict()]  # _make_event_dict has no available_tools key
        self._call(extra_metadata={"tool_search_events": events})
        entry = self._read_entry()
        ev = entry["tool_search_events"][0]
        # Rendering helper must gracefully default to []
        from openshard.cli.run_output import _loop_event_value
        self.assertEqual(_loop_event_value(ev, "available_tools", []), [])


class TestFormFactorHistory(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def _make_ff_decision(self):
        from openshard.routing.form_factor_policy import ExecutionFormFactorDecision
        return ExecutionFormFactorDecision(
            public_mode="run",
            internal_form_factor="staged",
            reason="staged planning selected",
            confidence="high",
            risk_level="low",
            read_only=False,
            write_requested=True,
            verification_available=True,
            context_quality=None,
            warnings=[],
        )

    def test_form_factor_stored_in_history(self):
        ff = self._make_ff_decision()
        self._call(form_factor_decision=ff)
        entry = self._read_entry()
        self.assertIn("form_factor", entry)
        ff_stored = entry["form_factor"]
        self.assertEqual(ff_stored["public_mode"], "run")
        self.assertEqual(ff_stored["internal_form_factor"], "staged")
        self.assertEqual(ff_stored["reason"], "staged planning selected")
        self.assertEqual(ff_stored["confidence"], "high")
        self.assertEqual(ff_stored["risk_level"], "low")
        self.assertIs(ff_stored["read_only"], False)
        self.assertIs(ff_stored["write_requested"], True)
        self.assertIs(ff_stored["verification_available"], True)
        self.assertIsNone(ff_stored["context_quality"])
        self.assertEqual(ff_stored["warnings"], [])

    def test_form_factor_absent_when_none(self):
        self._call(form_factor_decision=None)
        entry = self._read_entry()
        self.assertNotIn("form_factor", entry)

    def test_old_entries_without_form_factor_are_valid(self):
        self._call()
        entry = self._read_entry()
        ff = entry.get("form_factor", None)
        self.assertIsNone(ff)


class TestVerificationContractResultHistory(unittest.TestCase):
    """verification_contract_result serialization in run history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self, **kwargs):
        defaults = dict(
            start=time.time(),
            task="test task",
            generator=_make_generator(),
            retry_triggered=False,
            files=[],
            verification_attempted=False,
            verification_passed=None,
            workspace=None,
        )
        defaults.update(kwargs)
        with patch("openshard.cli.main.Path.cwd", return_value=self._tmpdir):
            _log_run(**defaults)

    def _read_entry(self) -> dict:
        log_path = self._tmpdir / ".openshard" / "runs.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def _make_vcr_dict(self) -> dict:
        return {
            "checks": [
                {
                    "check_id": "check_0",
                    "expected_check": "tests pass",
                    "verification_source": "verification_loop",
                    "status": "passed",
                    "reason": "verification suite passed",
                    "evidence_summary": "exit_code=0, 200 chars output",
                    "raw_content_stored": False,
                }
            ],
            "overall_status": "passed",
            "reason": "verification suite passed",
            "raw_content_stored": False,
        }

    def test_verification_contract_result_serialized(self):
        vcr = self._make_vcr_dict()
        self._call(extra_metadata={"verification_contract_result": vcr})
        entry = self._read_entry()
        self.assertIn("verification_contract_result", entry)
        stored = entry["verification_contract_result"]
        self.assertEqual(stored["overall_status"], "passed")
        self.assertEqual(len(stored["checks"]), 1)
        self.assertFalse(stored["raw_content_stored"])

    def test_verification_contract_result_json_roundtrip(self):
        from dataclasses import asdict

        from openshard.native.context import (
            NativeContractCheckResult,
            NativeVerificationContractResult,
        )
        result = NativeVerificationContractResult(
            checks=[
                NativeContractCheckResult(
                    check_id="check_0",
                    expected_check="tests pass",
                    verification_source="verification_loop",
                    status="passed",
                    reason="verification suite passed",
                    evidence_summary="exit_code=0, 200 chars output",
                    raw_content_stored=False,
                )
            ],
            overall_status="passed",
            reason="verification suite passed",
            raw_content_stored=False,
        )
        serialized = asdict(result)
        raw = json.dumps(serialized)
        parsed = json.loads(raw)
        self.assertEqual(parsed["overall_status"], "passed")
        self.assertFalse(parsed["raw_content_stored"])
        self.assertFalse(parsed["checks"][0]["raw_content_stored"])
        self.assertEqual(parsed["checks"][0]["check_id"], "check_0")


if __name__ == "__main__":
    unittest.main()
