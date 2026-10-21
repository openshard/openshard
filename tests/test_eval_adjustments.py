from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.evals.adjustments import (
    _ADJ_MAX,
    _ADJ_MIN,
    _CAT_COST_BONUS,
    _CAT_PASS_RATE_BONUS,
    _CAT_PASS_RATE_PENALTY,
    _CAT_UNSAFE_PENALTY,
    _PASS_RATE_BONUS,
    _PASS_RATE_PENALTY,
    _TOKEN_BONUS,
    _UNSAFE_PENALTY,
    MIN_CATEGORY_EVAL_RUNS,
    MIN_EVAL_RUNS,
    compute_category_eval_adjustment_reasons,
    compute_category_eval_adjustments,
    compute_eval_adjustment_reasons,
    compute_eval_adjustments,
)
from openshard.evals.stats import CategoryStats, EvalStats


def _stat(
    model: str = "m/a",
    suite: str = "basic",
    task_id: str = "t1",
    run_count: int = 3,
    pass_count: int = 3,
    unsafe_file_count: int = 0,
    avg_total_tokens: float | None = None,
) -> EvalStats:
    fail_count = run_count - pass_count
    pass_rate = pass_count / run_count if run_count else 0.0
    return EvalStats(
        model=model,
        suite=suite,
        task_id=task_id,
        run_count=run_count,
        pass_count=pass_count,
        fail_count=fail_count,
        pass_rate=pass_rate,
        avg_duration=1.0,
        avg_total_tokens=avg_total_tokens,
        unsafe_file_count=unsafe_file_count,
    )


class TestComputeEvalAdjustments(unittest.TestCase):

    def test_empty_stats_returns_empty_dict(self):
        self.assertEqual(compute_eval_adjustments([]), {})

    def test_run_count_below_min_excluded(self):
        s = _stat(run_count=MIN_EVAL_RUNS - 1, pass_count=MIN_EVAL_RUNS - 1)
        self.assertEqual(compute_eval_adjustments([s]), {})

    def test_high_pass_rate_gives_bonus(self):
        s = _stat(run_count=3, pass_count=3)
        adj = compute_eval_adjustments([s])
        self.assertAlmostEqual(adj["m/a"], _PASS_RATE_BONUS)

    def test_low_pass_rate_gives_penalty(self):
        s = _stat(run_count=4, pass_count=1)
        adj = compute_eval_adjustments([s])
        self.assertAlmostEqual(adj["m/a"], _PASS_RATE_PENALTY)

    def test_mid_pass_rate_no_adjustment(self):
        # pass_rate = 0.67: not >= 0.80 and not < 0.50
        s = _stat(run_count=3, pass_count=2)
        adj = compute_eval_adjustments([s])
        self.assertNotIn("m/a", adj)

    def test_unsafe_files_penalty_stacks_with_pass_penalty(self):
        s = _stat(run_count=4, pass_count=1, unsafe_file_count=1)
        adj = compute_eval_adjustments([s])
        expected = max(_ADJ_MIN, _PASS_RATE_PENALTY + _UNSAFE_PENALTY)
        self.assertAlmostEqual(adj["m/a"], expected)

    def test_unsafe_files_penalty_alone_when_mid_pass_rate(self):
        s = _stat(run_count=3, pass_count=2, unsafe_file_count=2)
        adj = compute_eval_adjustments([s])
        self.assertAlmostEqual(adj["m/a"], _UNSAFE_PENALTY)

    def test_token_bonus_applies_when_pass_rate_strong(self):
        s = _stat(run_count=3, pass_count=3, avg_total_tokens=1000.0)
        adj = compute_eval_adjustments([s])
        self.assertAlmostEqual(adj["m/a"], _PASS_RATE_BONUS + _TOKEN_BONUS)

    def test_token_bonus_suppressed_when_pass_rate_not_strong(self):
        s = _stat(run_count=4, pass_count=1, avg_total_tokens=1000.0)
        adj = compute_eval_adjustments([s])
        # Only pass-rate penalty, no token bonus
        self.assertAlmostEqual(adj["m/a"], _PASS_RATE_PENALTY)

    def test_token_bonus_suppressed_when_avg_tokens_above_threshold(self):
        s = _stat(run_count=3, pass_count=3, avg_total_tokens=10000.0)
        adj = compute_eval_adjustments([s])
        self.assertAlmostEqual(adj["m/a"], _PASS_RATE_BONUS)

    def test_adjustment_clamped_to_max(self):
        # High pass rate + token bonus: 0.5 + 0.2 = 0.7, well under max.
        # Force over max by creating a scenario that would exceed +1.0 if unclamped.
        # Currently impossible with our rules (max is 0.5 + 0.2 = 0.7), but verify clamping is applied.
        s = _stat(run_count=3, pass_count=3, avg_total_tokens=100.0)
        adj = compute_eval_adjustments([s])
        self.assertLessEqual(adj["m/a"], _ADJ_MAX)

    def test_adjustment_clamped_to_min(self):
        s = _stat(run_count=4, pass_count=1, unsafe_file_count=5)
        adj = compute_eval_adjustments([s])
        self.assertGreaterEqual(adj["m/a"], _ADJ_MIN)

    def test_multiple_models_are_independent(self):
        good = _stat(model="m/good", run_count=3, pass_count=3)
        bad = _stat(model="m/bad", run_count=4, pass_count=1)
        adj = compute_eval_adjustments([good, bad])
        self.assertAlmostEqual(adj["m/good"], _PASS_RATE_BONUS)
        self.assertAlmostEqual(adj["m/bad"], _PASS_RATE_PENALTY)

    def test_stats_for_one_model_do_not_affect_another(self):
        s = _stat(model="m/a", run_count=3, pass_count=3)
        adj = compute_eval_adjustments([s])
        self.assertNotIn("m/b", adj)

    def test_aggregates_across_tasks_for_same_model(self):
        # Two tasks: 3 passes + 0 passes (total 3/6 = 0.5, not < 0.50)
        s1 = _stat(model="m/a", task_id="t1", run_count=3, pass_count=3)
        s2 = _stat(model="m/a", task_id="t2", run_count=3, pass_count=0)
        adj = compute_eval_adjustments([s1, s2])
        # pass_rate = 3/6 = 0.5: not >= 0.80, not < 0.50 → no pass adjustment
        self.assertNotIn("m/a", adj)

    def test_only_nonzero_adjustments_returned(self):
        # Mid pass rate, no unsafe → no adjustment
        s = _stat(run_count=3, pass_count=2)
        adj = compute_eval_adjustments([s])
        self.assertNotIn("m/a", adj)


