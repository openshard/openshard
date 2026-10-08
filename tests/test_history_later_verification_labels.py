"""A later OpenShard re-run (or CI verdict) resolves the turn status and the history checks column.

The Receipt bytes are never rewritten; the attestation is joined at read
time. Before this, ``openshard last`` showed ``Status  Turn completed
(unverified)`` two rows above ``Verified  Passed (OpenShard re-ran the
check(s) ...)``, and ``openshard history`` rows kept saying ``(unverified) ·
checks: not run`` with no sign that OpenShard had verified the Shard.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from openshard.cli.visibility import checks_label, history_json_body, status_label
from openshard.history.locate import locate_history
from openshard.history.query import recent_shards
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.verification_truth import (
    BASIS_CI,
    STATE_VERIFIED_FAILED,
    STATE_VERIFIED_PASSED,
    checks_row_label,
    interpret_evidence,
    interpret_receipt,
    turn_status_label,
)
from openshard.verification import post_session as ps
from tests.test_cli_visibility import HOOKS_FULL, _invoke, _ok, _repo

HEAD = "59f31c55f399" + "0" * 28


def _attest(repo: Path, entry: dict, outcome: str, *, bound: bool = True) -> None:
    planned = ps.plan_checks(repo, {"verification_commands": [["python", "-m", "pytest"]]}, entry)
    tree = ps.TreeState(head=HEAD, dirty=not bound, tracked_dirty=not bound)
    ps.record_attestation(repo, ps.build_attestation(
        entry, [ps.CheckRun(planned[0], outcome, exit_code=0 if outcome == "passed" else 1)],
        before=tree, after=tree, started_at="2026-10-08T07:15:00Z", completed_at="2026-10-08T07:15:01Z",
    ))


def _receipt(repo: Path):
    return recent_shards(repo_path=repo).items[0].receipt


class TestWithoutLaterEvidence:
    def test_turn_status_and_checks_are_unchanged(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        receipt = _receipt(repo)
        assert status_label(receipt) == "Turn completed (unverified)"
        assert checks_label(receipt) == "not run"
        out = _ok(_invoke(["last"], repo))
        assert "Status      Turn completed (unverified)" in out
        assert "Verified    Not run (no check observed)" in out


class TestAfterOpenShardReRun:
    def test_passed_rerun_resolves_the_turn_status_and_history_row(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        _attest(repo, HOOKS_FULL, "passed")
        receipt = _receipt(repo)
        truth = interpret_receipt(receipt)
        assert truth.state == STATE_VERIFIED_PASSED and truth.basis == "post_session"
        assert status_label(receipt) == "Turn completed (verified later: passed, OpenShard re-run)"
        assert checks_label(receipt) == "1/1 passed (OpenShard re-run @ 59f31c55f399)"
        out = _ok(_invoke(["history"], repo))
        assert "Turn completed (verified later: passed, OpenShard re-run)" in out
        assert "checks: 1/1 passed (OpenShard re-run @ 59f31c55f399)" in out
        assert "(unverified)" not in out and "agent-reported" not in out
        out = _ok(_invoke(["last"], repo))
        assert "Status      Turn completed (verified later: passed, OpenShard re-run)" in out
        assert "Verified    Passed (OpenShard re-ran the check(s): 1/1 passed @ 59f31c55f399)" in out
        assert "(unverified)" not in out

    def test_failed_rerun_is_named_and_never_hidden(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        _attest(repo, HOOKS_FULL, "passed")
        _attest(repo, HOOKS_FULL, "failed")  # the newest conclusive re-run wins
        receipt = _receipt(repo)
        assert interpret_receipt(receipt).state == STATE_VERIFIED_FAILED
        assert status_label(receipt) == "Turn completed (verified later: failed, OpenShard re-run)"
        assert checks_label(receipt) == "0/1 passed (OpenShard re-run @ 59f31c55f399)"
        out = _ok(_invoke(["history"], repo))
        assert "verified later: failed" in out

    def test_unbound_rerun_says_so_instead_of_naming_a_commit(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        _attest(repo, HOOKS_FULL, "passed", bound=False)
        receipt = _receipt(repo)
        assert checks_label(receipt) == "1/1 passed (OpenShard re-run)"
        assert "@" not in checks_label(receipt)

    def test_history_json_carries_the_same_truth_as_last(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        _attest(repo, HOOKS_FULL, "passed")
        page = recent_shards(repo_path=repo)
        row = history_json_body(page, locate_history(repo))["shards"][0]
        assert row["verification_truth"]["state"] == STATE_VERIFIED_PASSED
        assert row["verification_truth"]["basis"] == "post_session"
        assert row["verification_truth"]["post_session_artifact_sha"] == HEAD
        last = json.loads(_ok(_invoke(["last", "--json"], repo)))
        assert last["verification_truth"]["state"] == row["verification_truth"]["state"]
        assert last["verification_truth"]["label"] == row["verification_truth"]["label"]
        # The stored record was never touched.
        stored = json.loads((repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert stored["verification_attempted"] is False and "post_session_verification" not in stored


class TestLabelHelpers:
    def test_nothing_resolves_without_an_observed_outcome(self):
        truth = interpret_evidence(None)
        assert turn_status_label("Turn completed (unverified)", truth) == "Turn completed (unverified)"
        assert turn_status_label(None, truth) is None
        assert checks_row_label(truth) is None
        agent = interpret_evidence({
            "version": 1, "status": "passed", "source": "agent_reported",
            "observation_mode": "hook_tool_event", "checks_attempted": 1, "checks_passed": 1,
        })
        assert agent.state == "agent_reported_passed"
        assert turn_status_label("Turn completed (unverified)", agent) == "Turn completed (unverified)"
        assert checks_row_label(agent) is None  # the session's own display (agent-reported) stays

    def test_ci_basis_names_independent_ci_and_the_commit(self):
        base = interpret_evidence(None)
        truth = dataclasses.replace(
            base, state=STATE_VERIFIED_PASSED, basis=BASIS_CI, authority="independently_verified",
            effective_status="passed", checks_passed=3, checks_attempted=3, artifact_sha="abcdef0123456789",
        )
        assert turn_status_label("Turn completed (unverified)", truth) == (
            "Turn completed (verified later: passed, independent CI)"
        )
        assert checks_row_label(truth) == "3/3 passed (independent CI @ abcdef012345)"
        unbound = dataclasses.replace(truth, checks_passed=None, checks_attempted=None, artifact_sha=None)
        assert checks_row_label(unbound) == "passed (independent CI)"

    def test_receipt_status_row_uses_the_same_label(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        _attest(repo, HOOKS_FULL, "passed")
        receipt = _receipt(repo)
        rebuilt = build_shard_receipt(HOOKS_FULL, index=0, post_session_verification=receipt.post_session_verification)
        assert status_label(rebuilt) == "Turn completed (verified later: passed, OpenShard re-run)"
