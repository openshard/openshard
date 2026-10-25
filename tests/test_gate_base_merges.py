"""The merge gate on an agent's pull request after its branch was updated from the base.

GitHub's "Update branch" adds a merge commit with no agent marks on top of the
agent's work. That commit never gets a Receipt; the agent's last commit has
one. The gate judges the agent's change on that commit, but only when every
commit above it merges the base branch in cleanly and adds nothing of its own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from click.testing import CliRunner

from openshard.ci import gate_platform as gp
from openshard.ci.base_merges import base_merges_after_agent
from openshard.ci.merge_gate import STATUS_FAIL, STATUS_PASS, decide
from openshard.cli.main import cli

OIDC_URL = "https://token.actions.example/token?x=1"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "OPENSHARD_GATE_API",
                 "OPENSHARD_GATE_API_KEY", "OPENSHARD_GATE_ORG", "OPENSHARD_GATE_FORK", "GITHUB_EVENT_PATH",
                 "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def pr(tmp_path: Path) -> dict:
    """main: base -> m2.  pr branch: base -> agent commit -> merge of main (clean)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, ".gitignore", ".openshard/\n")
    _write(root, "a.py", "x = 1\n")
    _write(root, "c.py", "c = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "pr")
    _write(root, "a.py", "x = 1\ny = 2\n")
    _git(root, "commit", "-qam", "agent work\n\nOpenshard-Agent: Codex")
    agent = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    _write(root, "c.py", "c = 2\n")
    _git(root, "commit", "-qam", "someone else's change on main")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "pr")
    _git(root, "merge", "-q", "--no-ff", "--no-edit", "main")
    head = _git(root, "rev-parse", "HEAD")
    return {"root": root, "agent": agent, "base": base, "head": head}


class TestBaseMerges:
    def test_update_branch_merge_points_at_the_agent_commit(self, pr):
        found = base_merges_after_agent(pr["root"], pr["head"], pr["base"])
        assert found is not None and found.agent_commit == pr["agent"] and found.merges == (pr["head"],)

    def test_a_merge_that_also_edits_is_not_carried(self, pr):
        root = pr["root"]
        _git(root, "reset", "-q", "--hard", pr["agent"])
        _git(root, "merge", "-q", "--no-ff", "--no-commit", "main")
        _write(root, "a.py", "x = 1\ny = 2\nz = 3\n")  # an extra edit hidden in the merge
        _git(root, "commit", "-qam", "Merge branch 'main' into pr")
        assert base_merges_after_agent(root, _git(root, "rev-parse", "HEAD"), pr["base"]) is None

    def test_a_plain_commit_on_top_is_not_carried(self, pr):
        root = pr["root"]
        _write(root, "a.py", "x = 1\ny = 2\nw = 4\n")
        _git(root, "commit", "-qam", "a human follow-up")
        assert base_merges_after_agent(root, _git(root, "rev-parse", "HEAD"), pr["base"]) is None

    def test_merging_something_not_on_the_base_is_not_carried(self, pr):
        root = pr["root"]
        _git(root, "checkout", "-q", "-b", "side", pr["base"])
        _write(root, "d.py", "d = 1\n")
        _git(root, "add", "d.py")
        _git(root, "commit", "-qm", "unreviewed side branch")
        _git(root, "checkout", "-q", "pr")
        _git(root, "merge", "-q", "--no-ff", "--no-edit", "side")
        assert base_merges_after_agent(root, _git(root, "rev-parse", "HEAD"), pr["base"]) is None

    def test_head_with_its_own_marks_needs_its_own_receipt(self, pr):
        assert base_merges_after_agent(pr["root"], pr["agent"], pr["base"]) is None

    def test_two_updates_in_a_row_are_carried(self, pr):
        root = pr["root"]
        _git(root, "checkout", "-q", "main")
        _write(root, "c.py", "c = 3\n")
        _git(root, "commit", "-qam", "another change on main")
        base = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "-q", "pr")
        _git(root, "merge", "-q", "--no-ff", "--no-edit", "main")
        head = _git(root, "rev-parse", "HEAD")
        found = base_merges_after_agent(root, head, base)
        assert found is not None and found.agent_commit == pr["agent"] and found.merges == (head, pr["head"])

    def test_no_base_or_bad_input_is_none(self, pr):
        assert base_merges_after_agent(pr["root"], pr["head"], None) is None
        assert base_merges_after_agent(pr["root"], "not-a-sha", pr["base"]) is None


