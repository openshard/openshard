"""Parallel writing workers: decomposition, topology decision, scoped authority, synthesis, Receipt evidence."""
from __future__ import annotations

import json
import sys
import threading

import pytest

from openshard.history.receipt_evidence import (
    economics_block,
    synthesis_block,
    topology_block,
    workers_block,
)
from openshard.osn import roles
from openshard.osn.decompose import (
    Subtask,
    decomposition_from_plan,
    scopes_overlap,
    validate_decomposition,
)
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import IterativeModelProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.osn.synthesis import resolution_advisory, synthesize
from openshard.osn.topology import (
    REASON_BUDGET,
    REASON_DECOMPOSITION_INVALID,
    REASON_PARALLEL_SELECTED,
    REASON_TASK_SIMPLE,
    REASON_USER_SINGLE,
    TOPOLOGY_PARALLEL_SUBTASKS,
    TOPOLOGY_PLANNER_EXECUTOR_VERIFIER,
    TOPOLOGY_SINGLE,
    decide_topology,
)
from openshard.osn.workers import ScopedFileMutationGate, WorkerResult, WorkerSpec, run_workers
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = [PY, "-c",
         "import sys; sys.exit(0 if open('src/a.txt').read()=='A' and open('src/b.txt').read()=='B' else 1)"]
TASK = "make src/a.txt contain A and src/b.txt contain B"


def _turn(*actions, note=""):
    return json.dumps({"actions": list(actions), "note": note})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


def _subtask(sid, path, **kw):
    return {"id": sid, "objective": f"write {path}", "allowed_write_paths": [path], "parallel_safe": True,
            "required": True, **kw}


PLAN = {"summary": "two independent files", "files": ["src/a.txt", "src/b.txt"], "steps": ["write both"],
        "verification": ["the check exits 0"], "simple": False,
        "subtasks": [_subtask("a", "src/a.txt"), _subtask("b", "src/b.txt", preferred_capability="deep_reasoning")]}


