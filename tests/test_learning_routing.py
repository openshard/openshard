"""Task-scoped history for Adaptive Routing V2: narrower evidence first, same gate, same fallback."""
from __future__ import annotations

import json

from openshard.learning.routing import (
    SCOPE_HARNESS,
    SCOPE_TASK,
    ScopedHistoryEvidence,
    load_scoped_history,
)
from openshard.routing.adaptive.history_evidence import HistoryEvidence, load_history_evidence
from openshard.routing.adaptive.policy_v2 import TrajectoryPolicyV2
from tests.learning_fixtures import REPO, osn_entry
from tests.test_routing_v2_trajectory import _ctx, _decide


def _runs(tmp_path, entries):
    p = tmp_path / "runs.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


def _outcomes(model, n, passed, *, repo=REPO, category="standard", cost=0.01):
    state = "passed" if passed else "failed"
    return [osn_entry("Tidy the cart totals", attempts=[(model, state)], repo=repo, category=category, cost=cost)
            for _ in range(n)]


def _broad_favours_mid1():
    # Across other repositories, acme/mid-1 looks cheap and reliable.
    return (_outcomes("acme/mid-1", 10, True, repo="github.com/other/app", cost=0.001)
            + _outcomes("zeta/mid-2", 6, True, repo="github.com/other/app", cost=0.05))


def test_repo_task_history_decides_when_it_clears_the_same_gate(tmp_path):
    # In *this* repository and task category, acme/mid-1 has failed verification every time.
    scoped = _outcomes("acme/mid-1", 5, False) + _outcomes("zeta/mid-2", 5, True)
    runs = _runs(tmp_path, _broad_favours_mid1() + scoped)

    broad_only = _decide(_ctx(), policy=TrajectoryPolicyV2(history=load_history_evidence(runs, harness="osn_loop")))
    assert broad_only.selected_model == "acme/mid-1"

    history = load_scoped_history(runs, harness="osn_loop", repo=REPO, task_category="standard")
    d = _decide(_ctx(), policy=TrajectoryPolicyV2(history=history))
    assert d.selected_model == "zeta/mid-2"
    assert d.history_evidence["used"] is True
    assert d.history_evidence["scope"] == SCOPE_TASK
    assert d.history_evidence["repo"] == REPO and d.history_evidence["task_category"] == "standard"


def test_thin_scoped_history_falls_back_to_the_unchanged_harness_decision(tmp_path):
    # Four good runs is not enough to route on, however good they look.
    scoped = _outcomes("zeta/mid-2", 4, True)
    runs = _runs(tmp_path, _broad_favours_mid1() + scoped)
    before = _decide(_ctx(), policy=TrajectoryPolicyV2(history=load_history_evidence(runs, harness="osn_loop")))
    history = load_scoped_history(runs, harness="osn_loop", repo=REPO, task_category="standard")
    after = _decide(_ctx(), policy=TrajectoryPolicyV2(history=history))
    assert after.selected_model == before.selected_model
    assert [r["model"] for r in after.ranking] == [r["model"] for r in before.ranking]
    rec = after.history_evidence
    assert rec["scope"] == SCOPE_HARNESS
    assert rec["scoped"]["reason"] == "insufficient_observed_data"
    assert {k: v for k, v in rec.items() if k not in ("scope", "scoped")} == before.history_evidence


def test_no_history_at_all_changes_nothing(tmp_path):
    runs = tmp_path / "missing.jsonl"
    history = load_scoped_history(runs, harness="osn_loop", repo=REPO, task_category="standard")
    d = _decide(_ctx(), policy=TrajectoryPolicyV2(history=history))
    base = _decide(_ctx(), policy=TrajectoryPolicyV2(history=HistoryEvidence(harness="osn_loop")))
    assert d.selected_model == base.selected_model and d.history_evidence["used"] is False


def test_without_a_task_shape_the_broad_evidence_is_returned_as_before(tmp_path):
    runs = _runs(tmp_path, _broad_favours_mid1())
    assert isinstance(load_scoped_history(runs, harness="osn_loop", repo=None, task_category="standard"),
                      HistoryEvidence)
    assert isinstance(load_scoped_history(runs, harness="osn_loop", repo=REPO, task_category=None),
                      HistoryEvidence)
    assert isinstance(load_scoped_history(runs, harness="osn_loop", repo=REPO, task_category="standard"),
                      ScopedHistoryEvidence)

