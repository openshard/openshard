import copy
import json

import pytest

from openshard.history import outcome_classification as oc
from openshard.history.outcome_classification import (
    ObservedFacts,
    classify,
    classify_entry,
    parse_classification,
    read_classification,
)

OBSERVED = "directly_observed"


def _facts(**kw):
    return ObservedFacts(**kw)


# --- verification ------------------------------------------------------------


def test_verification_pass_is_model_evidence():
    c = classify(_facts(verification_status="passed", verification_source=OBSERVED))
    assert (c.outcome, c.cause, c.cause_basis) == (oc.VERIFICATION_PASSED, "model", "observed")
    assert c.routing_eligible


def test_agent_claimed_pass_is_not_evidence():
    c = classify(_facts(verification_status="passed", verification_source="agent_reported"))
    assert c.outcome == oc.COMPLETED_UNVERIFIED
    assert not c.routing_eligible


def test_verification_failure_with_uncertain_cause_does_not_count_against_model():
    c = classify(
        _facts(verification_status="failed", verification_source=OBSERVED, check_exit_codes=(1,))
    )
    assert c.outcome == oc.VERIFICATION_FAILED
    assert c.cause == "unknown" and c.cause_basis == "unproven"
    assert not c.routing_eligible


def test_verification_failure_counts_only_after_passing_preflight():
    c = classify(
        _facts(
            verification_status="failed",
            verification_source=OBSERVED,
            check_exit_codes=(1,),
            verifier_preflight="passed",
        )
    )
    assert (c.outcome, c.cause) == (oc.VERIFICATION_FAILED, "model")
    assert c.routing_eligible


@pytest.mark.parametrize("code", [126, 127])
def test_missing_check_tooling_is_harness_not_model(code):
    """The wrong-interpreter / pytest-not-found case must never blame the model."""
    c = classify(
        _facts(verification_status="failed", verification_source=OBSERVED, check_exit_codes=(code,))
    )
    assert (c.outcome, c.cause) == (oc.VERIFICATION_INFRA_ERROR, "harness")
    assert not c.routing_eligible


def test_passing_preflight_cannot_override_missing_tooling():
    c = classify(
        _facts(
            verification_status="failed",
            verification_source=OBSERVED,
            check_exit_codes=(127,),
            verifier_preflight="passed",
        )
    )
    assert c.outcome == oc.VERIFICATION_INFRA_ERROR
    assert not c.routing_eligible


def test_check_that_could_not_complete_is_infra():
    c = classify(_facts(check_not_completed=True, verification_status="failed", verification_source=OBSERVED))
    assert c.outcome == oc.VERIFICATION_INFRA_ERROR
    assert not c.routing_eligible


def test_agent_claimed_failure_is_unknown_failure():
    c = classify(_facts(verification_status="failed", verification_source="agent_reported"))
    assert c.outcome == oc.UNKNOWN_FAILURE and not c.routing_eligible


# --- model / provider errors ---------------------------------------------------


def test_malformed_model_reply_is_model_evidence():
    c = classify(_facts(error_kind="malformed_reply", retry_count=1))
    assert (c.outcome, c.cause, c.cause_basis) == (oc.MODEL_OUTPUT_INVALID, "model", "observed")
    assert c.routing_eligible
    assert c.evidence["retry_count"] == 1


@pytest.mark.parametrize(
    "kind,outcome",
    [
        ("provider", oc.PROVIDER_ERROR),
        ("auth", oc.PROVIDER_ERROR),
        ("rate_limit", oc.RATE_LIMITED),
        ("timeout", oc.TIMED_OUT),
        ("no_reply", oc.MODEL_NO_RESPONSE),
        ("harness", oc.HARNESS_ERROR),
    ],
)
def test_non_model_errors_are_never_eligible(kind, outcome):
    c = classify(_facts(error_kind=kind))
    assert c.outcome == outcome
    assert not c.routing_eligible


def test_infrastructure_error_beats_a_verification_result():
    c = classify(
        _facts(error_kind="rate_limit", verification_status="passed", verification_source=OBSERVED)
    )
    assert c.outcome == oc.RATE_LIMITED and not c.routing_eligible


