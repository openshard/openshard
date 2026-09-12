import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.evals.registry import (
    EvalTask,
    build_category_map,
    load_eval_tasks,
    validate_eval_task,
)
from openshard.evals.runner import EvalResult, run_eval_task
from openshard.evals.stats import (
    compute_category_stats,
    compute_eval_stats,
    load_eval_runs,
    rank_models,
)
from openshard.execution.generator import ChangedFile, ExecutionResult
from openshard.providers.base import UsageStats


def test_loads_bundled_basic_evals():
    tasks = load_eval_tasks("basic")
    assert len(tasks) == 10
    ids = {t.id for t in tasks}
    assert ids == {
        "docs_update",
        "helper_function",
        "cli_flag",
        "bug_fix",
        "config_parser",
        "validation_helper",
        "formatting_helper",
        "unit_test_addition",
        "add_docstring",
        "rename_symbol",
    }


def test_validates_required_metadata_fields(tmp_path):
    task_dir = tmp_path / "sample_task"
    task_dir.mkdir()
    (task_dir / "prompt.txt").write_text("Do something.")
    (task_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": "sample_task",
                "title": "Sample Task",
                "category": "standard",
                "expected_files": ["some_file.py"],
                "verification_command": None,
            }
        )
    )
    task = validate_eval_task(task_dir)
    assert task.id == "sample_task"
    assert task.title == "Sample Task"
    assert task.category == "standard"
    assert task.expected_files == ["some_file.py"]
    assert task.verification_command is None
    assert task.prompt == "Do something."


def test_rejects_missing_prompt_txt(tmp_path):
    task_dir = tmp_path / "no_prompt"
    task_dir.mkdir()
    (task_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": "no_prompt",
                "title": "No Prompt",
                "category": "standard",
                "expected_files": [],
                "verification_command": None,
            }
        )
    )
    with pytest.raises(FileNotFoundError, match="prompt.txt"):
        validate_eval_task(task_dir)


def test_rejects_invalid_metadata_json(tmp_path):
    task_dir = tmp_path / "bad_meta"
    task_dir.mkdir()
    (task_dir / "prompt.txt").write_text("Do something.")
    (task_dir / "metadata.json").write_text("{ not valid json }")
    with pytest.raises(ValueError, match="Invalid metadata.json"):
        validate_eval_task(task_dir)


def test_rejects_missing_metadata_fields(tmp_path):
    task_dir = tmp_path / "incomplete_meta"
    task_dir.mkdir()
    (task_dir / "prompt.txt").write_text("Do something.")
    (task_dir / "metadata.json").write_text(json.dumps({"id": "incomplete_meta"}))
    with pytest.raises(ValueError, match="missing fields"):
        validate_eval_task(task_dir)


def test_eval_list_output_includes_fixture_ids():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "list"])
    assert result.exit_code == 0, result.output
    assert "docs_update" in result.output
    assert "helper_function" in result.output
    assert "cli_flag" in result.output


def test_eval_list_with_suite_option():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "list", "--suite", "basic"])
    assert result.exit_code == 0, result.output
    assert "docs_update" in result.output
    assert "helper_function" in result.output
    assert "cli_flag" in result.output


def test_eval_list_missing_suite_fails_cleanly():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "list", "--suite", "nonexistent"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output or "Error" in result.output


def test_eval_validate_succeeds_on_bundled_fixtures():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "validate"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "10" in result.output


def test_eval_validate_with_suite_option():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "validate", "--suite", "basic"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "10" in result.output
    assert "basic" in result.output


def test_eval_validate_missing_suite_fails_cleanly():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "validate", "--suite", "nonexistent"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output or "Error" in result.output


