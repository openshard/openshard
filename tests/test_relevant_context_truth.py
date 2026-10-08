"""What an agent is told about prior work (``relevant_context``) reads the shared verification truth.

The context block is the input to every later run, so it must carry the
same claim as the Receipt: an agent-reported pass is ``unknown`` and named
as the agent's claim, a later ``openshard verify`` re-run or CI verdict
describes the current outcome, and a completed hook turn is a turn status,
not a verified result.
"""
from __future__ import annotations

from pathlib import Path

from openshard.history.query import relevant_context
from openshard.verification import post_session as ps
from tests.test_cli_visibility import HOOKS_FULL, NATIVE_PASSED, _invoke, _ok, _repo

HEAD = "59f31c55f399" + "0" * 28

# A Claude Code hook session whose agent ran a check and reported it passed.
HOOKS_AGENT_REPORTED = {
    **HOOKS_FULL,
    "shard_id": "shard-hooks-agent", "run_id": HOOKS_FULL["run_id"] + "-agent",
    "task": "fix the greet function so greet('x') returns 'Hello x!'",
    "verification_attempted": True, "verification_passed": True,
    "verification": {
        "version": 1, "status": "passed", "source": "agent_reported", "observation_mode": "hook_tool_event",
        "checks_attempted": 1, "checks_passed": 1, "checks_failed": 0, "checks_skipped": 0,
        "checks": [{"name": "Bash: python -m pytest -q", "kind": "test", "status": "passed", "exit_code": 0}],
        "reason": "The agent's hook reported the check command outcome(s); OpenShard did not run them.",
        "complete": True, "incomplete_reasons": [],
    },
}


def _attest(repo: Path, entry: dict, outcome: str) -> None:
    planned = ps.plan_checks(repo, {"verification_commands": [["python", "-m", "pytest"]]}, entry)
    tree = ps.TreeState(head=HEAD, dirty=False, tracked_dirty=False)
    ps.record_attestation(repo, ps.build_attestation(
        entry, [ps.CheckRun(planned[0], outcome, exit_code=0 if outcome == "passed" else 1)],
        before=tree, after=tree, started_at="2026-10-08T07:15:00Z", completed_at="2026-10-08T07:15:01Z",
    ))


def _match(repo: Path):
    ctx = relevant_context("fix the greet function", repo_path=repo)
    assert ctx.matches, ctx.context_text
    return ctx, ctx.matches[0]


class TestAgentReportedClaim:
    def test_an_agent_reported_pass_is_unknown_and_named_as_the_claim(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_AGENT_REPORTED])
        ctx, m = _match(repo)
        assert m.status == "Turn completed (unverified)"
        assert m.verification_status == "unknown"
        assert m.verification_reason == "Not verified by OpenShard (agent reported 1/1 passed)"
        assert "Verification: unknown — Not verified by OpenShard (agent reported 1/1 passed)" in ctx.context_text
        assert "Verification: passed" not in ctx.context_text
        assert "Status: Passed" not in ctx.context_text
        assert m.attempts[0].verification_status == "unknown"
        out = _ok(_invoke(["context", "fix", "the", "greet", "function"], repo))
        assert "verification: unknown" in out and "agent reported 1/1 passed" in out
        assert "verification: passed" not in out


class TestLaterEvidence:
    def test_an_openshard_rerun_that_passed_is_the_current_claim(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_AGENT_REPORTED])
        _attest(repo, HOOKS_AGENT_REPORTED, "passed")
        ctx, m = _match(repo)
        assert m.status == "Turn completed (verified later: passed, OpenShard re-run)"
        assert m.verification_status == "passed"
        assert m.verification_reason == (
            "Passed (OpenShard re-ran the check(s): 1/1 passed @ 59f31c55f399); the agent had reported passed"
        )
        assert "Verification: passed — Passed (OpenShard re-ran the check(s): 1/1 passed @ 59f31c55f399)" in (
            ctx.context_text
        )

    def test_a_failed_rerun_is_never_hidden_by_the_agents_claim(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_AGENT_REPORTED])
        _attest(repo, HOOKS_AGENT_REPORTED, "failed")
        ctx, m = _match(repo)
        assert m.verification_status == "failed"
        assert m.status == "Turn completed (verified later: failed, OpenShard re-run)"
        assert "Verification: failed — Failed (OpenShard re-ran the check(s): 0/1 passed @ 59f31c55f399)" in (
            ctx.context_text
        )


class TestUnchangedCases:
    def test_a_hook_session_without_checks_still_reads_not_run(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        ctx = relevant_context("refactor auth middleware", repo_path=repo)
        m = ctx.matches[0]
        assert m.status == "Turn completed (unverified)"
        assert m.verification_status == "not_run"
        assert "Verification: not_run — Not run (no check observed)" in ctx.context_text

    def test_a_native_run_openshard_verified_keeps_passed(self, tmp_path: Path):
        repo = _repo(tmp_path, [NATIVE_PASSED])
        ctx = relevant_context("add JWT auth middleware", repo_path=repo)
        m = ctx.matches[0]
        assert m.status == "Passed"
        assert m.verification_status == "passed"
        assert "Verification: passed — Passed (OpenShard ran the check(s)" in ctx.context_text
