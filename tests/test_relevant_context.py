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
# PR11: evidence-backed recovery observations
# ---------------------------------------------------------------------------


def _tool_event(tool: str, event_id: str) -> dict:
    """An embedded canonical Event (see openshard.history.event) recording
    one tool invocation -- the same shape adapters like the Claude Code
    hooks adapter already write onto ``events``."""
    return {
        "schema_version": 1, "event_id": event_id, "event_type": "tool.invoked",
        "occurred_at": None, "run_id": None, "shard_id": None, "attempt_number": None,
        "actor": None, "source": "test", "action": tool, "target": None,
        "status": "unknown", "evidence": "unknown", "metadata": {"tool": tool},
        "raw_content_stored": False,
    }


def _recovery_pair(
    shard_id: str,
    *,
    failed_ts: str = T1,
    recovery_ts: str = T3,
    recovery_kwargs: dict | None = None,
    failed_kwargs: dict | None = None,
) -> list[dict]:
    """A minimal two-attempt Shard: attempt 1 fails verification, attempt 2
    later passes. Both entries carry "terraform verification" in their task
    text so the Shard matches regardless of which attempt is "latest"."""
    return [
        _entry("add terraform verification step", failed_ts, shard_id=shard_id,
               attempt_number=1, verification_passed=False, **(failed_kwargs or {})),
        _entry("terraform verification retry", recovery_ts, shard_id=shard_id,
               attempt_number=2, verification_passed=True, **(recovery_kwargs or {})),
    ]


