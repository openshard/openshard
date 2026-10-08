from __future__ import annotations

from openshard.history.shard_contract import FileEvidence, ShardReceipt
from openshard.tui.action_blocks import (
    render_action_block,
    render_actions_section,
    render_check_actions_section,
    render_evidence_section,
    render_result_section,
)

# ---------------------------------------------------------------------------
# render_action_block
# ---------------------------------------------------------------------------


def test_render_action_block_title_only():
    result = render_action_block("Scanned repo")
    assert result == "  Scanned repo"
    assert "↳" not in result


def test_render_action_block_with_detail():
    result = render_action_block("Loaded workflow pack", "ready")
    assert "  Loaded workflow pack" in result
    assert "↳ [dim]ready[/dim]" in result


def test_render_action_block_empty_detail_omitted():
    result = render_action_block("Something", None)
    assert "↳" not in result


# ---------------------------------------------------------------------------
# render_actions_section
# ---------------------------------------------------------------------------


def test_render_actions_section_empty_list():
    assert render_actions_section([]) == ""


def test_render_actions_section_all_empty_labels():
    events = [{"label": "", "status": "completed"}, {"label": None, "status": "completed"}]
    assert render_actions_section(events) == ""


def test_render_actions_section_completed_events():
    events = [
        {"label": "Loaded workflow pack", "status": "completed"},
        {"label": "Scanned repo", "status": "completed"},
    ]
    result = render_actions_section(events)
    assert "[bold]ACTIONS[/bold]" in result
    assert "  Loaded workflow pack" in result
    assert "  Scanned repo" in result


def test_render_actions_section_skipped_event():
    events = [{"label": "Some step", "status": "skipped"}]
    result = render_actions_section(events)
    assert "↳ [dim]skipped[/dim]" in result


def test_render_actions_section_failed_event():
    events = [{"label": "Failed step", "status": "failed"}]
    result = render_actions_section(events)
    assert "↳ [dim]failed[/dim]" in result


def test_render_actions_section_count_event():
    events = [{"label": "Found raw findings", "status": "completed", "count": 27}]
    result = render_actions_section(events)
    assert "↳ [dim]27 total[/dim]" in result


def test_render_actions_section_explicit_detail():
    events = [{"label": "Custom step", "status": "completed", "detail": "my custom detail"}]
    result = render_actions_section(events)
    assert "↳ [dim]my custom detail[/dim]" in result


def test_render_actions_section_explicit_detail_takes_priority_over_count():
    events = [{"label": "Step", "status": "completed", "detail": "explicit", "count": 5}]
    result = render_actions_section(events)
    assert "explicit" in result
    assert "5 total" not in result


def test_render_actions_section_ends_with_newline():
    events = [{"label": "Scanned repo", "status": "completed"}]
    result = render_actions_section(events)
    assert result.endswith("\n")


# ---------------------------------------------------------------------------
# render_check_actions_section
# ---------------------------------------------------------------------------


def test_render_check_actions_section_empty_list():
    assert render_check_actions_section([]) == ""


def test_render_check_actions_section_all_empty_names():
    checks = [{"name": "", "status": "passed"}, {"name": None, "status": "passed"}]
    assert render_check_actions_section(checks) == ""


def test_render_check_actions_section_passed():
    checks = [{"name": "terraform fmt", "status": "passed"}]
    result = render_check_actions_section(checks)
    assert "[bold]CHECK ACTIONS[/bold]" in result
    assert "[green]✓[/green]" in result
    assert "terraform fmt" in result
    assert "passed" in result


def test_render_check_actions_section_failed():
    checks = [{"name": "terraform validate", "status": "failed"}]
    result = render_check_actions_section(checks)
    assert "[red]✗[/red]" in result
    assert "failed" in result


def test_render_check_actions_section_skipped_with_reason():
    checks = [{"name": "tflint", "status": "skipped", "reason": "tflint not installed"}]
    result = render_check_actions_section(checks)
    assert "[dim]-[/dim]" in result
    assert "skipped — tflint not installed" in result


def test_render_check_actions_section_skipped_no_reason():
    checks = [{"name": "tflint", "status": "skipped"}]
    result = render_check_actions_section(checks)
    assert "skipped" in result
    assert " — " not in result


def test_render_check_actions_section_with_summary():
    checks = [{"name": "unit tests", "status": "passed", "summary": "42 tests passed"}]
    result = render_check_actions_section(checks)
    assert "42 tests passed" in result
    assert "passed" in result