def test_eval_commands_are_documented():
    # The command-by-command reference lives in docs/cli-reference.md; the
    # README keeps only the beginner flow and links to it.
    root = Path(__file__).parent.parent
    reference = (root / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert "eval list" in reference
    assert "eval validate" in reference
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "docs/cli-reference.md" in readme


# ---------------------------------------------------------------------------
# eval run tests — all mock ExecutionGenerator.generate (no real AI calls)
# ---------------------------------------------------------------------------

def _fake_result(files: list[ChangedFile] | None = None) -> ExecutionResult:
    return ExecutionResult(
        summary="done",
        files=files or [],
        notes=[],
        usage=UsageStats(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def test_eval_run_help_exits_zero():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "run", "--help"])
    assert result.exit_code == 0
    assert "--suite" in result.output
    assert "--model" in result.output


def test_eval_run_missing_suite_fails_cleanly():
    runner = CliRunner()
    with patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()):
        result = runner.invoke(cli, ["eval", "run", "--suite", "nonexistent"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output or "Error" in result.output


def test_eval_run_writes_result_record(tmp_path, monkeypatch):
    log_path = tmp_path / ".openshard" / "eval-runs.jsonl"
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()),
        patch("openshard.run.pipeline._copy_cwd_to_workspace"),
        patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
    ):
        runner = CliRunner()
        runner.invoke(cli, ["eval", "run", "--suite", "basic"])

    assert log_path.exists(), "eval-runs.jsonl was not created"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 10  # one per task in basic suite

    required_keys = {
        "timestamp", "suite", "task_id", "model", "passed",
        "duration_seconds", "verification_attempted", "verification_passed",
        "verification_returncode", "verification_output",
        "files_written", "unsafe_files",
        "prompt_tokens", "completion_tokens", "total_tokens", "cost", "error",
    }
    for line in lines:
        record = json.loads(line)
        assert required_keys.issubset(record.keys()), f"missing keys in: {record}"
        assert record["suite"] == "basic"


def test_eval_run_prints_pass_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()),
        patch("openshard.run.pipeline._copy_cwd_to_workspace"),
        patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "run", "--suite", "basic"])

    task_ids = {"docs_update", "helper_function", "cli_flag"}
    for task_id in task_ids:
        assert task_id in result.output, f"{task_id} not in output"
    assert "PASS" in result.output or "FAIL" in result.output


def test_generated_unsafe_path_is_not_written(tmp_path, monkeypatch):
    unsafe_file = ChangedFile(path="../../etc/passwd", change_type="create", content="pwned", summary="unsafe")
    fake = _fake_result(files=[unsafe_file])

    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=fake),
        patch("openshard.run.pipeline._copy_cwd_to_workspace"),
        patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
    ):
        runner = CliRunner()
        runner.invoke(cli, ["eval", "run", "--suite", "basic"])

    log_path = tmp_path / ".openshard" / "eval-runs.jsonl"
    assert log_path.exists()
    for line in log_path.read_text(encoding="utf-8").strip().splitlines():
        record = json.loads(line)
        assert "../../etc/passwd" not in record["files_written"]
        assert "../../etc/passwd" in record["unsafe_files"] or record["files_written"] == []

    passwd_path = tmp_path / "etc" / "passwd"
    assert not passwd_path.exists(), "unsafe path was written to disk"


# ---------------------------------------------------------------------------
# eval report tests — no AI, reads from a JSONL fixture written by the test
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _rec(
    task_id: str = "t1",
    model: str = "m1",
    suite: str = "basic",
    passed: bool = True,
    duration: float = 1.0,
    tokens: int = 100,
    unsafe: list[str] | None = None,
) -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00Z",
        "suite": suite,
        "task_id": task_id,
        "model": model,
        "passed": passed,
        "duration_seconds": duration,
        "total_tokens": tokens,
        "unsafe_files": unsafe or [],
        "error": None,
        "verification_attempted": True,
        "verification_passed": passed,
        "verification_returncode": 0 if passed else 1,
        "verification_output": "",
        "files_written": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def test_eval_report_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "report"])
    assert result.exit_code == 0, result.output
    assert "No eval runs found" in result.output


def test_eval_report_shows_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1", passed=True),
        _rec(task_id="t2", passed=True),
        _rec(task_id="t3", passed=False),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "report"])
    assert result.exit_code == 0, result.output
    assert "Total runs" in result.output
    assert "3" in result.output
    assert "Passed" in result.output
    assert "2" in result.output
    assert "Failed" in result.output
    assert "1" in result.output
    assert "66.7" in result.output


def test_eval_report_suite_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1", suite="basic", passed=True),
        _rec(task_id="t2", suite="basic", passed=True),
        _rec(task_id="t3", suite="other", passed=False),
    ])
    runner = CliRunner()

    result = runner.invoke(cli, ["eval", "report", "--suite", "basic"])
    assert result.exit_code == 0, result.output
    assert "2" in result.output
    assert "t1" in result.output
    assert "t3" not in result.output

    result2 = runner.invoke(cli, ["eval", "report", "--suite", "other"])
    assert result2.exit_code == 0, result2.output
    assert "t3" in result2.output
    assert "t1" not in result2.output


