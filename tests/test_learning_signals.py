"""Learning signals: derived from observed evidence only, sized honestly, privacy-bounded."""
from __future__ import annotations

import json
import random

from openshard.learning.signals import (
    KIND_CHECK,
    KIND_FAILURE,
    KIND_MODEL_OUTCOMES,
    KIND_POLICY,
    KIND_RECOVERY,
    STALE,
    STRENGTH_ANECDOTAL,
    STRENGTH_MODERATE,
    STRENGTH_STRONG,
    STRENGTH_WEAK,
    derive_signals,
    observe,
    read_entries,
    signal_id_for,
    strength_for,
    task_terms,
)
from tests.learning_fixtures import MOBILE_CHECK, NOW, REPO, UNIT_CHECK, osn_entry


def _derive(entries, repo=REPO):
    return derive_signals(entries, repo=repo, now=NOW)


def _of(index, kind):
    return [s for s in index.signals if s.kind == kind]


def test_no_history_yields_no_signals():
    idx = _derive([])
    assert idx.signals == () and idx.receipts_observed == 0 and idx.entries_scanned == 0


def test_model_outcomes_count_first_attempt_and_retries_by_task_category():
    entries = [
        osn_entry(attempts=[("model/a", "passed")]),
        osn_entry(attempts=[("model/a", "passed")]),
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")]),
    ]
    idx = _derive(entries)
    [a] = [s for s in _of(idx, KIND_MODEL_OUTCOMES) if s.subject == {"model": "model/a"}]
    assert a.task_category == "visual" and a.samples == 3 and a.strength == STRENGTH_MODERATE
    assert a.stats["first_attempt_passed"] == 2 and a.stats["first_attempt_failed"] == 1
    assert a.stats["runs_needing_retry"] == 1 and a.stats["runs_verified"] == 3
    assert "2 of 3 recorded runs" in a.summary
    # Observations, never rankings.
    for word in ("best", "better", "worse", "recommend"):
        assert word not in a.summary.lower()
    # A model that only ever ran as the recovery attempt is not credited with first attempts.
    assert not [s for s in _of(idx, KIND_MODEL_OUTCOMES) if s.subject == {"model": "model/b"}]


def test_one_receipt_is_anecdotal_and_never_surfaceable():
    idx = _derive([osn_entry()])
    [s] = _of(idx, KIND_MODEL_OUTCOMES)
    assert s.strength == STRENGTH_ANECDOTAL and not s.surfaceable
    assert [strength_for(n) for n in (1, 2, 3, 5)] == [STRENGTH_ANECDOTAL, STRENGTH_WEAK, STRENGTH_MODERATE,
                                                        STRENGTH_STRONG]


def test_missing_cost_is_never_treated_as_zero():
    known = [osn_entry(cost=0.02) for _ in range(3)]
    idx = _derive(known)
    [s] = _of(idx, KIND_MODEL_OUTCOMES)
    assert s.stats["cost_per_verified_success_usd"] == 0.02
    assert s.stats["cost_basis"] == "provider_reported_estimates"

    one_unknown = [osn_entry(cost=0.02), osn_entry(cost=0.02), osn_entry(cost=None)]
    [s] = _of(_derive(one_unknown), KIND_MODEL_OUTCOMES)
    assert s.stats["cost_per_verified_success_usd"] is None
    assert s.stats["cost_basis"] == "insufficient_cost_evidence"
    assert "$" not in s.summary

    too_few = [osn_entry(cost=0.02), osn_entry(cost=0.02)]
    [s] = _of(_derive(too_few), KIND_MODEL_OUTCOMES)
    assert s.stats["cost_per_verified_success_usd"] is None


def test_failed_verification_becomes_a_recurring_failure_signal():
    entries = [osn_entry(attempts=[("model/a", "failed")]) for _ in range(2)] + [osn_entry()]
    [f] = _of(_derive(entries), KIND_FAILURE)
    assert f.subject == {"failure_category": "verification_failed"}
    assert f.samples == 2 and f.stats["comparable_runs"] == 3
    assert f.summary.startswith("2 of 3 recorded runs on visual tasks failed")
    assert f.stats["operational"] is False


