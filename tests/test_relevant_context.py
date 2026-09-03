"""Tests for openshard.history.query.relevant_context (Demo v1 PR3: Relevant Context)
and the ``relevant_context`` MCP tool exposing it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from openshard.history.jsonl_store import write_jsonl
from openshard.history.query import (
    DEFAULT_CONTEXT_LIMIT,
    RelevantContext,
    RelevantMatch,
    relevant_context,
)
from openshard.mcp.server import MAX_LIMIT, build_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(task: str, ts: str, **kwargs) -> dict:
    """A minimal native pipeline entry, the same shape _log_run persists."""
    base: dict = {
        "schema_version": "1.2",
        "timestamp": ts,
        "run_id": ts,
        "task": task,
        "workflow": "native",
        "executor": "native",
        "retry_triggered": False,
        "verification_attempted": True,
        "verification_passed": True,
        "summary": "done",
        "repo_name": "alpha",
    }
    base.update(kwargs)
    return base


def _write(repo_path: Path, entries: list[dict]) -> None:
    write_jsonl(repo_path / ".openshard" / "runs.jsonl", entries)


T1 = "2026-08-01T10:00:00Z"
T2 = "2026-08-02T10:00:00Z"
T3 = "2026-08-03T10:00:00Z"
T4 = "2026-08-04T10:00:00Z"


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """One unrelated Shard, one failed relevant attempt, one later relevant
    attempt that succeeds, and a multi-attempt Shard on the same topic.

    Mirrors the manual smoke-test scenario in the PR spec: given a query
    about terraform verification, the terraform-related history should win
    over the unrelated login-bug Shard.
    """
    _write(tmp_path, [
        _entry("fix login button alignment", T1, shard_id="shard-unrelated"),
        _entry("add terraform verification step", T2, shard_id="shard-failed",
               verification_passed=False,
               findings=[{"severity": "High", "message": "terraform validate failed: missing provider block",
                          "path": "main.tf"}],
               agent_notes=["do not merge yet"]),
        _entry("retry terraform verification", T3, shard_id="shard-passed",
               verification_passed=True),
    ])
    return tmp_path


@pytest.fixture
def multi_attempt(tmp_path: Path) -> Path:
    """One Shard retried across two attempts: fails, then passes."""
    _write(tmp_path, [
        _entry("implement retry handling for terraform verification", T1,
               shard_id="shard-multi", attempt_number=1, verification_passed=False,
               review_checks=[{"name": "terraform_validate", "status": "failed",
                                "summary": "missing required provider"}]),
        _entry("unrelated task", T2, shard_id="shard-solo", attempt_number=1),
        _entry("implement retry handling for terraform verification (retry)", T3,
               shard_id="shard-multi", attempt_number=2, retry_triggered=True,
               verification_passed=True),
    ])
    return tmp_path


# ---------------------------------------------------------------------------
# History-layer: relevant_context()
# ---------------------------------------------------------------------------


class TestRelevantContextRanking:
    def test_direct_keyword_overlap_wins(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        ids = [m.shard.shard_id for m in ctx.matches]
        assert "shard-unrelated" not in ids
        assert "shard-failed" in ids and "shard-passed" in ids

    def test_case_normalization(self, scenario: Path):
        ctx = relevant_context("TERRAFORM Verification", repo_path=scenario)
        ids = {m.shard.shard_id for m in ctx.matches}
        assert ids == {"shard-failed", "shard-passed"}

    def test_unrelated_history_excluded(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        assert all(m.shard.shard_id != "shard-unrelated" for m in ctx.matches)

    def test_unrelated_history_excluded_even_with_failure(self, tmp_path: Path):
        """A failure/retry bonus never pulls in a Shard with zero keyword overlap."""
        _write(tmp_path, [
            _entry("fix login button alignment", T1, shard_id="s1", verification_passed=False),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        assert ctx.matches == []

    def test_repository_filtering(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("add terraform verification", T1, shard_id="s-alpha", repo_name="alpha"),
            _entry("add terraform verification", T2, shard_id="s-beta", repo_name="beta"),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path, repo="beta")
        assert [m.shard.shard_id for m in ctx.matches] == ["s-beta"]

    def test_recency_tie_breaking(self, tmp_path: Path):
        """Equal keyword score (no failure/retry bonus on either side) -> newest first."""
        _write(tmp_path, [
            _entry("add auth support", T1, shard_id="s-old"),
            _entry("add auth support", T2, shard_id="s-new"),
        ])
        ctx = relevant_context("auth support", repo_path=tmp_path)
        assert [m.shard.shard_id for m in ctx.matches] == ["s-new", "s-old"]
        assert ctx.matches[0].score == ctx.matches[1].score

    def test_failure_signal_surfaced(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        failed = next(m for m in ctx.matches if m.shard.shard_id == "shard-failed")
        assert "prior verification failure" in failed.signals
        assert failed.status == "Failed"

    def test_verification_failure_signal_from_review_checks(self, multi_attempt: Path):
        ctx = relevant_context("terraform verification", repo_path=multi_attempt)
        matched = next(m for m in ctx.matches if m.shard.shard_id == "shard-multi")
        assert "prior verification failure" in matched.signals

    def test_failed_shard_ranks_above_passed_shard_on_same_topic(self, scenario: Path):
        """A failed attempt about the same topic carries a bonus a plain pass doesn't."""
        ctx = relevant_context("terraform verification", repo_path=scenario)
        ids = [m.shard.shard_id for m in ctx.matches]
        assert ids.index("shard-failed") < ids.index("shard-passed")

    def test_successful_later_attempt_represented_correctly(self, multi_attempt: Path):
        ctx = relevant_context("terraform verification", repo_path=multi_attempt)
        matched = next(m for m in ctx.matches if m.shard.shard_id == "shard-multi")
        assert matched.status == "Passed"  # latest attempt's state
        assert matched.attempts[0].status == "Checks: 1 failed"  # attempt 1: review_checks failure
        assert matched.attempts[1].status == "Passed"  # attempt 2: recovered
        assert matched.attempts[0].attempt_number == 1
        assert matched.attempts[1].attempt_number == 2

    def test_multi_attempt_shard_appears_once(self, multi_attempt: Path):
        ctx = relevant_context("terraform verification", repo_path=multi_attempt)
        ids = [m.shard.shard_id for m in ctx.matches]
        assert ids.count("shard-multi") == 1

    def test_multi_attempt_bonus_in_signals(self, multi_attempt: Path):
        ctx = relevant_context("terraform verification", repo_path=multi_attempt)
        matched = next(m for m in ctx.matches if m.shard.shard_id == "shard-multi")
        assert "multiple attempts (2)" in matched.signals

    def test_deterministic_ordering(self, scenario: Path):
        first = relevant_context("terraform verification", repo_path=scenario)
        second = relevant_context("terraform verification", repo_path=scenario)
        assert [(m.shard.shard_id, m.score) for m in first.matches] == \
               [(m.shard.shard_id, m.score) for m in second.matches]

    def test_result_limit(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario, limit=1)
        assert len(ctx.matches) == 1

    def test_default_limit_is_five(self):
        assert DEFAULT_CONTEXT_LIMIT == 5

    def test_zero_limit_returns_empty(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario, limit=0)
        assert ctx.matches == []
        assert ctx.context_text

    def test_negative_limit_returns_empty(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario, limit=-3)
        assert ctx.matches == []

    def test_blank_task_returns_empty_with_honest_text(self, scenario: Path):
        ctx = relevant_context("", repo_path=scenario)
        assert ctx.matches == []
        assert "No task given" in ctx.context_text

    def test_whitespace_only_task_returns_empty(self, scenario: Path):
        ctx = relevant_context("   ", repo_path=scenario)
        assert ctx.matches == []

    def test_empty_history_returns_empty(self, tmp_path: Path):
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        assert ctx.matches == []
        assert "No relevant prior OpenShard history" in ctx.context_text

    def test_weak_no_match_returns_empty_with_honest_text(self, scenario: Path):
        ctx = relevant_context("zzz-nonexistent-topic-zzz", repo_path=scenario)
        assert ctx.matches == []
        assert "No relevant prior OpenShard history" in ctx.context_text

    def test_returns_relevant_context_dataclass(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        assert isinstance(ctx, RelevantContext)
        assert all(isinstance(m, RelevantMatch) for m in ctx.matches)


# ---------------------------------------------------------------------------
# PR10: evidence-based ranking (file overlap, generic-noise filtering,
# failure-then-resolution, recency vs. signal strength)
# ---------------------------------------------------------------------------


class TestEvidenceBasedRanking:
    def test_file_path_overlap_ranks_strongly(self, tmp_path: Path):
        """A task naming a file the Shard actually touched outranks a Shard
        that only shares ordinary topic words -- exact evidence beats
        looser topical overlap."""
        _write(tmp_path, [
            _entry("write documentation about the query module", T1, shard_id="s-weak"),
            _entry("unrelated cleanup work", T2, shard_id="s-file",
                   files_detail=[{"path": "openshard/history/query.py", "change_type": "update"}]),
        ])
        ctx = relevant_context(
            "refactor openshard/history/query.py for the query module", repo_path=tmp_path,
        )
        ids = [m.shard.shard_id for m in ctx.matches]
        assert ids[0] == "s-file"
        assert ids[1] == "s-weak"
        top = ctx.matches[0]
        assert any("modified the same file: openshard/history/query.py" in s for s in top.signals)
        assert top.score > ctx.matches[1].score

    def test_referenced_file_finding_counts_as_evidence(self, tmp_path: Path):
        """A non-Note finding's path is legitimate file evidence even when the
        Shard's task text shares no words with the query at all."""
        _write(tmp_path, [
            _entry("investigate slow query", T1, shard_id="s1",
                   findings=[{"severity": "High", "message": "slow join",
                              "path": "openshard/history/metrics.py"}]),
        ])
        ctx = relevant_context("openshard/history/metrics.py performance", repo_path=tmp_path)
        assert [m.shard.shard_id for m in ctx.matches] == ["s1"]
        assert any(
            "prior finding referenced the same file: openshard/history/metrics.py" in s
            for s in ctx.matches[0].signals
        )

    def test_note_severity_finding_path_is_not_file_evidence(self, tmp_path: Path):
        """A finding with no recorded severity defaults to Note and must never
        become file-overlap evidence -- the same boundary that keeps it out
        of the match's displayed findings (see TestPrivacyBoundary)."""
        _write(tmp_path, [
            _entry("cleanup notes", T1, shard_id="s1",
                   findings=[{"message": "looked at this", "path": "secret/internal.py"}]),
        ])
        ctx = relevant_context("check secret/internal.py", repo_path=tmp_path)
        assert ctx.matches == []

    def test_generic_keyword_noise_does_not_rank(self, tmp_path: Path):
        """A task built entirely from generic engineering verbs ("fix",
        "bug", "add", "test") matches nothing, even though history is full
        of entries using those same words prominently."""
        _write(tmp_path, [
            _entry("fix bug in login flow", T1, shard_id="s1"),
            _entry("add test for signup", T2, shard_id="s2"),
        ])
        ctx = relevant_context("fix the bug and add a test", repo_path=tmp_path)
        assert ctx.matches == []
        assert "No relevant prior OpenShard history" in ctx.context_text

    def test_generic_words_do_not_block_the_specific_terms_around_them(self, tmp_path: Path):
        """Generic terms are dropped from scoring, not the whole query --
        "fix terraform validate" still matches on "terraform"/"validate"."""
        _write(tmp_path, [_entry("fix flaky terraform validate test", T1, shard_id="s1")])
        ctx = relevant_context("fix terraform validate", repo_path=tmp_path)
        assert [m.shard.shard_id for m in ctx.matches] == ["s1"]
        assert "task overlap: terraform, validate" in ctx.matches[0].signals

    def test_failed_then_resolved_signal(self, tmp_path: Path):
        """An earlier failed attempt later resolved by a passing attempt gets
        its own explicit, explainable signal -- not just "multiple attempts"."""
        _write(tmp_path, [
            _entry("implement retry handling for terraform verification", T1,
                   shard_id="shard-multi", attempt_number=1, verification_passed=False),
            _entry("implement retry handling for terraform verification (retry)", T3,
                   shard_id="shard-multi", attempt_number=2, retry_triggered=True,
                   verification_passed=True),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        assert len(ctx.matches) == 1
        assert "earlier attempt failed verification; a later attempt passed" in ctx.matches[0].signals

    def test_unrelated_recent_task_does_not_beat_older_high_signal(self, tmp_path: Path):
        """Strong old evidence (file overlap + broad keyword overlap) beats
        weak recent overlap -- recency only ever breaks a tie, never
        outweighs the underlying evidence."""
        _write(tmp_path, [
            _entry("update terraform provider config in openshard/infra/main.tf", T1,
                   shard_id="s-old",
                   files_detail=[{"path": "openshard/infra/main.tf", "change_type": "update"}]),
            _entry("terraform notes", T4, shard_id="s-recent"),
        ])
        ctx = relevant_context("openshard/infra/main.tf terraform provider", repo_path=tmp_path)
        ids = [m.shard.shard_id for m in ctx.matches]
        assert ids[0] == "s-old"
        assert ctx.matches[0].score > ctx.matches[1].score

    def test_no_evidence_at_all_returns_no_result(self, tmp_path: Path):
        """A well-formed, non-generic task with genuinely nothing in common
        with recorded history returns no matches, never padded-in noise."""
        _write(tmp_path, [_entry("rotate kubernetes certificates", T1, shard_id="s1")])
        ctx = relevant_context("refactor the billing invoice exporter", repo_path=tmp_path)
        assert ctx.matches == []

    def test_existing_callers_remain_compatible(self, scenario: Path):
        """The public signature/return type used before PR10 keeps working
        unchanged: positional task, keyword-only limit/repo/repo_path."""
        ctx = relevant_context("terraform verification", limit=3, repo=None, repo_path=scenario)
        assert isinstance(ctx, RelevantContext)
        assert all(isinstance(m, RelevantMatch) for m in ctx.matches)
        assert len(ctx.matches) <= 3


# ---------------------------------------------------------------------------
# Path-match semantics: whole-segment matching only, deterministic choice
# ---------------------------------------------------------------------------


class TestPathMatchSemantics:
    def test_same_basename_in_a_different_directory_does_not_match(self, tmp_path: Path):
        """The core false positive: "backend/utils.py" must not match a Shard
        that touched "frontend/utils.py". Claiming "modified the same file"
        about a different file is worse than returning nothing."""
        _write(tmp_path, [
            _entry("rework the frontend helpers", T1, shard_id="s-frontend",
                   files_detail=[{"path": "frontend/utils.py", "change_type": "update"}]),
        ])
        ctx = relevant_context("backend/utils.py is throwing", repo_path=tmp_path)
        assert ctx.matches == []

    def test_partial_path_matches_a_longer_repo_relative_path(self, tmp_path: Path):
        """Real suffix semantics still work: a query naming "backend/utils.py"
        matches the Shard that touched "src/backend/utils.py"."""
        _write(tmp_path, [
            _entry("rework helpers", T1, shard_id="s1",
                   files_detail=[{"path": "src/backend/utils.py", "change_type": "update"}]),
        ])
        ctx = relevant_context("backend/utils.py is throwing", repo_path=tmp_path)
        assert [m.shard.shard_id for m in ctx.matches] == ["s1"]
        assert "modified the same file: src/backend/utils.py" in ctx.matches[0].signals

    def test_bare_filename_matches_any_directory(self, tmp_path: Path):
        """A bare filename names no directory to contradict, so it may match
        one at any depth -- but naming a directory that disagrees must not."""
        _write(tmp_path, [
            _entry("rework helpers", T1, shard_id="s1",
                   files_detail=[{"path": "frontend/utils.py", "change_type": "update"}]),
        ])
        bare = relevant_context("refactor utils.py", repo_path=tmp_path)
        assert [m.shard.shard_id for m in bare.matches] == ["s1"]
        assert "modified the same file: frontend/utils.py" in bare.matches[0].signals
        scoped = relevant_context("refactor backend/utils.py", repo_path=tmp_path)
        assert scoped.matches == []

    def test_exact_path_still_matches(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("rework helpers", T1, shard_id="s1",
                   files_detail=[{"path": "openshard/history/query.py", "change_type": "update"}]),
        ])
        ctx = relevant_context("openshard/history/query.py needs a look", repo_path=tmp_path)
        assert "modified the same file: openshard/history/query.py" in ctx.matches[0].signals

    def test_windows_separators_normalize_to_the_same_file(self, tmp_path: Path):
        """Normalization behaviour is preserved: a backslash path in the
        query and a forward-slash path in the evidence are the same file."""
        _write(tmp_path, [
            _entry("rework helpers", T1, shard_id="s1",
                   files_detail=[{"path": "src/backend/utils.py", "change_type": "update"}]),
        ])
        ctx = relevant_context(r"look at backend\utils.py", repo_path=tmp_path)
        assert "modified the same file: src/backend/utils.py" in ctx.matches[0].signals

    def test_common_filenames_do_not_create_misleading_directory_matches(self, tmp_path: Path):
        """index.ts / config.py live in many directories; naming one directory
        must not match a Shard that touched another."""
        _write(tmp_path, [
            _entry("ship the web client", T1, shard_id="s-web",
                   files_detail=[{"path": "lib/index.ts", "change_type": "update"}]),
            _entry("tidy the fixtures", T2, shard_id="s-fixtures",
                   files_detail=[{"path": "tests/config.py", "change_type": "update"}]),
        ])
        assert relevant_context("src/index.ts renders twice", repo_path=tmp_path).matches == []
        assert relevant_context("app/config.py defaults", repo_path=tmp_path).matches == []

    def test_signal_path_choice_is_stable_within_a_process(self, tmp_path: Path):
        """Several touched files match a bare filename; the same one is named
        every time (evidence is scanned in sorted order, not set order)."""
        _write(tmp_path, [
            _entry("touch several same-named files", T1, shard_id="s1",
                   files_detail=[{"path": p, "change_type": "update"} for p in
                                 ("pkg/z/utils.py", "pkg/a/utils.py", "pkg/m/utils.py")]),
        ])
        signals = [
            relevant_context("refactor utils.py", repo_path=tmp_path).matches[0].signals
            for _ in range(5)
        ]
        assert all(s == signals[0] for s in signals)
        assert signals[0] == ["modified the same file: pkg/a/utils.py"]

    def test_signal_path_choice_is_stable_across_hash_seeds(self, tmp_path: Path):
        """The real proof: set iteration order varies with the interpreter's
        hash seed between processes, so the same query is run in separate
        processes under different seeds and must name the same file."""
        _write(tmp_path, [
            _entry("touch several same-named files", T1, shard_id="s1",
                   files_detail=[{"path": p, "change_type": "update"} for p in
                                 ("pkg/z/utils.py", "pkg/a/utils.py", "pkg/m/utils.py")]),
        ])
        script = (
            "import json, sys; from pathlib import Path;"
            "from openshard.history.query import relevant_context;"
            "print(json.dumps(relevant_context('refactor utils.py',"
            " repo_path=Path(sys.argv[1])).matches[0].signals))"
        )
        outputs = []
        for seed in ("0", "1", "2", "3"):
            proc = subprocess.run(
                [sys.executable, "-c", script, str(tmp_path)],
                capture_output=True, text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            assert proc.returncode == 0, proc.stderr
            outputs.append(proc.stdout.strip())
        assert len(set(outputs)) == 1, outputs
        assert json.loads(outputs[0]) == ["modified the same file: pkg/a/utils.py"]


# ---------------------------------------------------------------------------
# Performance: one load/group pass regardless of matches
# ---------------------------------------------------------------------------


class TestSingleLoadBehavior:
    def test_load_runs_called_once(self, scenario: Path):
        from openshard.history import query as history_query_module

        with patch.object(
            history_query_module, "load_runs", wraps=history_query_module.load_runs
        ) as spy:
            relevant_context("terraform verification", repo_path=scenario)
            assert spy.call_count == 1

    def test_load_runs_called_once_even_with_many_matches(self, tmp_path: Path):
        from openshard.history import query as history_query_module

        entries = [
            _entry(f"add terraform verification step {i}", f"2026-08-{i + 1:02d}T10:00:00Z",
                   shard_id=f"s{i}")
            for i in range(15)
        ]
        _write(tmp_path, entries)
        with patch.object(
            history_query_module, "load_runs", wraps=history_query_module.load_runs
        ) as spy:
            ctx = relevant_context("terraform verification", repo_path=tmp_path, limit=5)
            assert spy.call_count == 1
        assert len(ctx.matches) == 5


# ---------------------------------------------------------------------------
# Privacy: findings/notes boundary
# ---------------------------------------------------------------------------


class TestPrivacyBoundary:
    def test_note_severity_findings_excluded(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        for m in ctx.matches:
            assert all(f.severity != "Note" for f in m.findings)

    def test_agent_notes_not_leaked_into_findings(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        failed = next(m for m in ctx.matches if m.shard.shard_id == "shard-failed")
        assert not any("do not merge yet" in f.message for f in failed.findings)

    def test_agent_notes_not_leaked_into_context_text(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        assert "do not merge yet" not in ctx.context_text

    def test_structured_findings_are_surfaced(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        failed = next(m for m in ctx.matches if m.shard.shard_id == "shard-failed")
        assert any("terraform validate failed" in f.message for f in failed.findings)
        assert "terraform validate failed" in ctx.context_text

    def test_no_raw_prompt_or_transcript_leaks(self, tmp_path: Path):
        _write(tmp_path, [_entry(
            "add terraform verification", T1, shard_id="s1",
            raw_prompt="secret-prompt-token",
            transcript="secret-transcript-token",
            notes=["secret-note-token"],
            workspace_path="C:/Users/private/secret-folder",
        )])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        blob = ctx.context_text + json.dumps([m.__dict__ for m in ctx.matches], default=str)
        for marker in ("secret-prompt-token", "secret-transcript-token", "secret-note-token", "secret-folder"):
            assert marker not in blob


# ---------------------------------------------------------------------------
# context_text shape
# ---------------------------------------------------------------------------


class TestContextText:
    def test_context_text_is_bounded(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        assert len(ctx.context_text) < 4000

    def test_context_text_includes_task(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        assert "terraform verification" in ctx.context_text

    def test_context_text_includes_shard_ids(self, scenario: Path):
        ctx = relevant_context("terraform verification", repo_path=scenario)
        assert "shard-failed" in ctx.context_text
        assert "shard-passed" in ctx.context_text

    def test_context_text_shows_attempt_history(self, multi_attempt: Path):
        ctx = relevant_context("terraform verification", repo_path=multi_attempt)
        assert "Attempts:" in ctx.context_text
        assert "failed" in ctx.context_text and "Passed" in ctx.context_text


# ---------------------------------------------------------------------------
# MCP tool: registration and call
# ---------------------------------------------------------------------------


def _call(server, name: str, args: dict):
    return asyncio.run(server.call_tool(name, args))


class TestRelevantContextMcpTool:
    def test_tool_is_registered(self, scenario: Path):
        server = build_server(repo_path=scenario)
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        assert "relevant_context" in tools
        assert tools["relevant_context"].description and len(tools["relevant_context"].description) > 10

    def test_tool_requires_task_argument(self, scenario: Path):
        server = build_server(repo_path=scenario)
        schema = asyncio.run(server.list_tools())
        tool = next(t for t in schema if t.name == "relevant_context")
        assert "task" in tool.inputSchema.get("required", [])

    def test_call_returns_matches_and_context_text(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        assert structured["task"] == "terraform verification"
        ids = {m["shard_id"] for m in structured["matches"]}
        assert ids == {"shard-failed", "shard-passed"}
        assert "context_text" in structured and structured["context_text"]

    def test_call_empty_task_returns_no_matches(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": ""})
        assert structured["matches"] == []

    def test_call_repo_filter(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("add terraform verification", T1, shard_id="s-alpha", repo_name="alpha"),
            _entry("add terraform verification", T2, shard_id="s-beta", repo_name="beta"),
        ])
        server = build_server(repo_path=tmp_path)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification", "repo": "beta"})
        assert [m["shard_id"] for m in structured["matches"]] == ["s-beta"]

    def test_call_limit_is_respected(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification", "limit": 1})
        assert len(structured["matches"]) == 1

    def test_huge_limit_is_clamped_not_rejected(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context",
                               {"task": "terraform verification", "limit": 10_000_000})
        assert len(structured["matches"]) <= 2
        assert MAX_LIMIT < 10_000_000

    def test_result_is_json_safe(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        json.dumps(structured)  # must not raise

    def test_match_fields_are_bounded(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        match = structured["matches"][0]
        assert set(match) == {
            "shard_id", "created_at", "task_short", "task_full", "agent", "origin", "capture_depth",
            "score", "why_relevant", "status", "verification_status", "verification_reason",
            "result", "repo", "files", "findings", "attempts",
        }

    def test_privacy_no_forbidden_fields_leak(self, tmp_path: Path):
        _write(tmp_path, [_entry(
            "add terraform verification", T1, shard_id="s1",
            agent_notes=["private agent note"],
            raw_prompt="secret-prompt-token",
            transcript="secret-transcript-token",
            adapter_stdout_summary="secret-stdout-token",
            adapter_stderr_summary="secret-stderr-token",
            workspace_path="C:/Users/private/secret-folder",
        )])
        server = build_server(repo_path=tmp_path)
        content, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        blob = "".join(b.text for b in content) + json.dumps(structured)
        for marker in ("secret-prompt-token", "secret-transcript-token", "secret-stdout-token",
                       "secret-stderr-token", "secret-folder", "private agent note"):
            assert marker not in blob

    def test_unknown_repo_returns_empty(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context",
                               {"task": "terraform verification", "repo": "does-not-exist"})
        assert structured["matches"] == []
