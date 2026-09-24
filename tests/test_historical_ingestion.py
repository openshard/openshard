"""Historical Ingestion v1: Claude Code + Codex history -> honestly labelled receipts.

Fixtures are small synthetic transcripts (``tests/fixtures/ingest/``) shaped
like real Claude Code 2.1.x transcripts and Codex CLI rollouts, seeded with
fake secrets and marker text that must never reach ``.openshard/``. The git
repository is real, with backdated commits, so git evidence is exercised
against actual commit-graph queries.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import tracemalloc
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.event import events_from_entry
from openshard.history.shard import ORIGIN_HISTORICAL_IMPORT, derive_shard_identity
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.history.shard_hash import verify_shard_hash
from openshard.history.store import SealedReceiptError, amend_latest_record, load_history
from openshard.history.views import receipt_to_dict
from openshard.ingest import JobSpec, cancel_job, job_status, resume_job, run_job, scan
from openshard.ingest import jobs as jobs_mod
from openshard.ingest.connectors.local_agent_history import LocalAgentHistoryConnector
from openshard.ingest.jobs import JobNotResumable
from openshard.ingest.model import SourceObject
from openshard.ingest.parsers.base import ParseError
from openshard.ingest.parsers.claude_code import ClaudeCodeParser
from openshard.ingest.parsers.codex import CodexParser
from openshard.ingest.receipt_builder import FORBIDDEN_EVIDENCE
from openshard.ingest.store import ActiveJobError, LocalJobStore

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
CLAUDE_SID = "11111111-2222-4333-8444-555555555555"
CLAUDE_SID2 = "66666666-7777-4888-9999-aaaaaaaaaaaa"
CODEX_SID = "019e0000-aaaa-7bbb-8ccc-dddddddddddd"

NEVER_STORED = (
    "SECOND_PROMPT_MARKER", "TOOL_OUTPUT_MARKER", "FILE_CONTENT_MARKER", "THINKING_MARKER",
    "FUTURE_MARKER", "BASE_INSTRUCTIONS_MARKER",
    "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKE", "ghp_FAKEFAKEFAKEFAKEFAKEFAKE1234",
    "ghp_FAKEFAKEFAKEFAKEFAKEFAKE9999", "hunter2secret",
)
ALLOWED_EVIDENCE = {
    "imported_transcript", "agent_reported", "git_observed", "git_verified",
    "independently_verified", "estimated", "unknown",
}
OLD = time.time() - 24 * 3600


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    out = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def hist_repo(tmp_path: Path) -> dict:
    """A repo whose history matches the fixture sessions' timeline."""
    repo = (tmp_path / "proj repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 0\n", encoding="utf-8")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init", date="2026-02-20T10:00:00Z")
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix parser", date="2026-03-01T10:20:00Z")
    verified = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "follow-up", date="2026-03-01T10:40:00Z")
    candidate = _git(repo, "rev-parse", "HEAD")
    head = candidate
    # Uncommitted work today must never be attributed to a past session.
    (repo / "dirty_today.py").write_text("print('today')\n", encoding="utf-8")
    (repo / "README.md").write_text("changed today\n", encoding="utf-8")
    return {"repo": repo, "verified": verified, "candidate": candidate, "head": head}


def _esc(s: str) -> str:
    return json.dumps(s)[1:-1]


def _msys(p: Path) -> str:
    s = str(p).replace("\\", "/")
    return f"/{s[0].lower()}{s[2:]}" if len(s) > 2 and s[1] == ":" else s


def _render(template: str, repo: Path, *, sid: str, commit_short: str = "", head: str = "",
            outside: Path | None = None) -> str:
    outside = outside or (repo.parent / "elsewhere" / "notes.md")
    return (template
            .replace("{{SID}}", sid)
            .replace("{{REPO_FILE}}", _esc(str(repo / "src" / "app.py")))
            .replace("{{REPO_MSYS}}", _esc(_msys(repo)))
            .replace("{{REPO_JSON}}", _esc(_esc(str(repo))))
            .replace("{{REPO}}", _esc(str(repo)))
            .replace("{{OUTSIDE_FILE}}", _esc(str(outside)))
            .replace("{{COMMIT_SHORT}}", commit_short)
            .replace("{{HEAD}}", head))


@pytest.fixture
def homes(tmp_path: Path, monkeypatch) -> dict:
    claude = tmp_path / "claude-home"
    codex = tmp_path / "codex-home"
    (claude / "projects").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return {"claude": claude, "codex": codex}


