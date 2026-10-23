"""Fail-without-fix: a change's new tests are run on the code before the change."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from openshard.verification import fail_without_fix as fwf

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
OLD_TESTS = "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "calc.py").write_text(BUGGY, encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(OLD_TESTS, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path, files: dict[str, str]) -> str:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return _git(root, "rev-parse", "HEAD")


def test_a_test_that_fails_before_the_fix_proves_it_and_one_that_passes_does_not(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {
        "calc.py": FIXED,
        "tests/test_calc.py": OLD_TESTS + (
            "\n\ndef test_add_positive():\n    assert add(2, 3) == 5\n"
            "\n\ndef test_add_zero_right():\n    assert add(4, 0) == 4\n"),
    })
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["status"] == "complete", result
    assert result["counts"] == {"new_or_changed": 2, "proves_fix": 1, "cannot_load_on_base": 0,
                                "passes_without_fix": 1, "fails_on_head": 0, "not_run": 0}
    assert result["passes_without_fix_tests"] == ["tests/test_calc.py::test_add_zero_right"]
    assert result["base_commit"] == base and result["raw_output_stored"] is False


def test_a_new_test_for_a_new_module_cannot_load_on_base_which_is_not_proof(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"shapes.py": "def area(w, h):\n    return w * h\n",
                   "tests/test_shapes.py": "from shapes import area\n\n\ndef test_area():\n    assert area(2, 3) == 6\n"})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["counts"]["cannot_load_on_base"] == 1 and result["counts"]["proves_fix"] == 0
    assert result["cannot_load_on_base_tests"] == ["tests/test_shapes.py::test_area"]


def test_a_new_test_that_fails_on_head_is_reported_as_such(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"tests/test_more.py": "from calc import add\n\n\ndef test_wrong():\n    assert add(1, 1) == 3\n"})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["counts"]["fails_on_head"] == 1
    assert result["fails_on_head_tests"] == ["tests/test_more.py::test_wrong"]


def test_unchanged_tests_and_non_test_changes_are_not_counted(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"calc.py": FIXED})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["status"] == "complete" and result["counts"]["new_or_changed"] == 0


def test_a_rewritten_test_counts_and_class_tests_are_named(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"calc.py": FIXED, "tests/test_calc.py": (
        "from calc import add\n\n\ndef test_zero():\n    assert add(0, 1) == 1\n"
        "\n\nclass TestAdd:\n    def test_pair(self):\n        assert add(1, 2) == 3\n")})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["counts"]["new_or_changed"] == 2 and result["counts"]["proves_fix"] == 2


def test_an_unknown_base_is_incomplete_never_a_pass(tmp_path):
    root, _ = _repo(tmp_path)
    result = fwf.run_fail_without_fix(root, "no-such-ref", timeout=30)
    assert (result["status"], result["reason"]) == ("incomplete", "base_commit_unavailable")


def test_worktrees_are_cleaned_up_and_no_output_is_kept(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"calc.py": FIXED, "tests/test_calc.py": OLD_TESTS + "\n\ndef test_x():\n    print('SECRET_OUT')\n    assert add(1, 2) == 3\n"})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert "SECRET_OUT" not in json.dumps(result)
    assert _git(root, "worktree", "list").count("\n") == 0  # only the main checkout remains


def test_test_functions_hash_bodies_and_ignore_helpers():
    a = fwf.test_functions("def test_a():\n    assert 1\n\n\ndef helper():\n    pass\n")
    b = fwf.test_functions("def test_a():\n    assert 2\n")
    assert a is not None and b is not None and list(a) == ["test_a"] and a != b
    assert fwf.test_functions("def broken(:\n") is None


def test_a_base_run_that_breaks_for_another_reason_proves_nothing(tmp_path):
    """A conftest that crashes on base makes no new test a proof of the fix."""
    root, base = _repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "tmp")
    _commit(root, {"tests/conftest.py": "raise SystemExit(7)\n"})
    broken_base = _git(root, "rev-parse", "HEAD")
    _commit(root, {"tests/conftest.py": "", "calc.py": FIXED,
                   "tests/test_calc.py": OLD_TESTS + "\n\ndef test_add_positive():\n    assert add(2, 3) == 5\n"})
    result = fwf.run_fail_without_fix(root, broken_base, timeout=120)
    assert result["counts"]["proves_fix"] == 0 and result["counts"]["not_run"] == 1


def test_read_outcomes_folds_parametrised_cases(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text('<testsuite><testcase classname="tests.test_x" name="test_p[1]"/>'
                      '<testcase classname="tests.test_x" name="test_p[2]"><failure/></testcase>'
                      '<testcase classname="tests.test_x.TestK" name="test_k"/></testsuite>', encoding="utf-8")
    assert fwf.read_outcomes(report) == {("tests.test_x", "test_p"): "failed", ("tests.test_x", "TestK.test_k"): "passed"}


def test_the_cli_reports_json_and_exit_codes(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from openshard.cli.main import cli

    root, base = _repo(tmp_path)
    _commit(root, {"calc.py": FIXED, "tests/test_calc.py": OLD_TESTS + "\n\ndef test_two():\n    assert add(1, 1) == 2\n"})
    monkeypatch.chdir(root)
    ok = CliRunner().invoke(cli, ["ci", "fail-without-fix", "--base", base, "--json"])
    assert ok.exit_code == 0, ok.output
    data = json.loads(ok.output)
    assert data["schema"] == "openshard.fail_without_fix.v1" and data["counts"]["proves_fix"] == 1
    bad = CliRunner().invoke(cli, ["ci", "fail-without-fix", "--base", "nope"])
    assert bad.exit_code == 2 and "not established" in bad.output


def test_an_import_of_a_missing_helper_inside_the_test_cannot_load_on_base(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"calc.py": FIXED + "\n\ndef double(x):\n    return 2 * x\n",
                   "tests/test_calc.py": OLD_TESTS + ("\n\ndef test_double():\n    from calc import double\n\n"
                                                      "    assert double(2) == 4\n")})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["counts"]["cannot_load_on_base"] == 1 and result["counts"]["proves_fix"] == 0


def test_a_missing_fixture_on_base_cannot_load_but_a_real_assertion_failure_proves_the_fix(tmp_path):
    root, base = _repo(tmp_path)
    _commit(root, {"calc.py": FIXED,
                   "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef pair():\n    return (2, 3)\n",
                   "tests/test_calc.py": OLD_TESTS + ("\n\ndef test_with_fixture(pair):\n    assert add(*pair) == 5\n"
                                                      "\n\ndef test_plain():\n    assert add(2, 3) == 5\n")})
    result = fwf.run_fail_without_fix(root, base, timeout=120)
    assert result["counts"]["cannot_load_on_base"] == 1  # conftest.py is not copied: its fixture is missing on base
    assert result["counts"]["proves_fix"] == 1  # add(2, 3) == -1 on base: a real failure
    assert result["cannot_load_on_base_tests"] == ["tests/test_calc.py::test_with_fixture"]


def test_read_outcomes_tells_load_failures_from_real_failures(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text('<testsuite>'
                      '<testcase classname="tests.test_x" name="test_a"><failure message="ImportError: cannot import name"/></testcase>'
                      '<testcase classname="tests.test_x" name="test_b"><failure message="AssertionError: assert 1 == 2"/></testcase>'
                      '<testcase classname="tests.test_x" name="test_c"><error message="fixture \'pair\' not found"/></testcase>'
                      '<testcase classname="tests.test_x" name="test_d[1]"><error message="setup"/></testcase>'
                      '<testcase classname="tests.test_x" name="test_d[2]"><failure message="assert 0"/></testcase>'
                      '</testsuite>', encoding="utf-8")
    assert fwf.read_outcomes(report) == {("tests.test_x", "test_a"): "cannot_load", ("tests.test_x", "test_b"): "failed",
                                         ("tests.test_x", "test_c"): "cannot_load", ("tests.test_x", "test_d"): "failed"}