def test_timeout_cause_is_not_guessed():
    c = classify(_facts(error_kind="timeout"))
    assert c.cause == "unknown" and c.cause_basis == "unproven"


# --- blocks --------------------------------------------------------------------


def test_policy_block():
    c = classify(_facts(policy_denied=True, verification_status="passed", verification_source=OBSERVED))
    assert (c.outcome, c.cause) == (oc.POLICY_BLOCKED, "policy")
    assert not c.routing_eligible


def test_approval_denied_and_unavailable():
    d = classify(_facts(approval="denied"))
    u = classify(_facts(approval="unavailable"))
    assert (d.outcome, d.cause) == (oc.APPROVAL_DENIED, "user")
    assert (u.outcome, u.cause) == (oc.APPROVAL_UNAVAILABLE, "harness")
    assert not d.routing_eligible and not u.routing_eligible


def test_task_invalid_and_execution_and_unknown_failures():
    assert classify(_facts(task_invalid=True)).outcome == oc.TASK_INVALID
    assert classify(_facts(execution_failed=True)).outcome == oc.EXECUTION_FAILED
    unknown = classify(_facts(run_failed=True))
    assert unknown.outcome == oc.UNKNOWN_FAILURE
    for c in (classify(_facts(task_invalid=True)), classify(_facts(execution_failed=True)), unknown):
        assert not c.routing_eligible


def test_nothing_observed_is_completed_unverified():
    c = classify(_facts())
    assert c.outcome == oc.COMPLETED_UNVERIFIED and not c.routing_eligible


# --- eligibility invariants ------------------------------------------------------


def test_bare_model_observed_claim_without_evidence_is_withheld():
    for outcome in oc.OUTCOMES:
        assert oc.routing_use_for(outcome, "model", "observed") == oc.USE_WITHHELD
        assert oc.routing_use_for(outcome, "unknown", "unproven") == oc.USE_WITHHELD
        assert oc.routing_use_for(outcome, "model", "unproven") == oc.USE_WITHHELD


def test_routing_use_groups():
    def use(**kw):
        return classify(_facts(**kw)).routing_use

    assert use(verification_status="passed", verification_source=OBSERVED) == "coding"
    assert use(error_kind="malformed_reply") == "format"
    assert use(error_kind="rate_limit") == "provider"
    assert use(error_kind="network", http_status=503) == "provider"
    assert use(error_kind="auth") == "harness"
    assert use(error_kind="harness") == "harness"
    assert use(approval="unavailable") == "harness"
    assert use(policy_denied=True) == "policy"
    assert use(error_kind="secret_scan") == "policy"
    for withheld in (
        dict(approval="denied"),
        dict(task_invalid=True),
        dict(error_kind="timeout"),
        dict(error_kind="no_reply"),
        dict(run_failed=True),
        dict(verification_status="failed", verification_source=OBSERVED),
    ):
        assert use(**withheld) == "withheld"


def test_provider_failures_never_lower_coding_quality():
    for kind in ("rate_limit", "provider", "network", "auth", "timeout", "no_reply"):
        assert not classify(_facts(error_kind=kind, http_status=500)).routing_eligible


def test_forged_model_block_without_supporting_evidence_is_withheld():
    for outcome, evidence in [
        (oc.VERIFICATION_FAILED, {}),  # no preflight
        (oc.VERIFICATION_FAILED, {"verification_status": "failed", "verification_source": OBSERVED}),
        (
            oc.VERIFICATION_FAILED,
            {
                "verification_status": "failed",
                "verification_source": OBSERVED,
                "verifier_preflight": "passed",
                "check_exit_codes": [127],
            },
        ),
        (oc.VERIFICATION_PASSED, {"verification_status": "passed", "verification_source": "agent_reported"}),
        (oc.MODEL_OUTPUT_INVALID, {"error_kind": "rate_limit"}),
    ]:
        block = {
            "version": 1, "outcome": outcome, "cause": "model", "cause_basis": "observed",
            "routing_use": "coding", "evidence": evidence,
        }
        assert parse_classification(block).routing_use == "withheld", (outcome, evidence)