def test_eval_report_model_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1", model="m1", passed=True),
        _rec(task_id="t2", model="m2", passed=False),
    ])
    runner = CliRunner()

    result = runner.invoke(cli, ["eval", "report", "--model", "m1"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    assert "t2" not in result.output

    result2 = runner.invoke(cli, ["eval", "report", "--model", "m2"])
    assert result2.exit_code == 0, result2.output
    assert "t2" in result2.output
    assert "t1" not in result2.output


def test_eval_report_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(_rec(task_id="good")) + "\n"
        + "\n"
        + "not valid json at all\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "report"])
    assert result.exit_code == 0, result.output
    assert "good" in result.output
    assert "Total runs" in result.output


def test_eval_report_no_matching_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [_rec(suite="basic")])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "report", "--suite", "nosuchsuite"])
    assert result.exit_code == 0, result.output
    assert "No records match" in result.output


# ---------------------------------------------------------------------------
# fixture copying and suite-level sanity checks
# ---------------------------------------------------------------------------

def test_run_eval_task_copies_fixture_files(tmp_path):
    task_dir = tmp_path / "my_task"
    task_dir.mkdir()
    (task_dir / "prompt.txt").write_text("Do something.")
    (task_dir / "metadata.json").write_text(
        json.dumps({
            "id": "my_task",
            "title": "My Task",
            "category": "standard",
            "expected_files": ["foo.py"],
            "verification_command": None,
        })
    )
    fixtures = task_dir / "fixtures"
    fixtures.mkdir()
    (fixtures / "seed.txt").write_text("hello fixture")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    task = EvalTask(
        id="my_task",
        title="My Task",
        category="standard",
        expected_files=["foo.py"],
        verification_command=None,
        prompt="Do something.",
        task_dir=task_dir,
    )

    fake_result = ExecutionResult(
        summary="done",
        files=[],
        notes=[],
        usage=UsageStats(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )
    with patch("openshard.evals.runner.ExecutionGenerator") as mock_cls:
        mock_cls.return_value.generate.return_value = fake_result
        run_eval_task(task, model="test-model", suite="test", workspace_root=workspace)

    assert (workspace / "seed.txt").exists(), "fixture file was not copied into workspace"
    assert (workspace / "seed.txt").read_text() == "hello fixture"


def test_basic_suite_tasks_have_verification_commands():
    tasks = load_eval_tasks("basic")
    for task in tasks:
        assert task.verification_command is not None, (
            f"task {task.id!r} has no verification_command — eval run will always fail"
        )


# ---------------------------------------------------------------------------
# eval compare tests — all mock ExecutionGenerator.generate (no real AI calls)
# ---------------------------------------------------------------------------

_COMPARE_PATCHES = (
    patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()),
    patch("openshard.run.pipeline._copy_cwd_to_workspace"),
    patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
)


def test_eval_compare_help_exits_zero():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "compare", "--help"])
    assert result.exit_code == 0
    assert "--suite" in result.output
    assert "--models" in result.output


def test_eval_compare_requires_models():
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "compare"])
    assert result.exit_code != 0


def test_eval_compare_writes_results_for_each_model(tmp_path, monkeypatch):
    log_path = tmp_path / ".openshard" / "eval-runs.jsonl"
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()),
        patch("openshard.run.pipeline._copy_cwd_to_workspace"),
        patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
    ):
        runner = CliRunner()
        runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "modelA,modelB"])

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    # 2 models × 10 tasks in basic suite = 20 records
    assert len(lines) == 20
    models_seen = {json.loads(line)["model"] for line in lines}
    assert models_seen == {"modelA", "modelB"}


def test_eval_compare_prints_per_model_task_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()),
        patch("openshard.run.pipeline._copy_cwd_to_workspace"),
        patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "modelA,modelB"])

    assert "[modelA]" in result.output
    assert "[modelB]" in result.output
    for task_id in ("cli_flag", "docs_update", "helper_function"):
        assert result.output.count(task_id) == 2, f"{task_id} should appear once per model"


