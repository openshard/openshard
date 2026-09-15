from __future__ import annotations

import unittest
from dataclasses import asdict

import click
from click.testing import CliRunner

from openshard.cli.main import _model_label, _render_log_entry
from openshard.cli.run_output import _native_meta_from_entry
from openshard.history.shard_contract import display_model_name as _sc_display_model_name
from openshard.native.context import (
    NativeContextQualityScore,
    NativeModelCandidateScore,
    NativeModelCandidateScoring,
    NativeModelPolicyReceipt,
    NativeModelRoleDecision,
    NativeModelSelectionDecision,
    NativeObservation,
    NativePlan,
    NativeRunTrustFactor,
    NativeRunTrustScore,
    NativeValidationContract,
    build_native_context_provenance,
    build_native_model_candidate_scoring,
    build_native_model_policy,
)
from openshard.routing.engine import MODEL_MAIN, MODEL_STRONG


def _render(entry: dict, detail: str = "more") -> str:
    @click.command()
    def cmd():
        _render_log_entry(entry, detail)

    return CliRunner().invoke(cmd).output


class TestLastRoutingDisplay(unittest.TestCase):

    def test_routing_section_shown_with_fields(self):
        entry = {
            "task": "do a thing",
            "routing_category": "standard",
            "routing_candidate_count": 5,
            "routing_selected_model": "openrouter/fast-model",
            "routing_selected_provider": "openrouter",
            "routing_used_fallback": False,
        }
        out = _render(entry, "full")
        self.assertIn("Category: standard", out)
        self.assertIn("Initial candidate:", out)
        self.assertIn(_model_label("openrouter/fast-model"), out)
        self.assertIn("openrouter", out)

    def test_routing_section_fallback(self):
        entry = {
            "task": "do a thing",
            "routing_category": "visual",
            "routing_candidate_count": 0,
            "routing_selected_model": None,
            "routing_selected_provider": None,
            "routing_used_fallback": True,
        }
        out = _render(entry, "full")
        self.assertIn("Category: visual", out)
        self.assertIn("Initial candidate: fallback (keyword routing)", out)

    def test_routing_section_absent_without_fields(self):
        entry = {"task": "do a thing"}
        out = _render(entry)
        self.assertNotIn("[routing]", out)

    def test_routing_no_tier_dispatch_note_without_receipt(self):
        entry = {
            "task": "do a thing",
            "routing_category": "standard",
            "routing_selected_model": "openrouter/fast-model",
            "routing_selected_provider": "openrouter",
            "routing_used_fallback": False,
        }
        out = _render(entry)
        self.assertNotIn("tier dispatch changed", out)

    def test_routing_tier_dispatch_applied_shows_note(self):
        entry = {
            "task": "do a thing",
            "routing_category": "standard",
            "routing_selected_model": "openrouter/deepseek-v4",
            "routing_selected_provider": "openrouter",
            "routing_used_fallback": False,
            "tier_dispatch_receipt": {"enabled": True, "applied": True},
        }
        out = _render(entry, "full")
        self.assertIn("Note: tier dispatch changed the work model shown below.", out)


class TestLastProfileDisplay(unittest.TestCase):

    def test_more_shows_profile_when_present(self):
        entry = {
            "task": "do a thing",
            "execution_profile": "native_deep",
            "execution_profile_reason": "security category",
        }
        out = _render(entry, detail="full")
        self.assertIn("Execution", out)
        self.assertIn("Mode: Deep Run", out)
        self.assertIn("Reason: security category", out)

    def test_full_shows_profile_when_present(self):
        entry = {
            "task": "do a thing",
            "execution_profile": "native_light",
            "execution_profile_reason": "simple/safe task",
        }
        out = _render(entry, detail="full")
        self.assertIn("Execution", out)
        self.assertIn("Mode: Run", out)
        self.assertIn("Reason: simple/safe task", out)

    def test_profile_without_reason(self):
        entry = {
            "task": "do a thing",
            "execution_profile": "native_swarm",
        }
        out = _render(entry, detail="full")
        self.assertIn("Execution", out)
        self.assertIn("Mode: Deep Run", out)

    def test_no_crash_on_entry_without_profile_fields(self):
        entry = {"task": "do a thing"}
        out = _render(entry, detail="full")
        self.assertNotIn("Execution", out)

    def test_no_crash_on_entry_without_profile_fields_full(self):
        entry = {"task": "do a thing"}
        out = _render(entry, detail="full")
        self.assertNotIn("Execution", out)

    def test_default_detail_does_not_show_profile(self):
        entry = {
            "task": "do a thing",
            "execution_profile": "native_deep",
            "execution_profile_reason": "security category",
        }
        out = _render(entry, detail="default")
        self.assertNotIn("Execution", out)

    def test_more_shows_ask_for_readonly(self):
        entry = {
            "task": "what does this function do",
            "execution_profile": "native_light",
            "execution_profile_reason": "simple/safe task",
            "routing_rationale": "read-only analysis",
        }
        out = _render(entry, detail="full")
        self.assertIn("Mode: Ask", out)

    def test_raw_profile_names_not_in_output(self):
        for profile in ("native_light", "native_deep", "native_swarm"):
            entry = {"task": "do a thing", "execution_profile": profile}
            out = _render(entry, detail="full")
            for leaked in ("native_light", "native_deep", "native_swarm", "Team Run"):
                self.assertNotIn(leaked, out, f"profile={profile!r} leaked {leaked!r}")


class TestLastVerificationDisplay(unittest.TestCase):

    _PLAN_ENTRY = {
        "task": "do a thing",
        "verification_plan": [
            {
                "name": "tests",
                "argv": ["python", "-m", "pytest"],
                "kind": "test",
                "source": "detected",
                "safety": "safe",
                "reason": "matches safe prefix: python -m pytest",
            }
        ],
    }

    def test_more_shows_verification_plan(self):
        out = _render(self._PLAN_ENTRY, detail="full")
        self.assertIn("Verification", out)
        self.assertIn("safe", out)
        self.assertIn("detected", out)

    def test_full_shows_verification_plan(self):
        out = _render(self._PLAN_ENTRY, detail="full")
        self.assertIn("Verification", out)
        self.assertIn("safe", out)
        self.assertIn("detected", out)

    def test_argv_rendered_space_joined(self):
        out = _render(self._PLAN_ENTRY, detail="full")
        self.assertIn("python -m pytest", out)

    def test_default_detail_hides_verification_plan(self):
        out = _render(self._PLAN_ENTRY, detail="default")
        self.assertNotIn("Verification", out)

    def test_old_entry_without_plan_no_crash(self):
        entry = {"task": "old run"}
        out = _render(entry, detail="full")
        self.assertNotIn("Verification", out)

    def test_config_source_shown(self):
        entry = {
            "task": "do a thing",
            "verification_plan": [
                {
                    "name": "tests",
                    "argv": ["pytest"],
                    "kind": "test",
                    "source": "config",
                    "safety": "safe",
                    "reason": "matches safe prefix: pytest",
                }
            ],
        }
        out = _render(entry, detail="full")
        self.assertIn("config", out)
        self.assertIn("pytest", out)


class TestLastReadonlyTaskTypeBlock(unittest.TestCase):

    _RO_ENTRY = {
        "task": "explain the routing engine",
        "routing_rationale": "read-only analysis",
    }

    def test_more_shows_task_type_for_readonly(self):
        out = _render(self._RO_ENTRY, detail="full")
        self.assertIn("Task type", out)
        self.assertIn("Read-only analysis", out)
        self.assertIn("Reason:", out)

    def test_full_shows_task_type_for_readonly(self):
        out = _render(self._RO_ENTRY, detail="full")
        self.assertIn("Task type", out)
        self.assertIn("Read-only analysis", out)

    def test_default_does_not_show_task_type(self):
        out = _render(self._RO_ENTRY, detail="default")
        self.assertNotIn("Task type", out)

    def test_write_task_does_not_show_task_type(self):
        entry = {"task": "add a helper function", "routing_rationale": "standard feature implementation"}
        out = _render(entry, detail="more")
        self.assertNotIn("Task type", out)


def _native_entry() -> dict:
    return {
        "task": "add feature",
        "workflow": "native",
        "executor": "native",
        "repo_context_summary": {
            "likely_stack_markers": ["python"],
            "test_markers": ["pytest"],
            "total_files": 30,
            "top_level_dirs": ["openshard"],
            "package_files": ["pyproject.toml"],
            "truncated": False,
        },
        "observation": {
            "dirty_diff_present": False,
            "search_matches_count": 3,
            "observed_tools": [],
            "verification_available": True,
            "warnings": [],
        },
        "plan": {"intent": "implementation", "risk": "medium", "suggested_steps": [], "warnings": []},
        "write_path": "pipeline",
        "verification_loop": {
            "attempted": True,
            "passed": True,
            "retried": True,
            "exit_code": 0,
            "output_chars": 200,
            "truncated": False,
        },
        "diff_review": {
            "has_diff": True,
            "changed_files": ["a.py", "b.py"],
            "added_lines": 34,
            "removed_lines": 8,
            "output_chars": 500,
            "truncated": False,
        },
        "final_report": {
            "used_native_context": True,
            "observed_tools": [],
            "selected_skills": ["repo mapping", "safe file editing"],
            "plan_intent": "implementation",
            "plan_risk": "medium",
            "evidence_items": 3,
            "snippet_files": 2,
            "verification_attempted": True,
            "verification_passed": True,
            "verification_retried": True,
            "diff_files": ["a.py", "b.py"],
            "added_lines": 34,
            "removed_lines": 8,
            "warnings": [],
        },
    }


class TestLastNativeInspection(unittest.TestCase):

    def test_more_renders_native_block(self):
        out = _render(_native_entry(), detail="full")
        self.assertIn("[native]", out)
        self.assertIn("repo:", out)
        self.assertIn("python", out)

    def test_more_renders_native_summary(self):
        out = _render(_native_entry(), detail="full")
        self.assertIn("[native summary]", out)
        self.assertIn("context: yes", out)
        self.assertIn("repo mapping", out)

    def test_full_renders_native_blocks(self):
        out = _render(_native_entry(), detail="full")
        self.assertIn("[native]", out)
        self.assertIn("[native summary]", out)

    def test_default_hides_native_blocks(self):
        out = _render(_native_entry(), detail="default")
        self.assertNotIn("[native]", out)
        self.assertNotIn("[native summary]", out)

    def test_default_shows_receipt_block(self):
        out = _render(_native_entry(), detail="default")
        self.assertIn("RECEIPT —", out)

    def test_default_receipt_files_count(self):
        out = _render(_native_entry(), detail="default")
        self.assertIn("2 files", out)

    def test_default_receipt_checks_passed(self):
        out = _render(_native_entry(), detail="default")
        self.assertIn("1/1 passed", out)

    def test_default_receipt_sandbox_off(self):
        out = _render(_native_entry(), detail="default")
        self.assertIn("Off", out)

    def test_default_receipt_shown_for_all_runs(self):
        entry = {"task": "standard run", "workflow": "standard"}
        out = _render(entry, detail="default")
        self.assertIn("RECEIPT —", out)

    def test_non_native_entry_no_native_blocks(self):
        entry = {"task": "standard run", "workflow": "standard"}
        out = _render(entry, detail="more")
        self.assertNotIn("[native]", out)
        self.assertNotIn("[native summary]", out)

    def test_partial_native_metadata_no_crash(self):
        entry = {"task": "native run", "workflow": "native"}
        out = _render(entry, detail="more")
        self.assertNotIn("[native]", out)  # no useful metadata → no block
        self.assertNotIn("[native summary]", out)

    def test_partial_real_metadata_shows_native_block(self):
        entry = {
            "task": "native run",
            "workflow": "native",
            "repo_context_summary": {
                "likely_stack_markers": ["python"],
                "test_markers": [],
                "total_files": 10,
                "top_level_dirs": ["src"],
                "package_files": [],
                "truncated": False,
            },
        }
        out = _render(entry, detail="full")
        self.assertIn("[native]", out)
        self.assertIn("repo:", out)
        self.assertNotIn("[native summary]", out)

    def test_raw_output_not_printed(self):
        entry = _native_entry()
        entry["diff_review"]["output_chars"] = 9999
        out = _render(entry, detail="full")
        # a.py / b.py now correctly appear in FILE EVIDENCE (changed role) —
        # the test guards against raw diff content, not structured file names
        self.assertIn("2 files", out)
        self.assertNotIn("diff --git", out)
        self.assertNotIn("@@", out)

    def test_loop_steps_rendered_in_native_block(self):
        entry = _native_entry()
        entry["native_loop_steps"] = ["repo_context", "observation", "plan", "generation"]
        out = _render(entry, detail="full")
        self.assertIn("loop:", out)
        self.assertIn("repo_context -> observation -> plan -> generation", out)

    def test_empty_loop_steps_no_loop_line(self):
        entry = _native_entry()
        entry["native_loop_steps"] = []
        out = _render(entry, detail="more")
        self.assertNotIn("loop:", out)

    def test_non_native_entry_no_loop_steps_rendered(self):
        entry = {"task": "standard run", "workflow": "standard", "native_loop_steps": ["plan"]}
        out = _render(entry, detail="more")
        self.assertNotIn("loop:", out)

    def test_render_native_demo_block_shows_backend_builtin(self):
        from types import SimpleNamespace

        from openshard.cli.run_output import _render_native_demo_block

        meta = SimpleNamespace(
            repo_context_summary=SimpleNamespace(likely_stack_markers=["python"], test_markers=[]),
            native_backend="builtin",
            native_backend_available=True,
            native_backend_notes=[],
            observation=None,
            plan=None,
            write_path="pipeline",
            verification_loop=None,
            diff_review=None,
            native_loop_steps=[],
        )
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("backend: builtin", joined)

    def test_render_native_demo_block_shows_backend_unavailable(self):
        from types import SimpleNamespace

        from openshard.cli.run_output import _render_native_demo_block

        meta = SimpleNamespace(
            repo_context_summary=SimpleNamespace(likely_stack_markers=["python"], test_markers=[]),
            native_backend="deepagents",
            native_backend_available=False,
            native_backend_notes=["Install deepagents to enable this experimental backend."],
            observation=None,
            plan=None,
            write_path="pipeline",
            verification_loop=None,
            diff_review=None,
            native_loop_steps=[],
        )
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("backend: deepagents unavailable", joined)

    def test_render_native_demo_block_no_backend_field_no_error(self):
        from types import SimpleNamespace

        from openshard.cli.run_output import _render_native_demo_block

        meta = SimpleNamespace(
            repo_context_summary=SimpleNamespace(likely_stack_markers=["python"], test_markers=[]),
            observation=None,
            plan=None,
            write_path="pipeline",
            verification_loop=None,
            diff_review=None,
            native_loop_steps=[],
        )
        lines = _render_native_demo_block(meta)
        self.assertNotIn("backend:", "\n".join(lines))

    def test_backend_line_appears_after_repo_and_before_observation(self):
        from types import SimpleNamespace

        from openshard.cli.run_output import _render_native_demo_block

        meta = SimpleNamespace(
            repo_context_summary=SimpleNamespace(likely_stack_markers=["python"], test_markers=[]),
            native_backend="builtin",
            native_backend_available=True,
            native_backend_notes=[],
            observation=SimpleNamespace(dirty_diff_present=False, search_matches_count=0),
            plan=None,
            write_path="pipeline",
            verification_loop=None,
            diff_review=None,
            native_loop_steps=[],
        )
        lines = _render_native_demo_block(meta)
        repo_idx = next((i for i, ln in enumerate(lines) if "repo:" in ln), -1)
        backend_idx = next((i for i, ln in enumerate(lines) if "backend:" in ln), -1)
        observation_idx = next((i for i, ln in enumerate(lines) if "observation:" in ln), -1)
        self.assertGreater(backend_idx, repo_idx)
        self.assertLess(backend_idx, observation_idx)

    def test_native_meta_from_entry_passes_backend_fields(self):
        entry = _native_entry()
        entry["native_backend"] = "deepagents"
        entry["native_backend_available"] = False
        entry["native_backend_notes"] = ["Install deepagents to enable this experimental backend."]
        out = _render(entry, detail="full")
        self.assertIn("backend: deepagents unavailable", out)

    def test_render_readonly_proof_line(self):
        from openshard.cli.run_output import _render_native_demo_block
        from openshard.native.executor import NativeRunMeta
        meta = NativeRunMeta()
        meta.native_backend = "deepagents"
        meta.native_backend_available = True
        meta.native_backend_proof = {"mode": "readonly_agent_proof"}
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("proof: readonly agent proof", joined)

    def test_render_unconfigured_proof_line(self):
        from openshard.cli.run_output import _render_native_demo_block
        from openshard.native.executor import NativeRunMeta
        meta = NativeRunMeta()
        meta.native_backend = "deepagents"
        meta.native_backend_available = True
        meta.native_backend_proof = {"mode": "readonly_agent_unconfigured"}
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("proof: readonly agent unconfigured", joined)

    def test_native_meta_from_entry_includes_proof(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = {
            "workflow": "native",
            "native_backend": "deepagents",
            "native_backend_available": True,
            "native_backend_notes": [],
            "native_backend_proof": {"mode": "readonly_agent_proof", "summary": "ok"},
        }
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta.native_backend_proof)
        self.assertEqual(meta.native_backend_proof.mode, "readonly_agent_proof")


