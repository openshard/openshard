"""Baseline diagnostics never turn pre-existing failures into passing checks."""
import subprocess

from openshard.verification.baseline import compare_reports, read_report, run_baseline_comparison
from openshard.verification.post_session import PlannedCheck


def test_comparison_distinguishes_baseline_new_missing_and_skipped():
    result = compare_reports({"status": "complete", "outcomes": {"a": "failed", "b": "passed", "old": "passed"}},
                             {"status": "complete", "outcomes": {"a": "failed", "b": "failed", "new": "skipped"}})
    assert result == {"status": "failed", "passed": 0, "failed": 2, "skipped": 1,
                      "also_failed_on_base": 1, "new_failures": 1, "base_only_tests": 1,
                      "head_only_tests": 1, "environment_incompatible": None,
                      "environment_cause": "not_established"}


def test_incomplete_duplicate_and_setup_reports_are_not_comparable(tmp_path):
    report = tmp_path / "report.xml"
    for content, code in [
        ('<testsuite><testcase classname="a" name="x"/><testcase classname="a" name="x"/></testsuite>', 0),
        ('<testsuite><testcase classname="a" name="x"><error/></testcase></testsuite>', 1),
        ('<testsuite><testcase classname="a" name="x"/></testsuite>', 2),
        ('<!DOCTYPE a [<!ENTITY x "bad">]><testsuite/>', 0),
    ]:
        report.write_text(content)
        assert read_report(report, code)["status"] == "incomplete"
    assert compare_reports({"status": "incomplete"}, {"status": "complete"})["status"] == "incomplete"


def test_independent_real_base_and_head_runs_keep_failure_and_cleanup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    test = repo / "test_sample.py"
    test.write_text('def test_baseline():\n    assert False\ndef test_changed():\n    assert True\n')
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    test.write_text('def test_baseline():\n    assert False\ndef test_changed():\n    assert False\n')
    git("add", ".")
    git("commit", "-qm", "head")
    check = PlannedCheck(name="pytest", argv=("pytest", "-q"), kind="test", origin="contract", safety="safe", reason="")
    result = run_baseline_comparison(repo, base, [check], approve=False, timeout=30)
    assert result["status"] == "failed", result
    assert result["checks"][0]["also_failed_on_base"] == 1
    assert result["checks"][0]["new_failures"] == 1
    assert result["checks"][0]["environment_incompatible"] is None
    assert git("status", "--porcelain") == ""
    assert git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert "assert False" not in str(result)


def test_dirty_tree_is_never_compared(repo):
    (repo / "untracked.py").write_text("pass")
    result = run_baseline_comparison(repo, "HEAD", [], approve=False, timeout=1)
    assert result["reason"] == "clean_head_required"
