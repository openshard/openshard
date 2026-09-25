from __future__ import annotations

import json

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.routing.adaptive.report import build_routing_report


def _entry(model, cls, *, verified, retry=False, fixer=None, cost=0.01):
    e = {
        "execution_model": model,
        "duration_seconds": 3.0,
        "estimated_cost": cost,
        "retry_triggered": retry,
        "routing_provenance": {
            "resolved_class": cls,
            "selected_model": model,
            "agrees_with_execution": True,
            "policy": {"name": "deterministic_baseline", "version": 1},
            "fingerprints": {"decision": "d"},
            "context": {"harness": "native"},
        },
    }
    if fixer:
        e["fixer_model"] = fixer
    if verified is not None:
        e["verification_attempted"] = True
        e["verification_passed"] = verified
    return e


def test_groups_by_class_and_model_with_coverage():
    entries = [
        _entry("m/a", "balanced_coding", verified=True),
        _entry("m/a", "balanced_coding", verified=False, retry=True, fixer="m/b"),
        _entry("m/b", "cheap_coding", verified=None),
    ]
    rep = build_routing_report(entries)
    assert rep["runs"] == 3
    by = {(g["routing_class"], g["model"]): g for g in rep["groups"]}
    a = by[("balanced_coding", "m/a")]
    assert a["runs"] == 2 and a["escalations"] == 1 and a["retries_observed"] == 1
    b = by[("cheap_coding", "m/b")]
    assert b["verification_known"] == 0 and b["verified_success_rate"] is None  # unknown != failure


def test_pre_provenance_and_malformed_entries_do_not_crash():
    rep = build_routing_report([{"execution_model": "m/x"}, "garbage", None])  # type: ignore[list-item]
    assert rep["runs"] == 1 and rep["skipped_malformed"] == 2
    assert any(g["routing_class"] == "unknown" for g in rep["groups"])


def test_nonfinite_values_are_not_observations_and_json_safe():
    e = _entry("m/a", "balanced_coding", verified=True, cost=float("inf"))
    e["duration_seconds"] = float("nan")
    rep = build_routing_report([e])
    json.dumps(rep, allow_nan=False)
    g = rep["groups"][0]
    assert g["mean_latency_seconds"] is None and g["cost_known"] == 0


def test_no_misleading_mean_attempts():
    rep = build_routing_report([_entry("m/a", "c", verified=True)])
    assert "mean_attempts" not in rep["overall"] and "mean_attempts" not in rep["groups"][0]


def test_cli_empty_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["stats", "routing", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output)["status"] == "not_found"
