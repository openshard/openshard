from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openshard.cli.run_output import (
    _extract_findings_from_model_answer,
    _extract_findings_from_review_files,
    _extract_structured_findings,
    _native_meta_from_entry,
    _render_native_receipt,
    render_review_fallback_memo,
    render_review_tldr_memo,
)
from openshard.history.shard_contract import (
    ShardFinding,
    ShardReceipt,
    _display_model_name,
    build_live_run_receipt,
    render_compact_shard_receipt,
)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _report(**kwargs):
    defaults = dict(
        used_native_context=True,
        observed_tools=[],
        selected_skills=[],
        plan_intent=None,
        plan_risk=None,
        evidence_items=0,
        snippet_files=0,
        verification_attempted=False,
        verification_passed=False,
        verification_retried=False,
        diff_files=[],
        added_lines=0,
        removed_lines=0,
        warnings=[],
    )
    defaults.update(kwargs)
    return _ns(**defaults)


def _meta(**kwargs):
    defaults = dict(
        final_report=None,
        diff_review=None,
        approval_receipt=None,
    )
    defaults.update(kwargs)
    return _ns(**defaults)


class TestRenderNativeReceipt(unittest.TestCase):

    def test_receipt_saved_always_present(self):
        out = _render_native_receipt(_meta())
        self.assertIn("Receipt saved", out)

    def test_ends_with_period(self):
        out = _render_native_receipt(_meta())
        self.assertTrue(out.endswith("."))

    def test_no_files_changed_omitted(self):
        meta = _meta(final_report=_report(diff_files=[]))
        out = _render_native_receipt(meta)
        self.assertNotIn("file", out)

    def test_one_file_changed(self):
        meta = _meta(final_report=_report(diff_files=["a.py"]))
        out = _render_native_receipt(meta)
        self.assertIn("1 file changed", out)

    def test_plural_files_changed(self):
        meta = _meta(final_report=_report(diff_files=["a.py", "b.py", "c.py"]))
        out = _render_native_receipt(meta)
        self.assertIn("3 files changed", out)

    def test_files_from_diff_review_fallback(self):
        diff_review = _ns(changed_files=["x.py", "y.py"], has_diff=True)
        meta = _meta(final_report=None, diff_review=diff_review)
        out = _render_native_receipt(meta)
        self.assertIn("2 files changed", out)

    def test_verification_passed(self):
        meta = _meta(
            final_report=_report(
                diff_files=["a.py"],
                verification_attempted=True,
                verification_passed=True,
            )
        )
        out = _render_native_receipt(meta)
        self.assertIn("Verification passed", out)
        self.assertNotIn("Verification failed", out)

    def test_verification_failed(self):
        meta = _meta(
            final_report=_report(
                diff_files=["a.py"],
                verification_attempted=True,
                verification_passed=False,
            )
        )
        out = _render_native_receipt(meta)
        self.assertIn("Verification failed", out)
        self.assertNotIn("Verification passed", out)

    def test_verification_not_attempted_omitted(self):
        meta = _meta(final_report=_report(verification_attempted=False))
        out = _render_native_receipt(meta)
        self.assertNotIn("Verification", out)

    def test_no_risky_writes_default(self):
        out = _render_native_receipt(_meta(approval_receipt=None))
        self.assertIn("No risky writes", out)
        self.assertNotIn("Write approved", out)

    def test_write_approved_when_granted(self):
        receipt = _ns(source="change_budget_soft_gate", granted=True)
        meta = _meta(approval_receipt=receipt)
        out = _render_native_receipt(meta)
        self.assertIn("Write approved", out)
        self.assertNotIn("No risky writes", out)

    def test_approval_not_granted_shows_writes_blocked(self):
        receipt = _ns(source="change_budget_soft_gate", granted=False)
        meta = _meta(approval_receipt=receipt)
        out = _render_native_receipt(meta)
        self.assertIn("Writes blocked", out)
        self.assertNotIn("No risky writes", out)

    def test_no_receipt_shows_no_risky_writes(self):
        out = _render_native_receipt(_meta(approval_receipt=None))
        self.assertIn("No risky writes", out)
        self.assertNotIn("Writes blocked", out)

    def test_full_receipt_format(self):
        meta = _meta(
            final_report=_report(
                diff_files=["a.py", "b.py"],
                verification_attempted=True,
                verification_passed=True,
            )
        )
        out = _render_native_receipt(meta)
        self.assertEqual(
            out,
            "2 files changed. Verification passed. No risky writes. Receipt saved.",
        )

    def test_receipt_from_entry_via_native_meta_from_entry(self):
        entry = {
            "workflow": "native",
            "executor": "native",
            "diff_review": {
                "has_diff": True,
                "changed_files": ["x.py"],
                "added_lines": 5,
                "removed_lines": 2,
                "output_chars": 100,
                "truncated": False,
            },
            "final_report": {
                "used_native_context": True,
                "observed_tools": [],
                "selected_skills": [],
                "plan_intent": None,
                "plan_risk": None,
                "evidence_items": 0,
                "snippet_files": 0,
                "verification_attempted": True,
                "verification_passed": True,
                "verification_retried": False,
                "diff_files": ["x.py"],
                "added_lines": 5,
                "removed_lines": 2,
                "warnings": [],
            },
        }
        nm = _native_meta_from_entry(entry)
        out = _render_native_receipt(nm)
        self.assertIn("1 file changed", out)
        self.assertIn("Verification passed", out)
        self.assertIn("Receipt saved", out)

    def test_non_native_entry_returns_none_meta(self):
        entry = {"workflow": "standard", "task": "do a thing"}
        nm = _native_meta_from_entry(entry)
        self.assertIsNone(nm)


def _nine_findings() -> list[ShardFinding]:
    return [
        ShardFinding(severity="Critical", message="Database allows public network access"),
        ShardFinding(severity="Critical", message="Customer document bucket may be publicly readable"),
        ShardFinding(severity="Critical", message="Live API key appears committed in terraform.tfvars"),
        ShardFinding(severity="High", message="IAM permissions are too broad"),
        ShardFinding(severity="High", message="Secrets are passed as plain environment variables"),
        ShardFinding(severity="High", message="Backups are disabled"),
        ShardFinding(severity="High", message="SSL is not enforced on database connections"),
        ShardFinding(severity="Medium", message="Deletion protection is disabled"),
        ShardFinding(severity="Medium", message="CI security checks are missing"),
    ]