def test_eval_compare_summary_table_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.execution.generator.ExecutionGenerator.generate", return_value=_fake_result()),
        patch("openshard.run.pipeline._copy_cwd_to_workspace"),
        patch("openshard.verification.executor.run_verification_plan", return_value=(0, "ok")),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "modelA,modelB"])

    for col in ("Runs", "Pass", "Fail", "Rate"):
        assert col in result.output
    assert "modelA" in result.output
    assert "modelB" in result.output


def _make_eval_result(passed: bool) -> EvalResult:
    return EvalResult(
        timestamp="2026-01-01T00:00:00+00:00",
        suite="basic",
        task_id="t1",
        model="modelA",
        passed=passed,
        duration_seconds=0.1,
        verification_attempted=True,
        verification_passed=passed,
        verification_returncode=0 if passed else 1,
        verification_output="ok" if passed else "fail",
    )


def test_eval_compare_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.cli.main.run_eval_task", return_value=_make_eval_result(True)),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "modelA"])
    assert result.exit_code == 0, result.output

    with (
        patch("openshard.cli.main.run_eval_task", return_value=_make_eval_result(False)),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "modelA"])
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# eval stats — unit tests for load_eval_runs and compute_eval_stats
# ---------------------------------------------------------------------------

def test_load_eval_runs_missing_file(tmp_path):
    assert load_eval_runs(tmp_path / "nonexistent.jsonl") == []


