"""OSN dogfood path, end to end: task -> policy (allow / ask / deny) -> budget -> routing ->
execution in an isolated copy -> OpenShard-run verification -> Receipt.

Nothing here fakes success. A fake *model* proposes writes; everything after
that is the real loop: the file-mutation gate, the budget ledger, the adaptive
routing decision, real file writes into the isolated copy, a real verify
command run by OpenShard, and the real Shard entry / Receipt projections.

The assertions are the product story: from the stored Receipt alone,
OpenShard can explain what the agent attempted, what it allowed, what needed
approval, what it denied, which files changed, which commands ran and how
they exited, what it cost where known, whether verification actually ran,
what the outcome was, and why execution continued or stopped.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.failures import classify_failure
from openshard.history.run_cost import run_total_cost
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.history.trust_score import evaluate_trust_score
from openshard.history.views import receipt_to_dict
from openshard.models.catalog import ModelEntry, build_catalog
from openshard.osn.budget import BudgetLedger, BudgetLimits
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import ModelActionProvider
from openshard.osn.routing import resolve_osn_routing
from openshard.osn.run_entry import build_osn_run_entry
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.routing.engine import route
from openshard.routing.provider_availability import ProviderAvailability
from openshard.sync import config as sync_config

PY = sys.executable
TASK = "write ok into src/app.txt"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
OPENROUTER = ProviderAvailability(("openrouter",), True, False, False)
VERIFY = [PY, "-c", "import sys; c=open('src/app.txt').read(); print(c); sys.exit(0 if c=='ok' else 1)"]
VERIFY_CLI = f'"{PY}" -c "import sys; c=open(\'src/app.txt\').read(); print(c); sys.exit(0 if c==\'ok\' else 1)"'


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


class FakeModel(BaseProvider):
    """The only fake: a model that proposes writes. It never touches files or commands."""

    def __init__(self, replies, cost=0.02):
        self.replies = list(replies)
        self.calls: list[str] = []
        self.cost = cost

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append(model)
        return ChatResponse(self.replies.pop(0), model, UsageStats(100, 50, 150, self.cost))


def _writes(*pairs):
    return json.dumps({"writes": [{"path": p, "content": c} for p, c in pairs]})


@pytest.fixture
def catalog():
    with patch("openshard.models.catalog.load_catalog", return_value=CATALOG), \
         patch("openshard.routing.provider_availability.detect_provider_availability", return_value=OPENROUTER):
        yield


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.txt").write_text("bad")
    return r


def _decisions(entry, decision=None):
    out = [d for d in entry["policy_decisions"] if decision is None or d["decision"] == decision]
    return sorted(out, key=lambda d: d["resource"] or "")


class TestAllowAskEscalateVerify:
    """Allowed write + approved sensitive write, observed failure, escalation, observed pass."""

    def test_the_receipt_tells_the_whole_story(self, repo, catalog):
        model = FakeModel([
            _writes(("src/app.txt", "nope"), ("pyproject.toml", "[tool.x]\n")),
            _writes(("src/app.txt", "ok")),
        ])
        ledger = BudgetLedger(BudgetLimits(max_spend_usd=1.0, max_attempts=3, max_commands=3, max_writes=10))
        routing = resolve_osn_routing(
            TASK, explicit_model=None, escalate=[], capability_enabled=lambda: True,
            legacy_model=lambda t: route(t).model,
        )
        provider = ModelActionProvider(model, routing.models, repo, budget=ledger)
        approvals: list[tuple[str, str]] = []

        def approver(rel, decision):
            approvals.append((rel, decision.decision))
            return True, "test_reviewer"

        result = run_bounded_loop(repo, TASK, provider, VERIFY, max_attempts=3, approver=approver, budget=ledger)
        entry = build_osn_run_entry(
            result, task=TASK, usage=provider.usage, duration_seconds=2.0, repo_path=repo,
            budget_record=ledger.to_record(), routing_decision=routing.decision,
            routing_record_mode=routing.record_mode, routing_record=routing.record,
        )
        receipt = build_shard_receipt(entry, index=0)
        text = render_full_shard_receipt(receipt)

        # Outcome, and why the loop continued and then stopped.
        assert result.status == "verified" and result.stop_reason == "verification_passed"
        assert entry["summary"] == "OSN loop verified: verification_passed"
        attempts = entry["osn_loop"]["attempts"]
        assert [a["verification"]["exit_code"] for a in attempts] == [1, 0]
        assert model.calls == ["acme/mid-1", "acme/frontier-1"]  # escalated only after an observed failure
        assert entry["retry_triggered"] is True and entry["fixer_model"] == "acme/frontier-1"
        assert entry["retry_attempts"][0]["model"] == "acme/frontier-1"

        # What the agent attempted (declared), what OpenShard allowed, what needed approval.
        assert attempts[0]["proposed"] == ["src/app.txt", "pyproject.toml"]
        assert attempts[0]["applied"] == ["src/app.txt", "pyproject.toml"] and attempts[0]["blocked"] == []
        assert attempts[0]["policy"]["approval_required"] == ["pyproject.toml"]
        assert attempts[0]["policy"]["approval_granted"] == ["pyproject.toml"]
        assert attempts[0]["policy"]["approval_sources"] == ["test_reviewer"]
        assert approvals == [("pyproject.toml", "ask")]
        assert entry["osn_loop"]["evidence"]["actions"] == "agent_declared"
        assert entry["osn_loop"]["evidence"]["policy_and_file_effects"] == "openshard_observed"

        allows = _decisions(entry, "allow")
        asks = _decisions(entry, "ask")
        assert _decisions(entry, "deny") == []
        assert [d["resource"] for d in allows] == ["src/app.txt", "src/app.txt"]  # one per attempt that wrote it
        assert [d["resource"] for d in asks] == ["pyproject.toml"]
        assert asks[0]["approval_required"] is True and asks[0]["approval_granted"] is True
        assert asks[0]["source"] == "file_mutation_policy" and asks[0]["action"] == "file_write"
        assert entry["approval_receipt"] == {
            "source": "file_mutation_policy", "requested": True, "granted": True, "action": "file_write",
            "reason": "approval granted for 1 sensitive path(s)", "approval_sources": ["test_reviewer"],
        }
        assert receipt.approval == "Required → Granted" and receipt.approval_granted is True
        assert len(receipt.policy_decisions) == 3

        # What changed, and where: the isolated copy only.
        assert sorted(entry["osn_loop"]["changed_files"]) == ["pyproject.toml", "src/app.txt"]
        assert entry["files_created"] == 1 and entry["files_updated"] == 1  # pyproject new, app.txt existed
        assert (repo / "src" / "app.txt").read_text() == "bad" and not (repo / "pyproject.toml").exists()
        assert (Path(result.sandbox_path) / "src" / "app.txt").read_text() == "ok"

        # What ran: the verify command, by OpenShard, twice, exit codes observed.
        v = entry["verification"]
        assert v["status"] == "passed" and v["source"] == "directly_observed"
        assert v["observation_mode"] == "openshard_executed" and v["exit_code"] == 0
        assert entry["verification_attempted"] is True and entry["verification_passed"] is True
        assert all(a["verification"]["ran"] and a["verification"]["observed"] for a in attempts)
        assert all(len(a["verification"]["command"]) == 1 for a in attempts)  # executable name only

        # What it cost, where known, and the budget that watched it.
        assert entry["estimated_cost"] == 0.02 and entry["retry_estimated_cost"] == 0.02
        assert run_total_cost(entry) == (0.04, True)  # total, and every attempt's cost was known
        budget = entry["agent_budgets"]
        assert budget["enforced"] is True and budget["limit_reached"] is None and budget["action"] == "none"
        assert budget["usage"] == {"spend_usd": 0.04, "spend_known": True, "spend_is_estimate": True,
                                   "model_calls": 2, "attempts": 2, "commands": 2, "writes": 3}

        # How the model was chosen.
        ar = entry["adaptive_routing"]
        assert ar["applied"] is True and ar["selected_model"] == "acme/mid-1"
        assert ar["escalation_ladder"] == ["acme/frontier-1"] and ar["ladder_source"] == "recovery_plan"
        assert entry["routing_provenance"]["record_mode"] == "applied"
        assert entry["routing_provenance"]["agrees_with_execution"] is True

        # The same story from the canonical Receipt and its projections.
        ev = receipt.recorded_evidence
        assert ev["execution_loop"]["status"] == "verified"
        assert [a["applied_count"] for a in ev["execution_loop"]["attempts"]] == [2, 1]
        assert ev["agent_budgets"]["usage"]["commands"] == 2
        assert ev["adaptive_routing"]["escalation_ladder"] == ["acme/frontier-1"]
        for section in ("POLICY DECISIONS", "BUDGET", "ADAPTIVE ROUTING", "APPROVAL"):
            assert section in text, section
        assert "ask     file_write" in text.replace("  ", " ").replace(" ", " ") or "ask" in text
        ext = receipt_to_dict(receipt, extended=True)
        assert ext["policy_decisions"] and {d["decision"] for d in ext["policy_decisions"]} == {"allow", "ask"}
        assert all("resource" not in d for d in ext["policy_decisions"])  # paths never leave the machine
        assert ext["verification_status"] == "passed"
        assert "agent_budgets" not in ext and "adaptive_routing" not in ext  # closed wire contract
        assert classify_failure(entry, receipt).category == "no_failure_detected"
        assert not any("policy_denied" in str(p) for p in evaluate_trust_score(entry, receipt).penalties)


# ---------------------------------------------------------------------------
# CLI path with a loopback Platform: deny and ask-without-approver
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}

    def do_GET(self):  # noqa: N802 - http.server API
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
    _Handler.routes = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cli_repo(tmp_path, monkeypatch, platform):
    repo = tmp_path / "repo"
    (repo / ".openshard").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.txt").write_text("bad")
    (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"agent_budgets": {"max_attempts": 2, "max_writes": 5}}))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    monkeypatch.setenv(sync_config.ENDPOINT_ENV, f"http://127.0.0.1:{platform.server_address[1]}")
    monkeypatch.setenv(sync_config.ORG_ENV, ORG)
    monkeypatch.setenv(sync_config.API_KEY_ENV, KEY)
    _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, json.dumps({
        "organisation_id": ORG,
        "capabilities": [{"key": k, "name": k, "description": "", "stage": "internal", "enabled": True,
                          "enabled_at": "x"} for k in ("agent_budgets", "adaptive_routing")],
    }).encode())
    return repo


def _run_cli(monkeypatch, model):
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("openrouter", model))
    return CliRunner().invoke(cli, ["osn", "run", TASK, "--verify-cmd", VERIFY_CLI, "--max-attempts", "3", "--json"])


def _last_run(repo):
    return json.loads((repo / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])


class TestDenyAndAskViaCli:
    def test_a_denied_secret_write_stops_the_run_and_is_explained(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform)
        model = FakeModel([_writes(("src/app.txt", "ok"), (".env", "SECRET=1"))])
        r = _run_cli(monkeypatch, model)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "blocked" and body["stop_reason"] == "policy_or_path_block"
        assert body["models"] == ["acme/mid-1", "acme/frontier-1"] and model.calls == ["acme/mid-1"]  # no retry
        entry = _last_run(repo)
        receipt = build_shard_receipt(entry, index=0)

        denies = _decisions(entry, "deny")
        assert [d["resource"] for d in denies] == [".env"]
        assert denies[0]["source"] == "file_mutation_policy" and denies[0]["severity"] == "high"
        assert [d["resource"] for d in _decisions(entry, "allow")] == ["src/app.txt"]
        assert entry["osn_loop"]["attempts"][0]["blocked"] == [".env"]
        assert entry["osn_loop"]["attempts"][0]["applied"] == ["src/app.txt"]  # in the isolated copy only
        assert (repo / "src" / "app.txt").read_text() == "bad" and not (repo / ".env").exists()

        # Nothing was verified and the record does not pretend otherwise.
        assert entry["verification"]["status"] == "not_run"
        assert entry["verification_attempted"] is False and entry["verification_passed"] is None
        assert entry["agent_budgets"]["usage"]["commands"] == 0 and entry["agent_budgets"]["action"] == "none"
        assert entry["adaptive_routing"]["applied"] is True

        # Downstream consumers see a policy denial, not a mystery.
        assert classify_failure(entry, receipt).category == "policy_denied"
        assert any("policy_denied" in str(p) for p in evaluate_trust_score(entry, receipt).penalties)
        text = render_full_shard_receipt(receipt)
        assert "deny" in text and "protected path" in text
        ext = receipt_to_dict(receipt, extended=True)
        assert any(d["decision"] == "deny" for d in ext["policy_decisions"])

    def test_a_sensitive_write_without_an_approver_fails_closed_and_says_approval_was_needed(
        self, tmp_path, monkeypatch, platform, catalog,
    ):
        repo = _cli_repo(tmp_path, monkeypatch, platform)
        model = FakeModel([_writes(("src/app.txt", "ok"), ("pyproject.toml", "x"))])
        r = _run_cli(monkeypatch, model)
        assert r.exit_code == 0, r.output
        assert json.loads(r.stdout)["status"] == "blocked"
        entry = _last_run(repo)
        receipt = build_shard_receipt(entry, index=0)
        asks = _decisions(entry, "ask")
        assert [d["resource"] for d in asks] == ["pyproject.toml"]
        assert asks[0]["approval_required"] is True and asks[0]["approval_granted"] is None  # nobody was asked
        assert entry["osn_loop"]["attempts"][0]["policy"]["approval_unavailable"] == ["pyproject.toml"]
        assert entry["approval_receipt"]["granted"] is False
        assert "no approver" in entry["approval_receipt"]["reason"]
        assert receipt.approval == "Required → Denied"
        assert classify_failure(entry, receipt).category == "policy_denied"
        assert not (repo / "pyproject.toml").exists()

    def test_budget_and_routing_are_both_in_the_same_receipt(self, tmp_path, monkeypatch, platform, catalog):
        repo = _cli_repo(tmp_path, monkeypatch, platform)
        model = FakeModel([_writes(("src/app.txt", "nope")), _writes(("src/app.txt", "still")),
                           _writes(("src/app.txt", "ok"))])
        r = _run_cli(monkeypatch, model)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        # Routing planned two rungs; the budget (max_attempts 2) stopped before a third model call.
        assert body["status"] == "budget_exhausted" and body["stop_reason"] == "budget_max_attempts"
        assert model.calls == ["acme/mid-1", "acme/frontier-1"]
        entry = _last_run(repo)
        assert entry["agent_budgets"]["limit_reached"] == "max_attempts"
        assert entry["agent_budgets"]["action"] == "stopped_before_attempt"
        assert entry["adaptive_routing"]["escalation_ladder"] == ["acme/frontier-1"]
        assert entry["verification"]["status"] == "failed"  # the last observed verification
        assert [a["verification"]["exit_code"] for a in entry["osn_loop"]["attempts"]] == [1, 1]
        assert _decisions(entry, "deny") == [] and len(_decisions(entry, "allow")) == 2
        receipt = build_shard_receipt(entry, index=0)
        assert classify_failure(entry, receipt).category == "verification_failed"
