"""PR findings: a change that edits what grades it (tests, CI, lint and coverage settings)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from openshard.verification import pr_findings as pf

PYPROJECT = """[project]
name = "x"
dependencies = ["click"]

[tool.ruff]
line-length = 100

[tool.coverage.report]
fail_under = 90
"""
PACKAGE = '{"name": "w", "scripts": {"test": "vitest run", "build": "tsc"}, "dependencies": {"a": "1"}}'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    files = {
        "app.py": "def f():\n    return 1\n",
        "tests/test_app.py": "from app import f\n\n\ndef test_f():\n    assert f() == 1\n",
        "tests/conftest.py": "",
        "pyproject.toml": PYPROJECT,
        "web/package.json": PACKAGE,
        ".github/workflows/ci.yml": "on: push\n",
        "setup.cfg": "[metadata]\nname = x\n\n[flake8]\nmax-line-length = 100\n",
    }
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path, files: dict[str, str] | None = None, remove: tuple[str, ...] = ()) -> str:
    for rel, body in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    for rel in remove:
        (root / rel).unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    return _git(root, "rev-parse", "HEAD")


def _grader(root: Path, base: str, head: str) -> pf.Finding | None:
    findings = pf.pr_findings(root, base, head)
    assert findings is not None
    hits = [f for f in findings if f.kind == "grader_touched"]
    return hits[0] if hits else None


def test_weakening_an_existing_assertion_touches_the_grader(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"tests/test_app.py": "from app import f\n\n\ndef test_f():\n    assert True\n"})
    finding = _grader(root, base, head)
    assert finding is not None and finding.extra["categories"] == ["tests_edited"]
    assert finding.paths == ("tests/test_app.py",)


def test_deleting_or_renaming_a_test_file_touches_the_grader(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, remove=("tests/test_app.py",))
    assert _grader(root, base, head).extra["categories"] == ["tests_removed"]  # type: ignore[union-attr]


def test_adding_a_test_file_or_appending_tests_is_writing_tests_not_touching_them(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {
        "tests/test_new.py": "def test_new():\n    assert 1\n",
        "tests/test_app.py": "from app import f\n\n\ndef test_f():\n    assert f() == 1\n\n\ndef test_g():\n    assert 2\n",
        "app.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n",
    })
    assert _grader(root, base, head) is None


def test_editing_ci_config_or_conftest_touches_the_grader(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {".github/workflows/ci.yml": "on: push\njobs: {}\n", "tests/conftest.py": "x = 1\n"})
    finding = _grader(root, base, head)
    assert finding is not None and finding.extra["categories"] == ["check_config", "ci_config"]


def test_adding_a_new_workflow_or_lint_config_touches_the_grader(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {".github/workflows/skip.yml": "on: push\n", "ruff.toml": "line-length = 200\n"})
    assert set(_grader(root, base, head).paths) == {".github/workflows/skip.yml", "ruff.toml"}  # type: ignore[union-attr]


def test_lowering_a_coverage_threshold_in_pyproject_touches_the_grader(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"pyproject.toml": PYPROJECT.replace("fail_under = 90", "fail_under = 10")})
    finding = _grader(root, base, head)
    assert finding is not None and finding.paths == ("pyproject.toml",)


def test_adding_a_dependency_in_pyproject_is_not_a_grader_change(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"pyproject.toml": PYPROJECT.replace('["click"]', '["click", "rich"]')})
    assert _grader(root, base, head) is None


def test_a_package_json_test_script_change_is_a_grader_change_but_a_build_script_is_not(tmp_path):
    root, base = _repo(tmp_path)
    mid = _commit(root, {"web/package.json": PACKAGE.replace('"tsc"', '"tsc -b"')})
    assert _grader(root, base, mid) is None
    head = _commit(root, {"web/package.json": PACKAGE.replace('"vitest run"', '"exit 0"')})
    assert _grader(root, mid, head).paths == ("web/package.json",)  # type: ignore[union-attr]


def test_setup_cfg_tool_sections_count_and_metadata_does_not(tmp_path):
    root, base = _repo(tmp_path)
    cfg = "[metadata]\nname = y\n\n[flake8]\nmax-line-length = 100\n"
    mid = _commit(root, {"setup.cfg": cfg})
    assert _grader(root, base, mid) is None
    head = _commit(root, {"setup.cfg": cfg.replace("100", "300")})
    assert _grader(root, mid, head) is not None


def test_an_unparseable_pyproject_is_not_called_a_grader_change(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"pyproject.toml": "[tool.ruff\nbroken"})
    assert _grader(root, base, head) is None


def test_findings_name_paths_never_contents(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"tests/test_app.py": "SECRET_TOKEN = 1\n"})
    finding = _grader(root, base, head)
    assert finding is not None and "SECRET_TOKEN" not in repr(finding.to_dict())