def _answer(status: str, sha: str, receipts: list | None = None) -> dict:
    return {"schema": gp.SCHEMA, "sha": sha, "repository": "o/r", "status": status,
            "policy": {"require_receipt": "agent_prs"}, "receipts": receipts or [], "merge_group": None,
            "reason": None, "checked_at": "2026-10-25T10:00:00Z"}


class TestDecide:
    AGENT, HEAD = "a" * 40, "b" * 40

    def _carried(self, status: str) -> dict:
        return {"agent_commit": self.AGENT, "merges": [self.HEAD], "platform": _answer(status, self.AGENT)}

    def test_verified_agent_commit_passes_and_says_why(self):
        r = decide(self.HEAD, _answer("none", self.HEAD), agent_marks=["Openshard-Agent"],
                   carried=self._carried("verified"))
        assert r.status == STATUS_PASS and r.verdict == "verified"
        assert "agent's last commit aaaaaaaaaaaa is verified" in r.headline
        assert "1 later commit only merges the base branch in" in r.headline
        assert "merges the base branch in" in r.summary_markdown

    @pytest.mark.parametrize("status,text", [("failed", "verification failed"), ("pending", "had not finished")])
    def test_failed_or_pending_agent_commit_fails(self, status, text):
        r = decide(self.HEAD, _answer("none", self.HEAD), agent_marks=["Openshard-Agent"],
                   carried=self._carried(status))
        assert r.status == STATUS_FAIL and text in r.headline

    def test_no_receipt_for_the_agent_commit_either_still_fails(self):
        r = decide(self.HEAD, _answer("none", self.HEAD), agent_marks=["Openshard-Agent"],
                   carried=self._carried("none"))
        assert r.status == STATUS_FAIL and "no Receipt" in r.headline

    def test_the_head_own_answer_wins(self):
        r = decide(self.HEAD, _answer("failed", self.HEAD), agent_marks=["Openshard-Agent"],
                   carried=self._carried("verified"))
        assert r.status == STATUS_FAIL and r.verdict == "failed"


def _serve_by_sha(monkeypatch, answers: dict[str, dict], seen: list) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "token.actions.example":
            return httpx.Response(200, json={"value": "jwt"})
        sha = parse_qs(urlparse(str(request.url)).query)["sha"][0]
        seen.append(sha)
        return httpx.Response(200, json=answers[sha])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))


class TestCli:
    def _gate(self, pr, monkeypatch, tmp_path):
        monkeypatch.chdir(pr["root"])
        event = {"pull_request": {"base": {"sha": pr["base"], "repo": {"full_name": "o/r"}},
                                  "head": {"sha": pr["head"], "repo": {"full_name": "o/r"}}, "body": ""}}
        path = tmp_path / "event.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": OIDC_URL, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req",
               "GITHUB_EVENT_PATH": str(path)}
        return CliRunner().invoke(cli, ["ci", "gate", "--poll-seconds", "0.1", "--wait-minutes", "0.01",
                                        "--commit", pr["head"], "--json"], env=env)

    def test_updated_branch_passes_on_the_agent_commit_receipt(self, pr, monkeypatch, tmp_path):
        seen: list = []
        _serve_by_sha(monkeypatch, {pr["head"]: _answer("none", pr["head"]),
                                    pr["agent"]: _answer("verified", pr["agent"])}, seen)
        res = self._gate(pr, monkeypatch, tmp_path)
        body = json.loads(res.stdout)
        assert res.exit_code == 0 and body["verdict"] == "verified"
        assert body["base_merges"] == {"agent_commit": pr["agent"], "merges": [pr["head"]]}
        # The head is asked once (no wait: it can never get a Receipt), then the agent's commit.
        assert seen[0] == pr["head"] and seen.count(pr["head"]) == 1 and pr["agent"] in seen

    def test_updated_branch_fails_when_the_agent_commit_has_no_receipt(self, pr, monkeypatch, tmp_path):
        seen: list = []
        _serve_by_sha(monkeypatch, {pr["head"]: _answer("none", pr["head"]),
                                    pr["agent"]: _answer("none", pr["agent"])}, seen)
        res = self._gate(pr, monkeypatch, tmp_path)
        assert res.exit_code == 1 and "an agent changed code but there is no Receipt" in json.loads(res.stdout)["headline"]
