"""Verification hardening: later evidence strengthens a Shard's current state, history stays intact.

An external agent's Receipt records what was known when the session ended
(often only the agent's own account). These tests pin the path from there to
a green state that is honest about its source:

    agent reports -> OpenShard re-runs the checks -> CI reports on the exact commit

and the rules that keep it truthful: the stored Receipt is never rewritten,
weak evidence is never promoted, a failure is never hidden by a pass, CI
from another commit or for a dirty tree is never attached, and unsafe or
approval-gated commands stay gated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import (
    buffer_path,
    handle_claude_hook,
    handle_hook,
    sweep_stale_buffers,
)
from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt, checks_label
from openshard.history.shard_hash import verify_shard_hash
from openshard.history.verification_truth import (
    BASIS_CI,
    BASIS_POST_SESSION,
    BASIS_SESSION,
    counts_phrase,
    interpret_evidence,
    interpret_receipt,
    verification_label,
)
from openshard.history.verification_view import (
    WORK_COMPLETED,
    WORK_ENDED_WITHOUT_FINAL_EVENT,
    build_verification_view,
    render_verification_view,
)
from openshard.verification import auto
from openshard.verification import ci_evidence as ci
from openshard.verification import post_session as ps
from tests.capture_fixtures import _make_repo

SID = "33333333-4444-4555-8666-777777777777"
SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "hardening repo")


def _runs(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _claude(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _bash(command: str, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, **extra}


def _claude_session(repo: Path, *calls: tuple[str, dict], end: bool = True) -> dict:
    _claude(repo, "UserPromptSubmit", prompt="fix the bug and run the tests")
    for event, fields in calls:
        _claude(repo, event, **fields)
    _claude(repo, "Stop")
    if end:
        _claude(repo, "SessionEnd", reason="exit")
    return _runs(repo)[-1]


def _check(name: str, safety: str = "safe") -> ps.PlannedCheck:
    return ps.PlannedCheck(name=name, argv=[name], kind="test", origin="contract", safety=safety, reason="")


def _rerun(entry: dict, statuses: list[str], *, sha: str | None = SHA_A, at: str = "2026-09-30T10:00:00Z") -> dict:
    """An ``openshard verify`` attestation with the given per-check outcomes."""
    results = [
        ps.CheckRun(_check(f"check-{i}"), status, exit_code={"passed": 0, "failed": 1}.get(status))
        for i, status in enumerate(statuses)
    ]
    tree = ps.TreeState(head=sha or SHA_A, dirty=sha is None, tracked_dirty=sha is None)
    return ps.build_attestation(entry, results, before=tree, after=tree, started_at=at, completed_at=at)


def _ci_runs(sha: str, *conclusions: str, status: str = "completed") -> list[dict]:
    return [
        {"name": f"job-{i}", "head_sha": sha, "status": status, "conclusion": c, "started_at": f"2026-09-30T11:0{i}:00Z"}
        for i, c in enumerate(conclusions)
    ]


def _ci_attestation(entry: dict, sha: str, *conclusions: str, at: str = "2026-09-30T12:00:00Z") -> dict:
    result = ci.classify_check_runs(_ci_runs(sha, *conclusions), sha)
    att = ci.build_ci_attestation(entry, result, ci.CITarget(sha=sha, binding=ci.BINDING_RERUN), created_at=at)
    assert att is not None
    return att


def _receipt(entry: dict, *attestations: dict):
    return build_shard_receipt(entry, 0, post_session_verification=ps.latest_for_entry(entry, list(attestations)))


# ---------------------------------------------------------------------------
# What the session itself recorded: four separate facts
# ---------------------------------------------------------------------------


class TestSessionRecordAlone:
    def test_agent_reported_pass_is_shown_as_passed_but_never_as_confirmed(self, repo):
        entry = _claude_session(repo, ("PostToolUse", _bash("pytest -q")))
        receipt = _receipt(entry)
        truth = interpret_receipt(receipt)
        view = build_verification_view(receipt)
        assert view["verification"]["headline"] == "Passed"  # ordinary case: a claim
        assert view["verification"]["confirmed_pass"] is False
        assert view["evidence"]["source"] == "agent_reported"
        assert "not verified by OpenShard" in view["evidence"]["label"]
        # Nothing that scores or gates treats the claim as a pass.
        assert truth.effective_status == "unknown" and truth.basis == BASIS_SESSION
        assert view["work"]["state"] == WORK_COMPLETED
        assert view["capture"]["label"] == "Partial" and view["capture"]["status"] == "complete"  # hooks only

    def test_agent_reported_failure_names_the_failed_check(self, repo):
        entry = _claude_session(repo, ("PostToolUseFailure", _bash("pytest -q", error="Exit code 1\nRAW OUTPUT")))
        view = build_verification_view(_receipt(entry))
        assert view["verification"]["headline"] == "Failed"
        assert view["verification"]["checks_failed"] == 1
        assert view["verification"]["failed_checks"] == ["Bash: pytest -q"]
        assert view["evidence"]["source"] == "agent_reported"
        assert "RAW OUTPUT" not in json.dumps(view)

    def test_counts_name_passed_failed_and_unknown_separately(self, repo):
        assert counts_phrase(19, 4, 50) == "19 passed, 4 failed, 27 unknown"
        assert counts_phrase(50, 0, 50) == "50/50 passed"
        assert counts_phrase(None, None, None) == ""
        entry = _claude_session(
            repo,
            ("PostToolUse", _bash("pytest -q tests/a")),
            ("PostToolUseFailure", _bash("pytest -q tests/b", error="Exit code 1")),
            ("PostToolUse", _bash("pytest -q tests/c", tool_input={"command": "pytest -q tests/c",
                                                                   "run_in_background": True})),
        )
        receipt = _receipt(entry)
        assert checks_label(receipt) == "1 passed, 1 failed, 1 unknown (agent-reported)"
        assert receipt.checks_display == "1/3 passed"  # the stored/synced string is unchanged

    def test_tool_failures_are_reported_apart_from_check_failures(self, repo):
        entry = _claude_session(
            repo,
            ("PostToolUseFailure", {"tool_name": "Read", "tool_input": {"file_path": "missing.txt"}, "error": "nope"}),
            ("PostToolUse", _bash("pytest -q")),
        )
        view = build_verification_view(_receipt(entry))
        assert view["activity"] == {"tool_calls": 2, "tool_failures": 1}
        assert view["verification"]["checks_failed"] == 0  # the failed Read is not a failed check
        text = "\n".join(render_verification_view(view))
        assert "2 call(s), 1 tool failure(s) (not verification)" in text

    def test_codex_exposes_no_outcome_and_cursor_reports_an_exit_code(self, repo):
        for event, fields in (
            ("UserPromptSubmit", {"prompt": "test it"}),
            ("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
            ("Stop", {}),
        ):
            handle_hook({"session_id": SID, "hook_event_name": event, "cwd": str(repo), "model": "gpt-5-codex",
                         **fields}, env={}, agent="codex")
        codex = build_verification_view(_receipt(_runs(repo)[-1]))
        assert codex["verification"]["state"] == "attempted_unverified"
        assert codex["verification"]["confirmed_pass"] is False

        cursor_sid = SID.replace("3", "5", 1)
        for doc in (
            {"hook_event_name": "beforeSubmitPrompt", "prompt": "test it"},
            {"hook_event_name": "postToolUse", "tool_name": "Shell", "tool_input": {"command": "npm test"},
             "tool_output": json.dumps({"exitCode": 0})},
            {"hook_event_name": "stop", "status": "completed"},
        ):
            handle_hook({"conversation_id": cursor_sid, "workspace_roots": [str(repo)], "model": "m", **doc},
                        env={}, agent="cursor")
        cursor = build_verification_view(_receipt(_runs(repo)[-1]))
        assert cursor["verification"]["state"] == "agent_reported_passed"
        assert cursor["evidence"]["source"] == "agent_reported"


# ---------------------------------------------------------------------------
# Session finalisation
# ---------------------------------------------------------------------------


class TestSessionFinalisation:
    def test_missing_end_hook_is_closed_by_the_sweep_without_a_fabricated_end(self, repo):
        entry = _claude_session(repo, ("PostToolUse", _bash("pytest -q")), end=False)
        assert entry["capture"]["session_end_observed"] is False
        assert build_verification_view(_receipt(entry))["work"]["label"] == "Turn completed (session still open)"

        assert sweep_stale_buffers(repo.resolve(), max_age_seconds=0) == [SID]
        assert not buffer_path(repo.resolve(), SID).exists()
        swept = _runs(repo)[-1]
        assert swept["capture"]["session_end_observed"] is False  # no clean end is invented
        assert not any(e.get("event_type") == "run.completed" for e in swept["events"])
        view = build_verification_view(_receipt(swept))
        assert view["work"]["state"] == WORK_ENDED_WITHOUT_FINAL_EVENT
        assert view["work"]["label"] == "Session ended without final event"
        assert view["capture"]["label"] == "Partial" and "session end was not observed" in view["capture"]["gaps"]

    def test_partial_capture_does_not_grey_out_an_observed_pass(self, repo):
        _claude_session(repo, ("PostToolUse", _bash("pytest -q")), end=False)
        sweep_stale_buffers(repo.resolve(), max_age_seconds=0)
        entry = _runs(repo)[-1]
        view = build_verification_view(_receipt(entry, _rerun(entry, ["passed"] * 3)))
        assert view["capture"]["label"] == "Partial"
        assert view["verification"]["headline"] == "PASSED" and view["verification"]["confirmed_pass"] is True
        assert view["verification"]["counts_label"] == "3/3 passed"
        assert view["evidence"]["label"] == "OpenShard verified" and view["evidence"]["artifact_sha"] == SHA_A

    def test_partial_capture_with_a_failed_rerun_is_failed(self, repo):
        _claude_session(repo, ("PostToolUse", _bash("pytest -q")), end=False)
        sweep_stale_buffers(repo.resolve(), max_age_seconds=0)
        entry = _runs(repo)[-1]
        view = build_verification_view(_receipt(entry, _rerun(entry, ["passed", "failed"])))
        assert view["capture"]["label"] == "Partial" and view["verification"]["headline"] == "FAILED"
        assert view["verification"]["counts_label"] == "1 passed, 1 failed"
        assert view["verification"]["failed_checks"] == ["check-1"]
        assert view["original"] == {"status": "passed", "source": "agent_reported", "counts_label": "1/1 passed"}

    def test_verify_closes_a_stale_session_before_verifying_it(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        _claude_session(repo, ("PostToolUse", _bash("pytest -q")), end=False)
        path = buffer_path(repo.resolve(), SID)
        buf = json.loads(path.read_text(encoding="utf-8"))
        buf["last_activity_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(buf), encoding="utf-8")
        assert CliRunner().invoke(cli, ["verify", "--dry-run", "--json"]).exit_code == 0
        assert not path.exists()
        reasons = _runs(repo)[-1]["capture"]["completeness"]["reasons"]
        assert [r["kind"] for r in reasons] == ["session_end_not_observed"]


# ---------------------------------------------------------------------------
# Later evidence: the upgrade path and its precedence
# ---------------------------------------------------------------------------


class TestEvidenceUpgrades:
    @pytest.fixture
    def failed_entry(self, repo) -> dict:
        return _claude_session(repo, ("PostToolUseFailure", _bash("pytest -q", error="Exit code 1")))

    def test_t1_agent_failed_t2_rerun_passes_t3_ci_passes(self, repo, failed_entry):
        entry = failed_entry
        stored = (repo / ".openshard" / "runs.jsonl").read_bytes()

        t1 = interpret_receipt(_receipt(entry))
        assert (t1.state, t1.authority, t1.basis) == ("agent_reported_failed", "agent_reported", BASIS_SESSION)

        rerun = _rerun(entry, ["passed"] * 8)
        t2 = interpret_receipt(_receipt(entry, rerun))
        assert (t2.state, t2.authority, t2.basis) == ("verified_passed", "directly_observed", BASIS_POST_SESSION)
        assert (t2.checks_passed, t2.checks_attempted) == (8, 8)

        receipt = _receipt(entry, rerun, _ci_attestation(entry, SHA_A, "success", "success", "skipped"))
        t3 = interpret_receipt(receipt)
        assert (t3.state, t3.authority, t3.basis) == ("verified_passed", "independently_verified", BASIS_CI)
        assert t3.artifact_sha == SHA_A and t3.effective_status == "passed"
        assert verification_label(t3) == (
            f"Passed (independent CI: 2/2 passed @ {SHA_A[:12]}); the agent had reported failed"
        )
        # The earlier states are all still there, oldest first.
        assert [(h["kind"], h["source"], h["status"]) for h in t3.history] == [
            ("session", "agent_reported", "failed"),
            ("rerun", "directly_observed", "passed"),
            ("ci", "independently_verified", "passed"),
        ]
        assert (t3.claim_status, t3.post_session_status) == ("failed", "passed")

        view = build_verification_view(receipt)
        assert view["verification"]["headline"] == "VERIFIED"
        assert view["evidence"]["label"] == "Independent CI" and view["evidence"]["artifact_sha"] == SHA_A
        assert view["original"]["status"] == "failed" and view["original"]["source"] == "agent_reported"
        text = "\n".join(render_verification_view(view))
        assert f"Independent CI, commit {SHA_A[:12]}" in text and "Original      Failed (agent reported" in text

        # Nothing above touched the stored Receipt.
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == stored
        assert receipt.verification["status"] == "failed" and receipt.verification["source"] == "agent_reported"
        assert verify_shard_hash(entry)["status"] in ("valid", "missing")

    def test_ci_failure_on_the_same_commit(self, failed_entry):
        entry = failed_entry
        truth = interpret_receipt(_receipt(
            entry, _rerun(entry, ["passed"]), _ci_attestation(entry, SHA_A, "success", "failure"),
        ))
        assert (truth.state, truth.authority) == ("verified_failed", "independently_verified")
        assert truth.failed_checks == ("job-1",) and truth.effective_status == "failed"

    def test_a_weaker_pass_never_overrides_a_ci_failure_on_the_same_commit(self, failed_entry):
        entry = failed_entry
        truth = interpret_receipt(_receipt(
            entry,
            _ci_attestation(entry, SHA_A, "failure", at="2026-09-30T09:00:00Z"),
            _rerun(entry, ["passed"], sha=SHA_A, at="2026-09-30T10:00:00Z"),
        ))
        assert (truth.state, truth.basis) == ("verified_failed", BASIS_CI)

    def test_a_later_failed_rerun_is_never_hidden_by_an_earlier_ci_pass(self, failed_entry):
        entry = failed_entry
        truth = interpret_receipt(_receipt(
            entry,
            _ci_attestation(entry, SHA_A, "success", at="2026-09-30T09:00:00Z"),
            _rerun(entry, ["failed"], sha=SHA_A, at="2026-09-30T10:00:00Z"),
        ))
        assert (truth.state, truth.authority, truth.basis) == ("verified_failed", "directly_observed", BASIS_POST_SESSION)

    def test_ci_for_an_older_commit_does_not_vouch_for_a_newer_rerun(self, failed_entry):
        entry = failed_entry
        truth = interpret_receipt(_receipt(
            entry,
            _ci_attestation(entry, SHA_A, "success", at="2026-09-30T09:00:00Z"),
            _rerun(entry, ["passed"], sha=SHA_B, at="2026-09-30T10:00:00Z"),
        ))
        assert (truth.authority, truth.basis, truth.artifact_sha) == ("directly_observed", BASIS_POST_SESSION, SHA_B)

    def test_evidence_that_concluded_nothing_overrides_nothing(self, failed_entry):
        entry = failed_entry
        cancelled = ci.build_ci_attestation(
            entry, ci.classify_check_runs(_ci_runs(SHA_A, "cancelled"), SHA_A),
            ci.CITarget(sha=SHA_A, binding=ci.BINDING_RERUN), created_at="2026-09-30T12:00:00Z",
        )
        assert cancelled is not None and cancelled["verification"]["status"] == "unknown"
        assert cancelled["verification"]["incomplete_reasons"] == ["ci_cancelled"]
        truth = interpret_receipt(_receipt(entry, _rerun(entry, ["skipped"]), cancelled))
        assert (truth.state, truth.basis) == ("agent_reported_failed", BASIS_SESSION)
        assert [h["kind"] for h in truth.history] == ["session", "rerun", "ci"]  # still listed

    def test_forged_ci_lines_are_never_promoted(self, failed_entry):
        entry = failed_entry
        real = _ci_attestation(entry, SHA_A, "success")
        not_ci_source = {**real, "verification": {**real["verification"], "source": "agent_reported"}}
        no_commit = {**real, "verification": {**real["verification"], "artifact_sha": None}}
        rerun_claiming_ci = {**real, "kind": ps.KIND_POST_SESSION}
        for forged in (not_ci_source, no_commit, rerun_claiming_ci):
            truth = interpret_receipt(_receipt(entry, forged))
            assert truth.state == "agent_reported_failed", forged
        assert ps.summarize_attestation(no_commit)["verification"]["status"] == "unknown"

    def test_the_pre_history_single_summary_shape_still_reads(self, failed_entry):
        legacy = {k: v for k, v in ps.summarize_attestation(_rerun(failed_entry, ["passed"])).items() if k != "kind"}
        truth = interpret_evidence(failed_entry["verification"], post_session_verification=legacy)
        assert (truth.state, truth.basis) == ("verified_passed", BASIS_POST_SESSION)


# ---------------------------------------------------------------------------
# CI: exact-artifact binding
# ---------------------------------------------------------------------------


def _ancestor(answer: bool | None):
    return lambda a, b: answer


class TestCIBinding:
    def test_classification_of_each_ci_state(self):
        assert ci.classify_check_runs(_ci_runs(SHA_A, "success", "neutral"), SHA_A).outcome == ci.OUTCOME_PASSED
        assert ci.classify_check_runs(_ci_runs(SHA_A, "success", "timed_out"), SHA_A).outcome == ci.OUTCOME_FAILED
        assert ci.classify_check_runs(_ci_runs(SHA_A, "success", "cancelled"), SHA_A).outcome == ci.OUTCOME_CANCELLED
        pending = _ci_runs(SHA_A, "success") + _ci_runs(SHA_A, None, status="in_progress")[:1]
        pending[1]["name"] = "slow-job"
        assert ci.classify_check_runs(pending, SHA_A).outcome == ci.OUTCOME_PENDING
        # A failure is conclusive even while other jobs are still running.
        assert ci.classify_check_runs(pending + [
            {"name": "lint", "head_sha": SHA_A, "status": "completed", "conclusion": "failure"},
        ], SHA_A).outcome == ci.OUTCOME_FAILED
        assert ci.classify_check_runs([], SHA_A).outcome == ci.OUTCOME_UNAVAILABLE
        assert ci.classify_check_runs(_ci_runs(SHA_A, "skipped"), SHA_A).outcome == ci.OUTCOME_UNAVAILABLE
        assert ci.classify_check_runs("garbage", SHA_A).outcome == ci.OUTCOME_UNAVAILABLE

    def test_runs_for_a_different_commit_are_never_counted(self):
        result = ci.classify_check_runs(_ci_runs(SHA_B, "success", "success"), SHA_A)
        assert result.outcome == ci.OUTCOME_UNAVAILABLE and result.runs == []
        assert "other commits" in result.detail
        assert ci.build_ci_attestation({}, result, ci.CITarget(sha=SHA_A), created_at="2026-09-30T12:00:00Z") is None
        # Even a passing verdict is refused when it is not for the target commit.
        other = ci.classify_check_runs(_ci_runs(SHA_B, "success"), SHA_B)
        assert ci.build_ci_attestation({}, other, ci.CITarget(sha=SHA_A), created_at="2026-09-30T12:00:00Z") is None

    def test_a_rerun_of_the_same_check_uses_only_its_newest_run(self):
        runs = [
            {"name": "tests", "head_sha": SHA_A, "status": "completed", "conclusion": "failure",
             "started_at": "2026-09-30T10:00:00Z"},
            {"name": "tests", "head_sha": SHA_A, "status": "completed", "conclusion": "success",
             "started_at": "2026-09-30T11:00:00Z"},
        ]
        assert ci.classify_check_runs(runs, SHA_A).outcome == ci.OUTCOME_PASSED

    def test_pending_and_unavailable_ci_record_nothing(self):
        target = ci.CITarget(sha=SHA_A, binding=ci.BINDING_CLEAN_HEAD)
        pending = ci.classify_check_runs(_ci_runs(SHA_A, None, status="queued"), SHA_A)
        assert ci.build_ci_attestation({}, pending, target, created_at="2026-09-30T12:00:00Z") is None
        unavailable = ci.classify_check_runs([], SHA_A)
        assert ci.build_ci_attestation({}, unavailable, target, created_at="2026-09-30T12:00:00Z") is None

    def test_dirty_tree_cannot_be_artifact_bound(self):
        dirty = ps.TreeState(head=SHA_A, dirty=True, tracked_dirty=True)
        target = ci.resolve_ci_target({}, [], dirty, is_ancestor=_ancestor(True))
        assert target.sha is None and target.refusal == ci.REFUSAL_DIRTY_TREE
        unknown = ps.TreeState(head=SHA_A, dirty=None)
        assert ci.resolve_ci_target({}, [], unknown, is_ancestor=_ancestor(True)).refusal == ci.REFUSAL_DIRTY_TREE
        assert ci.resolve_ci_target({}, [], ps.TreeState(head=None, dirty=False),
                                    is_ancestor=_ancestor(True)).refusal == ci.REFUSAL_NO_COMMIT

    def test_clean_head_must_contain_the_sessions_work(self):
        clean = ps.TreeState(head=SHA_B, dirty=False, tracked_dirty=False)
        ok = ci.resolve_ci_target({"git_head_commit_hash": SHA_A}, [], clean, is_ancestor=_ancestor(True))
        assert (ok.sha, ok.binding) == (SHA_B, ci.BINDING_CLEAN_HEAD)
        # HEAD is unrelated to where the session started.
        assert ci.resolve_ci_target({"git_head_commit_hash": SHA_A}, [], clean,
                                    is_ancestor=_ancestor(False)).refusal == ci.REFUSAL_NOT_DESCENDED
        # Still on the start commit although the session changed files: nothing was committed.
        entry = {"git_head_commit_hash": SHA_B, "files_updated": 2}
        assert ci.resolve_ci_target(entry, [], clean, is_ancestor=_ancestor(True)).refusal == (
            ci.REFUSAL_SESSION_CHANGES_NOT_COMMITTED
        )

    def test_commit_moved_after_a_bound_rerun_keeps_the_verified_commit(self):
        entry = {"receipt_id": "rcpt_x"}
        evidence = ps.evidence_for_entry(entry, [_rerun(entry, ["passed"], sha=SHA_A)])
        moved = ps.TreeState(head=SHA_B, dirty=True, tracked_dirty=True)
        target = ci.resolve_ci_target(entry, evidence, moved, is_ancestor=_ancestor(True))
        assert (target.sha, target.binding, target.head_moved) == (SHA_A, ci.BINDING_RERUN, True)
        # An unbound re-run (dirty tree) binds nothing.
        unbound = ps.evidence_for_entry(entry, [_rerun(entry, ["passed"], sha=None)])
        assert ci.resolve_ci_target(entry, unbound, moved, is_ancestor=_ancestor(True)).refusal == (
            ci.REFUSAL_DIRTY_TREE
        )

    def test_pr_head_moving_on_is_recorded_not_attached(self):
        entry = {"receipt_id": "rcpt_x"}
        result = ci.classify_check_runs(_ci_runs(SHA_A, "success"), SHA_A)
        att = ci.build_ci_attestation(entry, result, ci.CITarget(sha=SHA_A, binding=ci.BINDING_RERUN),
                                      created_at="2026-09-30T12:00:00Z", pr_head=SHA_B)
        assert att is not None and att["ci"]["pr_head_matches"] is False
        assert att["ci"]["commit"] == SHA_A and att["verification"]["artifact_sha"] == SHA_A
        assert att["raw_output_stored"] is False and "url" not in json.dumps(att)

    def test_fetch_is_read_only_and_never_raises(self, repo):
        calls: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = json.dumps({"total_count": 1, "check_runs": _ci_runs(SHA_A, "success")})

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return _Proc()

        runs, error = ci.fetch_github_check_runs(repo, SHA_A, runner=runner)
        assert error == "" and runs is not None and len(runs) == 1
        assert calls[0][:2] == ["gh", "api"] and calls[0][-1].endswith(f"/commits/{SHA_A}/check-runs?per_page=100")
        assert "-X" not in calls[0] and "--method" not in calls[0]

        def failing(argv, **kwargs):
            raise OSError("no gh")

        assert ci.fetch_github_check_runs(repo, SHA_A, runner=failing)[0] is None
        _Proc.stdout = "not json"
        assert ci.fetch_github_check_runs(repo, SHA_A, runner=runner)[0] is None


# ---------------------------------------------------------------------------
# Safety: what a post-session verification may run
# ---------------------------------------------------------------------------


class TestVerificationSafety:
    def test_unsafe_command_stays_blocked_even_with_approval(self, repo):
        planned = ps.plan_checks(repo, {"verification_commands": [["rm", "-rf", "build"], ["git", "push"]]}, None)
        assert [c.safety for c in planned] == ["blocked", "blocked"]
        ran: list[list[str]] = []
        results = ps.run_checks(planned, repo, approve=True, runner=lambda argv, **k: ran.append(argv))
        assert ran == [] and [r.status for r in results] == ["skipped", "skipped"]
        block = ps.build_attestation(
            {}, results, before=ps.TreeState(SHA_A, False), after=ps.TreeState(SHA_A, False, False),
            started_at="2026-09-30T10:00:00Z", completed_at="2026-09-30T10:00:01Z",
        )["verification"]
        assert block["status"] == "not_run"  # nothing ran, so nothing is claimed

    def test_approval_required_command_stays_gated_without_approval(self, repo):
        planned = ps.plan_checks(repo, {"verification_commands": [["make", "test"]]}, None)
        assert planned[0].safety == "needs_approval"
        ran: list[list[str]] = []
        results = ps.run_checks(planned, repo, approve=False, runner=lambda argv, **k: ran.append(argv))
        assert ran == [] and results[0].status == "skipped" and "needs approval" in results[0].note

    def test_a_skipped_only_rerun_does_not_turn_the_state_green(self, repo):
        entry = _claude_session(repo, ("PostToolUse", _bash("pytest -q")))
        truth = interpret_receipt(_receipt(entry, _rerun(entry, ["skipped"])))
        assert truth.state == "agent_reported_passed" and truth.effective_status == "unknown"


class TestAutoPostSessionVerify:
    def _enable(self, repo: Path, value: object = "safe") -> None:
        import yaml

        (repo / ".openshard").mkdir(exist_ok=True)
        (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({auto.CONFIG_KEY: value}), encoding="utf-8")

    def test_off_by_default_and_off_for_any_value_but_safe(self, repo):
        spawned: list = []
        entry = {"receipt_id": "rcpt_1"}
        assert auto.schedule_post_session_verify(repo, entry, spawner=lambda a, c: spawned.append(a) or True) is False
        for value in (True, "all", "approve", "off"):
            self._enable(repo, value)
            assert auto.auto_verify_enabled(repo, {}) is False, value
        assert spawned == []

    def test_enabled_starts_a_safe_only_verify_for_that_receipt(self, repo):
        self._enable(repo)
        spawned: list[tuple[list[str], Path]] = []
        started = auto.schedule_post_session_verify(
            repo, {"receipt_id": "rcpt_1"}, spawner=lambda a, c: spawned.append((a, c)) or True, env={},
        )
        assert started is True
        argv, cwd = spawned[0]
        assert argv[-4:] == ["verify", "--receipt", "rcpt_1", "--json"] and cwd == repo
        assert "--approve" not in argv and "--from-observed" not in argv
        assert auto.schedule_post_session_verify(repo, {"receipt_id": "rcpt_1"}, spawner=lambda a, c: True,
                                                 env={auto.ENV_DISABLE: "1"}) is False

    def test_session_end_triggers_it_once_and_a_turn_end_does_not(self, repo, monkeypatch):
        self._enable(repo)
        monkeypatch.delenv(auto.ENV_DISABLE, raising=False)
        spawned: list[list[str]] = []
        monkeypatch.setattr(auto, "_spawn_detached", lambda argv, cwd: spawned.append(argv) or True)
        _claude(repo, "UserPromptSubmit", prompt="fix it")
        _claude(repo, "PostToolUse", **_bash("pytest -q"))
        _claude(repo, "Stop")
        assert spawned == []
        _claude(repo, "SessionEnd", reason="exit")
        assert len(spawned) == 1 and spawned[0][-2] == _runs(repo)[-1]["receipt_id"]

    def test_stale_sweep_triggers_it_for_a_session_with_no_end_event(self, repo, monkeypatch):
        self._enable(repo)
        monkeypatch.delenv(auto.ENV_DISABLE, raising=False)
        spawned: list[list[str]] = []
        monkeypatch.setattr(auto, "_spawn_detached", lambda argv, cwd: spawned.append(argv) or True)
        _claude_session(repo, ("PostToolUse", _bash("pytest -q")), end=False)
        sweep_stale_buffers(repo.resolve(), max_age_seconds=0)
        assert len(spawned) == 1

    def test_a_spawn_failure_never_breaks_capture(self, repo, monkeypatch):
        self._enable(repo)
        monkeypatch.delenv(auto.ENV_DISABLE, raising=False)

        def boom(argv, cwd):
            raise OSError("cannot spawn")

        monkeypatch.setattr(auto, "_spawn_detached", boom)
        entry = _claude_session(repo, ("PostToolUse", _bash("pytest -q")))
        assert entry["capture"]["session_end_observed"] is True


# ---------------------------------------------------------------------------
# End to end through the CLI
# ---------------------------------------------------------------------------


class TestCliFlow:
    @pytest.fixture
    def session(self, repo, monkeypatch) -> Path:
        """Claude edits the repo, reports a failing test run, and the work is committed."""
        monkeypatch.chdir(repo)
        (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        _claude(repo, "UserPromptSubmit", prompt="add a test")
        _claude(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "test_ok.py")})
        _claude(repo, "PostToolUseFailure", **_bash("pytest -q", error="Exit code 1"))
        _claude(repo, "Stop")
        _claude(repo, "SessionEnd", reason="exit")
        subprocess.run(["git", "add", "test_ok.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "test"],
            cwd=repo, check=True, capture_output=True,
        )
        import yaml

        (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"verification_commands": [
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_ok.py"],
        ]}), encoding="utf-8")
        return repo

    def _head(self, repo: Path) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def _last(self) -> dict:
        return json.loads(CliRunner().invoke(cli, ["last", "--json"]).output)

    def _patch_ci(self, monkeypatch, runs: list | None, error: str = "", pr_head: str | None = None) -> None:
        monkeypatch.setattr(ci, "fetch_github_check_runs", lambda root, sha, **k: (runs, error))
        monkeypatch.setattr(ci, "fetch_github_pr_head", lambda root, **k: pr_head)

    def test_external_agent_run_goes_green_then_ci_verified_without_rewriting_the_receipt(self, session, monkeypatch):
        repo, head = session, self._head(session)
        stored = (repo / ".openshard" / "runs.jsonl").read_bytes()

        first = self._last()
        assert first["verification_truth"]["state"] == "agent_reported_failed"
        assert first["verification_view"]["verification"]["headline"] == "Failed"

        assert CliRunner().invoke(cli, ["verify", "--json"]).exit_code == 0
        second = self._last()
        assert second["verification_truth"]["state"] == "verified_passed"
        assert second["verification_truth"]["authority"] == "directly_observed"
        assert second["verification_view"]["verification"]["headline"] == "PASSED"
        assert second["verification_view"]["evidence"]["artifact_sha"] == head

        self._patch_ci(monkeypatch, _ci_runs(head, "success", "success"))
        result = CliRunner().invoke(cli, ["verify", "--ci", "--json", "--strict"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert (payload["status"], payload["outcome"], payload["commit"]) == ("ok", "passed", head)
        assert payload["binding"] == ci.BINDING_RERUN

        third = self._last()
        truth = third["verification_truth"]
        assert (truth["state"], truth["authority"], truth["basis"]) == (
            "verified_passed", "independently_verified", "ci")
        assert truth["artifact_sha"] == head
        assert [h["kind"] for h in truth["history"]] == ["session", "rerun", "ci"]
        assert third["verification_view"]["verification"]["headline"] == "VERIFIED"
        assert third["verification_view"]["original"]["status"] == "failed"
        # The Receipt is byte-identical and still says what the session knew.
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == stored
        stored_block = _runs(repo)[-1]["verification"]
        assert (stored_block["status"], stored_block["source"]) == ("failed", "agent_reported")
        assert truth["session_status"] == "failed" and truth["claim_source"] == "agent_reported"

        human = CliRunner().invoke(cli, ["last"]).output
        assert "Verification  VERIFIED (2/2 passed)" in human
        assert f"Evidence      Independent CI, commit {head[:12]}" in human
        assert "Original      Failed (agent reported: 0 passed, 1 failed)" in human
        assert "Evidence history (oldest first; nothing here was rewritten)" in human

    def test_ci_failure_is_attached_and_strict_exits_one(self, session, monkeypatch):
        head = self._head(session)
        self._patch_ci(monkeypatch, _ci_runs(head, "success", "failure"))
        result = CliRunner().invoke(cli, ["verify", "--ci", "--strict"])
        assert result.exit_code == 1 and "Failed: job-1" in result.output
        truth = self._last()["verification_truth"]
        assert (truth["state"], truth["authority"]) == ("verified_failed", "independently_verified")

    def test_ci_from_a_different_commit_is_not_attached(self, session, monkeypatch):
        self._patch_ci(monkeypatch, _ci_runs(SHA_B, "success", "success"))
        result = CliRunner().invoke(cli, ["verify", "--ci", "--json", "--strict"])
        assert result.exit_code == 2 and json.loads(result.output)["status"] == "ci_unavailable"
        assert not (session / ".openshard" / ps.ATTESTATIONS_FILENAME).exists()
        assert self._last()["verification_truth"]["state"] == "agent_reported_failed"

    @pytest.mark.parametrize(("runs", "error", "status"), [
        ([{"name": "tests", "status": "in_progress", "conclusion": None}], "", "ci_pending"),
        (None, "the GitHub CLI (gh) is not installed", "ci_unavailable"),
        ([], "", "ci_unavailable"),
    ])
    def test_pending_or_unavailable_ci_records_nothing(self, session, monkeypatch, runs, error, status):
        head = self._head(session)
        for run in runs or []:
            run["head_sha"] = head
        self._patch_ci(monkeypatch, runs, error)
        result = CliRunner().invoke(cli, ["verify", "--ci", "--json"])
        assert result.exit_code == 0 and json.loads(result.output)["status"] == status
        assert not (session / ".openshard" / ps.ATTESTATIONS_FILENAME).exists()

    def test_cancelled_ci_is_recorded_but_changes_nothing(self, session, monkeypatch):
        head = self._head(session)
        self._patch_ci(monkeypatch, _ci_runs(head, "cancelled"))
        result = CliRunner().invoke(cli, ["verify", "--ci", "--json", "--strict"])
        assert result.exit_code == 2 and json.loads(result.output)["outcome"] == "cancelled"
        truth = self._last()["verification_truth"]
        assert truth["state"] == "agent_reported_failed" and [h["kind"] for h in truth["history"]] == ["session", "ci"]

    def test_dirty_tree_refuses_ci_binding(self, session, monkeypatch):
        (session / "test_ok.py").write_text("def test_ok():\n    assert 1\n", encoding="utf-8")
        called: list = []
        monkeypatch.setattr(ci, "fetch_github_check_runs", lambda root, sha, **k: called.append(sha) or ([], ""))
        result = CliRunner().invoke(cli, ["verify", "--ci", "--json", "--strict"])
        payload = json.loads(result.output)
        assert result.exit_code == 2 and payload["status"] == "not_bound" and payload["refusal"] == "dirty_tree"
        assert called == []  # CI is not even asked

    def test_failed_rerun_stays_failed_and_ci_check_reads_the_current_state(self, session):
        import yaml

        assert CliRunner().invoke(cli, ["ci", "check", "--json"]).exit_code == 1  # agent-reported failure
        assert CliRunner().invoke(cli, ["verify", "--json"]).exit_code == 0
        passed = json.loads(CliRunner().invoke(cli, ["ci", "check", "--json"]).output)
        assert passed["checks"]["verification"] == "passed"

        (session / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
        (session / ".openshard" / "config.yml").write_text(yaml.safe_dump({"verification_commands": [
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_bad.py"],
        ]}), encoding="utf-8")
        assert CliRunner().invoke(cli, ["verify", "--json"]).exit_code == 0
        last = self._last()
        assert last["verification_truth"]["state"] == "verified_failed"
        assert last["verification_view"]["evidence"]["bound"] is False  # untracked file: not a commit
        (failed,) = last["verification_view"]["verification"]["failed_checks"]
        assert failed.startswith("python -m pytest -q") and str(session) not in failed  # path-free label
        assert CliRunner().invoke(cli, ["ci", "check", "--json"]).exit_code == 1
