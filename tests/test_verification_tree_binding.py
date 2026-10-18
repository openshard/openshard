"""Verification evidence names the git tree as well as the commit, and survives a squash merge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.verification import ci_evidence as ci
from openshard.verification import post_session as ps


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "factory"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _commit(root, "a.py", "x = 1\n", "base")
    return root


ENTRY = {"receipt_id": "rcpt_" + "1" * 32, "run_id": "run-1", "task": "t", "executor": "claude-code"}


def _check() -> ps.PlannedCheck:
    return ps.PlannedCheck(name="pytest", argv=["pytest"], kind="test", origin="contract", safety="safe", reason="")


def _rerun_here(repo: Path, status: str = "passed") -> dict:
    """Record a re-run of the checked-out commit, as ``openshard verify --commit`` would."""
    tree = ps.tree_state(repo)
    attestation = ps.build_attestation(
        ENTRY, [ps.CheckRun(_check(), status, exit_code=0 if status == "passed" else 1)], before=tree, after=tree,
        started_at="2026-10-18T11:00:00Z", completed_at="2026-10-18T11:00:01Z", commit=tree.head)
    ps.record_attestation(repo, attestation)
    return attestation


def _verdict(repo: Path, monkeypatch, sha: str) -> tuple[int, dict]:
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["verdict", "--commit", sha, "--json"])
    return result.exit_code, json.loads(result.output)


def _squash_feature(repo: Path) -> tuple[str, str]:
    """A two-commit feature branch squash-merged onto main: ``(feature head, squash commit)``."""
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "b.py", "y = 1\n", "wip")
    head = _commit(repo, "b.py", "y = 2\n", "fix")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "feature")
    _git(repo, "commit", "-q", "-m", "feature (squashed)")
    return head, _git(repo, "rev-parse", "HEAD")


class TestRecorded:
    def test_a_bound_rerun_records_the_tree_beside_the_commit(self, repo):
        attestation = _rerun_here(repo)
        assert attestation["commit"] == _git(repo, "rev-parse", "HEAD")
        assert attestation["tree_oid"] == _git(repo, "rev-parse", "HEAD^{tree}")
        assert attestation["verification"]["artifact_sha"] == attestation["commit"]

    def test_an_unbound_rerun_records_no_tree(self, repo):
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        attestation = _rerun_here(repo)
        assert "tree_oid" not in attestation and attestation["verification"]["artifact_sha"] is None

    def test_a_ci_verdict_records_the_tree_it_was_given(self, repo):
        sha, tree = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")
        runs = [{"name": "ci", "head_sha": sha, "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-18T12:00:00Z"}]
        item = ci.build_ci_attestation(ENTRY, ci.classify_check_runs(runs, sha), ci.CITarget(sha=sha, binding="clean_head"),
                                       created_at="2026-10-18T12:00:01Z", tree_oid=ps.commit_tree(repo, sha))
        assert item is not None and item["tree_oid"] == tree and item["ci"]["commit"] == sha

    def test_commit_tree_is_none_for_an_unknown_commit(self, repo):
        assert ps.commit_tree(repo, "f" * 40) is None


class TestLookup:
    def test_a_squash_merge_is_verified_by_tree_and_labelled(self, repo, monkeypatch):
        head, squash = _squash_feature(repo)
        _git(repo, "checkout", "-q", head)
        _rerun_here(repo)
        _git(repo, "checkout", "-q", "main")
        code, doc = _verdict(repo, monkeypatch, squash)
        assert (code, doc["verdict"], doc["match"]) == (0, "verified", "tree")
        assert doc["subject"] == {"kind": "commit", "id": squash, "tree_oid": _git(repo, "rev-parse", f"{squash}^{{tree}}")}
        [row] = doc["evidence"]
        assert (row["artifact_sha"], row["match"]) == (head, "tree")

    def test_exact_commit_evidence_wins_over_tree_evidence(self, repo, monkeypatch):
        head, squash = _squash_feature(repo)
        _git(repo, "checkout", "-q", head)
        _rerun_here(repo, "passed")
        _git(repo, "checkout", "-q", "main")
        _rerun_here(repo, "failed")  # the squash commit itself, checked out on main
        code, doc = _verdict(repo, monkeypatch, squash)
        assert (code, doc["match"], [r["artifact_sha"] for r in doc["evidence"]]) == (1, "commit", [squash])

    def test_a_different_tree_never_matches(self, repo, monkeypatch):
        _rerun_here(repo)
        other = _commit(repo, "a.py", "x = 3\n", "change")
        code, doc = _verdict(repo, monkeypatch, other)
        assert (code, doc["reason"], doc["match"]) == (2, "not_checked", None)
