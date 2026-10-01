"""``openshard learn`` and learning impact: human-readable, honest about samples, read-only."""
from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.learning.impact import (
    COHORT_NOT_USED,
    COHORT_UNRECORDED,
    COHORT_USED,
    DISCLAIMER,
    measure,
)
from openshard.learning.signals import derive_signals
from tests.learning_fixtures import MOBILE_CHECK, NOW, REPO, osn_entry


def _repo(tmp_path, monkeypatch, entries=()):
    r = tmp_path / "shop"
    r.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    store = r / ".openshard"
    store.mkdir()
    (store / "runs.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    monkeypatch.chdir(r)
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: r.resolve())
    return r


def _learn(*args):
    return CliRunner().invoke(cli, ["learn", *args])


def _history():
    t = "Fix responsive dashboard layout"
    return [
        osn_entry(t, attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK, repo="shop"),
        osn_entry(t, attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK, repo="shop"),
        osn_entry("Dashboard layout grid", attempts=[("model/b", "passed")], check=MOBILE_CHECK, repo="shop"),
        osn_entry("Rename the billing export", category="standard", repo="shop"),
    ]


def test_signals_with_no_history_say_so(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    r = _learn("signals")
    assert r.exit_code == 0, r.output
    assert "0 recorded" in r.output and "No signals with enough evidence yet." in r.output


def test_signals_lists_by_task_class_and_hides_anecdotes(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch, _history())
    r = _learn("signals")
    assert r.exit_code == 0, r.output
    assert "Task class: visual" in r.output and "Task class: standard" not in r.output
    assert "`pnpm test:e2e -- mobile` caught a failure in 2 of 3" in r.output
    assert "hidden; --all shows them" in r.output
    shown_all = _learn("signals", "--all")
    assert "Task class: standard" in shown_all.output
    # No giant dumps by default.
    assert "{" not in r.output


def test_signals_for_a_task_explain_why_and_recommend_checks(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch, _history())
    r = _learn("signals", "Update", "the", "dashboard", "analytics", "layout")
    assert r.exit_code == 0, r.output
    assert "Task class   visual" in r.output and "Signals OSN would supply (advisory)" in r.output
    assert "why: same_repo, same_task_category, task_terms:dashboard,layout" in r.output
    assert "Check history suggests: `pnpm test:e2e -- mobile`" in r.output and "not run automatically" in r.output
    body = json.loads(_learn("signals", "Update the dashboard analytics layout", "--json").output)
    assert body["status"] == "used" and body["task_shape"]["task_category"] == "visual"
    assert body["recommended_checks"] == ["pnpm test:e2e -- mobile"]
    assert all(s["reasons"][0] == "same_repo" for s in body["signals"])

    unrelated = _learn("signals", "Rotate the ledger database credentials")
    assert "No relevant signals" in unrelated.output


def test_inspect_shows_counts_and_supporting_receipts(tmp_path, monkeypatch):
    entries = _history()
    _repo(tmp_path, monkeypatch, entries)
    body = json.loads(_learn("signals", "--json").output)
    check = next(s for s in body["signals"] if s["kind"] == "recurring_check_failure")
    r = _learn("inspect", check["signal_id"])
    assert r.exit_code == 0, r.output
    assert "runs_caught: 2" in r.output and "directly_observed" in r.output
    assert entries[0]["receipt_id"] in r.output
    as_json = json.loads(_learn("inspect", check["signal_id"], "--json").output)
    assert as_json["signal_id"] == check["signal_id"] and as_json["stats"]["runs_caught"] == 2
    missing = _learn("inspect", "ls_000000000000")
    assert missing.exit_code != 0 and "No signal ls_000000000000" in missing.output


def test_inspect_explains_why_an_anecdote_is_not_surfaced(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch, _history())
    body = json.loads(_learn("signals", "--all", "--json").output)
    anecdote = next(s for s in body["signals"] if s["strength"] == "anecdotal")
    r = _learn("inspect", anecdote["signal_id"])
    assert "Not surfaced to OSN: one Receipt is an anecdote." in r.output


def test_last_reports_learning_and_outcome(tmp_path, monkeypatch):
    used = osn_entry("Update the dashboard analytics layout", repo="shop", learning={
        "used": True, "status": "used", "signals_used": 2, "context_supplied": True,
        "signal_ids": ["ls_aaaaaaaaaaaa", "ls_bbbbbbbbbbbb"],
        "signals": [{"signal_id": "ls_aaaaaaaaaaaa", "kind": "recurring_check_failure", "strength": "weak",
                     "reasons": ["same_repo", "same_task_category"]}],
        "routing": {"influenced": False, "reason": "adaptive_routing_not_governing"},
        "verification": {"influenced": False, "recommended_checks": [{"label": "pnpm test:e2e -- mobile"}]},
    })
    _repo(tmp_path, monkeypatch, [*_history(), used])
    r = _learn("last")
    assert r.exit_code == 0, r.output
    assert "2 prior verified signal(s) considered" in r.output and "supplied to the model" in r.output
    assert "Routing      influenced: no" in r.output and "suggested check: `pnpm test:e2e -- mobile`" in r.output
    assert "verification passed · 1 attempt(s) · cost $0.0100" in r.output
    body = json.loads(_learn("last", "--json").output)
    assert body["learning"]["signals_used"] == 2 and body["outcome"]["verification"] == "passed"


def test_last_without_an_osn_run_is_an_error_and_old_runs_say_not_recorded(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    assert _learn("last").exit_code != 0
    old = osn_entry(repo="shop", record_models=False)
    _repo(tmp_path / "b", monkeypatch, [old])
    assert "not recorded (this run predates Learning Loop V1)" in _learn("last").output


class TestImpact:
    def _obs(self):
        used = {"used": True, "signal_ids": ["ls_check"]}
        not_used = {"used": False}
        return [
            osn_entry(repo=REPO, learning=used, attempts=[("m", "passed")]),
            osn_entry(repo=REPO, learning=used, attempts=[("m", "failed"), ("m", "passed")]),
            osn_entry(repo=REPO, learning=not_used, attempts=[("m", "failed")]),
            osn_entry(repo=REPO, learning=not_used, attempts=[("m", "passed")], cost=None),
            osn_entry(repo=REPO, record_models=False, attempts=[("m", "passed")]),
        ]

    def test_cohorts_count_outcomes_and_keep_unknown_cost_unknown(self):
        report = measure(derive_signals(self._obs(), repo=REPO, now=NOW))
        used = report.cohort(COHORT_USED)
        assert (used.runs, used.observed, used.verified_successes, used.first_attempt_passed, used.retried) == \
            (2, 2, 2, 1, 1)
        assert used.cost_per_verified_success_usd == 0.015  # (0.01 + 0.01 + 0.01) / 2
        not_used = report.cohort(COHORT_NOT_USED)
        assert not_used.runs == 2 and not_used.verified_successes == 1
        assert not_used.cost_per_verified_success_usd is None  # one run's cost was not reported
        assert report.cohort(COHORT_UNRECORDED).runs == 1
        [f] = report.followups
        assert (f.signal_id, f.later_runs, f.verified_successes) == ("ls_check", 2, 2)

    def test_rates_only_with_enough_runs_and_always_the_disclaimer(self):
        d = measure(derive_signals(self._obs(), repo=REPO, now=NOW)).to_dict()
        assert all("verified_success_rate" not in c for c in d["cohorts"])
        assert d["disclaimer"] == DISCLAIMER and "not evidence that learning caused" in DISCLAIMER
        many = [osn_entry(repo=REPO, learning={"used": True}) for _ in range(5)]
        big = measure(derive_signals(many, repo=REPO, now=NOW)).to_dict()
        assert big["cohorts"][0]["verified_success_rate"] == 1.0

    def test_cli(self, tmp_path, monkeypatch):
        entries = [osn_entry(repo="shop", learning={"used": True, "signal_ids": ["ls_x"]}),
                   osn_entry(repo="shop", learning={"used": False})]
        _repo(tmp_path, monkeypatch, entries)
        r = _learn("impact")
        assert r.exit_code == 0, r.output
        assert "Used learning" in r.output and "verified 1/1 observed" in r.output
        assert "Before V1         no runs" in r.output and DISCLAIMER in r.output
        assert json.loads(_learn("impact", "--json").output)["cohorts"][0]["runs"] == 1
