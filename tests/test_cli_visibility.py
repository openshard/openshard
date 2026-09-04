"""CLI tests for the PR9 local visibility loop:

    openshard last / history / context / stats

Covers repo-root vs nested-subdirectory resolution, no-history state, partial
model/cost/token data, verified vs unverified receipts, estimated-cost
labelling, JSON envelopes, privacy (no raw content or absolute paths), repo
isolation, and compatibility of the existing `last` views.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.jsonl_store import write_jsonl

T1 = "2026-08-01T10:00:00Z"
T2 = "2026-08-02T10:00:00Z"
T3 = "2026-08-03T10:00:00Z"
T4 = "2026-08-04T10:00:00Z"

SECRET_TOKENS = (
    "secret-transcript-token", "secret-note-token", "secret-assistant-token",
    "secret-tool-output-token", "SECRET_ENV_VALUE", "secret-folder",
)

NATIVE_PASSED = {
    "schema_version": "1.2", "shard_id": "shard-native-1", "timestamp": T1, "run_id": T1,
    "task": "add JWT auth to the API", "workflow": "native", "executor": "native",
    "execution_profile": "native_deep", "execution_model": "anthropic/claude-sonnet-4-6",
    "retry_triggered": False, "duration_seconds": 12.5, "estimated_cost": 0.0123,
    "files_created": 1, "files_updated": 0, "files_deleted": 0,
    "files_detail": [{"path": "api/auth.py", "change_type": "create", "summary": ""}],
    "verification_attempted": True, "verification_passed": True,
    "summary": "Added JWT auth and verified it.", "repo_name": "widget",
}
NATIVE_FAILED = {
    "schema_version": "1.2", "shard_id": "shard-native-2", "timestamp": T2, "run_id": T2,
    "task": "fix flaky terraform validate test", "workflow": "native", "executor": "native",
    "execution_model": "deepseek/deepseek-v4-pro", "retry_triggered": False,
    "duration_seconds": 30.0, "estimated_cost": 0.0040,
    "files_created": 0, "files_updated": 1, "files_deleted": 0,
    "files_detail": [{"path": "infra/main.tf", "change_type": "update", "summary": ""}],
    "verification_attempted": True, "verification_passed": False,
    "review_checks": [{"name": "terraform_validate", "status": "failed", "summary": "missing provider"}],
    "findings": [{"severity": "High", "message": "terraform validate failed: missing provider block",
                  "path": "infra/main.tf"}],
    "agent_notes": ["secret-note-token"],
    "summary": "Verification failed.", "repo_name": "widget",
}
HOOKS_FULL = {
    "schema_version": "1.2", "shard_id": "shard-hooks-1", "timestamp": T3, "run_id": T3 + "-abcd1234",
    "attempt_number": 1, "task": "refactor auth middleware", "executor": "claude_code_hooks",
    "import_source": "claude_code", "import_method": "hooks", "files_source": "git_diff_inferred",
    "execution_model": "claude-sonnet-4-6", "estimated_cost": 0.42, "cost_provenance": "provider_reported",
    "prompt_tokens": 14000, "completion_tokens": 2500, "total_tokens": 16500,
    "cache_creation_tokens": 0, "cache_read_tokens": 30000, "tokens_provenance": "provider_reported",
    "duration_seconds": 95.0,
    "files_created": 0, "files_updated": 2, "files_deleted": 0,
    "files_detail": [{"path": "api/middleware.py", "change_type": "update", "summary": "git diff"},
                     {"path": "api/auth.py", "change_type": "update", "summary": "git diff"}],
    "verification_attempted": False, "verification_passed": None,
    "git_branch": "main", "git_dirty": True,
    "summary": "Claude Code session: 2 file(s) changed, 6 tool call(s). 1 prompt(s), 1 turn(s) completed, observed via hooks.",
    "capture": {"source": "claude_code_hooks", "session_id": "sess-1", "status": "in_progress",
                "session_end_observed": False, "prompt_count": 1, "turn_count": 1, "tool_call_count": 6,
                "task_status": "turn_completed", "models_seen": ["claude-sonnet-4-6"],
                "model_source": "status_line"},
    "events": [
        {"schema_version": 1, "event_id": "e1", "event_type": "tool.invoked", "occurred_at": T3,
         "run_id": T3 + "-abcd1234", "shard_id": "shard-hooks-1", "attempt_number": 1,
         "actor": "claude_code", "source": "claude_code_hooks", "action": "Edit: api/auth.py",
         "target": "api/auth.py", "status": "unknown", "evidence": "agent_reported",
         "raw_content_stored": False, "metadata": {"hook": "PostToolUse", "tool": "Edit"}},
    ],
    "transcript": "secret-transcript-token", "assistant_response": "secret-assistant-token",
    "tool_output": "secret-tool-output-token", "env": {"API_KEY": "SECRET_ENV_VALUE"},
    "workspace_path": "C:/Users/private/secret-folder/widget", "repo_name": "widget",
}
HOOKS_UNKNOWN = {
    "schema_version": "1.2", "shard_id": "shard-hooks-2", "timestamp": T4, "run_id": T4 + "-ef567890",
    "attempt_number": 1, "task": "investigate slow query", "executor": "claude_code_hooks",
    "import_source": "claude_code", "import_method": "hooks", "files_source": "not_available",
    "execution_model": "unknown",
    "files_created": 0, "files_updated": 0, "files_deleted": 0, "files_detail": [],
    "verification_attempted": False, "verification_passed": None,
    "summary": "Claude Code session: 0 file(s) changed, 3 tool call(s). 1 prompt(s), in progress, observed via hooks.",
    "capture": {"source": "claude_code_hooks", "session_id": "sess-2", "status": "in_progress",
                "task_status": "in_progress", "models_seen": [], "model_source": "not_captured"},
    "repo_name": "widget",
}
ALL = [NATIVE_PASSED, NATIVE_FAILED, HOOKS_FULL, HOOKS_UNKNOWN]


def _repo(root: Path, entries: list[dict] | None, name: str = "widget") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    if entries is not None:
        write_jsonl(repo / ".openshard" / "runs.jsonl", entries)
    return repo


def _invoke(args: list[str], cwd: Path):
    runner = CliRunner()
    with patch.object(Path, "cwd", return_value=cwd):
        return runner.invoke(cli, args)


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path, ALL)


@pytest.fixture
def nested(repo: Path) -> Path:
    deep = repo / "src" / "pkg" / "deeper"
    deep.mkdir(parents=True)
    return deep


# ---------------------------------------------------------------------------
# openshard last
# ---------------------------------------------------------------------------


class TestLast:
    def test_from_repo_root(self, repo: Path):
        out = _ok(_invoke(["last"], repo))
        assert "RECEIPT" in out
        assert "investigate slow query" in out
        assert "History read from the repository root" not in out

    def test_from_nested_subdirectory_reads_same_history(self, repo: Path, nested: Path):
        out = _ok(_invoke(["last"], nested))
        assert "investigate slow query" in out
        assert "History read from the repository root (widget)." in out

    def test_json_from_nested_has_repo_block_without_absolute_path(self, repo: Path, nested: Path):
        out = _ok(_invoke(["last", "--json"], nested))
        payload = json.loads(out)
        assert payload["status"] == "ok"
        assert payload["shard_id"] == "shard-hooks-2"
        assert payload["repo"]["name"] == "widget"
        assert payload["repo"]["history"] == ".openshard/runs.jsonl"
        assert payload["repo"]["from_subdirectory"] is True
        assert str(repo) not in out and str(nested) not in out

    def test_unknown_model_and_missing_cost_are_said_plainly(self, repo: Path):
        out = _ok(_invoke(["last"], repo))
        assert "Unknown" in out          # model unknown, never guessed
        assert "Not recorded" in out     # cost not recorded
        assert "partial" in out          # capture depth
        assert "In progress" in out      # turn status, not verification

    def test_full_capture_receipt_shows_tokens_est_cost_and_evidence(self, tmp_path: Path):
        repo = _repo(tmp_path, [HOOKS_FULL])
        out = _ok(_invoke(["last"], repo))
        assert "$0.42 est." in out
        assert "14k input / 2.5k output" in out
        assert "Completed" in out
        assert "Evidence" in out
        assert "api/middleware.py" in out

    def test_no_history_state(self, tmp_path: Path):
        repo = _repo(tmp_path, None)
        sub = repo / "sub"
        sub.mkdir()
        out = _ok(_invoke(["last"], sub))
        assert "No run history found" in out
        assert "openshard setup" in out
        payload = json.loads(_ok(_invoke(["last", "--json"], repo)))
        assert payload["status"] == "not_found"
        assert payload["repo"]["name"] == "widget"

    def test_existing_default_and_more_views_still_render(self, tmp_path: Path):
        repo = _repo(tmp_path, [NATIVE_PASSED])
        out = _ok(_invoke(["last"], repo))
        assert "Proof:" in out and "Time:" in out and "Cost:" in out
        more = _ok(_invoke(["last", "--more"], repo))
        assert "SHARD" in more and "shard-native-1" in more


# ---------------------------------------------------------------------------
# openshard history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_from_nested_subdirectory(self, repo: Path, nested: Path):
        out = _ok(_invoke(["history"], nested))
        assert out.startswith("Recent work")
        assert "History read from the repository root (widget)." in out
        assert "Showing 4 of 4 Shards, newest first." in out
        # Newest first.
        assert out.index("shard-hooks-2") < out.index("shard-hooks-1") < out.index("shard-native-2") < out.index("shard-native-1")

    def test_rows_show_truthful_status_checks_and_estimated_cost(self, repo: Path):
        out = _ok(_invoke(["history"], repo))
        assert "OpenShard Native" in out and "Claude Code (external)" in out
        from openshard.cli.visibility import _DOT

        assert f" {_DOT} Passed {_DOT} " in out and f" {_DOT} Failed {_DOT} " in out  # verified outcomes
        assert "Completed" in out and "In progress" in out        # turn status (not verification)
        assert "checks: not run" in out
        assert "checks: 1 failed" in out
        assert "$0.0123 est." in out and "$0.42 est." in out
        assert "cost: not recorded" in out
        assert "partial capture" in out
        assert "2 files" in out
        assert "Costs are estimates." in out

    def test_limit(self, repo: Path):
        out = _ok(_invoke(["history", "--limit", "2"], repo))
        assert "Showing 2 of 4 Shards" in out
        assert "shard-hooks-2" in out and "shard-native-1" not in out

    def test_json_envelope(self, repo: Path, nested: Path):
        payload = json.loads(_ok(_invoke(["history", "--json", "--limit", "3"], nested)))
        assert payload["schema_version"] == "1"
        assert payload["command"] == "history" and payload["status"] == "ok"
        assert payload["total_shards"] == 4 and payload["shown"] == 3
        assert payload["repo"]["name"] == "widget"
        rows = {r["shard_id"]: r for r in payload["shards"]}
        assert list(rows) == ["shard-hooks-2", "shard-hooks-1", "shard-native-2"]
        full = rows["shard-hooks-1"]
        assert full["cost"] == "$0.42 est." and full["cost_is_estimate"] is True
        assert full["cost_provenance"] == "provider_reported"
        assert full["tokens_input"] == 14000 and full["tokens_provenance"] == "provider_reported"
        assert full["task_completion"] == "Completed"
        unknown = rows["shard-hooks-2"]
        assert unknown["model"] == "Unknown" and unknown["cost"] == "Not recorded"
        assert unknown["cost_usd"] is None and unknown["tokens_input"] is None
        failed = rows["shard-native-2"]
        assert failed["has_failure"] is True and failed["attempt_count"] == 1

    def test_no_history_state(self, tmp_path: Path):
        repo = _repo(tmp_path, None)
        assert "No run history found" in _ok(_invoke(["history"], repo))
        payload = json.loads(_ok(_invoke(["history", "--json"], repo)))
        assert payload["status"] == "not_found" and payload["shards"] == []

    def test_repo_filter(self, repo: Path):
        out = _ok(_invoke(["history", "--repo", "widget"], repo))
        assert "Showing 4 of 4" in out
        out = _ok(_invoke(["history", "--repo", "gadget"], repo))
        assert "No Shards recorded for repository filter 'gadget'" in out


# ---------------------------------------------------------------------------
# openshard context
# ---------------------------------------------------------------------------


class TestContext:
    def test_from_nested_subdirectory_explains_matches(self, repo: Path, nested: Path):
        out = _ok(_invoke(["context", "fix terraform validate"], nested))
        assert out.startswith('Context for: "fix terraform validate"')
        assert "History read from the repository root (widget)." in out
        assert "1 of 4 recorded Shards matched" in out
        assert "shard-native-2" in out
        assert "Why matched" in out
        assert "task overlap: terraform, validate" in out
        assert "prior verification failure" in out
        assert "(score" in out
        assert "verification" in out.lower()
        assert "infra/main.tf" in out
        assert "[High] terraform validate failed" in out
        assert "OpenShard ran it" in out and "full capture" in out
        assert "How ranking works" in out
        # Unrelated shards are not padded in.
        assert "shard-hooks-1" not in out and "shard-native-1" not in out

    def test_external_capture_is_labelled(self, repo: Path):
        out = _ok(_invoke(["context", "refactor auth middleware"], repo))
        assert "shard-hooks-1" in out
        assert "observed externally, not executed by OpenShard" in out
        assert "partial capture" in out

    def test_no_match_is_honest(self, repo: Path):
        out = _ok(_invoke(["context", "zzz-nothing-like-this"], repo))
        assert "No relevant prior work" in out
        assert "shard-" not in out.split("How ranking works")[0]

    def test_no_task(self, repo: Path):
        out = _ok(_invoke(["context"], repo))
        assert "No task given" in out
        payload = json.loads(_ok(_invoke(["context", "--json"], repo)))
        assert payload["status"] == "no_task"

    def test_text_flag_prints_exact_agent_block(self, repo: Path, nested: Path):
        from openshard.history.query import relevant_context

        expected = relevant_context("fix terraform validate", repo_path=repo).context_text
        out = _ok(_invoke(["context", "--text", "fix terraform validate"], nested))
        assert out.strip() == expected.strip()
        assert "Why relevant:" in out

    def test_json_envelope(self, repo: Path):
        payload = json.loads(_ok(_invoke(["context", "--json", "auth"], repo)))
        assert payload["command"] == "context" and payload["status"] == "ok"
        assert payload["task"] == "auth"
        assert payload["total_shards"] == 4 and payload["matched"] == 2
        ids = [m["shard_id"] for m in payload["matches"]]
        assert set(ids) == {"shard-native-1", "shard-hooks-1"}
        m = next(x for x in payload["matches"] if x["shard_id"] == "shard-hooks-1")
        assert m["why_relevant"] == ["task overlap: auth"]
        assert m["origin"] == "external_observed" and m["capture_depth"] == "partial"
        assert payload["ranking"]["weights"] == {
            "task_text": 2, "shard_id": 1, "agent": 1, "file_touched": 6, "file_referenced": 4,
        }
        assert payload["ranking"]["bonuses"] == {
            "prior_verification_failure": 2, "multiple_attempts": 1, "resolved_after_failure": 2,
        }
        assert "context_text" in payload

    def test_json_no_match_and_not_found(self, repo: Path, tmp_path: Path):
        payload = json.loads(_ok(_invoke(["context", "--json", "zzz-nothing"], repo)))
        assert payload["status"] == "no_match" and payload["matches"] == []
        empty = _repo(tmp_path / "other", None, name="empty")
        payload = json.loads(_ok(_invoke(["context", "--json", "anything"], empty)))
        assert payload["status"] == "not_found" and payload["total_shards"] == 0

    def test_retry_history_is_shown(self, tmp_path: Path):
        first = dict(NATIVE_FAILED, shard_id="shard-multi", attempt_number=1, task="implement terraform retry")
        first.pop("review_checks")
        second = dict(NATIVE_PASSED, shard_id="shard-multi", attempt_number=2, timestamp=T3, run_id=T3,
                      task="implement terraform retry (retry)", retry_triggered=True)
        repo = _repo(tmp_path, [first, second])
        out = _ok(_invoke(["context", "terraform retry"], repo))
        assert "multiple attempts (2)" in out
        assert "Attempts" in out and "1: Failed" in out and "2: Passed" in out

    def test_recovery_observation_is_shown(self, tmp_path: Path):
        first = dict(NATIVE_FAILED, shard_id="shard-multi", attempt_number=1, task="implement terraform retry")
        first.pop("review_checks")
        second = dict(
            NATIVE_PASSED, shard_id="shard-multi", attempt_number=2, timestamp=T3, run_id=T3,
            task="implement terraform retry (retry)", retry_triggered=True,
            files_detail=[{"path": "openshard/history/query.py", "change_type": "update"}],
        )
        repo = _repo(tmp_path, [first, second])
        out = _ok(_invoke(["context", "terraform retry"], repo))
        assert "Recovery" in out
        assert "attempt 1 failed" in out and "attempt 2 passed" in out
        assert "openshard/history/query.py" in out
        payload = json.loads(_ok(_invoke(["context", "--json", "terraform retry"], repo)))
        match = next(m for m in payload["matches"] if m["shard_id"] == "shard-multi")
        assert match["recovery"]["failed_attempt_number"] == 1
        assert match["recovery"]["recovery_attempt_number"] == 2
        assert match["recovery"]["intervening_files"] == ["openshard/history/query.py"]

    def test_no_recovery_row_when_no_recovery(self, repo: Path):
        out = _ok(_invoke(["context", "fix terraform validate"], repo))
        assert "Recovery" not in out


# ---------------------------------------------------------------------------
# openshard stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_from_nested_subdirectory(self, repo: Path, nested: Path):
        out = _ok(_invoke(["stats"], nested))
        assert out.startswith("OpenShard stats")
        assert "History read from the repository root (widget)." in out
        assert "4 Shards" in out
        assert "2026-08-01" in out and "2026-08-04" in out
        assert "OpenShard Native 2" in out and "Claude Code (external) 2" in out
        assert "run by OpenShard 2" in out and "observed externally 2" in out
        assert "full 2" in out and "partial 2" in out
        assert "Claude Sonnet 4.6 2" in out and "DeepSeek V4 Pro 1" in out and "unknown 1" in out
        assert "passed 1" in out and "failed 1" in out and "not run 2" in out
        assert "completed 1" in out and "in progress 1" in out and "not verification" in out
        assert "$0.44 estimated across 3 Shards (1 agent-reported); not recorded for 1" in out
        assert "14k input / 2.5k output (+30k cache read)" in out
        assert "provider-reported for 1 Shard" in out
        assert "observed across 3 Shards" in out
        assert "Files changed" in out and "4 across 3 Shards" in out
        assert "api/auth.py" in out
        assert "not a judgement of the work" in out

    def test_no_productivity_scores(self, repo: Path):
        out = _ok(_invoke(["stats"], repo)).lower()
        for banned in ("efficiency", "productivity", "score", "saving", "%"):
            assert banned not in out, banned

    def test_json_envelope(self, repo: Path, nested: Path):
        payload = json.loads(_ok(_invoke(["stats", "--json"], nested)))
        assert payload["command"] == "stats" and payload["status"] == "ok"
        assert payload["repo"]["name"] == "widget" and payload["limit"] is None
        assert payload["shards"] == 4 and payload["attempts"] == 4
        assert payload["models"]["unknown"] == 1
        assert payload["verification"] == {"not_run": 2, "failed": 1, "passed": 1}
        assert payload["cost"]["is_estimate"] is True
        assert payload["cost"]["total_usd"] == pytest.approx(0.4363)
        assert payload["cost"]["shards_missing"] == 1
        assert payload["tokens"]["input"] == 14000 and payload["tokens"]["shards_with_tokens"] == 1
        assert payload["files"]["top"][0] == {"path": "api/auth.py", "shards": 2}

    def test_limit(self, repo: Path):
        out = _ok(_invoke(["stats", "--limit", "1"], repo))
        assert "Most recent 1 Shards only." in out
        assert "1 Shard" in out
        payload = json.loads(_ok(_invoke(["stats", "--json", "--limit", "2"], repo)))
        assert payload["limit"] == 2 and payload["shards"] == 2

    def test_no_history_state(self, tmp_path: Path):
        repo = _repo(tmp_path, None)
        assert "No run history found" in _ok(_invoke(["stats"], repo))
        payload = json.loads(_ok(_invoke(["stats", "--json"], repo)))
        assert payload["status"] == "not_found" and payload["shards"] == 0

    def test_subcommands_still_work_from_nested(self, repo: Path, nested: Path):
        out = _ok(_invoke(["stats", "completeness"], nested))
        assert "runs checked:         4" in out
        out = _ok(_invoke(["stats", "failures", "--json"], nested))
        assert json.loads(out)["runs_checked"] == 4


# ---------------------------------------------------------------------------
# privacy and repo isolation
# ---------------------------------------------------------------------------


class TestPrivacy:
    @pytest.mark.parametrize("args", [
        ["last"], ["last", "--json"], ["history"], ["history", "--json"],
        ["context", "refactor auth middleware terraform"], ["context", "--json", "refactor auth middleware terraform"],
        ["context", "--text", "refactor auth middleware terraform"], ["stats"], ["stats", "--json"],
    ])
    def test_no_raw_content_or_absolute_paths(self, repo: Path, nested: Path, args: list[str]):
        out = _ok(_invoke(args, nested))
        for token in SECRET_TOKENS:
            assert token not in out, (args, token)
        assert "C:/Users" not in out and "C:\\Users" not in out
        assert str(repo) not in out


class TestRepoIsolation:
    def test_nested_repo_sees_only_its_own_history(self, tmp_path: Path):
        outer = _repo(tmp_path, [NATIVE_PASSED], name="outer")
        inner = _repo(outer / "vendor", [HOOKS_UNKNOWN], name="inner")
        deep = inner / "lib"
        deep.mkdir()
        out = _ok(_invoke(["history"], deep))
        assert "shard-hooks-2" in out and "shard-native-1" not in out
        out = _ok(_invoke(["history"], outer / "vendor"))
        assert "shard-native-1" in out and "shard-hooks-2" not in out

    def test_sibling_repo_is_not_read(self, tmp_path: Path):
        a = _repo(tmp_path, [NATIVE_PASSED], name="a")
        _repo(tmp_path, [HOOKS_UNKNOWN], name="b")
        sub = a / "src"
        sub.mkdir()
        out = _ok(_invoke(["stats", "--json"], sub))
        payload = json.loads(out)
        assert payload["shards"] == 1 and payload["repo"]["name"] == "a"

    def test_stray_history_dir_in_subdir_does_not_shadow_repo_root(self, tmp_path: Path):
        repo = _repo(tmp_path, ALL)
        stray = repo / "pkg"
        (stray / ".openshard").mkdir(parents=True)
        out = _ok(_invoke(["last"], stray))
        assert "investigate slow query" in out
        assert "History read from the repository root (widget)." in out


class TestSetupDoctorRegression:
    """`setup` / `doctor` keep their own `find_repo_root`; they must keep working
    from a nested directory alongside the new history resolver."""

    @staticmethod
    def _no_claude():
        from openshard.adapters.claude_mcp_install import ClaudeCliAvailability

        # claude_setup binds detect_claude_cli by name at import time, so the
        # patch has to target that binding, not the defining module.
        return patch(
            "openshard.adapters.claude_setup.detect_claude_cli",
            return_value=ClaudeCliAvailability(available=False, path=None, reason="not found"),
        )

    def test_doctor_json_still_runs_from_nested_dir(self, repo: Path, nested: Path):
        import os

        no_keys = {"OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "OPENSHARD_CONFIG": ""}
        with patch.dict(os.environ, no_keys, clear=False), self._no_claude():
            result = _invoke(["doctor", "--json"], nested)
        assert result.exit_code == 0, result.output
        state = json.loads(result.output)
        assert state["claude_code"]["claude_cli_available"] is False
        assert state["claude_code"]["repo_root"] is not None

    def test_setup_agent_snapshot_still_runs(self, repo: Path):
        with self._no_claude():
            result = _invoke(["setup", "--agent"], repo)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["mode"] == "agent" and "claude_code" in payload
