"""Agent Budgets V1 (``openshard.osn.budget``) in the bounded OSN loop.

Proves: the capability off (or unconfirmed) leaves ``openshard osn run``
unchanged; with it on, spend / attempt / command / write limits stop the
run at the boundary before more work, retries cannot bypass them, and the
Receipt records the configured limits, the observed usage, the limit
reached and the action OpenShard took.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.osn.budget import BudgetExhausted, BudgetLedger, BudgetLimits, not_enforced_record
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import ModelActionProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.sync import config as sync_config

PY = sys.executable
CHECK = [PY, "-c", "import sys; sys.exit(0 if open('out.txt').read()=='ok' else 1)"]
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
ORG_B = "11111111-2222-4333-8444-555555555555"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
KEY_B = "osk_zzzzZZZZ_0123456789abcdefghijklmnopqrstuvwxyz"


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
        content = self.replies.pop(0)
        return ChatResponse(content, model, UsageStats(10, 5, 15, self.cost))


def _writes(*pairs):
    return json.dumps({"writes": [{"path": p, "content": c} for p, c in pairs]})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "out.txt").write_text("bad")
    return r


# ---------------------------------------------------------------------------
# limits and ledger
# ---------------------------------------------------------------------------


class TestLimits:
    def test_missing_block_means_no_budget(self):
        assert BudgetLimits.from_config(None).configured is False
        assert BudgetLimits.from_config({}).configured is False
        assert BudgetLimits.from_config({"max_writes": None}).configured is False

    def test_valid_block(self):
        lim = BudgetLimits.from_config({"max_spend_usd": 0.5, "max_attempts": 2, "max_commands": 3.0, "max_writes": 10})
        assert lim == BudgetLimits(0.5, 2, 3, 10)
        assert lim.to_dict() == {"max_spend_usd": 0.5, "max_attempts": 2, "max_commands": 3, "max_writes": 10}
        assert BudgetLimits.from_config({"max_spend_usd": 2}).to_dict() == {"max_spend_usd": 2.0}

    @pytest.mark.parametrize("block", [
        "cheap", ["x"], {"max_dollars": 1}, {"max_spend_usd": 0}, {"max_spend_usd": -1}, {"max_spend_usd": "1"},
        {"max_spend_usd": True}, {"max_attempts": 0}, {"max_attempts": 1.5}, {"max_commands": -3},
        {"max_writes": "10"}, {"max_writes": False},
    ])
    def test_unclear_blocks_are_refused_not_guessed(self, block):
        with pytest.raises(ValueError):
            BudgetLimits.from_config(block)


class TestLedger:
    def test_attempts(self):
        led = BudgetLedger(BudgetLimits(max_attempts=2))
        led.start_attempt()
        led.start_attempt()
        assert led.limit_reached == "max_attempts" and led.action == "none"
        with pytest.raises(BudgetExhausted) as info:
            led.start_attempt()
        assert info.value.stop_reason == "budget_max_attempts"
        assert led.attempts == 2 and led.action == "stopped_before_attempt"

    def test_spend_is_checked_before_the_call_and_recorded_after(self):
        led = BudgetLedger(BudgetLimits(max_spend_usd=0.25))
        led.before_model_call()
        led.record_model_call(0.30)  # one call may overshoot; the total is what it was
        assert led.limit_reached == "max_spend_usd" and led.spend_usd == 0.30
        with pytest.raises(BudgetExhausted) as info:
            led.before_model_call()
        assert info.value.stop_reason == "budget_max_spend_usd"
        assert led.action == "stopped_before_model_call"
        assert led.to_record()["usage"]["spend_usd"] == 0.30

    def test_unknown_spend_with_a_spend_limit_fails_closed(self):
        led = BudgetLedger(BudgetLimits(max_spend_usd=5.0))
        led.record_model_call(None)
        with pytest.raises(BudgetExhausted) as info:
            led.before_model_call()
        assert info.value.stop_reason == "budget_spend_unobservable"
        rec = led.to_record()
        assert rec["usage"]["spend_usd"] is None and rec["usage"]["spend_known"] is False
        assert rec["limit_reached"] is None and rec["action"] == "stopped_spend_unobservable"

    def test_unknown_spend_without_a_spend_limit_is_just_recorded(self):
        led = BudgetLedger(BudgetLimits(max_attempts=3))
        led.record_model_call(None)
        led.before_model_call()  # no spend limit: nothing to enforce
        assert led.to_record()["usage"]["spend_known"] is False

    def test_commands_and_writes(self):
        led = BudgetLedger(BudgetLimits(max_commands=1, max_writes=2))
        led.authorize_command()
        with pytest.raises(BudgetExhausted) as info:
            led.authorize_command()
        assert info.value.stop_reason == "budget_max_commands" and led.commands == 1
        led2 = BudgetLedger(BudgetLimits(max_writes=2))
        led2.authorize_write()
        led2.authorize_write()
        with pytest.raises(BudgetExhausted) as info:
            led2.authorize_write()
        assert info.value.stop_reason == "budget_max_writes" and led2.writes == 2

    def test_a_spent_command_or_write_budget_refuses_the_next_attempt_before_its_model_call(self):
        led = BudgetLedger(BudgetLimits(max_commands=1))
        led.start_attempt()
        led.authorize_command()
        with pytest.raises(BudgetExhausted) as info:
            led.start_attempt()
        assert info.value.limit == "max_commands" and info.value.action == "stopped_before_attempt"
        assert led.attempts == 1

    def test_record_shape(self):
        rec = BudgetLedger(BudgetLimits(max_spend_usd=1.0, max_writes=3)).to_record()
        assert rec == {
            "capability": "agent_budgets", "enforced": True,
            "limits": {"max_spend_usd": 1.0, "max_writes": 3},
            "usage": {"spend_usd": 0.0, "spend_known": True, "spend_is_estimate": True, "model_calls": 0, "attempts": 0, "commands": 0, "writes": 0},
            "limit_reached": None, "action": "none",
            "evidence": {"counts": "openshard_observed", "spend": "provider_usage_estimate"},
        }
        assert not_enforced_record(BudgetLimits(max_attempts=1), "capability_not_enabled") == {
            "capability": "agent_budgets", "enforced": False, "reason": "capability_not_enabled",
            "limits": {"max_attempts": 1},
        }


# ---------------------------------------------------------------------------
# enforcement inside the loop
# ---------------------------------------------------------------------------


def _model_provider(repo, fp, models=("m/a",), budget=None):
    return ModelActionProvider(fp, list(models), repo, budget=budget)


class TestLoopEnforcement:
    def test_no_budget_is_the_existing_loop(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp), CHECK, max_attempts=3)
        assert r.status == "verified" and len(fp.calls) == 2

    def test_retry_limit_stops_before_the_second_attempt(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        led = BudgetLedger(BudgetLimits(max_attempts=1))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_attempts"
        assert len(fp.calls) == 1  # no retry
        assert r.verification_state == "failed"  # the one observed verification did fail
        rec = led.to_record()
        assert rec["limit_reached"] == "max_attempts" and rec["action"] == "stopped_before_attempt"
        assert rec["usage"] == {"spend_usd": 0.001, "spend_known": True, "spend_is_estimate": True, "model_calls": 1, "attempts": 1,
                                "commands": 1, "writes": 1}

    def test_command_limit_stops_before_another_verify_run(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        led = BudgetLedger(BudgetLimits(max_commands=1))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_commands"
        assert led.commands == 1 and len(fp.calls) == 1  # no model call for an attempt that could not verify
        assert [a.verification is not None for a in r.attempts] == [True]

    def test_write_limit_stops_mid_proposal_and_writes_nothing_more(self, repo):
        fp = FakeProvider([_writes(("x.txt", "1"), ("y.txt", "2"))])
        led = BudgetLedger(BudgetLimits(max_writes=1))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_writes"
        sandbox = Path(r.sandbox_path)
        assert (sandbox / "x.txt").read_text() == "1" and not (sandbox / "y.txt").exists()
        assert not (repo / "x.txt").exists() and not (repo / "y.txt").exists()
        assert r.attempts[0].applied == ["x.txt"] and r.attempts[0].proposed == ["x.txt", "y.txt"]
        assert r.attempts[0].verification is None  # no command ran
        assert r.verification_state == "not_run"
        assert led.to_record()["action"] == "stopped_before_write" and led.writes == 1

    def test_spend_limit_stops_before_the_next_model_call_and_records_the_real_total(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))], cost=0.30)
        led = BudgetLedger(BudgetLimits(max_spend_usd=0.25))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_spend_usd"
        assert len(fp.calls) == 1
        assert len(r.attempts) == 2 and r.attempts[1].policy == {"budget_stop": "budget_max_spend_usd"}
        rec = led.to_record()
        assert rec["usage"]["attempts"] == 2 == len(r.attempts)
        assert rec["usage"]["spend_usd"] == 0.30 and rec["limit_reached"] == "max_spend_usd"
        assert rec["action"] == "stopped_before_model_call"

    def test_the_malformed_reply_reask_cannot_bypass_the_spend_limit(self, repo):
        fp = FakeProvider(["not json", _writes(("out.txt", "ok"))], cost=0.30)
        led = BudgetLedger(BudgetLimits(max_spend_usd=0.25))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_spend_usd"
        assert len(fp.calls) == 1  # stopped inside attempt 1, before the re-ask
        assert [a.policy for a in r.attempts] == [{"budget_stop": "budget_max_spend_usd"}]
        assert led.attempts == len(r.attempts) == 1 and led.model_calls == 1

    def test_unknown_cost_with_a_spend_limit_stops_rather_than_pretending_zero(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))], cost=None)
        led = BudgetLedger(BudgetLimits(max_spend_usd=1.0))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_spend_unobservable"
        assert len(fp.calls) == 1
        rec = led.to_record()
        assert rec["usage"]["spend_known"] is False and rec["usage"]["spend_usd"] is None
        assert rec["limit_reached"] is None and rec["action"] == "stopped_spend_unobservable"

    def test_a_roomy_budget_changes_nothing_and_records_usage(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))], cost=0.01)
        led = BudgetLedger(BudgetLimits(max_spend_usd=5.0, max_attempts=5, max_commands=5, max_writes=5))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert r.status == "verified"
        rec = led.to_record()
        assert rec["limit_reached"] is None and rec["action"] == "none"
        assert rec["usage"] == {"spend_usd": 0.02, "spend_known": True, "spend_is_estimate": True, "model_calls": 2, "attempts": 2,
                                "commands": 2, "writes": 2}

    def test_reaching_a_limit_on_the_last_permitted_unit_is_recorded_without_an_action(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        led = BudgetLedger(BudgetLimits(max_attempts=2))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert r.status == "verified"
        rec = led.to_record()
        assert rec["limit_reached"] == "max_attempts" and rec["action"] == "none"

    def test_policy_block_still_wins_over_budget_accounting(self, repo):
        fp = FakeProvider([_writes((".env", "SECRET=1"))])
        led = BudgetLedger(BudgetLimits(max_writes=5))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, budget=led)
        assert r.status == "blocked" and led.writes == 0  # a denied write is never counted as a write


    def test_escalation_models_go_through_the_same_ledger(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "still")), _writes(("out.txt", "ok"))],
                          cost=0.10)
        led = BudgetLedger(BudgetLimits(max_spend_usd=0.15))
        ap = _model_provider(repo, fp, models=("cheap/m", "strong/m", "stronger/m"), budget=led)
        # The verifier echoes the content so two failures are not identical output
        # (identical failures already stop the loop on their own).
        check = [PY, "-c", "import sys; c=open('out.txt').read(); print(c); sys.exit(0 if c=='ok' else 1)"]
        r = run_bounded_loop(repo, "t", ap, check, max_attempts=3, budget=led)
        assert r.status == "budget_exhausted" and r.stop_reason == "budget_max_spend_usd"
        assert [m for m, _ in fp.calls] == ["cheap/m", "strong/m"]  # the third rung was never paid for
        assert led.to_record()["usage"]["spend_usd"] == 0.20 and len(r.attempts) == 3
        assert r.attempts[2].policy == {"budget_stop": "budget_max_spend_usd"}

    def test_a_budget_stop_before_an_attempt_records_no_attempt_and_counts_agree(self, repo):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        led = BudgetLedger(BudgetLimits(max_attempts=1))
        r = run_bounded_loop(repo, "t", _model_provider(repo, fp, budget=led), CHECK, max_attempts=3, budget=led)
        assert led.attempts == len(r.attempts) == 1


# ---------------------------------------------------------------------------
# receipt entry
# ---------------------------------------------------------------------------


class TestReceiptEntry:
    def _run(self, repo, limits):
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))], cost=0.02)
        led = BudgetLedger(limits)
        ap = _model_provider(repo, fp, budget=led)
        r = run_bounded_loop(repo, "t", ap, CHECK, max_attempts=3, budget=led)
        entry = build_osn_run_entry(r, task="t", usage=ap.usage, duration_seconds=1.0, repo_path=repo,
                                    budget_record=led.to_record())
        return r, entry

    def test_enforced_budget_is_recorded_and_projected_to_hosted_receipt(self, repo):
        r, entry = self._run(repo, BudgetLimits(max_attempts=1, max_spend_usd=1.0))
        assert r.status == "budget_exhausted"
        block = entry["agent_budgets"]
        assert block["enforced"] is True and block["limits"] == {"max_spend_usd": 1.0, "max_attempts": 1}
        assert block["limit_reached"] == "max_attempts" and block["action"] == "stopped_before_attempt"
        assert block["usage"]["spend_usd"] == 0.02 and block["usage"]["attempts"] == 1
        assert entry["summary"] == "OSN loop budget_exhausted: budget_max_attempts"
        assert entry["verification"]["status"] == "failed"  # the observed verification, not a budget verdict
        assert entry["estimated_cost"] == 0.02

        receipt = build_shard_receipt(entry, index=0)
        ev = receipt.recorded_evidence["agent_budgets"]
        assert ev["enforced"] is True and ev["limits"] == {"max_spend_usd": 1.0, "max_attempts": 1}
        assert ev["usage"]["attempts"] == 1 and ev["limit_reached"] == "max_attempts"
        assert ev["action"] == "stopped_before_attempt"
        assert ev["evidence"] == {"counts": "openshard_observed", "spend": "provider_usage_estimate"}
        assert ev["usage"]["spend_is_estimate"] is True

        text = render_full_shard_receipt(receipt)
        assert "BUDGET" in text and "max_attempts=1" in text and "stopped_before_attempt" in text

        hosted = receipt_to_dict(receipt, extended=True)
        assert hosted["agent_budgets"] == ev
        assert "agent_budgets" not in receipt_to_dict(receipt)  # default/MCP shape stays unchanged

    def test_not_enforced_budget_is_recorded_as_such(self, repo):
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        ap = _model_provider(repo, fp)
        r = run_bounded_loop(repo, "t", ap, CHECK)
        entry = build_osn_run_entry(
            r, task="t", usage=ap.usage, duration_seconds=1.0, repo_path=repo,
            budget_record=not_enforced_record(BudgetLimits(max_writes=2), "capability_not_enabled"),
        )
        assert entry["agent_budgets"] == {"capability": "agent_budgets", "enforced": False,
                                          "reason": "capability_not_enabled", "limits": {"max_writes": 2}}
        ev = build_shard_receipt(entry, index=0).recorded_evidence["agent_budgets"]
        assert ev == {"enforced": False, "reason": "capability_not_enabled", "limits": {"max_writes": 2}}
        assert "no (capability_not_enabled)" in render_full_shard_receipt(build_shard_receipt(entry, index=0))

    def test_no_budget_leaves_the_entry_unchanged(self, repo):
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        ap = _model_provider(repo, fp)
        r = run_bounded_loop(repo, "t", ap, CHECK)
        entry = build_osn_run_entry(r, task="t", usage=ap.usage, duration_seconds=1.0, repo_path=repo)
        assert "agent_budgets" not in entry
        assert build_shard_receipt(entry, index=0).recorded_evidence["agent_budgets"] is None


# ---------------------------------------------------------------------------
# `openshard osn run` end to end, gated by the Platform capability
# ---------------------------------------------------------------------------


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
    no_policy = lambda org: json.dumps({
        "organisation_id": org, "version": None, "hash": None, "policy": None, "updated_at": None,
    }).encode()
    _Handler.routes = {
        f"/v1/orgs/{ORG}/policy": (200, no_policy(ORG)),
        f"/v1/orgs/{ORG_B}/policy": (200, no_policy(ORG_B)),
    }
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _link_env(monkeypatch, endpoint, org=ORG, key=KEY):
    monkeypatch.setenv(sync_config.ENDPOINT_ENV, endpoint)
    monkeypatch.setenv(sync_config.ORG_ENV, org)
    monkeypatch.setenv(sync_config.API_KEY_ENV, key)


def _unlink_env(monkeypatch):
    for var in (sync_config.ENDPOINT_ENV, sync_config.ORG_ENV, sync_config.API_KEY_ENV):
        monkeypatch.delenv(var, raising=False)


def _cli_repo(tmp_path, monkeypatch, budget_block):
    repo = tmp_path / "repo"
    (repo / ".openshard").mkdir(parents=True)
    (repo / "out.txt").write_text("bad")
    if budget_block is not None:
        (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"agent_budgets": budget_block}))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return repo


def _invoke(monkeypatch, fp, *extra):
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fp))
    return CliRunner().invoke(cli, [
        "osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd",
        f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"',
        "--max-attempts", "2", *extra,
    ])


def _last_run(repo):
    return json.loads((repo / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])


class TestCli:
    def test_without_a_budget_block_nothing_is_looked_up_and_nothing_changes(self, tmp_path, monkeypatch, platform):
        repo = _cli_repo(tmp_path, monkeypatch, None)
        _link_env(monkeypatch, f"http://127.0.0.1:{platform.server_address[1]}")
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp, "--json")
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "verified" and body["attempts"] == 2 and "agent_budgets" not in body
        assert "agent_budgets" not in _last_run(repo)
        assert _Handler.seen == [f"/v1/orgs/{ORG}/policy"]  # policy is checked even when no local budget exists

    def test_capability_off_records_not_enforced_and_runs_as_before(self, tmp_path, monkeypatch, platform):
        repo = _cli_repo(tmp_path, monkeypatch, {"max_attempts": 1})
        _link_env(monkeypatch, f"http://127.0.0.1:{platform.server_address[1]}")
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, []))
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        assert "not enforced" in r.output
        entry = _last_run(repo)
        assert entry["osn_loop"]["status"] == "verified" and len(entry["osn_loop"]["attempts"]) == 2
        assert entry["agent_budgets"] == {"capability": "agent_budgets", "enforced": False,
                                          "reason": "capability_not_enabled", "limits": {"max_attempts": 1}}
        assert _Handler.seen == [f"/v1/orgs/{ORG}/policy", f"/v1/orgs/{ORG}/capabilities"]

    def test_platform_unavailable_refuses_a_linked_run_before_work(self, tmp_path, monkeypatch):
        repo = _cli_repo(tmp_path, monkeypatch, {"max_attempts": 1})
        _link_env(monkeypatch, f"http://127.0.0.1:{_closed_port()}")
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 1
        assert "Organisation policy could not be refreshed (platform_policy_unavailable)" in r.output
        assert fp.calls == [] and not (repo / ".openshard" / "runs.jsonl").exists()

    def test_no_platform_link_keeps_the_feature_off(self, tmp_path, monkeypatch):
        repo = _cli_repo(tmp_path, monkeypatch, {"max_writes": 1})
        _unlink_env(monkeypatch)
        fp = FakeProvider([_writes(("a.txt", "1"), ("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        entry = _last_run(repo)
        assert entry["osn_loop"]["status"] == "verified"
        assert sorted(entry["osn_loop"]["changed_files"]) == ["a.txt", "out.txt"]  # both writes happened
        assert entry["agent_budgets"] == {"capability": "agent_budgets", "enforced": False,
                                          "reason": "no_platform_link", "limits": {"max_writes": 1}}

    def test_capability_on_enforces_and_the_receipt_explains_the_decision(self, tmp_path, monkeypatch, platform):
        repo = _cli_repo(tmp_path, monkeypatch, {"max_attempts": 1, "max_spend_usd": 1.0})
        _link_env(monkeypatch, f"http://127.0.0.1:{platform.server_address[1]}")
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, ["agent_budgets"]))
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))], cost=0.05)
        r = _invoke(monkeypatch, fp, "--json")
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "budget_exhausted" and body["stop_reason"] == "budget_max_attempts"
        assert body["attempts"] == 1 and len(fp.calls) == 1
        assert body["agent_budgets"]["enforced"] is True
        assert body["agent_budgets"]["limit_reached"] == "max_attempts"
        assert body["agent_budgets"]["action"] == "stopped_before_attempt"
        assert body["agent_budgets"]["usage"] == {"spend_usd": 0.05, "spend_known": True, "spend_is_estimate": True, "model_calls": 1,
                                                  "attempts": 1, "commands": 1, "writes": 1}
        entry = _last_run(repo)
        assert entry["agent_budgets"] == body["agent_budgets"]
        assert entry["verification"]["status"] == "failed"
        assert (repo / "out.txt").read_text() == "bad"  # nothing promoted, repo untouched

        receipt = build_shard_receipt(entry, index=0)
        text = render_full_shard_receipt(receipt)
        assert "BUDGET" in text and "Reached" in text and "max_attempts" in text
        hosted = receipt_to_dict(receipt, extended=True)["agent_budgets"]
        assert hosted["enforced"] is True
        assert hosted["limits"] == {"max_spend_usd": 1.0, "max_attempts": 1}
        assert hosted["limit_reached"] == "max_attempts" and hosted["action"] == "stopped_before_attempt"

        # A second run takes its own fresh snapshot at its start (one request per
        # run), so a dashboard toggle applies to the next run, not after a TTL.
        fp2 = FakeProvider([_writes(("out.txt", "ok"))], cost=0.05)
        r2 = _invoke(monkeypatch, fp2, "--json")
        assert r2.exit_code == 0, r2.output
        assert json.loads(r2.stdout)["agent_budgets"]["enforced"] is True
        assert _Handler.seen == [f"/v1/orgs/{ORG}/policy", f"/v1/orgs/{ORG}/capabilities"] * 2

    def test_capability_on_text_output_shows_the_budget_line(self, tmp_path, monkeypatch, platform):
        _cli_repo(tmp_path, monkeypatch, {"max_writes": 1})
        _link_env(monkeypatch, f"http://127.0.0.1:{platform.server_address[1]}")
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, ["agent_budgets"]))
        fp = FakeProvider([_writes(("a.txt", "1"), ("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        assert "budget_exhausted (budget_max_writes)" in r.output
        assert "budget: enforced (max_writes=1)" in r.output and "stopped_before_write" in r.output

    def test_another_organisations_grant_does_not_enable_this_one(self, tmp_path, monkeypatch, platform):
        repo = _cli_repo(tmp_path, monkeypatch, {"max_attempts": 1})
        ep = f"http://127.0.0.1:{platform.server_address[1]}"
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, ["agent_budgets"]))
        _Handler.routes[f"/v1/orgs/{ORG_B}/capabilities"] = (403, b'{"error":{"code":"forbidden"}}')

        # Organisation A runs first and its answer is cached in this home.
        _link_env(monkeypatch, ep)
        r = _invoke(monkeypatch, FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))]))
        assert r.exit_code == 0 and _last_run(repo)["agent_budgets"]["enforced"] is True

        # Organisation B, same machine, same home: refused by the Platform, so off.
        _link_env(monkeypatch, ep, org=ORG_B, key=KEY_B)
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 0, r.output
        entry = _last_run(repo)
        assert entry["agent_budgets"]["enforced"] is False and len(fp.calls) == 2
        assert _Handler.seen[-1] == f"/v1/orgs/{ORG_B}/capabilities"

    def test_an_unreadable_budget_block_is_refused_before_any_work(self, tmp_path, monkeypatch):
        _cli_repo(tmp_path, monkeypatch, {"max_writes": 0})
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 2
        assert "max_writes" in r.output and fp.calls == []

    def test_an_unparseable_config_file_is_refused_rather_than_silently_dropping_the_budget(self, tmp_path, monkeypatch):
        repo = _cli_repo(tmp_path, monkeypatch, None)
        (repo / ".openshard" / "config.yml").write_text("agent_budgets: [unclosed\n  max_writes: 1\n")
        fp = FakeProvider([_writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 2
        assert "could not be parsed" in r.output and fp.calls == []

    def test_platform_sync_kill_switch_refuses_a_linked_run(self, tmp_path, monkeypatch, platform):
        repo = _cli_repo(tmp_path, monkeypatch, {"max_attempts": 1})
        _link_env(monkeypatch, f"http://127.0.0.1:{platform.server_address[1]}")
        _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, _caps_body(ORG, ["agent_budgets"]))
        monkeypatch.setenv(sync_config.DISABLE_ENV, "off")
        fp = FakeProvider([_writes(("out.txt", "nope")), _writes(("out.txt", "ok"))])
        r = _invoke(monkeypatch, fp)
        assert r.exit_code == 1
        assert "Organisation policy could not be refreshed (platform_sync_disabled)" in r.output
        assert fp.calls == [] and not (repo / ".openshard" / "runs.jsonl").exists()
        assert _Handler.seen == []  # no request left the machine
