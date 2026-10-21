"""Codex and Cursor sessions end to end: hooks -> Receipt -> commit re-run -> verdict -> Platform envelopes.

The Claude Code path to a green "Verified" Receipt is proven in production.
These tests drive the same path for Codex and Cursor from recorded-shape hook
streams (``tests/fixtures/agents/*-session.jsonl``, each document in the
agent's documented hook vocabulary, with ``@REPO@`` / ``@SID@`` filled in):

    session hooks (the agent edits, runs its tests, commits)
    -> one Receipt whose resulting commit git observed the session create
    -> capture requests commit verification for exactly that commit
    -> ``openshard verify --commit <sha> --auto`` re-runs the checks on it
    -> ``openshard verdict --commit <sha>`` says verified (exit 0)
    -> the receipt and evidence envelopes match the Platform contracts

What must not change on the way: the agent's own report keeps its source
(Cursor's ``exitCode: 0`` stays ``agent_reported``; Codex reports no outcome,
so its check stays ``unknown``), and before OpenShard's re-run the verdict is
``unknown``, never verified.

No real agent runs; hooks are folded in-process. ``capture_service`` sets the
one flag the capture service process sets (``enable_pr_lookup``), which is
what lets a completed turn record its commit when no session end arrives.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks import handle_hook
from openshard.cli.main import cli
from openshard.history.receipt_evidence import commit_value
from openshard.sync.envelope import build_envelope
from openshard.sync.evidence import build_evidence_envelope
from openshard.verification import auto
from openshard.verification import change_checks as cc
from openshard.verification.post_session import load_attestations

SID = "019a4f3c-6c1e-7d2b-9c3e-2f4a5b6c7d8e"
FIXTURES = Path(__file__).parent / "fixtures"
RECEIPT_SCHEMA = json.loads((FIXTURES / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8"))
EVIDENCE_SCHEMA = json.loads(
    (FIXTURES / "platform" / "verification-evidence-envelope.v1.json").read_text(encoding="utf-8"))

# agent -> (executor, label, the session-time check as the agent reported it)
AGENTS = {
    "codex": ("codex_hooks", "Codex (external)", ("unknown", "directly_observed")),
    "cursor": ("cursor_hooks", "Cursor (external)", ("passed", "agent_reported")),
}
SESSION_END = {"codex": "SessionEnd", "cursor": "sessionEnd"}
STOP = {"codex": "Stop", "cursor": "stop"}
GIT_COMMIT_TOOL = {"codex": ("PostToolUse", "Bash"), "cursor": ("postToolUse", "Shell")}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A small Python project whose test fails until ``f`` returns 2."""
    root = tmp_path / "widget"
    files = {
        "pyproject.toml": "[project]\nname='widget'\n",
        "pkg/__init__.py": "",
        "pkg/widget.py": "def f():\n    return 1\n",
        "tests/__init__.py": "",
        "tests/test_widget.py": "from pkg.widget import f\n\n\ndef test_f():\n    assert f() == 2\n",
        ".gitignore": ".openshard/\n__pycache__/\n.pytest_cache/\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    monkeypatch.setattr(cc, "python_executable", lambda _root: sys.executable)
    monkeypatch.setattr(auto, "_requested", set())
    return root


@pytest.fixture
def scheduled(monkeypatch) -> list[tuple[str, str | None]]:
    """Commit-verification requests capture made, as (receipt_id, commit); nothing is spawned."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(auto, "schedule_commit_verify",
                        lambda root, entry, **_kw: calls.append((entry.get("receipt_id"), commit_value(entry))))
    return calls


@pytest.fixture
def capture_service(monkeypatch) -> None:
    monkeypatch.setattr(ch, "_PR_LOOKUP_ENABLED", True)
    monkeypatch.setenv(ch.PR_LOOKUP_ENV, "off")  # no gh call from a test


def _session(agent: str, repo: Path) -> list[dict]:
    text = (FIXTURES / "agents" / f"{agent}-session.jsonl").read_text(encoding="utf-8")
    repo_json = json.dumps(str(repo))[1:-1]
    return [json.loads(line.replace("@REPO@", repo_json).replace("@SID@", SID)) for line in text.splitlines()]


def _replay(agent: str, repo: Path, docs: list[dict], *, good: bool = True) -> str | None:
    """Fold *docs* in order; ``_step`` lines are the agent's side effects. Returns the commit made."""
    sha = None
    for doc in docs:
        step = doc.get("_step")
        if step == "edit":
            (repo / "pkg" / "widget.py").write_text(f"def f():\n    return {2 if good else 3}\n", encoding="utf-8")
        elif step == "commit":
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "Make f return 2")
            sha = _git(repo, "rev-parse", "HEAD")
        else:
            handle_hook(doc, env={}, agent=agent)
    return sha


