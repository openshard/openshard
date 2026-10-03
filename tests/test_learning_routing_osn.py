"""Adaptive Routing V2 under the capability, through the real CLI, with repo/task-scoped history."""
from __future__ import annotations

import json
import subprocess

import pytest

from tests import test_adaptive_routing_osn as base
from tests.learning_fixtures import osn_entry, publish_learning
from tests.test_adaptive_routing_osn import FakeProvider, _invoke, _last_run, _writes

# The capability-on CLI harness: a fake Platform serving capabilities and a fixed catalog.
platform = base.platform
catalog = base.catalog


pytestmark = pytest.mark.usefixtures("generous_learning_budget")


def _cli_repo(tmp_path, monkeypatch, platform, keys):
    repo = base._cli_repo(tmp_path, monkeypatch, platform, keys)
    # Its own git root, so the repository identity is this folder and not an ancestor checkout.
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _seed(repo, entries):
    with (repo / ".openshard" / "runs.jsonl").open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    publish_learning(repo)


def _history(model, n, passed, *, repo="repo"):
    state = "passed" if passed else "failed"
    return [osn_entry("write ok into the out file", attempts=[(model, state)], repo=repo, category="standard")
            for _ in range(n)]


def test_scoped_history_chooses_the_model_and_the_receipt_says_so(tmp_path, monkeypatch, platform, catalog):
    repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
    # In this repository, for this kind of task, acme/mid-1 has never passed verification.
    _seed(repo, _history("acme/mid-1", 5, False) + _history("zeta/mid-2", 5, True))
    fp = FakeProvider([_writes(("out.txt", "ok"))])
    r = _invoke(monkeypatch, fp)
    assert r.exit_code == 0, r.output
    assert fp.calls[0][0] == "zeta/mid-2"
    entry = _last_run(repo)
    history = entry["adaptive_routing"]["history"]
    assert history["used"] is True and history["scope"] == "repo_task_category"
    assert history["repo"] == "repo" and history["task_category"] == "standard"
    assert entry["learning"]["routing"] == {
        "influenced": True, "reason": "history_evidence_used", "history_scope": "repo_task_category",
    }
    assert json.loads(r.stdout)["learning"]["routing_influenced"] is True


def test_thin_history_leaves_the_capability_decision_unchanged(tmp_path, monkeypatch, platform, catalog):
    repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
    _seed(repo, _history("acme/mid-1", 2, False) + _history("zeta/mid-2", 2, True))
    fp = FakeProvider([_writes(("out.txt", "ok"))])
    r = _invoke(monkeypatch, fp)
    assert r.exit_code == 0, r.output
    assert fp.calls[0][0] == "acme/mid-1"  # the decision the policy makes with no usable history
    entry = _last_run(repo)
    assert entry["adaptive_routing"]["history"]["used"] is False
    assert entry["adaptive_routing"]["history"]["scoped"]["reason"] == "insufficient_observed_data"
    assert entry["learning"]["routing"]["influenced"] is False


def test_no_learning_keeps_the_harness_wide_history_path(tmp_path, monkeypatch, platform, catalog):
    repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
    _seed(repo, _history("acme/mid-1", 5, False) + _history("zeta/mid-2", 5, True))
    fp = FakeProvider([_writes(("out.txt", "ok"))])
    r = _invoke(monkeypatch, fp, "--no-learning")
    assert r.exit_code == 0, r.output
    history = _last_run(repo)["adaptive_routing"]["history"]
    # Routing still reads observed history exactly as before learning existed.
    assert "scope" not in history and history["used"] is True