class TestExtractStructuredFindings(unittest.TestCase):

    def test_happy_path_9_findings(self):
        summary = (
            "Reviewed the Terraform codebase thoroughly.\n"
            'STRUCTURED_FINDINGS: [{"severity": "Critical", "message": "DB public"}, '
            '{"severity": "High", "message": "IAM too broad"}, '
            '{"severity": "High", "message": "Secrets in env"}, '
            '{"severity": "High", "message": "Backups off"}, '
            '{"severity": "High", "message": "No SSL"}, '
            '{"severity": "Medium", "message": "Deletion protection off"}, '
            '{"severity": "Medium", "message": "No CI checks"}, '
            '{"severity": "Critical", "message": "Bucket public"}, '
            '{"severity": "Critical", "message": "API key committed"}]'
        )
        clean, findings = _extract_structured_findings(summary)
        self.assertEqual(len(findings), 9)
        self.assertNotIn("STRUCTURED_FINDINGS", clean)
        self.assertIn("Reviewed the Terraform", clean)

    def test_no_tag_returns_original(self):
        summary = "All good. No issues found."
        clean, findings = _extract_structured_findings(summary)
        self.assertEqual(clean, summary)
        self.assertEqual(findings, [])

    def test_malformed_json_returns_empty(self):
        summary = "Some analysis.\nSTRUCTURED_FINDINGS: not-valid-json"
        clean, findings = _extract_structured_findings(summary)
        self.assertEqual(findings, [])
        self.assertNotIn("STRUCTURED_FINDINGS", clean)

    def test_unknown_severity_coerced_to_note(self):
        summary = 'STRUCTURED_FINDINGS: [{"severity": "Unknown", "message": "test"}]'
        _, findings = _extract_structured_findings(summary)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "Note")

    def test_strips_findings_line_from_clean_summary(self):
        summary = "First line.\nSTRUCTURED_FINDINGS: []\nLast line."
        clean, _ = _extract_structured_findings(summary)
        self.assertIn("First line.", clean)
        self.assertIn("Last line.", clean)
        self.assertNotIn("STRUCTURED_FINDINGS", clean)


class TestRenderReviewTldrMemo(unittest.TestCase):

    def test_9_findings_says_9_issues(self):
        memo = render_review_tldr_memo(_nine_findings(), [])
        # 4 High findings exceed the cap of 3, so 1 is hidden → two-count format.
        self.assertIn("9 raw findings recorded", memo)

    def test_groups_by_severity(self):
        memo = render_review_tldr_memo(_nine_findings(), [])
        self.assertIn("Critical", memo)
        self.assertIn("High", memo)
        self.assertIn("Medium", memo)

    def test_critical_before_high(self):
        memo = render_review_tldr_memo(_nine_findings(), [])
        self.assertLess(memo.index("Critical"), memo.index("High"))

    def test_finding_messages_present(self):
        memo = render_review_tldr_memo(_nine_findings(), [])
        self.assertIn("Database allows public network access", memo)
        self.assertIn("IAM permissions are too broad", memo)
        self.assertIn("CI security checks are missing", memo)

    def test_files_created_section(self):
        files = [_ns(path="review/ASSESSMENT.md"), _ns(path="review/checklist.md")]
        memo = render_review_tldr_memo(_nine_findings(), files)
        self.assertIn("Files created", memo)
        self.assertIn("review/ASSESSMENT.md", memo)
        self.assertIn("review/checklist.md", memo)

    def test_no_files_omits_section(self):
        memo = render_review_tldr_memo(_nine_findings(), [])
        self.assertNotIn("Files created", memo)

    def test_empty_findings_returns_empty_string(self):
        self.assertEqual(render_review_tldr_memo([], []), "")

    def test_returns_string_not_none(self):
        result = render_review_tldr_memo(_nine_findings(), [])
        self.assertIsInstance(result, str)

    def test_singular_issue_wording(self):
        findings = [ShardFinding(severity="Critical", message="One problem")]
        memo = render_review_tldr_memo(findings, [])
        self.assertIn("1 issue", memo)
        self.assertNotIn("1 issues", memo)


class TestReceiptLabelExecutor(unittest.TestCase):

    def _make_receipt(self, agent: str = "OpenShard") -> ShardReceipt:
        return build_live_run_receipt(
            task="Review Terraform security",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent=agent,
            stage_runs=[],
            routing_model=None,
            risk="High",
            sandbox="Not required",
            files_changed=3,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=0.0123,
            result_summary="9 issues found; review files created.",
        )

    def test_compact_receipt_uses_executor_not_agent(self):
        out = render_compact_shard_receipt(self._make_receipt())
        self.assertIn("Executor", out)
        self.assertNotIn("  Agent ", out)

    def test_native_run_shows_openshard_native(self):
        receipt = self._make_receipt(agent="OpenShard Native")
        out = render_compact_shard_receipt(receipt)
        self.assertIn("OpenShard Native", out)

    def test_receipt_agent_field_openshard_native_for_native(self):
        receipt = build_live_run_receipt(
            task="Review task",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard Native",
            stage_runs=[],
            routing_model=None,
            risk="Low",
            sandbox="Not required",
            files_changed=0,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="Done.",
        )
        self.assertEqual(receipt.agent, "OpenShard Native")


class TestModelDisplayFullName(unittest.TestCase):

    def test_short_slug_without_prefix(self):
        self.assertEqual(_display_model_name("claude-sonnet-4-6"), "Claude Sonnet 4.6")

    def test_short_slug_dot_form(self):
        self.assertEqual(_display_model_name("claude-sonnet-4.6"), "Claude Sonnet 4.6")

    def test_full_slug_with_provider(self):
        self.assertEqual(_display_model_name("anthropic/claude-sonnet-4-6"), "Claude Sonnet 4.6")

    def test_opus_short_slug(self):
        self.assertEqual(_display_model_name("claude-opus-4-7"), "Claude Opus 4.7")

    def test_haiku_short_slug(self):
        self.assertEqual(_display_model_name("claude-haiku-4-5"), "Claude Haiku 4.5")