def test_operational_failures_are_labelled_operational_not_model_quality():
    entries = [osn_entry(attempts=[("model/a", "setup")]) for _ in range(2)]
    idx = _derive(entries)
    [f] = _of(idx, KIND_FAILURE)
    assert f.subject["failure_category"] == "verification_infra_error" and f.stats["operational"] is True
    # A verifier that could not run is not evidence about the model at all.
    assert _of(idx, KIND_MODEL_OUTCOMES) == []


def test_recovery_path_signal_records_failed_model_and_rescuer():
    entries = [
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")]),
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")]),
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "failed")]),
    ]
    [r] = _of(_derive(entries), KIND_RECOVERY)
    assert r.subject == {"from_model": "model/a", "to_model": "model/b"}
    assert r.stats == {"attempted": 3, "recovered": 2, "not_recovered": 1}
    assert "passed in 2 of 3" in r.summary


def test_recurring_check_signal_counts_runs_the_check_caught():
    entries = [
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry(attempts=[("model/a", "failed"), ("model/a", "passed")], check=MOBILE_CHECK),
        osn_entry(attempts=[("model/a", "failed")], check=MOBILE_CHECK),
        osn_entry(attempts=[("model/a", "passed")], check=MOBILE_CHECK),
        osn_entry(attempts=[("model/a", "passed")], check=UNIT_CHECK),
        osn_entry(attempts=[("model/a", "passed")], check=UNIT_CHECK),
    ]
    checks = _of(_derive(entries), KIND_CHECK)
    # A check that never failed has taught nothing yet: no signal for UNIT_CHECK.
    [c] = checks
    assert c.subject["check_label"] == "pnpm test:e2e -- mobile"
    assert c.stats == {"runs": 4, "runs_caught": 3, "failed_attempts": 3, "caught_then_passed": 2}
    assert "caught a failure in 3 of 4" in c.summary


def test_policy_boundary_signal_uses_areas_and_decisions_not_raw_paths():
    decisions = [{"decision": "ask", "resource": ".github/workflows/ci.yml", "decision_id": "d1"}]
    entries = [osn_entry(policy_decisions=decisions) for _ in range(2)]
    [p] = _of(_derive(entries), KIND_POLICY)
    assert p.subject == {"decision": "ask", "area": ".github/workflows"}
    assert "ci.yml" not in json.dumps(p.to_dict())


def test_other_repositories_and_unmarked_entries_are_not_learned_from():
    entries = [osn_entry(repo="github.com/other/repo") for _ in range(3)] + [osn_entry(repo=None) for _ in range(3)]
    idx = _derive(entries)
    assert idx.signals == () and idx.receipts_observed == 0 and idx.entries_scanned == 6


def test_agent_reported_and_imported_outcomes_are_excluded_not_counted():
    claimed = osn_entry(source="agent_reported")
    claimed.pop("osn_loop")
    claimed["executor"] = "claude_code_hooks"
    imported = osn_entry()
    imported["verification"]["observation_mode"] = "imported_transcript"
    idx = _derive([claimed, imported])
    assert idx.receipts_with_evidence == 0
    assert idx.excluded == {"agent_reported": 1, "imported_history": 1}
    assert _of(idx, KIND_MODEL_OUTCOMES) == []


def test_stale_history_is_marked_stale_and_not_surfaceable():
    idx = _derive([osn_entry(days_ago=200) for _ in range(5)])
    [s] = _of(idx, KIND_MODEL_OUTCOMES)
    assert s.strength == STRENGTH_STRONG and s.freshness == STALE and not s.surfaceable


