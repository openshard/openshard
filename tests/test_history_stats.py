"""Tests for openshard.history.stats and history.query.recent_shards (PR9)."""

from __future__ import annotations

from pathlib import Path

from openshard.history.jsonl_store import write_jsonl
from openshard.history.query import recent_shards
from openshard.history.stats import MODEL_UNKNOWN, compute_history_stats

T1 = "2026-08-01T10:00:00Z"
T2 = "2026-08-02T10:00:00Z"
T3 = "2026-08-03T10:00:00Z"
T4 = "2026-08-04T10:00:00Z"


def _native(task: str, ts: str, **kw) -> dict:
    base = {
        "schema_version": "1.2", "timestamp": ts, "run_id": ts, "task": task,
        "workflow": "native", "executor": "native", "retry_triggered": False,
        "verification_attempted": True, "verification_passed": True,
        "execution_model": "anthropic/claude-sonnet-4-6",
        "estimated_cost": 0.01, "duration_seconds": 10.0,
        "files_created": 1, "files_updated": 0, "files_deleted": 0,
        "files_detail": [{"path": "a.py", "change_type": "create", "summary": ""}],
        "summary": "done", "repo_name": "alpha",
    }
    base.update(kw)
    return base


def _hooks(task: str, ts: str, **kw) -> dict:
    base = {
        "schema_version": "1.2", "timestamp": ts, "run_id": ts, "task": task,
        "executor": "claude_code_hooks", "import_source": "claude_code",
        "execution_model": "unknown",
        "verification_attempted": False, "verification_passed": None,
        "files_created": 0, "files_updated": 0, "files_deleted": 0, "files_detail": [],
        "summary": "Claude Code session: 0 file(s) changed, 3 tool call(s).",
        "capture": {"source": "claude_code_hooks", "task_status": "turn_completed"},
    }
    base.update(kw)
    return base


def _write(repo: Path, entries: list[dict]) -> None:
    write_jsonl(repo / ".openshard" / "runs.jsonl", entries)


def _stats(repo: Path, limit=None):
    page = recent_shards(limit=limit, repo_path=repo)
    return compute_history_stats(page.items, total_attempts=page.total_attempts if limit is None else None), page


class TestRecentShards:
    def test_empty(self, tmp_path: Path):
        page = recent_shards(repo_path=tmp_path)
        assert page.total_shards == 0 and page.total_attempts == 0 and page.items == []

    def test_totals_count_whole_history_even_when_limited(self, tmp_path: Path):
        _write(tmp_path, [
            _native("a", T1, shard_id="s1"),
            _native("b", T2, shard_id="s2", attempt_number=1),
            _native("b retry", T3, shard_id="s2", attempt_number=2, retry_triggered=True),
            _native("c", T4, shard_id="s3"),
        ])
        page = recent_shards(limit=1, repo_path=tmp_path)
        assert page.total_shards == 3
        assert page.total_attempts == 4
        assert [i.shard.shard_id for i in page.items] == ["s3"]

    def test_attempt_count_and_failure_flag(self, tmp_path: Path):
        _write(tmp_path, [
            _native("b", T1, shard_id="s2", attempt_number=1, verification_passed=False),
            _native("b retry", T2, shard_id="s2", attempt_number=2, retry_triggered=True),
        ])
        item = recent_shards(repo_path=tmp_path).items[0]
        assert item.attempt_count == 2
        assert item.has_failure is True
        assert item.receipt.status == "Passed"  # latest attempt

    def test_limit_none_returns_everything(self, tmp_path: Path):
        _write(tmp_path, [_native(f"t{i}", f"2026-08-{i+1:02d}T10:00:00Z", shard_id=f"s{i}") for i in range(25)])
        page = recent_shards(limit=None, repo_path=tmp_path)
        assert len(page.items) == 25

    def test_zero_limit_gives_totals_only(self, tmp_path: Path):
        _write(tmp_path, [_native("a", T1, shard_id="s1")])
        page = recent_shards(limit=0, repo_path=tmp_path)
        assert page.total_shards == 1 and page.items == []