class FakeByModel(BaseProvider):
    """Replies scripted per model id, so concurrent workers consume their own queues."""

    def __init__(self, replies: dict[str, list], cost=0.002):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, str, str]] = []
        self.cost = cost
        self._lock = threading.Lock()

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        with self._lock:
            self.calls.append((model, system or "", prompt))
            queue = self.replies.get(model) or []
            if not queue:
                raise RuntimeError(f"no scripted reply left for {model}")
            content = queue.pop(0)
        if isinstance(content, Exception):
            raise content
        return ChatResponse(content, model, UsageStats(40, 10, 50, self.cost, cost_source="provider_reported"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.txt").write_bytes(b"old")
    (r / "src" / "b.txt").write_bytes(b"old")
    (r / "README.md").write_bytes(b"# demo\n")
    return r


def _ok_verify(sandbox, paths):
    from openshard.osn.loop import _observe_verification

    return _observe_verification(sandbox, paths, CHECK, 60.0, None)


class TestDecomposition:
    def test_valid_decomposition_from_a_plan(self):
        d = decomposition_from_plan(PLAN)
        assert d is not None and d.valid and [s.id for s in d.parallel_subtasks] == ["a", "b"]
        assert d.subtasks[1].preferred_capability == "deep_reasoning"
        assert decomposition_from_plan({"summary": "x"}) is None

    def test_overlapping_scopes_cycles_and_too_many_are_rejected(self):
        assert scopes_overlap("src/", "src/x.py") and scopes_overlap("src/**", "src/a/b.py")
        assert not scopes_overlap("src/a.txt", "src/b.txt")
        overlap = validate_decomposition([Subtask("a", "o", ("src/",)), Subtask("b", "o", ("src/x.py",))])
        assert not overlap.valid and any("overlap" in r for r in overlap.reasons)
        cycle = validate_decomposition([Subtask("a", "o", ("x",), dependencies=("b",)),
                                        Subtask("b", "o", ("y",), dependencies=("a",))])
        assert not cycle.valid and any("cycl" in r for r in cycle.reasons)
        many = validate_decomposition([Subtask(f"s{i}", "o", (f"f{i}",)) for i in range(4)])
        assert not many.valid

    def test_unsafe_scopes_are_rejected(self):
        d = validate_decomposition([Subtask("a", "o", ("**",)), Subtask("b", "o", ("../x",))])
        assert not d.valid and len(d.reasons) >= 1


class TestTopologyDecision:
    def _dec(self):
        return decomposition_from_plan(PLAN)

    def test_user_single_wins_and_auto_needs_a_complex_task(self):
        single = decide_topology("single", planner_ran=True, verifier_wanted=True, decomposition=self._dec(),
                                 task_complex=True, budget_headroom=None, distinct_models_available=2)
        assert single.selected == TOPOLOGY_SINGLE and single.reason == REASON_USER_SINGLE
        simple = decide_topology("auto", planner_ran=True, verifier_wanted=True, decomposition=self._dec(),
                                 task_complex=False, budget_headroom=None, distinct_models_available=2)
        assert simple.selected == TOPOLOGY_PLANNER_EXECUTOR_VERIFIER and REASON_TASK_SIMPLE in simple.notes

    def test_parallel_when_evidence_justifies_it_with_expected_cost(self):
        d = decide_topology("auto", planner_ran=True, verifier_wanted=True, decomposition=self._dec(),
                            task_complex=True, budget_headroom=True, distinct_models_available=2,
                            planner_cost_usd=0.01)
        assert d.selected == TOPOLOGY_PARALLEL_SUBTASKS and d.reason == REASON_PARALLEL_SELECTED
        assert d.worker_count == 2 and d.expected_extra_cost_usd == pytest.approx(0.02)
        rec = d.to_record()
        assert rec["topology_requested"] == "auto" and rec["expected_extra_cost_basis"]
        unknown = decide_topology("parallel", planner_ran=True, verifier_wanted=False, decomposition=self._dec(),
                                  task_complex=False, budget_headroom=None, distinct_models_available=1)
        assert unknown.selected == TOPOLOGY_PARALLEL_SUBTASKS and unknown.expected_extra_cost_usd is None

    def test_budget_and_invalid_decomposition_fall_back_with_the_reason(self):
        bad = validate_decomposition([Subtask("a", "o", ("src/",)), Subtask("b", "o", ("src/x",))])
        d = decide_topology("parallel", planner_ran=True, verifier_wanted=False, decomposition=bad,
                            task_complex=True, budget_headroom=True, distinct_models_available=2)
        assert d.selected != TOPOLOGY_PARALLEL_SUBTASKS and d.reason == REASON_DECOMPOSITION_INVALID
        d = decide_topology("parallel", planner_ran=True, verifier_wanted=False, decomposition=self._dec(),
                            task_complex=True, budget_headroom=False, distinct_models_available=2)
        assert d.reason == REASON_BUDGET and d.worker_count == 0

    def test_worker_count_is_capped_at_three(self):
        dec = validate_decomposition([Subtask(f"s{i}", "o", (f"f{i}.txt",)) for i in range(3)])
        d = decide_topology("parallel", planner_ran=True, verifier_wanted=False, decomposition=dec,
                            task_complex=True, budget_headroom=None, distinct_models_available=1, max_workers=9)
        assert d.worker_count == 3 and d.max_workers == 3


class TestScopedAuthority:
    def test_writes_outside_the_subtask_scope_are_denied(self):
        gate = ScopedFileMutationGate(allowed_patterns=("src/a.txt",), worker_id="worker-1")
        assert gate.authorize("src/a.txt") is True
        assert gate.authorize("src/b.txt") is False
        outside = gate.outcomes[-1]
        assert outside.decision == "deny" and outside.policy.source == "subtask_scope"
        blocked = ScopedFileMutationGate(blocked_patterns=("src/**",), allowed_patterns=("src/a.txt",))
        assert blocked.authorize("src/a.txt") is False  # policy still wins over scope


class TestWorkersAndSynthesis:
    def _specs(self):
        d = decomposition_from_plan(PLAN)
        assert d is not None
        return [WorkerSpec("worker-1", d.subtasks[0], "fast/m", provider_name="fake"),
                WorkerSpec("worker-2", d.subtasks[1], "deep/m", provider_name="fake")]

    def test_workers_write_in_their_own_copies_and_synthesis_applies_disjoint_files(self, repo, tmp_path):
        fake = FakeByModel({
            "fast/m": [_turn(_a("write_file", path="src/a.txt", content="A"), _a("run_verification")),
                       _turn(_a("finish"), note="a done")],
            "deep/m": [_turn(_a("write_file", path="src/b.txt", content="B"), _a("finish"), note="b done")],
        })
        main = tmp_path / "main"
        import shutil

        shutil.copytree(repo, main)
        results, usage = run_workers(self._specs(), provider=fake, task=TASK, plan=PLAN, repo_root=repo,
                                     base_sandbox=main, verify=_ok_verify)
        assert [r.status for r in results] == ["changed", "changed"]
        assert {r.model for r in results} == {"fast/m", "deep/m"}
        assert (main / "src" / "a.txt").read_bytes() == b"old"  # nothing touches the main copy before synthesis
        assert all(u.role == roles.ROLE_WORKER for u in usage) and {u.worker_id for u in usage} == {"worker-1", "worker-2"}
        assert [r.cost_usd for r in results] == [pytest.approx(0.004), pytest.approx(0.002)]  # per call, summed
        assert all(r.cost_source == "provider_reported" for r in results) and [r.calls for r in results] == [2, 1]
        # a worker's own-copy verification is informational: the check needs both files, so it fails there
        w1, w2 = results
        assert w1.verification["status"] == "failed" and w1.verification["scope"] == "worker_copy_informational"
        assert w1.turns == 2 and w2.verification is None  # worker-2 never asked for a check
        synth = synthesize(results, main_sandbox=main, scopes={"worker-1": ("src/a.txt",), "worker-2": ("src/b.txt",)})
        assert synth.applied == ["src/a.txt", "src/b.txt"] and not synth.needs_resolution
        assert (main / "src" / "a.txt").read_bytes() == b"A" and (main / "src" / "b.txt").read_bytes() == b"B"
        rec = synth.to_record()
        assert rec["resolution"] == "none_needed" and rec["evidence"]["file_effects"] == "openshard_observed"
        assert (repo / "src" / "a.txt").read_bytes() == b"old"  # the repo itself is never written

    def test_a_worker_cannot_write_outside_its_scope(self, repo, tmp_path):
        fake = FakeByModel({
            "fast/m": [_turn(_a("write_file", path="src/b.txt", content="B"), _a("finish"))],
            "deep/m": [_turn(_a("write_file", path="src/b.txt", content="B"), _a("finish"))],
        })
        import shutil

        main = tmp_path / "main"
        shutil.copytree(repo, main)
        results, _ = run_workers(self._specs(), provider=fake, task=TASK, plan=PLAN, repo_root=repo,
                                 base_sandbox=main, verify=_ok_verify)
        w1 = results[0]
        assert w1.status == "blocked" and "src/b.txt" in w1.blocked and not w1.changed_files
        assert any(d.get("source") == "subtask_scope" for d in w1.decisions)

    def test_conflicts_and_missing_required_work_go_to_resolution(self, tmp_path):
        main = tmp_path / "main"
        (main / "src").mkdir(parents=True)
        (main / "src" / "x.txt").write_bytes(b"base")
        w1 = tmp_path / "w1"
        w2 = tmp_path / "w2"
        for w, text in ((w1, b"one"), (w2, b"two")):
            (w / "src").mkdir(parents=True)
            (w / "src" / "x.txt").write_bytes(text)
        (w1 / "src" / "only.txt").write_bytes(b"ok")
        (w1 / "src" / "outside.txt").write_bytes(b"no")
        results = [
            WorkerResult("worker-1", "a", "changed", sandbox_path=str(w1),
                         changed_files=["src/x.txt", "src/only.txt", "src/outside.txt"]),
            WorkerResult("worker-2", "b", "changed", sandbox_path=str(w2), changed_files=["src/x.txt"]),
            WorkerResult("worker-3", "c", "failed", reason="provider_error", required=True),
        ]
        synth = synthesize(results, main_sandbox=main,
                           scopes={"worker-1": ("src/x.txt", "src/only.txt"), "worker-2": ("src/x.txt",)})
        assert [c["path"] for c in synth.conflicts] == ["src/x.txt"]
        assert synth.applied == ["src/only.txt"] and (main / "src" / "x.txt").read_bytes() == b"base"
        assert synth.rejected[0]["path"] == "src/outside.txt" and synth.rejected[0]["reason"] == "rejected_out_of_scope"
        assert synth.missing_required[0]["worker_id"] == "worker-3" and synth.needs_resolution
        advisory = resolution_advisory(synth, results)
        assert "src/x.txt" in advisory and "worker-3" in advisory and synth.conflict_context
        assert synth.to_record()["resolution"] == "executor_turns"


class TestWorkersInTheLoop:
    def _planner(self, fake, model="plan/m"):
        choice = roles.RoleModelChoice("planner", model, "explicit", True)

        def planner(sandbox, repo_files):
            plan, role, u = roles.run_planner_turns(
                fake, model, task=TASK, repo_root=sandbox.parent, sandbox=sandbox, repo_files=repo_files,
                choice=choice, provider_name="fake", decompose=True,
            )
            planner.usage.extend(u)
            return plan, role.to_record()

        planner.usage = []
        return planner

    def _workers(self, fake, repo, *, exclude_models=(), progress=None):
        from openshard.osn.topology import decide_topology as _decide

        def hook(sandbox, plan, files):
            dec = decomposition_from_plan(plan)
            decision = _decide("auto", planner_ran=plan is not None, verifier_wanted=False, decomposition=dec,
                               task_complex=True, budget_headroom=None, distinct_models_available=2)
            record = decision.to_record()
            if decision.selected != TOPOLOGY_PARALLEL_SUBTASKS or dec is None:
                return {"topology": record, "ran": False}
            models = ["fast/m", "deep/m"]
            specs = [WorkerSpec(f"worker-{i + 1}", st, models[i], provider_name="fake")
                     for i, st in enumerate(dec.parallel_subtasks[: decision.worker_count])]
            results, usage = run_workers(specs, provider=fake, task=TASK, plan=plan, repo_root=repo,
                                         base_sandbox=sandbox, verify=_ok_verify, progress=progress)
            hook.usage.extend(usage)
            synth = synthesize(results, main_sandbox=sandbox,
                               scopes={s.worker_id: s.subtask.allowed_write_paths for s in specs})
            record["actual_extra_cost_usd"] = sum(r.cost_usd or 0 for r in results)
            record["distinct_models"] = len({s.model for s in specs})
            return {"topology": record, "ran": True, "workers": [r.to_record() for r in results],
                    "synthesis": synth.to_record(), "applied": list(synth.applied),
                    "blocked": [r["path"] for r in synth.rejected],
                    "decisions": [d for r in results for d in r.decisions],
                    "advisory": resolution_advisory(synth, results) if synth.needs_resolution else None}

        hook.usage = []
        return hook

    def test_planner_decomposition_runs_workers_and_the_run_verifies_without_executor_turns(self, repo):
        fake = FakeByModel({
            "plan/m": [json.dumps({"plan": PLAN})],
            "fast/m": [_turn(_a("write_file", path="src/a.txt", content="A"), _a("finish"))],
            "deep/m": [_turn(_a("write_file", path="src/b.txt", content="B"), _a("finish"))],
        })
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        planner = self._planner(fake)
        events: list = []
        progress = lambda kind, data: events.append((kind, data))  # noqa: E731
        workers = self._workers(fake, repo, progress=progress)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=planner, workers=workers,
                               progress=progress)
        assert rec.status == "verified", rec.stop_reason
        assert "PLANNER_DECOMPOSE" not in fake.calls[0][1] and "subtasks" in fake.calls[0][1]  # the note reached the planner
        assert not any(c[0] == "exec/m" for c in fake.calls)  # synthesis was clean: no executor turns
        assert rec.topology["topology_selected"] == "parallel_subtasks" and rec.topology["worker_count"] == 2
        assert [w["status"] for w in rec.workers] == ["changed", "changed"]
        assert {w["model"] for w in rec.workers} == {"fast/m", "deep/m"}
        assert rec.synthesis["resolution"] == "none_needed" and sorted(rec.changed_files) == ["src/a.txt", "src/b.txt"]
        assert rec.attempts[0].turns == 0 and rec.attempts[0].parallel_stage
        assert any(e[0] == "stage_start" and e[1]["stage"] == "workers" for e in events)
        assert any(e[0] == "stage_end" and e[1]["workers"] == 2 and e[1]["applied"] == 2 for e in events)
        # Live transparency: every event a worker emits says which worker it came
        # from, so a renderer can label concurrent workers' lines; the planner's
        # role_end carries the bounded plan the executor and workers were given.
        worker_events = [d for e, d in events if e in ("turn_start", "action", "turn_response")]
        assert worker_events and all(d.get("worker_id") in ("worker-1", "worker-2") for d in worker_events)
        assert {d["worker_id"] for d in worker_events} == {"worker-1", "worker-2"}
        assert all(d.get("subtask_id") in ("a", "b") for d in worker_events)
        planner_end = next(d for e, d in events if e == "role_end" and d["role"] == "planner")
        assert planner_end["has_plan"] and planner_end["plan_steps"] == PLAN["steps"]
        assert planner_end["plan_subtasks"] == ["a", "b"] and planner_end["plan_files"] == PLAN["files"]

        entry = build_osn_run_entry(rec, task=TASK, usage=[*planner.usage, *workers.usage], duration_seconds=2.0,
                                    repo_path=repo)
        loop = entry["osn_loop"]
        assert loop["topology"]["topology_selected"] == "parallel_subtasks"
        assert loop["roles"]["planner"]["status"] == "ran" and loop["roles"]["worker"]["calls"] == 2
        econ = loop["economics"]
        assert econ["cost_complete"] and econ["model_calls"] == 3
        assert econ["by_worker"] == {"worker-1": pytest.approx(0.002), "worker-2": pytest.approx(0.002)}
        assert econ["by_role"]["worker"] == pytest.approx(0.004) and econ["by_role"]["planner"] == pytest.approx(0.002)
        assert econ["cost_per_verified_success"] == pytest.approx(0.006)
        assert entry["estimated_cost"] == pytest.approx(0.006)  # planner + both workers: the whole run
        assert entry["total_tokens"] == 150 and entry["cost_provenance"] == "provider_reported"
        stage_models = [s["model"] for s in entry["stage_runs"]]
        assert stage_models == ["plan/m", "fast/m", "deep/m"]
        # Model truth: the implementation was the workers', never the planner's or a verifier's.
        assert entry["execution_model"] == "fast/m" and loop["implementation_models"] == ["fast/m", "deep/m"]
        assert loop["roles"]["executor"] == {**loop["roles"]["executor"], "status": "skipped",
                                             "reason": "workers_synthesised_cleanly", "calls": 0}
        tier = entry["tier_dispatch_receipt"]
        assert tier["executor_model_actual"] == "fast/m" and "2 parallel worker(s)" in tier["warnings"][0]
        assert not any(e[0] == "attempt_start" for e in events)  # no executor attempt was announced

    def test_a_conflict_hands_resolution_to_the_executor_which_is_verified(self, repo):
        plan = dict(PLAN, subtasks=[_subtask("a", "src/"), _subtask("b", "docs/")])
        fake = FakeByModel({
            "plan/m": [json.dumps({"plan": plan})],
            "fast/m": [_turn(_a("write_file", path="src/a.txt", content="A"), _a("finish"))],
            "deep/m": [RuntimeError("provider down")],
            "exec/m": [_turn(_a("write_file", path="src/b.txt", content="B"), _a("finish"))],
        })
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        events: list = []
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=self._planner(fake),
                               workers=self._workers(fake, repo),
                               progress=lambda kind, data: events.append((kind, data)))
        assert rec.status == "verified", rec.stop_reason
        kinds = [e[0] for e in events]
        assert kinds.index("stage_end") < kinds.index("attempt_start")  # the executor is announced after the stage
        assert next(e[1] for e in events if e[0] == "attempt_start")["after_workers"] is True
        assert [w["status"] for w in rec.workers] == ["changed", "failed"]
        assert rec.synthesis["missing_required"][0]["worker_id"] == "worker-2"
        assert rec.synthesis["resolution"] == "executor_turns"
        exec_calls = [c for c in fake.calls if c[0] == "exec/m"]
        assert exec_calls and "worker-2" in exec_calls[0][2]  # the advisory reached the executor
        assert sorted(rec.changed_files) == ["src/a.txt", "src/b.txt"]

    def test_no_decomposition_means_no_workers_and_the_reason_is_recorded(self, repo):
        plan = {k: v for k, v in PLAN.items() if k != "subtasks"}
        fake = FakeByModel({
            "plan/m": [json.dumps({"plan": plan})],
            "exec/m": [_turn(_a("write_file", path="src/a.txt", content="A"),
                             _a("write_file", path="src/b.txt", content="B"), _a("finish"))],
        })
        provider = IterativeModelProvider(fake, ["exec/m"], repo)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=self._planner(fake),
                               workers=self._workers(fake, repo))
        assert rec.status == "verified" and rec.workers == [] and rec.synthesis is None
        assert rec.topology["topology_selected"] == "planner_executor"
        entry = build_osn_run_entry(rec, task=TASK, usage=[*provider.usage], duration_seconds=1.0, repo_path=repo)
        assert entry["execution_model"] == "exec/m" and entry["osn_loop"]["roles"]["executor"]["status"] == "ran"
        assert "planner_proposed_no_decomposition" in rec.topology["notes"]