def test_load_eval_runs_empty_file(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text("", encoding="utf-8")
    assert load_eval_runs(p) == []


def test_load_eval_runs_skips_blank_lines(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text('\n{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
    assert load_eval_runs(p) == [{"a": 1}, {"b": 2}]


def test_load_eval_runs_skips_malformed_json(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"b": 2}\n', encoding="utf-8")
    assert load_eval_runs(p) == [{"a": 1}, {"b": 2}]


def test_compute_eval_stats_grouping():
    records = [
        _rec(task_id="t1", model="m1", suite="s1", passed=True),
        _rec(task_id="t1", model="m1", suite="s1", passed=False),
        _rec(task_id="t2", model="m2", suite="s1", passed=True),
    ]
    rows = compute_eval_stats(records)
    assert len(rows) == 2
    by_key = {(r.model, r.task_id): r for r in rows}
    assert by_key[("m1", "t1")].run_count == 2
    assert by_key[("m1", "t1")].pass_count == 1
    assert by_key[("m1", "t1")].fail_count == 1
    assert by_key[("m1", "t1")].pass_rate == pytest.approx(0.5)
    assert by_key[("m2", "t2")].pass_count == 1


def test_compute_eval_stats_skips_records_missing_required_fields():
    records = [
        {"suite": "s", "task_id": "t"},          # missing model
        {"model": "m", "task_id": "t"},           # missing suite
        {"model": "m", "suite": "s"},             # missing task_id
        _rec(task_id="t1", model="m1", suite="s1"),
    ]
    rows = compute_eval_stats(records)
    assert len(rows) == 1


def test_compute_eval_stats_avg_duration_only_numeric():
    records = [
        {**_rec(), "duration_seconds": 2.0},
        {**_rec(), "duration_seconds": "bad"},
        {**_rec(), "duration_seconds": 4.0},
    ]
    rows = compute_eval_stats(records)
    assert rows[0].avg_duration == pytest.approx(3.0)


def test_compute_eval_stats_avg_tokens_includes_zero():
    records = [
        {**_rec(), "total_tokens": 0},
        {**_rec(), "total_tokens": 200},
    ]
    rows = compute_eval_stats(records)
    assert rows[0].avg_total_tokens == pytest.approx(100.0)


def test_compute_eval_stats_avg_tokens_none_when_missing():
    records = [
        {k: v for k, v in _rec().items() if k != "total_tokens"},
    ]
    rows = compute_eval_stats(records)
    assert rows[0].avg_total_tokens is None


def test_compute_eval_stats_unsafe_count():
    records = [
        _rec(unsafe=["a/b"]),
        _rec(unsafe=["x", "y"]),
        _rec(unsafe=[]),
    ]
    rows = compute_eval_stats(records)
    assert rows[0].unsafe_file_count == 3


def test_compute_eval_stats_unsafe_count_skips_non_list():
    records = [
        {**_rec(), "unsafe_files": "not-a-list"},
        _rec(unsafe=["x"]),
    ]
    rows = compute_eval_stats(records)
    assert rows[0].unsafe_file_count == 1


def test_compute_eval_stats_suite_filter():
    records = [_rec(suite="a"), _rec(suite="b")]
    rows = compute_eval_stats(records, suite="a")
    assert all(r.suite == "a" for r in rows)
    assert len(rows) == 1


def test_compute_eval_stats_model_filter():
    records = [_rec(model="m1"), _rec(model="m2")]
    rows = compute_eval_stats(records, model="m1")
    assert all(r.model == "m1" for r in rows)


def test_compute_eval_stats_task_filter():
    records = [_rec(task_id="t1"), _rec(task_id="t2")]
    rows = compute_eval_stats(records, task="t1")
    assert all(r.task_id == "t1" for r in rows)


# ---------------------------------------------------------------------------
# eval stats — CLI tests
# ---------------------------------------------------------------------------

def test_eval_stats_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats"])
    assert result.exit_code == 0, result.output
    assert "No eval results found" in result.output


def test_eval_stats_basic_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1", model="m1", suite="basic", passed=True),
        _rec(task_id="t1", model="m1", suite="basic", passed=False),
        _rec(task_id="t2", model="m1", suite="basic", passed=True),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    assert "t2" in result.output
    assert "total: 3 runs" in result.output
    assert "pass: 2" in result.output
    assert "fail: 1" in result.output


def test_eval_stats_suite_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1", suite="foo"),
        _rec(task_id="t2", suite="bar"),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--suite", "foo"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    assert "t2" not in result.output


def test_eval_stats_model_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1", model="m1"),
        _rec(task_id="t2", model="m2"),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--model", "m1"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    assert "t2" not in result.output


def test_eval_stats_task_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="t1"),
        _rec(task_id="t2"),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--task", "t1"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    assert "t2" not in result.output


def test_eval_stats_output_has_suite_model_task_columns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [_rec(task_id="mytask", model="mymodel", suite="mysuite")])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats"])
    assert result.exit_code == 0, result.output
    assert "mysuite" in result.output
    assert "mymodel" in result.output
    assert "mytask" in result.output


def test_eval_stats_sorted_by_suite_model_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _rec(task_id="z", model="m", suite="b"),
        _rec(task_id="a", model="m", suite="a"),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats"])
    assert result.exit_code == 0, result.output
    idx_a = result.output.index("a")
    idx_z = result.output.index("z")
    assert idx_a < idx_z


# ---------------------------------------------------------------------------
# rank_models — unit tests
# ---------------------------------------------------------------------------

def _er(
    model: str = "model-a",
    passed: bool = True,
    cost: float | None = None,
    duration: float = 1.0,
    tokens: int = 100,
    unsafe: list[str] | None = None,
) -> EvalResult:
    return EvalResult(
        timestamp="2026-01-01T00:00:00+00:00",
        suite="basic",
        task_id="t1",
        model=model,
        passed=passed,
        duration_seconds=duration,
        verification_attempted=True,
        verification_passed=passed,
        verification_returncode=0 if passed else 1,
        verification_output="ok",
        total_tokens=tokens,
        cost=cost,
        unsafe_files=unsafe or [],
    )


def test_rank_models_sorts_by_pass_rate():
    results = {
        "model-a": [_er("model-a", passed=True), _er("model-a", passed=False)],  # 50%
        "model-b": [_er("model-b", passed=True), _er("model-b", passed=True)],   # 100%
    }
    ranking = rank_models(results)
    assert ranking[0].model == "model-b"
    assert ranking[1].model == "model-a"


def test_rank_models_tiebreak_by_cost_per_pass():
    results = {
        "model-a": [_er("model-a", passed=True, cost=0.005)],  # 100%, higher cost
        "model-b": [_er("model-b", passed=True, cost=0.002)],  # 100%, lower cost
    }
    ranking = rank_models(results)
    assert ranking[0].model == "model-b"
    assert ranking[1].model == "model-a"


def test_rank_models_cost_none_sorts_after_known_cost():
    results = {
        "model-a": [_er("model-a", passed=True, cost=None)],   # 100%, no cost
        "model-b": [_er("model-b", passed=True, cost=0.010)],  # 100%, high cost
    }
    ranking = rank_models(results)
    # model-b has known cost (even if expensive) → sorts before model-a (unknown)
    assert ranking[0].model == "model-b"
    assert ranking[1].model == "model-a"


def test_rank_models_no_passing_runs_cost_none():
    results = {"model-a": [_er("model-a", passed=False, cost=0.001)]}
    ranking = rank_models(results)
    assert ranking[0].cost_per_pass is None


def test_rank_models_any_pass_missing_cost_gives_none():
    results = {
        "model-a": [
            _er("model-a", passed=True, cost=0.003),
            _er("model-a", passed=True, cost=None),  # one passing run has no cost
        ]
    }
    ranking = rank_models(results)
    assert ranking[0].cost_per_pass is None


def test_rank_models_all_passes_have_cost_computes_correctly():
    results = {
        "model-a": [
            _er("model-a", passed=True, cost=0.004),
            _er("model-a", passed=True, cost=0.006),
            _er("model-a", passed=False, cost=0.001),  # failed run not in cost_per_pass
        ]
    }
    ranking = rank_models(results)
    assert ranking[0].cost_per_pass == pytest.approx(0.005)


def test_rank_models_tiebreak_by_duration():
    results = {
        "model-a": [_er("model-a", passed=True, cost=0.002, duration=3.0)],
        "model-b": [_er("model-b", passed=True, cost=0.002, duration=1.0)],
    }
    ranking = rank_models(results)
    assert ranking[0].model == "model-b"


def test_rank_models_tiebreak_by_unsafe():
    results = {
        "model-a": [_er("model-a", passed=True, cost=0.002, duration=1.0, unsafe=["bad/path"])],
        "model-b": [_er("model-b", passed=True, cost=0.002, duration=1.0, unsafe=[])],
    }
    ranking = rank_models(results)
    assert ranking[0].model == "model-b"


def test_rank_models_assigns_sequential_ranks():
    results = {
        "model-a": [_er("model-a", passed=True)],
        "model-b": [_er("model-b", passed=True)],
        "model-c": [_er("model-c", passed=True)],
    }
    ranking = rank_models(results)
    assert [e.rank for e in ranking] == [1, 2, 3]


def test_rank_models_single_model():
    results = {"model-a": [_er("model-a", passed=True)]}
    ranking = rank_models(results)
    assert len(ranking) == 1
    assert ranking[0].rank == 1


# ---------------------------------------------------------------------------
# eval compare — ranking section CLI tests
# ---------------------------------------------------------------------------

def _make_er(model: str, passed: bool, cost: float | None = None) -> EvalResult:
    return EvalResult(
        timestamp="2026-01-01T00:00:00+00:00",
        suite="basic",
        task_id="t1",
        model=model,
        passed=passed,
        duration_seconds=1.0,
        verification_attempted=True,
        verification_passed=passed,
        verification_returncode=0 if passed else 1,
        verification_output="ok",
        cost=cost,
    )


def test_eval_compare_ranking_section_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = iter([
        _make_er("model-a", passed=True),
        _make_er("model-a", passed=True),
        _make_er("model-b", passed=False),
        _make_er("model-b", passed=False),
    ] * 10)

    with (
        patch("openshard.cli.main.run_eval_task", side_effect=results),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "model-a,model-b"])

    assert "[ranking]" in result.output


