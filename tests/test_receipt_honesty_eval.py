"""The Receipt honesty eval: every scenario holds, and every rule catches its violation.

The first group runs each scenario end to end (real hook fold, real OSN loop,
real ``openshard verify`` re-runs) and fails if any Receipt surface claims
more than its evidence supports. The second group proves the rules have
teeth: each reintroduces one kind of dishonesty, in the surface read-back or
in Core itself, and requires the matching rule to fire.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from openshard.evals import receipt_honesty as rh

_BY_ID = {s.id: s for s in rh.SCENARIOS}


def _rules(result: rh.ScenarioResult) -> set[str]:
    return {v.rule for v in result.violations}


def _explain(result: rh.ScenarioResult) -> str:
    return result.error or "\n".join(f"{v.rule}: {v.detail}" for v in result.violations)


@pytest.mark.parametrize("scenario", rh.SCENARIOS, ids=lambda s: s.id)
def test_scenario_receipt_claims_only_what_its_evidence_supports(scenario, tmp_path):
    result = rh.run_scenario(scenario, tmp_path)
    assert result.passed, _explain(result)


@pytest.mark.parametrize("scenario", rh.KNOWN_GAPS, ids=lambda s: s.id)
def test_known_gap_is_still_flagged(scenario, tmp_path):
    """Fails once Core closes the gap: then move the scenario into ``SCENARIOS``."""
    result = rh.run_scenario(scenario, tmp_path)
    assert result.error is None
    assert rh.RULE_OBSERVED_NEEDS_EVIDENCE in _rules(result)


def test_scenario_ids_are_unique_and_every_scenario_states_expectations():
    ids = [s.id for s in rh.SCENARIOS + rh.KNOWN_GAPS]
    assert len(ids) == len(set(ids))
    assert all(s.expect for s in rh.SCENARIOS + rh.KNOWN_GAPS)


def test_free_and_paid_runs_state_identical_verification(tmp_path):
    free = rh.run_scenario(_BY_ID["osn_free_model_verified"], tmp_path / "free").observed
    paid = rh.run_scenario(_BY_ID["osn_paid_model_verified"], tmp_path / "paid").observed
    keys = ("state", "authority", "effective_status", "basis", "label", "origin", "capture_depth",
            "verification_status", "tokens_input", "tokens_output", "tokens_provenance", "integrity")
    assert {k: free[k] for k in keys} == {k: paid[k] for k in keys}
    assert (free["cost_usd"], paid["cost_usd"]) == (0.0, 0.0123)


def test_suite_leaves_no_telemetry_or_worker_switch_behind(tmp_path, monkeypatch):
    for key in rh._QUIET_ENV:
        monkeypatch.delenv(key, raising=False)
    report = rh.run_suite(rh.SCENARIOS[:1], workdir=tmp_path)
    assert report.passed, rh.render(report)
    assert not any(key in os.environ for key in rh._QUIET_ENV)
    assert (tmp_path / rh.SCENARIOS[0].id / "repo" / ".openshard" / "runs.jsonl").is_file()


def test_main_exit_code_and_json_report(capsys, monkeypatch):
    monkeypatch.setattr(rh, "SCENARIOS", rh.SCENARIOS[:2])
    assert rh.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True and report["scenarios"] == 2 and report["rules"] == list(rh.RULES)
    assert rh.main(["--known-gaps"]) == 1
    assert "FAIL  legacy_unknown_origin_record" in capsys.readouterr().out


def test_a_broken_scenario_is_reported_not_raised(tmp_path):
    def boom(_workdir: Path) -> rh.Built:
        raise RuntimeError("fixture broke")

    result = rh.run_scenario(rh.Scenario("boom", "broken", boom, {"state": "x"}), tmp_path)
    assert not result.passed and result.error == "RuntimeError: fixture broke"


# ---------------------------------------------------------------------------
# The rules have teeth
# ---------------------------------------------------------------------------


def _corrupted(**changes: Any):
    def observer(entry: dict, attestations: list[dict]) -> dict[str, Any]:
        obs = rh.observe(entry, attestations)
        obs.update(changes)
        return obs

    return observer


_UPGRADED = {"state": "verified_passed", "authority": "directly_observed", "effective_status": "passed",
             "label": "Passed (OpenShard ran the check(s))"}


@pytest.mark.parametrize(("scenario_id", "changes", "rule"), [
    ("agent_claims_pass_unverified", _UPGRADED, rh.RULE_OBSERVED_NEEDS_EVIDENCE),
    ("agent_claims_pass_unverified", {"label": "Passed"}, rh.RULE_CLAIM_IS_NOT_VERIFIED),
    ("agent_claims_pass_unverified", {"effective_status": "passed"}, rh.RULE_CLAIM_IS_NOT_VERIFIED),
    ("agent_claims_pass_openshard_rerun_fails", {"effective_status": "unknown"}, rh.RULE_FAILURE_NOT_HIDDEN),
    ("rerun_passes_then_ci_fails_same_commit", {"effective_status": "passed"}, rh.RULE_FAILURE_NOT_HIDDEN),
    ("agent_claims_fail_openshard_rerun_passes", {"claim_status": None}, rh.RULE_CLAIM_KEPT),
    ("agent_claims_pass_unverified", {"cost_usd": 0.0, "cost": "$0.0000"}, rh.RULE_MISSING_NOT_ZERO),
    ("osn_unknown_cost_verified_failure", {"cost_usd": 0.0}, rh.RULE_MISSING_NOT_ZERO),
    ("agent_claims_pass_unverified", {"tokens_input": 0}, rh.RULE_MISSING_NOT_ZERO),
    ("osn_free_model_verified", {"cost_usd": None}, rh.RULE_ZERO_NOT_MISSING),
    ("legacy_external_record_without_capture", {"model": "GPT-4o"}, rh.RULE_MODEL_NOT_INVENTED),
    ("agent_claims_pass_unverified", {"origin": "openshard_routed"}, rh.RULE_OBSERVATION_NOT_CONTROL),
    ("agent_claims_pass_unverified", {"capture_depth": "full"}, rh.RULE_OBSERVATION_NOT_CONTROL),
    ("agent_claims_pass_unverified", {"hosted_verification_status": "verified"}, rh.RULE_SURFACES_AGREE),
    ("agent_claims_pass_unverified", {"mcp_verification_status": None}, rh.RULE_SURFACES_AGREE),
    ("agent_claims_pass_openshard_rerun_fails",
     {"hosted_state": {"state": "verified_passed", "authority": "directly_observed",
                       "effective_status": "passed", "basis": "post_session"}}, rh.RULE_SURFACES_AGREE),
    ("stored_claim_upgraded_by_edit", {"integrity": "valid"}, rh.RULE_EDIT_DETECTED),
])
def test_each_rule_catches_its_violation(scenario_id, changes, rule, tmp_path):
    result = rh.run_scenario(_BY_ID[scenario_id], tmp_path, observer=_corrupted(**changes))
    assert result.error is None
    assert rule in _rules(result), _explain(result)


def test_price_dependent_evidence_is_caught(tmp_path):
    def paid_runs_look_verified(entry: dict, attestations: list[dict]) -> dict[str, Any]:
        obs = rh.observe(entry, attestations)
        if (entry.get("estimated_cost") or 0) > 0:
            obs["truth"] = {**obs["truth"], "state": "verified_passed"}
        return obs

    result = rh.run_scenario(_BY_ID["agent_claims_pass_unverified"], tmp_path, observer=paid_runs_look_verified)
    assert rh.RULE_PRICE_INDEPENDENT in _rules(result), _explain(result)


def test_core_regression_reading_external_booleans_as_executed_is_caught(tmp_path, monkeypatch):
    from openshard.history import shard, verification

    monkeypatch.setattr(verification, "derive_shard_identity",
                        lambda entry: ("OpenShard", shard.ORIGIN_OPENSHARD_ROUTED, shard.CAPTURE_FULL))
    result = rh.run_scenario(_BY_ID["legacy_external_record_without_capture"], tmp_path)
    assert rh.RULE_OBSERVED_NEEDS_EVIDENCE in _rules(result), _explain(result)


def test_core_regression_hosted_payload_drifting_is_caught(tmp_path, monkeypatch):
    from openshard.sync import envelope

    real = envelope.receipt_payload

    def drifted(entry: dict, index: int) -> dict:
        payload = real(entry, index)
        payload["verification_status"] = "passed" if payload["verification_status"] == "failed" else "failed"
        return payload

    monkeypatch.setattr(envelope, "receipt_payload", drifted)
    result = rh.run_scenario(_BY_ID["osn_unknown_cost_verified_failure"], tmp_path)
    assert rh.RULE_SURFACES_AGREE in _rules(result), _explain(result)