def _receipts(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _cli(*args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, json.loads(result.output)


def _without(docs: list[dict], *events: str) -> list[dict]:
    return [d for d in docs if d.get("hook_event_name") not in events]


@pytest.mark.parametrize("agent", sorted(AGENTS))
class TestVerifiedReceipt:
    def test_a_committed_session_becomes_verified_only_after_openshards_rerun(self, agent, repo, scheduled):
        executor, label, (session_status, session_source) = AGENTS[agent]
        sha = _replay(agent, repo, _session(agent, repo))

        [entry] = _receipts(repo)
        assert entry["executor"] == executor
        assert commit_value(entry) == sha  # git observed the session create it
        assert (entry["receipt_id"], sha) in scheduled  # capture asked for exactly this commit
        v = entry["verification"]
        assert (v["status"], v["source"]) == (session_status, session_source)  # the agent's word, as reported

        # Before any re-run nothing is verified, whatever the agent said.
        code, before = _cli("verdict", "--commit", sha, "--json")
        assert (code, before["verdict"], before["match"]) == (2, "unknown", None)

        code, out = _cli("verify", "--json", "--receipt", entry["receipt_id"], "--commit", sha, "--auto")
        assert code == 0, out
        rerun = out["verification"]
        assert (rerun["status"], rerun["source"], rerun["observation_mode"], rerun["artifact_sha"]) == (
            "passed", "directly_observed", "openshard_executed", sha)

        code, after = _cli("verdict", "--commit", sha, "--json")
        assert (code, after["verdict"], after["authority"], after["match"]) == (0, "verified", "directly_observed",
                                                                                 "commit")
        [stored] = _receipts(repo)
        assert stored["verification"] == v  # the session's own block is never rewritten

        # What sync would send: both envelopes satisfy the Platform contracts.
        receipt = build_envelope(stored, 0, core_version="0.0.0-test")
        jsonschema.validate(receipt, RECEIPT_SCHEMA)
        assert receipt["receipt"]["agent"] == label
        assert receipt["receipt"]["origin"] == "external_observed"
        evidence = build_evidence_envelope(stored, 0, load_attestations(repo / ".openshard"), core_version="0.0.0-test")
        assert evidence is not None
        jsonschema.validate(evidence, EVIDENCE_SCHEMA)
        state = evidence["state"]
        assert (state["state"], state["authority"], state["artifact_sha"]) == ("verified_passed", "directly_observed",
                                                                               sha)
        assert (state["session_status"], state["session_source"]) == (session_status, session_source)

    def test_a_failing_commit_is_failed_not_verified(self, agent, repo, scheduled):
        sha = _replay(agent, repo, _session(agent, repo), good=False)
        [entry] = _receipts(repo)
        code, out = _cli("verify", "--json", "--receipt", entry["receipt_id"], "--commit", sha, "--auto")
        assert out["verification"]["status"] == "failed"
        code, verdict = _cli("verdict", "--commit", sha, "--json")
        assert (code, verdict["verdict"]) == (1, "failed")

    def test_without_a_session_end_the_completed_turn_still_requests_verification(
        self, agent, repo, scheduled, capture_service,
    ):
        # Headless runs and Cursor background agents may never send a session end.
        sha = _replay(agent, repo, _without(_session(agent, repo), SESSION_END[agent]))
        [entry] = _receipts(repo)
        assert commit_value(entry) == sha
        assert (entry["receipt_id"], sha) in scheduled

    def test_the_capture_service_receipt_with_its_merge_check_matches_the_platform_contract(
        self, agent, repo, scheduled, capture_service,
    ):
        base = _git(repo, "symbolic-ref", "--short", "HEAD")
        _git(repo, "checkout", "-q", "-b", "agent/make-f-2")  # merge_check compares a branch with its base
        _replay(agent, repo, _session(agent, repo))
        [entry] = _receipts(repo)
        receipt = build_envelope(entry, 0, core_version="0.0.0-test")
        block = receipt["receipt"].get("merge_check")  # stamped at session end (#622)
        assert isinstance(block, dict) and (block["base_ref"], block["conflicts"]) == (base, [])
        jsonschema.validate(receipt, RECEIPT_SCHEMA)

    def test_the_hook_stream_never_reaches_the_receipt(self, agent, repo, scheduled):
        _replay(agent, repo, _session(agent, repo))
        text = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert "dev@example.com" not in text  # Cursor's user_email is never read
        assert "Done: f returns 2" not in text  # Codex's last_assistant_message is never read
        assert "1 passed in 0.01s" not in text  # tool output is never stored


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_a_commit_reported_after_the_turns_stop_is_found_at_the_next_stop(agent, repo, scheduled, capture_service):
    """Codex runs PostToolUse hooks async (codex_hooks_install), so ``git commit``'s hook can land after Stop.

    The next completed turn must re-check git even though HEAD has not moved
    since the outcome recorded at the first Stop.
    """
    docs = _without(_session(agent, repo), SESSION_END[agent])
    event, tool = GIT_COMMIT_TOOL[agent]
    commit_hook = next(d for d in docs if d.get("tool_name") == tool and "git commit" in d["tool_input"]["command"])
    stop = next(d for d in docs if d.get("hook_event_name") == STOP[agent])
    docs.remove(commit_hook)
    docs.insert(docs.index(stop) + 1, commit_hook)  # the late tool hook
    sha = _replay(agent, repo, docs)
    [entry] = _receipts(repo)
    assert commit_value(entry) is None and scheduled == []  # at the first Stop git had no corroboration yet

    handle_hook(stop, env={}, agent=agent)  # the next completed turn
    [entry] = _receipts(repo)
    assert commit_value(entry) == sha
    assert (entry["receipt_id"], sha) in scheduled


def test_a_commit_the_agent_never_reported_is_still_not_its_work(repo, scheduled, capture_service):
    """The recompute after a late git write never relaxes attribution: no git write, no session commit."""
    docs = [d for d in _without(_session("codex", repo), "SessionEnd")
            if "git commit" not in json.dumps(d.get("tool_input", {}))]
    _replay("codex", repo, docs)
    stop = next(d for d in docs if d.get("hook_event_name") == "Stop")
    handle_hook(stop, env={}, agent="codex")
    [entry] = _receipts(repo)
    assert commit_value(entry) is None and scheduled == []