class TestCompactReceiptFindings(unittest.TestCase):

    def test_9_findings_in_compact_receipt(self):
        findings = _nine_findings()
        receipt = build_live_run_receipt(
            task="Terraform security review",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard Native",
            stage_runs=[],
            routing_model="anthropic/claude-sonnet-4-6",
            risk="High",
            sandbox="Not required",
            files_changed=3,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=0.0234,
            result_summary="9 issues found; review files created.",
            findings=findings,
        )
        out = render_compact_shard_receipt(receipt)
        self.assertIn("FINDINGS", out)
        # Compact receipt caps at 5 visible findings; top-priority ones must appear.
        top5 = [f for f in findings if f.severity == "Critical"][:3]
        for f in top5:
            self.assertIn(f.message[:40], out)
        # Overflow shown with "+N more" line
        self.assertIn("more findings recorded", out)

    def test_result_shows_issues_found(self):
        receipt = build_live_run_receipt(
            task="Terraform security review",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard Native",
            stage_runs=[],
            routing_model=None,
            risk="High",
            sandbox="Not required",
            files_changed=3,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="9 issues found; review files created.",
        )
        out = render_compact_shard_receipt(receipt)
        self.assertIn("9 issues found", out)


class TestNoInternalWording(unittest.TestCase):

    def test_render_review_memo_no_internal_wording(self):
        memo = render_review_tldr_memo(_nine_findings(), [])
        self.assertNotIn("native-loop-candidate", memo)
        self.assertNotIn("form factor", memo)
        self.assertNotIn("deep-run", memo)

    def test_compact_receipt_no_internal_wording(self):
        receipt = build_live_run_receipt(
            task="Terraform security review",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard Native",
            stage_runs=[],
            routing_model=None,
            risk="High",
            sandbox="Not required",
            files_changed=3,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="9 issues found; review files created.",
            findings=_nine_findings(),
        )
        out = render_compact_shard_receipt(receipt)
        self.assertNotIn("native-loop-candidate", out)
        self.assertNotIn("form factor", out)


class TestPackPromptSeparation(unittest.TestCase):
    """Pack display prompt must not contain the execution suffix."""

    def _get_review_pack(self, pack_id: str):
        from openshard.workflow_packs.packs import get_pack
        return get_pack(pack_id)

    def test_display_prompt_clean_no_structured_findings(self):
        p = self._get_review_pack("production-iac-hardening")
        self.assertNotIn("STRUCTURED_FINDINGS", p.prompt)

    def test_execution_suffix_contains_structured_findings(self):
        p = self._get_review_pack("production-iac-hardening")
        self.assertIn("STRUCTURED_FINDINGS", p.execution_prompt_suffix)

    def test_effective_execution_prompt_contains_instruction(self):
        p = self._get_review_pack("production-iac-hardening")
        effective = p.prompt + p.execution_prompt_suffix
        self.assertIn("STRUCTURED_FINDINGS:", effective)

    def test_all_review_packs_have_clean_display_prompt(self):
        from openshard.workflow_packs.packs import load_packs
        review_pack_ids = {
            "production-iac-hardening",
            "terraform-networking-review",
            "iam-security-review",
            "cicd-safety-review",
            "powershell-automation-review",
        }
        for p in load_packs():
            if p.id in review_pack_ids:
                self.assertNotIn(
                    "STRUCTURED_FINDINGS",
                    p.prompt,
                    f"Pack {p.id!r} prompt leaks STRUCTURED_FINDINGS into display text",
                )

    def test_repo_explanation_has_no_suffix(self):
        p = self._get_review_pack("repo-explanation")
        self.assertEqual(p.execution_prompt_suffix, "")


class TestReviewFallbackMemo(unittest.TestCase):
    """render_review_fallback_memo — shown when no structured findings captured."""

    def test_basic_content(self):
        memo = render_review_fallback_memo([])
        self.assertIn("completed the review", memo)
        self.assertIn("findings", memo.lower())

    def test_no_structured_findings_wording_in_default(self):
        memo = render_review_fallback_memo([])
        self.assertNotIn("Structured findings were not captured", memo)

    def test_diagnostic_shown_with_include_diagnostic(self):
        # diagnostic only shows when files are present; empty-files path always explains
        files = [_ns(path="review/ASSESSMENT.md")]
        memo = render_review_fallback_memo(files, include_diagnostic=True)
        self.assertIn("Structured findings were not captured", memo)

    def test_files_section_when_files_present(self):
        files = [_ns(path="review/ASSESSMENT.md"), _ns(path="review/checklist.md")]
        memo = render_review_fallback_memo(files)
        self.assertIn("Files created", memo)
        self.assertIn("review/ASSESSMENT.md", memo)
        self.assertIn("review/checklist.md", memo)

    def test_no_files_section_when_empty(self):
        memo = render_review_fallback_memo([])
        self.assertNotIn("Files created", memo)

    def test_returns_string(self):
        self.assertIsInstance(render_review_fallback_memo([]), str)

    def test_no_structured_findings_text_in_output(self):
        memo = render_review_fallback_memo([])
        self.assertNotIn("STRUCTURED_FINDINGS", memo)

    def test_no_files_does_not_mention_generated_review_files(self):
        memo = render_review_fallback_memo([])
        self.assertNotIn("generated review files", memo)

    def test_no_files_mentions_no_structured_findings_captured(self):
        memo = render_review_fallback_memo([])
        self.assertIn("no structured findings were captured", memo)

    def test_files_present_mentions_generated_review_files(self):
        files = [_ns(path="review/ASSESSMENT.md")]
        memo = render_review_fallback_memo(files)
        self.assertIn("generated review files", memo)


class TestModelDisplayFallback(unittest.TestCase):
    """build_live_run_receipt falls back to stage_runs model when routing_model is None."""

    def _make_stage_run(self, model: str, stage_type: str = "implementation"):
        from types import SimpleNamespace
        return SimpleNamespace(
            stage=SimpleNamespace(stage_type=stage_type),
            model=model,
        )

    def test_fallback_to_stage_run_model(self):
        receipt = build_live_run_receipt(
            task="Review task",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard Native",
            stage_runs=[self._make_stage_run("anthropic/claude-sonnet-4-6")],
            routing_model=None,
            risk="High",
            sandbox="Not required",
            files_changed=0,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="Done.",
        )
        self.assertEqual(receipt.model_display, "Claude Sonnet 4.6")

    def test_routing_model_takes_priority(self):
        receipt = build_live_run_receipt(
            task="Review task",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard Native",
            stage_runs=[self._make_stage_run("anthropic/claude-haiku-4-5")],
            routing_model="anthropic/claude-sonnet-4-6",
            risk="Low",
            sandbox="Not required",
            files_changed=0,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="Done.",
        )
        self.assertEqual(receipt.model_display, "Claude Sonnet 4.6")

    def test_not_recorded_when_no_data(self):
        receipt = build_live_run_receipt(
            task="Review task",
            run_id="2026-05-22T00:00:00Z",
            run_index=1,
            agent="OpenShard",
            stage_runs=[],
            routing_model=None,
            risk="Low",
            sandbox="Not required",
            files_changed=0,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="Done.",
        )
        self.assertEqual(receipt.model_display, "Not recorded")