class TestRecoveryObservation:
    def test_failed_then_later_passed_yields_recovery_observation(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair(
            "shard-recover",
            recovery_kwargs={
                "files_detail": [
                    {"path": "openshard/history/query.py", "change_type": "update"},
                    {"path": "tests/test_history_query.py", "change_type": "update"},
                ],
                "events": [_tool_event("Edit", "e1"), _tool_event("Bash", "e2")],
            },
        ))
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        match = next(m for m in ctx.matches if m.shard.shard_id == "shard-recover")
        ro = match.recovery
        assert ro is not None
        assert ro.shard_id == "shard-recover"
        assert ro.failed_attempt_number == 1
        assert ro.recovery_attempt_number == 2
        assert ro.intervening_files == ["openshard/history/query.py", "tests/test_history_query.py"]
        assert ro.intervening_tools == ["Edit", "Bash"]
        assert ro.later_same_file_activity == 0

    def test_no_observation_when_only_failure_and_no_later_pass(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("add terraform verification step", T1, shard_id="shard-onlyfail",
                   verification_passed=False),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        match = next(m for m in ctx.matches if m.shard.shard_id == "shard-onlyfail")
        assert match.recovery is None

    def test_no_observation_when_only_a_pass(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("add terraform verification step", T1, shard_id="shard-onlypass",
                   verification_passed=True),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        match = next(m for m in ctx.matches if m.shard.shard_id == "shard-onlypass")
        assert match.recovery is None

    def test_no_observation_when_two_attempts_both_fail(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("add terraform verification step", T1, shard_id="shard-stillfailing",
                   attempt_number=1, verification_passed=False),
            _entry("terraform verification retry", T3, shard_id="shard-stillfailing",
                   attempt_number=2, verification_passed=False),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        match = next(m for m in ctx.matches if m.shard.shard_id == "shard-stillfailing")
        assert match.recovery is None

    def test_deterministic_output_across_calls(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair(
            "shard-recover",
            recovery_kwargs={"files_detail": [{"path": "a.py", "change_type": "update"}]},
        ))
        first = relevant_context("terraform verification", repo_path=tmp_path)
        second = relevant_context("terraform verification", repo_path=tmp_path)
        r1 = next(m for m in first.matches if m.shard.shard_id == "shard-recover").recovery
        r2 = next(m for m in second.matches if m.shard.shard_id == "shard-recover").recovery
        assert r1 == r2

    def test_provenance_fields_are_correct(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair("shard-recover", failed_ts=T1, recovery_ts=T3))
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        ro = next(m for m in ctx.matches if m.shard.shard_id == "shard-recover").recovery
        assert ro.shard_id == "shard-recover"
        assert ro.failed_attempt_number == 1
        assert ro.recovery_attempt_number == 2
        assert ro.failed_timestamp == T1
        assert ro.recovery_timestamp == T3
        assert ro.failed_run_id == T1
        assert ro.recovery_run_id == T3
        assert ro.failure_status
        assert ro.recovery_status

    def test_intervening_files_are_repo_relative_and_bounded(self, tmp_path: Path):
        """An absolute path in the raw evidence is dropped, never surfaced --
        even though the rest of relevant_context does not re-check this for
        older evidence (see the module docstring's privacy rule for PR11)."""
        _write(tmp_path, _recovery_pair(
            "shard-abs",
            recovery_kwargs={"files_detail": [
                {"path": "C:/Users/private/secret.py", "change_type": "update"},
                {"path": "openshard/history/query.py", "change_type": "update"},
            ]},
        ))
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        ro = next(m for m in ctx.matches if m.shard.shard_id == "shard-abs").recovery
        assert ro.intervening_files == ["openshard/history/query.py"]

    def test_no_raw_output_or_secrets_leak_into_recovery_detail(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair(
            "shard-secret",
            failed_kwargs={"osn_verification_contract": {
                "enabled": True, "status": "failed",
                "summary": "token=sk-abcdefghijklmnopqrstuvwxyz123456 leaked in output",
            }},
            recovery_kwargs={"osn_verification_contract": {
                "enabled": True, "status": "passed", "summary": "all checks passed",
            }},
        ))
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        ro = next(m for m in ctx.matches if m.shard.shard_id == "shard-secret").recovery
        assert ro.failure_detail is None
        blob = json.dumps(ro.__dict__, default=str)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in blob

    def test_missing_detail_stays_missing_not_synthesized(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair("shard-nodetail"))
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        ro = next(m for m in ctx.matches if m.shard.shard_id == "shard-nodetail").recovery
        assert ro.failure_detail is None
        assert ro.recovery_detail is None
        assert ro.intervening_files == []
        assert ro.intervening_tools == []

    def test_later_same_file_activity_counts_correctly(self, tmp_path: Path):
        """Under the latest-state rule anything after the recovery attempt is
        unverified by construction; the count is of those unverified
        follow-ups that touched one of the same files."""
        _write(tmp_path, [
            _entry("add terraform verification step", T1, shard_id="shard-activity",
                   attempt_number=1, verification_passed=False),
            _entry("terraform verification retry", T2, shard_id="shard-activity",
                   attempt_number=2, verification_passed=True,
                   files_detail=[{"path": "openshard/history/query.py", "change_type": "update"}]),
            _entry("terraform verification follow-up", T3, shard_id="shard-activity",
                   attempt_number=3, **_UNVERIFIED,
                   files_detail=[{"path": "openshard/history/query.py", "change_type": "update"}]),
            _entry("terraform verification cleanup", T4, shard_id="shard-activity",
                   attempt_number=4, **_UNVERIFIED,
                   files_detail=[{"path": "openshard/history/other.py", "change_type": "update"}]),
        ])
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        ro = next(m for m in ctx.matches if m.shard.shard_id == "shard-activity").recovery
        assert ro.recovery_attempt_number == 2
        assert ro.intervening_files == ["openshard/history/query.py"]
        # attempt 3 (unverified) touched the same file (+1); attempt 4 touched
        # a different file (not counted).
        assert ro.later_same_file_activity == 1

    def test_recovery_observation_only_on_relevant_matches(self, tmp_path: Path):
        """A Shard with its own genuine failure/recovery pattern is not
        exposed at all -- recovery observations never widen the retrieval,
        they only ever decorate an already-relevant match."""
        entries = [
            _entry("rotate kubernetes certificates", T1, shard_id="shard-irrelevant",
                   attempt_number=1, verification_passed=False),
            _entry("rotate kubernetes certificates retry", T3, shard_id="shard-irrelevant",
                   attempt_number=2, verification_passed=True),
        ]
        _write(tmp_path, entries)
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        assert ctx.matches == []

    def test_unrelated_shards_do_not_get_recovery_observations(self, tmp_path: Path):
        entries = _recovery_pair("shard-recover")
        entries += [
            _entry("rotate kubernetes certificates", T1, shard_id="shard-unrelated-recovery",
                   attempt_number=1, verification_passed=False),
            _entry("rotate kubernetes certificates retry", T3, shard_id="shard-unrelated-recovery",
                   attempt_number=2, verification_passed=True),
        ]
        _write(tmp_path, entries)
        ctx = relevant_context("terraform verification", repo_path=tmp_path)
        ids = [m.shard.shard_id for m in ctx.matches]
        assert "shard-recover" in ids
        assert "shard-unrelated-recovery" not in ids

    def test_repository_isolation_preserved(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair(
            "shard-recover", failed_kwargs={"repo_name": "alpha"}, recovery_kwargs={"repo_name": "alpha"},
        ))
        ctx = relevant_context("terraform verification", repo_path=tmp_path, repo="beta")
        assert ctx.matches == []

    def test_relevant_context_does_not_touch_memory_stores(self, tmp_path: Path):
        """Read-time only (PR11): deriving a RecoveryObservation must never
        write memory.jsonl (PR9 explicit feedback) or failure_memory.jsonl
        (routing signal store) -- there is no new persistence in this PR."""
        _write(tmp_path, _recovery_pair(
            "shard-recover",
            recovery_kwargs={"files_detail": [{"path": "a.py", "change_type": "update"}]},
        ))
        relevant_context("terraform verification", repo_path=tmp_path)
        assert not (tmp_path / ".openshard" / "memory.jsonl").exists()
        assert not (tmp_path / ".openshard" / "failure_memory.jsonl").exists()

    def test_limit_and_no_match_behavior_preserved(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair(
            "shard-recover",
            recovery_kwargs={"files_detail": [{"path": "a.py", "change_type": "update"}]},
        ))
        assert relevant_context("terraform verification", repo_path=tmp_path, limit=0).matches == []
        assert relevant_context("zzz-nonexistent-zzz", repo_path=tmp_path).matches == []


# Verification-state vocabulary for _chain(): each attempt k also touches
# "f<k>.py" so intervening_files reveals exactly which attempts were counted.
_UNVERIFIED = {"verification_attempted": False, "verification_passed": None}
_STATE_KWARGS = {
    "fail": {"verification_passed": False},
    "pass": {"verification_passed": True},
    "none": _UNVERIFIED,
    # Conflicting signals on one attempt: the boolean says passed, an
    # independent review check says failed.
    "conflict": {
        "verification_passed": True,
        "review_checks": [{"name": "terraform_validate", "status": "failed", "summary": "x"}],
    },
}


def _chain(shard_id: str, states: list[str], **extra) -> list[dict]:
    """A Shard whose attempts, in append order, have the given verification
    states. Timestamps increase with append order; tasks always match
    "terraform verification"."""
    entries = []
    for k, state in enumerate(states, start=1):
        ts = f"2026-08-01T{k // 60:02d}:{k % 60:02d}:00Z"
        entries.append(_entry(
            f"terraform verification attempt {k}", ts, shard_id=shard_id, attempt_number=k,
            files_detail=[{"path": f"f{k}.py", "change_type": "update"}],
            **_STATE_KWARGS[state], **extra,
        ))
    return entries


def _recovery_of(tmp_path: Path, shard_id: str):
    ctx = relevant_context("terraform verification", repo_path=tmp_path)
    return next(m for m in ctx.matches if m.shard.shard_id == shard_id).recovery


class TestRecoverySemantics:
    """The latest-state rule, case by case (see the query module docstring)."""

    def test_fail_pass(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["fail", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (1, 2)
        assert ro.intervening_files == ["f2.py"]

    def test_fail_fail_pass_uses_most_recent_failure(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["fail", "fail", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (2, 3)
        assert ro.intervening_files == ["f3.py"]

    def test_fail_pass_pass_runs_to_latest_pass(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["fail", "pass", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (1, 3)
        assert ro.intervening_files == ["f2.py", "f3.py"]

    def test_fail_pass_fail_relapse_yields_nothing(self, tmp_path: Path):
        """The latest verified state is failed: reporting the earlier
        recovery would misrepresent the Shard's current state."""
        _write(tmp_path, _chain("s", ["fail", "pass", "fail"]))
        assert _recovery_of(tmp_path, "s") is None

    def test_fail_pass_fail_pass_reports_latest_sequence_only(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["fail", "pass", "fail", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (3, 4)
        assert ro.intervening_files == ["f4.py"]

    def test_pass_fail_pass(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["pass", "fail", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (2, 3)

    def test_pass_only_chain_yields_nothing(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["pass", "pass"]))
        assert _recovery_of(tmp_path, "s") is None

    def test_unverified_attempts_are_not_evidence(self, tmp_path: Path):
        """An attempt with no verification result is neither a pass nor a
        failure: it neither closes nor blocks a pair, but its activity is
        still reported when it sits between the failure and the pass."""
        _write(tmp_path, _chain("s", ["fail", "none", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (1, 3)
        assert ro.intervening_files == ["f2.py", "f3.py"]

    def test_unverified_tail_does_not_hide_or_fabricate_state(self, tmp_path: Path):
        _write(tmp_path, _chain("s", ["fail", "pass", "none"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (1, 2)
        assert ro.later_same_file_activity == 0  # f3.py is not one of the same files
        _write(tmp_path, _chain("t", ["fail", "none", "none"]))
        assert _recovery_of(tmp_path, "t") is None
        _write(tmp_path, _chain("u", ["none", "none"]))
        assert _recovery_of(tmp_path, "u") is None

    def test_conflicting_signals_on_one_attempt_count_as_failed(self, tmp_path: Path):
        """verification_passed=True plus a failed review check is a failure
        (failed wins): it can open a pair as the failure but never close
        one as the pass."""
        _write(tmp_path, _chain("s", ["conflict", "pass"]))
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (1, 2)
        _write(tmp_path, _chain("t", ["fail", "conflict"]))
        assert _recovery_of(tmp_path, "t") is None
        _write(tmp_path, _chain("u", ["fail", "pass", "conflict"]))
        assert _recovery_of(tmp_path, "u") is None

    def test_append_order_is_chronology_even_when_timestamps_disagree(self, tmp_path: Path):
        """The repository's established ordering: runs.jsonl append order is
        the attempt chronology (as _bounded_attempts and _resolution_signal
        already assume). A later-stamped failure appended *before* an
        earlier-stamped pass is therefore the earlier attempt."""
        _write(tmp_path, [
            _entry("terraform verification attempt", T3, shard_id="s", attempt_number=1,
                   verification_passed=False),
            _entry("terraform verification attempt", T1, shard_id="s", attempt_number=2,
                   verification_passed=True),
        ])
        ro = _recovery_of(tmp_path, "s")
        assert (ro.failed_attempt_number, ro.recovery_attempt_number) == (1, 2)
        assert (ro.failed_timestamp, ro.recovery_timestamp) == (T3, T1)
        # Reversed append order, same records: now the pass comes first and
        # the latest state is the failure -> no observation.
        _write(tmp_path, [
            _entry("terraform verification attempt", T1, shard_id="s", attempt_number=2,
                   verification_passed=True),
            _entry("terraform verification attempt", T3, shard_id="s", attempt_number=1,
                   verification_passed=False),
        ])
        assert _recovery_of(tmp_path, "s") is None


class TestRecoveryBoundedWork:
    @staticmethod
    def _long_chain(n: int) -> list[dict]:
        """n attempts: the first attempt inside the recovery window fails,
        the last passes, everything else is unverified -- the shape that
        maximises intervening work for a given window size."""
        from openshard.history.query import _MAX_RECOVERY_WINDOW

        states = ["none"] * n
        states[n - _MAX_RECOVERY_WINDOW] = "fail"
        states[-1] = "pass"
        return _chain("s", states)

    def test_work_does_not_grow_with_attempt_chain_length(self, tmp_path: Path):
        from openshard.history import query as q

        counts: dict[int, tuple[int, int]] = {}
        for n in (10, 60):
            _write(tmp_path, self._long_chain(n))
            with patch.object(q, "events_from_entry", wraps=q.events_from_entry) as ev_spy, \
                 patch.object(q, "build_shard_receipt", wraps=q.build_shard_receipt) as rc_spy:
                ro = _recovery_of(tmp_path, "s")
            assert ro is not None
            assert ro.recovery_attempt_number == n
            assert ro.failed_attempt_number == n - q._MAX_RECOVERY_WINDOW + 1
            counts[n] = (ev_spy.call_count, rc_spy.call_count)
        # Intervening projections are bounded by the window, and receipt
        # builds are exactly what _build_relevant_match already did before
        # PR11 (latest + bounded attempt history) -- none added by recovery.
        assert counts[10] == counts[60]
        assert counts[60][0] <= q._MAX_RECOVERY_WINDOW - 1

    def test_pair_entirely_before_the_window_is_not_reported(self, tmp_path: Path):
        """Missing evidence stays missing: a recovery older than the window
        is not reported, and nothing is synthesized in its place."""
        from openshard.history.query import _MAX_RECOVERY_WINDOW

        states = ["fail", "pass"] + ["none"] * _MAX_RECOVERY_WINDOW
        _write(tmp_path, _chain("s", states))
        assert _recovery_of(tmp_path, "s") is None

    def test_intervening_lists_are_capped(self, tmp_path: Path):
        from openshard.history.query import _MAX_RECOVERY_FILES, _MAX_RECOVERY_TOOLS

        many_files = [{"path": f"pkg/m{i}.py", "change_type": "update"} for i in range(30)]
        many_tools = [_tool_event(f"Tool{i}", f"e{i}") for i in range(30)]
        _write(tmp_path, _recovery_pair(
            "s", recovery_kwargs={"files_detail": many_files, "events": many_tools},
        ))
        ro = _recovery_of(tmp_path, "s")
        assert len(ro.intervening_files) == _MAX_RECOVERY_FILES
        assert len(ro.intervening_tools) == _MAX_RECOVERY_TOOLS


class TestRecoveryNativeToolEvidence:
    def test_native_tool_trace_yields_intervening_tools(self, tmp_path: Path):
        """Regression: the real native path (tool_trace -> _build_native_events
        -> embedded ``events``) must expose tool identity to recovery
        observations, exactly like the Claude hooks path does."""
        from openshard.run._pipeline_helpers import _build_native_events

        failed, recovery = _recovery_pair("s")
        recovery["tool_trace"] = [
            {"tool": "read_file", "ok": True, "approved": True, "output_chars": 12, "error": None},
            {"tool": "write_file", "ok": True, "approved": True, "output_chars": 0, "error": None},
            {"tool": "read_file", "ok": True, "approved": True, "output_chars": 40, "error": None},
            {"tool": "run_verification", "ok": True, "approved": True, "output_chars": 3, "error": None},
        ]
        recovery["events"] = _build_native_events(recovery, [], True, True, False)
        failed["events"] = _build_native_events(failed, [], True, False, False)
        _write(tmp_path, [failed, recovery])
        ro = _recovery_of(tmp_path, "s")
        assert ro.intervening_tools == ["read_file", "write_file", "run_verification"]

    def test_native_tool_events_without_structured_identity_are_skipped(self, tmp_path: Path):
        """A native record written before metadata["tool"] existed has no
        structured identity; its free-text ``action`` is never parsed."""
        failed, recovery = _recovery_pair("s")
        legacy_event = _tool_event("ignored", "e-legacy")
        legacy_event["metadata"] = {"approved": True, "output_chars": 5}
        legacy_event["action"] = "tool read_file"
        recovery["events"] = [legacy_event]
        _write(tmp_path, [failed, recovery])
        assert _recovery_of(tmp_path, "s").intervening_tools == []


# ---------------------------------------------------------------------------
# MCP tool: registration and call
# ---------------------------------------------------------------------------


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    return result.content, result.structured_content


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
        assert "task" in tool.input_schema.get("required", [])

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

    def test_recovery_is_none_when_shard_has_no_recovery(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        failed_match = next(m for m in structured["matches"] if m["shard_id"] == "shard-failed")
        assert failed_match["recovery"] is None

    def test_recovery_dict_is_populated_for_a_recovered_shard(self, tmp_path: Path):
        _write(tmp_path, _recovery_pair(
            "shard-recover",
            recovery_kwargs={"files_detail": [{"path": "openshard/history/query.py", "change_type": "update"}]},
        ))
        server = build_server(repo_path=tmp_path)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        match = next(m for m in structured["matches"] if m["shard_id"] == "shard-recover")
        assert match["recovery"] == {
            "shard_id": "shard-recover",
            "failed_attempt_number": 1,
            "recovery_attempt_number": 2,
            "failed_run_id": T1,
            "recovery_run_id": T3,
            "failed_timestamp": T1,
            "recovery_timestamp": T3,
            "failure_status": "failed",
            "failure_detail": None,
            "recovery_status": "passed",
            "recovery_detail": None,
            "intervening_files": ["openshard/history/query.py"],
            "intervening_tools": [],
            "later_same_file_activity": 0,
        }

    def test_match_fields_are_bounded(self, scenario: Path):
        server = build_server(repo_path=scenario)
        _, structured = _call(server, "relevant_context", {"task": "terraform verification"})
        match = structured["matches"][0]
        assert set(match) == {
            "shard_id", "created_at", "task_short", "task_full", "agent", "origin", "capture_depth",
            "score", "why_relevant", "status", "verification_status", "verification_reason",
            "result", "repo", "files", "findings", "attempts", "recovery",
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