def test_eval_compare_ranking_not_shown_for_single_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with (
        patch("openshard.cli.main.run_eval_task", return_value=_make_er("model-a", passed=True)),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "model-a"])

    assert "[ranking]" not in result.output


def test_eval_compare_ranking_order_by_pass_rate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # model-b passes all 10, model-a passes none
    side_effects = (
        [_make_er("model-a", passed=False)] * 10
        + [_make_er("model-b", passed=True)] * 10
    )

    with (
        patch("openshard.cli.main.run_eval_task", side_effect=side_effects),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "model-a,model-b"])

    ranking_section = result.output.split("[ranking]")[-1]
    idx_a = ranking_section.index("model-a")
    idx_b = ranking_section.index("model-b")
    assert idx_b < idx_a  # model-b (100%) ranked above model-a (0%)


def test_eval_compare_ranking_dash_when_no_cost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    side_effects = (
        [_make_er("model-a", passed=True, cost=None)] * 10
        + [_make_er("model-b", passed=True, cost=None)] * 10
    )

    with (
        patch("openshard.cli.main.run_eval_task", side_effect=side_effects),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "model-a,model-b"])

    assert "[ranking]" in result.output
    ranking_section = result.output.split("[ranking]")[-1]
    assert "cost/pass: -" in ranking_section