def write_claude(homes: dict, hist: dict, sid: str = CLAUDE_SID, *, slug: str = "proj", text: str | None = None,
                 mtime: float = OLD) -> Path:
    d = homes["claude"] / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else _render(
        (FIXTURES / "claude_code" / "session.jsonl.tmpl").read_text(encoding="utf-8"),
        hist["repo"], sid=sid, commit_short=hist["verified"][:7],
    )
    path = d / f"{sid}.jsonl"
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def write_codex(homes: dict, hist: dict, sid: str = CODEX_SID, *, mtime: float = OLD) -> Path:
    d = homes["codex"] / "sessions" / "2026" / "03" / "02"
    d.mkdir(parents=True, exist_ok=True)
    body = _render((FIXTURES / "codex" / "rollout.jsonl.tmpl").read_text(encoding="utf-8"),
                   hist["repo"], sid=sid, head=hist["head"])
    path = d / f"rollout-2026-03-02T09-00-00-{sid}.jsonl"
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _obj(path: Path, hint: str) -> SourceObject:
    return SourceObject(connector="local_agent_history", object_id=f"t:{path.name}", locator=str(path), hint=hint)


def _parse(parser, path: Path):
    with path.open("rb") as fh:
        return list(parser.parse(fh, _obj(path, parser.name)))


def _records(repo: Path) -> list[dict]:
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)


def _no_sleep(_s: float) -> None:
    return None


def _run(repo: Path, sources=("claude-code", "codex"), **kw):
    return run_job(JobSpec(sources=list(sources), **kw), repo_path=repo, sleep=_no_sleep)


