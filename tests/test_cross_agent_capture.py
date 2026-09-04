"""Cross-agent history (PR12): Claude Code, Codex and OpenCode in one repository.

The product invariant under test: work observed from all three agents
lands in the *same* canonical history (one ``.openshard/runs.jsonl``, one
Shard model, one receipt renderer) and is reachable through the existing
query surfaces -- with the executor identity of each Shard preserved, no
accidental cross-Shard merging, and no cross-repository bleed.

Every entry here is produced by driving the real adapter/translator
boundary (``handle_hook`` for each agent, and the shared capture service
over HTTP), never by hand-building canonical Events.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks import (
    ReducedHookPayload,
    apply_reduced_hook,
    handle_claude_hook,
    handle_hook,
)
from openshard.history.event import (
    SOURCE_CLAUDE_CODE_HOOKS,
    SOURCE_CODEX_HOOKS,
    SOURCE_OPENCODE_PLUGIN,
)
from openshard.history.query import get_receipt, list_shards, relevant_context, search_history
from openshard.history.shard import ORIGIN_EXTERNAL_OBSERVED

CLAUDE_SID = "11111111-1111-4111-8111-111111111111"
CODEX_SID = "22222222-2222-4222-8222-222222222222"
OPENCODE_SID = "ses_33333333333333333333333333333333"
SHARED_SID = "44444444-4444-4444-8444-444444444444"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "shared repo")


def _lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# -- per-agent session scripts, expressed as the raw documents each agent sends -----------------


def claude_docs(repo: Path, sid: str = CLAUDE_SID, task: str = "Fix the login button alignment in auth.py") -> list[dict]:
    base = {"session_id": sid, "cwd": str(repo), "transcript_path": "/x/t.jsonl", "permission_mode": "default"}
    return [
        {**base, "hook_event_name": "SessionStart", "source": "startup"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": task},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit",
         "tool_input": {"file_path": str(repo / "auth.py"), "old_string": "a", "new_string": "b"}},
        {**base, "hook_event_name": "Stop"},
        {**base, "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"},
    ]


def codex_docs(repo: Path, sid: str = CODEX_SID, task: str = "Add terraform verification step for infra") -> list[dict]:
    base = {"session_id": sid, "cwd": str(repo), "model": "gpt-5-codex", "transcript_path": "/x/r.jsonl"}
    return [
        {**base, "hook_event_name": "SessionStart", "source": "startup"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": task},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "apply_patch",
         "tool_input": {"patch": "*** Begin Patch\n*** Update File: infra/main.tf\n@@\n+x\n*** End Patch\n"}},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "terraform validate"}},
        {**base, "hook_event_name": "Stop"},
        {**base, "hook_event_name": "SessionEnd"},
    ]


def opencode_docs(repo: Path, sid: str = OPENCODE_SID, task: str = "Write release notes for the changelog") -> list[dict]:
    base = {"agent": "opencode", "session_id": sid, "directory": str(repo), "worktree": str(repo)}
    return [
        {**base, "event": "session.created", "parent_id": None},
        {**base, "event": "chat.message", "prompt": task, "provider_id": "openai", "model_id": "gpt-5"},
        {**base, "event": "tool.execute.after", "tool": "edit", "file_path": str(repo / "CHANGELOG.md")},
        {**base, "event": "message.updated", "message_id": "msg_a1", "provider_id": "openai", "model_id": "gpt-5",
         "cost": 0.03, "tokens": {"input": 300, "output": 40, "reasoning": 0, "cache": {"read": 0, "write": 0}}},
        {**base, "event": "session.idle"},
        {**base, "event": "session.deleted"},
    ]


def drive_all_inline(repo: Path) -> None:
    (repo / "auth.py").write_text("x = 1\n", encoding="utf-8")
    for d in claude_docs(repo):
        handle_claude_hook(d, env={"CLAUDE_PROJECT_DIR": str(repo)})
    (repo / "infra").mkdir(exist_ok=True)
    (repo / "infra" / "main.tf").write_text("resource {}\n", encoding="utf-8")
    for d in codex_docs(repo):
        handle_hook(d, env={}, agent="codex")
    (repo / "CHANGELOG.md").write_text("# notes\n", encoding="utf-8")
    for d in opencode_docs(repo):
        handle_hook(d, env={}, agent="opencode")


# ---------------------------------------------------------------------------


class TestOneHistoryThreeAgents:
    def test_entries_coexist_with_distinct_identity(self, repo):
        drive_all_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 3
        by_executor = {e["executor"]: e for e in lines}
        assert set(by_executor) == {"claude_code_hooks", "codex_hooks", "opencode_plugin"}
        assert len({e["shard_id"] for e in lines}) == 3
        assert len({e["run_id"] for e in lines}) == 3
        assert by_executor["claude_code_hooks"]["capture"]["agent"] == "claude_code"
        assert by_executor["codex_hooks"]["capture"]["agent_vendor"] == "OpenAI"
        assert by_executor["opencode_plugin"]["capture"]["provider"] == "openai"
        assert by_executor["opencode_plugin"]["execution_model"] == "openai/gpt-5"
        assert by_executor["codex_hooks"]["execution_model"] == "gpt-5-codex"
        # Same repo identity fields on every record (canonical repo semantics).
        assert len({e.get("repo_identity") for e in lines}) == 1
        sources = {e["executor"]: {ev["source"] for ev in e["events"]} for e in lines}
        assert sources["claude_code_hooks"] == {SOURCE_CLAUDE_CODE_HOOKS}
        assert sources["codex_hooks"] == {SOURCE_CODEX_HOOKS}
        assert sources["opencode_plugin"] == {SOURCE_OPENCODE_PLUGIN}

    def test_shards_and_receipts_preserve_executor_identity(self, repo):
        drive_all_inline(repo)
        shards = list_shards(repo_path=repo)
        assert len(shards) == 3
        agents = {s.task_short: s.agent for s in shards}
        assert agents["Fix the login button alignment in auth.py"] == "Claude Code (external)"
        assert agents["Add terraform verification step for infra"] == "Codex (external)"
        assert agents["Write release notes for the changelog"] == "OpenCode (external)"
        for s in shards:
            assert s.origin == ORIGIN_EXTERNAL_OBSERVED
            receipt = get_receipt(s.shard_id, repo_path=repo)
            assert receipt.agent == s.agent
            assert receipt.status != "passed"  # nothing was verified by OpenShard
        opencode_receipt = next(get_receipt(s.shard_id, repo_path=repo) for s in shards if "OpenCode" in s.agent)
        assert opencode_receipt.tokens_input == 300 and opencode_receipt.tokens_provenance == "agent_reported"
        codex_receipt = next(get_receipt(s.shard_id, repo_path=repo) for s in shards if "Codex" in s.agent)
        assert codex_receipt.tokens_input is None and codex_receipt.cost_provenance is None

    def test_search_and_relevant_context_reach_every_agent(self, repo):
        drive_all_inline(repo)
        assert [h.shard.agent for h in search_history("codex", repo_path=repo)] == ["Codex (external)"]
        assert [h.shard.agent for h in search_history("opencode", repo_path=repo)] == ["OpenCode (external)"]
        assert [h.shard.agent for h in search_history("claude", repo_path=repo)] == ["Claude Code (external)"]

        ctx = relevant_context("terraform verification for infra", repo_path=repo)
        assert ctx.matches and ctx.matches[0].shard.agent == "Codex (external)"
        ctx = relevant_context("update the changelog release notes", repo_path=repo)
        assert ctx.matches and ctx.matches[0].shard.agent == "OpenCode (external)"
        ctx = relevant_context("login button alignment", repo_path=repo)
        assert ctx.matches and ctx.matches[0].shard.agent == "Claude Code (external)"
        ctx = relevant_context("auth.py", repo_path=repo)
        assert ctx.matches and ctx.matches[0].shard.agent == "Claude Code (external)"

    def test_same_session_id_across_agents_never_merges(self, repo):
        for d in claude_docs(repo, sid=SHARED_SID, task="claude task"):
            handle_claude_hook(d, env={"CLAUDE_PROJECT_DIR": str(repo)})
        for d in codex_docs(repo, sid=SHARED_SID, task="codex task"):
            handle_hook(d, env={}, agent="codex")
        for d in opencode_docs(repo, sid=SHARED_SID, task="opencode task"):
            handle_hook(d, env={}, agent="opencode")
        lines = _lines(repo)
        assert len(lines) == 3 and len({e["shard_id"] for e in lines}) == 3
        assert all(e["capture"]["session_id"] == SHARED_SID for e in lines)
        assert {e["task"] for e in lines} == {"claude task", "codex task", "opencode task"}
        # A second turn of the Codex session updates the Codex record only.
        handle_hook({"session_id": SHARED_SID, "cwd": str(repo), "hook_event_name": "UserPromptSubmit",
                     "prompt": "more"}, env={}, agent="codex")
        handle_hook({"session_id": SHARED_SID, "cwd": str(repo), "hook_event_name": "Stop"}, env={}, agent="codex")
        lines = _lines(repo)
        assert len(lines) == 3
        codex = next(e for e in lines if e["executor"] == "codex_hooks")
        assert codex["capture"]["prompt_count"] == 2 and codex["capture"]["turn_count"] == 2
        assert next(e for e in lines if e["executor"] == "claude_code_hooks")["capture"]["turn_count"] == 1
        opencode = next(e for e in lines if e["executor"] == "opencode_plugin")
        assert opencode["capture"]["turn_count"] == 0 and opencode["capture"]["idle_count"] == 1

    def test_no_cross_repository_bleed(self, tmp_path):
        a = _make_repo(tmp_path / "repo a")
        b = _make_repo(tmp_path / "repo b")
        drive_all_inline(a)
        for d in codex_docs(b, task="only in repo b"):
            handle_hook(d, env={}, agent="codex")
        assert len(_lines(a)) == 3 and len(_lines(b)) == 1
        assert {s.task_short for s in list_shards(repo_path=b)} == {"only in repo b"}
        assert not relevant_context("login button alignment", repo_path=b).matches
        assert not relevant_context("only in repo b", repo_path=a).matches
        assert not (tmp_path / ".openshard").exists()


# ---------------------------------------------------------------------------
# Fail-closed tool semantics: "passed" needs a provider-attested success signal
# ---------------------------------------------------------------------------


def _no_git():
    """Patches making every capture root a plain directory with git unavailable."""
    return (
        patch("openshard.adapters.claude_mcp_install.find_repo_root", return_value=None),
        patch("openshard.adapters.claude_code_import.subprocess.run", side_effect=FileNotFoundError("no git")),
    )


class TestFailClosedToolSemantics:
    def test_claude_post_tool_use_is_still_an_attested_success(self, repo):
        (repo / "auth.py").write_text("x = 1\n", encoding="utf-8")
        for d in claude_docs(repo):
            handle_claude_hook(d, env={"CLAUDE_PROJECT_DIR": str(repo)})
        entry = _lines(repo)[0]
        tool = next(e for e in entry["events"] if e["event_type"] == "tool.invoked")
        # Claude Code documents PostToolUse as firing only after a successful
        # tool run, so its file edits stay "passed" exactly as before PR12.
        assert tool["status"] == "passed" and tool["target"] == "auth.py"
        assert entry["capture"]["turn_count"] == 1 and entry["capture"]["task_status"] == "turn_completed"

    def test_codex_and_opencode_file_tools_are_never_passed(self, repo):
        drive_all_inline(repo)
        by = {e["executor"]: e for e in _lines(repo)}
        for executor in ("codex_hooks", "opencode_plugin"):
            tools = [e for e in by[executor]["events"] if e["event_type"] == "tool.invoked"]
            assert tools and all(e["status"] == "unknown" for e in tools), executor
        codex_patch = next(e for e in by["codex_hooks"]["events"] if e["metadata"].get("tool") == "apply_patch")
        assert codex_patch["target"] == "infra/main.tf"  # the attempted target is still recorded
        # git-observed changes are evidence on their own, independent of any hook claim.
        assert {f["path"] for f in by["codex_hooks"]["files_detail"]} >= {"infra/main.tf"}
        assert {f["path"] for f in by["opencode_plugin"]["files_detail"]} >= {"CHANGELOG.md"}
        assert all(e["evidence"] == "git_observed"
                   for entry in by.values() for e in entry["events"] if e["event_type"] == "file.changed")

    def test_hook_reported_fallback_without_git_needs_a_positive_signal(self, tmp_path):
        claude_root, codex_root, oc_root = tmp_path / "claude", tmp_path / "codex", tmp_path / "opencode"
        for root in (claude_root, codex_root, oc_root):
            root.mkdir()
        no_root, no_git = _no_git()
        with no_root, no_git:
            for d in claude_docs(claude_root)[:4]:
                handle_claude_hook(d, env={"CLAUDE_PROJECT_DIR": str(claude_root)})
            for d in codex_docs(codex_root)[:5]:
                handle_hook(d, env={}, agent="codex")
            for d in opencode_docs(oc_root)[:5]:
                handle_hook(d, env={}, agent="opencode")
        claude, codex, opencode = _lines(claude_root)[0], _lines(codex_root)[0], _lines(oc_root)[0]
        # Claude Code: PostToolUse attests success -> hook-reported file kept.
        assert claude["files_source"] == "claude_hook_reported"
        assert [f["path"] for f in claude["files_detail"]] == ["auth.py"]
        # Codex apply_patch / OpenCode tool.execute.after: no success signal -> nothing reported.
        assert codex["files_source"] == "not_available" and codex["files_detail"] == []
        assert opencode["files_source"] == "not_available" and opencode["files_detail"] == []
        for entry in (codex, opencode):
            assert not [e for e in entry["events"] if e["event_type"] == "file.changed"]


# ---------------------------------------------------------------------------
# Backwards compatibility with pre-PR12 staging buffers and queue lines
# ---------------------------------------------------------------------------


class TestPrePR12Compat:
    def test_staging_buffer_without_agent_field_is_a_claude_session(self, repo):
        buf = ch._new_buffer(CLAUDE_SID, repo, "SessionStart")
        for key in ("agent", "idle_count", "last_idle_at", "model_source", "provider_current",
                    "usage_by_key", "usage_provenance"):
            del buf[key]
        buf.update({"prompt_count": 1, "task": "legacy task", "first_prompt_at": buf["started_at"]})
        path = ch.buffer_path(repo, CLAUDE_SID)
        ch._write_buffer(path, buf)
        outcome = handle_claude_hook({"session_id": CLAUDE_SID, "cwd": str(repo), "hook_event_name": "Stop"},
                                     env={"CLAUDE_PROJECT_DIR": str(repo)})
        assert outcome.action == "record_created", outcome
        entry = _lines(repo)[0]
        assert entry["executor"] == "claude_code_hooks" and entry["capture"]["agent"] == "claude_code"
        assert entry["task"] == "legacy task" and entry["capture"]["turn_count"] == 1
        assert entry["capture"]["idle_count"] == 0
        assert all(ev["source"] == SOURCE_CLAUDE_CODE_HOOKS and ev["actor"] == "claude_code" for ev in entry["events"])
        assert entry["summary"].startswith("Claude Code session")

    def test_queue_line_without_agent_or_success_flag_is_a_claude_success(self, repo):
        (repo / "a.py").write_text("x\n", encoding="utf-8")
        legacy = {"event": "PostToolUse", "session_id": CLAUDE_SID, "tool_name": "Write", "file_target": "a.py"}
        reduced = ReducedHookPayload.from_dict(legacy)
        assert reduced.agent == "claude_code" and reduced.tool_success is True
        # A PR12 Codex line never gains a success signal from the compat rule.
        codex = ReducedHookPayload.from_dict({**legacy, "agent": "codex", "tool_kind": "file", "tool_name": "apply_patch"})
        assert codex.tool_success is None
        handle_claude_hook({"session_id": CLAUDE_SID, "cwd": str(repo), "hook_event_name": "UserPromptSubmit",
                            "prompt": "legacy"}, env={"CLAUDE_PROJECT_DIR": str(repo)})
        assert apply_reduced_hook(reduced, repo, dedup_id="legacy-1").action == "buffered"
        handle_claude_hook({"session_id": CLAUDE_SID, "cwd": str(repo), "hook_event_name": "Stop"},
                           env={"CLAUDE_PROJECT_DIR": str(repo)})
        entry = _lines(repo)[0]
        tool = next(e for e in entry["events"] if e["event_type"] == "tool.invoked")
        assert tool["status"] == "passed" and tool["target"] == "a.py"


# ---------------------------------------------------------------------------
# The same, through the one shared capture service
# ---------------------------------------------------------------------------


class _Service:
    def __init__(self, env: dict) -> None:
        self.ready = threading.Event()
        self.box: list = []
        self.thread = threading.Thread(
            target=svc.serve, kwargs={"port": 0, "idle_timeout": 0.0, "env": env,
                                      "ready": self.ready, "server_box": self.box}, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    @property
    def server(self):
        return self.box[0]

    def stop(self) -> None:
        if self.box:
            self.box[0].begin_shutdown("test")
        self.thread.join(60)


@pytest.fixture
def capture_env(monkeypatch) -> dict:
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    return dict(os.environ)


@pytest.fixture
def service(capture_env):
    running = _Service(capture_env)
    yield running
    running.stop()


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestSharedService:
    def test_three_agents_one_service_one_history(self, service, repo):
        port = service.server.port
        (repo / "auth.py").write_text("x = 1\n", encoding="utf-8")
        for d in claude_docs(repo):
            assert client.post_hook(port, json.dumps(d).encode(), project_dir=str(repo))
        for d in codex_docs(repo):
            assert client.post_hook(port, json.dumps(d).encode(), hook_path=client.CODEX_HOOK_PATH)
        for d in opencode_docs(repo):
            assert client.post_hook(port, json.dumps(d).encode(), hook_path=client.OPENCODE_HOOK_PATH)
        # Three sessions fold in the background (each fold runs git); on a
        # contended box that can take well over a minute, so the wait is
        # generous -- correctness, not latency, is what this test checks.
        assert _wait_for(lambda: len(_lines(repo)) == 3 and all(e["capture"]["session_end_observed"]
                                                                 for e in _lines(repo)), timeout=180)
        assert service.server.recorder.wait_idle(60)
        stats = client.health(port)["stats"]
        assert stats["queued"] == 17
        assert {e["executor"] for e in _lines(repo)} == {"claude_code_hooks", "codex_hooks", "opencode_plugin"}
        assert {s.agent for s in list_shards(repo_path=repo)} == {
            "Claude Code (external)", "Codex (external)", "OpenCode (external)"}
        # Every queue file is eventually consumed; nothing agent-specific is
        # left behind. Bounded wait rather than an instant check: a replay
        # that hit a transient file lock (Windows antivirus) is retried after
        # a short backoff, and the queue file legitimately survives until then.
        sessions = repo / ".openshard" / "claude_sessions"
        assert _wait_for(lambda: not list(sessions.glob("*")), timeout=30), list(sessions.glob("*"))

    def test_agent_scoped_queues_survive_a_crash_and_replay_independently(self, capture_env, repo):
        """Three agents, one shared session id, service killed mid-queue: each agent's
        queue file (hook *and* status lines) is separate and replays into its own Shard."""
        recorder = svc.CaptureRecorder(instance_id="crashy")
        recorder.pause_processing()
        recorder.start()
        server = svc.CaptureServer(0, recorder, instance_id="crashy", started_at="now")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        (repo / "auth.py").write_text("x = 1\n", encoding="utf-8")
        try:
            for d in claude_docs(repo, sid=SHARED_SID, task="claude task")[:4]:
                assert client.post_hook(server.port, json.dumps(d).encode(), project_dir=str(repo))
            for d in codex_docs(repo, sid=SHARED_SID, task="codex task")[:5]:
                assert client.post_hook(server.port, json.dumps(d).encode(), hook_path=client.CODEX_HOOK_PATH)
            for d in opencode_docs(repo, sid=SHARED_SID, task="opencode task")[:5]:
                assert client.post_hook(server.port, json.dumps(d).encode(), hook_path=client.OPENCODE_HOOK_PATH)
        finally:
            server.shutdown()
            server.server_close()
        assert _lines(repo) == []
        sessions = repo / ".openshard" / "claude_sessions"
        names = sorted(p.name for p in sessions.glob("*.queue*.jsonl"))
        assert names == sorted([f"{SHARED_SID}{svc.QUEUE_SUFFIX}", f"codex.{SHARED_SID}{svc.QUEUE_SUFFIX}",
                                f"opencode.{SHARED_SID}{svc.QUEUE_SUFFIX}"])
        opencode_lines = [json.loads(ln) for ln in
                          (sessions / f"opencode.{SHARED_SID}{svc.QUEUE_SUFFIX}").read_text(encoding="utf-8").splitlines()]
        assert [ln["kind"] for ln in opencode_lines] == ["hook", "hook", "hook", "status", "hook"]
        assert opencode_lines[3]["data"]["usage_key"] == "msg_a1" and opencode_lines[3]["data"]["agent"] == "opencode"
        state = Path(client.state_path(capture_env))
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"recent_repos": [str(repo)]}), encoding="utf-8")
        running = _Service(capture_env)
        try:
            assert _wait_for(lambda: len(_lines(repo)) == 3, timeout=120), _lines(repo)
            assert running.server.recorder.wait_idle(60)
            by = {e["executor"]: e for e in _lines(repo)}
            assert set(by) == {"claude_code_hooks", "codex_hooks", "opencode_plugin"}
            assert len({e["shard_id"] for e in by.values()}) == 3
            assert all(e["capture"]["session_id"] == SHARED_SID for e in by.values())
            assert by["claude_code_hooks"]["task"] == "claude task" and by["claude_code_hooks"]["capture"]["turn_count"] == 1
            codex = by["codex_hooks"]["capture"]
            assert by["codex_hooks"]["task"] == "codex task" and codex["turn_count"] == 1 and codex["tool_call_count"] == 2
            opencode = by["opencode_plugin"]
            assert opencode["task"] == "opencode task"
            assert opencode["capture"]["turn_count"] == 0 and opencode["capture"]["idle_count"] == 1
            # The queued status line replayed into the OpenCode record only.
            assert opencode["estimated_cost"] == pytest.approx(0.03) and opencode["prompt_tokens"] == 300
            assert "estimated_cost" not in by["codex_hooks"] and "estimated_cost" not in by["claude_code_hooks"]
            assert _wait_for(lambda: not list(sessions.glob("*.queue*.jsonl")), timeout=30)
        finally:
            running.stop()