class TestComputeEvalAdjustmentReasons(unittest.TestCase):

    def test_high_pass_rate_reason(self):
        s = _stat(run_count=3, pass_count=3)
        reasons = compute_eval_adjustment_reasons([s])
        self.assertIn("high eval pass rate", reasons["m/a"])

    def test_low_pass_rate_reason(self):
        s = _stat(run_count=4, pass_count=1)
        reasons = compute_eval_adjustment_reasons([s])
        self.assertIn("low eval pass rate", reasons["m/a"])

    def test_unsafe_files_reason(self):
        s = _stat(run_count=3, pass_count=2, unsafe_file_count=1)
        reasons = compute_eval_adjustment_reasons([s])
        self.assertIn("unsafe files in evals", reasons["m/a"])

    def test_token_efficiency_reason(self):
        s = _stat(run_count=3, pass_count=3, avg_total_tokens=500.0)
        reasons = compute_eval_adjustment_reasons([s])
        self.assertIn("low token usage", reasons["m/a"])

    def test_reasons_combined_with_semicolon(self):
        s = _stat(run_count=3, pass_count=3, unsafe_file_count=1, avg_total_tokens=500.0)
        reasons = compute_eval_adjustment_reasons([s])
        parts = reasons["m/a"].split("; ")
        self.assertGreater(len(parts), 1)

    def test_no_reason_when_below_min_runs(self):
        s = _stat(run_count=MIN_EVAL_RUNS - 1, pass_count=MIN_EVAL_RUNS - 1)
        reasons = compute_eval_adjustment_reasons([s])
        self.assertNotIn("m/a", reasons)

    def test_no_reason_when_adjustment_is_zero(self):
        s = _stat(run_count=3, pass_count=2)
        reasons = compute_eval_adjustment_reasons([s])
        self.assertNotIn("m/a", reasons)


