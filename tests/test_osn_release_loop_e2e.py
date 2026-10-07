"""v0.5.0 release loop, end to end, under a real organisation policy.

Task -> organisation policy (served by a loopback Platform) -> effective
restrictions -> OSN execution in an isolated copy -> OpenShard-run
verification -> canonical Receipt in runs.jsonl -> privacy-bounded envelope
-> flush to the Platform transport.

The only fake is the model (``FakeModel`` from the dogfood suite): policy,
the file-mutation gate, the command gate, the budget ledger, routing, file
writes, the verifier, the Receipt, the projection and the sync client are
all real. The other dogfood files serve ``"policy": null``; every CLI test
here serves an actual policy document, which is what makes #378/#381
controls provable.

Per-control edge cases live next to the unit they test (the command-prefix
matcher in test_command_policy.py, approver outcomes in
test_outcome_classification.py, the verifier env in test_osn_bounded_loop.py,
promotion aliasing in test_file_mutation_policy.py).
"""

from __future__ import annotations

import json
import os

import pytest
import yaml
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt
from openshard.sync import client, envelope, policies, transport
from openshard.sync import config as sync_config
from tests.test_osn_dogfood_e2e import (
    KEY,
    ORG,
    PY,
    TASK,
    VERIFY_CLI,
    FakeModel,
    _Handler,
    _writes,
)
from tests.test_osn_dogfood_e2e import catalog as catalog  # noqa: F401  (pytest fixture)
from tests.test_osn_dogfood_e2e import platform as platform  # noqa: F401  (pytest fixture)
from tests.test_platform_sync import CONTRACT_RECEIPT_KEYS, FORBIDDEN_KEYS

POLICY_HASH = "sha256:" + "ab" * 32
# An OSN Receipt also carries the bounded hosted ``learning`` summary (docs/learning.md).
HOSTED_KEYS = CONTRACT_RECEIPT_KEYS | {"learning"}


def _policy_document(*, permissions=None, models=None, budgets=None) -> dict:
    doc = {
        "schema_version": 1,
        "models": {
            "allowed_models": [], "blocked_models": [], "allowed_providers": [], "blocked_providers": [],
            "max_cost_class": None,
            # Lifecycle flags gate the routing pool and are ANDed with local
            # config; True leaves the local defaults in charge.
            "allow_specialist": True, "allow_experimental": True, "allow_watchlist": True,
            "allow_deprecated": True, "allow_open_weight": True, "allow_fallback": True,
            "allow_openrouter_wide": True,
            **(models or {}),
        },
        "budgets": {"max_spend_usd": None, "max_attempts": None, "max_commands": None, "max_writes": None,
                    **(budgets or {})},
    }
    if permissions is not None:
        doc["permissions"] = {"blocked_write_paths": [], "approval_write_paths": [], "blocked_command_prefixes": [],
                              **permissions}
    return doc


def _linked_repo(tmp_path, monkeypatch, platform, document, *, capabilities=("agent_budgets", "adaptive_routing")):
    repo = tmp_path / "repo"
    (repo / ".openshard").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.txt").write_text("bad")
    (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"agent_budgets": {"max_attempts": 2}}))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    monkeypatch.setenv(sync_config.ENDPOINT_ENV, f"http://127.0.0.1:{platform.server_address[1]}")
    monkeypatch.setenv(sync_config.ORG_ENV, ORG)
    monkeypatch.setenv(sync_config.API_KEY_ENV, KEY)
    _Handler.routes[f"/v1/orgs/{ORG}/capabilities"] = (200, json.dumps({
        "organisation_id": ORG,
        "capabilities": [{"key": k, "name": k, "description": "", "stage": "internal", "enabled": True,
                          "enabled_at": "x"} for k in capabilities],
    }).encode())
    _Handler.routes[f"/v1/orgs/{ORG}/policy"] = (200, json.dumps({
        "organisation_id": ORG, "version": 7, "hash": POLICY_HASH, "policy": document,
        "updated_at": "2026-09-29T00:00:00Z",
    }).encode())
    return repo


def _osn(monkeypatch, model, *extra, verify=VERIFY_CLI, max_attempts="2"):
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("openrouter", model))
    return CliRunner().invoke(cli, ["osn", "run", TASK, "--verify-cmd", verify, "--max-attempts", max_attempts,
                                    "--json", *extra])


def _runs(repo) -> list[dict]:
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()]


def _permissions(entry) -> dict:
    return {p["scope"]: p["state"] for p in entry["permission_evidence"]}


def _hosted(entry) -> dict:
    receipt = envelope.build_envelope(entry, 0, core_version="0.5.0")["receipt"]
    assert set(receipt) == HOSTED_KEYS
    assert not (FORBIDDEN_KEYS & set(receipt))
    return receipt


