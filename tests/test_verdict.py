"""``openshard verdict``: a stable verdict document and exit codes for orchestrators."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.verification import build_verification
from openshard.verification import ci_evidence as ci
from openshard.verification import post_session as ps
from openshard.verification import verdict

TASK = "task_018f4d2a-1c3e-7000-8b1a-0242ac120002"
OTHER_TASK = "task_018f4d2a-1c3e-7000-9c2b-0242ac120003"
KEYS = {"schema", "schema_version", "subject", "verdict", "exit_code", "reason", "state", "authority", "basis",
        "artifact_sha", "evidence", "receipts"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "factory"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    return root


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _entry(n: int, task: str | None = TASK, verification: dict | None = None) -> dict:
    entry: dict = {"receipt_id": f"rcpt_{n:032x}", "run_id": f"run-{n}", "timestamp": f"2026-10-18T10:0{n}:00Z",
                   "task": "change a", "executor": "claude-code"}
    if task:
        entry["task_id"] = task
    if verification:
        entry["verification"] = verification
    return entry


def _write_runs(repo: Path, entries: list[dict]) -> None:
    path = repo / ".openshard" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _rerun(repo: Path, entry: dict, status: str, sha: str, *, bound: bool = True) -> None:
    check = ps.PlannedCheck(name="pytest", argv=["pytest"], kind="test", origin="contract", safety="safe", reason="")
    tree = ps.TreeState(head=sha, dirty=not bound, tracked_dirty=not bound)
    ps.record_attestation(repo, ps.build_attestation(
        entry, [ps.CheckRun(check, status, exit_code=0 if status == "passed" else 1)], before=tree, after=tree,
        started_at="2026-10-18T11:00:00Z", completed_at="2026-10-18T11:00:01Z", commit=sha))


def _ci(repo: Path, entry: dict, sha: str, conclusion: str) -> None:
    runs = [{"name": "ci", "head_sha": sha, "status": "completed", "conclusion": conclusion,
             "started_at": "2026-10-18T12:00:00Z"}]
    item = ci.build_ci_attestation(entry, ci.classify_check_runs(runs, sha), ci.CITarget(sha=sha, binding="clean_head"),
                                   created_at="2026-10-18T12:00:01Z")
    assert item is not None
    ps.record_attestation(repo, item)


def _run(repo: Path, monkeypatch, *args: str) -> tuple[int, dict]:
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["verdict", *args, "--json"])
    return result.exit_code, json.loads(result.output)


class TestCommit:
    def test_not_checked_yet_is_unknown(self, repo, monkeypatch):
        code, doc = _run(repo, monkeypatch, "--commit", "HEAD")
        assert code == 2 and doc["verdict"] == "unknown" and doc["reason"] == "not_checked"
        assert set(doc) == KEYS | {"match"} and doc["match"] is None
        assert doc["subject"] == {"kind": "commit", "id": _head(repo),
                                  "tree_oid": _git(repo, "rev-parse", "HEAD^{tree}")}

    def test_a_bound_passing_rerun_is_verified(self, repo, monkeypatch):
        entry = _entry(1)
        _write_runs(repo, [entry])
        _rerun(repo, entry, "passed", _head(repo))
        code, doc = _run(repo, monkeypatch, "--commit", _head(repo)[:12])
        assert (code, doc["verdict"], doc["authority"]) == (0, "verified", "directly_observed")
        assert doc["evidence"][0]["receipt_id"] == entry["receipt_id"]

    def test_a_failing_rerun_is_failed(self, repo, monkeypatch):
        _rerun(repo, _entry(1), "failed", _head(repo))
        code, doc = _run(repo, monkeypatch, "--commit", "HEAD")
        assert (code, doc["verdict"], doc["state"]) == (1, "failed", "verified_failed")

    def test_a_rerun_not_bound_to_the_commit_does_not_count(self, repo, monkeypatch):
        _rerun(repo, _entry(1), "passed", _head(repo), bound=False)
        code, doc = _run(repo, monkeypatch, "--commit", "HEAD")
        assert (code, doc["reason"], doc["evidence"]) == (2, "not_checked", [])

    def test_ci_on_the_commit_counts_as_independent(self, repo, monkeypatch):
        _ci(repo, _entry(1), _head(repo), "success")
        code, doc = _run(repo, monkeypatch, "--commit", "HEAD")
        assert (code, doc["verdict"], doc["authority"]) == (0, "verified", "independently_verified")

    def test_evidence_for_another_commit_never_counts(self, repo, monkeypatch):
        _rerun(repo, _entry(1), "passed", "f" * 40)
        code, doc = _run(repo, monkeypatch, "--commit", "HEAD")
        assert code == 2

    def test_a_bad_commit_is_an_error(self, repo, monkeypatch):
        code, doc = _run(repo, monkeypatch, "--commit", "no-such-ref")
        assert (code, doc["verdict"]) == (3, "error") and set(doc) == KEYS | {"message"}


class TestTask:
    def test_an_agent_reported_pass_is_unknown(self, repo, monkeypatch):
        claim = build_verification(source="agent_reported", observation_mode="hook_tool_event", status="passed",
                                   checks=[{"name": "pytest", "kind": "test", "status": "passed"}])
        _write_runs(repo, [_entry(1, verification=claim)])
        code, doc = _run(repo, monkeypatch, "--task", TASK)
        assert (code, doc["verdict"], doc["state"]) == (2, "unknown", "agent_reported_passed")

    def test_the_latest_receipt_decides_and_every_receipt_is_listed(self, repo, monkeypatch):
        first, second = _entry(1), _entry(2)
        _write_runs(repo, [first, _entry(3, task=OTHER_TASK), second])
        _rerun(repo, first, "passed", _head(repo))
        code, doc = _run(repo, monkeypatch, "--task", TASK)
        assert (code, doc["receipt_id"]) == (2, second["receipt_id"])
        assert [r["verdict"] for r in doc["receipts"]] == ["verified", "unknown"]
        _rerun(repo, second, "passed", _head(repo))
        code, doc = _run(repo, monkeypatch, "--task", TASK)
        assert (code, doc["verdict"]) == (0, "verified")

    def test_no_receipt_for_the_task_is_unknown(self, repo, monkeypatch):
        code, doc = _run(repo, monkeypatch, "--task", TASK)
        assert (code, doc["reason"]) == (2, "no_receipt")

    def test_a_malformed_task_id_is_an_error(self, repo, monkeypatch):
        code, doc = _run(repo, monkeypatch, "--task", "task_nope")
        assert (code, doc["verdict"]) == (3, "error")


def test_exactly_one_subject_is_required(repo, monkeypatch):
    monkeypatch.chdir(repo)
    # Usage errors are errors (3), never "unknown" (2).
    assert CliRunner().invoke(cli, ["verdict", "--json"]).exit_code == 3
    assert CliRunner().invoke(cli, ["verdict", "--commit", "HEAD", "--task", TASK]).exit_code == 3
    assert CliRunner().invoke(cli, ["verdict", "--no-such-option"]).exit_code == 3
    assert verdict.EXIT_CODES == {"verified": 0, "failed": 1, "unknown": 2, "error": 3}


def test_text_output_names_the_verdict(repo, monkeypatch):
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["verdict", "--commit", "HEAD"])
    assert result.exit_code == 2 and result.output.startswith(f"commit {_head(repo)}: unknown (not_checked)")
