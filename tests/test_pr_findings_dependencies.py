"""PR findings: packages a change adds to its manifests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from openshard.verification import pr_findings as pf

PYPROJECT = """[project]
name = "x"
dependencies = ["click>=8", "Rich"]

[project.optional-dependencies]
dev = ["pytest"]
"""
PACKAGE = {"name": "w", "dependencies": {"react": "^19"}, "devDependencies": {"vitest": "^3"}}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "web").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    (root / "web" / "package.json").write_text(json.dumps(PACKAGE), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path, files: dict[str, str]) -> str:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    return _git(root, "rev-parse", "HEAD")


def _deps(root: Path, base: str, head: str) -> pf.Finding | None:
    hits = [f for f in pf.pr_findings(root, base, head) or [] if f.kind == "new_dependencies"]
    return hits[0] if hits else None


def test_packages_added_across_manifests_are_named_with_their_ecosystem(tmp_path):
    root, base = _repo(tmp_path)
    pkg = {**PACKAGE, "dependencies": {"react": "^19", "left-pad": "1.0.0"}}
    head = _commit(root, {
        "pyproject.toml": PYPROJECT.replace('"Rich"]', '"Rich", "Typer_CLI"]').replace('["pytest"]', '["pytest", "hypothesis"]'),
        "requirements.txt": "requests==2.32.0\n# a comment\nhttpx>=0.27 ; python_version > '3.10'\n-r other.txt\n",
        "web/package.json": json.dumps(pkg),
        "web/pnpm-lock.yaml": "lock\n",
    })
    finding = _deps(root, base, head)
    assert finding is not None and finding.count == 4
    assert finding.extra["packages"] == [
        {"name": "left-pad", "ecosystem": "npm", "manifest": "web/package.json"},
        {"name": "httpx", "ecosystem": "pypi", "manifest": "requirements.txt"},
        {"name": "hypothesis", "ecosystem": "pypi", "manifest": "pyproject.toml"},
        {"name": "typer-cli", "ecosystem": "pypi", "manifest": "pyproject.toml"},
    ]
    assert finding.extra["lockfiles"] == ["web/pnpm-lock.yaml"]


def test_a_version_bump_or_a_renormalised_name_is_not_new(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"pyproject.toml": PYPROJECT.replace("click>=8", "click>=8.1").replace('"Rich"', '"rich"'),
                          "requirements.txt": "requests==2.33.0\n"})
    assert _deps(root, base, head) is None


def test_a_new_manifest_counts_every_package_in_it(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"services/api/requirements.txt": "fastapi\nuvicorn\n"})
    finding = _deps(root, base, head)
    assert finding is not None and [p["name"] for p in finding.extra["packages"]] == ["fastapi", "uvicorn"]


def test_moving_a_package_between_dependency_maps_is_not_new(tmp_path):
    root, base = _repo(tmp_path)
    pkg = {"name": "w", "dependencies": {"react": "^19", "vitest": "^3"}}
    head = _commit(root, {"web/package.json": json.dumps(pkg)})
    assert _deps(root, base, head) is None


def test_an_unparseable_manifest_is_skipped(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"web/package.json": "{not json", "pyproject.toml": "[project\n"})
    assert _deps(root, base, head) is None


def test_poetry_tables_are_read_and_python_itself_is_not_a_package(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"pyproject.toml": PYPROJECT + '\n[tool.poetry.dependencies]\npython = "^3.11"\nattrs = "*"\n'})
    finding = _deps(root, base, head)
    assert finding is not None and [p["name"] for p in finding.extra["packages"]] == ["attrs"]
