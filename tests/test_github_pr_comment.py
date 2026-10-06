"""Tests for openshard.github.pr_comment.

Covers PRCommentSummary safety, build_pr_comment_summary, render_pr_comment,
and the CLI command openshard pr comment.

No network calls, no shell execution, no provider calls.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.github.pr_comment import (
    _MAX_CHECKS,
    _MAX_EVIDENCE,
    _MAX_INSPECTED_FILES,
    _MAX_OSN_SECTIONS,
    _MAX_TEXT,
    _MAX_WARNINGS,
    PRCommentSummary,
    build_pr_comment_summary,
    render_pr_comment,
)
from openshard.history.shard_contract import ShardReceipt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_receipt(**overrides) -> ShardReceipt:
    defaults = dict(
        shard_id="test-001",
        created_at="2026-01-01T00:00:00Z",
        task_short="test task",
        task_full="test task full",
        agent="native",
        strategy="Single",
        model_display="Claude Sonnet",
        risk="low",
        sandbox="trusted",
        files_changed=2,
        checks_display="Not run",
        approval="not required",
        cost_display="$0.01",
        result="done",
        status="completed",
        duration_seconds=5.0,
    )
    defaults.update(overrides)
    return ShardReceipt(**defaults)


def _minimal_entry(**overrides) -> dict:
    base: dict = {"workflow": "native", "executor": "native"}
    base.update(overrides)
    return base


_ONE_RUN_ENTRY = json.dumps(_minimal_entry())


def _invoke(args: list[str], runs_content: str | None = _ONE_RUN_ENTRY):
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        log_dir = td_path / ".openshard"
        log_dir.mkdir()
        if runs_content is not None:
            (log_dir / "runs.jsonl").write_text(runs_content, encoding="utf-8")
        with patch("openshard.cli.main.Path.cwd", return_value=td_path):
            result = runner.invoke(cli, ["pr"] + args)
    return result


# ---------------------------------------------------------------------------
# PRCommentSummary dataclass safety (tests 1-6)
# ---------------------------------------------------------------------------

def test_defaults_are_safe():
    s = PRCommentSummary()
    assert s.enabled is True
    assert s.source == "github_pr_comment_v1"
    assert s.raw_content_stored is False
    assert s.manual_review_required is False
    assert s.inspected_files == []
    assert s.checks == []
    assert s.warnings == []
    assert s.evidence == []
    assert s.osn_sections == []


def test_raw_content_stored_always_false():
    s = PRCommentSummary(raw_content_stored=False)
    assert s.raw_content_stored is False
    d = dataclasses.asdict(s)
    assert d["raw_content_stored"] is False


def test_lists_are_independent_per_instance():
    a = PRCommentSummary()
    b = PRCommentSummary()
    a.checks.append("x")
    assert b.checks == []


def test_text_fields_exist_and_are_strings():
    s = PRCommentSummary()
    assert isinstance(s.title, str)
    assert isinstance(s.run_status, str)
    assert isinstance(s.risk, str)
    assert isinstance(s.recommended_next_step, str)


def test_json_safe_via_asdict():
    s = PRCommentSummary(run_status="completed", risk="low", files_changed=3)
    d = dataclasses.asdict(s)
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["run_status"] == "completed"
    assert decoded["raw_content_stored"] is False


def test_no_raw_output_fields_on_dataclass():
    fields = {f.name for f in dataclasses.fields(PRCommentSummary)}
    forbidden = {"raw_prompt", "raw_output", "raw_command_output", "chain_of_thought"}
    assert not fields & forbidden


# ---------------------------------------------------------------------------
# Builder: basic mappings (tests 7-10)
# ---------------------------------------------------------------------------

def test_builds_from_minimal_entry_and_receipt():
    entry = _minimal_entry()
    receipt = _minimal_receipt()
    summary = build_pr_comment_summary(entry, receipt)
    assert isinstance(summary, PRCommentSummary)
    assert summary.raw_content_stored is False


def test_status_maps_from_receipt():
    receipt = _minimal_receipt(status="failed")
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.run_status == "failed"


def test_files_changed_maps_from_receipt():
    receipt = _minimal_receipt(files_changed=7)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.files_changed == 7
    assert summary.files_observable is True
    assert "**Files changed:** 7" in render_pr_comment(summary)


def test_unobservable_file_changes_are_not_rendered_as_zero():
    receipt = _minimal_receipt(
        files_changed=0, changes={"agent_reported": 0, "git_observed": 0, "files_observable": False},
    )
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.files_observable is False
    assert dataclasses.asdict(summary)["files_changed"] == 0  # existing JSON field kept
    md = render_pr_comment(summary)
    assert "**Files changed:** not observable" in md
    assert "**Files changed:** 0" not in md


def test_zero_files_on_observable_capture_still_renders_zero():
    receipt = _minimal_receipt(files_changed=0, changes={"agent_reported": 0, "git_observed": 0})
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.files_observable is True
    assert "**Files changed:** 0" in render_pr_comment(summary)


def test_inspected_files_capped():
    long_list = [f"src/file_{i}.py" for i in range(20)]
    receipt = _minimal_receipt(inspected_files=long_list)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert len(summary.inspected_files) <= _MAX_INSPECTED_FILES


# ---------------------------------------------------------------------------
# Builder: caps and content filtering (tests 11-16)
# ---------------------------------------------------------------------------

def test_checks_capped():
    long_checks = [f"check_{i}" for i in range(30)]
    receipt = _minimal_receipt(check_results=long_checks)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert len(summary.checks) <= _MAX_CHECKS


def test_warnings_include_error_class():
    receipt = _minimal_receipt(error_class="timeout_error")
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert any("timeout_error" in w for w in summary.warnings)


def test_warnings_include_verification_skipped():
    loop_summary = {"enabled": True, "verification_status": "skipped"}
    entry = _minimal_entry(osn_loop_summary=loop_summary)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert any("skipped" in w.lower() for w in summary.warnings)


def test_manual_review_required_when_approval_required():
    receipt = _minimal_receipt(approval_required=True, approval_granted=None)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.manual_review_required is True


def test_manual_review_false_when_not_required():
    receipt = _minimal_receipt(approval_required=False, error_class=None)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.manual_review_required is False


def test_manual_review_when_verification_failed_fallback():
    # Old fallback path via osn_loop_summary.verification_status
    loop_summary = {"enabled": True, "verification_status": "failed"}
    entry = _minimal_entry(osn_loop_summary=loop_summary)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.manual_review_required is True


# ---------------------------------------------------------------------------
# Builder: explicit OSN metadata detection (tests 17-25)
# ---------------------------------------------------------------------------

def test_explicit_osn_observation_detected():
    entry = _minimal_entry(osn_observation={"enabled": True, "stack_signals": []})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN OBSERVATION" in summary.osn_sections


def test_explicit_osn_progress_memory_detected():
    entry = _minimal_entry(osn_progress_memory={"enabled": True, "attempted_steps": 3})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN PROGRESS" in summary.osn_sections


def test_explicit_osn_verification_contract_detected():
    entry = _minimal_entry(osn_verification_contract={"enabled": True, "strength": "strong"})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN VERIFICATION" in summary.osn_sections


def test_explicit_osn_retry_diagnosis_detected():
    entry = _minimal_entry(osn_retry_diagnosis={"enabled": True, "retry_count": 1})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN RETRY" in summary.osn_sections


def test_osn_loop_detected_via_loop_summary():
    entry = _minimal_entry(osn_loop_summary={"enabled": True, "steps_run": 3})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN LOOP" in summary.osn_sections


def test_osn_observation_fallback_when_explicit_missing():
    # No osn_observation key; fallback via loop step
    loop_summary = {
        "enabled": True,
        "steps": [{"name": "repo_observation", "status": "completed"}],
    }
    entry = _minimal_entry(osn_loop_summary=loop_summary)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN OBSERVATION" in summary.osn_sections


def test_osn_progress_fallback_when_explicit_missing():
    # No osn_progress_memory; fallback via osn_loop_summary existing
    entry = _minimal_entry(osn_loop_summary={"enabled": True, "attempted_steps": 2})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN PROGRESS" in summary.osn_sections


def test_osn_verification_fallback_when_explicit_missing():
    # No osn_verification_contract; fallback via validation_contract
    entry = _minimal_entry(validation_contract={"intent": "do x", "strength": "strong"})
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN VERIFICATION" in summary.osn_sections


def test_osn_retry_fallback_when_explicit_missing():
    # No osn_retry_diagnosis; fallback via retry_used in osn_loop_summary
    loop_summary = {"enabled": True, "retry_used": True, "retry_count": 1}
    entry = _minimal_entry(osn_loop_summary=loop_summary)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert "OSN RETRY" in summary.osn_sections


# ---------------------------------------------------------------------------
# Builder: explicit metadata for recommended next step (tests 26-29)
# ---------------------------------------------------------------------------

def test_osn_progress_memory_next_safe_step():
    prog = {"enabled": True, "next_safe_step": "Run focused tests before merging."}
    entry = _minimal_entry(osn_progress_memory=prog)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.recommended_next_step == "Run focused tests before merging."


def test_osn_retry_diagnosis_next_action_used_when_no_progress_step():
    retry = {"enabled": True, "next_action": "Retry with smaller patch."}
    entry = _minimal_entry(osn_retry_diagnosis=retry)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.recommended_next_step == "Retry with smaller patch."


def test_osn_progress_memory_takes_priority_over_retry_next_action():
    prog = {"enabled": True, "next_safe_step": "Run verification."}
    retry = {"enabled": True, "next_action": "Retry with smaller patch."}
    entry = _minimal_entry(osn_progress_memory=prog, osn_retry_diagnosis=retry)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.recommended_next_step == "Run verification."


def test_recommended_next_step_fallback():
    summary = build_pr_comment_summary(_minimal_entry(), _minimal_receipt())
    assert summary.recommended_next_step
    assert len(summary.recommended_next_step) <= _MAX_TEXT


# ---------------------------------------------------------------------------
# Builder: explicit metadata for manual review (tests 30-33)
# ---------------------------------------------------------------------------

def test_osn_verification_contract_manual_review():
    verif = {"enabled": True, "manual_review_required": True}
    entry = _minimal_entry(osn_verification_contract=verif)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.manual_review_required is True


def test_osn_retry_diagnosis_manual_review():
    retry = {"enabled": True, "manual_review_required": True}
    entry = _minimal_entry(osn_retry_diagnosis=retry)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.manual_review_required is True


def test_osn_progress_memory_blockers_manual_review():
    prog = {"enabled": True, "blockers": ["approval needed"]}
    entry = _minimal_entry(osn_progress_memory=prog)
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.manual_review_required is True


def test_error_class_triggers_manual_review():
    receipt = _minimal_receipt(error_class="execution_error", approval_required=False)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert summary.manual_review_required is True


# ---------------------------------------------------------------------------
# Builder: path safety (tests 34-35)
# ---------------------------------------------------------------------------

def test_absolute_paths_not_included():
    abs_files = ["/home/user/project/src/foo.py", "C:\\Users\\user\\project\\bar.py"]
    receipt = _minimal_receipt(inspected_files=abs_files)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    for f in summary.inspected_files:
        assert not f.startswith("/")
        assert ":\\" not in f


def test_codegraph_paths_not_included():
    mixed = ["src/foo.py", ".codegraph/nodes.json", "src/bar.py"]
    receipt = _minimal_receipt(inspected_files=mixed)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert all(".codegraph" not in f for f in summary.inspected_files)


# ---------------------------------------------------------------------------
# Renderer: structure (tests 36-46)
# ---------------------------------------------------------------------------

def _summary_with(**kwargs) -> PRCommentSummary:
    defaults = dict(
        run_status="completed",
        risk="low",
        files_changed=2,
        recommended_next_step="Run tests.",
    )
    defaults.update(kwargs)
    return PRCommentSummary(**defaults)


def test_markdown_includes_title():
    md = render_pr_comment(_summary_with())
    assert "## OpenShard Run Summary" in md


def test_markdown_includes_status():
    md = render_pr_comment(_summary_with(run_status="failed"))
    assert "**Status:** failed" in md


def test_markdown_includes_files_changed():
    md = render_pr_comment(_summary_with(files_changed=5))
    assert "**Files changed:** 5" in md


def test_markdown_includes_manual_review():
    md = render_pr_comment(_summary_with(manual_review_required=True))
    assert "**Manual review:** yes" in md


def test_markdown_includes_evidence_section():
    md = render_pr_comment(_summary_with(evidence=["Inspected 3 files"]))
    assert "### Evidence" in md
    assert "Inspected 3 files" in md


def test_markdown_includes_validation_when_checks_exist():
    md = render_pr_comment(_summary_with(checks=["python -m pytest tests/ -v"]))
    assert "### Validation" in md
    assert "python -m pytest tests/ -v" in md


def test_markdown_omits_validation_when_no_checks():
    md = render_pr_comment(_summary_with(checks=[]))
    assert "### Validation" not in md


def test_markdown_includes_warnings_when_present():
    md = render_pr_comment(_summary_with(warnings=["Something went wrong"]))
    assert "### Warnings" in md
    assert "Something went wrong" in md


def test_markdown_omits_warnings_when_empty():
    md = render_pr_comment(_summary_with(warnings=[]))
    assert "### Warnings" not in md


def test_markdown_includes_recommended_next_step():
    md = render_pr_comment(_summary_with(recommended_next_step="Check the diff."))
    assert "### Recommended next step" in md
    assert "Check the diff." in md


def test_markdown_includes_advisory_footer():
    md = render_pr_comment(_summary_with())
    assert "Generated locally by OpenShard. Advisory only." in md


# ---------------------------------------------------------------------------
# Renderer: safety constraints (tests 47-49)
# ---------------------------------------------------------------------------

def test_no_em_dash_in_output():
    summary = _summary_with(
        warnings=["a - b"],
        evidence=["c - d"],
        recommended_next_step="Do x - not y.",
    )
    md = render_pr_comment(summary)
    assert "—" not in md


def test_output_is_deterministic():
    summary = _summary_with(
        run_status="completed",
        risk="medium",
        files_changed=3,
        warnings=["w1", "w2"],
        evidence=["e1"],
        checks=["pytest tests/ -v"],
        recommended_next_step="Review.",
    )
    assert render_pr_comment(summary) == render_pr_comment(summary)


def test_no_chain_of_thought_phrases():
    summary = _summary_with(
        warnings=["clean run"],
        recommended_next_step="Merge when ready.",
    )
    md = render_pr_comment(summary)
    forbidden = ["let me think", "step by step", "chain of thought", "reasoning:"]
    lower = md.lower()
    for phrase in forbidden:
        assert phrase not in lower


# ---------------------------------------------------------------------------
# CLI (tests 50-56)
# ---------------------------------------------------------------------------

def test_pr_comment_handles_no_history():
    result = _invoke(["comment"], runs_content=None)
    assert result.exit_code == 0
    assert "No run history found" in result.output


def test_pr_comment_renders_markdown_for_latest_run():
    result = _invoke(["comment"])
    assert result.exit_code == 0
    assert "## OpenShard Run Summary" in result.output
    assert "Generated locally by OpenShard. Advisory only." in result.output


def test_pr_comment_output_has_no_em_dash():
    result = _invoke(["comment"])
    assert result.exit_code == 0
    assert "—" not in result.output


def test_pr_comment_output_flag_writes_file():
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        log_dir = td_path / ".openshard"
        log_dir.mkdir()
        (log_dir / "runs.jsonl").write_text(_ONE_RUN_ENTRY, encoding="utf-8")
        out_path = td_path / "output" / "comment.md"
        with patch("openshard.cli.main.Path.cwd", return_value=td_path):
            result = runner.invoke(cli, ["pr", "comment", "--output", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "## OpenShard Run Summary" in content


def test_pr_comment_does_not_call_network():
    import socket

    def _fail(*_a, **_kw):
        raise AssertionError("Network call detected")

    result = None
    with patch.object(socket.socket, "connect", _fail):
        result = _invoke(["comment"])
    assert result is not None
    assert result.exit_code == 0


def test_reflect_last_still_works():
    result = _invoke(["--help"])
    assert result.exit_code == 0


def test_pr_group_help_displayed_without_subcommand():
    result = _invoke([])
    assert result.exit_code == 0
    assert "comment" in result.output.lower() or "PR comment" in result.output


# ---------------------------------------------------------------------------
# Regression: caps and safety (tests 57-63)
# ---------------------------------------------------------------------------

def test_evidence_list_never_exceeds_cap():
    receipt = _minimal_receipt(
        evidence_capsules=[],
        policy_decisions=[{"decision": "allow"}] * 30,
        inspected_files=[f"src/f{i}.py" for i in range(30)],
    )
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert len(summary.evidence) <= _MAX_EVIDENCE


def test_warnings_list_never_exceeds_cap():
    receipt = _minimal_receipt(
        error_class="some_error",
        agent_notes=[f"note {i}" for i in range(25)],
        policy_decisions=[{"decision": "deny"}] * 5,
    )
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert len(summary.warnings) <= _MAX_WARNINGS


def test_osn_sections_never_exceeds_cap():
    loop_summary = {
        "enabled": True,
        "retry_used": True,
        "retry_count": 2,
        "steps": [{"name": "repo_observation", "status": "completed"}],
    }
    entry = _minimal_entry(
        osn_observation={"enabled": True},
        osn_progress_memory={"enabled": True},
        osn_loop_summary=loop_summary,
        osn_verification_contract={"enabled": True},
        osn_retry_diagnosis={"enabled": True},
        validation_contract={"intent": "x"},
        verification_contract_result={"overall_status": "passed"},
    )
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert len(summary.osn_sections) <= _MAX_OSN_SECTIONS


def test_text_fields_capped_at_max_text():
    long_str = "x" * 1000
    receipt = _minimal_receipt(status=long_str, risk=long_str, error_class=long_str)
    summary = build_pr_comment_summary(_minimal_entry(), receipt)
    assert len(summary.run_status) <= _MAX_TEXT
    assert len(summary.risk) <= _MAX_TEXT
    assert len(summary.recommended_next_step) <= _MAX_TEXT


def test_build_from_non_native_entry():
    entry = {"workflow": "standard", "executor": "openrouter"}
    summary = build_pr_comment_summary(entry, _minimal_receipt(status="completed"))
    assert isinstance(summary, PRCommentSummary)
    assert summary.run_status == "completed"


def test_render_empty_summary_is_safe():
    summary = PRCommentSummary()
    md = render_pr_comment(summary)
    assert "## OpenShard Run Summary" in md
    assert "Generated locally by OpenShard. Advisory only." in md
    assert "—" not in md


def test_explicit_metadata_takes_priority_over_fallback():
    # Both explicit osn_progress_memory AND osn_loop_summary present.
    # OSN PROGRESS should be listed once (via explicit path), not twice.
    entry = _minimal_entry(
        osn_progress_memory={"enabled": True},
        osn_loop_summary={"enabled": True},
    )
    summary = build_pr_comment_summary(entry, _minimal_receipt())
    assert summary.osn_sections.count("OSN PROGRESS") == 1
