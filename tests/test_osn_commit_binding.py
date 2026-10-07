"""``osn run --promote --commit``: the verified result becomes a commit, and verification is bound to it.

Promotion copies the verified files into the repository through the policy
gate; ``--commit`` then commits exactly those files on the current branch and
re-runs the run's own verification command on that commit through the
post-session verification path, recording evidence bound to the commit only
when the tree was clean. The Receipt carries the commit OpenShard created and
observed (``git_end_head`` / ``session_commits``), so the hosted Receipt can
show the commit and a verification that is really tied to it.
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history import receipt_evidence as ev
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.verification_truth import BASIS_POST_SESSION, interpret_receipt
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.verification.post_session import summarize_attestation

PY = sys.executable
VERIFY = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "osn@example.com")
    _git(repo, "config", "user.name", "OSN Test")
    (repo / "out.txt").write_text("bad")
    (repo / ".gitignore").write_text(".openshard/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run(monkeypatch, repo, fake, *extra):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd", VERIFY,
                                    "--roles", "executor", "--json", *extra])


def _last_entry(repo):
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]


def test_commit_binds_verification_to_the_new_commit(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    r = _run(monkeypatch, repo, FakeProvider([json.dumps({"writes": [{"path": "out.txt", "content": "ok"}]})]),
             "--promote", "--commit")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["status"] == "verified" and body["promoted"] == ["out.txt"]
    sha = body["commit"]["sha"]
    assert sha and sha == _git(repo, "rev-parse", "HEAD") and sha != base
    assert body["commit"]["files"] == ["out.txt"] and body["commit"]["branch"] in ("master", "main")
    assert _git(repo, "status", "--porcelain") == ""  # exactly the promoted file was committed
    assert "Receipt:" in _git(repo, "log", "-1", "--format=%B")

    bound = body["bound_verification"]
    assert bound["bound"] is True and bound["artifact_sha"] == sha and bound["status"] == "passed"
    assert bound["source"] == "directly_observed"

    entry = _last_entry(repo)
    assert entry["git_end_head"] == sha and entry["session_commits"]["shas"] == [sha]
    assert entry["osn_commit"]["source"] == "openshard_committed"
    assert ev.commit_value(entry) == sha  # the Receipt's "commit" is the one OpenShard created and observed
    assert entry["git_head_commit_hash"] == base  # the base the run started from is unchanged

    attestations = [json.loads(x) for x in (repo / ".openshard" / "verifications.jsonl").read_text().splitlines()]
    assert attestations[-1]["receipt_id"] == entry["receipt_id"]
    assert attestations[-1]["verification"]["artifact_sha"] == sha
    receipt = build_shard_receipt(entry, index=0, post_session_verification=summarize_attestation(attestations[-1]))
    truth = interpret_receipt(receipt)
    assert truth.basis == BASIS_POST_SESSION and truth.artifact_sha == sha
    assert truth.effective_status == "passed" and truth.state == "verified_passed"
    assert truth.post_session_artifact_sha == sha


def test_commit_is_not_bound_when_the_tree_is_dirty_and_says_so(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "scratch.txt").write_text("uncommitted local work")  # untracked: not swept into the commit
    r = _run(monkeypatch, repo, FakeProvider([json.dumps({"writes": [{"path": "out.txt", "content": "ok"}]})]),
             "--promote", "--commit")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["commit"]["sha"] and body["commit"]["files"] == ["out.txt"]
    assert "scratch.txt" not in _git(repo, "show", "--name-only", "--format=", "HEAD")
    bound = body["bound_verification"]
    assert bound["status"] == "passed" and bound["bound"] is False and bound["artifact_sha"] is None
    assert bound["reason"] == "working_tree_not_clean"
    entry = _last_entry(repo)
    assert ev.commit_value(entry) == body["commit"]["sha"]  # the commit is real; only the binding is withheld


def test_commit_title_prefers_what_the_run_recorded_and_stays_short():
    from openshard.cli.osn_cmd import _commit_title

    long_task = "In openshard/osn/run_entry.py, build_osn_run_entry mints shard_id with `_make_shard_id(entry['timestamp'], None)`, so every Receipt gets 0001. Fix it."
    entry = {"osn_loop": {"attempts": [{"final_note": "Implemented OSN shard ids from the receipt count."}],
                          "plan": {"summary": "Add run_index to the OSN entry builder"}}}
    assert _commit_title(long_task, entry) == "Implemented OSN shard ids from the receipt count"
    assert _commit_title(long_task, {"osn_loop": {"attempts": [], "plan": {"summary": "Add run_index to the OSN entry builder"}}}) == "Add run_index to the OSN entry builder"
    title = _commit_title(long_task, {})
    assert len(title) <= 72 and title.endswith("…") and not title.rstrip("…").endswith("`")
    assert _commit_title("make out ok", {}) == "make out ok"


def test_commit_requires_promote_and_a_failed_run_commits_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _run(monkeypatch, repo, FakeProvider([json.dumps({"writes": [{"path": "out.txt", "content": "ok"}]})]),
             "--commit")
    assert r.exit_code != 0 and "--commit requires --promote" in r.output
    base = _git(repo, "rev-parse", "HEAD")
    r = _run(monkeypatch, repo, FakeProvider([json.dumps({"writes": [{"path": "out.txt", "content": "still bad"}]})]),
             "--promote", "--commit", "--max-attempts", "1")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["status"] == "failed" and body["commit"] is None and body["bound_verification"] is None
    assert _git(repo, "rev-parse", "HEAD") == base
    entry = _last_entry(repo)
    assert "session_commits" not in entry and ev.commit_value(entry) is None