class TestGoldenPath:
    def test_policy_to_flushed_receipt_tells_one_story(self, tmp_path, monkeypatch, platform, catalog):
        repo = _linked_repo(tmp_path, monkeypatch, platform,
                            _policy_document(permissions={"blocked_write_paths": ["infra/**"]}))
        for _ in range(2):
            r = _osn(monkeypatch, FakeModel([_writes(("src/app.txt", "ok"))]))
            assert r.exit_code == 0, r.output
            assert json.loads(r.stdout)["status"] == "verified"
        first, second = _runs(repo)

        # Two independent runs are two Shards (history groups on shard_id).
        assert first["shard_id"] != second["shard_id"]
        assert first["receipt_id"] != second["receipt_id"]

        # Control evidence: the organisation policy that governed the run, by identity only.
        org = first["organisation_policy"]
        assert (org["organisation_policy_version"], org["organisation_policy_hash"]) == (7, POLICY_HASH)
        assert org["applied"] is True and org["effective_policy_hash"].startswith("sha256:")
        assert _permissions(first) == {"repo:write": "granted", "verification:execute": "granted"}
        # Execution + verification evidence: OpenShard ran the check itself.
        assert first["executor"] == "osn_loop"
        assert first["verification"]["source"] == "directly_observed"
        assert first["verification"]["observation_mode"] == "openshard_executed"
        assert first["verification"]["status"] == "passed"
        assert first["estimated_cost"] == pytest.approx(0.02) and first["tokens_provenance"] == "provider_reported"
        assert (repo / "src" / "app.txt").read_text() == "bad"  # isolated until promotion

        # The same truth survives the privacy-bounded projection...
        hosted = _hosted(first)
        assert hosted["origin"] == "openshard_routed"
        assert hosted["organisation_policy"]["organisation_policy_hash"] == POLICY_HASH
        assert {p["scope"]: p["state"] for p in hosted["permissions"]} == _permissions(first)
        assert (hosted["verification"]["source"], hosted["verification"]["observation_mode"]) == (
            "directly_observed", "openshard_executed")
        assert hosted["verification_status"] == "passed" and hosted["cost_usd"] == pytest.approx(0.02)
        assert any(d["decision"] == "allow" for d in hosted["policy_decisions"])
        assert str(repo) not in json.dumps(hosted) and "import sys" not in json.dumps(hosted)

        # ...and reaches the Platform transport exactly once per Receipt.
        rt = transport.RecordingPlatformTransport()
        report = client.flush(repo, env=os.environ, transport=rt)
        assert report.created == 2, report
        sent = [e["receipt"] for e in rt.envelopes]
        assert {s["receipt_id"] for s in sent} == {first["receipt_id"], second["receipt_id"]}
        assert len({s["shard_id"] for s in sent}) == 2
        assert all(set(s) == HOSTED_KEYS for s in sent)
        assert client.flush(repo, env=os.environ, transport=rt).sent == 0  # idempotent


class TestOrganisationControlsThroughTheCli:
    def test_an_organisation_blocked_write_path_stops_the_run(self, tmp_path, monkeypatch, platform, catalog):
        repo = _linked_repo(tmp_path, monkeypatch, platform,
                            _policy_document(permissions={"blocked_write_paths": ["infra/**"]}))
        model = FakeModel([_writes(("src/app.txt", "ok"), ("infra/main.tf", "x"))])
        r = _osn(monkeypatch, model)
        assert r.exit_code == 0, r.output
        body = json.loads(r.stdout)
        assert body["status"] == "blocked" and len(model.calls) == 1  # a policy block is never retried
        (entry,) = _runs(repo)
        deny = [d for d in entry["policy_decisions"] if d["decision"] == "deny"]
        assert [(d["resource"], d["source"]) for d in deny] == [("infra/main.tf", "organisation_policy")]
        assert _permissions(entry)["repo:write"] == "blocked"
        assert entry["verification"]["status"] == "not_run"  # nothing is claimed about unverified work
        assert not (repo / "infra").exists()
        hosted = _hosted(entry)
        assert {p["scope"]: p["state"] for p in hosted["permissions"]}["repo:write"] == "blocked"
        # Unverified work is never projected as a verdict: the structured block
        # says not_run and the flat token carries no status (nothing was observed).
        assert hosted["verification"]["status"] == "not_run" and hosted["verification_status"] is None

    @pytest.mark.parametrize(("flags", "outcome", "granted"), [
        ((), "unanswered", False),        # --json and no --yes: nobody could answer, nobody refused
        (("--yes",), "granted", True),
    ])
    def test_an_organisation_approval_path_records_who_answered(
        self, tmp_path, monkeypatch, platform, catalog, flags, outcome, granted,
    ):
        repo = _linked_repo(tmp_path, monkeypatch, platform,
                            _policy_document(permissions={"approval_write_paths": ["src/**"]}))
        r = _osn(monkeypatch, FakeModel([_writes(("src/app.txt", "ok"))]), *flags)
        assert r.exit_code == 0, r.output
        (entry,) = _runs(repo)
        approval = entry["approval_receipt"]
        assert (approval["outcome"], approval["granted"]) == (outcome, granted)
        assert json.loads(r.stdout)["status"] == ("verified" if granted else "blocked")
        ask = next(d for d in entry["policy_decisions"] if d["decision"] == "ask")
        assert ask["source"] == "organisation_policy"
        if granted:
            assert ask["approval_granted"] is True
        else:
            assert ask.get("approval_granted") is None
            assert build_shard_receipt(entry).approval_reason.endswith("no approver was available")
        assert _hosted(entry)["approval_detail"]["granted"] is granted

    def test_an_organisation_blocked_verify_prefix_stops_before_any_model_call(
        self, tmp_path, monkeypatch, platform, catalog,
    ):
        repo = _linked_repo(tmp_path, monkeypatch, platform,
                            _policy_document(permissions={"blocked_command_prefixes": ["python -c"]}))
        model = FakeModel([_writes(("src/app.txt", "ok"))])
        # A bare `python` is run as sys.executable (python.exe / python3.x): the rule still matches.
        r = _osn(monkeypatch, model, verify='python -c "print(1)"')
        assert r.exit_code == 0, r.output
        assert json.loads(r.stdout)["status"] == "blocked" and model.calls == []
        (entry,) = _runs(repo)
        assert entry["osn_loop"]["stop_reason"] == "verification_command_policy_block"
        assert _permissions(entry) == {"verification:execute": "blocked"}
        assert entry["verification"]["status"] == "not_run"
        assert _hosted(entry)["permissions"] == [{"scope": "verification:execute", "state": "blocked"}]