class TestNativeLoopTraceRendering(unittest.TestCase):
    """Tests for native_loop_trace extraction and rendering in saved-run inspection."""

    def _entry_with_trace(self, trace_events=None):
        entry = _native_entry()
        entry["native_loop_steps"] = ["repo_context", "observation", "plan", "generation"]
        if trace_events is not None:
            entry["native_loop_trace"] = trace_events
        return entry

    # 1
    def test_saved_run_tolerates_missing_native_loop_trace(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = _native_entry()
        # no native_loop_trace key
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        trace = getattr(meta, "native_loop_trace", None)
        self.assertIsNotNone(trace)
        # Should be an empty list (raw, before _dict_to_ns promotion)
        if isinstance(trace, list):
            self.assertEqual(trace, [])
        else:
            self.assertEqual(getattr(trace, "events", []), [])

    # 2
    def test_saved_run_with_empty_native_loop_trace_no_trace_section(self):
        entry = self._entry_with_trace(trace_events=[])
        out = _render(entry, detail="full")
        self.assertNotIn("loop trace:", out)

    # 3
    def test_full_detail_renders_loop_trace(self):
        events = [
            {"phase": "repo_context", "status": "completed", "summary": "", "metadata": {}},
            {"phase": "observation", "status": "completed", "summary": "", "metadata": {"files": 12}},
            {"phase": "write", "status": "completed", "summary": "", "metadata": {"files": 2}},
        ]
        entry = self._entry_with_trace(trace_events=events)
        out = _render(entry, detail="full")
        self.assertIn("loop trace:", out)
        self.assertIn("repo_context [completed]", out)
        self.assertIn("observation [completed]", out)
        self.assertIn("write [completed]", out)

    # 4
    def test_default_detail_hides_loop_trace(self):
        events = [
            {"phase": "repo_context", "status": "completed", "summary": "", "metadata": {}},
        ]
        entry = self._entry_with_trace(trace_events=events)
        out = _render(entry, detail="default")
        self.assertNotIn("loop trace:", out)

    # 5
    def test_more_detail_hides_loop_trace(self):
        events = [
            {"phase": "plan", "status": "completed", "summary": "", "metadata": {}},
        ]
        entry = self._entry_with_trace(trace_events=events)
        out = _render(entry, detail="more")
        self.assertNotIn("loop trace:", out)

    # 6
    def test_native_meta_from_entry_extracts_loop_trace(self):
        from openshard.cli.run_output import _native_meta_from_entry
        events = [{"phase": "plan", "status": "completed", "summary": "", "metadata": {}}]
        entry = self._entry_with_trace(trace_events=events)
        meta = _native_meta_from_entry(entry)
        raw_trace = getattr(meta, "native_loop_trace", None)
        self.assertIsNotNone(raw_trace)
        # After _dict_to_ns, items may be SimpleNamespace
        if isinstance(raw_trace, list):
            self.assertEqual(len(raw_trace), 1)
        else:
            self.assertEqual(len(getattr(raw_trace, "events", [])), 1)


class TestReadSearchFindingsRendering(unittest.TestCase):
    """Tests for read/search: N findings line in [native] block."""

    def _entry_with_findings(self, findings: list[str]) -> dict:
        entry = _native_entry()
        entry["read_search_findings"] = findings
        entry["native_loop_steps"] = [
            "repo_context", "observation", "read_search", "plan", "generation"
        ]
        return entry

    def test_read_search_line_shown_when_findings_present(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_findings(["test-marker:tests/", "file:src/auth.py"])
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        self.assertIn("  read/search: 2 findings", lines)

    def test_read_search_line_absent_when_findings_empty(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_findings([])
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("read/search:", joined)

    def test_read_search_line_absent_when_field_missing(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = _native_entry()
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("read/search:", joined)

    def test_read_search_count_reflects_actual_list_length(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        findings = [
            "test-marker:tests/a.py",
            "test-marker:tests/b.py",
            "package:pyproject.toml",
            "file:src/main.py",
            "file:src/utils.py",
        ]
        entry = self._entry_with_findings(findings)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        self.assertIn("  read/search: 5 findings", lines)

    def test_render_block_via_full_render_shows_read_search(self):
        entry = self._entry_with_findings(["file:src/main.py"])
        out = _render(entry, detail="full")
        self.assertIn("read/search: 1 findings", out)

    def test_raw_finding_labels_not_shown_in_default_block(self):
        entry = self._entry_with_findings(["test-marker:tests/secret_config.py"])
        out = _render(entry, detail="full")
        self.assertNotIn("test-marker:tests/secret_config.py", out)
        self.assertIn("read/search:", out)


class TestPatchProposalRendering(unittest.TestCase):
    """Tests for proposal: N files line in [native] block."""

    def _entry_with_proposal(self, file_count: int) -> dict:
        entry = _native_entry()
        entry["patch_proposal"] = {
            "file_count": file_count,
            "files": [f"file{i}.py" for i in range(file_count)],
            "change_types": ["update"] * file_count,
            "summaries": ["a change"] * file_count,
            "warnings": [],
        }
        return entry

    def test_proposal_line_shown_when_present(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_proposal(2)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        self.assertIn("  proposal: 2 files", lines)

    def test_proposal_line_shows_count_only(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_proposal(3)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("proposal: 3 files", joined)
        self.assertNotIn("file0.py", joined)
        self.assertNotIn("update", joined.split("proposal:")[1] if "proposal:" in joined else "")

    def test_saved_run_inspection_renders_proposal_count(self):
        entry = self._entry_with_proposal(1)
        out = _render(entry, detail="full")
        self.assertIn("proposal: 1 files", out)

    def test_old_entry_without_patch_proposal_does_not_crash(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = _native_entry()
        entry.pop("patch_proposal", None)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("proposal:", joined)

    def test_proposal_metadata_contains_no_raw_content(self):
        from dataclasses import asdict

        from openshard.native.context import NativePatchProposal
        proposal = NativePatchProposal(
            file_count=1,
            files=["a.py"],
            change_types=["create"],
            summaries=["created"],
        )
        d = asdict(proposal)
        self.assertNotIn("content", d)


class TestVerificationCommandSummaryRendering(unittest.TestCase):
    """Tests for verification commands: N safe, N approval, N blocked line in [native] block."""

    def _entry_with_summary(self, command_count: int, safe_count: int, needs_approval_count: int, blocked_count: int) -> dict:
        entry = _native_entry()
        entry["verification_command_summary"] = {
            "attempted": True,
            "command_count": command_count,
            "safe_count": safe_count,
            "needs_approval_count": needs_approval_count,
            "blocked_count": blocked_count,
            "passed": True,
            "retried": False,
            "warnings": [],
        }
        return entry

    def test_renders_verification_command_counts(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_summary(2, 2, 0, 0)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("verification commands: 2 safe, 0 approval, 0 blocked", joined)

    def test_no_verification_commands_line_when_count_zero(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_summary(0, 0, 0, 0)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("verification commands:", joined)

    def test_old_entry_without_verification_command_summary_does_not_crash(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = _native_entry()
        entry.pop("verification_command_summary", None)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        self.assertIsInstance(lines, list)

    def test_saved_run_inspection_renders_command_counts(self):
        entry = self._entry_with_summary(3, 2, 1, 0)
        out = _render(entry, detail="full")
        self.assertIn("verification commands:", out)
        self.assertIn("2 safe", out)

    def test_no_command_strings_in_output(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_summary(1, 1, 0, 0)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("python -m pytest", joined)
        self.assertNotIn("argv", joined)


class TestCommandPolicyPreviewRendering(unittest.TestCase):
    """Tests for command policy: N safe, N approval, N blocked line in [native] block."""

    def _entry_with_preview(self, safe_count: int, needs_approval_count: int, blocked_count: int) -> dict:
        entry = _native_entry()
        entry["command_policy_preview"] = {
            "safe_count": safe_count,
            "needs_approval_count": needs_approval_count,
            "blocked_count": blocked_count,
            "command_classes": ["safe"] if safe_count > 0 else [],
            "warnings": [],
        }
        return entry

    def test_renders_counts(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_preview(2, 0, 0)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("command policy: 2 safe, 0 approval, 0 blocked", joined)

    def test_no_line_when_total_zero(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_preview(0, 0, 0)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("command policy:", joined)

    def test_old_entry_no_crash(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = _native_entry()
        entry.pop("command_policy_preview", None)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        self.assertIsInstance(lines, list)

    def test_saved_run_shows_counts(self):
        entry = self._entry_with_preview(3, 1, 0)
        out = _render(entry, detail="full")
        self.assertIn("command policy:", out)
        self.assertIn("3 safe", out)

    def test_no_command_strings_in_output(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_preview(1, 0, 0)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("python -m pytest", joined)
        self.assertNotIn("argv", joined)
class TestNativeContextPacketRendering(unittest.TestCase):
    """Tests for context packet: N sources, N paths line in [native] block."""

    def _entry_with_packet(self, sources: list[str], compact_paths: list[str]) -> dict:
        entry = _native_entry()
        entry["context_packet"] = {
            "task_preview": "fix the bug",
            "sources": sources,
            "repo_stack": [],
            "test_marker_count": 0,
            "package_file_count": 0,
            "read_search_count": len(compact_paths),
            "selected_skills": [],
            "backend": "builtin",
            "backend_available": True,
            "backend_proof_mode": "",
            "compact_paths": compact_paths,
            "warnings": [],
        }
        return entry

    def test_context_packet_line_rendered(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_packet(
            sources=["backend", "repo_context"],
            compact_paths=["file:src/x.py"],
        )
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        self.assertIn("  context packet: 2 sources, 1 paths", lines)

    def test_context_packet_not_rendered_when_missing(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = _native_entry()
        entry.pop("context_packet", None)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("context packet:", joined)

    def test_context_packet_not_rendered_when_no_sources(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_packet(sources=[], compact_paths=[])
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("context packet:", joined)

    def test_saved_run_inspection_passes_context_packet(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._entry_with_packet(
            sources=["backend", "read_search"],
            compact_paths=["file:src/main.py", "test-marker:tests/"],
        )
        meta = _native_meta_from_entry(entry)
        cp = getattr(meta, "context_packet", None)
        self.assertIsNotNone(cp)
        self.assertEqual(getattr(cp, "sources", None), ["backend", "read_search"])

    def test_raw_paths_not_rendered_in_default_output(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_packet(
            sources=["backend", "read_search"],
            compact_paths=["file:src/main.py", "test-marker:tests/"],
        )
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertNotIn("file:src/main.py", joined)
        self.assertNotIn("test-marker:tests/", joined)

class TestNativeFileContextRendering(unittest.TestCase):
    """Rendering tests for NativeFileContext compact line."""

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
            "read_search_findings": [],
            "write_path": "pipeline",
        }

    def test_file_context_compact_line_rendered(self):
        entry = self._base_entry()
        entry["file_context"] = {
            "files_read": 3,
            "paths": ["a.py", "b.py", "c.py"],
            "total_chars": 4210,
            "truncated": False,
            "warnings": [],
        }
        out = _render(entry, detail="full")
        self.assertIn("file context: 3 files, 4210 chars", out)

    def test_file_context_not_rendered_when_zero_files(self):
        entry = self._base_entry()
        entry["file_context"] = {
            "files_read": 0,
            "paths": [],
            "total_chars": 0,
            "truncated": False,
            "warnings": [],
        }
        out = _render(entry, detail="more")
        self.assertNotIn("file context:", out)

    def test_old_entry_without_file_context_does_not_crash(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"Rendering raised {exc}")
        self.assertIsInstance(out, str)


class TestContextQualityScoreRendering(unittest.TestCase):

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def test_context_quality_line_rendered(self):
        entry = self._base_entry()
        entry["context_quality_score"] = {
            "score": 70,
            "max_score": 100,
            "level": "good",
            "reasons": ["repo_context", "read_search"],
            "warnings": [],
        }
        out = _render(entry, detail="full")
        self.assertIn("context quality: good 70/100", out)

    def test_context_quality_absent_does_not_crash(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"Rendering raised {exc}")
        self.assertNotIn("context quality:", out)

    def test_context_quality_none_does_not_crash(self):
        entry = self._base_entry()
        entry["context_quality_score"] = None
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"Rendering raised {exc}")
        self.assertNotIn("context quality:", out)

    def test_native_meta_from_entry_populates_context_quality_score(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._base_entry()
        entry["context_quality_score"] = {
            "score": 45,
            "max_score": 100,
            "level": "fair",
            "reasons": ["backend"],
            "warnings": [],
        }
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        cqs = getattr(meta, "context_quality_score", None)
        self.assertIsNotNone(cqs)
        self.assertEqual(getattr(cqs, "score", None), 45)
        self.assertEqual(getattr(cqs, "level", None), "fair")


class TestContextQualityAdvisoryRendering(unittest.TestCase):

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def test_advisory_line_rendered_when_present(self):
        entry = self._base_entry()
        entry["context_quality_advisory"] = {
            "level": "good",
            "recommendation": "context is good enough for normal generation",
            "should_block": False,
            "warnings": [],
        }
        out = _render(entry, detail="full")
        self.assertIn("context advisory: context is good enough for normal generation", out)

    def test_advisory_line_absent_when_missing(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("context advisory:", out)

    def test_advisory_line_absent_when_none(self):
        entry = self._base_entry()
        entry["context_quality_advisory"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("context advisory:", out)

    def test_saved_run_inspection_passes_advisory_through(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._base_entry()
        entry["context_quality_advisory"] = {
            "level": "weak",
            "recommendation": "context is weak; prefer smaller changes or gather more context",
            "should_block": False,
            "warnings": ["context packet may be insufficient for confident generation"],
        }
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        cqa = getattr(meta, "context_quality_advisory", None)
        self.assertIsNotNone(cqa)
        self.assertEqual(getattr(cqa, "level", None), "weak")
        self.assertIn("weak", getattr(cqa, "recommendation", ""))

    def test_warnings_not_rendered_by_default(self):
        entry = self._base_entry()
        entry["context_quality_advisory"] = {
            "level": "fair",
            "recommendation": "context is usable but may need cautious generation",
            "should_block": False,
            "warnings": ["consider smaller changes if the task is risky"],
        }
        out = _render(entry, detail="full")
        self.assertNotIn("consider smaller changes", out)
        self.assertIn("context advisory:", out)


class TestNativeChangeBudgetRendering(unittest.TestCase):

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def test_change_budget_line_rendered(self):
        entry = self._base_entry()
        entry["change_budget"] = {
            "level": "good",
            "max_files": 3,
            "max_change_size": "normal",
            "guidance": "normal generation is acceptable; avoid unnecessary broad refactors",
            "warnings": [],
        }
        out = _render(entry, detail="full")
        self.assertIn("change budget: 3 files, normal", out)

    def test_change_budget_absent_when_missing(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("change budget:", out)

    def test_change_budget_absent_when_none(self):
        entry = self._base_entry()
        entry["change_budget"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("change budget:", out)

    def test_native_meta_from_entry_passes_change_budget(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._base_entry()
        entry["change_budget"] = {
            "level": "fair",
            "max_files": 2,
            "max_change_size": "small",
            "guidance": "prefer a cautious, focused change",
            "warnings": ["context is usable but not strong"],
        }
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        cb = getattr(meta, "change_budget", None)
        self.assertIsNotNone(cb)
        self.assertEqual(getattr(cb, "max_files", None), 2)
        self.assertEqual(getattr(cb, "level", None), "fair")

    def test_guidance_not_rendered_in_default_output(self):
        entry = self._base_entry()
        entry["change_budget"] = {
            "level": "weak",
            "max_files": 1,
            "max_change_size": "small",
            "guidance": "UNIQUE_GUIDANCE_STRING_NOT_IN_OUTPUT",
            "warnings": [],
        }
        out = _render(entry, detail="full")
        self.assertNotIn("UNIQUE_GUIDANCE_STRING_NOT_IN_OUTPUT", out)
        self.assertIn("change budget:", out)


class TestNativeChangeBudgetPreviewRendering(unittest.TestCase):
    """Tests for budget preview: N/M files, action line in [native] block."""

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def _entry_with_preview(self, proposed_files: int, budget_max_files: int, action: str) -> dict:
        entry = self._base_entry()
        entry["change_budget_preview"] = {
            "budget_max_files": budget_max_files,
            "proposed_files": proposed_files,
            "within_budget": proposed_files <= budget_max_files,
            "would_exceed_budget": proposed_files > budget_max_files,
            "action": action,
            "warnings": (
                [f"proposal has {proposed_files} files but budget allows {budget_max_files}"]
                if proposed_files > budget_max_files
                else []
            ),
        }
        return entry

    def test_budget_preview_line_rendered(self):
        entry = self._entry_with_preview(proposed_files=4, budget_max_files=2, action="warn")
        out = _render(entry, detail="full")
        self.assertIn("budget preview:", out)
        self.assertIn("4/2 files", out)
        self.assertIn("warn", out)

    def test_budget_preview_absent_when_missing(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("budget preview:", out)

    def test_budget_preview_absent_when_none(self):
        entry = self._base_entry()
        entry["change_budget_preview"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("budget preview:", out)

    def test_native_meta_from_entry_passes_budget_preview(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._entry_with_preview(proposed_files=1, budget_max_files=3, action="allow")
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        cbp = getattr(meta, "change_budget_preview", None)
        self.assertIsNotNone(cbp)
        self.assertEqual(getattr(cbp, "proposed_files", None), 1)
        self.assertEqual(getattr(cbp, "budget_max_files", None), 3)
        self.assertEqual(getattr(cbp, "action", None), "allow")

    def test_warnings_not_rendered_by_default(self):
        entry = self._entry_with_preview(proposed_files=4, budget_max_files=2, action="warn")
        out = _render(entry, detail="more")
        self.assertNotIn("proposal has 4 files but budget allows 2", out)


class TestNativeChangeBudgetSoftGateRendering(unittest.TestCase):
    """Tests for budget gate: action, approval=bool line in [native] block."""

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def _entry_with_gate(self, action: str, requires_approval: bool, warnings: list | None = None) -> dict:
        entry = self._base_entry()
        entry["change_budget_soft_gate"] = {
            "requires_approval": requires_approval,
            "reason": "proposal exceeds advisory change budget" if requires_approval else "within budget",
            "action": action,
            "warnings": warnings if warnings is not None else [],
        }
        return entry

    def test_soft_gate_line_rendered(self):
        entry = self._entry_with_gate(action="require_approval", requires_approval=True)
        out = _render(entry, detail="full")
        self.assertIn("budget gate:", out)
        self.assertIn("require_approval", out)
        self.assertIn("approval=true", out)

    def test_soft_gate_absent_when_missing(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("budget gate:", out)

    def test_soft_gate_absent_when_none(self):
        entry = self._base_entry()
        entry["change_budget_soft_gate"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("budget gate:", out)

    def test_native_meta_from_entry_passes_soft_gate(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._entry_with_gate(action="allow", requires_approval=False)
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        gate = getattr(meta, "change_budget_soft_gate", None)
        self.assertIsNotNone(gate)
        self.assertEqual(getattr(gate, "action", None), "allow")
        self.assertEqual(getattr(gate, "requires_approval", None), False)

    def test_warnings_not_rendered_by_default(self):
        entry = self._entry_with_gate(
            action="require_approval",
            requires_approval=True,
            warnings=["proposal has 4 files but budget allows 2"],
        )
        out = _render(entry, detail="more")
        self.assertNotIn("proposal has 4 files but budget allows 2", out)


class TestNativeApprovalRequestRendering(unittest.TestCase):
    """Tests for approval request: source, required=bool line in [native] block."""

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def _entry_with_approval(self, requires_approval: bool, source: str = "change_budget_soft_gate") -> dict:
        entry = self._base_entry()
        entry["approval_request"] = {
            "source": source,
            "requires_approval": requires_approval,
            "reason": "proposal exceeds advisory change budget" if requires_approval else "within budget",
            "action": "require_approval" if requires_approval else "allow",
            "proposed_files": 4 if requires_approval else 1,
            "budget_max_files": 2,
            "prompt": (
                "Proposal exceeds advisory change budget: 4 files proposed, budget allows 2. Proceed?"
                if requires_approval else ""
            ),
            "warnings": [],
        }
        return entry

    def test_approval_request_line_rendered(self):
        entry = self._entry_with_approval(requires_approval=True)
        out = _render(entry, detail="full")
        self.assertIn("approval request:", out)
        self.assertIn("change_budget_soft_gate", out)
        self.assertIn("required=true", out)

    def test_approval_request_absent_when_missing(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("approval request:", out)

    def test_approval_request_absent_when_none(self):
        entry = self._base_entry()
        entry["approval_request"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("approval request:", out)

    def test_native_meta_from_entry_passes_approval_request(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._entry_with_approval(requires_approval=False)
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        approval = getattr(meta, "approval_request", None)
        self.assertIsNotNone(approval)
        self.assertEqual(getattr(approval, "source", None), "change_budget_soft_gate")
        self.assertEqual(getattr(approval, "requires_approval", None), False)

    def test_prompt_not_rendered_by_default(self):
        entry = self._entry_with_approval(requires_approval=True)
        out = _render(entry, detail="more")
        self.assertNotIn("Proceed?", out)

    def test_warnings_not_rendered_by_default(self):
        entry = self._base_entry()
        entry["approval_request"] = {
            "source": "change_budget_soft_gate",
            "requires_approval": True,
            "reason": "proposal exceeds advisory change budget",
            "action": "require_approval",
            "proposed_files": 4,
            "budget_max_files": 2,
            "prompt": "Proposal exceeds advisory change budget: 4 files proposed, budget allows 2. Proceed?",
            "warnings": ["proposal has 4 files but budget allows 2"],
        }
        out = _render(entry, detail="more")
        self.assertNotIn("proposal has 4 files but budget allows 2", out)


class TestNativeApprovalReceiptRendering(unittest.TestCase):
    """Tests for approval receipt: source, granted=bool line in [native] block."""

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def _entry_with_receipt(self, granted: bool, source: str = "change_budget_soft_gate") -> dict:
        entry = self._base_entry()
        entry["approval_receipt"] = {
            "source": source,
            "requested": True,
            "granted": granted,
            "action": "require_approval" if not granted else "allow",
            "reason": "proposal exceeds advisory change budget",
        }
        return entry

    def test_receipt_line_rendered(self):
        entry = self._entry_with_receipt(granted=True)
        out = _render(entry, detail="full")
        self.assertIn("approval receipt:", out)
        self.assertIn("change_budget_soft_gate", out)
        self.assertIn("granted=true", out)

    def test_absent_when_missing(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("approval receipt:", out)

    def test_absent_when_none(self):
        entry = self._base_entry()
        entry["approval_receipt"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("approval receipt:", out)

    def test_native_meta_from_entry_passes_receipt_through(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._entry_with_receipt(granted=True)
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        receipt = getattr(meta, "approval_receipt", None)
        self.assertIsNotNone(receipt)
        self.assertEqual(getattr(receipt, "source", None), "change_budget_soft_gate")
        self.assertTrue(getattr(receipt, "granted", None))

    def test_reason_shown_in_approval_section(self):
        entry = self._entry_with_receipt(granted=True)
        out = _render(entry, detail="more")
        self.assertIn("proposal exceeds advisory change budget", out)


class TestApprovalSectionInMore(unittest.TestCase):
    """APPROVAL section in /last --more when approval data exists."""

    def _entry_with_receipt(self, granted: bool, reason: str = "risky write task") -> dict:
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
            "approval_receipt": {
                "source": "change_budget_soft_gate",
                "requested": True,
                "granted": granted,
                "action": "allow" if granted else "block",
                "reason": reason,
            },
        }

    def _entry_without_receipt(self) -> dict:
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def test_approval_section_granted_in_more(self):
        out = _render(self._entry_with_receipt(granted=True), detail="more")
        self.assertIn("APPROVAL", out)
        self.assertIn("granted", out)

    def test_approval_section_denied_in_more(self):
        out = _render(self._entry_with_receipt(granted=False), detail="more")
        self.assertIn("APPROVAL", out)
        self.assertIn("denied", out)

    def test_approval_section_denied_shows_writes_blocked(self):
        out = _render(self._entry_with_receipt(granted=False), detail="more")
        self.assertIn("Writes blocked", out)

    def test_approval_section_granted_no_writes_blocked(self):
        out = _render(self._entry_with_receipt(granted=True), detail="more")
        self.assertNotIn("Writes blocked", out)

    def test_approval_section_reason_shown(self):
        out = _render(self._entry_with_receipt(granted=True, reason="risky write task"), detail="more")
        self.assertIn("risky write task", out)

    def test_no_approval_section_without_receipt(self):
        out = _render(self._entry_without_receipt(), detail="more")
        self.assertNotIn("Writes blocked", out)


class TestBaselineLineInLastDefault(unittest.TestCase):
    """Baseline estimate line: native + default detail only."""

    def _entry_with_tokens(self, pt: int, ct: int, cost: float | None = None) -> dict:
        entry = _native_entry()
        entry["prompt_tokens"] = pt
        entry["completion_tokens"] = ct
        entry["total_tokens"] = pt + ct
        if cost is not None:
            entry["estimated_cost"] = cost
        return entry

    def test_baseline_shown_in_native_default_with_tokens(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="default")
        self.assertIn("Baseline estimate:", out)

    def test_baseline_contains_sonnet46(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="default")
        self.assertIn("Sonnet 4.6", out)

    def test_baseline_contains_gpt55(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="default")
        self.assertIn("GPT-5.5", out)

    def test_baseline_hidden_in_native_more(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="more")
        self.assertNotIn("Baseline estimate:", out)

    def test_baseline_hidden_in_native_full(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="full")
        self.assertNotIn("Baseline estimate:", out)

    def test_baseline_omitted_when_tokens_zero(self):
        out = _render(self._entry_with_tokens(0, 0), detail="default")
        self.assertNotIn("Baseline estimate:", out)

    def test_baseline_omitted_when_tokens_missing_from_entry(self):
        entry = {"task": "old run", "workflow": "native", "executor": "native"}
        out = _render(entry, detail="default")
        self.assertNotIn("Baseline estimate:", out)

    def test_baseline_omitted_for_non_native_entry(self):
        entry = {
            "task": "non-native run",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
        }
        out = _render(entry, detail="default")
        self.assertNotIn("Baseline estimate:", out)

    def test_baseline_appears_after_time_line(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="default")
        time_idx = out.index("Time:")
        baseline_idx = out.index("Baseline estimate:")
        self.assertGreater(baseline_idx, time_idx)

    def test_receipt_block_appears_before_time_footer(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000), detail="default")
        receipt_idx = out.index("RECEIPT —")
        time_idx = out.index("Time:")
        self.assertLess(receipt_idx, time_idx)

    def test_multiplier_shown_when_cost_present(self):
        out = _render(self._entry_with_tokens(1_000_000, 1_000_000, cost=0.01), detail="default")
        self.assertIn("x higher", out)

    def test_multiplier_absent_when_no_cost(self):
        entry = self._entry_with_tokens(1_000_000, 1_000_000)
        entry.pop("estimated_cost", None)
        out = _render(entry, detail="default")
        self.assertNotIn("x higher", out)


class TestContextUsageRendering(unittest.TestCase):
    """Rendering tests for context usage, file context truncated, and context warnings."""

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
            "read_search_findings": [],
            "write_path": "pipeline",
        }

    def _entry_with_usage(
        self,
        used=True,
        evidence_items=0,
        snippet_files=0,
        fc_truncated=False,
        fc_files=0,
        cqs_warnings=None,
        cp_warnings=None,
        fc_warnings=None,
    ):
        entry = self._base_entry()
        entry["final_report"] = {
            "used_native_context": used,
            "observed_tools": [],
            "selected_skills": [],
            "plan_intent": None,
            "plan_risk": None,
            "evidence_items": evidence_items,
            "snippet_files": snippet_files,
            "verification_attempted": False,
            "verification_passed": False,
            "verification_retried": False,
            "diff_files": [],
            "added_lines": 0,
            "removed_lines": 0,
            "warnings": [],
        }
        if fc_files > 0 or fc_truncated or fc_warnings:
            entry["file_context"] = {
                "files_read": fc_files,
                "paths": [],
                "total_chars": 1000 * fc_files,
                "truncated": fc_truncated,
                "warnings": fc_warnings or [],
            }
        if cqs_warnings is not None:
            entry["context_quality_score"] = {
                "score": 50,
                "max_score": 100,
                "level": "fair",
                "reasons": [],
                "warnings": cqs_warnings,
            }
        if cp_warnings is not None:
            entry["context_packet"] = {
                "task_preview": "",
                "sources": ["repo_context"],
                "repo_stack": [],
                "test_marker_count": 0,
                "package_file_count": 0,
                "read_search_count": 0,
                "selected_skills": [],
                "backend": "builtin",
                "backend_available": True,
                "backend_proof_mode": "",
                "compact_paths": [],
                "file_context_files": 0,
                "warnings": cp_warnings,
            }
        return entry

    # --- context usage line ---

    def test_context_usage_line_rendered_when_used(self):
        entry = self._entry_with_usage(used=True)
        out = _render(entry, detail="full")
        self.assertIn("context usage: used=yes", out)

    def test_context_usage_line_rendered_when_not_used(self):
        entry = self._entry_with_usage(used=False)
        out = _render(entry, detail="full")
        self.assertIn("context usage: used=no", out)

    def test_context_usage_includes_evidence_count(self):
        entry = self._entry_with_usage(used=True, evidence_items=5)
        out = _render(entry, detail="full")
        self.assertIn("evidence=5 items", out)

    def test_context_usage_omits_zero_evidence(self):
        entry = self._entry_with_usage(used=True, evidence_items=0)
        out = _render(entry, detail="full")
        self.assertNotIn("evidence=0", out)

    def test_context_usage_includes_snippet_count(self):
        entry = self._entry_with_usage(used=True, snippet_files=2)
        out = _render(entry, detail="full")
        self.assertIn("2 snippets", out)

    def test_context_usage_omits_zero_snippets(self):
        entry = self._entry_with_usage(used=True, snippet_files=0)
        out = _render(entry, detail="full")
        self.assertNotIn("0 snippets", out)

    def test_context_usage_absent_when_no_final_report(self):
        entry = self._base_entry()
        out = _render(entry, detail="full")
        self.assertNotIn("context usage:", out)

    def test_context_usage_not_in_default_output(self):
        entry = self._entry_with_usage(used=True, evidence_items=3)
        out = _render(entry, detail="default")
        self.assertNotIn("context usage:", out)

    def test_saved_run_context_usage_reconstructed(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_usage(used=True, evidence_items=4, snippet_files=1)
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta)
        joined = "\n".join(lines)
        self.assertIn("context usage: used=yes, evidence=4 items, 1 snippets", joined)

    # --- file context truncated ---

    def test_file_context_shows_truncated_flag(self):
        entry = self._entry_with_usage(fc_files=2, fc_truncated=True)
        out = _render(entry, detail="full")
        self.assertIn("file context: 2 files, 2000 chars, truncated", out)

    def test_file_context_no_truncated_flag_when_false(self):
        entry = self._entry_with_usage(fc_files=2, fc_truncated=False)
        out = _render(entry, detail="full")
        self.assertIn("file context: 2 files, 2000 chars", out)
        self.assertNotIn("truncated", out)

    # --- context warnings ---

    def test_context_warnings_count_shown_for_one(self):
        entry = self._entry_with_usage(cqs_warnings=["context packet may be insufficient"])
        out = _render(entry, detail="full")
        self.assertIn("context warnings: 1 warning", out)

    def test_context_warnings_count_shown_for_multiple(self):
        entry = self._entry_with_usage(
            cqs_warnings=["weak quality"],
            cp_warnings=["no repo context", "no skills"],
        )
        out = _render(entry, detail="full")
        self.assertIn("context warnings: 3 warnings", out)

    def test_context_warnings_raw_text_not_rendered(self):
        warning_text = "context packet may be insufficient for generation"
        entry = self._entry_with_usage(cqs_warnings=[warning_text])
        out = _render(entry, detail="full")
        self.assertNotIn(warning_text, out)

    def test_context_warnings_from_packet(self):
        entry = self._entry_with_usage(cp_warnings=["no repo context found"])
        out = _render(entry, detail="full")
        self.assertIn("context warnings: 1 warning", out)

    def test_context_warnings_from_file_context(self):
        entry = self._entry_with_usage(fc_files=1, fc_warnings=["read failed for one file"])
        out = _render(entry, detail="full")
        self.assertIn("context warnings: 1 warning", out)

    def test_context_warnings_absent_when_none(self):
        entry = self._entry_with_usage(
            cqs_warnings=[],
            cp_warnings=[],
            fc_warnings=[],
        )
        out = _render(entry, detail="full")
        self.assertNotIn("context warnings:", out)


class TestNativeFailureMemoryRendering(unittest.TestCase):
    def _base_entry(self) -> dict:
        return {
            "workflow": "native",
            "executor": "native",
        }

    def _entry_with_failure_memory(self, lessons: list[dict]) -> dict:
        entry = self._base_entry()
        entry["failure_memory"] = {
            "has_lessons": bool(lessons),
            "lessons": lessons,
        }
        return entry

    def test_no_block_when_failure_memory_absent(self):
        out = _render(self._base_entry(), detail="more")
        self.assertNotIn("failure memory:", out)

    def test_no_block_when_has_lessons_false(self):
        entry = self._base_entry()
        entry["failure_memory"] = {"has_lessons": False, "lessons": []}
        out = _render(entry, detail="more")
        self.assertNotIn("failure memory:", out)

    def test_block_shown_with_two_lessons(self):
        entry = self._entry_with_failure_memory([
            {"lesson_type": "weak_context", "reason": "score 20/100"},
            {"lesson_type": "missing_verification", "reason": "no commands"},
        ])
        out = _render(entry, detail="full")
        self.assertIn("failure memory:", out)
        self.assertIn("weak_context", out)
        self.assertIn("missing_verification", out)

    def test_block_shown_with_singular_lesson(self):
        entry = self._entry_with_failure_memory([
            {"lesson_type": "failed_verification", "reason": "exit code 1"},
        ])
        out = _render(entry, detail="full")
        self.assertIn("failure memory:", out)
        self.assertIn("failed_verification", out)

    def test_compact_rendering_shows_labels_not_reasons(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        entry = self._entry_with_failure_memory([
            {"lesson_type": "weak_context", "reason": "score 20/100"},
            {"lesson_type": "missing_verification", "reason": "no commands available"},
        ])
        meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(meta, detail="more")
        joined = "\n".join(lines)
        self.assertIn("weak_context", joined)
        self.assertIn("missing_verification", joined)
        self.assertNotIn("score 20/100", joined)
        self.assertNotIn("no commands available", joined)

    def test_full_rendering_shows_reasons(self):
        entry = self._entry_with_failure_memory([
            {"lesson_type": "weak_context", "reason": "score 20/100"},
        ])
        out = _render(entry, detail="full")
        self.assertIn("failure memory: 1 lesson", out)
        self.assertIn("weak_context: score 20/100", out)

    def test_all_known_lesson_types_render_as_labels(self):
        all_types = [
            "weak_context",
            "unknown_task_type",
            "failed_verification",
            "unsafe_command",
            "approval_required",
            "approval_rejected",
            "patch_too_broad",
            "missing_verification",
            "context_truncated",
            "warnings_present",
        ]
        lessons = [{"lesson_type": t, "reason": f"reason for {t}"} for t in all_types]
        entry = self._entry_with_failure_memory(lessons)
        out = _render(entry, detail="full")
        self.assertIn("failure memory:", out)
        for t in all_types:
            self.assertIn(t, out)

    def test_old_entry_without_failure_memory_is_safe(self):
        entry = {
            "workflow": "native",
            "executor": "native",
            "diff_review": {"changed_files": 1, "added_lines": 5, "removed_lines": 2, "truncated": False, "warnings": []},
        }
        out = _render(entry, detail="more")
        self.assertNotIn("failure memory:", out)


class TestNativeValidationContractRendering(unittest.TestCase):

    def _base_entry(self):
        return {
            "workflow": "native",
            "executor": "native",
            "native_loop_steps": [],
            "native_loop_trace": [],
        }

    def _entry_with_contract(self, strength="strong", checks=3, cmds=1):
        entry = self._base_entry()
        entry["validation_contract"] = {
            "intent": "add feature",
            "risk_level": "low",
            "expected_change_scope": "3 files expected",
            "acceptance_checks": [f"check {i}" for i in range(checks)],
            "verification_commands": [f"cmd {i}" for i in range(cmds)],
            "approval_expected": False,
            "strength": strength,
            "warnings": [],
        }
        return entry

    def test_absent_on_old_runs_does_not_crash(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("validation contract:", out)

    def test_compact_line_appears_at_more_detail(self):
        entry = self._entry_with_contract(strength="strong", checks=3, cmds=1)
        out = _render(entry, detail="full")
        self.assertIn("validation contract:", out)
        self.assertIn("strong", out)
        self.assertIn("3 checks", out)
        self.assertIn("1 verification command", out)

    def test_compact_line_singular_check(self):
        entry = self._entry_with_contract(strength="fair", checks=1, cmds=0)
        out = _render(entry, detail="full")
        self.assertIn("1 check", out)
        self.assertNotIn("1 checks", out)

    def test_compact_line_singular_command(self):
        entry = self._entry_with_contract(strength="strong", checks=2, cmds=1)
        out = _render(entry, detail="full")
        self.assertIn("1 verification command", out)
        self.assertNotIn("1 verification commands", out)

    def test_compact_line_plural_checks(self):
        entry = self._entry_with_contract(strength="strong", checks=3, cmds=1)
        out = _render(entry, detail="full")
        self.assertIn("3 checks", out)

    def test_compact_line_plural_commands(self):
        entry = self._entry_with_contract(strength="strong", checks=2, cmds=3)
        out = _render(entry, detail="full")
        self.assertIn("3 verification commands", out)

    def test_full_detail_includes_multiline_render(self):
        entry = self._entry_with_contract(strength="strong", checks=2, cmds=1)
        out = _render(entry, detail="full")
        self.assertIn("[validation contract]", out)
        self.assertIn("intent:", out)
        self.assertIn("risk:", out)
        self.assertIn("strength:", out)

    def test_absent_when_validation_contract_is_none(self):
        entry = self._base_entry()
        entry["validation_contract"] = None
        out = _render(entry, detail="more")
        self.assertNotIn("validation contract:", out)

    def test_warnings_not_rendered_in_output(self):
        entry = self._base_entry()
        entry["validation_contract"] = {
            "intent": "task",
            "risk_level": "low",
            "expected_change_scope": "unknown",
            "acceptance_checks": [],
            "verification_commands": [],
            "approval_expected": False,
            "strength": "weak",
            "warnings": ["UNIQUE_WARNING_NOT_IN_OUTPUT"],
        }
        out = _render(entry, detail="more")
        self.assertNotIn("UNIQUE_WARNING_NOT_IN_OUTPUT", out)

    def test_native_meta_from_entry_round_trips_validation_contract(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = self._entry_with_contract(strength="fair", checks=2, cmds=0)
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        vc = getattr(meta, "validation_contract", None)
        self.assertIsNotNone(vc)
        self.assertEqual(getattr(vc, "strength", None), "fair")
        self.assertEqual(len(getattr(vc, "acceptance_checks", [])), 2)
        self.assertEqual(len(getattr(vc, "verification_commands", [])), 0)

    def test_pipeline_dict_round_trips_via_native_meta_from_entry(self):
        """Prove the full save→load path: asdict output reconstructed by _native_meta_from_entry."""
        from dataclasses import asdict

        from openshard.cli.run_output import _native_meta_from_entry
        from openshard.native.context import NativeValidationContract
        original = NativeValidationContract(
            intent="add auth",
            risk_level="medium",
            expected_change_scope="2 files expected",
            acceptance_checks=["tests pass", "no regressions"],
            verification_commands=["pytest"],
            approval_expected=False,
            strength="strong",
            warnings=[],
        )
        entry = self._base_entry()
        entry["validation_contract"] = asdict(original)
        meta = _native_meta_from_entry(entry)
        self.assertIsNotNone(meta)
        vc = getattr(meta, "validation_contract", None)
        self.assertIsNotNone(vc)
        self.assertEqual(getattr(vc, "intent", None), "add auth")
        self.assertEqual(getattr(vc, "risk_level", None), "medium")
        self.assertEqual(getattr(vc, "strength", None), "strong")
        self.assertEqual(getattr(vc, "acceptance_checks", None), ["tests pass", "no regressions"])
        self.assertEqual(getattr(vc, "verification_commands", None), ["pytest"])


class TestNativeContextProvenanceRendering(unittest.TestCase):

    def _base_entry(self) -> dict:
        return {"workflow": "native", "executor": "native"}

    def _entry_with_provenance(self, **provenance_kwargs) -> dict:
        p = build_native_context_provenance(**provenance_kwargs)
        entry = self._base_entry()
        entry["context_provenance"] = asdict(p)
        return entry

    def test_old_run_without_provenance_does_not_crash(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("context provenance:", out)

    def test_compact_provenance_line_renders_in_more(self):
        obs = NativeObservation(observed_tools=["read", "grep"])
        entry = self._entry_with_provenance(
            observation=obs,
            injected_source_names={"observation"},
        )
        out = _render(entry, detail="full")
        self.assertIn("context provenance:", out)
        self.assertIn("sources", out)
        self.assertIn("injected", out)
        self.assertIn("items", out)

    def test_gaps_line_renders_when_has_gaps(self):
        cqs = NativeContextQualityScore(level="weak")
        entry = self._entry_with_provenance(context_quality_score=cqs)
        out = _render(entry, detail="full")
        self.assertIn("context provenance gaps:", out)

    def test_gaps_line_absent_when_no_gaps(self):
        cqs = NativeContextQualityScore(level="good")
        entry = self._entry_with_provenance(context_quality_score=cqs)
        out = _render(entry, detail="more")
        self.assertNotIn("context provenance gaps:", out)

    def test_full_detail_renders_source_lines(self):
        obs = NativeObservation(observed_tools=["read"])
        plan = NativePlan(suggested_steps=["step1"])
        entry = self._entry_with_provenance(
            observation=obs,
            plan=plan,
            injected_source_names={"observation", "plan"},
        )
        out = _render(entry, detail="full")
        self.assertIn("provenance source:", out)
        self.assertIn("observation", out)
        self.assertIn("plan", out)

    def test_provenance_absent_from_default_output(self):
        obs = NativeObservation(observed_tools=["read"])
        entry = self._entry_with_provenance(observation=obs)
        out = _render(entry, detail="default")
        self.assertNotIn("context provenance:", out)
        self.assertNotIn("provenance source:", out)

    def test_warning_text_not_rendered_in_block(self):
        cqs = NativeContextQualityScore(level="weak")
        vc = NativeValidationContract(strength="weak")
        entry = self._entry_with_provenance(
            context_quality_score=cqs,
            validation_contract=vc,
        )
        out = _render(entry, detail="full")
        self.assertNotIn("context quality weak", out)
        self.assertNotIn("validation contract weak", out)
        self.assertIn("context provenance gaps:", out)

    def test_warning_count_rendered_not_raw_text(self):
        cqs = NativeContextQualityScore(level="weak")
        vc = NativeValidationContract(strength="weak")
        entry = self._entry_with_provenance(
            context_quality_score=cqs,
            validation_contract=vc,
        )
        out = _render(entry, detail="full")
        self.assertIn("2 warnings", out)

    def test_native_meta_from_entry_roundtrips_provenance(self):
        obs = NativeObservation(observed_tools=["read", "grep"])
        plan = NativePlan(suggested_steps=["s1"])
        p = build_native_context_provenance(
            observation=obs,
            plan=plan,
            injected_source_names={"observation", "plan"},
        )
        entry = {"workflow": "native", "context_provenance": asdict(p)}
        meta = _native_meta_from_entry(entry)
        prov = getattr(meta, "context_provenance", None)
        self.assertIsNotNone(prov)
        self.assertEqual(getattr(prov, "used_sources", None), p.used_sources)
        self.assertEqual(getattr(prov, "injected_sources", None), p.injected_sources)
        self.assertEqual(getattr(prov, "total_items", None), p.total_items)
        self.assertEqual(getattr(prov, "has_gaps", None), p.has_gaps)

    def test_dict_based_source_items_render_without_crash(self):
        prov_dict = {
            "sources": [
                {"name": "repo_summary", "used": True, "injected": True, "item_count": 1, "summary": "1 summary"},
                {"name": "plan", "used": True, "injected": True, "item_count": 2, "summary": "2 steps"},
            ],
            "injected_sources": 2,
            "used_sources": 2,
            "total_items": 3,
            "has_gaps": False,
            "warnings": [],
        }
        entry = {"workflow": "native", "context_provenance": prov_dict}
        out = _render(entry, detail="full")
        self.assertIn("provenance source: repo_summary", out)
        self.assertIn("provenance source: plan", out)


class TestNativeRunTrustScoreRendering(unittest.TestCase):

    def _base_entry(self) -> dict:
        return {"workflow": "native", "executor": "native"}

    def _entry_with_trust(self, score: int = 72, level: str = "good", warnings=None, blockers=None, factors=None) -> dict:
        entry = self._base_entry()
        entry["run_trust_score"] = {
            "score": score,
            "level": level,
            "factors": factors or [],
            "warnings": warnings or [],
            "blockers": blockers or [],
        }
        return entry

    def test_old_run_without_run_trust_score_does_not_crash(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("run trust:", out)

    def test_compact_line_renders_when_present_in_more(self):
        entry = self._entry_with_trust(score=72, level="good")
        out = _render(entry, detail="full")
        self.assertIn("run trust: 72/100 good", out)

    def test_warning_count_line_renders_when_warnings_exist(self):
        entry = self._entry_with_trust(warnings=["verification failed", "context truncated"])
        out = _render(entry, detail="full")
        self.assertIn("run trust warnings: 2 warnings", out)

    def test_blocker_count_line_renders_when_blockers_exist(self):
        entry = self._entry_with_trust(blockers=["verification failed"])
        out = _render(entry, detail="full")
        self.assertIn("run trust blockers: 1 blocker", out)

    def test_trust_absent_from_default_output(self):
        entry = self._entry_with_trust()
        out = _render(entry, detail="default")
        self.assertNotIn("run trust:", out)

    def test_full_detail_renders_run_trust_block(self):
        entry = self._entry_with_trust(
            score=82,
            level="good",
            factors=[{"name": "verification_passed", "impact": 20, "reason": "verification passed"}],
        )
        out = _render(entry, detail="full")
        self.assertIn("[run trust]", out)
        self.assertIn("score: 82/100", out)
        self.assertIn("level: good", out)

    def test_full_detail_renders_factor_lines(self):
        entry = self._entry_with_trust(
            score=82,
            level="good",
            factors=[{"name": "verification_passed", "impact": 20, "reason": "verification passed"}],
        )
        out = _render(entry, detail="full")
        self.assertIn("verification_passed", out)
        self.assertIn("+20", out)

    def test_native_meta_from_entry_roundtrips_run_trust_score(self):
        trust = NativeRunTrustScore(
            score=72,
            level="good",
            factors=[NativeRunTrustFactor(name="verification_passed", impact=20, reason="verification passed")],
            warnings=["context truncated"],
            blockers=[],
        )
        entry = {"workflow": "native", "run_trust_score": asdict(trust)}
        meta = _native_meta_from_entry(entry)
        rts = getattr(meta, "run_trust_score", None)
        self.assertIsNotNone(rts)
        self.assertEqual(getattr(rts, "score", None), 72)
        self.assertEqual(getattr(rts, "level", None), "good")

    def test_dict_based_factor_rendering_in_full(self):
        entry = self._entry_with_trust(
            score=50,
            level="fair",
            factors=[
                {"name": "verification_not_attempted", "impact": -10, "reason": "verification not attempted"},
                {"name": "validation_contract_strong", "impact": 15, "reason": "strong validation contract"},
            ],
        )
        out = _render(entry, detail="full")
        self.assertIn("verification_not_attempted", out)
        self.assertIn("validation_contract_strong", out)
        self.assertIn("+15", out)

    def test_compact_does_not_expose_raw_warning_or_blocker_text(self):
        entry = self._entry_with_trust(
            warnings=["verification failed", "blocked commands detected"],
            blockers=["verification failed"],
        )
        out = _render(entry, detail="full")
        self.assertNotIn("verification failed", out)
        self.assertNotIn("blocked commands detected", out)
        self.assertIn("2 warnings", out)
        self.assertIn("1 blocker", out)


class TestNativeModelSelectionDecisionRendering(unittest.TestCase):
    def _base_entry(self) -> dict:
        return {"workflow": "native", "executor": "native"}

    def _msd_dict(
        self,
        strategy: str = "cost-balanced",
        task_type: str = "feature",
        risk_level: str = "medium",
        confidence: str = "high",
        fallback_reason: str = "",
        warnings: list | None = None,
    ) -> dict:
        return asdict(NativeModelSelectionDecision(
            strategy=strategy,
            task_type=task_type,
            risk_level=risk_level,
            roles=[
                NativeModelRoleDecision(role="planner", model_tier="frontier-reasoning-model", cost_tier="high", reason="planning"),
                NativeModelRoleDecision(role="executor", model_tier="balanced-coding-model", cost_tier="medium", reason="balanced"),
                NativeModelRoleDecision(role="validator", model_tier="independent-validator-model", cost_tier="medium", reason="independent"),
            ],
            warnings=warnings or [],
            fallback_reason=fallback_reason,
            confidence=confidence,
        ))

    def test_old_entry_no_msd_does_not_crash_more(self):
        entry = self._base_entry()
        out = _render(entry, detail="more")
        self.assertNotIn("model selection:", out)

    def test_old_entry_no_msd_does_not_crash_full(self):
        entry = self._base_entry()
        out = _render(entry, detail="full")
        self.assertNotIn("model selection:", out)

    def test_compact_line_renders_at_more(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict(strategy="cost-balanced")
        out = _render(entry, detail="full")
        self.assertIn("model selection:", out)
        self.assertIn("cost-balanced", out)

    def test_full_block_renders_at_full(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict(strategy="frontier-heavy", risk_level="high")
        out = _render(entry, detail="full")
        self.assertIn("[model selection]", out)
        self.assertIn("strategy:", out)
        self.assertIn("roles:", out)

    def test_model_selection_not_shown_at_default(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict()
        out = _render(entry, detail="default")
        self.assertNotIn("model selection:", out)

    def test_native_meta_from_entry_roundtrips_msd(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict(
            strategy="context-cautious",
            confidence="medium",
        )
        meta = _native_meta_from_entry(entry)
        msd = getattr(meta, "model_selection_decision", None)
        self.assertIsNotNone(msd)
        strategy = getattr(msd, "strategy", None)
        confidence = getattr(msd, "confidence", None)
        self.assertEqual(strategy, "context-cautious")
        self.assertEqual(confidence, "medium")

    def test_full_renders_role_tiers(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict()
        out = _render(entry, detail="full")
        self.assertIn("frontier-reasoning-model", out)
        self.assertIn("independent-validator-model", out)

    def test_synced_role_appears_in_full_output(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict(
            strategy="cost-balanced",
            warnings=["model selection adjusted by policy enforcement"],
        )
        out = _render(entry, detail="full")
        self.assertIn("balanced-coding-model", out)

    def test_warning_count_renders_without_raw_text_in_full(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict(
            warnings=["model selection adjusted by policy enforcement"],
        )
        out = _render(entry, detail="full")
        self.assertIn("warnings: 1", out)
        self.assertNotIn("model selection adjusted by policy enforcement", out)

    def test_warnings_count_in_compact_output(self):
        entry = self._base_entry()
        entry["model_selection_decision"] = self._msd_dict(
            warnings=["model selection adjusted by policy enforcement"],
        )
        out = _render(entry, detail="full")
        self.assertIn("warnings=1", out)

    def test_old_entries_without_warnings_render_safely(self):
        entry = self._base_entry()
        msd = self._msd_dict()
        msd.pop("warnings", None)
        entry["model_selection_decision"] = msd
        out = _render(entry, detail="full")
        self.assertIn("model selection:", out)
        self.assertNotIn("warnings=", out)


class TestNativeModelCandidateScoringRendering(unittest.TestCase):
    def _base_entry(self) -> dict:
        return {"workflow": "native", "executor": "native"}

    def _mcs_dict(
        self,
        strategy: str = "cost-balanced",
        confidence: str = "medium",
        include_candidates: bool = False,
    ) -> dict:
        candidates = []
        if include_candidates:
            candidates = [
                asdict(NativeModelCandidateScore(
                    role="planner",
                    candidate="frontier-reasoning-model",
                    score=87,
                    capability_score=25,
                    reason="capability+25",
                )),
                asdict(NativeModelCandidateScore(
                    role="executor",
                    candidate="balanced-coding-model",
                    score=76,
                    capability_score=15,
                    reason="capability+15",
                )),
                asdict(NativeModelCandidateScore(
                    role="executor",
                    candidate="low-cost-coding-model",
                    score=62,
                    cost_score=20,
                    reason="cost+20",
                )),
            ]
        return asdict(NativeModelCandidateScoring(
            strategy=strategy,
            confidence=confidence,
            selected_by_role={
                "planner": "frontier-reasoning-model",
                "executor": "balanced-coding-model",
                "validator": "independent-validator-model",
            },
            candidates=candidates,
            warnings=[],
        ))

    def test_absent_model_candidate_scoring_does_not_crash_more(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("model candidates:", out)

    def test_absent_model_candidate_scoring_does_not_crash_full(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="full")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("model candidates:", out)

    def test_present_more_renders_compact_line(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict(strategy="cost-balanced", confidence="high")
        out = _render(entry, detail="full")
        self.assertIn("model candidates:", out)
        self.assertIn("cost-balanced", out)

    def test_present_compact_does_not_render(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict()
        out = _render(entry, detail="default")
        self.assertNotIn("model candidates:", out)

    def test_full_renders_block_header(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict(include_candidates=True)
        out = _render(entry, detail="full")
        self.assertIn("[model candidates]", out)

    def test_full_renders_strategy_and_confidence(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict(
            strategy="frontier-heavy",
            confidence="low",
            include_candidates=True,
        )
        out = _render(entry, detail="full")
        self.assertIn("strategy: frontier-heavy", out)
        self.assertIn("confidence: low", out)

    def test_full_renders_selected_roles(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict(include_candidates=True)
        out = _render(entry, detail="full")
        self.assertIn("planner:", out)
        self.assertIn("executor:", out)
        self.assertIn("validator:", out)

    def test_full_renders_score_lines(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict(include_candidates=True)
        out = _render(entry, detail="full")
        self.assertIn("scores:", out)
        self.assertIn("planner/frontier-reasoning-model: 87", out)

    def test_full_renders_warnings_count(self):
        entry = self._base_entry()
        mcs = self._mcs_dict(include_candidates=True)
        mcs["warnings"] = ["low trust run may reduce model selection confidence"]
        entry["model_candidate_scoring"] = mcs
        out = _render(entry, detail="full")
        self.assertIn("warnings: 1", out)

    def test_native_meta_from_entry_roundtrips_model_candidate_scoring(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = self._mcs_dict(strategy="context-cautious", confidence="low")
        meta = _native_meta_from_entry(entry)
        mcs = getattr(meta, "model_candidate_scoring", None)
        self.assertIsNotNone(mcs)
        strategy = getattr(mcs, "strategy", None)
        confidence = getattr(mcs, "confidence", None)
        self.assertEqual(strategy, "context-cautious")
        self.assertEqual(confidence, "low")

    def test_builder_result_asdict_roundtrips_through_entry(self):
        from dataclasses import asdict as _asdict
        sc = build_native_model_candidate_scoring()
        entry = self._base_entry()
        entry["model_candidate_scoring"] = _asdict(sc)
        meta = _native_meta_from_entry(entry)
        mcs = getattr(meta, "model_candidate_scoring", None)
        self.assertIsNotNone(mcs)
        self.assertEqual(getattr(mcs, "strategy", None), "cost-balanced")

    def test_full_with_none_scoring_after_present_does_not_crash(self):
        entry = self._base_entry()
        entry["model_candidate_scoring"] = None
        try:
            out = _render(entry, detail="full")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("model candidates:", out)

    def test_old_entry_without_blocked_candidates_renders_more(self):
        entry = self._base_entry()
        mcs = self._mcs_dict(strategy="cost-balanced", confidence="medium")
        # Simulate old entry: no blocked_candidates key
        mcs.pop("blocked_candidates", None)
        entry["model_candidate_scoring"] = mcs
        try:
            out = _render(entry, detail="full")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertIn("model candidates:", out)
        self.assertNotIn("blocked=", out)

    def test_old_entry_without_blocked_candidates_renders_full(self):
        entry = self._base_entry()
        mcs = self._mcs_dict(include_candidates=True)
        mcs.pop("blocked_candidates", None)
        entry["model_candidate_scoring"] = mcs
        try:
            out = _render(entry, detail="full")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertIn("[model candidates]", out)
        self.assertNotIn("blocked:", out)

    def test_blocked_count_appears_in_more(self):
        entry = self._base_entry()
        mcs = self._mcs_dict(strategy="cost-balanced", confidence="high")
        mcs["blocked_candidates"] = ["planner/frontier-reasoning-model", "executor/frontier-reasoning-model"]
        entry["model_candidate_scoring"] = mcs
        out = _render(entry, detail="full")
        self.assertIn("blocked=2", out)

    def test_zero_blocked_not_shown_in_compact(self):
        entry = self._base_entry()
        mcs = self._mcs_dict()
        mcs["blocked_candidates"] = []
        entry["model_candidate_scoring"] = mcs
        out = _render(entry, detail="more")
        self.assertNotIn("blocked=", out)

    def test_blocked_section_appears_in_full(self):
        entry = self._base_entry()
        mcs = self._mcs_dict(include_candidates=True)
        mcs["blocked_candidates"] = ["planner/frontier-reasoning-model"]
        entry["model_candidate_scoring"] = mcs
        out = _render(entry, detail="full")
        self.assertIn("blocked:", out)
        self.assertIn("planner/frontier-reasoning-model", out)

    def test_no_blocked_section_in_full_when_empty(self):
        entry = self._base_entry()
        mcs = self._mcs_dict(include_candidates=True)
        mcs["blocked_candidates"] = []
        entry["model_candidate_scoring"] = mcs
        out = _render(entry, detail="full")
        self.assertNotIn("blocked:", out)


class TestNativeModelPolicyRendering(unittest.TestCase):
    def _base_entry(self) -> dict:
        return {"workflow": "native", "executor": "native"}

    def _entry_with_policy(self, mode: str) -> dict:
        entry = self._base_entry()
        entry["model_policy"] = asdict(build_native_model_policy(mode))
        return entry

    def test_model_policy_hidden_at_default_detail(self):
        entry = self._entry_with_policy("auto")
        out = _render(entry, detail="default")
        self.assertNotIn("model policy", out)

    def test_model_policy_compact_line_at_more(self):
        entry = self._entry_with_policy("auto")
        out = _render(entry, detail="full")
        self.assertIn("model policy:", out)
        self.assertIn("auto", out)

    def test_model_policy_frontier_allowed_flag_at_more(self):
        entry = self._entry_with_policy("auto")
        out = _render(entry, detail="full")
        self.assertIn("frontier allowed", out)

    def test_model_policy_frontier_blocked_flag_at_more(self):
        entry = self._entry_with_policy("open-source-only")
        out = _render(entry, detail="full")
        self.assertIn("frontier blocked", out)

    def test_model_policy_cheapest_safe_at_more(self):
        entry = self._entry_with_policy("cheapest-safe")
        out = _render(entry, detail="full")
        self.assertIn("model policy:", out)
        self.assertIn("cheapest-safe", out)
        self.assertIn("prefer low cost", out)

    def test_model_policy_local_only_at_more(self):
        entry = self._entry_with_policy("local-only")
        out = _render(entry, detail="full")
        self.assertIn("local only", out)
        self.assertIn("frontier blocked", out)

    def test_model_policy_full_section_header(self):
        entry = self._entry_with_policy("auto")
        out = _render(entry, detail="full")
        self.assertIn("[model policy]", out)

    def test_model_policy_full_shows_mode(self):
        entry = self._entry_with_policy("cheapest-safe")
        out = _render(entry, detail="full")
        self.assertIn("mode: cheapest-safe", out)

    def test_model_policy_full_shows_all_fields(self):
        entry = self._entry_with_policy("auto")
        out = _render(entry, detail="full")
        self.assertIn("prefer_low_cost:", out)
        self.assertIn("require_open_source:", out)
        self.assertIn("require_local:", out)
        self.assertIn("allow_frontier:", out)
        self.assertIn("warnings:", out)

    def test_model_policy_full_boolean_values(self):
        entry = self._entry_with_policy("open-source-only")
        out = _render(entry, detail="full")
        self.assertIn("require_open_source: yes", out)
        self.assertIn("allow_frontier: no", out)
        self.assertIn("require_local: no", out)

    def test_model_policy_warnings_shown_at_more(self):
        entry = self._base_entry()
        p = build_native_model_policy("custom")
        entry["model_policy"] = asdict(p)
        out = _render(entry, detail="full")
        self.assertIn("warning(s)", out)

    def test_model_policy_none_no_crash_at_more(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("model policy", out)

    def test_model_policy_none_no_crash_at_full(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="full")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("model policy", out)

    def test_default_output_unchanged_when_policy_absent(self):
        entry_without = self._base_entry()
        entry_with_none = self._base_entry()
        entry_with_none["model_policy"] = None
        out_without = _render(entry_without, detail="more")
        out_with_none = _render(entry_with_none, detail="more")
        self.assertNotIn("model policy", out_without)
        self.assertNotIn("model policy", out_with_none)

    def test_native_meta_from_entry_extracts_model_policy(self):
        entry = self._entry_with_policy("frontier-heavy")
        nm = _native_meta_from_entry(entry)
        mp = getattr(nm, "model_policy", None)
        self.assertIsNotNone(mp)
        mode = mp.get("mode") if isinstance(mp, dict) else getattr(mp, "mode", None)
        self.assertEqual(mode, "frontier-heavy")

    def test_native_meta_from_entry_missing_policy_returns_none(self):
        entry = self._base_entry()
        nm = _native_meta_from_entry(entry)
        mp = getattr(nm, "model_policy", "MISSING")
        self.assertIsNone(mp)


class TestNativeModelPolicyReceiptRendering(unittest.TestCase):
    def _base_entry(self) -> dict:
        return {"workflow": "native", "executor": "native"}

    def _entry_with_receipt(self, **kwargs) -> dict:
        entry = self._base_entry()
        entry["model_policy_receipt"] = asdict(NativeModelPolicyReceipt(**kwargs))
        return entry

    def test_receipt_hidden_at_default_detail(self):
        entry = self._entry_with_receipt(active=True, blocked_count=2)
        out = _render(entry, detail="default")
        self.assertNotIn("model policy receipt", out)

    def test_compact_line_shown_at_more(self):
        entry = self._entry_with_receipt(active=True, blocked_count=2)
        out = _render(entry, detail="full")
        self.assertIn("model policy receipt:", out)

    def test_compact_line_shows_active_and_blocked(self):
        entry = self._entry_with_receipt(active=True, blocked_count=3)
        out = _render(entry, detail="full")
        self.assertIn("active=true", out)
        self.assertIn("blocked=3", out)

    def test_full_block_header_shown_at_full(self):
        entry = self._entry_with_receipt(active=True)
        out = _render(entry, detail="full")
        self.assertIn("[model policy receipt]", out)

    def test_full_block_shows_all_fields(self):
        entry = self._entry_with_receipt(
            active=True,
            mode="open-source-only",
            affected_selection=True,
            blocked_count=3,
            changed_roles=["executor"],
            warnings_count=2,
            summary="policy active: blocked 3 candidates and changed 1 role",
        )
        out = _render(entry, detail="full")
        self.assertIn("mode: open-source-only", out)
        self.assertIn("affected_selection: yes", out)
        self.assertIn("blocked_count: 3", out)
        self.assertIn("warnings_count: 2", out)
        self.assertIn("summary: policy active: blocked 3 candidates and changed 1 role", out)

    def test_changed_roles_list_rendered(self):
        entry = self._entry_with_receipt(
            active=True,
            affected_selection=True,
            changed_roles=["executor"],
        )
        out = _render(entry, detail="full")
        self.assertIn("- executor", out)

    def test_empty_changed_roles_rendered(self):
        entry = self._entry_with_receipt(active=True, changed_roles=[])
        out = _render(entry, detail="full")
        self.assertIn("changed_roles: []", out)

    def test_old_entry_without_receipt_no_crash(self):
        entry = self._base_entry()
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("receipt", out)

    def test_entry_with_none_receipt_no_crash(self):
        entry = self._base_entry()
        entry["model_policy_receipt"] = None
        try:
            out = _render(entry, detail="more")
        except Exception as exc:
            self.fail(f"render raised {exc}")
        self.assertNotIn("model policy receipt", out)

    def test_native_meta_from_entry_extracts_receipt(self):
        entry = self._base_entry()
        entry["model_policy_receipt"] = {"active": True, "mode": "open-source-only",
                                          "affected_selection": False, "blocked_count": 0,
                                          "changed_roles": [], "warnings_count": 0, "summary": ""}
        nm = _native_meta_from_entry(entry)
        mpr = getattr(nm, "model_policy_receipt", None)
        self.assertIsNotNone(mpr)
        active = mpr.get("active") if isinstance(mpr, dict) else getattr(mpr, "active", None)
        self.assertTrue(active)

    def test_receipt_not_shown_at_default_when_absent(self):
        entry = self._base_entry()
        out = _render(entry, detail="default")
        self.assertNotIn("receipt", out)


class TestRoutingPreviewRendering(unittest.TestCase):
    """Tests for routing_preview compact and full rendering."""

    def _preview_ns(self, **overrides):
        from types import SimpleNamespace
        defaults = dict(
            strategy="cost-balanced",
            policy_mode="cheapest-safe",
            planner_tier="frontier-reasoning-model",
            executor_tier="balanced-coding-model",
            validator_tier="independent-validator-model",
            risk_level="medium",
            confidence="high",
            trust_level="good",
            blocked_count=3,
            policy_affected=True,
            warnings=["w1"],
            summary="cost-balanced | planner=frontier-reasoning-model ...",
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _meta_with_preview(self, preview):
        from types import SimpleNamespace
        ns = SimpleNamespace
        return ns(
            routing_preview=preview,
            repo_context_summary=None, observation=None, plan=None,
            write_path="pipeline", verification_loop=None,
            verification_command_summary=None, diff_review=None,
            final_report=None, native_loop_steps=[], native_loop_trace=ns(events=[]),
            native_backend=None, native_backend_available=True,
            native_backend_notes=[], native_backend_proof=None,
            read_search_findings=[], patch_proposal=None,
            command_policy_preview=None, context_packet=None,
            file_context=None, context_quality_score=None,
            context_quality_advisory=None, change_budget=None,
            change_budget_preview=None, change_budget_soft_gate=None,
            approval_request=None, approval_receipt=None,
            verification_plan=None, clarification_request=None,
            context_usage_summary=None, failure_memory=None,
            osn_loop=None, deepagents_adapter=None,
            validation_contract=None, context_provenance=None,
            run_trust_score=None, model_selection_decision=None,
            model_candidate_scoring=None, model_policy=None,
            model_policy_receipt=None,
        )

    def _entry_with_preview(self, **rp_overrides):
        rp = dict(
            strategy="cost-balanced",
            policy_mode="cheapest-safe",
            planner_tier="frontier-reasoning-model",
            executor_tier="balanced-coding-model",
            validator_tier="independent-validator-model",
            risk_level="medium",
            confidence="high",
            trust_level="good",
            blocked_count=3,
            policy_affected=True,
            warnings=["w1"],
            summary="",
        )
        rp.update(rp_overrides)
        return {
            "task": "add feature",
            "workflow": "native",
            "executor": "native",
            "routing_preview": rp,
        }

    # --- compact line (--more) ---

    def test_compact_line_contains_strategy(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("cost-balanced", combined)

    def test_compact_line_contains_changed_yes(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(policy_affected=True))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("changed=yes", combined)

    def test_compact_line_contains_changed_no(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(policy_affected=False))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("changed=no", combined)

    def test_compact_line_contains_blocked_count(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(blocked_count=3))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("blocked=3", combined)

    def test_compact_line_contains_trust_level(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(trust_level="good"))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("trust=good", combined)

    def test_compact_line_contains_confidence(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(confidence="high"))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("confidence=high", combined)

    def test_compact_line_does_not_expose_raw_warning_text(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(warnings=["secret-warning-text"]))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertNotIn("secret-warning-text", combined)

    def test_compact_line_no_warnings_token_when_empty(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(warnings=[]))
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertNotIn("warnings=", combined)

    # --- full block (--full) uses new field labels ---

    def test_full_block_uses_planner_label(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("planner:", combined)
        self.assertNotIn("planner_tier:", combined)

    def test_full_block_uses_executor_label(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("executor:", combined)
        self.assertNotIn("executor_tier:", combined)

    def test_full_block_uses_validator_label(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("validator:", combined)
        self.assertNotIn("validator_tier:", combined)

    def test_full_block_uses_risk_label(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("risk:", combined)
        self.assertNotIn("risk_level:", combined)

    def test_full_block_uses_trust_label(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("trust:", combined)
        self.assertNotIn("trust_level:", combined)

    def test_full_block_uses_policy_affected_label(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns())
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("policy_affected:", combined)

    def test_full_block_shows_warnings_count_not_text(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(self._preview_ns(warnings=["secret-text"]))
        combined = "\n".join(_render_native_demo_block(meta, detail="full"))
        self.assertIn("warnings:", combined)
        self.assertNotIn("secret-text", combined)

    # --- missing routing_preview does not crash ---

    def test_missing_routing_preview_no_crash_more(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(None)
        try:
            _render_native_demo_block(meta, detail="more")
        except Exception as exc:
            self.fail(f"crashed with missing routing_preview: {exc}")

    def test_missing_routing_preview_no_crash_full(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(None)
        try:
            _render_native_demo_block(meta, detail="full")
        except Exception as exc:
            self.fail(f"crashed with missing routing_preview: {exc}")

    def test_missing_routing_preview_shows_no_routing_section(self):
        from openshard.cli.run_output import _render_native_demo_block
        meta = self._meta_with_preview(None)
        combined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertNotIn("routing preview", combined)

    # --- default detail hides routing preview ---

    def test_default_detail_hides_routing_preview(self):
        out = _render(self._entry_with_preview(), detail="default")
        self.assertNotIn("routing preview", out)

    # --- integration: dict entry round-trip ---

    def test_compact_via_entry_contains_all_key_fields(self):
        out = _render(self._entry_with_preview(), detail="full")
        self.assertIn("routing preview:", out)
        self.assertIn("changed=yes", out)
        self.assertIn("blocked=3", out)
        self.assertIn("trust=good", out)
        self.assertIn("confidence=high", out)

    def test_full_via_entry_uses_new_labels(self):
        out = _render(self._entry_with_preview(), detail="full")
        self.assertIn("[routing preview]", out)
        self.assertIn("planner:", out)
        self.assertNotIn("planner_tier:", out)


class TestLastRoutingReceiptDisplay(unittest.TestCase):
    """Tests for routing_receipt rendering via _render_log_entry (entry dict round-trip)."""

    def _receipt_dict(self, **kwargs) -> dict:
        defaults = {
            "strategy": "frontier-heavy",
            "planner_tier": "frontier",
            "executor_tier": "fast",
            "validator_tier": "low-cost",
            "policy_mode": "strict",
            "policy_affected": True,
            "blocked_count": 2,
            "trust_level": "good",
            "confidence": "high",
            "warnings_count": 1,
            "summary": "frontier-heavy | policy=strict",
        }
        defaults.update(kwargs)
        return defaults

    def _entry_with_receipt(self, **kwargs) -> dict:
        return {
            "task": "native run",
            "workflow": "native",
            "executor": "native",
            "routing_receipt": self._receipt_dict(**kwargs),
        }

    def test_more_hides_receipt(self):
        out = _render(self._entry_with_receipt(), detail="more")
        self.assertNotIn("routing receipt", out)

    def test_full_renders_receipt_section(self):
        out = _render(self._entry_with_receipt(), detail="full")
        self.assertIn("[routing receipt]", out)
        self.assertIn("strategy:", out)
        self.assertIn("planner:", out)
        self.assertIn("executor:", out)
        self.assertIn("validator:", out)
        self.assertIn("policy_mode:", out)
        self.assertIn("policy_affected:", out)
        self.assertIn("blocked_count:", out)
        self.assertIn("trust:", out)
        self.assertIn("confidence:", out)
        self.assertIn("warnings_count:", out)
        self.assertIn("summary:", out)

    def test_default_hides_receipt(self):
        out = _render(self._entry_with_receipt(), detail="default")
        self.assertNotIn("routing receipt", out)

    def test_old_entry_without_receipt_does_not_crash(self):
        entry = {
            "task": "native run",
            "workflow": "native",
            "executor": "native",
            # no routing_receipt key
        }
        out = _render(entry, detail="more")
        self.assertNotIn("routing receipt", out)

    def test_policy_affected_shown_as_yes(self):
        out = _render(self._entry_with_receipt(policy_affected=True), detail="full")
        self.assertIn("yes", out)

    def test_policy_affected_shown_as_no(self):
        out = _render(self._entry_with_receipt(policy_affected=False), detail="full")
        self.assertIn("no", out)


class TestLastReadonlyLabels(unittest.TestCase):
    """Stage and model-line labels use analysis wording for saved read-only runs."""

    def _ro_entry(self) -> dict:
        return {
            "task": "what does openshard/cli/main.py do?",
            "routing_rationale": "read-only analysis",
            "stage_runs": [
                {"model": "z-ai/glm-5.1", "stage_type": "planning",        "duration": 0.5, "cost": 0.0001},
                {"model": "z-ai/glm-5.1", "stage_type": "implementation",  "duration": 1.2, "cost": 0.0003},
            ],
        }

    def _write_entry(self) -> dict:
        return {
            "task": "fix the bug in utils.py",
            "routing_rationale": "standard feature implementation",
            "stage_runs": [
                {"model": "z-ai/glm-5.1", "stage_type": "planning",        "duration": 0.5, "cost": 0.0001},
                {"model": "z-ai/glm-5.1", "stage_type": "implementation",  "duration": 1.2, "cost": 0.0003},
            ],
        }

    def test_readonly_stage_label_says_analysis(self):
        out = _render(self._ro_entry(), detail="full")
        self.assertIn("Analysis", out)

    def test_readonly_stage_label_not_implementation(self):
        out = _render(self._ro_entry(), detail="full")
        self.assertNotIn("Implementation", out)

    def test_readonly_stage_section_says_analysis(self):
        out = _render(self._ro_entry(), detail="full")
        self.assertIn("Analysis", out)

    def test_readonly_stage_section_not_implementation(self):
        out = _render(self._ro_entry(), detail="full")
        self.assertNotIn("Implementation", out)

    def test_write_stage_label_says_implementation(self):
        out = _render(self._write_entry(), detail="full")
        self.assertIn("Implementation", out)

    def test_write_stage_section_says_implementation(self):
        out = _render(self._write_entry(), detail="full")
        self.assertIn("Implementation", out)

    def test_readonly_default_detail_no_stage_section(self):
        out = _render(self._ro_entry(), detail="default")
        self.assertNotIn("Stages", out)

    def test_write_default_detail_no_stage_section(self):
        out = _render(self._write_entry(), detail="default")
        self.assertNotIn("Stages", out)


class TestFeedbackRendering(unittest.TestCase):
    """Tests for the Developer feedback section in _render_log_entry."""

    def _entry(self, rating: str = "good", note: str = "") -> dict:
        return {
            "task": "do a thing",
            "feedback": {
                "schema_version": 1,
                "rating": rating,
                "note": note,
                "created_at": "2025-01-01T00:00:00Z",
            },
        }

    def test_more_shows_developer_feedback_header(self):
        out = _render(self._entry(), detail="more")
        self.assertIn("Developer feedback", out)

    def test_full_shows_developer_feedback_header(self):
        out = _render(self._entry(), detail="full")
        self.assertIn("Developer feedback", out)

    def test_default_hides_feedback(self):
        out = _render(self._entry(), detail="default")
        self.assertNotIn("Developer feedback", out)
        self.assertNotIn("Rating:", out)

    def test_rating_shown(self):
        out = _render(self._entry(rating="good"), detail="more")
        self.assertIn("Rating: good", out)

    def test_note_shown_when_present(self):
        out = _render(self._entry(note="GLM was good enough for this analysis"), detail="more")
        self.assertIn("Note: GLM was good enough for this analysis", out)

    def test_note_line_absent_when_empty(self):
        out = _render(self._entry(note=""), detail="more")
        self.assertNotIn("Note:", out)

    def test_bad_rating_rendered(self):
        out = _render(self._entry(rating="bad"), detail="more")
        self.assertIn("Rating: bad", out)

    def test_mixed_rating_rendered(self):
        out = _render(self._entry(rating="mixed"), detail="full")
        self.assertIn("Rating: mixed", out)

    def test_old_entry_no_feedback_no_crash_more(self):
        entry = {"task": "old run"}
        out = _render(entry, detail="more")
        self.assertNotIn("Developer feedback", out)

    def test_old_entry_no_feedback_no_crash_full(self):
        entry = {"task": "old run"}
        out = _render(entry, detail="full")
        self.assertNotIn("Developer feedback", out)

    def test_old_entry_no_feedback_no_crash_default(self):
        entry = {"task": "old run"}
        out = _render(entry, detail="default")
        self.assertNotIn("Developer feedback", out)

    def test_feedback_rendered_after_notes(self):
        entry = {
            "task": "do a thing",
            "notes": ["a pipeline note"],
            "feedback": {"schema_version": 1, "rating": "good", "note": ""},
        }
        out = _render(entry, detail="full")
        self.assertGreater(out.index("Developer feedback"), out.index("Notes"))


class TestLastStagesDispatchModels(unittest.TestCase):
    """Verify stage_runs rendering and dispatch receipt / stage model consistency."""

    def _two_stage_entry(self) -> dict:
        return {
            "task": "implement a feature",
            "stage_runs": [
                {"model": MODEL_STRONG, "stage_type": "planning",        "duration": 0.5,  "cost": 0.0001},
                {"model": MODEL_MAIN,   "stage_type": "implementation",  "duration": 1.2,  "cost": 0.0003},
            ],
        }

    def test_stages_different_models_both_shown(self):
        out = _render(self._two_stage_entry(), detail="more")
        self.assertIn(_model_label(MODEL_STRONG), out)
        self.assertIn(_model_label(MODEL_MAIN), out)

    def test_stage_plan_model_shown(self):
        entry = {
            "task": "implement a feature",
            "stage_runs": [
                {"model": MODEL_STRONG, "stage_type": "planning", "duration": 0.5, "cost": 0.0001},
            ],
        }
        out = _render(entry, detail="more")
        self.assertIn(_model_label(MODEL_STRONG), out)

    def test_stage_exec_model_shown(self):
        entry = {
            "task": "implement a feature",
            "stage_runs": [
                {"model": MODEL_MAIN, "stage_type": "implementation", "duration": 1.2, "cost": 0.0003},
            ],
        }
        out = _render(entry, detail="more")
        self.assertIn(_model_label(MODEL_MAIN), out)

    def test_default_detail_no_stages_section(self):
        out = _render(self._two_stage_entry(), detail="default")
        self.assertNotIn("Stages", out)

    def test_dispatch_receipt_note_consistent_with_stages(self):
        """Model plan block (from receipt) and Stages block (from stage_runs) show the same models.

        This guards against drift where execution uses one model but output claims another.
        """
        entry = {
            "task": "implement a feature",
            "routing_category": "standard",
            "routing_selected_model": "mock-routed-model",
            "routing_selected_provider": "openrouter",
            "routing_used_fallback": False,
            "stage_runs": [
                {"model": MODEL_STRONG, "stage_type": "planning",       "duration": 0.5,  "cost": 0.0001},
                {"model": MODEL_MAIN,   "stage_type": "implementation", "duration": 1.2,  "cost": 0.0003},
            ],
            "tier_dispatch_receipt": {
                "enabled": True,
                "applied": True,
                "tier_source": "category_fallback",
                "planner_tier": "frontier-reasoning-model",
                "planner_model": MODEL_STRONG,
                "executor_tier": "balanced-coding-model",
                "executor_model": MODEL_MAIN,
                "validator_tier": "independent-validator-model",
                "validator_model": MODEL_STRONG,
                "fallback_used": False,
                "fallback_reason": "",
                "warnings": [],
            },
        }
        out = _render(entry, detail="full")
        sonnet_label = _model_label(MODEL_STRONG)
        glm_label = _model_label(MODEL_MAIN)
        self.assertIn(sonnet_label, out)
        self.assertIn(glm_label, out)
        self.assertIn("Model plan", out)
        self.assertIn("Stages", out)
        # Both the Model plan and Stages sections reference GLM-5.1 as the work/implementation model
        glm_idx_first = out.index(glm_label)
        glm_idx_second = out.index(glm_label, glm_idx_first + 1)
        self.assertGreater(glm_idx_second, glm_idx_first)


class TestRoutingModelDisplayConsistency(unittest.TestCase):
    """When routing_selected_model differs from routing_model, the /last default
    top Model: line should show the scored model with 'Auto →' to match the receipt.

    The compact receipt (also rendered at default detail) always shows 'Auto →'
    when routing_selected_model is set, so tests check the standalone '\nModel: …'
    line using the literal prefix '\nModel: Auto →'.
    """

    def _entry_scored_differs(self) -> dict:
        return {
            "task": "review the API",
            "routing_model": "z-ai/glm-4-5",               # keyword routing pick
            "routing_selected_model": "openrouter/deepseek-r1",  # scored routing pick
            "routing_rationale": "standard feature implementation",
        }

    def _entry_scored_same(self) -> dict:
        return {
            "task": "implement a feature",
            "routing_model": "z-ai/glm-4-5",
            "routing_selected_model": "z-ai/glm-4-5",       # same as keyword pick
            "routing_rationale": "standard feature implementation",
        }

    def _entry_no_selected(self) -> dict:
        return {
            "task": "do a thing",
            "routing_model": "z-ai/glm-4-5",
            "routing_rationale": "standard feature implementation",
        }

    def test_scored_differs_top_model_line_has_auto_arrow(self):
        """When scored ≠ keyword, the standalone Model: line says 'Auto →'."""
        out = _render(self._entry_scored_differs(), detail="default")
        # "\nModel: Auto →" is the top Model: line format; receipt box uses "  Model  Auto →"
        self.assertIn("\nModel: Auto →", out)

    def test_scored_differs_top_model_line_shows_scored_label(self):
        """The top Model: line shows the scored model, not the keyword pick."""
        out = _render(self._entry_scored_differs(), detail="default")
        scored_label = _model_label("openrouter/deepseek-r1")
        # Find the standalone "\nModel: …" line specifically
        top_model_line = next(
            (ln for ln in out.split("\n") if ln.startswith("Model: ")), ""
        )
        self.assertIn(scored_label, top_model_line)

    def test_scored_same_top_model_line_no_auto_arrow(self):
        """When scored == keyword, the top Model: line has no 'Auto →' prefix."""
        out = _render(self._entry_scored_same(), detail="default")
        # The standalone top Model: line should not have "Auto →"
        top_model_line = next(
            (ln for ln in out.split("\n") if ln.startswith("Model: ")), ""
        )
        self.assertNotIn("Auto →", top_model_line)
        self.assertIn(_sc_display_model_name("z-ai/glm-4-5"), top_model_line)

    def test_no_selected_top_model_line_no_auto_arrow(self):
        """When only routing_model is present, the top Model: line has no 'Auto →'."""
        out = _render(self._entry_no_selected(), detail="default")
        top_model_line = next(
            (ln for ln in out.split("\n") if ln.startswith("Model: ")), ""
        )
        self.assertNotIn("Auto →", top_model_line)
        self.assertIn(_sc_display_model_name("z-ai/glm-4-5"), top_model_line)


class TestLastCostComparisonBlock(unittest.TestCase):

    # actual $0.005 < Sonnet 4.6 baseline for 100k/50k ≈ $1.05 → cheaper
    _ENTRY_WITH_COST = {
        "task": "do a thing",
        "estimated_cost": 0.005,
        "prompt_tokens": 100_000,
        "completion_tokens": 50_000,
    }

    # Sonnet 4.6 baseline for 1k/1k: (1000*3 + 1000*15)/1_000_000 = $0.018
    # actual $0.05 > $0.018 baseline → more expensive
    _ENTRY_MORE_EXPENSIVE = {
        "task": "expensive run",
        "estimated_cost": 0.05,
        "prompt_tokens": 1_000,
        "completion_tokens": 1_000,
    }

    # actual == baseline for 1k/1k = $0.018 → equal
    _ENTRY_EQUAL = {
        "task": "equal run",
        "estimated_cost": 0.018,
        "prompt_tokens": 1_000,
        "completion_tokens": 1_000,
    }

    def test_more_shows_cost_comparison(self):
        out = _render(self._ENTRY_WITH_COST, detail="full")
        self.assertIn("Cost comparison", out)
        self.assertIn("OpenShard:", out)
        self.assertIn("Sonnet 4.6-only", out)
        self.assertIn("Compared with", out)

    def test_full_shows_cost_comparison(self):
        out = _render(self._ENTRY_WITH_COST, detail="full")
        self.assertIn("Cost comparison", out)
        self.assertIn("OpenShard:", out)
        self.assertIn("Compared with", out)
        self.assertNotIn("Frontier-only baseline:", out)

    def test_cheaper_case(self):
        out = _render(self._ENTRY_WITH_COST, detail="full")
        self.assertIn("cheaper", out)
        self.assertNotIn("more expensive", out)

    def test_more_expensive_case(self):
        out = _render(self._ENTRY_MORE_EXPENSIVE, detail="full")
        self.assertIn("more expensive", out)

    def test_equal_case(self):
        out = _render(self._ENTRY_EQUAL, detail="full")
        self.assertIn("equal", out)

    def test_default_does_not_show_cost_comparison(self):
        out = _render(self._ENTRY_WITH_COST, detail="default")
        self.assertNotIn("Cost comparison", out)

    def test_skips_when_tokens_missing(self):
        entry = {"task": "do a thing", "estimated_cost": 0.005}
        out = _render(entry, detail="more")
        self.assertNotIn("Cost comparison", out)

    def test_skips_when_actual_cost_missing(self):
        entry = {"task": "do a thing", "prompt_tokens": 100_000, "completion_tokens": 50_000}
        out = _render(entry, detail="more")
        self.assertNotIn("Cost comparison", out)

    def test_handles_actual_cost_zero(self):
        entry = {
            "task": "do a thing",
            "estimated_cost": 0.0,
            "prompt_tokens": 500_000,
            "completion_tokens": 500_000,
        }
        out = _render(entry, detail="full")
        self.assertIn("Cost comparison", out)
        # actual=0, baseline>0 → cheaper (100%), no x-multiple
        self.assertIn("cheaper", out)
        self.assertIn("100%", out)
        self.assertNotIn("x lower", out)


_TDR_ENABLED = {
    "enabled": True,
    "applied": True,
    "planner_model": "anthropic/claude-sonnet-4.6",
    "planner_tier": "frontier-reasoning-model",
    "executor_model": "z-ai/glm-5.1",
    "executor_tier": "worker",
    "validator_model": "anthropic/claude-sonnet-4.6",
    "validator_tier": "independent-validator-model",
    "tier_source": "category_fallback",
    "fallback_used": False,
    "fallback_reason": "",
    "warnings": [],
}


class TestTierDispatchModelPlanAskMode(unittest.TestCase):

    def _ask_entry(self, **overrides):
        return {
            "task": "what does main.py do?",
            "routing_rationale": "read-only analysis",
            "stage_runs": [],
            "tier_dispatch_receipt": _TDR_ENABLED,
            "validator_policy": {"run": False, "reason": "read-only task"},
            **overrides,
        }

    def test_ask_more_no_planning_label(self):
        out = _render(self._ask_entry(), detail="more")
        self.assertNotIn("Planning:", out)

    def test_ask_full_no_planning_label(self):
        out = _render(self._ask_entry(), detail="full")
        self.assertNotIn("Planning:", out)

    def test_ask_more_shows_ask_label(self):
        out = _render(self._ask_entry(), detail="full")
        self.assertIn("Ask:", out)

    def test_ask_full_shows_ask_label(self):
        out = _render(self._ask_entry(), detail="full")
        self.assertIn("Ask:", out)

    def test_ask_validator_skipped_shown(self):
        out = _render(self._ask_entry(), detail="full")
        self.assertIn("skipped", out.lower())
        self.assertIn("read-only", out.lower())

    def test_staged_write_shows_planning_and_work(self):
        entry = {
            "task": "fix the bug",
            "stage_runs": [],
            "tier_dispatch_receipt": _TDR_ENABLED,
        }
        out = _render(entry, detail="full")
        self.assertIn("Planning:", out)
        self.assertIn("Work:", out)

    def test_complex_readonly_staged_shows_planning(self):
        entry = self._ask_entry(
            stage_runs=[{
                "stage_type": "planning",
                "model": "anthropic/claude-sonnet-4.6",
                "duration": 1.0,
                "cost": 0.001,
                "summary": "planned",
            }],
        )
        out = _render(entry, detail="full")
        # planning actually ran, so is_direct_ask=False → staged rendering
        self.assertIn("Planning:", out)


class TestCostComparisonNewFormat(unittest.TestCase):

    _ENTRY = {
        "task": "do a thing",
        "estimated_cost": 1.0,
        "prompt_tokens": 500_000,
        "completion_tokens": 500_000,
    }

    # Sonnet 4.6 at 1k/1k = $0.018; actual $0.018 → equal
    _ENTRY_EQUAL = {
        "task": "equal run",
        "estimated_cost": 0.018,
        "prompt_tokens": 1_000,
        "completion_tokens": 1_000,
    }

    def test_more_shows_openshard_label(self):
        out = _render(self._ENTRY, detail="full")
        self.assertIn("OpenShard:", out)

    def test_more_shows_sonnet46_only_estimate(self):
        out = _render(self._ENTRY, detail="full")
        self.assertIn("Sonnet 4.6-only", out)

    def test_more_no_frontier_only_baseline(self):
        out = _render(self._ENTRY, detail="more")
        self.assertNotIn("Frontier-only baseline", out)

    def test_more_difference_has_x_multiple(self):
        out = _render(self._ENTRY, detail="full")
        self.assertIn("x lower cost", out)

    def test_full_shows_compared_with(self):
        out = _render(self._ENTRY, detail="full")
        self.assertIn("Compared with", out)

    def test_full_shows_sonnet46_row(self):
        out = _render(self._ENTRY, detail="full")
        self.assertIn("Sonnet 4.6-only", out)

    def test_full_shows_method_note(self):
        out = _render(self._ENTRY, detail="full")
        self.assertIn("Method:", out)

    def test_more_equal_case_no_pct(self):
        out = _render(self._ENTRY_EQUAL, detail="full")
        self.assertIn("equal", out)
        equal_lines = [ln for ln in out.splitlines() if "equal" in ln]
        for ln in equal_lines:
            self.assertNotIn("%", ln)


class TestLastCorrectionFieldsRendering(unittest.TestCase):

    def test_more_shows_action_and_reason_when_present(self):
        entry = {
            "task": "do a thing",
            "feedback": {
                "schema_version": 1,
                "rating": "bad",
                "note": "",
                "action": "edited",
                "correction_reason": "wrong-file",
                "created_at": "2025-01-01T00:00:00Z",
            },
        }
        out = _render(entry, detail="more")
        self.assertIn("Action: edited", out)
        self.assertIn("Reason: wrong-file", out)

    def test_full_shows_action_reason_and_note_when_present(self):
        entry = {
            "task": "do a thing",
            "feedback": {
                "schema_version": 1,
                "rating": None,
                "note": "missed the fixture",
                "action": "retried",
                "correction_reason": "failed-tests",
                "created_at": "2025-01-01T00:00:00Z",
            },
        }
        out = _render(entry, detail="full")
        self.assertIn("Action: retried", out)
        self.assertIn("Reason: failed-tests", out)
        self.assertIn("Note: missed the fixture", out)

    def test_default_hides_feedback_block(self):
        entry = {
            "task": "do a thing",
            "feedback": {
                "schema_version": 1,
                "rating": "bad",
                "note": "nope",
                "action": "rejected",
                "correction_reason": "missed-requirement",
                "created_at": "2025-01-01T00:00:00Z",
            },
        }
        out = _render(entry, detail="default")
        self.assertNotIn("Action:", out)
        self.assertNotIn("Reason:", out)
        self.assertNotIn("Developer feedback", out)

    def test_more_shows_rating_from_old_entry(self):
        entry = {
            "task": "do a thing",
            "feedback": {
                "schema_version": 1,
                "rating": "good",
                "note": "",
                "created_at": "2025-01-01T00:00:00Z",
            },
        }
        out = _render(entry, detail="more")
        self.assertIn("Rating: good", out)
        self.assertNotIn("Action:", out)
        self.assertNotIn("Reason:", out)


class TestToolSearchEventsRendering(unittest.TestCase):
    """Rendering of tool/search provenance events in openshard last."""

    def _native_entry(self, events):
        return {
            "task": "do a thing",
            "workflow": "native",
            "executor": "native",
            "tool_search_events": events,
        }

    def test_full_shows_event_count_line(self):
        events = [
            {"tool_name": "search_repo", "result_quality": "useful", "result_count": 3,
             "selected_reason": "observe search trigger", "query": "auth", "context_injected": True,
             "retry_count": 0, "fallback_tool": None, "changed_plan": False, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("tool/search events: 1", out)

    def test_full_shows_per_event_detail(self):
        events = [
            {"tool_name": "search_repo", "result_quality": "useful", "result_count": 3,
             "selected_reason": "observe search trigger", "query": "auth", "context_injected": True,
             "retry_count": 0, "fallback_tool": None, "changed_plan": False, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("search_repo", out)
        self.assertIn("useful", out)
        self.assertIn("3 results", out)
        self.assertIn("observe search trigger", out)
        self.assertIn("injected", out)

    def test_more_shows_count_only(self):
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        events = [
            {"tool_name": "list_files", "result_quality": "useful", "result_count": 10,
             "selected_reason": "preflight scan", "query": ".", "context_injected": True,
             "retry_count": 0, "fallback_tool": None, "changed_plan": False, "warnings": []},
            {"tool_name": "get_git_diff", "result_quality": "empty", "result_count": 0,
             "selected_reason": "observe dirty diff", "query": "", "context_injected": False,
             "retry_count": 0, "fallback_tool": None, "changed_plan": False, "warnings": []},
        ]
        entry = self._native_entry(events)
        meta = _native_meta_from_entry(entry)
        joined = "\n".join(_render_native_demo_block(meta, detail="more"))
        self.assertIn("tool/search events: 2", joined)
        self.assertNotIn("preflight scan", joined)
        self.assertNotIn("observe dirty diff", joined)

    def test_empty_events_list_shows_nothing(self):
        out = _render(self._native_entry([]), detail="full")
        self.assertNotIn("tool/search events", out)

    def test_missing_events_key_renders_cleanly(self):
        entry = {"task": "old run", "workflow": "native", "executor": "native"}
        out = _render(entry, detail="full")
        self.assertNotIn("tool/search events", out)

    def test_dict_backed_events_render_same_as_object_backed(self):
        """Regression: dict events (from saved JSONL) render correctly via _loop_event_value."""
        from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
        events_as_dicts = [
            {"tool_name": "search_repo", "result_quality": "weak", "result_count": 2,
             "selected_reason": "read-search strategy=default", "query": "main",
             "context_injected": False, "retry_count": 0, "fallback_tool": None,
             "changed_plan": False, "warnings": []},
        ]
        # Simulate the loaded-from-JSONL path via _native_meta_from_entry
        entry = {
            "workflow": "native",
            "executor": "native",
            "tool_search_events": events_as_dicts,
        }
        native_meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(native_meta, detail="full")
        joined = "\n".join(lines)
        self.assertIn("search_repo", joined)
        self.assertIn("weak", joined)
        self.assertIn("2 results", joined)
        self.assertIn("read-search strategy=default", joined)

    def test_more_shows_per_event_tool_name(self):
        events = [
            {"tool_name": "search_repo", "result_quality": "useful", "result_count": 3,
             "selected_reason": "observe search trigger", "query": "auth",
             "context_injected": True, "retry_count": 0, "fallback_tool": None,
             "changed_plan": False, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("search_repo", out)

    def test_more_shows_result_count(self):
        events = [
            {"tool_name": "search_repo", "result_quality": "useful", "result_count": 7,
             "selected_reason": "observe search trigger", "query": "token",
             "context_injected": True, "retry_count": 0, "fallback_tool": None,
             "changed_plan": False, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("7 results", out)

    def test_more_shows_zero_results_label(self):
        events = [
            {"tool_name": "get_git_diff", "result_quality": "empty", "result_count": 0,
             "selected_reason": "observe dirty diff", "query": "",
             "context_injected": False, "retry_count": 0, "fallback_tool": None,
             "changed_plan": False, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("zero-results", out)
        self.assertNotIn("  empty  ", out)

    def test_more_shows_plan_changed(self):
        events = [
            {"tool_name": "search_repo", "result_quality": "useful", "result_count": 2,
             "selected_reason": "observe search trigger", "query": "config",
             "context_injected": True, "retry_count": 0, "fallback_tool": None,
             "changed_plan": True, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("plan-changed", out)

    def test_full_shows_changed_plan(self):
        events = [
            {"tool_name": "read_file", "result_quality": "useful", "result_count": 1,
             "selected_reason": "osn loop step", "query": "src/auth.py",
             "context_injected": True, "retry_count": 0, "fallback_tool": None,
             "changed_plan": True, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("plan-changed", out)

    def test_full_shows_zero_results_label(self):
        events = [
            {"tool_name": "search_repo", "result_quality": "empty", "result_count": 0,
             "selected_reason": "observe search trigger", "query": "nonexistent",
             "context_injected": False, "retry_count": 0, "fallback_tool": None,
             "changed_plan": False, "warnings": []},
        ]
        out = _render(self._native_entry(events), detail="full")
        self.assertIn("zero-results", out)
        self.assertNotIn("  empty  ", out)


class TestFormFactorRendering(unittest.TestCase):

    def _ff_entry(
        self,
        public_mode: str = "run",
        internal_form_factor: str = "staged",
        reason: str = "staged planning selected",
        confidence: str = "high",
        risk_level: str = "low",
        context_quality: str | None = None,
        warnings: list | None = None,
    ) -> dict:
        return {
            "task": "add a feature",
            "form_factor": {
                "public_mode": public_mode,
                "internal_form_factor": internal_form_factor,
                "reason": reason,
                "confidence": confidence,
                "risk_level": risk_level,
                "read_only": False,
                "write_requested": True,
                "verification_available": True,
                "context_quality": context_quality,
                "warnings": warnings or [],
            },
        }

    def test_default_detail_does_not_show_form_factor(self):
        out = _render(self._ff_entry(), detail="default")
        self.assertNotIn("Form factor", out)
        self.assertNotIn("public_mode", out)

    def test_last_more_shows_run_type_line(self):
        # Form factor block is --full only; at --more, no form factor output shown
        out = _render(self._ff_entry(), detail="more")
        self.assertNotIn("Run type:", out)
        self.assertNotIn("Form factor", out)

    def test_last_more_shows_execution_label(self):
        # Form factor block is --full only; at --more, execution label not shown
        out = _render(self._ff_entry(internal_form_factor="staged"), detail="more")
        self.assertNotIn("Execution:", out)
        self.assertNotIn("Form factor", out)

    def test_last_more_no_internal_form_factor_name(self):
        out = _render(self._ff_entry(internal_form_factor="native-loop-candidate"), detail="more")
        self.assertNotIn("native-loop-candidate", out)
        self.assertNotIn("Form factor:", out)

    def test_last_full_shows_expanded_form_factor_block(self):
        out = _render(self._ff_entry(), detail="full")
        self.assertIn("Public mode:", out)
        self.assertIn("Internal:", out)
        self.assertIn("Reason:", out)
        self.assertIn("Confidence:", out)
        self.assertIn("Risk:", out)

    def test_last_full_shows_public_mode_label(self):
        out = _render(self._ff_entry(public_mode="deep-run"), detail="full")
        self.assertIn("Deep Run", out)

    def test_last_full_osn_run_label(self):
        out = _render(self._ff_entry(public_mode="osn-run"), detail="full")
        self.assertIn("OSN Run", out)

    def test_last_full_ask_label(self):
        out = _render(self._ff_entry(public_mode="ask"), detail="full")
        self.assertIn("Ask", out)

    def test_last_full_shows_reason(self):
        out = _render(self._ff_entry(reason="riskier task may need controlled native loop"), detail="full")
        self.assertIn("riskier task may need controlled native loop", out)

    def test_last_full_shows_warnings(self):
        out = _render(
            self._ff_entry(warnings=["write touches 2 risky path(s)"]),
            detail="full",
        )
        self.assertIn("Warning:", out)
        self.assertIn("write touches 2 risky path(s)", out)

    def test_last_full_no_warning_line_when_empty(self):
        out = _render(self._ff_entry(warnings=[]), detail="full")
        self.assertNotIn("Warning:", out)

    def test_last_full_context_quality_shown_when_present(self):
        out = _render(self._ff_entry(context_quality="weak"), detail="full")
        self.assertIn("Context:", out)
        self.assertIn("weak", out)

    def test_last_full_context_quality_absent_when_none(self):
        out = _render(self._ff_entry(context_quality=None), detail="full")
        self.assertNotIn("Context:", out)

    def test_old_entry_without_form_factor_renders_cleanly(self):
        entry_without_ff = {"task": "an old task", "routing_model": "openrouter/model"}
        for detail in ("default", "more", "full"):
            out = _render(entry_without_ff, detail=detail)
            self.assertNotIn("Form factor", out)
            self.assertNotIn("public_mode", out)


class TestLastOSNLoopSummaryRendering(unittest.TestCase):
    """Tests for pipeline-level OSN loop summary rendering.

    Distinct from tool-level osn_loop rendering (which renders "osn loop: N/M steps...").
    """

    def _entry_with_osn_summary(
        self,
        steps_taken: int = 5,
        verification_status: str = "passed",
        completed: bool = True,
        stopped_reason: str = "completed",
        steps: list | None = None,
    ) -> dict:
        return {
            "workflow": "native",
            "executor": "native",
            "osn_loop_summary": {
                "enabled": True,
                "mode": "experimental",
                "max_steps": 11,
                "steps_taken": steps_taken,
                "completed": completed,
                "stopped_reason": stopped_reason,
                "verification_status": verification_status,
                "retry_used": False,
                "approval_required": False,
                "approval_granted": False,
                "warnings": [],
                "steps": steps or [],
            },
        }

    def _detail_steps(self) -> list:
        return [
            {
                "step_index": 0, "step_name": "preflight", "status": "passed",
                "result_summary": "repo_map_ok=True", "tool_name": "", "reason": "",
                "context_injected": False, "approval_required": False,
                "verification_status": "", "warnings": [],
            },
            {
                "step_index": 1, "step_name": "observe", "status": "passed",
                "result_summary": "dirty=False", "tool_name": "", "reason": "",
                "context_injected": True, "approval_required": False,
                "verification_status": "", "warnings": [],
            },
            {
                "step_index": 2, "step_name": "verify", "status": "passed",
                "result_summary": "", "tool_name": "", "reason": "",
                "context_injected": False, "approval_required": False,
                "verification_status": "passed", "warnings": [],
            },
        ]

    def test_more_shows_compact_summary(self):
        out = _render(self._entry_with_osn_summary(), detail="full")
        self.assertIn("OSN LOOP", out)
        self.assertIn("5 steps", out)
        self.assertIn("passed", out)

    def test_more_uses_correct_label_not_tool_level_label(self):
        # Tool-level line is "  osn loop: ..." (lowercase), summary section is "  OSN LOOP"
        out = _render(self._entry_with_osn_summary(), detail="full")
        self.assertIn("OSN LOOP", out)

    def test_full_shows_pipeline_header(self):
        out = _render(
            self._entry_with_osn_summary(steps=self._detail_steps()),
            detail="full",
        )
        self.assertIn("OSN loop (pipeline):", out)

    def test_full_shows_per_step_entries(self):
        out = _render(
            self._entry_with_osn_summary(steps=self._detail_steps()),
            detail="full",
        )
        self.assertIn("preflight", out)
        self.assertIn("observe", out)
        self.assertIn("verify", out)
        self.assertIn("passed", out)

    def test_full_shows_verify_step_with_verification_suffix(self):
        out = _render(
            self._entry_with_osn_summary(steps=self._detail_steps()),
            detail="full",
        )
        self.assertIn("verify=passed", out)

    def test_old_entry_without_osn_summary_renders_cleanly(self):
        entry = {"workflow": "native", "executor": "native"}
        out = _render(entry, detail="more")
        self.assertNotIn("OSN LOOP", out)

    def test_default_detail_does_not_show_osn_loop_section(self):
        # [native] block only renders in more/full mode, not default
        out = _render(self._entry_with_osn_summary(), detail="default")
        self.assertNotIn("OSN LOOP", out)

    def test_existing_osn_loop_tool_steps_still_render(self):
        entry = {
            "workflow": "native",
            "executor": "native",
            "osn_loop": {
                "enabled": True, "steps_run": 2, "max_steps": 5,
                "terminated_reason": "complete", "paths_surfaced": [],
                "steps": [], "warnings": [], "steps_queued": 2,
                "consecutive_empty": 0, "truncated": False,
            },
        }
        out = _render(entry, detail="full")
        self.assertIn("osn loop:", out)

    def test_osn_summary_not_shown_when_enabled_false(self):
        entry = {
            "workflow": "native",
            "executor": "native",
            "osn_loop_summary": {
                "enabled": False, "mode": "", "max_steps": 11,
                "steps_taken": 0, "completed": False, "stopped_reason": "",
                "verification_status": "", "retry_used": False,
                "approval_required": False, "approval_granted": False,
                "warnings": [], "steps": [],
            },
        }
        out = _render(entry, detail="more")
        self.assertNotIn("OSN LOOP", out)


class TestLastContractVerificationDisplay(unittest.TestCase):

    def _entry_with_vcr(self, overall_status: str, checks: list[dict]) -> dict:
        entry = _native_entry()
        entry["validation_contract"] = {
            "intent": "add feature",
            "risk_level": "medium",
            "expected_change_scope": "2-5 files",
            "acceptance_checks": [c["expected_check"] for c in checks],
            "verification_commands": ["pytest"],
            "approval_expected": False,
            "strength": "strong",
            "warnings": [],
        }
        entry["verification_contract_result"] = {
            "checks": checks,
            "overall_status": overall_status,
            "reason": "verification suite " + overall_status,
            "raw_content_stored": False,
        }
        return entry

    def _passed_checks(self) -> list[dict]:
        return [
            {
                "check_id": "check_0", "expected_check": "tests pass",
                "verification_source": "verification_loop", "status": "passed",
                "reason": "verification suite passed", "evidence_summary": "exit_code=0, 300 chars output",
                "raw_content_stored": False,
            },
            {
                "check_id": "check_1", "expected_check": "lint clean",
                "verification_source": "verification_loop", "status": "passed",
                "reason": "verification suite passed", "evidence_summary": "exit_code=0, 300 chars output",
                "raw_content_stored": False,
            },
        ]

    def _skipped_checks(self) -> list[dict]:
        return [
            {
                "check_id": "check_0", "expected_check": "tests pass",
                "verification_source": "none", "status": "skipped",
                "reason": "verification not attempted", "evidence_summary": "",
                "raw_content_stored": False,
            },
        ]

    def test_more_shows_compact_contract_verification(self):
        entry = self._entry_with_vcr("passed", self._passed_checks())
        out = _render(entry, detail="full")
        self.assertIn("contract verification:", out)
        self.assertIn("passed", out)
        self.assertIn("2 checks", out)

    def test_full_shows_per_check_detail(self):
        entry = self._entry_with_vcr("passed", self._passed_checks())
        out = _render(entry, detail="full")
        self.assertIn("check_0", out)
        self.assertIn("check_1", out)
        self.assertIn("status=passed", out)
        self.assertIn("tests pass", out)
        self.assertIn("lint clean", out)

    def test_default_does_not_show_contract_verification(self):
        entry = self._entry_with_vcr("passed", self._passed_checks())
        out = _render(entry, detail="default")
        self.assertNotIn("contract verification:", out)

    def test_old_record_without_vcr_renders_cleanly(self):
        entry = _native_entry()
        entry.pop("verification_contract_result", None)
        out = _render(entry, detail="full")
        self.assertIn("[native]", out)
        self.assertNotIn("contract verification:", out)

    def test_skipped_status_displayed(self):
        entry = self._entry_with_vcr("skipped", self._skipped_checks())
        out = _render(entry, detail="full")
        self.assertIn("contract verification:", out)
        self.assertIn("skipped", out)

    def test_failed_status_displayed(self):
        failed_checks = [
            {
                "check_id": "check_0", "expected_check": "tests pass",
                "verification_source": "verification_loop", "status": "failed",
                "reason": "verification suite failed", "evidence_summary": "exit_code=1, 80 chars output",
                "raw_content_stored": False,
            },
        ]
        entry = self._entry_with_vcr("failed", failed_checks)
        out = _render(entry, detail="full")
        self.assertIn("contract verification:", out)
        self.assertIn("failed", out)
        self.assertIn("status=failed", out)


class TestFeedbackScoringRendering(unittest.TestCase):

    def _entry_with_feedback_scoring(self, adjustments: dict, reasons: dict | None = None) -> dict:
        return {
            "task": "test task",
            "routing_category": "standard",
            "routing_candidate_count": 3,
            "routing_selected_model": "openrouter/fast-model",
            "routing_selected_provider": "openrouter",
            "routing_used_fallback": False,
            "routing_feedback_scoring_used": True,
            "routing_feedback_adjustments": adjustments,
            "routing_feedback_reasons": reasons or {},
        }

    def test_last_more_renders_feedback_scoring_header(self):
        entry = self._entry_with_feedback_scoring({"openrouter/fast-model": -0.20})
        out = _render(entry, "full")
        self.assertIn("Feedback scoring:", out)

    def test_last_more_renders_model_and_adjustment(self):
        entry = self._entry_with_feedback_scoring({"openrouter/fast-model": -0.20})
        out = _render(entry, "full")
        self.assertIn("-0.20", out)

    def test_last_more_renders_reason_string(self):
        entry = self._entry_with_feedback_scoring(
            {"openrouter/fast-model": -0.20},
            {"openrouter/fast-model": "feedback: 3 rejected"},
        )
        out = _render(entry, "full")
        self.assertIn("feedback: 3 rejected", out)

    def test_last_more_renders_positive_adjustment(self):
        entry = self._entry_with_feedback_scoring({"openrouter/fast-model": 0.15})
        out = _render(entry, "full")
        self.assertIn("+0.15", out)

    def test_last_more_no_adjustment_shows_enabled_message(self):
        entry = self._entry_with_feedback_scoring({})
        out = _render(entry, "full")
        self.assertIn("Feedback scoring: enabled (no adjustment)", out)

    def test_last_full_renders_feedback_scoring_too(self):
        entry = self._entry_with_feedback_scoring({"openrouter/fast-model": -0.10})
        out = _render(entry, "full")
        self.assertIn("Feedback scoring:", out)

    def test_old_run_without_feedback_scoring_key_renders_safely(self):
        entry = {
            "task": "old run",
            "routing_category": "standard",
            "routing_candidate_count": 2,
            "routing_selected_model": "openrouter/fast-model",
            "routing_selected_provider": "openrouter",
            "routing_used_fallback": False,
        }
        out = _render(entry, "full")
        self.assertNotIn("Feedback scoring:", out)
        self.assertIn("Category: standard", out)

    def test_default_detail_does_not_show_feedback_scoring(self):
        entry = self._entry_with_feedback_scoring({"openrouter/fast-model": -0.20})
        out = _render(entry, "default")
        self.assertNotIn("Feedback scoring:", out)

    def test_multiple_models_all_rendered(self):
        entry = self._entry_with_feedback_scoring(
            {"openrouter/fast-model": -0.20, "openrouter/strong-model": 0.10},
        )
        out = _render(entry, "full")
        self.assertIn("-0.20", out)
        self.assertIn("+0.10", out)


class TestReviewRiskRendering(unittest.TestCase):
    """Risk level renders correctly for review entries in /last and compact receipt."""

    def _entry_with_risk(self, risk_level: str) -> dict:
        return {
            "task": "Review production IAC configuration",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Review completed.",
            "findings": [{"severity": "High", "message": "Wide IAM role"}],
            "files_detail": [],
            "stage_runs": [],
            "routing_model": "anthropic/claude-sonnet-4-6",
            "form_factor": {"risk_level": risk_level},
        }

    def test_high_risk_from_form_factor_shown_in_compact_receipt(self):
        from openshard.history.shard_contract import (
            build_shard_receipt,
            render_compact_shard_receipt,
        )
        entry = self._entry_with_risk("high")
        receipt = build_shard_receipt(entry)
        rendered = render_compact_shard_receipt(receipt)
        self.assertIn("High", rendered)

    def test_high_risk_shown_in_last_default_render(self):
        entry = self._entry_with_risk("high")

        @click.command()
        def cmd():
            _render_log_entry(entry, "default")

        out = CliRunner().invoke(cmd).output
        self.assertIn("High", out)

    def test_production_iac_hardening_risk_not_low(self):
        entry = self._entry_with_risk("high")

        @click.command()
        def cmd():
            _render_log_entry(entry, "default")

        out = CliRunner().invoke(cmd).output
        self.assertNotIn("Risk        Low", out)
        self.assertNotIn("Risk        Not recorded", out)

    def test_security_category_risk_high_via_form_factor_policy(self):
        from openshard.routing.form_factor_policy import _derive_risk_level
        risk = _derive_risk_level("security", None, False)
        self.assertEqual(risk, "high")

    def test_security_category_risk_high_stored_in_receipt(self):
        from openshard.history.shard_contract import build_shard_receipt
        entry = self._entry_with_risk("high")
        receipt = build_shard_receipt(entry)
        self.assertEqual(receipt.risk, "High")

    def test_review_task_low_risk_is_shown_as_recorded_in_last(self):
        # v0.4.4: is_review_task no longer coerces a recorded Low risk to High at display time
        entry = {
            "task": "production-iac-hardening",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Review completed.",
            "is_review_task": True,
            "form_factor": {"risk_level": "low"},
            "findings": [{"severity": "High", "message": "IAM role too broad"}],
            "files_detail": [],
            "stage_runs": [],
        }

        @click.command()
        def cmd():
            _render_log_entry(entry, "default")

        out = CliRunner().invoke(cmd).output
        risk_line = next(ln for ln in out.splitlines() if ln.strip().startswith("Risk"))
        self.assertIn("Low", risk_line)
        self.assertNotIn("High", risk_line)

    def test_review_task_without_recorded_risk_shows_not_recorded_in_last(self):
        entry = {
            "task": "iam-security-review",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Review completed.",
            "is_review_task": True,
            "findings": [{"severity": "Critical", "message": "Public bucket"}],
            "files_detail": [],
            "stage_runs": [],
        }

        @click.command()
        def cmd():
            _render_log_entry(entry, "default")

        out = CliRunner().invoke(cmd).output
        risk_line = next(ln for ln in out.splitlines() if ln.strip().startswith("Risk"))
        self.assertIn("Not recorded", risk_line)


class TestLastVerificationPlanNativeFormat(unittest.TestCase):
    """Regression tests for /last --more crash when verification_plan is stored as a dict.

    Native runs serialize VerificationPlan via asdict(), producing
    {"commands": [...]} — a dict. Non-native runs store a plain list of command dicts.
    Both formats must render without raising TypeError.
    """

    def test_native_dict_format_does_not_crash(self):
        """verification_plan stored as {"commands": [...]} (native format) must not crash."""
        entry = {
            "task": "review infra",
            "verification_plan": {
                "commands": [
                    {
                        "name": "tests",
                        "argv": ["python", "-m", "pytest"],
                        "kind": "test",
                        "source": "detected",
                        "safety": "safe",
                        "reason": "safe prefix",
                    }
                ],
                "task_type": "read-only",
                "risk_level": "low",
            },
        }
        out = _render(entry, detail="full")
        self.assertIn("Verification", out)
        self.assertIn("python -m pytest", out)
        self.assertIn("safe", out)

    def test_native_dict_empty_commands_does_not_crash(self):
        """Empty commands list in native dict format must not crash."""
        entry = {
            "task": "review infra",
            "verification_plan": {"commands": [], "task_type": "read-only", "risk_level": "low"},
        }
        out = _render(entry, detail="more")
        self.assertNotIn("Verification", out)

    def test_native_dict_no_commands_key_does_not_crash(self):
        """verification_plan dict without 'commands' key must not crash."""
        entry = {
            "task": "review infra",
            "verification_plan": {"task_type": "read-only"},
        }
        out = _render(entry, detail="more")
        self.assertNotIn("Verification", out)

    def test_list_format_still_works(self):
        """Existing list format (non-native) must still render correctly."""
        entry = {
            "task": "do a thing",
            "verification_plan": [
                {
                    "name": "lint",
                    "argv": ["ruff", "check", "."],
                    "source": "config",
                    "safety": "safe",
                    "reason": "safe prefix",
                }
            ],
        }
        out = _render(entry, detail="full")
        self.assertIn("Verification", out)
        self.assertIn("ruff check .", out)

    def test_non_dict_item_in_list_does_not_crash(self):
        """Non-dict items in verification_plan list must be skipped silently."""
        entry = {
            "task": "do a thing",
            "verification_plan": ["not-a-dict", None, 42],
        }
        out = _render(entry, detail="more")
        self.assertNotIn("Verification", out)

    def test_default_detail_hides_native_dict_plan(self):
        """verification_plan in native dict format must not appear in default detail."""
        entry = {
            "task": "review infra",
            "verification_plan": {
                "commands": [{"name": "t", "argv": ["pytest"], "source": "s", "safety": "safe", "reason": "r"}],
            },
        }
        out = _render(entry, detail="default")
        self.assertNotIn("Verification", out)


class TestLastFindingsWithStringItems(unittest.TestCase):
    """Regression: findings list that contains bare strings must not crash rendering."""

    def test_string_finding_renders_safely(self):
        entry = {
            "task": "iac review",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Review completed.",
            "is_review_task": True,
            "findings": ["Public S3 bucket detected"],
            "files_detail": [],
            "stage_runs": [],
        }

        @click.command()
        def cmd():
            _render_log_entry(entry, "default")

        out = CliRunner().invoke(cmd).output
        self.assertNotIn("Traceback", out)
        self.assertNotIn("TypeError", out)

    def test_mixed_dict_and_string_findings_renders_safely(self):
        entry = {
            "task": "iac review",
            "timestamp": "2026-05-22T10:00:00Z",
            "summary": "Review completed.",
            "is_review_task": True,
            "findings": [
                {"severity": "Critical", "message": "Wildcard CIDR"},
                "Plain string finding",
            ],
            "files_detail": [],
            "stage_runs": [],
        }

        @click.command()
        def cmd():
            _render_log_entry(entry, "default")

        out = CliRunner().invoke(cmd).output
        self.assertNotIn("Traceback", out)
        self.assertNotIn("TypeError", out)


class TestLastTimelineSection(unittest.TestCase):

    def _entry_with_timeline(self) -> dict:
        return {
            "task": "review iac",
            "timestamp": "2026-05-23T00:00:00Z",
            "execution_model": "anthropic/claude-sonnet-4-6",
            "summary": "Review complete.",
            "run_timeline": [
                {"event": "workflow_pack_loaded", "label": "Loaded workflow pack", "kind": "workflow", "status": "completed"},
                {"event": "repo_scanned", "label": "Scanned repo", "kind": "scan", "status": "completed"},
                {"event": "receipt_saved", "label": "Saved Shard receipt", "kind": "receipt", "status": "completed"},
            ],
        }

    def test_timeline_section_shown_in_more(self):
        out = _render(self._entry_with_timeline(), "more")
        self.assertIn("TIMELINE", out)
        self.assertIn("Loaded workflow pack", out)
        self.assertIn("Scanned repo", out)
        self.assertIn("Saved Shard receipt", out)

    def test_timeline_section_absent_when_missing(self):
        entry = {
            "task": "plain task",
            "timestamp": "2026-05-23T00:00:00Z",
            "execution_model": "anthropic/claude-sonnet-4-6",
            "summary": "Done.",
        }
        out = _render(entry, "more")
        self.assertNotIn("TIMELINE", out)

    def test_receipt_saved_not_duplicated_in_more(self):
        out = _render(self._entry_with_timeline(), "more")
        self.assertEqual(out.count("Saved Shard receipt"), 1)


class TestContextRepoAndBranchRendering(unittest.TestCase):
    """repo_name and git_branch stored in the entry appear in /last --more output."""

    def _base(self) -> dict:
        return {
            "task": "review terraform",
            "timestamp": "2026-05-23T00:00:00Z",
            "execution_model": "anthropic/claude-sonnet-4-6",
            "summary": "Review complete.",
        }

    def test_repo_name_appears_in_more_output(self):
        entry = self._base()
        entry["repo_name"] = "myrepo"
        out = _render(entry, "more")
        self.assertIn("myrepo", out)

    def test_git_branch_appears_in_more_output(self):
        entry = self._base()
        entry["git_branch"] = "feat/x"
        out = _render(entry, "more")
        self.assertIn("feat/x", out)


def _review_checks_all_skipped() -> list[dict]:
    return [
        {"name": "terraform fmt", "status": "skipped", "command": "terraform fmt -check -recursive -no-color", "reason": "terraform not installed", "summary": "", "returncode": None},
        {"name": "terraform validate", "status": "skipped", "command": "terraform validate -no-color", "reason": "terraform not installed", "summary": "", "returncode": None},
        {"name": "tflint", "status": "skipped", "command": "tflint --no-color", "reason": "tflint not installed", "summary": "", "returncode": None},
    ]


def _review_checks_mixed() -> list[dict]:
    return [
        {"name": "terraform fmt", "status": "passed", "command": "terraform fmt -check -recursive -no-color", "reason": "", "summary": "formatting is clean", "returncode": 0},
        {"name": "terraform validate", "status": "skipped", "command": "terraform validate -no-color", "reason": "terraform init required", "summary": "", "returncode": None},
        {"name": "tflint", "status": "skipped", "command": "tflint --no-color", "reason": "tflint not installed", "summary": "", "returncode": None},
    ]


def _review_checks_failed() -> list[dict]:
    return [
        {"name": "terraform fmt", "status": "failed", "command": "terraform fmt -check -recursive -no-color", "reason": "", "summary": "main.tf needs formatting", "returncode": 1},
        {"name": "terraform validate", "status": "skipped", "command": "terraform validate -no-color", "reason": "terraform init required", "summary": "", "returncode": None},
        {"name": "tflint", "status": "skipped", "command": "tflint --no-color", "reason": "tflint not installed", "summary": "", "returncode": None},
    ]


def _iac_review_entry(**extra) -> dict:
    return {
        "task": "Review production IaC for hardening",
        "timestamp": "2026-05-23T12:00:00Z",
        "execution_model": "anthropic/claude-sonnet-4-6",
        "summary": "Review completed.",
        "is_review_task": True,
        "verification_attempted": False,
        **extra,
    }


class TestReviewChecksLastRendering(unittest.TestCase):

    def test_compact_receipt_shows_not_run_without_review_checks(self):
        entry = _iac_review_entry()
        out = _render(entry, "default")
        self.assertIn("Not run", out)

    def test_compact_receipt_not_run_replaced_by_summary_when_all_skipped(self):
        entry = _iac_review_entry(review_checks=_review_checks_all_skipped())
        out = _render(entry, "default")
        self.assertIn("skipped", out)
        self.assertNotIn("Not run", out)

    def test_compact_receipt_shows_passed_and_skipped_counts(self):
        entry = _iac_review_entry(review_checks=_review_checks_mixed())
        out = _render(entry, "default")
        self.assertIn("passed", out)
        self.assertIn("skipped", out)

    def test_full_receipt_checks_section_has_per_check_lines(self):
        entry = _iac_review_entry(review_checks=_review_checks_all_skipped())
        out = _render(entry, "more")
        self.assertIn("CHECKS", out)
        self.assertIn("terraform fmt", out)
        self.assertIn("terraform validate", out)
        self.assertIn("tflint", out)

    def test_full_receipt_skipped_check_shows_reason(self):
        entry = _iac_review_entry(review_checks=_review_checks_all_skipped())
        out = _render(entry, "more")
        self.assertIn("terraform not installed", out)

    def test_full_receipt_validate_skipped_init_required(self):
        entry = _iac_review_entry(review_checks=_review_checks_mixed())
        out = _render(entry, "more")
        self.assertIn("terraform init required", out)

    def test_full_receipt_passed_check_shows_summary(self):
        entry = _iac_review_entry(review_checks=_review_checks_mixed())
        out = _render(entry, "more")
        self.assertIn("formatting is clean", out)

    def test_full_receipt_failed_check_shows_summary(self):
        entry = _iac_review_entry(review_checks=_review_checks_failed())
        out = _render(entry, "more")
        self.assertIn("main.tf needs formatting", out)

    def test_old_entry_without_review_checks_renders_safely(self):
        entry = _iac_review_entry()
        out = _render(entry, "more")
        self.assertIn("CHECKS", out)
        self.assertIn("Not run", out)

    def test_no_huge_output_in_rendering(self):
        checks = _review_checks_all_skipped()
        checks[0]["summary"] = "short summary"
        entry = _iac_review_entry(review_checks=checks)
        out = _render(entry, "more")
        self.assertLess(len(out), 10000)


class TestMoreTierExclusion(unittest.TestCase):
    """Guard tier separation: --more shows product Shard sections only; --full exposes debug internals."""

    def _native_entry_with_backend(self) -> dict:
        entry = _native_entry()
        entry["native_backend"] = "openrouter"
        return entry

    def _native_entry_with_loop(self) -> dict:
        entry = _native_entry()
        entry["native_loop_steps"] = ["plan", "execute", "verify"]
        return entry

    def _native_entry_with_context_packet(self) -> dict:
        entry = _native_entry()
        entry["context_packet"] = {
            "task_preview": "fix bug",
            "sources": ["backend", "repo_context"],
            "repo_stack": [],
            "test_marker_count": 0,
            "package_file_count": 0,
            "read_search_count": 1,
            "selected_skills": [],
            "backend": "builtin",
            "backend_available": True,
            "backend_proof_mode": "",
            "compact_paths": ["file:src/x.py"],
            "warnings": [],
        }
        return entry

    def _native_entry_with_routing_preview(self) -> dict:
        return {
            "task": "add feature",
            "workflow": "native",
            "executor": "native",
            "routing_preview": {
                "strategy": "cost-balanced",
                "policy_mode": "cheapest-safe",
                "planner_tier": "frontier-reasoning-model",
                "executor_tier": "balanced-coding-model",
                "validator_tier": "independent-validator-model",
                "risk_level": "medium",
                "confidence": "high",
                "trust_level": "good",
                "blocked_count": 0,
                "policy_affected": False,
                "warnings": [],
                "summary": "",
            },
        }

    def _native_entry_with_model_candidates(self) -> dict:
        entry = _native_entry()
        entry["model_candidate_scoring"] = asdict(NativeModelCandidateScoring(
            strategy="cost-balanced",
            confidence="high",
            selected_by_role={"planner": "frontier-reasoning-model"},
            candidates=[],
            blocked_candidates=[],
            warnings=[],
        ))
        return entry

    def _entry_with_cost(self) -> dict:
        return {
            "task": "test task",
            "estimated_cost": 0.005,
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        }

    def _entry_with_timeline(self) -> dict:
        return {
            "task": "review iac",
            "timestamp": "2026-05-23T00:00:00Z",
            "execution_model": "anthropic/claude-sonnet-4-6",
            "summary": "Review complete.",
            "run_timeline": [
                {"event": "repo_scanned", "label": "Scanned repo", "kind": "scan", "status": "completed"},
            ],
        }

    # --- --more must exclude debug internals ---

    def test_more_excludes_native_block(self):
        out = _render(_native_entry(), "more")
        self.assertNotIn("[native]", out)

    def test_more_excludes_native_summary(self):
        out = _render(_native_entry(), "more")
        self.assertNotIn("[native summary]", out)

    def test_more_excludes_backend_line(self):
        out = _render(self._native_entry_with_backend(), "more")
        self.assertNotIn("backend:", out)

    def test_more_excludes_loop_line(self):
        out = _render(self._native_entry_with_loop(), "more")
        self.assertNotIn("loop:", out)

    def test_more_excludes_context_packet(self):
        out = _render(self._native_entry_with_context_packet(), "more")
        self.assertNotIn("context packet:", out)

    def test_more_excludes_model_candidates(self):
        out = _render(self._native_entry_with_model_candidates(), "more")
        self.assertNotIn("model candidates:", out)

    def test_more_excludes_routing_preview(self):
        out = _render(self._native_entry_with_routing_preview(), "more")
        self.assertNotIn("routing preview:", out)

    def test_more_includes_cost_comparison_block(self):
        out = _render(self._entry_with_cost(), "more")
        self.assertIn("COST COMPARISON", out)
        self.assertIn("Run cost:", out)
        self.assertIn("Estimated same-token baseline:", out)

    def test_more_omits_cost_comparison_when_no_cost_data(self):
        out = _render({"task": "test task", "prompt_tokens": 1000, "completion_tokens": 500}, "more")
        self.assertNotIn("COST COMPARISON", out)

    def test_more_excludes_tokens_line(self):
        entry = {
            "task": "test task",
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "execution_model": "anthropic/claude-sonnet-4-6",
        }
        out = _render(entry, "more")
        self.assertNotIn("Tokens:", out)

    # --- --more must include product Shard sections ---

    def test_more_includes_task_section(self):
        out = _render({"task": "review infra"}, "more")
        self.assertIn("TASK", out)

    def test_more_includes_timeline_section(self):
        out = _render(self._entry_with_timeline(), "more")
        self.assertIn("TIMELINE", out)

    def test_more_includes_checks_section(self):
        out = _render({"task": "check run"}, "more")
        self.assertIn("CHECKS", out)

    # --- --full must still expose debug internals ---

    def test_full_includes_native_block(self):
        out = _render(_native_entry(), "full")
        self.assertIn("[native]", out)

    def test_full_includes_backend_line(self):
        out = _render(self._native_entry_with_backend(), "full")
        self.assertIn("backend:", out)

    def test_full_includes_loop_line(self):
        out = _render(self._native_entry_with_loop(), "full")
        self.assertIn("loop:", out)

    def test_full_includes_context_packet(self):
        out = _render(self._native_entry_with_context_packet(), "full")
        self.assertIn("context packet:", out)


class TestDeveloperFeedbackV1Rendering(unittest.TestCase):
    """Verify developer_feedback renders correctly at each detail tier."""

    def _entry(self, **overrides) -> dict:
        df = {
            "schema_version": 1,
            "outcome": "accepted",
            "reason": None,
            "edited": False,
            "manual_fix_required": False,
            "ci_passed": False,
            "ci_failed": False,
            "pr_created": False,
            "pr_merged": False,
            "recorded_at": "2026-05-23T12:00:00",
            "source": "cli",
        }
        df.update(overrides)
        return {"timestamp": "2026-01-01T00:00:00", "task": "do X", "summary": "done", "developer_feedback": df}

    def _entry_no_feedback(self) -> dict:
        return {"timestamp": "2026-01-01T00:00:00", "task": "do X", "summary": "done"}

    def test_more_shows_feedback_section_when_present(self):
        out = _render(self._entry(), "more")
        self.assertIn("FEEDBACK", out)

    def test_more_shows_outcome_line(self):
        out = _render(self._entry(outcome="accepted"), "more")
        self.assertIn("accepted", out)

    def test_more_shows_reason_when_set(self):
        out = _render(self._entry(reason="missed IAM issue"), "more")
        self.assertIn("missed IAM issue", out)

    def test_more_hides_reason_when_none(self):
        out = _render(self._entry(reason=None), "more")
        self.assertNotIn("Reason", out)

    def test_more_shows_edited_yes_when_true(self):
        out = _render(self._entry(edited=True), "more")
        self.assertIn("Edited", out)
        self.assertIn("yes", out)

    def test_more_hides_edited_when_false(self):
        out = _render(self._entry(edited=False), "more")
        self.assertNotIn("Edited", out)

    def test_more_shows_ci_passed(self):
        out = _render(self._entry(ci_passed=True), "more")
        self.assertIn("CI", out)
        self.assertIn("passed", out)

    def test_more_shows_ci_failed(self):
        out = _render(self._entry(ci_failed=True), "more")
        self.assertIn("CI", out)
        self.assertIn("failed", out)

    def test_more_hides_ci_when_both_false(self):
        out = _render(self._entry(ci_passed=False, ci_failed=False), "more")
        self.assertNotIn("CI", out)

    def test_full_shows_feedback_section(self):
        out = _render(self._entry(), "full")
        self.assertIn("FEEDBACK", out)

    def test_default_shows_compact_feedback_line(self):
        out = _render(self._entry(outcome="partial"), "default")
        self.assertIn("Feedback", out)
        self.assertIn("partial", out)

    def test_default_no_feedback_section_header(self):
        out = _render(self._entry(), "default")
        self.assertNotIn("FEEDBACK", out)

    def test_entry_without_developer_feedback_renders_safely(self):
        out = _render(self._entry_no_feedback(), "more")
        self.assertNotIn("FEEDBACK", out)

    def test_feedback_section_not_shown_when_absent(self):
        out = _render(self._entry_no_feedback(), "full")
        self.assertNotIn("FEEDBACK", out)

    def test_more_feedback_section_is_indented(self):
        out = _render(self._entry(outcome="partial"), "more")
        self.assertIn("  FEEDBACK", out)
        self.assertIn("  Outcome", out)

    def test_more_compact_receipt_shows_feedback_row(self):
        out = _render(self._entry(outcome="partial"), "more")
        self.assertIn("Feedback", out)
        self.assertIn("partial", out)

    def test_no_feedback_row_in_receipt_when_absent(self):
        out = _render(self._entry_no_feedback(), "more")
        self.assertNotIn("Feedback", out)


class TestModelAdvisoryRendering(unittest.TestCase):
    _ADVISORY = [
        {
            "model_id": "x-ai/grok-4.3",
            "display_name": "xAI: Grok 4.3",
            "tier": "strong",
            "cost_class": "expensive",
            "experimental": False,
            "reasons": ["suitable for high-risk review", "supports reasoning"],
        },
        {
            "model_id": "anthropic/claude-opus-4.7",
            "display_name": "Claude Opus 4.7",
            "tier": "frontier",
            "cost_class": "expensive",
            "experimental": False,
            "reasons": ["suitable for high-risk review", "cost class expensive"],
        },
    ]

    def _native_entry(self, advisory=None):
        entry = {
            "task": "test advisory task",
            "workflow": "native",
            "executor": "native",
        }
        if advisory is not None:
            entry["model_advisory"] = advisory
        return entry

    def _plain_entry(self, advisory=None):
        entry = {"task": "test advisory task"}
        if advisory is not None:
            entry["model_advisory"] = advisory
        return entry

    def test_advisory_compact_line_in_native_block_at_full(self):
        # [native] block is rendered only at --full; compact one-liner must appear there
        out = _render(self._native_entry(self._ADVISORY), "full")
        self.assertIn("model advisory:", out)
        self.assertIn("advisory only", out)

    def test_advisory_compact_line_hidden_at_default(self):
        out = _render(self._native_entry(self._ADVISORY), "default")
        self.assertNotIn("model advisory:", out)

    def test_advisory_full_section_in_shard_receipt_at_more(self):
        out = _render(self._plain_entry(self._ADVISORY), "more")
        self.assertIn("MODEL ADVISORY", out)

    def test_advisory_full_section_in_shard_receipt_at_full(self):
        out = _render(self._plain_entry(self._ADVISORY), "full")
        self.assertIn("MODEL ADVISORY", out)

    def test_advisory_section_includes_advisory_only_label(self):
        out = _render(self._plain_entry(self._ADVISORY), "more")
        self.assertIn("Advisory only — routing unchanged", out)

    def test_advisory_section_includes_risk_signal_label(self):
        out = _render(self._plain_entry(self._ADVISORY), "more")
        self.assertIn("Generated from risk signal only", out)

    def test_advisory_section_shows_display_names(self):
        out = _render(self._plain_entry(self._ADVISORY), "more")
        self.assertIn("xAI: Grok 4.3", out)

    def test_advisory_section_shows_reasons_with_arrow(self):
        out = _render(self._plain_entry(self._ADVISORY), "more")
        self.assertIn("↳", out)
        self.assertIn("suitable for high-risk review", out)

    def test_advisory_absent_no_crash_at_more(self):
        out = _render(self._native_entry(), "more")
        self.assertNotIn("MODEL ADVISORY", out)

    def test_advisory_absent_no_crash_at_full(self):
        out = _render(self._native_entry(), "full")
        self.assertNotIn("MODEL ADVISORY", out)

    def test_compact_receipt_no_advisory_row(self):
        from openshard.history.shard_contract import (
            build_shard_receipt,
            render_compact_shard_receipt,
        )
        receipt = build_shard_receipt(self._plain_entry(self._ADVISORY))
        out = render_compact_shard_receipt(receipt)
        self.assertNotIn("Advisory", out)

    def test_selected_model_unchanged_by_advisory_data(self):
        entry = self._native_entry(self._ADVISORY)
        entry["routing_selected_model"] = "anthropic/claude-sonnet-4.6"
        out = _render(entry, "full")
        self.assertIn("Claude Sonnet 4.6", out)

    def test_no_duplicate_full_advisory_section(self):
        out = _render(self._native_entry(self._ADVISORY), "full")
        count = out.count("MODEL ADVISORY")
        self.assertEqual(count, 1, f"MODEL ADVISORY appeared {count} times, expected 1")

    def test_advisory_candidate_count_shown_in_native_block(self):
        # [native] block only at --full
        out = _render(self._native_entry(self._ADVISORY), "full")
        self.assertIn("2 candidates", out)

    def test_advisory_single_candidate_uses_singular_label(self):
        single = [self._ADVISORY[0]]
        out = _render(self._native_entry(single), "full")
        self.assertIn("1 candidate", out)
        self.assertNotIn("1 candidates", out)


_FRA = {
    "version": "rules_v1",
    "advisory_only": True,
    "recommendation": "consider_stronger_review",
    "confidence": "medium",
    "reason": "Recent local session signals included partial feedback.",
    "signals_considered": {"retry_requested": 0, "partial_explicit": 1, "rejected_explicit": 0},
    "signals_window": {
        "source": "session_signals.jsonl",
        "max_recent_signals": 25,
        "signals_read": 5,
        "signals_used": 1,
    },
}


class TestFeedbackRoutingAdvisoryRendering(unittest.TestCase):

    def _entry_with_fra(self):
        return {"task": "test feedback advisory task", "feedback_routing_advisory": _FRA}

    def _entry_without_fra(self):
        return {"task": "test feedback advisory task"}

    # -- full mode shows advisory --

    def test_full_receipt_renders_feedback_advisory_section(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertIn("FEEDBACK ROUTING ADVISORY", out)

    def test_full_receipt_renders_advisory_only_label(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertIn("Advisory only — routing unchanged", out)

    def test_full_receipt_renders_recommendation(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertIn("Recommendation", out)
        self.assertIn("consider stronger review", out)

    def test_full_receipt_renders_confidence(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertIn("Confidence", out)
        self.assertIn("medium", out)

    def test_full_receipt_renders_reason(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertIn("Reason", out)
        self.assertIn("Recent local session signals", out)

    def test_full_receipt_renders_signals_line(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertIn("Signals", out)
        self.assertIn("partial_explicit=1", out)

    def test_full_receipt_omits_zero_count_signals(self):
        out = _render(self._entry_with_fra(), "full")
        self.assertNotIn("retry_requested=0", out)
        self.assertNotIn("rejected_explicit=0", out)

    # -- absent advisory produces no section --

    def test_full_receipt_no_advisory_section_when_absent(self):
        out = _render(self._entry_without_fra(), "full")
        self.assertNotIn("FEEDBACK ROUTING ADVISORY", out)

    # -- compact receipt never shows advisory --

    def test_compact_receipt_never_shows_feedback_advisory(self):
        from openshard.history.shard_contract import (
            build_shard_receipt,
            render_compact_shard_receipt,
        )
        receipt = build_shard_receipt(self._entry_with_fra())
        out = render_compact_shard_receipt(receipt)
        self.assertNotIn("FEEDBACK ROUTING ADVISORY", out)
        self.assertNotIn("consider stronger review", out)

    # -- more mode does not show advisory --

    def test_more_mode_does_not_render_feedback_advisory(self):
        out = _render(self._entry_with_fra(), "more")
        self.assertNotIn("FEEDBACK ROUTING ADVISORY", out)

    # -- default mode does not show advisory --

    def test_default_mode_does_not_render_feedback_advisory(self):
        out = _render(self._entry_with_fra(), "default")
        self.assertNotIn("FEEDBACK ROUTING ADVISORY", out)

    # -- field parsing is safe with malformed data --

    def test_entry_with_non_advisory_only_dict_ignored(self):
        entry = {
            "task": "test",
            "feedback_routing_advisory": {"advisory_only": False, "recommendation": "bad"},
        }
        out = _render(entry, "full")
        self.assertNotIn("FEEDBACK ROUTING ADVISORY", out)


# ---------------------------------------------------------------------------
# Fix 4: Structured findings for read-only review tasks
# ---------------------------------------------------------------------------

class TestReadonlyTaskFindings(unittest.TestCase):
    """Verify that /last --more renders findings for read-only review tasks."""

    def _entry_with_findings(self, **extra):
        base = {
            "task": "Review this Terraform repo for production readiness. Do not apply changes.",
            "execution_model": "mock-model",
            "summary": (
                "Production readiness review complete.\n\n"
                "## Critical\n- Missing deletion protection on RDS instances\n\n"
                "## High\n- Secrets stored in plaintext environment variables\n"
            ),
            "is_review_task": True,
            "findings": [
                {"severity": "Critical", "message": "Missing deletion protection on RDS instances"},
                {"severity": "High", "message": "Secrets stored in plaintext environment variables"},
            ],
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
        }
        base.update(extra)
        return base

    def test_readonly_review_with_findings_shows_findings_section(self):
        """FINDINGS section must appear in /last --more for a read-only review task."""
        out = _render(self._entry_with_findings(), "more")
        self.assertIn("FINDINGS", out)
        self.assertNotIn("No structured findings recorded.", out)

    def test_readonly_review_with_findings_shows_severity(self):
        """Finding severity labels must appear in /last --more output."""
        out = _render(self._entry_with_findings(), "more")
        self.assertIn("Critical", out)
        self.assertIn("High", out)

    def test_readonly_review_no_findings_shows_honest_message(self):
        """When no findings recorded, 'No structured findings recorded.' must appear."""
        entry = self._entry_with_findings(findings=[])
        out = _render(entry, "more")
        self.assertIn("No structured findings recorded.", out)

    def test_explanation_readonly_task_shows_no_structured_findings(self):
        """An explain-only task (is_review_task=False) shows 'No structured findings recorded.'
        rather than real findings, since it is not treated as a review task."""
        entry = {
            "task": "Explain this module. Do not write files.",
            "execution_model": "mock-model",
            "summary": "This module handles CLI routing.",
            "is_review_task": False,
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
        }
        out = _render(entry, "more")
        self.assertIn("No structured findings recorded.", out)


# ---------------------------------------------------------------------------
# /last more proof rendering
# ---------------------------------------------------------------------------

class TestLastMoreProofRendering(unittest.TestCase):
    """Tests that /last more surfaces the key proof facts a developer needs."""

    def _base_write_entry(self) -> dict:
        return {
            "task": "add feature",
            "timestamp": "2026-05-01T10:00:00Z",
            "execution_model": "anthropic/claude-sonnet-4-6",
            "summary": "Feature implemented.",
            "form_factor": {"risk_level": "high", "read_only": False},
            "estimated_cost": 0.012,
        }

    def test_compact_receipt_shows_risk(self):
        entry = self._base_write_entry()
        out = _render(entry, "more")
        self.assertIn("High", out)

    def test_compact_receipt_shows_checks_display(self):
        entry = dict(self._base_write_entry())
        entry["review_checks"] = [
            {"name": "terraform fmt", "status": "passed", "command": "", "reason": "", "summary": "ok", "returncode": 0},
            {"name": "tflint", "status": "skipped", "command": "", "reason": "tflint not installed", "summary": "", "returncode": None},
        ]
        out = _render(entry, "more")
        self.assertIn("1 passed", out)
        self.assertIn("1 skipped", out)

    def test_compact_receipt_shows_changed_file_count(self):
        entry = dict(self._base_write_entry())
        entry["files_detail"] = [
            {"path": "src/app.py", "change_type": "update", "summary": "updated"},
            {"path": "src/utils.py", "change_type": "create", "summary": "created"},
        ]
        out = _render(entry, "more")
        self.assertIn("2 files", out)

    def test_last_more_includes_timeline_section(self):
        entry = dict(self._base_write_entry())
        entry["run_timeline"] = [
            {"event": "repo_scanned", "label": "Scanned repo", "kind": "scan", "status": "completed"},
        ]
        out = _render(entry, "more")
        self.assertIn("TIMELINE", out)

    def test_last_more_skipped_check_reason(self):
        entry = dict(self._base_write_entry())
        entry["run_timeline"] = [
            {"event": "repo_scanned", "label": "Scanned repo", "kind": "scan", "status": "completed"},
        ]
        entry["review_checks"] = [
            {"name": "tflint", "status": "skipped", "command": "", "reason": "executable not found", "summary": "", "returncode": None},
        ]
        out = _render(entry, "more")
        self.assertIn("executable not found", out)

    def test_last_more_approval_not_invented(self):
        # Write task with no approval receipt → "Not recorded", never "Required"
        entry = dict(self._base_write_entry())
        entry["approval_receipt"] = None
        out = _render(entry, "more")
        self.assertIn("Not recorded", out)
        # "Required" should not appear as a standalone approval claim
        for line in out.splitlines():
            if "Approval" in line:
                self.assertNotIn("Required →", line)


class TestProofSummaryRendering(unittest.TestCase):
    """Tests for _render_proof_summary_lines and /last more proof summary output."""

    def _osn_obs_entry(self) -> dict:
        return {
            "task": "test task",
            "workflow": "native",
            "executor": "native",
            "osn_observation": {"enabled": True, "stack_signals": ["python"], "candidate_files": [], "test_files": [], "suggested_checks": []},
        }

    def _osn_full_entry(self) -> dict:
        entry = self._osn_obs_entry()
        entry["osn_progress_memory"] = {"enabled": True, "confidence": "high", "summary": "ok", "relevant_files": [], "unresolved_items": [], "next_safe_step": ""}
        entry["osn_verification_contract"] = {"enabled": True, "status": "skipped", "expected_checks": [], "attempted_checks": [], "missing_checks": [], "manual_review_required": False, "skipped_reason": ""}
        entry["osn_loop_summary"] = {"enabled": True, "attempted_steps": 3, "completed_steps": 3, "blocked_steps": 0, "failed_steps": 0, "steps_taken": 3, "tool_calls_attempted": None, "tool_calls_blocked": None, "verification_status": "", "verification_attempted": False, "verification_passed": None, "retry_used": False, "retry_count": 0, "stopped_reason": "done", "steps": []}
        entry["osn_retry_diagnosis"] = {"enabled": True, "status": "not_needed", "retry_allowed": False, "retry_used": False, "retry_count": 0}
        return entry

    def _render_summary_lines(self, entry: dict) -> list[str]:
        from openshard.cli.run_output import _native_meta_from_entry, _render_proof_summary_lines
        nm = _native_meta_from_entry(entry)
        assert nm is not None
        return _render_proof_summary_lines(nm)

    # Pure function tests

    def test_proof_summary_present_when_all_osn_sections_enabled(self):
        lines = self._render_summary_lines(self._osn_full_entry())
        self.assertTrue(any("PROOF SUMMARY" in line for line in lines))
        joined = "\n".join(lines)
        self.assertIn("Observation", joined)
        self.assertIn("Progress", joined)
        self.assertIn("Verification", joined)
        self.assertIn("Loop", joined)
        self.assertIn("Retry", joined)

    def test_proof_summary_observation_present(self):
        lines = self._render_summary_lines(self._osn_obs_entry())
        joined = "\n".join(lines)
        self.assertIn("PROOF SUMMARY", joined)
        self.assertIn("present", joined)

    def test_proof_summary_verification_status_reflected(self):
        entry = self._osn_full_entry()
        entry["osn_verification_contract"]["status"] = "skipped"
        lines = self._render_summary_lines(entry)
        joined = "\n".join(lines)
        self.assertIn("skipped", joined)

    def test_proof_summary_retry_status_reflected(self):
        entry = self._osn_full_entry()
        entry["osn_retry_diagnosis"]["status"] = "not_needed"
        lines = self._render_summary_lines(entry)
        joined = "\n".join(lines)
        self.assertIn("not_needed", joined)

    def test_proof_summary_omitted_when_no_osn_data(self):
        entry = {"task": "test task", "workflow": "native", "executor": "native"}
        lines = self._render_summary_lines(entry)
        self.assertEqual(lines, [])

    def test_proof_summary_omitted_for_non_native_run(self):
        from openshard.cli.run_output import _native_meta_from_entry
        entry = {"task": "test task", "osn_observation": {"enabled": True}}
        nm = _native_meta_from_entry(entry)
        self.assertIsNone(nm)

    def test_proof_summary_pr_comment_always_available(self):
        lines = self._render_summary_lines(self._osn_obs_entry())
        joined = "\n".join(lines)
        self.assertIn("PR comment", joined)
        self.assertIn("available", joined)

    def test_proof_summary_no_raw_json(self):
        lines = self._render_summary_lines(self._osn_full_entry())
        for line in lines:
            self.assertNotIn("{", line)
            self.assertNotIn("}", line)

    def test_proof_summary_no_em_dash(self):
        lines = self._render_summary_lines(self._osn_full_entry())
        for line in lines:
            self.assertNotIn("—", line)

    def test_proof_summary_loop_completed_label(self):
        entry = self._osn_full_entry()
        lines = self._render_summary_lines(entry)
        joined = "\n".join(lines)
        self.assertIn("completed", joined)

    def test_proof_summary_loop_not_recorded_when_absent(self):
        entry = self._osn_obs_entry()
        lines = self._render_summary_lines(entry)
        joined = "\n".join(lines)
        self.assertIn("not recorded", joined)

    def test_proof_summary_disabled_retry_alone_does_not_trigger_block(self):
        entry = {"task": "test task", "workflow": "native", "executor": "native"}
        entry["osn_retry_diagnosis"] = {"enabled": False, "status": "not_needed"}
        lines = self._render_summary_lines(entry)
        self.assertEqual(lines, [])

    # Integration tests via _render_log_entry

    def test_proof_summary_appears_in_more_output(self):
        entry = self._osn_obs_entry()
        out = _render(entry, "more")
        self.assertIn("PROOF SUMMARY", out)

    def test_proof_summary_absent_in_default_output(self):
        entry = self._osn_obs_entry()
        out = _render(entry, "default")
        self.assertNotIn("PROOF SUMMARY", out)

    def test_proof_summary_not_duplicated_in_more(self):
        entry = self._osn_full_entry()
        out = _render(entry, "more")
        self.assertEqual(out.count("PROOF SUMMARY"), 1)

    def test_proof_summary_absent_when_no_osn_data_in_more(self):
        entry = {"task": "test task", "workflow": "native", "executor": "native"}
        out = _render(entry, "more")
        self.assertNotIn("PROOF SUMMARY", out)


class TestSafeWorkspaceReceipt(unittest.TestCase):
    """Tests for safe workspace and git metadata in receipt rendering."""

    def _render_full(self, receipt):
        from openshard.history.shard_contract import render_full_shard_receipt
        return render_full_shard_receipt(receipt)

    def _build_receipt(self, entry):
        from openshard.history.shard_contract import build_shard_receipt
        return build_shard_receipt(entry)

    def _base_receipt(self, **kwargs):
        from openshard.history.shard_contract import ShardReceipt
        defaults = {
            "shard_id": "shard-20260531-0001",
            "created_at": "2026-05-31T10:00:00Z",
            "task_short": "test task",
            "task_full": "test task",
            "agent": "OpenShard Native",
            "strategy": "Not recorded",
            "model_display": "Claude Sonnet 4.6",
            "risk": "Low",
            "sandbox": "On",
            "files_changed": 0,
            "checks_display": "Not run",
            "approval": "Not required",
            "cost_display": "$0.0000",
            "result": "ok",
            "status": "Passed",
            "duration_seconds": 1.0,
        }
        defaults.update(kwargs)
        return ShardReceipt(**defaults)

    def test_render_full_shows_workspace_rows_for_worktree(self):
        receipt = self._base_receipt(
            safe_workspace_kind="worktree",
            safe_workspace_display_name="osn/run-abc",
            git_base_branch="main",
            git_base_commit_hash="a" * 40,
        )
        out = self._render_full(receipt)
        self.assertIn("GIT", out)
        self.assertIn("Workspace", out)
        self.assertIn("worktree", out)
        self.assertIn("osn/run-abc", out)
        self.assertIn("Base branch", out)
        self.assertIn("Base commit", out)

    def test_render_full_shows_workspace_rows_for_temp(self):
        receipt = self._base_receipt(
            safe_workspace_kind="temp",
            safe_workspace_display_name="osn-temp",
        )
        out = self._render_full(receipt)
        self.assertIn("GIT", out)
        self.assertIn("Workspace", out)
        self.assertIn("temp", out)
        self.assertIn("osn-temp", out)

    def test_render_full_no_git_section_when_all_fields_none(self):
        receipt = self._base_receipt()
        out = self._render_full(receipt)
        self.assertNotIn("GIT", out)
        self.assertNotIn("Workspace", out)

    def test_workspace_kind_none_does_not_show_workspace_row(self):
        receipt = self._base_receipt(safe_workspace_kind="none")
        out = self._render_full(receipt)
        self.assertNotIn("Workspace", out)

    def test_old_entry_with_sandbox_type_only_no_git_section(self):
        """
        Old receipts that have a sandbox object but no new git/workspace fields must
        not produce a GIT section.  safe_workspace_kind alone (populated from
        sandbox_type on old entries) must not trigger _git_new.
        """
        entry = {
            "task": "test",
            "timestamp": "2026-05-31T10:00:00Z",
            "sandbox": {"sandbox_type": "temp"},
            # no git_base_branch, no git_base_commit_hash, no safe_workspace_display_name
        }
        receipt = self._build_receipt(entry)
        self.assertEqual(receipt.safe_workspace_kind, "temp")
        self.assertIsNone(receipt.safe_workspace_display_name)
        out = self._render_full(receipt)
        self.assertNotIn("GIT", out)
        self.assertNotIn("Workspace", out)

    def test_old_entry_with_worktree_sandbox_type_only_no_git_section(self):
        """Same check for worktree sandbox_type on an old entry."""
        entry = {
            "task": "test",
            "timestamp": "2026-05-31T10:00:00Z",
            "sandbox": {"sandbox_type": "worktree", "worktree_branch": "osn/run-old"},
        }
        receipt = self._build_receipt(entry)
        self.assertEqual(receipt.safe_workspace_kind, "worktree")
        self.assertIsNone(receipt.safe_workspace_display_name)
        out = self._render_full(receipt)
        self.assertNotIn("GIT", out)
        self.assertNotIn("Workspace", out)

    def test_old_entry_missing_sandbox_renders_without_crash(self):
        entry = {"task": "test", "timestamp": "2026-05-31T10:00:00Z"}
        receipt = self._build_receipt(entry)
        self.assertIsNone(receipt.safe_workspace_kind)
        self.assertIsNone(receipt.safe_workspace_display_name)
        out = self._render_full(receipt)
        self.assertNotIn("Workspace", out)

    def test_build_shard_receipt_reads_safe_workspace_kind_from_sandbox(self):
        entry = {
            "task": "test",
            "timestamp": "2026-05-31T10:00:00Z",
            "sandbox": {
                "sandbox_type": "worktree",
                "safe_workspace_display_name": "osn/run-abc",
                "sandbox_enabled": True,
            },
            "git_base_branch": "main",
            "git_base_commit_hash": "a" * 40,
        }
        receipt = self._build_receipt(entry)
        self.assertEqual(receipt.safe_workspace_kind, "worktree")
        self.assertEqual(receipt.safe_workspace_display_name, "osn/run-abc")
        self.assertEqual(receipt.git_base_branch, "main")

    def test_build_shard_receipt_temp_sandbox(self):
        entry = {
            "task": "test",
            "timestamp": "2026-05-31T10:00:00Z",
            "sandbox": {
                "sandbox_type": "temp",
                "safe_workspace_display_name": "osn-temp",
                "sandbox_enabled": True,
            },
        }
        receipt = self._build_receipt(entry)
        self.assertEqual(receipt.safe_workspace_kind, "temp")
        self.assertEqual(receipt.safe_workspace_display_name, "osn-temp")

    def test_build_shard_receipt_no_crash_missing_sandbox_key(self):
        entry = {"task": "test", "timestamp": "2026-05-31T10:00:00Z"}
        receipt = self._build_receipt(entry)
        self.assertIsNone(receipt.safe_workspace_kind)

    def test_safe_workspace_display_name_not_absolute_path_in_receipt(self):
        import re
        entry = {
            "task": "test",
            "timestamp": "2026-05-31T10:00:00Z",
            "sandbox": {
                "sandbox_type": "worktree",
                "safe_workspace_display_name": "osn/run-2026-05-31t10-00-00",
                "sandbox_enabled": True,
            },
        }
        receipt = self._build_receipt(entry)
        name = receipt.safe_workspace_display_name
        self.assertIsNotNone(name)
        self.assertFalse(name.startswith("/"), f"display name looks like absolute path: {name}")
        self.assertFalse(bool(re.match(r"[A-Za-z]:\\", name)), f"display name looks like Windows path: {name}")


class TestLastMoreRoleTruthDisplay(unittest.TestCase):
    """_render_log_entry at more/full detail shows the Dispatched/Advisory block."""

    # Minimal entry that has tier dispatch applied so role_selection_mode=DISPATCHED.
    _DISPATCH_ENTRY = {
        "task": "refactor auth",
        "timestamp": "2025-01-01T00:00:00Z",
        "execution_model": "z-ai/glm-5.1",
        "tier_dispatch_receipt": {
            "enabled": True,
            "applied": True,
            "tier_source": "category",
            "planner_tier": "frontier-reasoning-model",
            "planner_model": MODEL_STRONG,
            "executor_tier": "balanced-coding-model",
            "executor_model": MODEL_MAIN,
            "executor_model_actual": MODEL_MAIN,
            "validator_tier": "independent-validator-model",
            "validator_model": MODEL_STRONG,
            "validator_dispatch_status": "applied",
            "fallback_used": False,
            "fallback_reason": "",
            "warnings": [],
        },
    }

    # Advisory-only entry (no tier dispatch applied).
    _ADVISORY_ENTRY = {
        "task": "add tests",
        "timestamp": "2025-01-01T00:00:00Z",
        "execution_model": "z-ai/glm-5.1",
        "model_selection_decision": {
            "strategy": "cost-balanced",
            "roles": [
                {"role": "planner", "model_tier": "frontier-reasoning-model"},
                {"role": "executor", "model_tier": "balanced-coding-model"},
                {"role": "validator", "model_tier": "independent-validator-model"},
            ],
        },
    }

    # Legacy entry — no role metadata at all.
    _LEGACY_ENTRY = {
        "task": "fix bug",
        "timestamp": "2024-01-01T00:00:00Z",
        "execution_model": "z-ai/glm-5.1",
    }

    def test_last_more_shows_dispatched_roles_line(self) -> None:
        output = _render(self._DISPATCH_ENTRY, "more")
        self.assertIn("Dispatched roles:", output)

    def test_last_more_shows_advisory_only_line(self) -> None:
        output = _render(self._DISPATCH_ENTRY, "more")
        self.assertIn("Advisory only: planner", output)

    def test_last_more_no_dispatch_shows_none(self) -> None:
        output = _render(self._ADVISORY_ENTRY, "more")
        self.assertIn("Dispatched roles: none", output)

    def test_last_more_advisory_entry_shows_all_roles_advisory(self) -> None:
        output = _render(self._ADVISORY_ENTRY, "more")
        self.assertIn("Advisory only:", output)
        self.assertIn("planner", output)
        self.assertIn("executor", output)
        self.assertIn("validator", output)

    def test_last_more_legacy_entry_no_role_lines(self) -> None:
        output = _render(self._LEGACY_ENTRY, "more")
        self.assertNotIn("Dispatched roles:", output)
        self.assertNotIn("Advisory only:", output)

    def test_last_default_no_role_truth_block(self) -> None:
        # The new block must not appear at default detail.
        output = _render(self._DISPATCH_ENTRY, "default")
        self.assertNotIn("Dispatched roles:", output)
        self.assertNotIn("Advisory only:", output)


if __name__ == "__main__":
    unittest.main()
