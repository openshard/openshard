"""Planner -> Executor -> Verifier as real OSN runtime behaviour.

The only fake is the model. The planner reads the isolated copy through the
same harness as the executor (writes refused), the executor runs the real
turn loop, the verifier reviews a result OpenShard itself verified, a failed
review buys one bounded recovery attempt whose changes are undone when it
does not verify, and the Receipt names every role's model, usage and cost
without ever letting a model's opinion stand in for verification.
"""
from __future__ import annotations

import json
import sys

import pytest

from openshard.history import receipt_evidence as ev
from openshard.history.routing_truth import build_routing_truth
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)
from openshard.history.views import receipt_to_dict
from openshard.osn import roles
from openshard.osn.budget import BudgetLedger, BudgetLimits
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import IterativeModelProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = [PY, "-c", "import sys; sys.exit(0 if open('src/app.txt').read()=='ok' else 1)"]
TASK = "make src/app.txt contain ok"


def _turn(*actions, note="", **extra):
    return json.dumps({"actions": list(actions), "note": note, **extra})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


PLAN = {"summary": "Write ok into src/app.txt", "files": ["src/app.txt"], "steps": ["read", "write", "verify"],
        "verification": ["the check exits 0"], "simple": True}


class FakeModel(BaseProvider):
    """Replies are consumed in call order; every call records which model was asked."""

    def __init__(self, replies, cost=0.001):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, str]] = []  # (model, system, prompt)
        self.cost = cost

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append((model, system or "", prompt))
        if not self.replies:
            raise RuntimeError("no scripted reply left")
        content = self.replies.pop(0)
        if isinstance(content, Exception):
            raise content
        return ChatResponse(content, model, UsageStats(50, 10, 60, self.cost, cost_source="provider_reported"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.txt").write_text("bad")
    (r / "README.md").write_text("# demo\n")
    return r


def _hooks(fake, *, planner_model="plan/m", verifier_model="review/m", executor_model="exec/m",
           provider_name="fake", plan_holder=None):
    """Planner and verifier hooks the way the CLI builds them."""
    planner_choice = roles.RoleModelChoice("planner", planner_model, "explicit", planner_model != executor_model)
    verifier_choice = roles.RoleModelChoice("verifier", verifier_model, "explicit", verifier_model != executor_model)
    usage: list = []

    def planner(sandbox, repo_files):
        plan, role, u = roles.run_planner_turns(
            fake, planner_model, task=TASK, repo_root=sandbox.parent, sandbox=sandbox, repo_files=repo_files,
            choice=planner_choice, provider_name=provider_name,
        )
        usage.extend(u)
        if plan_holder is not None:
            plan_holder.append(plan)
        return plan, role.to_record()

    def verifier(sandbox, changed, result, attempt):
        review, u, err = roles.run_verifier_call(
            fake, verifier_model, task=TASK, plan=plan_holder[-1] if plan_holder else None,
            diff_text="--- a\n+++ b\n-bad\n+ok", verification={"status": "passed", "exit_code": 0},
            changed_files=changed, attempt=attempt,
        )
        usage.extend(u)
        role = roles.RoleRun.from_usage("verifier", u, choice=verifier_choice, provider=provider_name,
                                        status=roles.STATUS_RAN if review else roles.STATUS_FAILED, reason=err)
        return review, role.to_record()

    return planner, verifier, usage


class TestRoleSelectionAndGating:
    def test_explicit_models_win_and_independence_is_recorded(self):
        c = roles.select_role_model("verifier", explicit="other/m", executor_model="exec/m", routing=None,
                                    provider_name="p")
        assert (c.model, c.source, c.independent) == ("other/m", "explicit", True)
        same = roles.select_role_model("verifier", explicit="exec/m", executor_model="exec/m", routing=None,
                                       provider_name="p")
        assert same.independent is False

    def test_fallback_to_role_tier_only_when_the_catalog_knows_it(self):
        known = roles.select_role_model("planner", explicit=None, executor_model="exec/m", routing=None,
                                        provider_name="p", catalog_knows=lambda m: True)
        assert known.source == "role_tier" and known.model and known.model != "exec/m"
        unknown = roles.select_role_model("verifier", explicit=None, executor_model="exec/m", routing=None,
                                          provider_name="p", catalog_knows=lambda m: False)
        assert (unknown.model, unknown.source, unknown.independent) == ("exec/m", "executor_model_reused", False)
        assert unknown.reason == roles.SKIP_NO_INDEPENDENT_MODEL
        # No catalog answer at all: the static tier is not assumed to be routable.
        no_catalog = roles.select_role_model("verifier", explicit=None, executor_model="exec/m", routing=None,
                                             provider_name="p", catalog_knows=None)
        assert no_catalog.source == "executor_model_reused" and no_catalog.independent is False

    def test_a_verifier_never_silently_reuses_the_executor_via_the_tier(self):
        from openshard.native.dispatch import resolve_role

        tier_model = resolve_role("validator")[0]
        c = roles.select_role_model("verifier", explicit=None, executor_model=tier_model, routing=None,
                                    provider_name="p", catalog_knows=lambda m: True)
        assert c.source == "executor_model_reused" and c.independent is False

    def test_planner_and_verifier_gating(self):
        assert roles.planner_wanted("executor", task_category="complex", repo_file_count=500) == (
            False, roles.SKIP_ROLES_EXECUTOR_ONLY)
        assert roles.planner_wanted("full", task_category="standard", repo_file_count=1) == (True, None)
        assert roles.planner_wanted("auto", task_category="standard", repo_file_count=3) == (False, roles.SKIP_TASK_TRIVIAL)
        assert roles.planner_wanted("auto", task_category="security", repo_file_count=3) == (True, None)
        assert roles.planner_wanted("auto", task_category="standard", repo_file_count=40) == (True, None)
        dependent = roles.RoleModelChoice("verifier", "exec/m", "executor_model_reused", False)
        independent = roles.RoleModelChoice("verifier", "other/m", "adaptive_routing_v2", True)
        big = {"task_category": "standard", "repo_file_count": 40}
        assert roles.verifier_wanted("auto", dependent, **big) == (False, roles.SKIP_NO_INDEPENDENT_MODEL)
        assert roles.verifier_wanted("auto", independent, **big) == (True, None)
        assert roles.verifier_wanted("auto", independent, task_category="standard", repo_file_count=2) == (
            False, roles.SKIP_TASK_TRIVIAL)  # a trivial task pays for no review either
        assert roles.verifier_wanted("auto", independent, task_category="security", repo_file_count=2) == (True, None)
        assert roles.verifier_wanted("full", dependent) == (True, None)
        assert roles.verifier_wanted("executor", independent, **big) == (False, roles.SKIP_ROLES_EXECUTOR_ONLY)

    def test_plan_and_review_parsing_are_bounded_and_fail_closed(self):
        assert roles.parse_plan("nope") is None and roles.parse_plan({}) is None
        plan = roles.parse_plan({"summary": "s" * 1000, "files": ["a.py", "../x", "C:/y"], "steps": ["x"] * 20,
                                 "verification": "not a list", "simple": "yes"})
        assert len(plan["summary"]) == 300 and plan["files"] == ["a.py"] and len(plan["steps"]) == 8
        assert plan["verification"] == [] and plan["simple"] is None
        with pytest.raises(roles.ReviewParseError):
            roles.parse_review('{"verdict": "maybe"}')
        with pytest.raises(roles.ReviewParseError):
            roles.parse_review("[]")
        r = roles.parse_review('```json\n{"verdict": "WARN", "summary": "ok", "concerns": ["a", 2, "b"]}\n```')
        assert r == {"verdict": "warn", "summary": "ok", "concerns": ["a", "b"]}


class TestPlannerRole:
    def test_planner_reads_then_plans_and_cannot_write(self, repo):
        fake = FakeModel([
            _turn(_a("read_file", path="src/app.txt"), _a("write_file", path="src/app.txt", content="hack"),
                  _a("run_verification")),
            json.dumps({"plan": PLAN}),
        ])
        from openshard.osn.loop import create_isolated_copy

        sandbox = create_isolated_copy(repo)
        plan, role, usage = roles.run_planner_turns(
            fake, "plan/m", task=TASK, repo_root=repo, sandbox=sandbox, repo_files=["src/app.txt", "README.md"],
            choice=roles.RoleModelChoice("planner", "plan/m", "explicit", True), provider_name="fake",
        )
        assert plan["summary"] == PLAN["summary"] and plan["files"] == ["src/app.txt"]
        assert role.status == "ran" and role.turns == 2 and role.calls == 2 and role.model == "plan/m"
        assert role.cost_usd == pytest.approx(0.002) and role.cost_source == "provider_reported"
        kinds = [(a["kind"], a["decision"], a["executed"]) for a in role.actions]
        assert kinds[0] == ("read_file", "allow", True)
        assert kinds[1] == ("write_file", "invalid", False) and kinds[2] == ("run_verification", "invalid", False)
        assert (sandbox / "src" / "app.txt").read_text() == "bad"  # read-only role: nothing changed
        assert all(u.role == "planner" and u.attempt == 0 for u in usage)
        assert fake.calls[0][1].startswith("You are the planning role")
        assert "read-only" in fake.calls[1][2]  # the refusal was reported back to the planner

    def test_planner_without_a_plan_is_recorded_as_failed(self, repo):
        fake = FakeModel([_turn(_a("finish", intent="nothing to plan"))])
        from openshard.osn.loop import create_isolated_copy

        plan, role, _ = roles.run_planner_turns(fake, "plan/m", task=TASK, repo_root=repo,
                                                sandbox=create_isolated_copy(repo), repo_files=[])
        assert plan is None and role.status == "failed" and role.reason == "no_plan_returned"


class TestVerifierRole:
    def test_verifier_parses_a_verdict_with_one_reask_and_records_usage(self):
        fake = FakeModel(["not json", json.dumps({"verdict": "pass", "summary": "fine", "concerns": []})])
        review, usage, err = roles.run_verifier_call(
            fake, "review/m", task=TASK, plan=PLAN, diff_text="+ok", verification={"status": "passed", "exit_code": 0},
            changed_files=["src/app.txt"], attempt=1,
        )
        assert review["verdict"] == "pass" and err is None and len(usage) == 2
        assert all(u.role == "verifier" and u.attempt == 1 and u.turn == 1 for u in usage)
        assert "<untrusted kind=\"diff\">" in fake.calls[0][2] and "Plan from the planning role" in fake.calls[0][2]

    def test_verifier_failures_are_reported_not_raised(self):
        fake = FakeModel([RuntimeError("down")])
        review, usage, err = roles.run_verifier_call(
            fake, "review/m", task=TASK, plan=None, diff_text="", verification={}, changed_files=[], attempt=1,
        )
        assert review is None and err.startswith("provider_error") and usage == []
        fake = FakeModel(["x", "y"])
        review, usage, err = roles.run_verifier_call(
            fake, "review/m", task=TASK, plan=None, diff_text="", verification={}, changed_files=[], attempt=1,
        )
        assert review is None and err == "malformed_review" and len(usage) == 2


class TestRolesInTheLoop:
    def _provider(self, fake, model="exec/m"):
        return IterativeModelProvider(fake, [model], None)

    def test_plan_reaches_the_executor_and_a_passing_review_is_recorded(self, repo):
        fake = FakeModel([
            json.dumps({"plan": PLAN}),                                                    # planner
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),       # executor
            json.dumps({"verdict": "pass", "summary": "does the task", "concerns": []}),    # verifier
        ])
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        plans: list = []
        planner, verifier, usage = _hooks(fake, plan_holder=plans)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=planner, verifier=verifier)
        assert rec.status == "verified" and rec.review_verdict == "pass"
        assert [c[0] for c in fake.calls] == ["plan/m", "exec/m", "review/m"]
        assert "Plan from the planning role" in fake.calls[1][2] and PLAN["summary"] in fake.calls[1][2]
        assert rec.plan["summary"] == PLAN["summary"]
        assert rec.roles["planner"]["status"] == "ran" and rec.roles["verifier"]["status"] == "ran"
        assert rec.reviews[0]["evidence"] == "model_reported" and rec.reviews[0]["recovery_requested"] is False
        assert len(rec.attempts) == 1

    def test_failed_review_buys_one_recovery_attempt_that_is_verified_again(self, repo):
        fake = FakeModel([
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
            json.dumps({"verdict": "fail", "summary": "README not updated", "concerns": ["update README"]}),
            _turn(_a("write_file", path="README.md", content="# demo\nok\n"), _a("finish")),
            json.dumps({"verdict": "pass", "summary": "now complete", "concerns": []}),
        ])
        provider = IterativeModelProvider(fake, ["exec/m", "strong/m"], repo)
        _, verifier, usage = _hooks(fake)
        events: list = []
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=3, verifier=verifier,
                               progress=lambda e, d: events.append((e, d)))
        assert rec.status == "verified" and rec.stop_reason == "verification_passed"
        assert [c[0] for c in fake.calls] == ["exec/m", "review/m", "exec/m", "review/m"]  # same model, no escalation
        assert "raised concerns" in fake.calls[2][2] and "update README" in fake.calls[2][2]
        assert [r["verdict"] for r in rec.reviews] == ["fail", "pass"]
        assert rec.reviews[0]["recovery_requested"] is True and rec.reviews[0]["recovery_outcome"] == "verified"
        assert [a.n for a in rec.attempts] == [1, 2] and rec.attempts[1].review_recovery is True
        assert rec.attempts[1].verification.passed and rec.changed_files == ["src/app.txt", "README.md"]
        assert any(e == "recovery_decision" and d["action"] == "review_recovery" for e, d in events)

    def test_recovery_that_breaks_verification_is_reverted_and_the_verified_state_stands(self, repo):
        fake = FakeModel([
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
            json.dumps({"verdict": "fail", "summary": "wrong", "concerns": ["rename"]}),
            _turn(_a("write_file", path="src/app.txt", content="broken"),
                  _a("write_file", path="new.txt", content="x"), _a("finish")),
        ])
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        _, verifier, usage = _hooks(fake)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=3, verifier=verifier)
        assert rec.status == "verified" and rec.verification_state == "passed"
        assert rec.reviews[0]["recovery_outcome"] == "reverted_to_verified_state"
        sandbox = __import__("pathlib").Path(rec.sandbox_path)
        assert (sandbox / "src" / "app.txt").read_text() == "ok" and not (sandbox / "new.txt").exists()
        assert rec.changed_files == ["src/app.txt"] and len(rec.attempts) == 2
        assert rec.attempts[1].verification.passed is False and rec.attempts[1].review_recovery
        assert rec.verified_file_hashes["src/app.txt"] == __import__("hashlib").sha256(b"ok").hexdigest()

    def test_reviews_are_bounded_and_a_no_change_recovery_stops(self, repo):
        fake = FakeModel([
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
            json.dumps({"verdict": "fail", "summary": "x", "concerns": ["y"]}),
            _turn(_a("finish", intent="the concern is not valid")),
        ])
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        _, verifier, _ = _hooks(fake)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=3, verifier=verifier)
        assert rec.status == "verified" and len(rec.reviews) == 1
        assert rec.reviews[0]["recovery_outcome"] == "no_change" and len(fake.replies) == 0

    def test_review_budget_stop_skips_the_review_and_keeps_the_verified_result(self, repo):
        fake = FakeModel([_turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish"))])
        ledger = BudgetLedger(BudgetLimits(max_spend_usd=0.0005))  # the executor's one call already exceeds it
        provider = IterativeModelProvider(fake, ["exec/m"], repo, budget=ledger)

        def verifier(sandbox, changed, result, attempt):
            ledger.before_model_call()  # raises: spend already over the cap, so no review call is made
            raise AssertionError("unreachable")

        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=1, verifier=verifier, budget=ledger)
        assert rec.status == "verified"
        assert rec.roles["verifier"]["status"] == "skipped" and rec.roles["verifier"]["reason"].startswith("budget_")

    def test_planner_budget_stop_is_recorded_and_the_run_goes_on(self, repo):
        fake = FakeModel([_turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish"))])
        from openshard.osn.budget import BudgetExhausted

        def planner(sandbox, files):
            raise BudgetExhausted("max_spend_usd", "stopped_before_model_call")

        rec = run_bounded_loop(repo, TASK, IterativeModelProvider(fake, ["exec/m"], repo), CHECK, planner=planner)
        assert rec.status == "verified" and rec.roles["planner"]["status"] == "skipped"
        assert rec.plan is None


class TestRoleReceipt:
    def _run(self, repo):
        fake = FakeModel([
            json.dumps({"plan": PLAN}),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
            json.dumps({"verdict": "warn", "summary": "fine but check docs", "concerns": ["docs"]}),
        ])
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        plans: list = []
        planner, verifier, usage = _hooks(fake, plan_holder=plans)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=planner, verifier=verifier)
        all_usage = [*usage, *provider.usage]
        entry = build_osn_run_entry(rec, task=TASK, usage=all_usage, duration_seconds=3.0, repo_path=repo)
        return rec, entry

    def test_entry_names_every_role_model_usage_and_cost_and_keeps_the_executor_as_the_run_model(self, repo):
        rec, entry = self._run(repo)
        assert entry["execution_model"] == "exec/m"  # never the planner's or verifier's
        r = entry["osn_loop"]["roles"]
        assert r["planner"]["model"] == "plan/m" and r["planner"]["status"] == "ran"
        assert r["executor"]["model"] == "exec/m" and r["executor"]["calls"] == 1 and r["executor"]["turns"] == 1
        assert r["verifier"]["model"] == "review/m" and r["verifier"]["independent"] is True
        for role in ("planner", "executor", "verifier"):
            assert r[role]["cost_source"] == "provider_reported" and r[role]["cost_usd"] == pytest.approx(0.001)
            assert r[role]["total_tokens"] == 60 and r[role]["duration_ms"] is not None
        assert [c["role"] for c in entry["osn_loop"]["model_calls"]] == ["planner", "executor", "verifier"]
        assert entry["estimated_cost"] == pytest.approx(0.003) and entry["cost_provenance"] == "provider_reported"
        stages = [(s["stage_type"], s["model"]) for s in entry["stage_runs"]]
        assert stages == [("planning", "plan/m"), ("implementation", "exec/m"), ("review", "review/m")]
        tdr = entry["tier_dispatch_receipt"]
        assert tdr["planner_model_actual"] == "plan/m" and tdr["validator_model_actual"] == "review/m"
        assert tdr["executor_model_actual"] == "exec/m" and tdr["validator_dispatch_status"] == "applied"
        truth = build_routing_truth(entry)
        assert (truth.planner_dispatched, truth.executor_dispatched, truth.validator_dispatched) == (True, True, True)
        assert entry["osn_loop"]["reviews"][0]["verdict"] == "warn"
        assert entry["verification"]["status"] == "passed" and entry["verification"]["source"] == "directly_observed"

    def test_skipped_roles_say_skipped_and_why(self, repo):
        fake = FakeModel([_turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish"))])
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=1)
        rec.roles["planner"] = roles.RoleRun.skipped("planner", roles.SKIP_TASK_TRIVIAL).to_record()
        rec.roles["verifier"] = roles.RoleRun.skipped(
            "verifier", roles.SKIP_NO_INDEPENDENT_MODEL,
            roles.RoleModelChoice("verifier", "exec/m", "executor_model_reused", False),
        ).to_record()
        entry = build_osn_run_entry(rec, task=TASK, usage=provider.usage, duration_seconds=1.0, repo_path=repo)
        r = entry["osn_loop"]["roles"]
        assert r["planner"] == {**r["planner"], "status": "skipped", "reason": "task_trivial"}
        assert r["verifier"]["status"] == "skipped" and r["verifier"]["independent"] is False
        assert "stage_runs" not in entry  # one real stage is just the execution model
        assert entry["tier_dispatch_receipt"]["planner_model_actual"] is None
        assert entry["tier_dispatch_receipt"]["validator_dispatch_status"] == "skipped"
        truth = build_routing_truth(entry)
        assert truth.executor_dispatched and not truth.planner_dispatched and not truth.validator_dispatched

    def test_receipt_surfaces_show_roles_plan_and_review_but_the_hosted_payload_is_unchanged(self, repo):
        _, entry = self._run(repo)
        block = ev.agent_loop_block(entry)
        assert block["roles"]["verifier"]["independent"] is True and block["plan"]["step_count"] == 3
        assert block["reviews"][0]["verdict"] == "warn" and block["reviews"][0]["evidence"] == "model_reported"
        assert block["reviews"][0]["concern_count"] == 1 and "concerns" not in block["reviews"][0]
        # Paths stay out of the role / call / review evidence (the plan summary is the planner's own text).
        assert "src/app.txt" not in json.dumps({k: v for k, v in block.items() if k != "plan"})
        receipt = build_shard_receipt(entry, index=0)
        full = render_full_shard_receipt(receipt)
        assert "ROLES" in full and "Planner" in full and "Verifier" in full and "independent" in full
        assert "PLAN" in full and PLAN["summary"] in full
        assert "REVIEW" in full and "WARN (model-reported" in full
        assert "A review never changes the verification result" in full
        compact = render_compact_shard_receipt(receipt)
        assert "ROLES" in compact and "Verifier" in compact
        hosted = receipt_to_dict(receipt, extended=True)
        assert set(hosted["execution_loop"]) == {"status", "stop_reason", "verification_state", "attempts", "evidence"}
        assert "roles" not in hosted and "agent_loop" not in hosted
        assert [s["stage"] for s in hosted["model_stages"]] == ["Planning", "Execution", "Review"]


class TestRolesViaCli:
    def test_full_roles_run_through_the_cli(self, tmp_path, monkeypatch):
        import subprocess

        from click.testing import CliRunner

        from openshard.cli.main import cli

        repo = tmp_path / "proj"
        (repo / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "src" / "app.txt").write_text("bad")
        monkeypatch.chdir(repo)
        fake = FakeModel([
            json.dumps({"plan": PLAN}),
            json.dumps({"writes": [{"path": "src/app.txt", "content": "ok"}]}),
            json.dumps({"verdict": "pass", "summary": "ok", "concerns": []}),
        ])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        verify = f'"{PY}" -c "import sys; sys.exit(0 if open(\'src/app.txt\').read()==\'ok\' else 1)"'
        r = CliRunner().invoke(cli, [
            "osn", "run", TASK, "--model", "exec/m", "--verify-cmd", verify, "--roles", "full",
            "--planner-model", "plan/m", "--verifier-model", "review/m", "--json",
        ])
        assert r.exit_code == 0, r.output
        body = json.loads(r.output)
        assert body["status"] == "verified"
        assert [c[0] for c in fake.calls] == ["plan/m", "exec/m", "review/m"]
        assert body["roles"]["planner"]["status"] == "ran" and body["roles"]["verifier"]["independent"] is True
        assert body["reviews"][0]["verdict"] == "pass" and body["plan"]["summary"] == PLAN["summary"]
        assert [c["role"] for c in body["model_calls"]] == ["planner", "executor", "verifier"]
        runs = [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()]
        assert runs[-1]["execution_model"] == "exec/m" and len(runs[-1]["stage_runs"]) == 3

    def test_executor_only_skips_both_roles_with_reasons(self, tmp_path, monkeypatch):
        import subprocess

        from click.testing import CliRunner

        from openshard.cli.main import cli

        repo = tmp_path / "proj"
        (repo / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "src" / "app.txt").write_text("bad")
        monkeypatch.chdir(repo)
        fake = FakeModel([json.dumps({"writes": [{"path": "src/app.txt", "content": "ok"}]})])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        verify = f'"{PY}" -c "import sys; sys.exit(0 if open(\'src/app.txt\').read()==\'ok\' else 1)"'
        r = CliRunner().invoke(cli, ["osn", "run", TASK, "--model", "exec/m", "--verify-cmd", verify,
                                     "--roles", "executor", "--json"])
        assert r.exit_code == 0, r.output
        body = json.loads(r.output)
        assert body["status"] == "verified" and len(fake.calls) == 1
        assert body["roles"]["planner"]["status"] == "skipped"
        assert body["roles"]["planner"]["reason"] == "roles_executor_only"
        assert body["roles"]["verifier"]["status"] == "skipped" and body["reviews"] == []
