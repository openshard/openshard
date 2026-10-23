"""``openshard ci findings``: the PR findings for a change, as text and as JSON."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path, files: dict[str, str]) -> str:
    for rel, body in files.items():
        (root / rel).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    return _git(root, "rev-parse", "HEAD")


def test_the_cli_reports_findings_as_json_and_exits_0(tmp_path, monkeypatch):
    root, base = _repo(tmp_path)
    _commit(root, {"big.py": "a = 1\n" * 450, "x.orig": "x\n"})
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["ci", "findings", "--base", base, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["schema"] == "openshard.pr_findings.v1" and data["checked"] is True
    assert [f["kind"] for f in data["findings"]] == ["large_change", "junk_files"]

    human = CliRunner().invoke(cli, ["ci", "findings", "--base", base])
    assert human.exit_code == 0 and "2 findings" in human.output and "consider splitting" in human.output

def test_the_cli_says_not_checked_and_exits_2_for_an_unknown_base(tmp_path, monkeypatch):
    root, _base = _repo(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["ci", "findings", "--base", "no-such-branch"])
    assert result.exit_code == 2 and "not checked" in result.output


def _dependency_change(tmp_path: Path) -> tuple[Path, str]:
    root, base = _repo(tmp_path)
    _commit(root, {"requirements.txt": "click\n"})
    return root, base


def test_publish_dates_are_not_looked_up_without_the_flag(tmp_path, monkeypatch):
    from openshard.verification import dependency_age

    def no_network(*_args, **_kwargs):
        raise AssertionError("no registry lookup without --registry-lookup")

    monkeypatch.setattr(dependency_age, "fetch_json", no_network)
    root, base = _dependency_change(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["ci", "findings", "--base", base, "--json"])
    assert result.exit_code == 0, result.output
    (finding,) = json.loads(result.output)["findings"]
    assert finding["kind"] == "new_dependencies" and "recent_dependencies" not in finding


def test_the_flag_looks_up_publish_dates(tmp_path, monkeypatch):
    from openshard.verification import dependency_age

    asked: list[str] = []
    monkeypatch.setattr(dependency_age, "fetch_json", lambda url, timeout=5.0: asked.append(url))
    root, base = _dependency_change(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["ci", "findings", "--base", base, "--registry-lookup", "--json"])
    assert result.exit_code == 0, result.output
    (finding,) = json.loads(result.output)["findings"]
    assert asked == ["https://pypi.org/pypi/click/json"]
    assert finding["unknown_age"] == 1 and finding["packages"][0]["recent"] is None