def test_unknown_fields_are_ignored_and_corrupted_records_are_skipped(tmp_path):
    good = osn_entry()
    good["future_field"] = {"nested": [1, 2, 3]}
    weird = osn_entry()
    weird["osn_loop"]["attempts"] = ["not a dict", {"n": "x", "verification": "nope"}]
    weird["routing_provenance"] = "garbage"
    weird["retry_attempts"] = "garbage"
    runs = tmp_path / "runs.jsonl"
    runs.write_text(
        "\n".join([json.dumps(good), "{not json", "[1, 2]", "", json.dumps(weird), json.dumps("str")]) + "\n",
        encoding="utf-8",
    )
    entries, bad = read_entries(runs)
    assert len(entries) == 2 and bad == 3
    idx = derive_signals([*entries, None, 42], repo=REPO, now=NOW)
    assert idx.unreadable == 2 and idx.receipts_observed == 2
    assert observe("nope") is None and observe({}) is None


def test_duplicate_receipts_count_once_and_shared_shard_ids_are_not_merged():
    a = osn_entry(receipt_id="rcpt-dup")
    b = dict(a)  # the same Receipt recorded twice
    c = osn_entry()
    c["shard_id"] = a["shard_id"]  # a colliding hook-style shard id is a different Receipt
    idx = _derive([a, b, c])
    assert idx.receipts_observed == 2
    [s] = _of(idx, KIND_MODEL_OUTCOMES)
    assert s.samples == 2


def test_privacy_no_secrets_absolute_paths_or_task_text_in_signals():
    secret_task = "Fix dashboard layout using key sk-abcdefghijklmnopqrstuvwx in C:/Users/me/secret.txt"
    entries = [
        osn_entry(task=secret_task, files=("C:/Users/me/abs.py", "../escape.py", "src/ui/a.tsx"),
                  attempts=[("model/a", "failed")])
        for _ in range(3)
    ]
    idx = _derive(entries)
    blob = json.dumps([s.to_dict() for s in idx.signals])
    for leaked in ("abcdefghijklmnopqrstuvwx", "C:/Users", "abs.py", "escape", "secret.txt", secret_task):
        assert leaked not in blob
    assert "src/ui" in blob  # repo-relative areas are kept
    assert "abcdefghijklmnopqrstuvwx" not in task_terms(secret_task)


def test_signal_ids_and_ordering_are_deterministic():
    entries = [
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry(attempts=[("model/b", "passed")], category="standard", task="Refactor the cart totals"),
        osn_entry(attempts=[("model/b", "passed")], category="standard", task="Refactor the cart totals"),
    ]
    first = [s.to_dict() for s in _derive(entries).signals]
    shuffled = list(entries)
    random.Random(7).shuffle(shuffled)
    assert [s.to_dict() for s in _derive(shuffled).signals] == first
    assert signal_id_for("k", "Repo", "c", {"m": 1}) == signal_id_for("k", "repo", "c", {"m": 1})
    assert len({s["signal_id"] for s in first}) == len(first)


def test_signals_trace_back_to_supporting_receipts():
    entries = [osn_entry(attempts=[("model/a", "failed")]) for _ in range(3)]
    [f] = _of(_derive(entries), KIND_FAILURE)
    assert set(f.receipt_ids) == {e["receipt_id"] for e in entries}
    assert set(f.shard_ids) == {e["shard_id"] for e in entries}
    assert f.first_seen and f.last_seen and f.evidence_sources == ("directly_observed",)


def test_first_model_is_unknown_rather_than_guessed_for_old_retried_runs():
    old = osn_entry(attempts=[("model/a", "failed"), ("model/b", "passed")], record_models=False)
    obs = observe(old)
    assert obs is not None
    assert obs.attempts[0].model is None and obs.attempts[1].model == "model/b"


def test_check_strength_reflects_failures_caught_not_runs_observed():
    entries = [osn_entry(attempts=[("model/a", "failed"), ("model/a", "passed")], check=MOBILE_CHECK)
               for _ in range(2)]
    entries += [osn_entry(attempts=[("model/a", "passed")], check=MOBILE_CHECK) for _ in range(4)]
    [c] = _of(_derive(entries), KIND_CHECK)
    assert c.samples == 6 and c.stats["runs_caught"] == 2
    assert c.strength == STRENGTH_WEAK