def test_render_check_actions_section_failed_with_summary():
    checks = [{"name": "unit tests", "status": "failed", "summary": "3 tests failed"}]
    result = render_check_actions_section(checks)
    assert "3 tests failed" in result


def test_render_check_actions_section_ends_with_newline():
    checks = [{"name": "terraform fmt", "status": "passed"}]
    result = render_check_actions_section(checks)
    assert result.endswith("\n")


def test_render_check_actions_section_multiple_checks():
    checks = [
        {"name": "terraform fmt", "status": "passed"},
        {"name": "tflint", "status": "skipped", "reason": "tflint not installed"},
        {"name": "unit tests", "status": "failed"},
    ]
    result = render_check_actions_section(checks)
    assert "terraform fmt" in result
    assert "tflint" in result
    assert "unit tests" in result
    assert "[green]✓[/green]" in result
    assert "[dim]-[/dim]" in result
    assert "[red]✗[/red]" in result


# ---------------------------------------------------------------------------
# render_evidence_section
# ---------------------------------------------------------------------------


def test_render_evidence_section_empty_list_returns_empty():
    assert render_evidence_section([]) == ""


def test_render_evidence_section_inspected_single_role():
    fe = [FileEvidence("src/main.py", ["inspected"])]
    result = render_evidence_section(fe)
    assert "Read src/main.py" in result
    assert "↳ [dim]inspected/read context[/dim]" in result
    assert "Finding source" not in result


def test_render_evidence_section_finding_source_single_role():
    fe = [FileEvidence("database.tf", ["finding_source"])]
    result = render_evidence_section(fe)
    assert "Finding source database.tf" in result
    assert "↳ [dim]finding source[/dim]" in result
    assert "Read" not in result


def test_render_evidence_section_changed_single_role():
    fe = [FileEvidence("main.py", ["changed"])]
    result = render_evidence_section(fe)
    assert "Changed main.py" in result
    assert "↳ [dim]changed[/dim]" in result


def test_render_evidence_section_header_is_bold():
    fe = [FileEvidence("foo.py", ["inspected"])]
    result = render_evidence_section(fe)
    assert "[bold]EVIDENCE[/bold]" in result


def test_render_evidence_section_ends_with_newline():
    fe = [FileEvidence("foo.py", ["inspected"])]
    result = render_evidence_section(fe)
    assert result.endswith("\n")


def test_render_evidence_section_blank_lines_between_items():
    fe = [FileEvidence("a.py", ["inspected"]), FileEvidence("b.py", ["inspected"])]
    result = render_evidence_section(fe)
    assert "\n\n" in result


def test_render_evidence_section_multi_role_uses_bare_path_as_title():
    fe = [FileEvidence("db.tf", ["inspected", "finding_source"])]
    result = render_evidence_section(fe)
    assert "  db.tf\n" in result
    assert "Read db.tf" not in result
    assert "Finding source db.tf" not in result


def test_render_evidence_section_multi_role_shows_all_role_lines():
    fe = [FileEvidence("db.tf", ["inspected", "finding_source"])]
    result = render_evidence_section(fe)
    assert "↳ [dim]inspected/read context[/dim]" in result
    assert "↳ [dim]finding source[/dim]" in result


def test_render_evidence_section_multi_role_no_duplicate_block():
    fe = [FileEvidence("db.tf", ["inspected", "finding_source"])]
    result = render_evidence_section(fe)
    assert result.count("db.tf") == 1


def test_render_evidence_section_truncates_at_max():
    fe = [FileEvidence(f"file{i}.py", ["inspected"]) for i in range(11)]
    result = render_evidence_section(fe)
    assert result.count("Read file") == 10
    assert "+1 more files" in result


def test_render_evidence_section_no_overflow_at_exact_limit():
    fe = [FileEvidence(f"file{i}.py", ["inspected"]) for i in range(10)]
    result = render_evidence_section(fe)
    assert "more files" not in result


def test_render_evidence_section_mixed_roles_present():
    fe = [
        FileEvidence("src/main.py", ["inspected"]),
        FileEvidence("database.tf", ["finding_source"]),
    ]
    result = render_evidence_section(fe)
    assert "Read src/main.py" in result
    assert "Finding source database.tf" in result


# ---------------------------------------------------------------------------
# render_result_section
# ---------------------------------------------------------------------------