class TestReceiptProjection:
    def test_blocks_keep_counts_and_decisions_but_never_paths(self):
        topo = topology_block({"topology_requested": "auto", "topology_selected": "parallel_subtasks",
                               "topology_reason": "independent_subtasks_with_disjoint_scopes", "worker_count": 2,
                               "distinct_models": 2, "expected_extra_cost_usd": 0.02, "actual_extra_cost_usd": 0.004})
        assert topo["selected"] == "parallel_subtasks" and topo["worker_count"] == 2
        assert topology_block({"topology_selected": "made_up"})["selected"] is None
        workers = workers_block([{
            "worker_id": "worker-1", "subtask_id": "a", "status": "changed", "model": "fast/m",
            "changed_files": ["src/secret/a.txt"], "blocked": [], "turns": 1, "calls": 1, "cost_usd": 0.002,
            "cost_source": "provider_reported", "verification": {"status": "failed"}, "sandbox_path": "C:/tmp/x",
        }])
        assert workers[0]["files_changed"] == 1 and workers[0]["own_copy_verification"] == "failed"
        assert "secret" not in json.dumps(workers) and "sandbox_path" not in workers[0]
        synth = synthesis_block({"applied": ["src/a.txt"], "conflicts": [{"path": "x"}], "rejected": [],
                                 "workers_accepted": ["worker-1"], "workers_rejected": [], "missing_required": [],
                                 "resolution": "executor_turns"})
        assert synth == {"applied_count": 1, "conflict_count": 1, "rejected_count": 0, "workers_accepted": 1,
                         "workers_rejected": 0, "missing_required": 0, "resolution": "executor_turns"}
        assert "src/a.txt" not in json.dumps(synth)
        econ = economics_block({"total_cost_usd": 0.006, "cost_complete": True, "model_calls": 3, "verified": True,
                                "cost_per_verified_success": 0.006, "by_role": {"planner": 0.002, "worker": None},
                                "by_worker": {"worker-1": 0.002}, "by_attempt": {"1": 0.006}})
        assert econ["cost_per_verified_success"] == 0.006 and econ["by_role"]["worker"] is None
        assert topology_block(None) is None and workers_block([]) is None and economics_block({}) is None