def _all_openshard_text(repo: Path) -> str:
    chunks = []
    for p in (repo / ".openshard").rglob("*"):
        if p.is_file():
            chunks.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _walk_evidence(obj, out: list) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("evidence", "source", "observation_mode", "attribution", "tokens_provenance") and isinstance(v, str):
                out.append((k, v))
            _walk_evidence(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_evidence(v, out)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class TestClaudeParser:
    def test_golden_session(self, homes, hist_repo):
        path = write_claude(homes, hist_repo)
        (s,) = _parse(ClaudeCodeParser(), path)
        assert s.native_session_id == CLAUDE_SID
        assert s.start == "2026-03-01T10:00:00.000Z"
        assert s.end == "2026-03-01T10:31:00.000Z"
        assert s.cwd == str(hist_repo["repo"])
        assert s.branch == "main"
        assert s.head_at_start is None  # Claude does not record HEAD
        assert s.models == ["claude-opus-5-5"]
        assert s.approval_policy == "acceptEdits"
        assert s.turns == 2
        # Usage counted once per message id (msg_1 streamed over two records).
        assert s.tokens == {"input": 100 + 200 + 10 + 10 + 10 + 10, "output": 50 + 20 + 5 + 5 + 5 + 30,
                            "cache_read": 1000 + 2000, "cache_creation": 10}
        by_id = {c.call_id: c for c in s.tool_calls}
        assert by_id["toolu_edit"].outcome == "passed"  # is_error omitted = not an error
        assert by_id["toolu_test"].outcome == "passed"
        assert by_id["toolu_lint"].outcome == "failed"
        assert by_id["toolu_commit"].commit_shas == [hist_repo["verified"][:7]]
        assert hist_repo["verified"][:7] in s.claimed_shas and "deadbeef1" in s.claimed_shas
        assert s.losses == {"unknown_record_type": 1, "malformed_line": 1}

    def test_truncated_and_non_object_lines_are_losses(self, homes, hist_repo, tmp_path):
        text = _render((FIXTURES / "claude_code" / "session.jsonl.tmpl").read_text(encoding="utf-8"),
                       hist_repo["repo"], sid=CLAUDE_SID)
        path = tmp_path / f"{CLAUDE_SID}.jsonl"
        path.write_text(text + '[1, 2]\n{"type":"assistant","message":{"con', encoding="utf-8")
        (s,) = _parse(ClaudeCodeParser(), path)
        assert s.losses["non_object_record"] == 1
        assert s.losses["malformed_line"] == 2
        assert s.native_session_id == CLAUDE_SID

    def test_not_a_session_raises(self, tmp_path):
        path = tmp_path / "junk.jsonl"
        path.write_text("hello\nworld\n", encoding="utf-8")
        with pytest.raises(ParseError):
            _parse(ClaudeCodeParser(), path)

    def test_sniff_never_claims_the_other_format(self, homes, hist_repo):
        claude = write_claude(homes, hist_repo).read_bytes()
        codex = write_codex(homes, hist_repo).read_bytes()
        assert ClaudeCodeParser().sniff(claude) > 0.5
        assert CodexParser().sniff(claude) == 0.0
        assert CodexParser().sniff(codex) > 0.5
        assert ClaudeCodeParser().sniff(codex) == 0.0

    def test_streaming_large_session_is_memory_bounded(self, tmp_path):
        path = tmp_path / f"{CLAUDE_SID}.jsonl"
        line = json.dumps({"type": "assistant", "sessionId": CLAUDE_SID, "timestamp": "2026-03-01T10:00:00Z",
                           "message": {"id": "m", "model": "claude-opus-5-5", "content": [
                               {"type": "tool_use", "id": "t{}", "name": "Read", "input": {"file_path": "a.py"}}]}})
        with path.open("w", encoding="utf-8") as fh:
            for i in range(20_000):
                fh.write(line.replace("t{}", f"t{i}") + "\n")
        tracemalloc.start()
        (s,) = _parse(ClaudeCodeParser(), path)
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(s.tool_calls) == 20_000
        assert peak < 200 * 1024 * 1024


class TestCodexParser:
    def test_golden_rollout(self, homes, hist_repo):
        path = write_codex(homes, hist_repo)
        (s,) = _parse(CodexParser(), path)
        assert s.native_session_id == CODEX_SID
        assert s.head_at_start == hist_repo["head"]
        assert s.branch == "main"
        assert s.models == ["gpt-5.5"]
        assert s.provider == "openai"
        assert s.approval_policy == "on-request"
        assert s.task.startswith("Add a retry helper")
        assert s.tokens == {"input": 5000, "output": 300, "cache_read": 4000}
        by_id = {c.call_id: c for c in s.tool_calls}
        assert by_id["call_test"].outcome == "failed" and by_id["call_test"].exit_code == 1
        assert by_id["call_patch"].outcome == "passed"
        assert sorted(by_id["call_patch"].paths) == [("src/app.py", "update"), ("src/retry.py", "create")]
        assert s.claimed_shas == ["cafebabe1"]
        assert s.losses == {"unknown_record_type": 1}

    def test_prompt_from_response_item_when_no_user_message_event(self, tmp_path, hist_repo):
        lines = [
            {"timestamp": "2026-03-02T09:00:00Z", "type": "session_meta",
             "payload": {"id": CODEX_SID, "cwd": str(hist_repo["repo"])}},
            {"timestamp": "2026-03-02T09:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
            {"timestamp": "2026-03-02T09:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "explain this repo"}]}},
        ]
        path = tmp_path / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        (s,) = _parse(CodexParser(), path)
        assert s.task == "explain this repo" and s.turns == 1
        assert s.head_at_start is None and s.repository_url is None  # no git block: stays unknown

    def test_no_session_meta_raises(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}) + "\n",
                        encoding="utf-8")
        with pytest.raises(ParseError):
            _parse(CodexParser(), path)


# ---------------------------------------------------------------------------
# End-to-end import
# ---------------------------------------------------------------------------


class TestImport:
    def test_claude_and_codex_become_sealed_historical_receipts(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        repo = hist_repo["repo"]
        result = _run(repo)
        assert result.state == "completed"
        assert result.counters.written == 2 and result.counters.quarantined == 0
        recs = _records(repo)
        assert len(recs) == 2
        for rec in recs:
            assert rec["origin"] == ORIGIN_HISTORICAL_IMPORT
            assert rec["sealed_at"]
            assert verify_shard_hash(rec)["status"] == "valid"
            agent, origin, depth = derive_shard_identity(rec)
            assert origin == ORIGIN_HISTORICAL_IMPORT and depth == "partial"
            assert rec["import"]["grouping"] == {"rule": "one_session_default", "evidence": "imported_transcript"}
            assert rec["attempt_number"] == 1
            # Recorded session start, never the import time.
            assert rec["timestamp"].startswith("2026-03-0")
        claude = next(r for r in recs if r["executor"] == "claude_code_history_import")
        codex = next(r for r in recs if r["executor"] == "codex_history_import")
        assert claude["import"]["import_key"] == f"claude_code:{CLAUDE_SID}"
        assert codex["import"]["import_key"] == f"codex:{CODEX_SID}"
        assert claude["execution_model"] == "claude-opus-5-5"
        assert codex["execution_model"] == "gpt-5.5"
        assert claude["tokens_provenance"] == "imported_transcript"
        assert "estimated_cost" not in claude and claude["facts"]["cost"]["evidence"] == "unknown"
        assert codex["repo_identity"] == "github.com/example/repo"  # credentials stripped
        assert codex["facts"]["repo"]["evidence"] == "imported_transcript"

    def test_every_fact_has_allowed_evidence_and_never_direct_observation(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        _run(hist_repo["repo"])
        for rec in _records(hist_repo["repo"]):
            for name, fact in rec["facts"].items():
                assert set(fact) >= {"value", "evidence", "ref"}, name
                assert fact["evidence"] in ALLOWED_EVIDENCE, (name, fact)
                if fact["value"] is None:
                    assert fact["evidence"] == "unknown", name
            found: list = []
            _walk_evidence(rec, found)
            assert not [v for _k, v in found if v in FORBIDDEN_EVIDENCE]
            for ev in rec["events"]:
                assert ev["evidence"] not in FORBIDDEN_EVIDENCE
            assert rec["facts"]["approvals"] == {"value": None, "evidence": "unknown", "ref": None}

    def test_command_outcomes_are_agent_reported(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        _run(hist_repo["repo"])
        recs = {r["executor"]: r for r in _records(hist_repo["repo"])}
        claude, codex = recs["claude_code_history_import"], recs["codex_history_import"]
        v = claude["verification"]
        assert v["source"] == "agent_reported" and v["observation_mode"] == "imported_transcript"
        assert {c["kind"]: c["status"] for c in v["checks"]} == {"test": "passed", "lint": "failed"}
        assert v["status"] == "failed"
        assert claude["verification_passed"] is None
        assert codex["verification"]["status"] == "failed"
        assert codex["verification"]["checks"][0]["exit_code"] == 1
        tool_events = [e for e in claude["events"] if e["event_type"] == "tool.invoked"]
        with_outcome = [e for e in tool_events if e["status"] in ("passed", "failed")]
        assert with_outcome and all(e["evidence"] == "agent_reported" for e in with_outcome)
        invocation_only = [e for e in tool_events if e["status"] == "unknown"]
        assert all(e["evidence"] == "imported_transcript" for e in invocation_only)
        assert claude["facts"]["test_runs"]["evidence"] == "agent_reported"
        assert claude["facts"]["final_message"]["evidence"] == "agent_reported"

    def test_losses_surface_as_known_gaps(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        kinds = {r["kind"]: r["count"] for r in rec["capture"]["completeness"]["reasons"]}
        assert kinds["unparsed_source_records"] == 2
        assert rec["capture"]["completeness"]["status"] == "incomplete"

    def test_existing_readers_render_historical_receipts(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = load_history(hist_repo["repo"] / ".openshard" / "runs.jsonl")
        receipt = build_shard_receipt(rec, 0)
        assert receipt.agent == "Claude Code (imported history)"
        text = render_compact_shard_receipt(receipt)
        assert "Reconstructed from history" in text
        d = receipt_to_dict(receipt, extended=True)
        assert d["origin"] == "historical_import"
        assert d["tokens_provenance"] == "imported_transcript"
        assert d["verification"]["observation_mode"] == "imported_transcript"
        events = events_from_entry(rec)
        assert {e.evidence for e in events} <= {"imported_transcript", "agent_reported", "git_verified", "git_observed"}
        assert "imported_transcript" in {e.evidence for e in events}  # survives the Event round-trip

    def test_scan_writes_nothing(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        repo = hist_repo["repo"]
        result = scan(JobSpec(sources=["claude-code", "codex"]), repo, sleep=_no_sleep)
        assert result["decisions"] == {"write": 2}
        assert result["evidence_coverage"]["approvals"] == {"unknown": 2}
        assert not (repo / ".openshard").exists()


class TestPrivacy:
    def test_no_raw_transcript_secrets_or_absolute_paths_persist(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        repo = hist_repo["repo"]
        _run(repo)
        blob = _all_openshard_text(repo)
        for marker in NEVER_STORED:
            assert marker not in blob, marker
        for form in {str(repo), _esc(str(repo)), str(repo).replace("\\", "/"), _msys(repo),
                     str(repo.parent), _esc(str(repo.parent)), str(homes["claude"]), _esc(str(homes["claude"]))}:
            assert form not in blob, form
        # Projections (MCP / history --json) are equally clean.
        projected = json.dumps([receipt_to_dict(build_shard_receipt(r, i), extended=True)
                                for i, r in enumerate(load_history(repo / ".openshard" / "runs.jsonl"))])
        for marker in NEVER_STORED:
            assert marker not in projected
        assert "locator_display" not in projected

    def test_first_prompt_excerpt_is_scrubbed(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        assert rec["task"].startswith("Fix the parser bug in src/app.py.")
        assert "sk-ant" not in rec["task"]
        assert "./" in rec["task"]  # the repo path was made repo-relative

    def test_paths_outside_the_repo_are_dropped(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        assert [f["path"] for f in rec["files_detail"]] == ["src/app.py"]
        assert "notes.md" not in json.dumps(rec)

    def test_sync_defers_historical_receipts(self, homes, hist_repo):
        from openshard.sync.envelope import REASON_HISTORICAL_IMPORT_DEFERRED, eligibility

        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        verdict = eligibility(rec)
        assert not verdict.eligible and verdict.reason == REASON_HISTORICAL_IMPORT_DEFERRED


# ---------------------------------------------------------------------------
# Git evidence
# ---------------------------------------------------------------------------


class TestGitEvidence:
    def test_sha_in_tool_output_is_git_verified(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        commits = {c["sha"]: c for c in rec["facts"]["commits"]["value"]}
        assert commits[hist_repo["verified"]]["evidence"] == "git_verified"
        assert rec["git_head_commit_hash"] == hist_repo["verified"]
        assert rec["files_detail"][0]["attribution"] == "git_verified"

    def test_window_only_commit_is_git_observed(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        commits = {c["sha"]: c for c in rec["facts"]["commits"]["value"]}
        cand = commits[hist_repo["candidate"]]
        assert cand["evidence"] == "git_observed"
        assert cand["reason"] == "commit_in_session_window_touching_edited_files"

    def test_missing_claimed_sha_stays_agent_reported(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        _run(hist_repo["repo"])
        recs = {r["executor"]: r for r in _records(hist_repo["repo"])}
        claude = {c["sha"]: c for c in recs["claude_code_history_import"]["facts"]["commits"]["value"]}
        assert claude["deadbeef1"]["evidence"] == "agent_reported"
        codex = {c["sha"]: c for c in recs["codex_history_import"]["facts"]["commits"]["value"]}
        assert codex["cafebabe1"]["evidence"] == "agent_reported"

    def test_claude_head_at_start_is_inferred_as_git_observed(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        init = _git(hist_repo["repo"], "rev-list", "--max-parents=0", "HEAD")
        assert rec["facts"]["head_at_start"] == {"value": init, "evidence": "git_observed",
                                                 "ref": "git:rev-list --before"}

    def test_codex_head_is_transcript_evidence(self, homes, hist_repo):
        write_codex(homes, hist_repo)
        _run(hist_repo["repo"], sources=["codex"])
        (rec,) = _records(hist_repo["repo"])
        assert rec["facts"]["head_at_start"]["evidence"] == "imported_transcript"
        assert rec["git_base_commit_hash"] == hist_repo["head"]

    def test_ambiguous_evidence_stays_unknown(self, homes, hist_repo):
        """A commit sits in the window, but the session edited nothing and printed no SHA."""
        lines = [
            {"type": "user", "sessionId": CLAUDE_SID2, "timestamp": "2026-03-01T10:00:00Z",
             "cwd": str(hist_repo["repo"]), "gitBranch": "main", "message": {"role": "user", "content": "look around"}},
            {"type": "assistant", "sessionId": CLAUDE_SID2, "timestamp": "2026-03-01T10:50:00Z",
             "cwd": str(hist_repo["repo"]), "message": {"id": "m", "model": "claude-opus-5-5",
                                                        "content": [{"type": "text", "text": "Looks fine."}]}},
        ]
        write_claude(homes, hist_repo, sid=CLAUDE_SID2, text="\n".join(json.dumps(x) for x in lines) + "\n")
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        assert rec["facts"]["commits"] == {"value": None, "evidence": "unknown", "ref": None}
        assert rec["facts"]["files_changed"]["evidence"] == "unknown"
        assert "git_head_commit_hash" not in rec

    def test_unknown_branch_infers_nothing(self, homes, hist_repo):
        text = _render((FIXTURES / "claude_code" / "session.jsonl.tmpl").read_text(encoding="utf-8"),
                       hist_repo["repo"], sid=CLAUDE_SID, commit_short=hist_repo["verified"][:7])
        write_claude(homes, hist_repo, text=text.replace('"gitBranch":"main"', '"gitBranch":"gone-branch"'))
        _run(hist_repo["repo"], sources=["claude-code"])
        (rec,) = _records(hist_repo["repo"])
        assert rec["facts"]["head_at_start"]["evidence"] == "unknown"
        commits = {c["sha"]: c for c in rec["facts"]["commits"]["value"]}
        # Exists, but reachability from the recorded branch cannot be shown.
        assert commits[hist_repo["verified"]]["evidence"] == "git_observed"
        assert hist_repo["candidate"] not in commits

    def test_working_tree_is_never_read(self, homes, hist_repo, monkeypatch):
        import openshard.util.git as ugit

        seen: list[list[str]] = []
        real = ugit.run_git

        def spy(root, args, **kw):
            seen.append(list(args))
            return real(root, args, **kw)

        monkeypatch.setattr(ugit, "run_git", spy)
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        subcommands = {a[0] for a in seen}
        assert subcommands <= {"rev-parse", "diff-tree", "rev-list", "log", "config"}, subcommands
        (rec,) = _records(hist_repo["repo"])
        paths = {f["path"] for f in rec["files_detail"]}
        assert "dirty_today.py" not in paths and "README.md" not in paths


# ---------------------------------------------------------------------------
# Dedupe / immutability
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_reimport_is_a_noop(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        repo = hist_repo["repo"]
        _run(repo)
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        second = _run(repo)
        assert second.counters.written == 0 and second.counters.skipped_duplicate == 2
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before

    def test_grown_source_supersedes_without_touching_the_sealed_receipt(self, homes, hist_repo):
        path = write_claude(homes, hist_repo)
        repo = hist_repo["repo"]
        _run(repo, sources=["claude-code"])
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        (old,) = _records(repo)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": CLAUDE_SID, "timestamp": "2026-03-01T11:00:00Z",
                                 "cwd": str(repo), "message": {"role": "user", "content": "one more thing"}}) + "\n")
        os.utime(path, (OLD, OLD))
        result = _run(repo, sources=["claude-code"])
        assert result.counters.written == 1 and result.counters.superseded == 1
        after = (repo / ".openshard" / "runs.jsonl").read_bytes()
        assert after.startswith(before)
        old2, new = _records(repo)
        assert old2 == old
        assert new["import"]["supersedes"] == old["receipt_id"]
        assert new["receipt_id"] != old["receipt_id"]
        assert verify_shard_hash(old2)["status"] == "valid"

    def test_no_update_skips_a_grown_source(self, homes, hist_repo):
        path = write_claude(homes, hist_repo)
        repo = hist_repo["repo"]
        _run(repo, sources=["claude-code"])
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")
        os.utime(path, (OLD, OLD))
        result = _run(repo, sources=["claude-code"], no_update=True)
        assert result.counters.written == 0
        assert result.counters.filtered_by_reason == {"no_update": 1}

    def test_live_captured_session_gets_an_attachment_not_a_duplicate(self, homes, hist_repo):
        from openshard.history.jsonl_store import append_jsonl
        from openshard.history.shard_schema import coerce_shard_entry

        repo = hist_repo["repo"]
        live = coerce_shard_entry({
            "schema_version": "1.2", "timestamp": "2026-03-01T10:00:00Z", "task": "live",
            "executor": "claude_code_hooks", "receipt_id": "rcpt_" + "a" * 32,
            "capture": {"session_id": CLAUDE_SID, "agent": "claude_code", "session_end_observed": True},
        })
        append_jsonl(repo / ".openshard" / "runs.jsonl", live)
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        write_claude(homes, hist_repo)
        result = _run(repo, sources=["claude-code"])
        assert result.counters.attached == 1 and result.counters.written == 0
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before
        (att,) = [json.loads(x) for x in (repo / ".openshard" / "attachments.jsonl").read_text().splitlines()]
        assert att["receipt_id"] == live["receipt_id"]
        assert att["pins_content_hash"] == live["content_hash"]
        assert att["kind"] == "reimport_note"
        assert verify_shard_hash(att)["status"] == "valid"
        again = _run(repo, sources=["claude-code"])
        assert again.counters.attached == 0 and again.counters.skipped_duplicate == 1

    def test_index_is_a_rebuildable_cache(self, homes, hist_repo):
        from openshard.ingest.store import LocalHistoryStore

        write_claude(homes, hist_repo)
        repo = hist_repo["repo"]
        _run(repo, sources=["claude-code"])
        (repo / ".openshard" / "imports" / "index.jsonl").unlink()
        again = _run(repo, sources=["claude-code"])
        assert again.counters.skipped_duplicate == 1  # dedupe never depended on the cache
        assert LocalHistoryStore(repo).rebuild_index() == 1

    def test_sealed_receipts_refuse_amendment(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        repo = hist_repo["repo"]
        _run(repo, sources=["claude-code"])
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        with pytest.raises(SealedReceiptError):
            amend_latest_record(repo / ".openshard" / "runs.jsonl", "note", lambda r: r.update(notes=[]))
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _three_sessions(homes, hist):
    for i, sid in enumerate((CLAUDE_SID, CLAUDE_SID2, "77777777-8888-4999-aaaa-bbbbbbbbbbbb")):
        write_claude(homes, hist, sid=sid, slug=f"p{i}")


class TestJobs:
    def test_crash_after_write_then_resume_writes_exactly_once(self, homes, hist_repo, monkeypatch):
        _three_sessions(homes, hist_repo)
        repo = hist_repo["repo"]
        real_append = LocalJobStore.append_item
        state = {"written": 0}

        def crashing(self, job_id, item):
            if item.get("state") == "written":
                state["written"] += 1
                if state["written"] == 2:
                    raise RuntimeError("simulated crash between receipt append and checkpoint")
            return real_append(self, job_id, item)

        monkeypatch.setattr(LocalJobStore, "append_item", crashing)
        with pytest.raises(RuntimeError):
            run_job(JobSpec(sources=["claude-code"]), repo_path=repo, sleep=_no_sleep, job_id="ijob_crash")
        monkeypatch.setattr(LocalJobStore, "append_item", real_append)
        assert len(_records(repo)) == 2
        assert job_status("ijob_crash", repo_path=repo)["state"] == "processing"
        result = resume_job("ijob_crash", repo_path=repo, sleep=_no_sleep)
        assert result.state == "completed"
        recs = _records(repo)
        assert len(recs) == 3
        assert len({r["import"]["import_key"] for r in recs}) == 3

    def test_cancel_is_honoured(self, homes, hist_repo):
        _three_sessions(homes, hist_repo)
        repo = hist_repo["repo"]

        def cancel_after_first(ev):
            if ev["kind"] == "item":
                cancel_job(ev["job_id"], repo_path=repo)

        result = run_job(JobSpec(sources=["claude-code"]), repo_path=repo, sleep=_no_sleep,
                         progress_cb=cancel_after_first)
        assert result.state == "cancelled"
        assert len(_records(repo)) == 1
        with pytest.raises(JobNotResumable):
            resume_job(result.job_id, repo_path=repo, sleep=_no_sleep)

    def test_ctrl_c_pauses_and_resume_finishes(self, homes, hist_repo, monkeypatch):
        _three_sessions(homes, hist_repo)
        repo = hist_repo["repo"]
        real = jobs_mod._process_object
        calls = {"n": 0}

        def interrupt(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real(*a, **kw)

        monkeypatch.setattr(jobs_mod, "_process_object", interrupt)
        result = _run(repo, sources=["claude-code"])
        assert result.state == "paused"
        monkeypatch.setattr(jobs_mod, "_process_object", real)
        resumed = resume_job(result.job_id, repo_path=repo, sleep=_no_sleep)
        assert resumed.state == "completed"
        assert len(_records(repo)) == 3

    def test_transient_connector_error_is_retried(self, homes, hist_repo, monkeypatch):
        from openshard.ingest.connectors.base import ConnectorIOError

        write_claude(homes, hist_repo)
        real_open = LocalAgentHistoryConnector.open
        fails = {"n": 2}

        def flaky(self, obj):
            if fails["n"]:
                fails["n"] -= 1
                raise ConnectorIOError("transient")
            return real_open(self, obj)

        monkeypatch.setattr(LocalAgentHistoryConnector, "open", flaky)
        slept: list[float] = []
        result = run_job(JobSpec(sources=["claude-code"]), repo_path=hist_repo["repo"], sleep=slept.append)
        assert result.counters.written == 1 and result.counters.retries == 2
        assert slept == [1.0, 2.0]

    def test_persistent_io_error_fails_the_item_not_the_job(self, homes, hist_repo, monkeypatch):
        from openshard.ingest.connectors.base import ConnectorIOError

        write_claude(homes, hist_repo)

        def broken(self, obj):
            raise ConnectorIOError("down")

        monkeypatch.setattr(LocalAgentHistoryConnector, "open", broken)
        result = _run(hist_repo["repo"], sources=["claude-code"])
        assert result.state == "completed" and result.counters.failed == 1

    def test_parse_error_is_quarantined_without_content(self, homes, hist_repo):
        write_claude(homes, hist_repo, sid="junk", text="SECRET_JUNK_MARKER not json\n")
        write_claude(homes, hist_repo)
        repo = hist_repo["repo"]
        result = _run(repo, sources=["claude-code"])
        assert result.counters.quarantined == 1 and result.counters.written == 1
        status = job_status(result.job_id, repo_path=repo)
        (q,) = status["items"]["quarantined"]
        assert q["error_class"] == "unrecognized_format"
        assert q["locator_display"] == "claude-code:junk.jsonl"
        assert "SECRET_JUNK_MARKER" not in _all_openshard_text(repo)

    def test_active_job_lock_prevents_a_second_job(self, homes, hist_repo):
        repo = hist_repo["repo"]
        with LocalJobStore(repo).active_lock():
            with pytest.raises(ActiveJobError):
                _run(repo, sources=["claude-code"])

    def test_possibly_live_sources_are_skipped(self, homes, hist_repo):
        write_claude(homes, hist_repo, mtime=time.time())
        result = _run(hist_repo["repo"], sources=["claude-code"])
        assert result.counters.written == 0
        assert result.counters.filtered_by_reason == {"possibly_live": 1}

    def test_since_until_filter_on_session_start(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        result = _run(hist_repo["repo"], since="2026-03-02")
        assert result.counters.written == 1
        assert result.counters.filtered_by_reason == {"before_since": 1}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_other_repo_is_skipped_by_default_and_all_repos_needs_opt_in(self, homes, hist_repo, tmp_path):
        other = (tmp_path / "other").resolve()
        other.mkdir()
        _git(other, "init", "-q", "-b", "main")
        text = _render((FIXTURES / "claude_code" / "session.jsonl.tmpl").read_text(encoding="utf-8"),
                       other, sid=CLAUDE_SID2)
        write_claude(homes, hist_repo, sid=CLAUDE_SID2, text=text)
        repo = hist_repo["repo"]
        r1 = _run(repo, sources=["claude-code"])
        assert r1.counters.filtered_by_reason == {"other_repository": 1}
        r2 = _run(repo, sources=["claude-code"], all_repos=True)
        assert r2.counters.filtered_by_reason == {"repository_without_openshard": 1}
        (other / ".openshard").mkdir()
        r3 = _run(repo, sources=["claude-code"], all_repos=True)
        assert r3.counters.written == 1
        assert len(_records(other)) == 1

    def test_home_directory_repository_is_refused(self, homes, hist_repo, monkeypatch):
        repo = hist_repo["repo"]
        write_claude(homes, hist_repo)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: repo))
        result = _run(repo, sources=["claude-code"])
        assert result.counters.filtered_by_reason == {"home_directory_repository_refused": 1}
        allowed = _run(repo, sources=["claude-code"], allow_home_repo=True)
        assert allowed.counters.written == 1

    def test_missing_cwd_is_filtered(self, homes, hist_repo, tmp_path):
        gone = tmp_path / "deleted-project"
        text = _render((FIXTURES / "claude_code" / "session.jsonl.tmpl").read_text(encoding="utf-8"),
                       gone, sid=CLAUDE_SID2)
        write_claude(homes, hist_repo, sid=CLAUDE_SID2, text=text)
        result = _run(hist_repo["repo"], sources=["claude-code"])
        assert result.counters.filtered_by_reason == {"cwd_no_longer_exists": 1}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def _invoke(self, *args):
        result = CliRunner().invoke(cli, ["ingest", *args], catch_exceptions=False)
        return result

    def test_sources_scan_run_status_list(self, homes, hist_repo):
        write_claude(homes, hist_repo)
        write_codex(homes, hist_repo)
        repo = str(hist_repo["repo"])
        src = json.loads(self._invoke("sources", "--repo-path", repo, "--json").output)
        by = {s["source"]: s for s in src["sources"]}
        assert by["claude-code"]["sessions"] == 1 and by["claude-code"]["in_this_repo"] == 1
        assert by["codex"]["in_this_repo"] == 1
        sc = json.loads(self._invoke("scan", "--repo-path", repo, "--json").output)
        assert sc["decisions"] == {"write": 2}
        run = self._invoke("run", "--repo-path", repo, "--json")
        assert run.exit_code == 0, run.output
        data = json.loads(run.output)
        assert data["state"] == "completed" and data["counters"]["written"] == 2
        st = json.loads(self._invoke("status", "--repo-path", repo, "--json").output)
        assert st["job_id"] == data["job_id"] and st["state"] == "completed"
        ls = json.loads(self._invoke("list", "--repo-path", repo, "--json").output)
        assert [j["job_id"] for j in ls["jobs"]] == [data["job_id"]]
        human = self._invoke("run", "--repo-path", repo)
        assert "duplicates 2" in human.output

    def test_cancel_unknown_job_errors(self, homes, hist_repo):
        result = CliRunner().invoke(cli, ["ingest", "cancel", "ijob_nope", "--repo-path", str(hist_repo["repo"])])
        assert result.exit_code != 0

    def test_note_refuses_a_sealed_receipt(self, homes, hist_repo, monkeypatch):
        write_claude(homes, hist_repo)
        _run(hist_repo["repo"], sources=["claude-code"])
        monkeypatch.chdir(hist_repo["repo"])
        result = CliRunner().invoke(cli, ["note", "hello"])
        assert result.exit_code != 0
        assert "sealed" in result.output

    def test_ingest_listed_under_integrations(self):
        result = CliRunner().invoke(cli, ["--help"])
        integrations = result.output.split("Integrations:")[1].split("Advanced:")[0]
        assert "ingest" in integrations
