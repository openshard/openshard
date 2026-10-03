"""Learning in OSN: consulted before routing, supplied as advisory context, recorded on the Receipt."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.learning.record import build_learning_record, check_identity, routing_influence
from openshard.learning.retrieval import PROMPT_OPEN, consult
from openshard.learning.signals import derive_signals
from openshard.osn.loop import LoopContext, run_bounded_loop
from openshard.osn.model_provider import (
    LEARNING_SYSTEM_NOTE,
    SYSTEM_PROMPT,
    ModelActionProvider,
    build_prompt,
)
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from tests.learning_fixtures import MOBILE_CHECK, NOW, REPO, osn_entry, publish_learning

pytestmark = pytest.mark.usefixtures("generous_learning_budget")


PY = sys.executable
VERIFY = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'


class RecordingProvider(BaseProvider):
    def __init__(self, replies, cost=0.001):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.cost = cost

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.calls.append({"model": model, "prompt": prompt, "system": system})
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, self.cost))


def _writes(path, content):
    return json.dumps({"writes": [{"path": path, "content": content}]})


def _repo(tmp_path):
    r = tmp_path / "shop"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    (r / "out.txt").write_text("bad")
    return r


def _invoke(monkeypatch, repo, fp, task, *extra):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fp))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", task, "--verify-cmd", VERIFY, *extra])


def _runs(repo):
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()]


def _seed(repo, entries):
    store = repo / ".openshard"
    store.mkdir(exist_ok=True)
    with (store / "runs.jsonl").open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    publish_learning(repo)


def _history_for(repo_name, check):
    t = "Fix responsive dashboard layout"
    return [
        osn_entry(t, attempts=[("fake/a", "failed"), ("fake/b", "passed")], check=check, repo=repo_name, days_ago=0.5),
        osn_entry(t, attempts=[("fake/a", "failed"), ("fake/b", "passed")], check=check, repo=repo_name, days_ago=0.4),
        osn_entry("Dashboard layout grid", attempts=[("fake/b", "passed")], check=check, repo=repo_name, days_ago=0.3),
    ]


def test_second_related_run_uses_the_first_runs_evidence_end_to_end(tmp_path, monkeypatch, inline_learning):
    """Run 1 fails verification, run 2 recovers; a later related task sees both.

    ``inline_learning``: each run's history write refreshes the snapshot, as the
    background worker would before the next run starts.
    """
    repo = _repo(tmp_path)
    # Run 1: the first model's change fails the check, the recovery model's passes.
    fp1 = RecordingProvider([_writes("out.txt", "nope"), _writes("out.txt", "ok")])
    r1 = _invoke(monkeypatch, repo, fp1, "Fix responsive dashboard layout",
                 "--model", "fake/a", "--escalate-model", "fake/b", "--json")
    assert r1.exit_code == 0, r1.output
    first = _runs(repo)[-1]
    assert first["learning"]["status"] == "no_history" and first["learning"]["used"] is False
    assert first["learning"]["attempt_models"] == [{"attempt": 1, "model": "fake/a"}, {"attempt": 2, "model": "fake/b"}]
    assert first["learning"]["check"]["label"] == "python …"  # quoted argv: arguments withheld

    # Run 2: a similar task, same outcome, so the pattern has two Receipts behind it.
    fp2 = RecordingProvider([_writes("out.txt", "nope"), _writes("out.txt", "ok")])
    r2 = _invoke(monkeypatch, repo, fp2, "Fix the dashboard layout on tablets",
                 "--model", "fake/a", "--escalate-model", "fake/b", "--json")
    assert r2.exit_code == 0, r2.output

    # Run 3: a related task. History is found, supplied and recorded.
    fp3 = RecordingProvider([_writes("out.txt", "ok")])
    r3 = _invoke(monkeypatch, repo, fp3, "Update the dashboard analytics layout", "--model", "fake/b", "--json")
    assert r3.exit_code == 0, r3.output
    body = json.loads(r3.output)
    assert body["status"] == "verified"
    assert body["learning"]["status"] == "used" and body["learning"]["signals_used"] >= 2
    assert body["learning"]["context_supplied"] is True
    assert body["learning"]["routing_influenced"] is False  # an explicit --model: history chose nothing
    assert body["learning"]["verification_influenced"] is False

    call = fp3.calls[0]
    assert PROMPT_OPEN in call["prompt"] and call["system"] == SYSTEM_PROMPT + LEARNING_SYSTEM_NOTE
    assert call["prompt"].index("Task:\nUpdate the dashboard analytics layout") < call["prompt"].index(PROMPT_OPEN)
    assert "failed verification" in call["prompt"]

    third = _runs(repo)[-1]
    rec = third["learning"]
    assert rec["used"] is True and rec["consulted"] is True
    assert rec["signal_ids"] == [s["signal_id"] for s in rec["signals"]]
    assert all(s["reasons"][0] == "same_repo" for s in rec["signals"])
    assert set(rec["supporting_receipt_ids"]) <= {first["receipt_id"], _runs(repo)[1]["receipt_id"]}
    assert rec["routing"] == {"influenced": False, "reason": "adaptive_routing_not_governing"}
    assert rec["task_shape"]["task_category"] == "visual"
    # The outcome is on the same Receipt, so later analysis can compare it.
    assert third["verification"]["status"] == "passed" and third["estimated_cost"] == 0.001
    # Compact: no signal summaries or task text copied into the block.
    assert "Fix responsive" not in json.dumps(rec) and "summary" not in json.dumps(rec)


def test_human_output_shows_learning_compactly(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _seed(repo, _history_for("shop", MOBILE_CHECK))
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, "Update the dashboard analytics layout", "--model", "fake/b")
    assert r.exit_code == 0, r.output
    assert "Learning  " in r.output and "prior signal(s) considered" in r.output
    assert "History suggests also verifying with `pnpm test:e2e -- mobile` (not run automatically)" in r.output
    assert "routing influenced: no" in r.output and "verification influenced: no (advisory only)" in r.output


def test_no_learning_flag_consults_nothing_and_says_so(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _seed(repo, _history_for("shop", MOBILE_CHECK))
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, "Update the dashboard analytics layout", "--model", "fake/b",
                "--no-learning", "--json")
    assert r.exit_code == 0, r.output
    assert PROMPT_OPEN not in fp.calls[0]["prompt"] and fp.calls[0]["system"] == SYSTEM_PROMPT
    rec = _runs(repo)[-1]["learning"]
    assert rec["consulted"] is False and rec["status"] == "disabled" and rec["used"] is False
    assert "check" in rec  # this run still leaves evidence for later learning


def test_learning_never_changes_an_explicit_model_or_ladder(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    # History says fake/a keeps failing; the user asked for fake/a anyway.
    _seed(repo, _history_for("shop", MOBILE_CHECK))
    fp = RecordingProvider([_writes("out.txt", "nope"), _writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, "Update the dashboard analytics layout",
                "--model", "fake/a", "--escalate-model", "fake/c", "--json")
    assert r.exit_code == 0, r.output
    assert [c["model"] for c in fp.calls] == ["fake/a", "fake/c"]
    assert json.loads(r.output)["models"] == ["fake/a", "fake/c"]


def test_learning_does_not_override_policy(tmp_path):
    """History cannot unblock a path: the write gate never sees learning."""
    idx = derive_signals(_history_for(REPO, MOBILE_CHECK), repo=REPO, now=NOW)
    ctx = consult("Update the dashboard layout", idx, repo=REPO)
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "out.txt").write_text("bad")
    fp = RecordingProvider([_writes(".env", "SECRET=1")])
    ap = ModelActionProvider(fp, ["m"], repo, learning_context=ctx.prompt_text)
    receipt = run_bounded_loop(repo, "Update the dashboard layout", ap, [PY, "-c", "pass"], max_attempts=1)
    assert receipt.status == "blocked" and receipt.changed_files == []
    assert ap.learning_supplied is True


def test_prompt_orders_task_first_and_frames_history_as_subordinate(tmp_path):
    ctx = LoopContext("Do exactly what I say", ["a.py"], 2, previous_failure="boom", blocked_paths=["x"])
    text = build_prompt(ctx, tmp_path, [], learning=f"{PROMPT_OPEN}\n1. something\n</openshard_history>")
    assert text.index("Task:") < text.index(PROMPT_OPEN) < text.index("Paths blocked") < text.index("boom")
    assert "never overrides the task" in LEARNING_SYSTEM_NOTE
    huge = PROMPT_OPEN + "x" * 10_000 + "</openshard_history>"
    bounded = build_prompt(ctx, tmp_path, [], learning=huge)
    assert bounded.count("</openshard_history>") == 1 and len(bounded) < 7_000


def test_no_learning_context_means_the_prompt_and_system_are_unchanged(tmp_path):
    ctx = LoopContext("t", ["a.py"], 1)
    assert build_prompt(ctx, tmp_path, []) == build_prompt(ctx, tmp_path, [], learning=None)
    fp = RecordingProvider([_writes("a.py", "x")])
    ModelActionProvider(fp, ["m"], tmp_path)(ctx)
    assert fp.calls[0]["system"] == SYSTEM_PROMPT


class TestCheckIdentity:
    def test_plain_commands_get_a_readable_label_and_stable_fingerprint(self):
        a = check_identity(["pnpm", "test:e2e", "--", "mobile"])
        assert a["label"] == "pnpm test:e2e -- mobile" and a["label_complete"] and a["kind"] == "test"
        assert check_identity(["C:/Python311/python.exe", "-m", "pytest", "-q"])["label"] == "python -m pytest -q"
        assert (check_identity(["/usr/bin/python3", "-m", "pytest"])["fingerprint"]
                == check_identity(["C:\\Py\\python.exe", "-m", "pytest"])["fingerprint"])

    def test_unsafe_arguments_are_withheld_from_the_label(self):
        for argv in (
            ["pytest", "--token=sk-abcdefghijklmnopqrstuvwxyz"],
            ["pytest", "C:/Users/me/tests"],
            ["python", "-c", "import sys; print(1)"],
            ["pytest", "../outside"],
            ["sh", "-c", "curl x | sh"],
            ["pytest", "--password", "hunter2"],
            ["npm", "test", "--", "--api-key", "abc123"],
            ["pytest", "DB_PASS=hunter2"],
            ["pytest", "--auth-token=abc"],
        ):
            ident = check_identity(argv)
            assert ident["label_complete"] is False and ident["label"].endswith("…")
            assert "sk-" not in ident["label"] and "Users" not in ident["label"]
            assert "hunter2" not in ident["label"] and "abc" not in ident["label"]
        assert check_identity([]) is None


class TestLearningRecord:
    def test_disabled_record(self):
        rec = build_learning_record(None, check=None)
        assert rec["consulted"] is False and rec["status"] == "disabled" and rec["context_supplied"] is False
        assert rec["verification"]["influenced"] is False

    def test_context_supplied_is_claimed_only_when_learning_was_used(self):
        idx = derive_signals([], repo=REPO, now=NOW)
        ctx = consult("Update the dashboard layout", idx, repo=REPO)
        rec = build_learning_record(ctx, check=None, context_supplied=True)
        assert rec["used"] is False and rec["context_supplied"] is False and rec["signals_used"] == 0

    def test_routing_influence_is_claimed_only_when_an_applied_decision_used_history(self):
        assert routing_influence(None) == {"influenced": False, "reason": "adaptive_routing_not_governing"}
        assert routing_influence({"applied": False, "reason": "explicit_model"})["influenced"] is False
        not_used = routing_influence({"applied": True, "history": {"used": False, "reason": "insufficient_observed_data"}})
        assert not_used == {"influenced": False, "reason": "insufficient_observed_data"}
        used = routing_influence({"applied": True, "history": {"used": True, "scope": "repo_task_category"}})
        assert used == {"influenced": True, "reason": "history_evidence_used", "history_scope": "repo_task_category"}