class TestEvalScoringCLI(unittest.TestCase):

    def _write_eval_run(self, path: Path, model: str, passed: bool) -> None:
        record = {
            "suite": "basic",
            "task_id": "t1",
            "model": model,
            "passed": passed,
            "duration_seconds": 1.0,
            "total_tokens": 100,
            "unsafe_files": [],
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _invoke_dry_run(self, args: list[str], tmp_path: Path, monkeypatch) -> object:
        from openshard.cli.main import cli

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        return runner.invoke(cli, ["run", "write a hello world function"] + args)

    def test_no_crash_with_missing_eval_file(self, tmp_path=None):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        runner = CliRunner()
        import os
        old = os.getcwd()
        try:
            os.chdir(tmp)
            from openshard.cli.main import cli
            result = runner.invoke(cli, ["run", "--eval-scoring", "--dry-run", "hello"])
        finally:
            os.chdir(old)
        self.assertNotEqual(result.exit_code, 2)

    def test_more_output_shows_eval_scoring_enabled(self, tmp_path=None):
        import os
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        eval_dir = tmp / ".openshard"
        eval_dir.mkdir()
        runs_file = eval_dir / "eval-runs.jsonl"
        # Write 3 passing records for a model that won't be in inventory —
        # the display should still say "eval scoring: enabled"
        for _ in range(3):
            self._write_eval_run(runs_file, "some/nonexistent-model", passed=True)

        runner = CliRunner()
        old = os.getcwd()
        try:
            os.chdir(tmp)
            from openshard.cli.main import cli
            result = runner.invoke(cli, ["run", "--eval-scoring", "--more", "--dry-run", "hello"])
        finally:
            os.chdir(old)

        self.assertIn("eval scoring: enabled", result.output)

    def test_more_output_without_flag_does_not_show_eval_scoring(self, tmp_path=None):
        import os
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        runner = CliRunner()
        old = os.getcwd()
        try:
            os.chdir(tmp)
            from openshard.cli.main import cli
            result = runner.invoke(cli, ["run", "--more", "--dry-run", "hello"])
        finally:
            os.chdir(old)

        self.assertNotIn("eval scoring", result.output)

    def _run_without_key(self, args: list[str], tmp: Path) -> object:
        """Invoke CLI from tmp dir with OPENROUTER_API_KEY removed from env."""
        import os
        runner = CliRunner()
        old_cwd = os.getcwd()
        old_key = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            os.chdir(tmp)
            from openshard.cli.main import cli
            return runner.invoke(cli, args)
        finally:
            os.chdir(old_cwd)
            if old_key is not None:
                os.environ["OPENROUTER_API_KEY"] = old_key

    def test_dry_run_works_without_api_key(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        result = self._run_without_key(["run", "--dry-run", "hello"], tmp)
        self.assertNotIn("OPENROUTER_API_KEY", result.output)
        self.assertNotEqual(result.exit_code, 1)

    def test_eval_scoring_dry_run_without_api_key(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        eval_dir = tmp / ".openshard"
        eval_dir.mkdir()
        runs_file = eval_dir / "eval-runs.jsonl"
        for _ in range(3):
            self._write_eval_run(runs_file, "some/model", passed=True)
        result = self._run_without_key(
            ["run", "--eval-scoring", "--more", "--dry-run", "hello"], tmp
        )
        self.assertIn("eval scoring: enabled", result.output)
        self.assertNotIn("OPENROUTER_API_KEY", result.output)

    def test_non_dry_run_fails_without_api_key(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        result = self._run_without_key(["run", "hello"], tmp)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("OPENROUTER_API_KEY", result.output)

    def test_config_eval_scoring_respected(self):
        from unittest.mock import MagicMock, patch

        from openshard.cli.main import cli

        mock_gen = MagicMock()
        mock_gen.generate.return_value = MagicMock(usage=None, files=[], summary="done", notes=[])
        mock_gen.model = "mock/model"
        mock_gen.fixer_model = "mock/fixer"

        runner = CliRunner()
        with patch("openshard.cli.main.load_config", return_value={"approval_mode": "smart", "eval_scoring": True}), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=mock_gen), \
             patch("openshard.run.pipeline.ProviderManager"), \
             patch("openshard.run.pipeline.analyze_repo", return_value=None), \
             runner.isolated_filesystem():  # a run records history in the cwd: never this checkout's
            result = runner.invoke(cli, ["run", "--more", "--dry-run", "hello"])

        self.assertIn("eval scoring: enabled", result.output)


def _cat_stat(
    model: str = "m/a",
    suite: str = "basic",
    category: str = "standard",
    run_count: int = 2,
    pass_count: int = 2,
    unsafe_file_count: int = 0,
    cost_per_pass: float | None = None,
    avg_total_tokens: float | None = None,
) -> CategoryStats:
    fail_count = run_count - pass_count
    pass_rate = pass_count / run_count if run_count else 0.0
    return CategoryStats(
        suite=suite,
        category=category,
        model=model,
        run_count=run_count,
        pass_count=pass_count,
        fail_count=fail_count,
        pass_rate=pass_rate,
        avg_duration=1.0,
        avg_total_tokens=avg_total_tokens,
        cost_per_pass=cost_per_pass,
        unsafe_file_count=unsafe_file_count,
    )


class TestComputeCategoryEvalAdjustments(unittest.TestCase):

    def test_empty_returns_empty(self):
        self.assertEqual(compute_category_eval_adjustments([], "standard"), {})

    def test_wrong_category_excluded(self):
        s = _cat_stat(category="boilerplate", run_count=3, pass_count=3)
        adj = compute_category_eval_adjustments([s], "security")
        self.assertNotIn("m/a", adj)

    def test_high_pass_rate_gives_bonus(self):
        s = _cat_stat(category="security", run_count=2, pass_count=2)
        adj = compute_category_eval_adjustments([s], "security")
        self.assertAlmostEqual(adj["m/a"], _CAT_PASS_RATE_BONUS)

    def test_low_pass_rate_gives_penalty(self):
        s = _cat_stat(category="security", run_count=4, pass_count=1)
        adj = compute_category_eval_adjustments([s], "security")
        self.assertAlmostEqual(adj["m/a"], _CAT_PASS_RATE_PENALTY)

    def test_mid_pass_rate_no_adjustment(self):
        # pass_rate = 0.67: neither >= 0.80 nor < 0.50
        s = _cat_stat(category="standard", run_count=3, pass_count=2)
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertNotIn("m/a", adj)

    def test_unsafe_penalty_alone(self):
        s = _cat_stat(category="standard", run_count=3, pass_count=2, unsafe_file_count=1)
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertAlmostEqual(adj["m/a"], _CAT_UNSAFE_PENALTY)

    def test_cost_bonus_with_strong_pass_rate_and_low_cost(self):
        s = _cat_stat(
            category="standard", run_count=2, pass_count=2,
            cost_per_pass=0.0005,
        )
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertAlmostEqual(adj["m/a"], _CAT_PASS_RATE_BONUS + _CAT_COST_BONUS)

    def test_cost_bonus_suppressed_when_cost_above_threshold(self):
        s = _cat_stat(
            category="standard", run_count=2, pass_count=2,
            cost_per_pass=0.005,
        )
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertAlmostEqual(adj["m/a"], _CAT_PASS_RATE_BONUS)

    def test_cost_bonus_suppressed_when_pass_rate_weak(self):
        s = _cat_stat(
            category="standard", run_count=4, pass_count=1,
            cost_per_pass=0.0001,
        )
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertAlmostEqual(adj["m/a"], _CAT_PASS_RATE_PENALTY)

    def test_cost_bonus_suppressed_when_cost_per_pass_none(self):
        s = _cat_stat(category="standard", run_count=2, pass_count=2, cost_per_pass=None)
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertAlmostEqual(adj["m/a"], _CAT_PASS_RATE_BONUS)

    def test_below_min_runs_excluded(self):
        s = _cat_stat(category="standard", run_count=MIN_CATEGORY_EVAL_RUNS - 1, pass_count=1)
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertNotIn("m/a", adj)

    def test_multiple_models_independent(self):
        good = _cat_stat(model="m/good", category="complex", run_count=2, pass_count=2)
        bad = _cat_stat(model="m/bad", category="complex", run_count=4, pass_count=1)
        adj = compute_category_eval_adjustments([good, bad], "complex")
        self.assertAlmostEqual(adj["m/good"], _CAT_PASS_RATE_BONUS)
        self.assertAlmostEqual(adj["m/bad"], _CAT_PASS_RATE_PENALTY)

    def test_only_nonzero_returned(self):
        s = _cat_stat(category="standard", run_count=3, pass_count=2)
        adj = compute_category_eval_adjustments([s], "standard")
        self.assertNotIn("m/a", adj)


class TestComputeCategoryEvalAdjustmentReasons(unittest.TestCase):

    def test_high_pass_rate_reason(self):
        s = _cat_stat(category="security", run_count=2, pass_count=2)
        reasons = compute_category_eval_adjustment_reasons([s], "security")
        self.assertIn("high category pass rate", reasons["m/a"])

    def test_low_pass_rate_reason(self):
        s = _cat_stat(category="security", run_count=4, pass_count=1)
        reasons = compute_category_eval_adjustment_reasons([s], "security")
        self.assertIn("low category pass rate", reasons["m/a"])

    def test_unsafe_reason(self):
        s = _cat_stat(category="standard", run_count=3, pass_count=2, unsafe_file_count=1)
        reasons = compute_category_eval_adjustment_reasons([s], "standard")
        self.assertIn("unsafe files in evals", reasons["m/a"])

    def test_cost_bonus_reason(self):
        s = _cat_stat(category="standard", run_count=2, pass_count=2, cost_per_pass=0.0005)
        reasons = compute_category_eval_adjustment_reasons([s], "standard")
        self.assertIn("low cost per pass", reasons["m/a"])

    def test_wrong_category_excluded(self):
        s = _cat_stat(category="boilerplate")
        reasons = compute_category_eval_adjustment_reasons([s], "security")
        self.assertNotIn("m/a", reasons)

    def test_below_min_runs_no_reason(self):
        s = _cat_stat(category="standard", run_count=MIN_CATEGORY_EVAL_RUNS - 1, pass_count=1)
        reasons = compute_category_eval_adjustment_reasons([s], "standard")
        self.assertNotIn("m/a", reasons)