def _render_last(entry: dict, detail: str = "default") -> str:
    import click
    from click.testing import CliRunner

    from openshard.cli.main import _render_log_entry

    @click.command()
    def cmd():
        _render_log_entry(entry, detail)

    return CliRunner().invoke(cmd).output


class TestLastReviewMemo(unittest.TestCase):
    """_render_log_entry shows review memo + 'Review complete' when findings stored."""

    def _entry(self, findings=None, files_detail=None):
        return {
            "task": "Review Terraform security",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Some raw summary text.",
            "findings": findings or [],
            "files_detail": files_detail or [],
            "stage_runs": [],
            "routing_model": "anthropic/claude-sonnet-4-6",
            "routing_rationale": "",
        }

    def test_review_complete_shown_when_findings_present(self):
        entry = self._entry(findings=[{"severity": "Critical", "message": "Public bucket"}])
        out = _render_last(entry)
        self.assertIn("Review complete", out)

    def test_done_not_shown_when_findings_present(self):
        entry = self._entry(findings=[{"severity": "High", "message": "Overly broad IAM role"}])
        out = _render_last(entry)
        self.assertNotIn("\nDone", out)

    def test_memo_groups_by_severity(self):
        entry = self._entry(findings=[
            {"severity": "Critical", "message": "Public bucket"},
            {"severity": "High", "message": "Broad IAM role"},
        ])
        out = _render_last(entry)
        self.assertIn("Critical", out)
        self.assertIn("High", out)
        self.assertIn("Public bucket", out)

    def test_files_created_section_in_memo(self):
        entry = self._entry(
            findings=[{"severity": "Medium", "message": "Missing encryption"}],
            files_detail=[{"path": "review/ASSESSMENT.md"}, {"path": "review/checklist.md"}],
        )
        out = _render_last(entry)
        self.assertIn("Files created", out)
        self.assertIn("review/ASSESSMENT.md", out)

    def test_no_findings_shows_done(self):
        entry = self._entry(findings=[])
        out = _render_last(entry)
        self.assertIn("Done", out)
        self.assertNotIn("Review complete", out)

    def test_structured_findings_json_not_in_output(self):
        entry = self._entry(findings=[{"severity": "Critical", "message": "Bad thing"}])
        out = _render_last(entry)
        self.assertNotIn("STRUCTURED_FINDINGS", out)


class TestTaskSuffixStripping(unittest.TestCase):
    """STRUCTURED_FINDINGS instruction must not appear in task display or stored task."""

    def test_task_display_strips_execution_suffix(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        base_task = "Review this Terraform configuration."
        full_task = base_task + _EXECUTION_FINDINGS_SUFFIX
        suffix_marker = "\n\nAfter completing your analysis"
        task_display = (
            full_task[:full_task.index(suffix_marker)].rstrip()
            if suffix_marker in full_task
            else full_task
        )
        self.assertEqual(task_display, base_task)
        self.assertNotIn("STRUCTURED_FINDINGS", task_display)

    def test_task_without_suffix_unchanged(self):
        task = "Fix the login bug."
        suffix_marker = " At the end of your analysis, output exactly one line: STRUCTURED_FINDINGS:"
        task_display = (
            task[:task.index(suffix_marker)].rstrip()
            if suffix_marker in task
            else task
        )
        self.assertEqual(task_display, task)


class TestExtractFindingsFromReviewFiles(unittest.TestCase):
    """_extract_findings_from_review_files — parse severity sections from Markdown files."""

    def _cf(self, path: str, content: str):
        return _ns(path=path, content=content, change_type="create", summary="")

    def _basic_md(self):
        return (
            "# Security Review\n\n"
            "## Critical\n\n"
            "- Database allows public network access\n"
            "- Customer bucket may be publicly readable\n\n"
            "## High\n\n"
            "- IAM permissions are too broad\n"
            "- Security group opens port 22 to 0.0.0.0/0\n\n"
            "## Medium\n\n"
            "- Missing lifecycle policies on storage buckets\n\n"
            "## Low\n\n"
            "- No resource tags for cost tracking\n"
        )

    def test_parses_all_severities(self):
        f = self._cf("review/ASSESSMENT.md", self._basic_md())
        findings = _extract_findings_from_review_files([f])
        sevs = [fi.severity for fi in findings]
        self.assertIn("Critical", sevs)
        self.assertIn("High", sevs)
        self.assertIn("Medium", sevs)
        self.assertIn("Low", sevs)

    def test_correct_finding_count(self):
        f = self._cf("review/ASSESSMENT.md", self._basic_md())
        findings = _extract_findings_from_review_files([f])
        self.assertEqual(len(findings), 6)

    def test_finding_messages_extracted(self):
        f = self._cf("review/ASSESSMENT.md", self._basic_md())
        findings = _extract_findings_from_review_files([f])
        messages = [fi.message for fi in findings]
        self.assertIn("Database allows public network access", messages)
        self.assertIn("IAM permissions are too broad", messages)

    def test_heading_variants_matched(self):
        md = (
            "## Critical Issues\n\n"
            "- Critical finding here\n\n"
            "## High Risk\n\n"
            "- High risk item\n"
        )
        findings = _extract_findings_from_review_files([self._cf("r.md", md)])
        sevs = {fi.severity for fi in findings}
        self.assertIn("Critical", sevs)
        self.assertIn("High", sevs)

    def test_non_markdown_files_ignored(self):
        f = self._cf("review/report.txt", "## Critical\n- Something bad\n")
        findings = _extract_findings_from_review_files([f])
        self.assertEqual(findings, [])

    def test_empty_content_returns_empty(self):
        f = self._cf("review/ASSESSMENT.md", "")
        findings = _extract_findings_from_review_files([f])
        self.assertEqual(findings, [])

    def test_no_severity_sections_returns_empty(self):
        md = "# Introduction\n\nThis is a general review document.\n"
        findings = _extract_findings_from_review_files([self._cf("review/r.md", md)])
        self.assertEqual(findings, [])

    def test_numbered_list_items_extracted(self):
        md = "## High\n\n1. Wildcard IAM permission grants full access\n2. EC2 uses default security group\n"
        findings = _extract_findings_from_review_files([self._cf("r.md", md)])
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(fi.severity == "High" for fi in findings))

    def test_multiple_files_combined(self):
        f1 = self._cf("review/ASSESSMENT.md", "## Critical\n- DB public IP exposed\n")
        f2 = self._cf("review/checklist.md", "## High\n- Wildcard role\n")
        findings = _extract_findings_from_review_files([f1, f2])
        self.assertEqual(len(findings), 2)
        sevs = {fi.severity for fi in findings}
        self.assertIn("Critical", sevs)
        self.assertIn("High", sevs)

    def test_returns_list_of_shard_findings(self):
        f = self._cf("r.md", "## High\n- Something wrong\n")
        findings = _extract_findings_from_review_files([f])
        self.assertIsInstance(findings[0], ShardFinding)

    def test_review_memo_uses_file_findings(self):
        f = self._cf("review/ASSESSMENT.md", "## Critical\n- Public bucket\n- Open DB port\n")
        findings = _extract_findings_from_review_files([f])
        memo = render_review_tldr_memo(findings, [f])
        self.assertIn("Found 2 issues", memo)
        self.assertIn("Critical", memo)
        self.assertIn("Public bucket", memo)