def _make_receipt(**overrides) -> ShardReceipt:
    defaults = dict(
        shard_id="shard-20260524-0001",
        created_at="2026-05-24T00:12:00Z",
        task_short="review this",
        task_full="review this repo",
        agent="OpenShard Native",
        strategy="Not recorded",
        model_display="Claude Sonnet 4.5",
        risk="High",
        sandbox="Off",
        files_changed=0,
        checks_display="3 skipped",
        approval="Not required",
        cost_display="$0.0480",
        result="9 issue areas found. 27 raw findings recorded.",
        status="Checks: 3 skipped",
        duration_seconds=12.3,
    )
    defaults.update(overrides)
    return ShardReceipt(**defaults)


def test_render_result_section_header_bold():
    result = render_result_section(_make_receipt())
    assert "[bold]RESULT[/bold]" in result


def test_render_result_section_result_line_present():
    result = render_result_section(_make_receipt())
    assert "9 issue areas found" in result


def test_render_result_section_risk_present():
    result = render_result_section(_make_receipt(risk="High"))
    assert "Risk  High" in result


def test_render_result_section_approval_as_own_row():
    result = render_result_section(_make_receipt(risk="High", approval="Not required"))
    assert "Approval  Not required" in result
    assert "↳ [dim]Not required[/dim]" not in result


def test_render_result_section_checks_present():
    result = render_result_section(_make_receipt(checks_display="3 skipped"))
    assert "Checks" in result
    assert "3 skipped" in result


def test_render_result_section_shard_id_present():
    result = render_result_section(_make_receipt(shard_id="shard-20260524-0001"))
    assert "Receipt" in result
    assert "shard-20260524-0001" in result


def test_render_result_section_cost_present():
    result = render_result_section(_make_receipt(cost_display="$0.0480"))
    assert "Cost" in result
    assert "$0.0480" in result


def test_render_result_section_skips_not_recorded_result():
    result = render_result_section(_make_receipt(result="Not recorded"))
    assert "9 issue" not in result
    assert "Not recorded" not in result or "Risk" in result


def test_render_result_section_skips_not_recorded_risk():
    result = render_result_section(_make_receipt(risk="Not recorded"))
    assert "Risk" not in result


def test_render_result_section_skips_not_recorded_cost():
    result = render_result_section(_make_receipt(cost_display="Not recorded"))
    assert "Cost" not in result


def test_render_result_section_skips_not_run_checks():
    result = render_result_section(_make_receipt(checks_display="Not run"))
    assert "Checks" not in result


def test_render_result_section_skips_not_recorded_checks():
    result = render_result_section(_make_receipt(checks_display="Not recorded"))
    assert "Checks" not in result


def test_render_result_section_all_sentinel_values_returns_empty():
    receipt = _make_receipt(
        result="Not recorded",
        risk="Not recorded",
        checks_display="Not recorded",
        shard_id="",
        cost_display="Not recorded",
        approval="Not recorded",
    )
    assert render_result_section(receipt) == ""


def test_render_result_section_ends_with_newline():
    result = render_result_section(_make_receipt())
    assert result.endswith("\n")


# ---------------------------------------------------------------------------
# render_result_section: the Checks block reads the shared verification truth
# ---------------------------------------------------------------------------


def test_result_section_checks_prefer_a_later_openshard_rerun(tmp_path):
    from openshard.history.query import recent_shards
    from openshard.tui.action_blocks import render_result_section
    from openshard.verification import post_session as ps
    from tests.test_cli_visibility import HOOKS_FULL, _repo

    repo = _repo(tmp_path, [HOOKS_FULL])
    before = render_result_section(recent_shards(repo_path=repo).items[0].receipt)
    assert "Checks" not in before  # the session's own display is "Not run"
    planned = ps.plan_checks(repo, {"verification_commands": [["python", "-m", "pytest"]]}, HOOKS_FULL)
    tree = ps.TreeState(head="59f31c55f399" + "0" * 28, dirty=False, tracked_dirty=False)
    ps.record_attestation(repo, ps.build_attestation(
        HOOKS_FULL, [ps.CheckRun(planned[0], "passed", exit_code=0)], before=tree, after=tree,
        started_at="2026-10-08T07:15:00Z", completed_at="2026-10-08T07:15:01Z",
    ))
    after = render_result_section(recent_shards(repo_path=repo).items[0].receipt)
    assert "Checks" in after and "1/1 passed (OpenShard re-run @ 59f31c55f399)" in after
