"""Tests for the read-only historical Receipt recovery scripts (docs/receipt-recovery-2026-10-10.md).

Both scripts must stay read-only and must never propose a value that the
evidence does not support: no model without one consistent session record, no
tokens or cost from session-wide totals, no agent rewrite of a sealed Receipt.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evidence = _load("receipt_recovery_github_evidence")
inventory = _load("receipt_recovery_inventory")

SESSION = {
    "id": "session_01ABC", "created": "2026-10-09T10:00:00Z", "updated": "2026-10-09T11:00:00Z",
    "model": "claude-fable-5-1", "served": "claude-fable-5-1", "repos": ["openshard/openshard"],
    "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 1.5},
}


# ----------------------------------------------------------------- evidence


def test_receipt_id_matches_the_platform_derivation():
    # Created in production on 9 October for Platform #139 and acknowledged with this id.
    assert evidence.receipt_id("1371280725", "e1be46ebfd9c910695398a7e53bbfc0e6ab897f9") == (
        "rcpt_fe0ec849ef514e5d0882db6003f82ef9"
    )


def test_commit_trailers_win_and_tagged_only_versions_ignore_pr_bodies():
    message = "Fix\n\nOpenshard-Agent: Codex\n"
    body = "Openshard-Agent: ChatGPT\nOpenshard-Model: GPT-6\n"
    assert evidence.metadata(message, body, tagged_only=False)["agent"] == "Codex"
    assert evidence.metadata(message, body, tagged_only=False)["model"] == "GPT-6"
    assert evidence.metadata(message, body, tagged_only=True)["model"] == ""


@pytest.mark.parametrize("committed_at,served,repos,expected", [
    ("2026-10-09T11:30:00+01:00", "claude-fable-5-1", ["openshard/openshard"], True),
    ("2026-10-09T12:55:49+01:00", "claude-fable-5-1", ["openshard/openshard"], False),  # merged after the session
    ("2026-10-09T11:30:00+01:00", "claude-opus-5-5", ["openshard/openshard"], False),  # fallback recorded
    ("2026-10-09T11:30:00+01:00", None, ["openshard/platform"], False),  # repository out of scope
])
def test_a_session_stands_for_a_commit_model_only_when_consistent(committed_at, served, repos, expected):
    record = evidence.session_evidence({**SESSION, "served": served, "repos": repos}, "openshard", committed_at)
    assert evidence.consistent(record) is expected


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_build_reconstructs_inspected_commits_from_runs(tmp_path):
    checkouts, data = tmp_path / "checkouts", tmp_path / "inv"
    data.mkdir()
    for repo in evidence.REPO_IDS:
        root = checkouts / repo
        root.mkdir(parents=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "T")
        workflow = root / ".github" / "workflows" / "openshard-cloud-receipts.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("inputs:\n  commit_sha:\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "seed")
        _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    core = checkouts / "openshard"
    _git(core, "commit", "--allow-empty", "-qm",
         "Tagged\n\nOpenshard-Agent: Claude Code\nClaude-Session: https://claude.ai/code/session_01ABC")
    tagged = _git(core, "rev-parse", "HEAD")
    _git(core, "commit", "--allow-empty", "-qm", "Untagged")
    untagged = _git(core, "rev-parse", "HEAD")
    (data / "openshard-runs.tsv").write_text(
        f"1\tpush\tmain\t{tagged}\tsuccess\t2026-10-09T10:30:00Z\t1\n2\tpush\tmain\t{untagged}\tsuccess\t2026-10-09T10:40:00Z\t1\n")
    (data / "platform-runs.tsv").write_text("")
    (data / "openshard-prs.jsonl").write_text("")
    (data / "platform-prs.jsonl").write_text("")
    (data / "sessions.json").write_text(json.dumps([SESSION]))

    result = evidence.build(data, checkouts)
    summary = result["summary"]["repos"]["openshard"]
    assert summary["commits_inspected"] == 2
    assert summary["expected_receipts_or_attachments"] == 1
    assert summary["skipped_no_agent"] == 1
    (row,) = [r for r in result["commits"] if r["sha"] == tagged]
    assert row["receipt_id"] == evidence.receipt_id("1204966204", tagged)
    assert row["sessions"] == ["session_01ABC"]
    assert row["session_evidence"][0]["usage_session_total"]["cost_usd"] == 1.5


# ---------------------------------------------------------------- inventory


def _view(**receipt):
    usage = receipt.pop("usage_current", None)
    current = receipt.pop("verification_current", None)
    base = {"receipt_id": "rcpt_x", "agent": "Claude Code", "origin": "github_observed", "model": None,
            "cost_usd": None, "tokens_input": None, "files_changed": 1,
            "repo_identity": "github.com/openshard/openshard", "commit": "a" * 40,
            "verification": {"status": "passed", "complete": True}}
    return {"source": {"product": "openshard-github-cloud"}, "usage_current": usage,
            "verification_current": current, "receipt": {**base, **receipt}}


def test_classify_keeps_unknown_unknown_and_zero_recorded():
    row = inventory.classify(_view(model="auto"))
    assert (row["has_model"], row["has_tokens"], row["has_cost"]) == (False, False, False)
    row = inventory.classify(_view(model="GPT-6", tokens_input=0, cost_usd=0.0, cost_is_estimate=True))
    assert (row["has_model"], row["has_tokens"], row["has_cost"], row["cost_kind"]) == (True, True, True, "estimate")


def test_classify_prefers_later_evidence_and_never_treats_placeholders_as_agents():
    later = {"usage": {"model": {"id": "claude-opus-5-5"}, "tokens": {"status": "reconciled"},
                       "cost": {"status": "estimated", "kind": "runtime_estimate"}}}
    row = inventory.classify(_view(usage_current=later, verification_current={"state": {"state": "verified_passed"}}))
    assert row["has_model"] and row["has_tokens"] and row["cost_kind"] == "runtime_estimate"
    assert row["verification"] == "independently_verified"
    for agent, origin in (("unknown", "github_observed"), ("GitHub observed cloud work", "github_observed"),
                          ("OpenShard", "unknown")):
        assert inventory.classify(_view(agent=agent, origin=origin))["agent"] is None
    assert inventory.classify(_view(agent="OpenShard", origin="openshard_routed"))["agent"] == "OpenShard"


def _evidence_for(sha: str, sessions: list[dict]) -> dict:
    return {"commits": [{"repo": "openshard", "sha": sha, "session_evidence": sessions}]}


def test_plan_proposes_a_model_only_from_one_consistent_session_and_never_usage():
    sha = "a" * 40
    good = evidence.session_evidence(SESSION, "openshard", "2026-10-09T11:30:00+01:00")
    late = evidence.session_evidence(SESSION, "openshard", "2026-10-09T12:55:49+01:00")
    rows = [inventory.classify(_view(commit=sha))]

    actions, unrecoverable = inventory.plan(rows, _evidence_for(sha, [good]))
    assert [(a["field"], a["value"]) for a in actions] == [("model", "claude-fable-5-1")]
    assert unrecoverable["tokens: session totals are not attributable per Receipt"] == 1
    assert not any(a["field"] in ("tokens", "cost") for a in actions)

    actions, _ = inventory.plan(rows, _evidence_for(sha, [late]))
    assert actions == []
    actions, _ = inventory.plan(rows, _evidence_for(sha, [good, good]))
    assert actions == []  # two candidate sessions: ambiguous, nothing proposed


def test_plan_never_rewrites_an_agent():
    sha = "b" * 40
    good = evidence.session_evidence(SESSION, "openshard", "2026-10-09T11:30:00+01:00")
    rows = [inventory.classify(_view(commit=sha, agent="unknown", model="claude-fable-5-1"))]
    actions, _ = inventory.plan(rows, _evidence_for(sha, [good]))
    assert [(a["field"], a["action"]) for a in actions] == [("agent", "report_only")]


def test_main_from_file_writes_reports(tmp_path):
    source = tmp_path / "receipts.json"
    source.write_text(json.dumps([_view(), _view(receipt_id="rcpt_y", model="GPT-6", tokens_input=5)]))
    assert inventory.main(["x", str(tmp_path / "out"), "--from-file", str(source)]) == 0
    stats = json.loads((tmp_path / "out" / "inventory.json").read_text())["stats"]
    assert (stats["total"], stats["with_model"], stats["with_tokens"]) == (2, 1, 1)
    assert (tmp_path / "out" / "dry_run_plan.json").exists()


def test_production_fetch_only_issues_paginated_gets(monkeypatch):
    seen = []
    pages = [{"receipts": [_view()], "next_cursor": "c2"}, {"receipts": [_view(receipt_id="rcpt_y")], "next_cursor": None}]

    class _Response:
        def __init__(self, body):
            self.body = json.dumps(body).encode()

        def read(self, *_):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def fake(request, timeout=None):
        seen.append((request.get_method(), request.full_url))
        return _Response(pages[len(seen) - 1])

    monkeypatch.setattr(inventory.urllib.request, "urlopen", fake)
    views = inventory.fetch_all("https://api.example.test", "org1", "osk_test")
    assert len(views) == 2
    assert [method for method, _ in seen] == ["GET", "GET"]
    assert "cursor=c2" in seen[1][1]
