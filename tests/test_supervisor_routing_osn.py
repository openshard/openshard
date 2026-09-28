"""Supervisor routing V1 for ``openshard osn run`` (``openshard.osn.supervisor``).

Proves: the supervisor is consulted only at an observed-failure boundary the
loop would retry; in shadow it changes nothing and records what it would have
done; applied, it stops a pointless retry or picks the next model, and the
Receipt says what was considered, why, on what evidence, and whether it was
acted on; capability off, or an explicit model, leaves everything as before.
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
from openshard.osn.budget import BudgetLedger, BudgetLimits
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import AttemptUsage, ModelActionProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.osn.supervisor import RECORD_APPLIED, RECORD_SHADOW, RecoverySupervisor
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.routing.adaptive.recovery import RecoveryStep, build_recovery_plan
from openshard.routing.provider_availability import ProviderAvailability
from openshard.sync import config as sync_config

PY = sys.executable
TASK = "write ok into out.txt"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
OPENROUTER = ProviderAvailability(("openrouter",), True, False, False)
# The verifier echoes the content so successive failures are not identical output.
CHECK = [PY, "-c", "import sys; c=open('out.txt').read(); print(c); sys.exit(0 if c=='ok' else 1)"]
CHECK_CLI = f'"{PY}" -c "import sys; c=open(\'out.txt\').read(); print(c); sys.exit(0 if c==\'ok\' else 1)"'


def _m(mid: str, **kw) -> ModelEntry:
    defaults = dict(display_name=mid, provider=mid.split("/")[0], tier="mid", cost_class="mid",
                    supports_tools=True, context_length=200_000, lifecycle="active_default")
    defaults.update(kw)
    return ModelEntry(id=mid, **defaults)


CATALOG = build_catalog([
    _m("acme/mid-1", roles=("standard_coding",)),
    _m("acme/frontier-1", tier="frontier", cost_class="expensive", supports_reasoning=True,
       roles=("escalation",), lifecycle="active_specialist"),
], [], synced_at="2026-09-24T00:00:00Z")


class FakeProvider(BaseProvider):
    def __init__(self, replies, cost=0.01):
        self.replies = list(replies)
        self.calls: list[str] = []
        self.cost = cost

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append(model)
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, self.cost))


def _writes(*pairs):
    return json.dumps({"writes": [{"path": p, "content": c} for p, c in pairs]})


def _plan(*models, max_attempts=3):
    steps = tuple(RecoveryStep("frontier_reasoning", m) for m in models)
    return build_recovery_plan(steps, verification_available=True, verification_requested=True,
                               max_attempts=max_attempts)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "out.txt").write_text("bad")
    return r


def _supervisor(ap, plan, *, mode=RECORD_APPLIED, budget=None, not_acted=None):
    return RecoverySupervisor(
        plan=plan, usage_for=ap.usage_for, record_mode=mode, not_acted_reason=not_acted,
        cost_budget_usd=budget, first_model="acme/mid-1", first_class="balanced_coding",
        ladder_model_for=ap.model_for,
    )


class TestDecisionFunction:
    def test_escalates_then_stops_when_the_ladder_is_exhausted(self, repo):
        fp = FakeProvider(["x", "y", "z"])
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        ap.usage.append(AttemptUsage(1, "acme/mid-1", 10, 5, 0.01))
        d1 = sup.after_failed_attempt(1, verification_observed=True)
        assert d1.action == "escalate" and d1.recommended_model == "acme/frontier-1"
        assert d1.acted_on is None  # pending until the next attempt really calls the model
        sup.mark_acted()
        assert d1.acted_on is True
        assert d1.evidence["models_tried"] == ["acme/mid-1"] and d1.evidence["spend_usd"] == 0.01
        ap.usage.append(AttemptUsage(2, "acme/frontier-1", 10, 5, 0.02))
        d2 = sup.after_failed_attempt(2, verification_observed=True)
        assert d2.action == "stop" and d2.reason == "plan_attempts_exhausted"
        assert d2.evidence["attempts_so_far"] == 2 and d2.evidence["spend_usd"] == 0.03

    def test_unobserved_failure_never_escalates(self, repo):
        fp = FakeProvider([])
        ap = ModelActionProvider(fp, ["acme/mid-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        ap.usage.append(AttemptUsage(1, "acme/mid-1", 10, 5, 0.01))
        d = sup.after_failed_attempt(1, verification_observed=False)
        assert d.action == "stop" and d.reason == "failure_not_directly_observed"
        assert d.evidence["verification_source"] is None

    def test_shadow_decisions_are_never_acted_on(self, repo):
        fp = FakeProvider([])
        ap = ModelActionProvider(fp, ["acme/mid-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"), mode=RECORD_SHADOW, not_acted="user_ladder")
        ap.usage.append(AttemptUsage(1, "acme/mid-1", 10, 5, 0.01))
        d = sup.after_failed_attempt(1, verification_observed=True)
        assert d.action == "escalate" and d.acted_on is False and d.not_acted_reason == "user_ladder"
        assert sup.to_record()["record_mode"] == "shadow" and sup.to_record()["not_applied_reason"] == "user_ladder"


class TestInTheLoop:
    def test_applied_supervisor_stops_a_retry_that_would_rerun_the_last_model(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, supervisor=sup)
        # Without the supervisor attempt 3 would rerun acme/frontier-1 with no new evidence;
        # the plan (one rung) allows two attempts, so the policy stops on its attempt cap.
        assert r.status == "failed" and r.stop_reason == "supervisor_stop:plan_attempts_exhausted"
        assert fp.calls == ["acme/mid-1", "acme/frontier-1"]
        assert [a.supervision["action"] for a in r.attempts] == ["escalate", "stop"]
        assert all(a.supervision["acted_on"] for a in r.attempts)
        assert r.verification_state == "failed"

    def test_shadow_supervisor_changes_nothing_and_records(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"), mode=RECORD_SHADOW, not_acted="user_ladder")
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, supervisor=sup)
        assert r.status == "verified" and fp.calls == ["acme/mid-1", "acme/frontier-1", "acme/frontier-1"]
        assert [a.supervision["action"] for a in r.attempts if a.supervision] == ["escalate", "stop"]
        assert all(a.supervision["acted_on"] is False for a in r.attempts if a.supervision)
        assert r.attempts[2].supervision is None  # the last attempt cannot be retried: no boundary

    def test_applied_supervisor_picks_the_next_model_from_the_plan(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1"], repo)  # ladder would rerun mid-1
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=2, supervisor=sup)
        assert r.status == "verified" and fp.calls == ["acme/mid-1", "acme/frontier-1"]
        assert r.attempts[0].supervision["recommended_model"] == "acme/frontier-1"
        assert r.attempts[0].supervision["acted_on"] is True

    def test_spend_cap_stops_with_the_budget_reason(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))], cost=0.30)
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"), budget=0.25)
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, supervisor=sup)
        assert r.stop_reason == "supervisor_stop:cost_budget_exhausted" and len(fp.calls) == 1
        assert r.attempts[0].supervision["evidence"]["cost_budget_usd"] == 0.25

    def test_a_provider_that_cannot_switch_is_recorded_not_acted_on(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1"], repo)
        def plain(ctx):  # a bare callable provider: no set_next_model
            return ap(ctx)

        sup = RecoverySupervisor(plan=_plan("acme/frontier-1"), usage_for=lambda n: ap.usage_for(n),
                                 record_mode=RECORD_APPLIED, first_model="acme/mid-1", first_class="balanced_coding")
        r = run_bounded_loop(repo, TASK, plain, CHECK, max_attempts=2, supervisor=sup)
        assert r.status == "verified" and fp.calls == ["acme/mid-1", "acme/mid-1"]
        assert r.attempts[0].supervision["acted_on"] is False
        assert r.attempts[0].supervision["not_acted_reason"] == "provider_cannot_switch"

    def test_receipt_projection_and_render(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, supervisor=sup)
        entry = build_osn_run_entry(r, task=TASK, usage=ap.usage, duration_seconds=1.0, repo_path=repo,
                                    supervisor_record=sup.to_record())
        block = entry["supervisor_routing"]
        assert block["record_mode"] == "applied" and block["boundary"] == "observed_verification_failure_before_retry"
        assert [d["action"] for d in block["decisions"]] == ["escalate", "stop"]
        assert block["evidence"]["history"] == "not_used"
        receipt = build_shard_receipt(entry, index=0)
        ev = receipt.recorded_evidence["supervisor_routing"]
        assert ev["decisions"][1]["reason"] == "plan_attempts_exhausted"
        assert ev["decisions"][0]["evidence"]["spend_known"] is True
        text = render_full_shard_receipt(receipt)
        assert "SUPERVISOR" in text and "plan_attempts_exhausted" in text
        hosted = receipt_to_dict(receipt, extended=True)["supervisor_routing"]
        assert hosted == ev
        assert "supervisor_routing" not in receipt_to_dict(receipt)  # default/MCP shape stays unchanged


    def test_not_consulted_where_the_loop_would_not_retry(self, repo):
        # Identical failure output: the loop stops on its own rule first.
        same = [PY, "-c", "import sys; sys.exit(1)"]
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b"))])
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, same, max_attempts=3, supervisor=sup)
        assert r.stop_reason == "no_progress_identical_failure"
        assert r.attempts[0].supervision is not None and r.attempts[1].supervision is None
        # A verifier that could not even start: no outcome was observed. The policy
        # never escalates on a failure nobody saw, so an applied supervisor stops
        # the retry the loop would otherwise have made.
        fp2 = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b"))])
        ap2 = ModelActionProvider(fp2, ["acme/mid-1"], repo)
        sup2 = _supervisor(ap2, _plan("acme/frontier-1"))
        r2 = run_bounded_loop(repo, TASK, ap2, ["definitely-not-a-command-xyz"], max_attempts=3, supervisor=sup2)
        assert r2.attempts[0].verification.observed is False
        if r2.stop_reason == "verifier_setup_failed":
            assert sup2.decisions == []  # detected as an environment problem: not a boundary
        else:
            assert r2.stop_reason == "supervisor_stop:failure_not_directly_observed" and len(fp2.calls) == 1
            assert sup2.decisions[0].evidence["verification_source"] is None

    def test_a_budget_that_would_stop_is_never_pre_empted(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        led = BudgetLedger(BudgetLimits(max_attempts=2))
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo, budget=led)
        sup = _supervisor(ap, _plan("acme/frontier-1", "acme/mid-1", max_attempts=4))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, budget=led, supervisor=sup)
        # After attempt 2 the budget refuses another attempt: the budget stops, the supervisor stays silent.
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_attempts"
        assert led.limit_reached == "max_attempts" and led.action == "stopped_before_attempt"
        assert r.attempts[0].supervision["action"] == "escalate" and r.attempts[1].supervision is None
        assert len(sup.decisions) == 1

    def test_an_escalation_is_acted_on_only_once_the_next_attempt_ran(self, repo):
        class Flaky(FakeProvider):
            def execute(self, model, prompt, system=None, max_tokens=None):
                if len(self.calls) == 1:
                    self.calls.append(model)
                    raise RuntimeError("provider down")
                return super().execute(model, prompt, system, max_tokens)

        fp = Flaky([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, supervisor=sup)
        assert r.status == "error" and r.stop_reason == "provider_error"
        d = r.attempts[0].supervision
        assert d["action"] == "escalate" and d["acted_on"] is False
        assert d["not_acted_reason"] == "run_ended_before_retry"
        # And when the retry does run, the record confirms it afterwards.
        fp_ok = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))])
        ap_ok = ModelActionProvider(fp_ok, ["acme/mid-1"], repo)
        sup_ok = _supervisor(ap_ok, _plan("acme/frontier-1"))
        r_ok = run_bounded_loop(repo, TASK, ap_ok, CHECK, max_attempts=2, supervisor=sup_ok)
        assert r_ok.attempts[0].supervision["acted_on"] is True and fp_ok.calls[1] == "acme/frontier-1"
        assert r_ok.attempts[0].supervision["evidence"]["ladder_model"] == "acme/mid-1"
        assert r_ok.attempts[0].supervision["evidence"]["changed_next_model"] is True

    def test_a_provider_reporting_variant_ids_does_not_confuse_the_plan(self, repo):
        class Variant(FakeProvider):
            def execute(self, model, prompt, system=None, max_tokens=None):
                self.calls.append(model)
                return ChatResponse(self.replies.pop(0), f"{model}:nitro", UsageStats(10, 5, 15, self.cost))

        fp = Variant([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1", "acme/frontier-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=3, supervisor=sup)
        # The plan speaks requested ids: the rung that just ran is recognised and not rerun.
        assert fp.calls == ["acme/mid-1", "acme/frontier-1"]
        assert r.stop_reason == "supervisor_stop:plan_attempts_exhausted"
        assert r.attempts[1].supervision["evidence"]["models_tried"] == ["acme/mid-1", "acme/frontier-1"]

    def test_the_reask_uses_the_recommended_model(self, repo):
        fp = FakeProvider([_writes(("out.txt", "a")), "not json", _writes(("out.txt", "ok"))])
        ap = ModelActionProvider(fp, ["acme/mid-1"], repo)
        sup = _supervisor(ap, _plan("acme/frontier-1"))
        r = run_bounded_loop(repo, TASK, ap, CHECK, max_attempts=2, supervisor=sup)
        assert r.status == "verified" and fp.calls == ["acme/mid-1", "acme/frontier-1", "acme/frontier-1"]

    def test_unknown_attempt_usage_is_a_recorded_non_decision(self, repo):
        sup = RecoverySupervisor(plan=_plan("acme/frontier-1"), usage_for=lambda n: (None, None),
                                 record_mode=RECORD_APPLIED, first_model="acme/mid-1", first_class="balanced_coding")
        d = sup.after_failed_attempt(1, verification_observed=True)
        assert d.action == "stop" and d.reason == "attempt_usage_unknown"
        assert d.acted_on is False and d.not_acted_reason == "attempt_usage_unknown"


# ---------------------------------------------------------------------------
# `openshard osn run` gating
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append(self.path)
        status, body = type(self).routes.get(self.path, (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


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


@pytest.fixture
def catalog():
    with patch("openshard.models.catalog.load_catalog", return_value=CATALOG), \
         patch("openshard.routing.provider_availability.detect_provider_availability", return_value=OPENROUTER):
        yield


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
    _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, json.dumps({"organisation_id": ORG, "capabilities": [
        {"key": k, "name": k, "description": "", "stage": "internal", "enabled": True, "enabled_at": "x"} for k in keys
    ]}).encode())
    return repo


def _invoke(monkeypatch, fp, *extra):
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("openrouter", fp))
    return CliRunner().invoke(cli, ["osn", "run", TASK, "--verify-cmd", CHECK_CLI, "--max-attempts", "3", "--json", *extra])


def _last_run(repo):
    return json.loads((repo / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])


class TestCli:
    def test_both_capabilities_on_applies_the_supervisor(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing", "supervisor_routing"])
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "failed" and body["stop_reason"] == "supervisor_stop:plan_attempts_exhausted"
        assert fp.calls == ["acme/mid-1", "acme/frontier-1"]
        assert body["supervisor_routing"]["record_mode"] == "applied"
        assert _last_run(repo)["supervisor_routing"]["decisions"][-1]["acted_on"] is True
        assert _Handler.seen == [f"/v1/orgs/{ORG}/capabilities"]  # one shared lookup

    def test_user_ladder_keeps_the_supervisor_in_shadow(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing", "supervisor_routing"])
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "b")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp, "--escalate-model", "acme/frontier-1")
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "verified" and len(fp.calls) == 3
        sup = _last_run(repo)["supervisor_routing"]
        assert sup["record_mode"] == "shadow" and sup["not_applied_reason"] == "user_ladder"
        assert [d["acted_on"] for d in sup["decisions"]] == [False, False]

    def test_supervisor_without_adaptive_routing_is_shadow(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["supervisor_routing"])
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        sup = _last_run(repo)["supervisor_routing"]
        assert sup["record_mode"] == "shadow" and sup["not_applied_reason"] == "adaptive_routing_not_applied"
        assert len(fp.calls) == 2  # the legacy model and ladder ran exactly as before

    def test_capability_off_or_explicit_model_records_nothing(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform, ["adaptive_routing"])
        fp = FakeProvider([_writes(("out.txt", "a")), _writes(("out.txt", "ok"))])
        assert _invoke(monkeypatch, fp).exit_code == 0
        assert "supervisor_routing" not in _last_run(repo)
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, json.dumps({"organisation_id": ORG, "capabilities": [
            {"key": "supervisor_routing", "name": "s", "description": "", "stage": "internal", "enabled": True, "enabled_at": "x"}
        ]}).encode())
        _Handler.seen = []
        # A model the catalog does not know: the decision cannot call it "explicit",
        # but the user named it, so nothing is supervised and nothing is looked up.
        monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home2"))  # no cache can answer for it
        fp2 = FakeProvider([_writes(("out.txt", "ok"))])
        assert _invoke(monkeypatch, fp2, "--model", "nobody/custom").exit_code == 0
        entry = _last_run(repo)
        assert "supervisor_routing" not in entry and _Handler.seen == []
        assert fp2.calls == ["nobody/custom"] and entry["routing_provenance"]["selection_mode"] == "none"