class TestComputeHistoryStats:
    def test_empty_stats(self):
        stats = compute_history_stats([])
        assert stats.shards == 0
        assert stats.cost_total_usd is None
        assert stats.tokens_input is None
        assert stats.to_dict()["cost"]["is_estimate"] is True

    def test_counts_are_honest(self, tmp_path: Path):
        _write(tmp_path, [
            _native("add auth", T1, shard_id="n1"),
            _native("fix test", T2, shard_id="n2", verification_passed=False,
                    estimated_cost=None, files_detail=[{"path": "a.py", "change_type": "update"},
                                                       {"path": "b.py", "change_type": "update"}],
                    files_updated=2, files_created=0),
            _hooks("refactor", T3, shard_id="h1", estimated_cost=0.5, cost_provenance="provider_reported",
                   prompt_tokens=1000, completion_tokens=200, cache_read_tokens=300,
                   tokens_provenance="provider_reported", execution_model="claude-sonnet-4-6",
                   duration_seconds=90.0),
            _hooks("investigate", T4, shard_id="h2"),
        ])
        stats, page = _stats(tmp_path)
        assert stats.shards == 4 and stats.attempts == 4 and stats.retried_shards == 0
        assert stats.first_at == T1 and stats.last_at == T4
        assert stats.agents == {"OpenShard Native": 2, "Claude Code (external)": 2}
        assert stats.origins == {"external_observed": 2, "openshard_routed": 2}
        assert stats.capture_depths == {"full": 2, "partial": 2}
        # Models: native both Claude Sonnet 4.6, one hooks Claude Sonnet 4.6, one unknown.
        assert stats.models == {"Claude Sonnet 4.6": 3, MODEL_UNKNOWN: 1}
        assert list(stats.models)[-1] == MODEL_UNKNOWN
        assert stats.verification == {"not_run": 2, "failed": 1, "passed": 1}
        assert stats.task_completion == {"completed": 2}
        # Cost: n1 0.01 + h1 0.5; n2 and h2 missing.
        assert stats.cost_total_usd == 0.51
        assert stats.cost_shards == 2 and stats.cost_missing_shards == 2
        assert stats.cost_provider_reported_shards == 1
        # Tokens only from the provider-reported hooks entry.
        assert (stats.tokens_input, stats.tokens_output, stats.tokens_cache_read) == (1000, 200, 300)
        assert stats.tokens_shards == 1
        assert stats.duration_total_seconds == 110.0 and stats.duration_shards == 3
        assert stats.files_changed_total == 3 and stats.files_changed_shards == 2
        assert stats.top_files == [("a.py", 2), ("b.py", 1)]

    def test_bare_token_counts_without_provenance_are_not_summed(self, tmp_path: Path):
        _write(tmp_path, [_native("a", T1, shard_id="n1", prompt_tokens=999, completion_tokens=1)])
        stats, _ = _stats(tmp_path)
        assert stats.tokens_shards == 0 and stats.tokens_input is None

    def test_multi_model_shard_counts_under_each_model(self, tmp_path: Path):
        _write(tmp_path, [_native("a", T1, shard_id="n1", stage_runs=[
            {"stage_type": "planning", "model": "anthropic/claude-opus-4-7", "duration": 1.0, "cost": 0.1},
            {"stage_type": "implementation", "model": "deepseek/deepseek-v4-pro", "duration": 1.0, "cost": 0.1},
        ])])
        stats, _ = _stats(tmp_path)
        assert stats.models == {"Claude Opus 4.7": 1, "DeepSeek V4 Pro": 1}

    def test_to_dict_shape(self, tmp_path: Path):
        _write(tmp_path, [_native("a", T1, shard_id="n1")])
        stats, _ = _stats(tmp_path)
        d = stats.to_dict()
        assert set(d) == {
            "shards", "attempts", "retried_shards", "first_at", "last_at", "agents", "origins",
            "capture_depths", "models", "verification", "task_completion", "cost", "tokens",
            "duration", "files",
        }
        assert d["cost"]["is_estimate"] is True
        assert d["tokens"]["provenance"] == "provider_reported"
        assert d["files"]["top"] == [{"path": "a.py", "shards": 1}]