def test_eval_compare_ranking_shows_cost_when_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    side_effects = (
        [_make_er("model-a", passed=True, cost=0.0031)] * 10
        + [_make_er("model-b", passed=True, cost=0.0009)] * 10
    )

    with (
        patch("openshard.cli.main.run_eval_task", side_effect=side_effects),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "model-a,model-b"])

    ranking_section = result.output.split("[ranking]")[-1]
    assert "$" in ranking_section


def test_eval_compare_all_failing_models_shows_unavailable_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    side_effects = (
        [_make_er("model-a", passed=False)] * 10
        + [_make_er("model-b", passed=False)] * 10
    )

    with (
        patch("openshard.cli.main.run_eval_task", side_effect=side_effects),
        patch("openshard.cli.main.append_eval_result"),
        patch("openshard.cli.main._copy_cwd_to_workspace"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["eval", "compare", "--suite", "basic", "--models", "model-a,model-b"])

    assert "[ranking]" in result.output
    ranking_section = result.output.split("[ranking]")[-1]
    assert "no passing runs" in ranking_section
    assert "unavailable" in ranking_section
    assert "1." not in ranking_section  # no numbered entries


# ---------------------------------------------------------------------------
# category-aware stats — unit tests for build_category_map and compute_category_stats
# ---------------------------------------------------------------------------



def _crec(
    task_id: str = "t1",
    model: str = "m1",
    suite: str = "basic",
    passed: bool = True,
    duration: float = 1.0,
    tokens: int = 100,
    cost: float | None = None,
    unsafe: list[str] | None = None,
) -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00Z",
        "suite": suite,
        "task_id": task_id,
        "model": model,
        "passed": passed,
        "duration_seconds": duration,
        "total_tokens": tokens,
        "cost": cost,
        "unsafe_files": unsafe or [],
        "error": None,
        "verification_attempted": True,
        "verification_passed": passed,
        "verification_returncode": 0 if passed else 1,
        "verification_output": "",
        "files_written": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def test_build_category_map_basic():
    cmap = build_category_map("basic")
    assert cmap["add_docstring"] == "boilerplate"
    assert cmap["docs_update"] == "boilerplate"
    assert cmap["bug_fix"] == "standard"
    assert cmap["cli_flag"] == "standard"
    assert len(cmap) == 10


def test_build_category_map_unknown_suite():
    import pytest
    with pytest.raises(FileNotFoundError):
        build_category_map("nonexistent_suite_xyz")


def test_compute_category_stats_grouping():
    cmap = {"s1": {"t1": "catA", "t2": "catB"}}
    records = [
        _crec(task_id="t1", model="m1", suite="s1", passed=True),
        _crec(task_id="t1", model="m1", suite="s1", passed=False),
        _crec(task_id="t2", model="m1", suite="s1", passed=True),
        _crec(task_id="t1", model="m2", suite="s1", passed=True),
    ]
    rows = compute_category_stats(records, cmap)
    assert len(rows) == 3
    by_key = {(r.category, r.model): r for r in rows}
    assert by_key[("catA", "m1")].run_count == 2
    assert by_key[("catA", "m1")].pass_count == 1
    assert by_key[("catA", "m2")].run_count == 1
    assert by_key[("catB", "m1")].run_count == 1


def test_compute_category_stats_unknown_task_uses_unknown():
    cmap = {"s1": {"t1": "catA"}}
    records = [_crec(task_id="deleted_task", model="m1", suite="s1")]
    rows = compute_category_stats(records, cmap)
    assert len(rows) == 1
    assert rows[0].category == "unknown"


def test_compute_category_stats_unknown_suite_uses_unknown():
    cmap = {}  # no mapping for suite "s2"
    records = [_crec(task_id="t1", model="m1", suite="s2")]
    rows = compute_category_stats(records, cmap)
    assert len(rows) == 1
    assert rows[0].category == "unknown"


def test_compute_category_stats_cost_per_pass_none_when_any_pass_missing_cost():
    cmap = {"s1": {"t1": "catA"}}
    records = [
        _crec(task_id="t1", model="m1", suite="s1", passed=True, cost=0.005),
        _crec(task_id="t1", model="m1", suite="s1", passed=True, cost=None),
    ]
    rows = compute_category_stats(records, cmap)
    assert rows[0].cost_per_pass is None


def test_compute_category_stats_cost_per_pass_computed_correctly():
    cmap = {"s1": {"t1": "catA"}}
    records = [
        _crec(task_id="t1", model="m1", suite="s1", passed=True, cost=0.004),
        _crec(task_id="t1", model="m1", suite="s1", passed=True, cost=0.006),
        _crec(task_id="t1", model="m1", suite="s1", passed=False, cost=0.001),
    ]
    rows = compute_category_stats(records, cmap)
    import pytest
    assert rows[0].cost_per_pass == pytest.approx(0.005)


def test_compute_category_stats_cost_per_pass_none_when_no_passes():
    cmap = {"s1": {"t1": "catA"}}
    records = [_crec(task_id="t1", model="m1", suite="s1", passed=False, cost=0.001)]
    rows = compute_category_stats(records, cmap)
    assert rows[0].cost_per_pass is None


def test_compute_category_stats_suite_filter():
    cmap = {"a": {"t1": "catA"}, "b": {"t1": "catA"}}
    records = [_crec(suite="a"), _crec(suite="b")]
    rows = compute_category_stats(records, cmap, suite="a")
    assert all(r.suite == "a" for r in rows)
    assert len(rows) == 1


def test_compute_category_stats_model_filter():
    cmap = {"s1": {"t1": "catA"}}
    records = [_crec(model="m1"), _crec(model="m2")]
    rows = compute_category_stats(records, cmap, model="m1")
    assert all(r.model == "m1" for r in rows)
    assert len(rows) == 1


def test_compute_category_stats_task_filter():
    cmap = {"s1": {"t1": "catA", "t2": "catB"}}
    records = [
        _crec(task_id="t1", suite="s1"),
        _crec(task_id="t2", suite="s1"),
    ]
    rows = compute_category_stats(records, cmap, task="t1")
    assert len(rows) == 1
    assert rows[0].category == "catA"


# ---------------------------------------------------------------------------
# category-aware stats — CLI tests
# ---------------------------------------------------------------------------

def test_eval_stats_by_category_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--by-category"])
    assert result.exit_code == 0, result.output
    assert "No eval results found" in result.output


def test_eval_stats_by_category_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _crec(task_id="bug_fix", model="m1", suite="basic", passed=True),
        _crec(task_id="add_docstring", model="m1", suite="basic", passed=True),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--by-category"])
    assert result.exit_code == 0, result.output
    assert "category" in result.output
    assert "standard" in result.output
    assert "boilerplate" in result.output
    assert "[eval stats --by-category]" in result.output


def test_eval_stats_by_category_cost_dash_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _crec(task_id="bug_fix", model="m1", suite="basic", passed=True, cost=None),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--by-category"])
    assert result.exit_code == 0, result.output
    assert "$0.0000" not in result.output
    assert "cost/pass" in result.output


def test_eval_stats_by_category_suite_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _crec(task_id="bug_fix", model="m1", suite="basic", passed=True),
        _crec(task_id="t1", model="m1", suite="other", passed=True),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--by-category", "--suite", "basic"])
    assert result.exit_code == 0, result.output
    assert "basic" in result.output
    assert "other" not in result.output


def test_eval_stats_by_category_task_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [
        _crec(task_id="bug_fix", model="m1", suite="basic"),
        _crec(task_id="cli_flag", model="m1", suite="basic"),
    ])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats", "--by-category", "--task", "bug_fix"])
    assert result.exit_code == 0, result.output
    assert "standard" in result.output
    assert "total: 1 runs" in result.output


def test_eval_stats_normal_unchanged_by_category_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / ".openshard" / "eval-runs.jsonl"
    _write_jsonl(log, [_rec(task_id="t1", model="m1", suite="basic")])
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "stats"])
    assert result.exit_code == 0, result.output
    assert "[eval stats]" in result.output
    assert "task_id" in result.output
    assert "category" not in result.output
