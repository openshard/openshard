"""Durable OSN run state: checkpoints at every loop boundary, and a resume that is safe or refused."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.receipt_evidence import resumed_block
from openshard.osn import checkpoint as ckpt
from openshard.osn.loop import create_isolated_copy, resume_state, run_bounded_loop
from openshard.osn.model_provider import IterativeModelProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = [PY, "-c", "import sys; sys.exit(0 if open('out.txt').read()=='ok' else 1)"]
TASK = "make out.txt contain ok"


def _turn(*actions, note=""):
    return json.dumps({"actions": list(actions), "note": note})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


class FakeModel(BaseProvider):
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
        if not self.replies:
            raise RuntimeError("no scripted reply left")
        content = self.replies.pop(0)
        if isinstance(content, BaseException):
            raise content
        return ChatResponse(content, model, UsageStats(30, 10, 40, self.cost, cost_source="provider_reported"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "out.txt").write_bytes(b"bad")
    (r / "README.md").write_bytes(b"# demo\n")
    return r


def _git_repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=r, check=True)
    (r / "out.txt").write_text("bad")
    return r


class TestCheckpointStore:
    def test_write_read_snapshot_restore_round_trip(self, repo, tmp_path):
        cp = ckpt.RunCheckpoint(run_id="osn-abc", task=TASK, verify_argv=CHECK, args={"max_attempts": 2},
                                repo=ckpt.repo_fingerprint(repo), models=["a/m", "b/m"])
        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        (sandbox / "out.txt").write_bytes(b"partial")
        (sandbox / "new.txt").write_bytes(b"new")
        cp.files = ckpt.snapshot_changed(sandbox, ["out.txt", "new.txt", "gone.txt"], repo, cp.run_id)
        cp.state = {"attempts": [], "changed": ["out.txt", "new.txt", "gone.txt"]}
        cp.phase = ckpt.PHASE_PLANNED
        path = ckpt.write_checkpoint(repo, cp)
        assert path.name == "checkpoint.json" and not list(path.parent.glob("*.tmp"))
        back = ckpt.read_checkpoint(repo, "osn-abc")
        assert back.run_id == "osn-abc" and back.phase == "planned" and back.pid and back.models == ["a/m", "b/m"]
        assert back.files["gone.txt"] is None and len(back.files["out.txt"]) == 64
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "out.txt").write_bytes(b"bad")
        (fresh / "gone.txt").write_bytes(b"x")
        restored = ckpt.restore_changed(repo, "osn-abc", back.files, fresh)
        assert sorted(restored) == ["gone.txt", "new.txt", "out.txt"]
        assert (fresh / "out.txt").read_bytes() == b"partial" and (fresh / "new.txt").read_bytes() == b"new"
        assert not (fresh / "gone.txt").exists()
        (ckpt.checkpoint_dir(repo, "osn-abc") / "files" / "new.txt").write_bytes(b"tampered")
        with pytest.raises(FileNotFoundError, match="checkpoint_files_missing"):
            ckpt.restore_changed(repo, "osn-abc", back.files, fresh)

    def test_unreadable_or_wrong_version_is_refused(self, repo):
        d = ckpt.checkpoint_dir(repo, "bad")
        d.mkdir(parents=True)
        (d / "checkpoint.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="checkpoint_unreadable"):
            ckpt.read_checkpoint(repo, "bad")
        (d / "checkpoint.json").write_text(json.dumps({"version": 99, "run_id": "bad"}), encoding="utf-8")
        with pytest.raises(ValueError, match="checkpoint_version_unsupported"):
            ckpt.read_checkpoint(repo, "bad")
        with pytest.raises(FileNotFoundError, match="checkpoint_missing"):
            ckpt.read_checkpoint(repo, "nope")

    def test_resumability_rules_in_order(self, repo):
        at_start = ckpt.repo_fingerprint(repo)

        def cp(**kw):
            base = dict(run_id="r", task=TASK, verify_argv=CHECK, args={}, repo=at_start,
                        models=["a/m"], phase=ckpt.PHASE_ATTEMPT_DONE, state={"attempts": [{}]}, pid=None)
            base.update(kw)
            return ckpt.RunCheckpoint(**base)

        assert ckpt.check_resumable(cp(), repo).ok
        assert ckpt.check_resumable(cp(status="completed", receipt_id="rcpt_x"), repo).reason == "run_already_completed"
        import os

        alive = cp(status="running", pid=os.getpid())
        assert ckpt.pid_alive(os.getpid()) is False  # our own pid is never "another process"
        assert ckpt.check_resumable(alive, repo).ok  # own pid: not alive-elsewhere
        assert ckpt.check_resumable(cp(pid=999999999), repo).ok  # a dead pid does not block
        (repo / "README.md").write_bytes(b"changed\n")
        assert ckpt.check_resumable(cp(), repo).reason == "repository_changed_since_run_started"
        fresh = cp(repo=ckpt.repo_fingerprint(repo))
        assert ckpt.check_resumable(fresh, repo, verify_argv=[PY, "-c", "other"]).reason == "verify_command_differs"
        assert ckpt.check_resumable(cp(repo=ckpt.repo_fingerprint(repo), phase="started", state={}), repo).reason == "nothing_to_resume"

    def test_budget_and_usage_round_trip(self):
        from openshard.osn.budget import BudgetLedger, BudgetLimits
        from openshard.osn.model_provider import AttemptUsage

        ledger = BudgetLedger(BudgetLimits(max_spend_usd=1.0))
        ledger.spend_usd, ledger.model_calls, ledger.attempts, ledger.writes = 0.42, 3, 1, 2
        counters = ckpt.budget_counters(ledger)
        fresh = BudgetLedger(BudgetLimits(max_spend_usd=1.0))
        assert ckpt.restore_budget(fresh, counters) and fresh.spend_usd == 0.42 and fresh.attempts == 1
        u = AttemptUsage(2, "a/m", 10, 5, 0.01, requested_model="a/m", turn=3, role="worker", cost_source="provider_reported")
        u.worker_id = "worker-2"  # type: ignore[attr-defined]
        back = ckpt.usage_from_record(ckpt.usage_to_record(u))
        assert (back.attempt, back.model, back.turn, back.role, back.cost_usd, back.worker_id) == (2, "a/m", 3, "worker", 0.01, "worker-2")


class TestLoopCheckpointAndResume:
    def test_the_loop_checkpoints_each_boundary_and_a_resume_continues_from_the_next_attempt(self, repo):
        phases: list[tuple[str, dict]] = []
        fake = FakeModel([
            _turn(_a("write_file", path="out.txt", content="wrong"), _a("finish")),  # attempt 1: fails verification
            _turn(_a("write_file", path="out.txt", content="ok"), _a("finish")),     # attempt 2: would pass
        ])
        provider = IterativeModelProvider(fake, ["a/m", "b/m"], repo)
        plan = {"summary": "write ok", "files": ["out.txt"], "steps": ["write"], "verification": ["check"], "simple": True}

        def planner(sandbox, files):
            return plan, {"role": "planner", "status": "ran", "model": "plan/m"}

        # Interrupt the first process right after attempt 1 was checkpointed.
        class Stop(BaseException):
            pass

        def checkpoint(phase, state):
            phases.append((phase, state))
            if phase == "attempt_done":
                raise Stop()

        with pytest.raises(Stop):
            run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=planner, checkpoint=checkpoint)
        assert [p for p, _ in phases] == ["planned", "attempt_done"]
        state = phases[-1][1]
        assert state["plan"] == plan and state["roles"]["planner"]["status"] == "ran"
        assert len(state["attempts"]) == 1 and state["attempts"][0]["verification"]["passed"] is False
        assert state["changed"] == ["out.txt"] and state["prev_failure"].startswith("verify command failed")

        # Second process: a fresh copy with the checkpointed file, the plan and attempt 1 restored.
        fresh = create_isolated_copy(repo)
        (fresh / "out.txt").write_bytes(b"wrong")
        later: list[str] = []
        provider2 = IterativeModelProvider(FakeModel([_turn(_a("write_file", path="out.txt", content="ok"), _a("finish"))]),
                                           ["a/m", "b/m"], repo)
        rec = run_bounded_loop(repo, TASK, provider2, CHECK, max_attempts=2, planner=planner, sandbox_path=fresh,
                               resume=state, checkpoint=lambda phase, s: later.append(phase))
        assert rec.status == "verified", rec.stop_reason
        assert [a.n for a in rec.attempts] == [1, 2] and rec.attempts[0].resumed and not rec.attempts[1].resumed
        assert rec.attempts[0].verification is not None and rec.attempts[0].verification.passed is False
        assert rec.plan == plan and rec.roles["planner"]["status"] == "ran"
        assert provider2.provider.calls[0][0] == "b/m"  # attempt 2 used the ladder's second rung, as planned
        assert "Plan from the planning role" in provider2.provider.calls[0][1]
        assert later == []  # no planner ran again, and a verified attempt completes the run: nothing more to save
        assert rec.resumed == {"attempts_restored": 1, "plan_restored": True, "topology_restored": False,
                               "files_restored": 1, "evidence": "checkpoint_recorded_by_openshard"}
        d = rec.to_dict()
        assert d["attempts"][0]["resumed_from_checkpoint"] is True and "resumed_from_checkpoint" not in d["attempts"][1]
        entry = build_osn_run_entry(rec, task=TASK, usage=[*provider.usage, *provider2.usage], duration_seconds=1.0,
                                    repo_path=repo)
        assert entry["retry_triggered"] is True and entry["execution_model"] == "b/m"
        assert entry["osn_loop"]["resumed"]["attempts_restored"] == 1

    def test_resume_state_is_plain_data(self, repo):
        s = resume_state(sandbox=repo, attempts=[], changed=["x"], prev_fingerprint=None, prev_failure=None,
                         blocked_seen=[], prev_actions=None, roles={}, plan=None, reviews=[], topology=None,
                         workers=[], synthesis=None)
        assert json.loads(json.dumps(s))["changed"] == ["x"]


class TestResumeCommand:
    def _invoke(self, repo, fake, extra):
        from click.testing import CliRunner as _R

        return _R().invoke(cli, extra, catch_exceptions=True)

    def test_interrupted_run_is_resumed_once_and_then_refused(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        verify = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'
        first = FakeModel([
            _turn(_a("write_file", path="out.txt", content="wrong"), _a("finish")),
            KeyboardInterrupt(),  # the user hits Ctrl-C as attempt 2 starts
        ])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", first))
        r = CliRunner().invoke(cli, ["osn", "run", TASK, "--model", "a/m", "--escalate-model", "b/m",
                                     "--verify-cmd", verify, "--roles", "executor", "--no-learning"])
        assert r.exit_code != 0 and "Resume with: openshard osn resume osn-" in r.output, r.output
        runs = ckpt.list_checkpoints(repo)
        assert len(runs) == 1
        cp = runs[0]
        assert cp.status == "interrupted" and cp.phase == "attempt_done" and cp.attempts_done == 1
        assert cp.interrupted["reason"] == "keyboard_interrupt" and cp.models == ["a/m", "b/m"]
        assert len(cp.usage) == 1 and cp.files["out.txt"] and not (repo / ".openshard" / "runs.jsonl").exists()

        listing = CliRunner().invoke(cli, ["osn", "runs"])
        assert cp.run_id in listing.output and "resumable" in listing.output and "not resumable" not in listing.output

        second = FakeModel([_turn(_a("write_file", path="out.txt", content="ok"), _a("finish"))])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", second))
        r2 = CliRunner().invoke(cli, ["osn", "resume", cp.run_id, "--json"])
        assert r2.exit_code == 0, r2.output
        body = json.loads(r2.output)
        assert body["status"] == "verified" and body["attempts"] == 2
        assert body["resumed"]["attempts_restored"] == 1 and body["resumed"]["prior_model_calls"] == 1
        assert body["resumed"]["prior_cost_usd"] == pytest.approx(0.001)
        assert body["resumed"]["unsaved_progress_discarded"] is True and body["checkpoint"]["status"] == "completed"
        assert body["resumed"]["checkpoint_status"] == "interrupted"  # Ctrl-C was recorded; a crash says 'running'
        assert second.calls[0][0] == "b/m"  # the ladder's second rung, exactly as the interrupted run planned
        entry = json.loads((repo / ".openshard" / "runs.jsonl").read_text().splitlines()[-1])
        assert entry["osn_loop"]["attempts"][0]["resumed_from_checkpoint"] is True
        assert entry["estimated_cost"] == pytest.approx(0.001)  # attempt 1: the interrupted process's call
        assert entry["retry_estimated_cost"] == pytest.approx(0.001)  # attempt 2: this process's call
        assert entry["execution_model"] == "b/m"
        done = ckpt.read_checkpoint(repo, cp.run_id)
        assert done.status == "completed" and done.receipt_id == entry["receipt_id"] and done.files == {}
        assert not (ckpt.checkpoint_dir(repo, cp.run_id) / "files").exists()

        again = CliRunner().invoke(cli, ["osn", "resume", cp.run_id])
        assert again.exit_code != 0 and "run_already_completed" in again.output

    def test_resume_refuses_when_the_repository_moved_on(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        verify = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'
        first = FakeModel([_turn(_a("write_file", path="out.txt", content="wrong"), _a("finish")), KeyboardInterrupt()])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", first))
        CliRunner().invoke(cli, ["osn", "run", TASK, "--model", "a/m", "--escalate-model", "b/m",
                                 "--verify-cmd", verify, "--roles", "executor", "--no-learning"])
        cp = ckpt.list_checkpoints(repo)[0]
        (repo / "README.md").write_text("someone edited the repository meanwhile\n")
        r = CliRunner().invoke(cli, ["osn", "resume", cp.run_id])
        assert r.exit_code != 0 and "repository_changed_since_run_started" in r.output
        assert "not resumable (repository_changed_since_run_started)" in CliRunner().invoke(cli, ["osn", "runs"]).output
        missing = CliRunner().invoke(cli, ["osn", "resume", "osn-nope"])
        assert missing.exit_code != 0 and "checkpoint_missing" in missing.output


def test_resumed_block_projects_counts_only():
    block = resumed_block({"attempts_restored": 1, "plan_restored": True, "files_restored": 2,
                           "checkpoint_phase": "attempt_done", "interrupted": {"reason": "keyboard_interrupt"},
                           "prior_model_calls": 3, "prior_cost_usd": 0.01, "unsaved_progress_discarded": True,
                           "times_resumed": 1, "evidence": "checkpoint_recorded_by_openshard",
                           "original_routing": {"secret": "C:/path"}})
    assert block["interrupted_reason"] == "keyboard_interrupt" and block["prior_cost_usd"] == 0.01
    assert "original_routing" not in block and "C:/path" not in json.dumps(block)
    assert resumed_block(None) is None
