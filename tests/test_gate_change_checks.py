"""The merge gate shows the PR findings ("Change checks") and they never change pass or fail."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from openshard.ci import gate_platform as gp
from openshard.ci.merge_gate import decide, render_findings
from openshard.cli.main import cli
from openshard.verification import dependency_age

OIDC_URL = "https://token.actions.example/token?api-version=2.0"
FINDING = {"kind": "grader_touched", "detail": "the change edits what checks it (ci config)",
           "paths": [".github/workflows/ci.yml"], "count": 1, "categories": ["ci_config"]}


def _verdict(status: str, sha: str) -> dict:
    return {"schema": gp.SCHEMA, "sha": sha, "repository": "o/r", "status": status,
            "policy": {"require_receipt": "agent_prs"}, "receipts": [], "merge_group": None, "reason": None,
            "checked_at": "2026-10-22T10:00:00Z"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "OPENSHARD_GATE_API",
                 "OPENSHARD_GATE_API_KEY", "OPENSHARD_GATE_ORG", "OPENSHARD_GATE_FORK", "GITHUB_EVENT_PATH",
                 "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dependency_age, "fetch_json", lambda url: None)  # never the network


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "factory"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".openshard/\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _serve(monkeypatch, status: str, sha: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "token.actions.example":
            return httpx.Response(200, json={"value": "jwt"})
        return httpx.Response(200, json=_verdict(status, sha))
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))


def _gate(repo: Path, monkeypatch, tmp_path: Path, base: str, head: str, *args: str):
    monkeypatch.chdir(repo)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "title": "Make the suite faster", "body": "",
        "base": {"sha": base, "repo": {"full_name": "o/r"}}, "head": {"sha": head, "repo": {"full_name": "o/r"}}}}),
        encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {"ACTIONS_ID_TOKEN_REQUEST_URL": OIDC_URL, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req",
           "GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary)}
    result = CliRunner().invoke(cli, ["ci", "gate", "--poll-seconds", "0.1", "--commit", head,
                                      "--github-step-summary", *args], env=env)
    return result, summary.read_text(encoding="utf-8") if summary.exists() else ""


def _weaken(repo: Path) -> str:
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("leftpad-py\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "speed up")
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(("status", "exit_code"), [("verified", 0), ("failed", 1)])
def test_findings_are_shown_and_the_result_is_the_platforms(repo, monkeypatch, tmp_path, status, exit_code):
    base = _git(repo, "rev-parse", "HEAD")
    head = _weaken(repo)
    _serve(monkeypatch, status, head)
    result, summary = _gate(repo, monkeypatch, tmp_path, base, head, "--json")
    assert result.exit_code == exit_code, result.output
    data = json.loads(result.output)
    kinds = [f["kind"] for f in data["change_findings"]]
    assert kinds == ["grader_touched", "new_dependencies"]
    deps = data["change_findings"][1]
    assert deps["packages"][0]["recent"] is None and deps["unknown_age"] == 1  # lookup failed: unknown
    assert "#### Change checks" in summary and "Information only" in summary
    assert "tests/test\\_a.py" in summary


def test_without_a_base_the_findings_are_not_checked(repo, monkeypatch, tmp_path):
    head = _weaken(repo)
    _serve(monkeypatch, "verified", head)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["ci", "gate", "--poll-seconds", "0.1", "--commit", head, "--json"],
                                env={"ACTIONS_ID_TOKEN_REQUEST_URL": OIDC_URL, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"})
    assert result.exit_code == 0
    assert json.loads(result.output)["change_findings"] is None


def test_decide_never_changes_status_for_findings():
    for status in ("verified", "failed", "pending"):
        plain = decide("a" * 40, _verdict(status, "a" * 40))
        shown = decide("a" * 40, _verdict(status, "a" * 40), change_findings=[FINDING])
        assert (plain.status, plain.exit_code, plain.headline) == (shown.status, shown.exit_code, shown.headline)
        assert "Change checks" not in plain.summary_markdown
        assert "Change checks" in shown.summary_markdown


def test_rendering_states_unchecked_and_empty_plainly_and_escapes_text():
    assert "Not checked" in "\n".join(render_findings(None))
    assert "Nothing found." in render_findings([])
    rows = "\n".join(render_findings([{**FINDING, "detail": "<script>|x"}]))
    assert "<script>" not in rows and "&lt;script&gt;\\|x" in rows