def test_unknown_verification_source_fails_closed():
    c = classify(_facts(verification_status="passed", verification_source="provider callback"))
    assert not c.routing_eligible


def test_failed_preflight_is_infrastructure():
    c = classify(
        _facts(verification_status="failed", verification_source=OBSERVED, check_exit_codes=(1,), verifier_preflight="failed")
    )
    assert c.outcome == oc.VERIFICATION_INFRA_ERROR and c.routing_use == "harness"


def test_per_attempt_classifications_are_independent():
    a1 = classify(
        _facts(verification_status="failed", verification_source=OBSERVED, verifier_preflight="passed"),
        attempt=1, model="acme/small-1",
    )
    a2 = classify(
        _facts(verification_status="passed", verification_source=OBSERVED),
        attempt=2, model="acme/big-1",
    )
    assert (a1.attempt, a1.model, a1.routing_use) == (1, "acme/small-1", "coding")
    assert (a2.attempt, a2.model, a2.routing_use) == (2, "acme/big-1", "coding")
    assert parse_classification(json.loads(json.dumps(a1.to_dict()))) == a1


def test_attempt_and_model_are_sanitised():
    c = classify(_facts(), attempt=0, model="bad model /home/x")
    assert c.attempt is None and c.model is None


@pytest.mark.parametrize(
    "bad",
    ["C:/Users/Michael/secret", "/home/user/key", "a b", "sk-ant-api03-" + "A" * 40, "x" * 81, "..", "a//b"],
)
def test_unsafe_model_ids_are_dropped(bad):
    assert classify(_facts(), model=bad).model is None


def test_normal_model_ids_are_kept():
    for ok in ("claude-sonnet-5", "deepseek/deepseek-chat", "acme/mid-1:free", "gpt-5.6-terra"):
        assert classify(_facts(), model=ok).model == ok


def test_retry_only_after_accountable_model_failure():
    model_fail = classify(
        _facts(verification_status="failed", verification_source=OBSERVED, verifier_preflight="passed")
    )
    assert oc.should_retry_with_another_model(model_fail)
    assert oc.should_retry_with_another_model(classify(_facts(error_kind="malformed_reply")))
    for facts in (
        _facts(error_kind="rate_limit"),
        _facts(error_kind="provider"),
        _facts(error_kind="timeout"),
        _facts(policy_denied=True),
        _facts(approval="unavailable"),
        _facts(verification_status="failed", verification_source=OBSERVED, check_exit_codes=(127,)),
        _facts(verification_status="failed", verification_source=OBSERVED),  # unknown cause
        _facts(run_failed=True),
        _facts(verification_status="passed", verification_source=OBSERVED),
    ):
        assert not oc.should_retry_with_another_model(classify(facts))


def test_old_records_without_block_are_conservative():
    passed = {"verification": {"version": 1, "status": "passed", "source": OBSERVED, "observation_mode": "openshard_executed"}}
    failed = {"verification": {"version": 1, "status": "failed", "source": OBSERVED, "observation_mode": "openshard_executed"}}
    claimed = {"verification": {"version": 1, "status": "passed", "source": "agent_reported", "observation_mode": "agent_claim"}}
    legacy_fail = {"verification_attempted": True, "verification_passed": False}
    assert oc.routing_use_for_entry(passed) == "coding"
    for e in (failed, claimed, legacy_fail, {}, None, "x"):
        assert oc.routing_use_for_entry(e) == "withheld"


def test_recorded_classification_beats_legacy_pass():
    entry = {
        "verification": {"version": 1, "status": "passed", "source": OBSERVED, "observation_mode": "openshard_executed"},
        "outcome_classification": classify(_facts(error_kind="rate_limit")).to_dict(),
    }
    assert oc.routing_use_for_entry(entry) == "provider"


# --- serialization and old records -------------------------------------------------


def test_round_trip_through_json():
    c = classify(
        _facts(verification_status="failed", verification_source=OBSERVED, check_exit_codes=(1, 2), retry_count=2)
    )
    back = parse_classification(json.loads(json.dumps(c.to_dict())))
    assert back == c
    assert back.to_dict()["routing_use"] == "withheld"


