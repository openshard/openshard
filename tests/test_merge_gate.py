"""The merge gate: ``ci/merge_gate.evaluate_gate`` and ``openshard ci gate``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.ci.merge_gate import (
    WITNESS_NO,
    WITNESS_UNKNOWN,
    WITNESS_YES,
    GateReceipt,
    evaluate_gate,
)
from openshard.cli.main import cli
from openshard.verification import ci_evidence as ci
from openshard.verification import post_session as ps
from openshard.verification import verdict

SHA = "a" * 40


def _doc(v: str, reason: str | None = None, **extra) -> dict:
    return {"schema": "openshard.verdict", "schema_version": 1, "subject": {"kind": "commit", "id": SHA},
            "verdict": v, "exit_code": verdict.EXIT_CODES[v], "reason": reason or v, **extra}


class TestDecision:
    @pytest.mark.parametrize("strict", [False, True])
    def test_verified_passes(self, strict):
        r = evaluate_gate(_doc("verified", "verified_passed", authority="independently_verified"), strict=strict)
        assert (r.status, r.exit_code, r.reasons) == ("pass", 0, [])
        assert "independent CI" in r.headline

    @pytest.mark.parametrize("strict", [False, True])
    def test_failed_fails(self, strict):
        r = evaluate_gate(_doc("failed", "verified_failed", authority="directly_observed"), strict=strict)
        assert (r.status, r.exit_code) == ("fail", 1)
        assert "Verification failed" in r.headline and "Openshard re-run" in r.headline

    def test_unknown_warns_and_does_not_block(self):
        r = evaluate_gate(_doc("unknown", "not_checked"))
        assert (r.status, r.exit_code, r.reasons) == ("warn", 0, [])
        assert "human-written" in r.headline

    def test_unknown_fails_under_strict(self):
        r = evaluate_gate(_doc("unknown", "agent_reported_passed"), strict=True)
        assert (r.status, r.exit_code) == ("fail", 1)
        assert "Only the agent says" in r.headline

    @pytest.mark.parametrize("doc", [
        verdict.error("commit", "x", "'x' is not a commit in this repository."),
        {}, None, "garbage", {"verdict": "maybe"},
    ])
    def test_error_or_unreadable_never_passes(self, doc):
        r = evaluate_gate(doc)
        assert (r.status, r.exit_code) == ("fail", 1)
        assert "could not answer" in r.headline

    def test_error_message_is_shown(self):
        r = evaluate_gate(verdict.error("commit", "x", "No OpenShard history found from this directory."))
        assert "No OpenShard history found" in r.summary_markdown


class TestSummary:
    def test_unknown_fields_say_unknown_never_zero(self):
        md = evaluate_gate(_doc("unknown", "not_checked")).summary_markdown
        for row in ("Agent", "Model", "Checks", "Cost", "Platform-witnessed"):
            assert f"| {row} | unknown |" in md
        assert "$0" not in md and "| 0 |" not in md

    def test_receipt_fields_are_shown(self):
        receipt = GateReceipt(receipt_id="rcpt_1", agent="Claude Code (external)", model="Claude Opus 5.5",
                              cost="$0.42 est.", witness=WITNESS_YES, witness_detail="3 of 3 events acknowledged")
        md = evaluate_gate(_doc("verified", "verified_passed"), receipt=receipt, checks="12/12 passed").summary_markdown
        assert "| Agent | Claude Code (external) |" in md
        assert "| Model | Claude Opus 5.5 |" in md
        assert "| Checks | 12/12 passed |" in md
        assert "| Cost | $0.42 est. |" in md
        assert "| Platform-witnessed | yes, 3 of 3 events acknowledged |" in md
        assert "| Receipt | rcpt_1 |" in md
        assert "detectable" in md and "not being verified" in md

    @pytest.mark.parametrize("witness,shown", [(WITNESS_NO, "no"), (WITNESS_UNKNOWN, "unknown")])
    def test_tamper_evidence_is_claimed_only_when_witnessed(self, witness, shown):
        md = evaluate_gate(_doc("verified", "verified_passed"), receipt=GateReceipt(witness=witness)).summary_markdown
        assert f"| Platform-witnessed | {shown} |" in md
        assert "tamper" not in md.lower() and "detectable" not in md

    def test_failed_check_names_are_listed(self):
        md = evaluate_gate(_doc("failed", "verified_failed"), checks="3 passed, 1 failed",
                           failed_checks=("pytest",)).summary_markdown
        assert "| Checks | 3 passed, 1 failed (failed: pytest) |" in md

    def test_cells_cannot_break_the_table(self):
        md = evaluate_gate(_doc("verified"), receipt=GateReceipt(agent="a|b\n`c`")).summary_markdown
        assert "| Agent | a\\|b 'c' |" in md

    def test_merge_check_is_informational(self):
        receipt = GateReceipt(merge_check="2 conflicting path(s) with origin/main")
        r = evaluate_gate(_doc("verified", "verified_passed"), receipt=receipt)
        assert r.status == "pass"
        assert "informational) | 2 conflicting path(s) with origin/main |" in r.summary_markdown

    def test_tree_match_is_said(self):
        md = evaluate_gate(_doc("verified", "verified_passed", match="tree", artifact_sha="b" * 40)).summary_markdown
        assert "bbbbbbbbbbbb" in md and "same tree" in md


# --- the CLI, against real git and recorded evidence ------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "factory"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".openshard/\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    return root


ENTRY = {"receipt_id": "rcpt_" + "1" * 32, "run_id": "run-1", "timestamp": "2026-10-18T10:00:00Z",
         "task": "change a", "executor": "claude-code", "execution_model": "claude-opus-5-5",
         "estimated_cost": 0.42}


def _write_runs(repo: Path) -> None:
    path = repo / ".openshard" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ENTRY) + "\n", encoding="utf-8")


def _rerun(repo: Path, status: str) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    check = ps.PlannedCheck(name="pytest", argv=["pytest"], kind="test", origin="contract", safety="safe", reason="")
    tree = ps.TreeState(head=sha, dirty=False, tracked_dirty=False)
    ps.record_attestation(repo, ps.build_attestation(
        ENTRY, [ps.CheckRun(check, status, exit_code=0 if status == "passed" else 1)], before=tree, after=tree,
        started_at="2026-10-18T11:00:00Z", completed_at="2026-10-18T11:00:01Z", commit=sha))


def _ci(repo: Path, conclusion: str) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    runs = [{"name": "tests", "head_sha": sha, "status": "completed", "conclusion": conclusion,
             "started_at": "2026-10-18T12:00:00Z"}]
    item = ci.build_ci_attestation(ENTRY, ci.classify_check_runs(runs, sha), ci.CITarget(sha=sha, binding="clean_head"),
                                   created_at="2026-10-18T12:00:01Z")
    assert item is not None
    ps.record_attestation(repo, item)


def _gate(repo: Path, monkeypatch, *args: str, summary: Path | None = None, output: Path | None = None):
    monkeypatch.chdir(repo)
    env = {}
    if summary is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    if output is not None:
        env["GITHUB_OUTPUT"] = str(output)
    return CliRunner().invoke(cli, ["ci", "gate", *args], env=env)


class TestCli:
    def test_no_evidence_warns_with_exit_0(self, repo, monkeypatch, tmp_path):
        summary, output = tmp_path / "summary.md", tmp_path / "out.txt"
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--github-step-summary", "--github-output",
                    summary=summary, output=output)
        assert res.exit_code == 0 and res.output.startswith("Openshard gate: warn")
        assert "### Openshard gate: warning: unknown" in summary.read_text(encoding="utf-8")
        assert output.read_text(encoding="utf-8") == "openshard_gate_status=warn\nopenshard_gate_verdict=unknown\n"

    def test_no_evidence_fails_under_strict(self, repo, monkeypatch):
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--strict")
        assert res.exit_code == 1 and res.output.startswith("Openshard gate: fail")

    def test_a_failing_rerun_fails(self, repo, monkeypatch, tmp_path):
        _write_runs(repo)
        _rerun(repo, "failed")
        summary = tmp_path / "summary.md"
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--github-step-summary", summary=summary)
        assert res.exit_code == 1 and res.output.startswith("Openshard gate: fail")
        md = summary.read_text(encoding="utf-8")
        assert "(failed: pytest)" in md and "| Receipt | rcpt_" in md

    def test_passing_ci_passes_and_summarises_the_receipt(self, repo, monkeypatch, tmp_path):
        _write_runs(repo)
        _ci(repo, "success")
        summary = tmp_path / "summary.md"
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--strict", "--github-step-summary", summary=summary)
        assert res.exit_code == 0 and res.output.startswith("Openshard gate: pass")
        md = summary.read_text(encoding="utf-8")
        assert "| Checks | 1/1 passed |" in md
        assert "| Cost | $0.4200 |" in md
        # A runner's spool holds no acknowledgement: unknown, never "no", never tamper-evident.
        assert "| Platform-witnessed | unknown |" in md and "detectable" not in md

    def test_a_bad_commit_fails(self, repo, monkeypatch):
        res = _gate(repo, monkeypatch, "--commit", "no-such-ref")
        assert res.exit_code == 1 and "could not answer" in res.output

    def test_committed_history_is_not_evidence(self, repo, monkeypatch):
        _write_runs(repo)
        _rerun(repo, "passed")
        _git(repo, "add", "-f", ".openshard/runs.jsonl", ".openshard/verifications.jsonl")
        _git(repo, "commit", "-q", "-m", "history")
        _rerun(repo, "passed")  # evidence for the new HEAD, but from files the change itself carries
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--json")
        body = json.loads(res.output)
        assert res.exit_code == 0 and body["status"] == "warn"
        assert body["verdict"]["reason"] == "untrusted_history"

    def test_json_carries_the_verdict_document(self, repo, monkeypatch):
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--json")
        body = json.loads(res.output)
        assert body["command"] == "ci gate" and body["exit_code"] == 0
        assert body["verdict"]["schema"] == "openshard.verdict"