class TestOrganisationRestrictionsBeforeExecution:
    def test_a_blocked_model_is_refused_and_nothing_is_recorded_as_run(
        self, tmp_path, monkeypatch, platform, catalog,
    ):
        repo = _linked_repo(tmp_path, monkeypatch, platform,
                            _policy_document(models={"blocked_models": ["acme/mid-1"]}))
        model = FakeModel([_writes(("src/app.txt", "ok"))])
        r = _osn(monkeypatch, model, "--model", "acme/mid-1")
        assert r.exit_code != 0
        assert "blocked by the effective policy" in r.output and "policy:blocked_model" in r.output
        assert model.calls == [] and not (repo / ".openshard" / "runs.jsonl").exists()

    def test_an_organisation_budget_is_enforced_without_the_capability(
        self, tmp_path, monkeypatch, platform, catalog,
    ):
        repo = _linked_repo(tmp_path, monkeypatch, platform, _policy_document(budgets={"max_writes": 0}),
                            capabilities=())
        r = _osn(monkeypatch, FakeModel([_writes(("src/app.txt", "ok"))]), "--model", "acme/mid-1")
        assert r.exit_code == 0, r.output
        assert json.loads(r.stdout)["status"] == "budget_exhausted"
        (entry,) = _runs(repo)
        budget = entry["agent_budgets"]
        assert budget["enforced"] is True and budget["limits"]["max_writes"] == 0
        assert entry["verification"]["status"] == "not_run"


class TestPolicyResponsesFailClosed:
    @pytest.mark.parametrize("body", [
        {"organisation_id": ORG, "version": 7, "hash": POLICY_HASH, "policy": {"schema_version": 2}},
        {"organisation_id": ORG, "version": 7, "hash": "md5:x", "policy": _policy_document()},
        {"organisation_id": ORG, "version": 0, "hash": POLICY_HASH, "policy": _policy_document()},
        {"organisation_id": "someone-else", "version": 7, "hash": POLICY_HASH, "policy": _policy_document()},
        {"organisation_id": ORG, "version": None, "hash": POLICY_HASH, "policy": None},
        {"organisation_id": ORG, "version": 7, "hash": POLICY_HASH,
         "policy": _policy_document(permissions={"blocked_command_prefixes": ["npm; rm -rf /"]})},
        {"organisation_id": ORG, "version": 7, "hash": POLICY_HASH,
         "policy": _policy_document(permissions={"blocked_write_paths": ["../outside"]})},
    ])
    def test_a_malformed_policy_response_is_never_accepted(self, body):
        assert policies._parse_response(json.dumps(body).encode(), organisation_id=ORG) is None

    def test_a_valid_policy_response_is_parsed(self):
        state = policies._parse_response(json.dumps({
            "organisation_id": ORG, "version": 7, "hash": POLICY_HASH,
            "policy": _policy_document(permissions={"approval_write_paths": ["src/**"]}),
        }).encode(), organisation_id=ORG)
        assert state is not None and state.applied and state.version == 7

    def test_an_unreadable_policy_refuses_a_linked_run(self, tmp_path, monkeypatch, platform, catalog):
        repo = _linked_repo(tmp_path, monkeypatch, platform, _policy_document())
        _Handler.routes[f"/v1/orgs/{ORG}/policy"] = (200, b'{"organisation_id": "' + ORG.encode()
                                                     + b'", "version": 7, "hash": "bad", "policy": {}}')
        model = FakeModel([_writes(("src/app.txt", "ok"))])
        r = _osn(monkeypatch, model)
        assert r.exit_code != 0 and "Organisation policy could not be refreshed" in r.output
        assert model.calls == [] and not (repo / ".openshard" / "runs.jsonl").exists()


def test_the_verifier_used_here_is_the_real_interpreter():
    """Guard: the E2E verifier is a real process OpenShard runs, not a stub."""
    assert os.path.exists(PY)