def test_old_record_without_block_reads_as_unclassified_and_is_untouched():
    entry = {"task": "x", "verification_attempted": True, "verification_passed": False}
    snapshot = copy.deepcopy(entry)
    c = read_classification(entry)
    assert c.outcome == oc.UNCLASSIFIED and not c.routing_eligible
    assert entry == snapshot


@pytest.mark.parametrize(
    "bad",
    [
        None, "x", 3, [], {},
        {"version": 1, "outcome": "nope", "cause": "model", "cause_basis": "observed"},
        {"version": 2, "outcome": "verification_passed", "cause": "model", "cause_basis": "observed"},
        {"outcome": "verification_passed", "cause": "model", "cause_basis": "observed"},
    ],
)
def test_malformed_blocks_read_as_unclassified(bad):
    assert read_classification({"outcome_classification": bad}).outcome == oc.UNCLASSIFIED
    assert read_classification("not a dict").outcome == oc.UNCLASSIFIED


def test_stored_eligibility_flag_is_not_trusted():
    block = classify(_facts(error_kind="rate_limit")).to_dict()
    block["routing_use"] = "coding"
    block["cause"] = "model"
    block["cause_basis"] = "observed"
    assert not parse_classification(block).routing_eligible  # outcome is not model evidence


def test_stored_evidence_is_bounded_and_free_text_dropped():
    block = classify(_facts(verification_status="failed", verification_source=OBSERVED)).to_dict()
    block["evidence"] = {
        "verification_status": "failed",
        "error_kind": "sk-secret-value /home/user/x",
        "stdout": "token=abc",
        "check_exit_codes": list(range(50)),
    }
    ev = parse_classification(block).evidence
    assert ev == {"verification_status": "failed", "check_exit_codes": list(range(8))}


# --- deriving facts from an existing entry -----------------------------------------


def _entry(**kw):
    return dict(kw)


def test_classify_entry_missing_pytest_is_infra():
    entry = _entry(
        verification={
            "version": 1,
            "status": "failed",
            "source": OBSERVED,
            "observation_mode": "openshard_executed",
            "checks_attempted": 1,
            "checks_failed": 1,
            "checks": [{"name": "pytest", "kind": "test", "status": "failed", "exit_code": 127}],
        }
    )
    c = classify_entry(entry)
    assert c.outcome == oc.VERIFICATION_INFRA_ERROR and not c.routing_eligible


def test_classify_entry_plain_failure_is_unknown_cause():
    entry = _entry(
        verification={
            "version": 1,
            "status": "failed",
            "source": OBSERVED,
            "observation_mode": "openshard_executed",
            "checks_attempted": 1,
            "checks_failed": 1,
            "checks": [{"name": "pytest", "kind": "test", "status": "failed", "exit_code": 1}],
        }
    )
    c = classify_entry(entry)
    assert (c.outcome, c.cause) == (oc.VERIFICATION_FAILED, "unknown")
    assert not c.routing_eligible


@pytest.mark.parametrize(
    "error_class,outcome",
    [
        ("ProviderRateLimitError", oc.RATE_LIMITED),
        ("ProviderError", oc.PROVIDER_ERROR),
        ("ProviderAuthError", oc.PROVIDER_ERROR),
        ("PreSendSecretScanError", oc.POLICY_BLOCKED),
        ("LockTimeoutError", oc.HARNESS_ERROR),
        ("ReadTimeout", oc.TIMED_OUT),
        ("SomethingWeirdError", oc.EXECUTION_FAILED),
    ],
)
def test_classify_entry_error_classes(error_class, outcome):
    c = classify_entry(_entry(error_class=error_class))
    assert c.outcome == outcome and not c.routing_eligible


def test_classify_entry_approval_and_policy():
    assert classify_entry(_entry(approval_receipt={"granted": False})).outcome == oc.APPROVAL_DENIED
    deny = _entry(policy_decisions=[{"decision_id": "d", "decision": "deny"}])
    assert classify_entry(deny).outcome == oc.POLICY_BLOCKED


@pytest.mark.parametrize("bad", [None, "x", 1, [], {"error_class": 5, "verification": "junk"}])
def test_classify_entry_never_raises(bad):
    assert not classify_entry(bad).routing_eligible