class TestExtractFindingsFromModelAnswer(unittest.TestCase):
    """_extract_findings_from_model_answer — parse severity sections from model plain-text output."""

    def test_extracts_standard_severity_bullets(self):
        text = (
            "Critical\n"
            "- Database allows public network access\n"
            "- Public bucket exposure\n\n"
            "High\n"
            "- IAM role is too broad\n\n"
            "Medium\n"
            "- Deletion protection disabled\n"
        )
        findings = _extract_findings_from_model_answer(text)
        sevs = [f.severity for f in findings]
        self.assertIn("Critical", sevs)
        self.assertIn("High", sevs)
        self.assertIn("Medium", sevs)
        self.assertEqual(len(findings), 4)

    def test_empty_when_no_severity_headings(self):
        text = (
            "I reviewed the infrastructure and found some potential improvements. "
            "There are several things to address. The configuration could be tightened."
        )
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(findings, [])

    def test_severity_heading_with_colon(self):
        text = "High:\n- Overly broad IAM permissions\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "High")

    def test_markdown_heading_h2(self):
        text = "## Critical\n- Public S3 bucket\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "Critical")

    def test_markdown_heading_h3_with_suffix(self):
        text = "### Critical Issues\n- RDS publicly accessible\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "Critical")

    def test_bold_heading(self):
        text = "**High**\n- Wildcard IAM policy attached\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "High")

    def test_suffix_word_high_risk(self):
        text = "High risk\n- Security group opens port 22 to 0.0.0.0/0\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "High")

    def test_suffix_word_medium_issues(self):
        text = "Medium issues\n- Missing lifecycle policy on storage bucket\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "Medium")

    def test_suffix_word_low_priority(self):
        text = "Low priority\n- No resource tags for cost tracking\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "Low")

    def test_prose_before_headings_ignored(self):
        text = (
            "Found 2 issues worth addressing.\n\n"
            "Critical\n"
            "- Public RDS instance\n\n"
            "High\n"
            "- Too-broad IAM role\n"
        )
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 2)
        msgs = [f.message for f in findings]
        self.assertIn("Public RDS instance", msgs)

    def test_numbered_list_items_extracted(self):
        text = "High\n1. Wildcard IAM permission grants full access\n2. EC2 uses default security group\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(f.severity == "High" for f in findings))

    def test_result_is_list_of_shard_findings(self):
        text = "High\n- Something wrong with the config\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertIsInstance(findings[0], ShardFinding)

    def test_non_severity_word_not_matched(self):
        text = "Warning\n- This should not appear\nNote:\n- Neither should this\n"
        findings = _extract_findings_from_model_answer(text)
        self.assertEqual(findings, [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(_extract_findings_from_model_answer(""), [])

    def test_none_like_empty_returns_empty(self):
        self.assertEqual(_extract_findings_from_model_answer("   "), [])

    def test_combined_with_structured_findings_tag_stripped_first(self):
        # Simulate the extraction chain: STRUCTURED_FINDINGS stripped, then plain-text parsed
        raw = (
            "Found 1 issue.\n\n"
            "Critical\n"
            "- Public bucket\n\n"
            "STRUCTURED_FINDINGS: [{\"severity\": \"Critical\", \"message\": \"Public bucket\"}]"
        )
        clean, json_findings = _extract_structured_findings(raw)
        # JSON path finds it; plain-text path is not needed
        self.assertEqual(len(json_findings), 1)
        # clean should not contain the tag
        self.assertNotIn("STRUCTURED_FINDINGS", clean)
        # If JSON path had failed, plain-text would still extract it
        plain_findings = _extract_findings_from_model_answer(clean)
        self.assertEqual(len(plain_findings), 1)
        self.assertEqual(plain_findings[0].severity, "Critical")


class TestExecutorMetadataConsistency(unittest.TestCase):
    """Executor label in receipt is consistent with actual execution metadata."""

    def _make_receipt(self, agent: str) -> ShardReceipt:
        return build_live_run_receipt(
            task="production-iac-hardening review",
            run_id="2026-05-22T10:00:00Z",
            run_index=1,
            agent=agent,
            stage_runs=[],
            routing_model="anthropic/claude-sonnet-4-6",
            risk="High",
            sandbox="Not required",
            files_changed=0,
            verification_attempted=False,
            verification_passed=None,
            approval="Not required",
            estimated_cost=None,
            result_summary="3 issues found.",
        )

    def test_native_agent_label_in_compact_receipt(self):
        receipt = self._make_receipt("OpenShard Native")
        out = render_compact_shard_receipt(receipt)
        self.assertIn("OpenShard Native", out)

    def test_non_native_agent_label_in_compact_receipt(self):
        receipt = self._make_receipt("OpenShard")
        out = render_compact_shard_receipt(receipt)
        self.assertIn("OpenShard", out)
        self.assertNotIn("OpenShard Native", out)

    def test_render_post_run_native_shows_openshard_native(self):
        import click
        from click.testing import CliRunner

        from openshard.cli.run_output import render_post_run

        @click.command()
        def cmd():
            render_post_run(
                stage_runs=[],
                routing_decision=None,
                verification_attempted=False,
                verification_passed=None,
                readonly_task=True,
                validator_policy=None,
                validator_result=None,
                final_files=[],
                detail="default",
                notes=[],
                repo_facts=None,
                task="production-iac-hardening review",
                run_id="2026-05-22T10:00:00Z",
                run_index=1,
                risk="High",
                sandbox="Not required",
                approval="Not required",
                is_native=True,
                exec_result_summary="2 issues found.",
                findings=[ShardFinding(severity="High", message="IAM role too broad")],
                is_review_task=True,
            )

        out = CliRunner().invoke(cmd).output
        self.assertIn("OpenShard Native", out)

    def test_render_post_run_non_native_shows_openshard(self):
        import click
        from click.testing import CliRunner

        from openshard.cli.run_output import render_post_run

        @click.command()
        def cmd():
            render_post_run(
                stage_runs=[],
                routing_decision=None,
                verification_attempted=False,
                verification_passed=None,
                readonly_task=True,
                validator_policy=None,
                validator_result=None,
                final_files=[],
                detail="default",
                notes=[],
                repo_facts=None,
                task="security review",
                run_id="2026-05-22T10:00:00Z",
                run_index=1,
                risk="High",
                sandbox="Not required",
                approval="Not required",
                is_native=False,
                exec_result_summary="1 issue found.",
                findings=[ShardFinding(severity="High", message="Wide permissions")],
                is_review_task=True,
            )

        out = CliRunner().invoke(cmd).output
        self.assertIn("OpenShard", out)
        self.assertNotIn("OpenShard Native", out)


class TestExecutionSuffixVisibleMemoInstruction(unittest.TestCase):
    """_EXECUTION_FINDINGS_SUFFIX must contain both the visible-memo template and the hidden machine line."""

    def test_suffix_contains_visible_memo_template(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        self.assertIn("Found X issues worth addressing", _EXECUTION_FINDINGS_SUFFIX)

    def test_suffix_contains_structured_findings_instruction(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        self.assertIn("STRUCTURED_FINDINGS:", _EXECUTION_FINDINGS_SUFFIX)

    def test_suffix_instructs_severity_grouping(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        self.assertIn("Critical", _EXECUTION_FINDINGS_SUFFIX)
        self.assertIn("High", _EXECUTION_FINDINGS_SUFFIX)

    def test_suffix_references_json_summary_field(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        self.assertIn("summary", _EXECUTION_FINDINGS_SUFFIX)

    def test_suffix_allows_multi_line_summary(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        self.assertIn("multi-line", _EXECUTION_FINDINGS_SUFFIX)


class TestReviewTaskRiskIsNotCoercedAtDisplay(unittest.TestCase):
    """v0.4.4: the receipt shows the *recorded* risk. The former display-time
    floor (review task + missing/Low -> High) silently turned one fact into
    another and is gone."""

    def _entry(self, is_review_task: bool, risk_level: str | None) -> dict:
        entry: dict = {
            "task": "production-iac-hardening",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Review completed.",
            "findings": [],
        }
        if is_review_task:
            entry["is_review_task"] = True
        if risk_level is not None:
            entry["form_factor"] = {"risk_level": risk_level}
        return entry

    def test_review_task_low_risk_stays_low(self):
        from openshard.history.shard_contract import build_shard_receipt
        receipt = build_shard_receipt(self._entry(is_review_task=True, risk_level="low"))
        self.assertEqual(receipt.risk, "Low")

    def test_review_task_no_form_factor_stays_not_recorded(self):
        from openshard.history.shard_contract import build_shard_receipt
        receipt = build_shard_receipt(self._entry(is_review_task=True, risk_level=None))
        self.assertEqual(receipt.risk, "Not recorded")

    def test_review_task_already_high_stays_high(self):
        from openshard.history.shard_contract import build_shard_receipt
        receipt = build_shard_receipt(self._entry(is_review_task=True, risk_level="high"))
        self.assertEqual(receipt.risk, "High")

    def test_non_review_task_low_stays_low(self):
        from openshard.history.shard_contract import build_shard_receipt
        receipt = build_shard_receipt(self._entry(is_review_task=False, risk_level="low"))
        self.assertEqual(receipt.risk, "Low")

    def test_review_task_recorded_low_risk_shown_in_compact_receipt(self):
        from openshard.history.shard_contract import (
            build_shard_receipt,
            render_compact_shard_receipt,
        )
        receipt = build_shard_receipt(self._entry(is_review_task=True, risk_level="low"))
        rendered = render_compact_shard_receipt(receipt)
        risk_line = rendered.split("Risk", 1)[-1].split("\n")[0]
        self.assertIn("Low", risk_line)
        self.assertNotIn("High", risk_line)


class TestReviewContextInstruction(unittest.TestCase):
    """Review pack tasks receive a review-specific context instruction, not the generic third-person one."""

    def test_review_task_instruction_not_third_person_explanation(self):
        # The generic readonly instruction says "third-person explanation of what the code does"
        # Review tasks must NOT receive this — it causes the model to write "Review completed."
        # We verify the execution suffix does not contain the generic readonly phrasing
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        self.assertNotIn("third-person explanation", _EXECUTION_FINDINGS_SUFFIX)

    def test_review_task_instruction_requests_complete_analysis(self):
        # The review suffix must instruct multi-line/complete analysis
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        lower = _EXECUTION_FINDINGS_SUFFIX.lower()
        self.assertTrue(
            "complete" in lower or "multi-line" in lower or "summary field" in lower,
            "suffix must instruct complete multi-line analysis in summary field",
        )


def _debug_kwargs(**overrides):
    defaults = dict(
        effective_executor="native",
        selected_workflow="auto",
        opencode_explicit=False,
        task="Review this config STRUCTURED_FINDINGS: [] After completing your analysis",
        exec_result_summary="Found 2 issues.\nCritical\n- Bad thing\n",
        exec_result_files_count=0,
        exec_result_notes=[],
        usage_info=None,
        extraction_source="STRUCTURED_FINDINGS",
        findings_count=2,
    )
    defaults.update(overrides)
    return defaults


class TestReviewDebugDiagnostics(unittest.TestCase):
    """_emit_review_debug writes to stderr + file only when OPENSHARD_DEBUG_REVIEW=1."""

    def _call(self, **kwargs):
        from openshard.run.pipeline import _emit_review_debug
        _emit_review_debug(**_debug_kwargs(**kwargs))

    def test_debug_flag_off_no_output(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENSHARD_DEBUG_REVIEW", None)
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call()
        self.assertEqual(buf.getvalue(), "")

    def test_debug_flag_zero_no_output(self):
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "0"}):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call()
        self.assertEqual(buf.getvalue(), "")

    def test_debug_flag_on_includes_executor(self):
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call(effective_executor="native")
        self.assertIn("native", buf.getvalue())
        self.assertIn("executor_path:", buf.getvalue())

    def test_debug_flag_on_includes_summary_preview(self):
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call(exec_result_summary="Critical section content here")
        output = buf.getvalue()
        self.assertIn("summary_preview:", output)
        self.assertIn("Critical section content here", output)

    def test_debug_flag_on_extraction_source_none_shows_fallback(self):
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call(extraction_source=None)
        self.assertIn("none (fallback)", buf.getvalue())

    def test_debug_writes_to_openshard_debug_dir(self):
        import openshard.run.pipeline as _pipe_mod
        tmp = Path(tempfile.mkdtemp())
        dot_openshard = tmp / ".openshard"
        dot_openshard.mkdir()
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            with patch.object(_pipe_mod.Path, "cwd", return_value=tmp):
                buf = io.StringIO()
                with patch("sys.stderr", buf):
                    self._call()
        log_file = tmp / ".openshard" / "debug" / "review-debug.log"
        self.assertTrue(log_file.exists(), f"expected {log_file} to exist")
        content = log_file.read_text(encoding="utf-8")
        self.assertIn("executor_path:", content)

    def test_debug_writes_to_tempdir_when_no_openshard_dir(self):
        import openshard.run.pipeline as _pipe_mod
        tmp = Path(tempfile.mkdtemp())
        fake_tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            with patch.object(_pipe_mod.Path, "cwd", return_value=tmp):
                with patch("tempfile.gettempdir", return_value=str(fake_tmp)):
                    buf = io.StringIO()
                    with patch("sys.stderr", buf):
                        self._call()
        log_file = fake_tmp / "openshard-review-debug.log"
        self.assertTrue(log_file.exists(), f"expected {log_file} to exist")
        content = log_file.read_text(encoding="utf-8")
        self.assertIn("extraction_source:", content)

    def test_debug_native_run_does_not_show_opencode_executor(self):
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call(effective_executor="native", selected_workflow="auto", opencode_explicit=False)
        output = buf.getvalue()
        self.assertNotIn("executor_path:     opencode", output)
        self.assertIn("executor_path:     native", output)

    def test_debug_direct_executor_shows_openshard_native_label(self):
        with patch.dict(os.environ, {"OPENSHARD_DEBUG_REVIEW": "1"}):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                self._call(effective_executor="direct", selected_workflow="auto", opencode_explicit=False)
        output = buf.getvalue()
        self.assertIn("public_label:      OpenShard Native", output)


class TestExecutorAutoSelection(unittest.TestCase):
    """_suggest_executor never returns opencode automatically."""

    def _suggest(self, task: str) -> tuple:
        from openshard.run.pipeline import _suggest_executor
        return _suggest_executor(task)

    def test_long_task_routes_to_native_not_opencode(self):
        long_task = "word " * 65  # 65 words, well above 60-word threshold
        executor, _, _ = self._suggest(long_task)
        self.assertEqual(executor, "native")
        self.assertNotEqual(executor, "opencode")

    def test_short_task_routes_to_direct(self):
        short_task = "Fix the typo in README"
        executor, _, _ = self._suggest(short_task)
        self.assertEqual(executor, "direct")

    def test_multifile_keyword_routes_to_native(self):
        executor, _, _ = self._suggest("Update all files to use the new API")
        self.assertEqual(executor, "native")
        self.assertNotEqual(executor, "opencode")

    def test_codebase_keyword_routes_to_native(self):
        executor, _, _ = self._suggest("Refactor across the codebase to remove deprecated calls")
        self.assertEqual(executor, "native")
        self.assertNotEqual(executor, "opencode")

    def test_suggest_executor_never_returns_opencode(self):
        from openshard.run.pipeline import _suggest_executor
        samples = [
            "word " * 70,
            "Update all files in the project",
            "Refactor throughout the entire codebase",
            "Review every file for security issues",
            "Fix multiple files across the codebase",
            "Short task",
            "Add a test",
        ]
        for s in samples:
            executor, _, _ = _suggest_executor(s)
            self.assertNotEqual(executor, "opencode", f"opencode returned for: {s!r}")

    def test_production_iac_hardening_full_prompt_routes_to_native(self):
        from openshard.workflow_packs.builtin import _EXECUTION_FINDINGS_SUFFIX
        base_prompt = (
            "Review and harden this deliberately flawed Terraform codebase. "
            "Assess it through security/compliance posture, 2am operability, and "
            "developer experience for a 5-10 person engineering team. Identify "
            "critical, high, and medium risks. Explain trade-offs. "
            "Do not apply changes directly without review."
        )
        full_prompt = base_prompt + _EXECUTION_FINDINGS_SUFFIX
        executor, reason, _ = self._suggest(full_prompt)
        self.assertEqual(executor, "native", f"expected native, got {executor!r} (reason={reason!r})")
        self.assertNotEqual(executor, "opencode")

    def test_explicit_opencode_mapping_unchanged(self):
        # Confirm pipeline maps effective_workflow="opencode" → effective_executor="opencode"
        # without going through _suggest_executor (which never returns opencode)
        # The mapping is a simple if/elif at lines ~353-354; verify the constant exists
        # We test the module compiles and the workflow choice "opencode" is accepted
        import inspect

        import openshard.run.pipeline as _pipe_mod
        src = inspect.getsource(_pipe_mod.RunPipeline.run)
        self.assertIn('"opencode"', src, "opencode workflow mapping must be preserved in pipeline.run()")


# ---------------------------------------------------------------------------
# Fix 2: Shard ID consistency — stored shard_id is single source of truth
# ---------------------------------------------------------------------------

class TestShardIdConsistency(unittest.TestCase):

    def _minimal_entry(self, **extra):
        base = {
            "timestamp": "2026-05-25T12:00:00Z",
            "task": "explain the pipeline",
            "execution_model": "mock-model",
            "retry_triggered": False,
            "duration_seconds": 1.0,
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
            "verification_attempted": False,
            "verification_passed": None,
            "summary": "Explanation of the pipeline.",
        }
        base.update(extra)
        return base

    def test_stored_shard_id_preferred_over_computed(self):
        """build_shard_receipt uses entry['shard_id'] rather than re-computing from index."""
        from openshard.history.shard_contract import build_shard_receipt
        entry = self._minimal_entry(shard_id="shard-20260525-0020")
        receipt = build_shard_receipt(entry)
        self.assertEqual(receipt.shard_id, "shard-20260525-0020")
        self.assertNotEqual(receipt.shard_id, "shard-20260525-0001")

    def test_fallback_to_computed_when_shard_id_not_stored(self):
        """When no stored shard_id, build_shard_receipt falls back to _make_shard_id(ts, index)."""
        from openshard.history.shard_contract import _make_shard_id, build_shard_receipt
        entry = self._minimal_entry()
        receipt = build_shard_receipt(entry, index=4)
        expected = _make_shard_id("2026-05-25T12:00:00Z", 4)
        self.assertEqual(receipt.shard_id, expected)
        self.assertEqual(receipt.shard_id, "shard-20260525-0005")

    def test_log_run_stores_shard_id_in_entry(self):
        """_log_run must write shard_id into the serialised JSONL entry."""
        import json
        import os
        import tempfile
        import time
        from unittest.mock import MagicMock, patch

        from openshard.run.pipeline import _log_run

        # Hermetic cwd: the history lock file is opened for real, so the
        # ".openshard" directory must exist even though Path.mkdir is patched
        # below. Without this the test only passed when an earlier test in the
        # same process had already created it in the repository directory.
        _td = tempfile.TemporaryDirectory()
        self.addCleanup(_td.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(_td.name)
        os.makedirs(".openshard", exist_ok=True)

        captured: list[str] = []

        class _FakeFile:
            def write(self, s: str) -> int:
                captured.append(s)
                return len(s)

            def flush(self) -> None:
                pass

            def fileno(self) -> int:
                return 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def _fake_path_open(self, mode="r", encoding=None, **kw):
            if mode == "a":
                return _FakeFile()
            import builtins
            return builtins.open(str(self), mode, encoding=encoding, **kw)

        gen_mock = MagicMock()
        gen_mock.model = "mock-model"
        gen_mock.fixer_model = "mock-fixer"

        with patch("pathlib.Path.open", _fake_path_open), \
             patch("pathlib.Path.mkdir"), \
             patch("openshard.history.jsonl_store.os.fsync", lambda fd: None):
            _log_run(
                start=time.time(),
                task="explain the pipeline",
                generator=gen_mock,
                retry_triggered=False,
                files=[],
                verification_attempted=False,
                verification_passed=None,
                workspace=None,
                run_index=2,
            )

        self.assertTrue(captured, "Nothing was written to the log file")
        written_line = captured[0]
        entry = json.loads(written_line.strip())
        self.assertIn("shard_id", entry, "shard_id must be stored in run entry")
        self.assertTrue(
            entry["shard_id"].startswith("shard-"),
            f"shard_id {entry['shard_id']!r} has wrong format",
        )
        # With run_index=2, the shard number should be 3 (index+1).
        self.assertTrue(
            entry["shard_id"].endswith("-0003"),
            f"shard_id {entry['shard_id']!r} should end with -0003 for run_index=2",
        )


# ---------------------------------------------------------------------------
# Fix 3: Evidence for read-only runs
# ---------------------------------------------------------------------------

class TestReadOnlyEvidence(unittest.TestCase):

    def _entry_with_files_detail(self, **extra):
        base = {
            "timestamp": "2026-05-25T12:00:00Z",
            "task": "Review this Terraform repo. Do not apply changes.",
            "execution_model": "mock-model",
            "retry_triggered": False,
            "duration_seconds": 1.0,
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
            "verification_attempted": False,
            "verification_passed": None,
            "summary": "Production readiness review complete.",
            "files_detail": [
                {"path": "TERRAFORM_PRODUCTION_READINESS_REVIEW.md", "change_type": "create", "summary": "review"},
            ],
        }
        base.update(extra)
        return base

    def test_zero_change_run_clears_files_touched(self):
        """files_touched must be empty for a run with files_created/updated/deleted all 0."""
        from openshard.history.shard_contract import build_shard_receipt
        entry = self._entry_with_files_detail()
        receipt = build_shard_receipt(entry)
        self.assertEqual(receipt.files_touched, [])
        touched_paths = [e.path for e in receipt.file_evidence if "changed" in e.roles]
        self.assertNotIn(
            "TERRAFORM_PRODUCTION_READINESS_REVIEW.md",
            touched_paths,
            "Generated report file must not appear as changed evidence for a zero-change run",
        )

    def test_nonzero_change_run_preserves_files_touched(self):
        """When files were actually changed, files_touched is preserved."""
        from openshard.history.shard_contract import build_shard_receipt
        entry = self._entry_with_files_detail(files_created=1)
        receipt = build_shard_receipt(entry)
        self.assertIn("TERRAFORM_PRODUCTION_READINESS_REVIEW.md", receipt.files_touched)

    def test_missing_counters_with_diff_review_preserves_files_touched(self):
        """Entries without explicit change counters must still use diff_review fallback."""
        from openshard.history.shard_contract import build_shard_receipt
        entry = {
            "timestamp": "2026-05-25T12:00:00Z",
            "task": "old entry without counters",
            "execution_model": "mock-model",
            "retry_triggered": False,
            "duration_seconds": 1.0,
            "verification_attempted": False,
            "verification_passed": None,
            "summary": "done",
            "diff_review": {"changed_files": ["src/main.py", "src/utils.py"]},
        }
        receipt = build_shard_receipt(entry)
        self.assertIn("src/main.py", receipt.files_touched)
        self.assertIn("src/utils.py", receipt.files_touched)

    def test_inspected_files_appear_in_evidence_when_no_changed_files(self):
        """file_context.paths must appear in evidence with 'inspected' role for read-only runs."""
        from openshard.history.shard_contract import build_shard_receipt
        entry = {
            "timestamp": "2026-05-25T12:00:00Z",
            "task": "Review this Terraform repo. Do not apply changes.",
            "execution_model": "mock-model",
            "retry_triggered": False,
            "duration_seconds": 1.0,
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
            "verification_attempted": False,
            "verification_passed": None,
            "summary": "Production readiness review complete.",
            "file_context": {"paths": ["main.tf", "outputs.tf"], "files_read": 2},
        }
        receipt = build_shard_receipt(entry)
        evidence_paths = [e.path for e in receipt.file_evidence]
        self.assertIn("main.tf", evidence_paths)
        self.assertIn("outputs.tf", evidence_paths)
        for e in receipt.file_evidence:
            if e.path in ("main.tf", "outputs.tf"):
                self.assertIn("inspected", e.roles)
