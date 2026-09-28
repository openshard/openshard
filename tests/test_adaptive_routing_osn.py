"""Adaptive routing for ``openshard osn run`` (``openshard.osn.routing``).

Proves: the capability off (or unconfirmed) leaves model choice exactly as
it was; with it on and no ``--model``, the Routing V2 decision picks the
first model and its recovery plan supplies the ladder; an explicit ``--model``
or ``--escalate-model`` always wins; no candidate falls back to the keyword
router; the Receipt records what was decided and why.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.models.catalog import ModelEntry, build_catalog
from openshard.osn.routing import resolve_osn_routing
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.routing.engine import route
from openshard.routing.model_policy import ModelPolicyConfig
from openshard.routing.provider_availability import ProviderAvailability
from openshard.sync import config as sync_config

PY = sys.executable
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
OPENROUTER = ProviderAvailability(("openrouter",), True, False, False)
NO_KEYS = ProviderAvailability((), False, False, False)


def _m(mid: str, **kw) -> ModelEntry:
    defaults = dict(
        display_name=mid, provider=mid.split("/")[0], tier="mid", cost_class="mid",
        supports_tools=True, context_length=200_000, lifecycle="active_default",
    )
    defaults.update(kw)
    return ModelEntry(id=mid, **defaults)


CURATED = [
    _m("acme/cheap-1", tier="cheap", cost_class="cheap", roles=("cheap_control", "boilerplate")),
    _m("acme/mid-1", roles=("standard_coding",)),
    _m("zeta/mid-2"),
    _m("acme/frontier-1", tier="frontier", cost_class="expensive", supports_reasoning=True,
       roles=("escalation",), lifecycle="active_specialist"),
    # A second reasoning model so the repair step has somewhere to go after
    # the first escalation (V2 never retries a tried model).
    _m("zeta/frontier-2", tier="frontier", cost_class="expensive", supports_reasoning=True,
       roles=("escalation",), lifecycle="active_specialist"),
]
CATALOG = build_catalog(CURATED, [], synced_at="2026-09-24T00:00:00Z")
TASK = "write ok into out.txt"  # no router keywords: standard -> balanced_coding


@pytest.fixture
def catalog():
    with patch("openshard.models.catalog.load_catalog", return_value=CATALOG), \
         patch("openshard.routing.provider_availability.detect_provider_availability", return_value=OPENROUTER):
        yield


def _resolve(task=TASK, *, explicit_model=None, escalate=(), enabled=True, policy_loader=None, max_attempts=None):
    calls = {"lookups": 0}

    def capability_enabled():
        calls["lookups"] += 1
        return enabled

    r = resolve_osn_routing(
        task, explicit_model=explicit_model, escalate=list(escalate),
        capability_enabled=capability_enabled, legacy_model=lambda t: route(t).model,
        model_policy_loader=policy_loader, max_attempts=max_attempts,
    )
    return r, calls["lookups"]


class TestResolve:
    def test_capability_off_is_the_keyword_router_and_no_record(self, catalog):
        r, lookups = _resolve(enabled=False)
        assert r.first_model == route(TASK).model and r.ladder == []
        assert r.record_mode == "shadow" and r.record is None and lookups == 1
        assert r.decision is not None and r.decision.selected_model == "acme/mid-1"  # still recorded

    def test_capability_on_applies_the_decision_and_its_recovery_ladder(self, catalog):
        r, lookups = _resolve()
        assert r.first_model == "acme/mid-1" and r.ladder == ["acme/frontier-1", "zeta/frontier-2"]
        assert r.models == ["acme/mid-1", "acme/frontier-1", "zeta/frontier-2"]
        assert r.record_mode == "applied" and lookups == 1
        rec = r.record
        assert rec["applied"] is True and rec["reason"] == "applied"
        assert rec["selected_model"] == "acme/mid-1" and rec["routing_class"] == "routine_coding"
        assert rec["policy"] == {"name": "deterministic_trajectory_v2", "version": "1"}
        assert rec["step_type"] == "execute" and rec["promotion_state"] == "stable"
        assert rec["escalation_ladder"] == ["acme/frontier-1", "zeta/frontier-2"]
        assert rec["ladder_source"] == "recovery_plan"
        assert rec["history_evidence"] == "not_used_insufficient_observed_data"
        assert rec["history"]["used"] is False
        assert rec["recovery_enabled"] is True and rec["decision_fingerprint"]

    def test_explicit_model_always_wins_and_is_never_looked_up(self, catalog):
        r, lookups = _resolve(explicit_model="acme/cheap-1", escalate=["zeta/mid-2"])
        assert r.first_model == "acme/cheap-1" and r.ladder == ["zeta/mid-2"]
        assert r.record_mode == "shadow" and r.record is None and lookups == 0
        assert r.decision is not None and r.decision.selection_mode == "explicit"
        assert r.decision.selected_model == "acme/cheap-1"

    def test_explicit_unknown_model_is_still_run_not_substituted(self, catalog):
        r, _ = _resolve(explicit_model="nobody/custom")
        assert r.first_model == "nobody/custom"
        assert r.decision is not None and r.decision.selected_model is None
        assert r.decision.selection_mode == "none"

    def test_user_ladder_wins_over_the_recovery_plan(self, catalog):
        r, _ = _resolve(escalate=["zeta/mid-2"])
        assert r.first_model == "acme/mid-1" and r.ladder == ["zeta/mid-2"]
        assert r.record["ladder_source"] == "user"

    def test_no_eligible_candidate_falls_back_to_the_keyword_router(self):
        with patch("openshard.models.catalog.load_catalog", return_value=CATALOG), \
             patch("openshard.routing.provider_availability.detect_provider_availability", return_value=NO_KEYS):
            r, _ = _resolve()
        assert r.first_model == route(TASK).model and r.ladder == []
        assert r.record_mode == "shadow"
        assert r.record["applied"] is False and r.record["reason"] == "no_eligible_candidate"
        assert r.record["eligible_count"] == 0

    def test_decision_failure_falls_back_and_says_so(self):
        with patch("openshard.routing.adaptive.runtime.plan_route_with_candidates",
                   side_effect=RuntimeError("boom")):
            r, _ = _resolve()
        assert r.first_model == route(TASK).model and r.decision is None
        assert r.record["applied"] is False and r.record["reason"] == "decision_unavailable"

    def test_model_policy_is_honoured_only_on_the_applied_path(self, catalog):
        blocked = ModelPolicyConfig(blocked_models=["acme/mid-1"])
        loads = {"n": 0}

        def loader():
            loads["n"] += 1
            return blocked

        r, _ = _resolve(policy_loader=loader)
        assert r.first_model != "acme/mid-1" and loads["n"] == 1  # the blocked model is skipped
        assert r.record["rejected_counts"].get("policy:blocked_model") == 1
        r_off, _ = _resolve(enabled=False, policy_loader=loader)
        assert r_off.first_model == route(TASK).model and loads["n"] == 1  # never consulted when off

    def test_an_unreadable_models_policy_falls_back_and_says_so(self, catalog):
        def loader():
            raise ValueError("bad models section")

        r, _ = _resolve(policy_loader=loader)
        assert r.first_model == route(TASK).model and r.record_mode == "shadow"
        assert r.record["applied"] is False and r.record["reason"] == "model_policy_invalid"
        assert r.record["detail"] == "ValueError"
        assert r.decision is not None  # the shadow decision is still recorded

    def test_the_ladder_is_cut_to_what_max_attempts_can_run(self, catalog):
        task = "add a simple helper"  # boilerplate -> routine_coding -> two rungs in deep_reasoning
        full, _ = _resolve(task, max_attempts=3)
        assert full.first_model == "acme/mid-1" and full.ladder == ["acme/frontier-1", "zeta/frontier-2"]
        assert full.record["recovery_enabled"] is True and full.record["max_attempts"] == 3
        one, _ = _resolve(task, max_attempts=2)
        assert one.ladder == ["acme/frontier-1"] and one.models == ["acme/mid-1", "acme/frontier-1"]
        none, _ = _resolve(task, max_attempts=1)
        assert none.ladder == [] and none.record["ladder_source"] == "none"
        assert none.record["recovery_enabled"] is False and none.record["applied"] is True

    def test_fall_back_after_the_fact_keeps_the_user_ladder_and_records_the_rejected_model(self, catalog):
        r, _ = _resolve(escalate=["zeta/mid-2"])
        assert r.applied and r.first_model == "acme/mid-1"
        r.fall_back("provider_mismatch", route(TASK).model, provider="anthropic")
        assert r.first_model == route(TASK).model and r.ladder == ["zeta/mid-2"]
        assert r.record_mode == "shadow" and r.applied is False
        assert r.record["applied"] is False and r.record["reason"] == "provider_mismatch"
        assert r.record["rejected_model"] == "acme/mid-1" and r.record["provider"] == "anthropic"

    def test_the_record_carries_identifiers_and_counts_only(self, catalog):
        r, _ = _resolve()
        allowed = {
            "capability", "record_mode", "history_evidence", "applied", "reason", "selected_model",
            "selection_mode", "selected_via", "routing_class", "requested_class", "policy", "considered",
            "escalation_ladder", "ladder_source", "recovery_enabled", "max_attempts", "decision_fingerprint",
            "step_type", "promotion_state", "ranking", "eligible_count", "rejected_counts",
            "shadow_candidates", "history", "reasons",
        }
        assert set(r.record) == allowed
        assert len(r.record["considered"]) <= 8 and all("/" in m for m in r.record["considered"])
        assert len(r.record["ranking"]) <= 3 and len(r.record["shadow_candidates"]) <= 3
        assert TASK not in json.dumps(r.record)  # no task text


# ---------------------------------------------------------------------------
# `openshard osn run` end to end
# ---------------------------------------------------------------------------


class FakeProvider(BaseProvider):
    def __init__(self, replies, cost=0.001):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.cost = cost

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append((model, prompt))
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, self.cost))


def _writes(*pairs):
    return json.dumps({"writes": [{"path": p, "content": c} for p, c in pairs]})


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append(self.path)
        status, body = type(self).routes.get(self.path, (404, b'{"error":{"code":"not_found"}}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _caps_body(org, keys):
    return json.dumps({"organisation_id": org, "capabilities": [
        {"key": k, "name": k, "description": "", "stage": "internal", "enabled": True, "enabled_at": "x"} for k in keys
    ]}).encode()


@pytest.fixture
def platform():
    _Handler.routes, _Handler.seen = {}, []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cli_repo(tmp_path, monkeypatch, platform, keys):
    repo = tmp_path / "repo"
    (repo / ".openshard").mkdir(parents=True)
    (repo / "out.txt").write_text("bad")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    monkeypatch.setenv(sync_config.ENDPOINT_ENV, f"http://127.0.0.1:{platform.server_address[1]}")
    monkeypatch.setenv(sync_config.ORG_ENV, ORG)
    monkeypatch.setenv(sync_config.API_KEY_ENV, KEY)
    _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, keys))
    return repo


def _invoke(monkeypatch, fp, *extra):
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("openrouter", fp))
    return CliRunner().invoke(cli, [
        "osn", "run", TASK, "--verify-cmd",
        f'"{PY}" -c "import sys; c=open(\'out.txt\').read(); print(c); sys.exit(0 if c==\'ok\' else 1)"',
        "--max-attempts", "2", "--json", *extra,
    ])


def _last_run(repo):
    return json.loads((repo / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])


class TestCli:
    def test_capability_off_runs_the_keyword_model_and_records_shadow_only(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, [])
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["models"] == [route(TASK).model] and "adaptive_routing" not in body
        assert fp.calls[0][0] == route(TASK).model
        entry = _last_run(repo)
        assert "adaptive_routing" not in entry
        assert entry["routing_provenance"]["record_mode"] == "shadow"
        assert entry["routing_provenance"]["selected_model"] == "acme/mid-1"  # recorded, not run
        assert _Handler.seen == [f"/v1/orgs/{ORG}/capabilities"]

    def test_capability_on_routes_and_escalates_along_the_plan(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "verified" and body["models"] == ["acme/mid-1", "acme/frontier-1"]
        assert [m for m, _ in fp.calls] == ["acme/mid-1", "acme/frontier-1"]  # escalated after an observed failure
        assert body["adaptive_routing"]["applied"] is True
        assert body["capability_snapshot"]["enabled"]["adaptive_routing"] is True
        assert body["capability_snapshot"]["refreshed_at_run_start"] is True
        entry = _last_run(repo)
        prov = entry["routing_provenance"]
        assert prov["record_mode"] == "applied" and prov["selected_model"] == "acme/mid-1"
        assert prov["executed_model"] == "acme/mid-1" and prov["agrees_with_execution"] is True
        assert entry["execution_model"] == "acme/frontier-1" and entry["fixer_model"] == "acme/frontier-1"
        assert entry["adaptive_routing"]["escalation_ladder"] == ["acme/frontier-1"]

        receipt = build_shard_receipt(entry, index=0)
        ev = receipt.recorded_evidence["adaptive_routing"]
        assert ev["applied"] is True and ev["selected_model"] == "acme/mid-1"
        assert ev["escalation_ladder"] == ["acme/frontier-1"] and ev["ladder_source"] == "recovery_plan"
        text = render_full_shard_receipt(receipt)
        assert "ADAPTIVE ROUTING" in text and "acme/mid-1" in text
        assert "adaptive_routing" not in receipt_to_dict(receipt, extended=True)  # wire unchanged

    def test_capability_on_but_explicit_model_wins_without_a_lookup(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp, "--model", "acme/cheap-1")
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["models"] == ["acme/cheap-1"] and "adaptive_routing" not in body
        entry = _last_run(repo)
        prov = entry["routing_provenance"]
        assert prov["selection_mode"] == "explicit" and prov["record_mode"] == "shadow"
        assert _Handler.seen == []  # no capability request when the user named the model

    def test_capability_on_with_a_budget_shares_one_lookup(self, tmp_path, monkeypatch, platform, catalog):
        import yaml

        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing", "agent_budgets"])
        (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"agent_budgets": {"max_attempts": 1}}))
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        # The budget allows one attempt, so the plan's ladder is cut to nothing: the
        # record promises no escalation the run could not make.
        assert body["status"] == "budget_exhausted" and body["models"] == ["acme/mid-1"]
        assert len(fp.calls) == 1
        assert body["agent_budgets"]["enforced"] is True and body["adaptive_routing"]["applied"] is True
        assert body["adaptive_routing"]["escalation_ladder"] == [] and body["adaptive_routing"]["max_attempts"] == 1
        assert _Handler.seen == [f"/v1/orgs/{ORG}/capabilities"]

    def test_user_escalation_ladder_wins_with_the_capability_on(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp, "--escalate-model", "zeta/mid-2")
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["models"] == ["acme/mid-1", "zeta/mid-2"] and [m for m, _ in fp.calls] == ["acme/mid-1", "zeta/mid-2"]
        assert _last_run(repo)["adaptive_routing"]["ladder_source"] == "user"

    def test_a_provider_that_cannot_serve_the_selected_model_falls_back(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("anthropic", fp))
        r = CliRunner().invoke(cli, [
            "osn", "run", TASK, "--verify-cmd", f'"{PY}" -c "pass"', "--provider", "anthropic", "--json",
        ])
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["models"] == [route(TASK).model] and fp.calls[0][0] == route(TASK).model
        ar = body["adaptive_routing"]
        assert ar["applied"] is False and ar["reason"] == "provider_mismatch"
        assert ar["rejected_model"] == "acme/mid-1" and ar["provider"] == "anthropic"
        assert "not applied (provider_mismatch)" in r.output
        assert _last_run(repo)["routing_provenance"]["record_mode"] == "shadow"

    def test_sync_kill_switch_leaves_routing_as_before(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        monkeypatch.setenv(sync_config.DISABLE_ENV, "off")
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        assert json.loads(r.stdout)["models"] == [route(TASK).model]
        assert "adaptive_routing" not in _last_run(repo) and _Handler.seen == []

    def test_stats_routing_attributes_an_applied_run_to_the_chosen_model(self, tmp_path, monkeypatch, platform, catalog):
        from openshard.routing.adaptive.outcome import outcome_from_receipt
        from openshard.routing.adaptive.report import build_routing_report

        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        assert _invoke(monkeypatch, fp).exit_code == 0
        entry = _last_run(repo)
        outcome = outcome_from_receipt(entry)
        assert outcome.record_mode == "applied" and outcome.shadow_agreed is None  # not a shadow metric
        assert outcome.routed_model == "acme/mid-1" and outcome.final_model == "acme/frontier-1"
        assert outcome.escalation_model == "acme/frontier-1" and outcome.verified_success is True
        report = build_routing_report([entry])
        groups = {(g["routing_class"], g["model"]): g for g in report["groups"]}
        assert ("routine_coding", "acme/mid-1") in groups  # credited to the decision, not the rung
        assert groups[("routine_coding", "acme/mid-1")]["escalations"] == 1
        assert report["overall"]["shadow_comparable"] == 0

    def test_platform_unavailable_keeps_routing_as_before(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (503, b"")
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        assert json.loads(r.stdout)["models"] == [route(TASK).model]
        assert "adaptive_routing" not in _last_run(repo)
