"""Tests for openshard.mcp.server (Demo v1 PR2: local read-only MCP server).

Exercises tools through the MCP tool-call boundary (``FastMCP.call_tool``)
rather than the private ``_shard_dict``/``_receipt_dict`` helpers directly,
so a regression in tool registration, argument validation, or MCP-level
JSON conversion would be caught here too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openshard.history.jsonl_store import write_jsonl
from openshard.mcp.server import DEFAULT_LIMIT, MAX_LIMIT, build_server

pytest.importorskip("mcp")

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(task: str, ts: str, **kwargs) -> dict:
    base: dict = {
        "schema_version": "1.2",
        "timestamp": ts,
        "run_id": ts,
        "task": task,
        "workflow": "native",
        "executor": "native",
        "retry_triggered": False,
        "verification_attempted": True,
        "verification_passed": True,
        "summary": "done",
        "repo_name": "alpha",
    }
    base.update(kwargs)
    return base


def _write(repo_path: Path, entries: list[dict]) -> None:
    write_jsonl(repo_path / ".openshard" / "runs.jsonl", entries)


def _call(server, name: str, args: dict):
    """Call a tool through the MCP boundary; return (content_blocks, structured)."""
    return asyncio.run(server.call_tool(name, args))


T1 = "2026-08-01T10:00:00Z"
T2 = "2026-08-02T10:00:00Z"
T3 = "2026-08-03T10:00:00Z"


@pytest.fixture
def history(tmp_path: Path) -> Path:
    _write(tmp_path, [
        _entry("add JWT auth", T1, shard_id="shard-a", repo_name="alpha"),
        _entry("fix flaky test", T2, shard_id="shard-b", repo_name="beta",
               repo_identity="github.com/acme/beta"),
        _entry("refactor db layer", T3, shard_id="shard-c", repo_name="alpha",
               repo_identity="github.com/acme/alpha"),
    ])
    return tmp_path


@pytest.fixture
def multi_attempt(tmp_path: Path) -> Path:
    _write(tmp_path, [
        _entry("add JWT auth", T1, shard_id="shard-multi", attempt_number=1,
               verification_passed=False),
        _entry("unrelated task", T2, shard_id="shard-solo", attempt_number=1),
        _entry("add JWT auth (retry)", T3, shard_id="shard-multi", attempt_number=2,
               retry_triggered=True, verification_passed=True),
    ])
    return tmp_path


@pytest.fixture
def server(tmp_path: Path):
    return build_server(repo_path=tmp_path)


# ---------------------------------------------------------------------------
# Server / tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_exposes_exactly_the_expected_tools(self, server):
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert names == {
            "recent_shards", "get_shard", "get_receipt", "search_history", "relevant_context",
        }

    def test_each_tool_has_a_description(self, server):
        tools = asyncio.run(server.list_tools())
        for t in tools:
            assert t.description and len(t.description) > 10

    def test_get_shard_schema_requires_shard_id(self, server):
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        schema = tools["get_shard"].inputSchema
        assert "shard_id" in schema.get("required", [])

    def test_get_receipt_schema_has_no_required_fields(self, server):
        """Both shard_id and run_id are optional at the schema level; the
        'at least one' rule is enforced at call time (see TestGetReceipt)."""
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        schema = tools["get_receipt"].inputSchema
        assert not schema.get("required")


# ---------------------------------------------------------------------------
# recent_shards
# ---------------------------------------------------------------------------


class TestRecentShards:
    def test_empty_history_returns_empty_list(self, server):
        _, structured = _call(server, "recent_shards", {})
        assert structured["result"] == []

    def test_newest_first(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {})
        ids = [s["shard_id"] for s in structured["result"]]
        assert ids == ["shard-c", "shard-b", "shard-a"]

    def test_limit(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {"limit": 2})
        assert len(structured["result"]) == 2

    def test_default_limit_used_when_omitted(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {})
        assert len(structured["result"]) <= DEFAULT_LIMIT

    def test_non_positive_limit_returns_empty(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {"limit": 0})
        assert structured["result"] == []
        _, structured = _call(server, "recent_shards", {"limit": -5})
        assert structured["result"] == []

    def test_huge_limit_is_clamped_not_rejected(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {"limit": 10_000_000})
        # Clamped server-side to MAX_LIMIT; still bounded by actual history size.
        assert len(structured["result"]) == 3
        assert MAX_LIMIT < 10_000_000

    def test_repo_filter(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {"repo": "alpha"})
        ids = {s["shard_id"] for s in structured["result"]}
        assert ids == {"shard-a", "shard-c"}

    def test_repo_filter_no_match(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {"repo": "does-not-exist"})
        assert structured["result"] == []

    def test_shard_fields_are_bounded_identity_only(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "recent_shards", {"limit": 1})
        shard = structured["result"][0]
        assert set(shard) == {
            "shard_id", "created_at", "task_short", "task_full",
            "agent", "origin", "capture_depth",
        }

    def test_multi_attempt_shard_listed_once_with_latest_state(self, multi_attempt: Path):
        server = build_server(repo_path=multi_attempt)
        _, structured = _call(server, "recent_shards", {})
        by_id = {s["shard_id"]: s for s in structured["result"]}
        assert list(by_id).count("shard-multi") == 0 or len(structured["result"]) == 2
        assert by_id["shard-multi"]["task_full"] == "add JWT auth (retry)"
        assert by_id["shard-multi"]["created_at"] == T3


# ---------------------------------------------------------------------------
# get_shard
# ---------------------------------------------------------------------------


class TestGetShard:
    def test_found(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "get_shard", {"shard_id": "shard-b"})
        assert structured["shard_id"] == "shard-b"
        assert structured["task_short"] == "fix flaky test"
        assert structured["agent"] == "OpenShard Native"

    def test_multi_attempt_returns_latest_attempt_state(self, multi_attempt: Path):
        server = build_server(repo_path=multi_attempt)
        _, structured = _call(server, "get_shard", {"shard_id": "shard-multi"})
        assert structured["task_full"] == "add JWT auth (retry)"
        assert structured["created_at"] == T3

    def test_unknown_shard_raises_clean_tool_error(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(ToolError) as exc_info:
            _call(server, "get_shard", {"shard_id": "does-not-exist"})
        msg = str(exc_info.value)
        assert "does-not-exist" in msg
        assert "Traceback" not in msg
        assert str(history) not in msg

    def test_unknown_shard_on_empty_history(self, tmp_path: Path):
        server = build_server(repo_path=tmp_path)
        with pytest.raises(ToolError):
            _call(server, "get_shard", {"shard_id": "anything"})

    def test_empty_shard_id_rejected(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(ToolError):
            _call(server, "get_shard", {"shard_id": "   "})

    def test_malformed_argument_type_rejected(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(Exception):
            _call(server, "get_shard", {"shard_id": 12345})

    def test_missing_required_argument_rejected(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(Exception):
            _call(server, "get_shard", {})


# ---------------------------------------------------------------------------
# get_receipt
# ---------------------------------------------------------------------------


class TestGetReceipt:
    def test_by_shard_id(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "get_receipt", {"shard_id": "shard-a"})
        assert structured["shard_id"] == "shard-a"
        assert structured["run_id"] == T1
        assert structured["status"] == "Passed"

    def test_by_run_id(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "get_receipt", {"run_id": T2})
        assert structured["shard_id"] == "shard-b"
        assert structured["run_id"] == T2

    def test_shard_and_run_id_together(self, multi_attempt: Path):
        server = build_server(repo_path=multi_attempt)
        _, structured = _call(
            server, "get_receipt", {"shard_id": "shard-multi", "run_id": T1}
        )
        assert structured["attempt_number"] == 1
        assert structured["status"] == "Failed"

    def test_run_id_not_under_shard_raises(self, multi_attempt: Path):
        server = build_server(repo_path=multi_attempt)
        with pytest.raises(ToolError):
            _call(server, "get_receipt", {"shard_id": "shard-multi", "run_id": T2})

    def test_multi_attempt_defaults_to_latest_attempt(self, multi_attempt: Path):
        server = build_server(repo_path=multi_attempt)
        _, structured = _call(server, "get_receipt", {"shard_id": "shard-multi"})
        assert structured["attempt_number"] == 2
        assert structured["run_id"] == T3
        assert structured["status"] == "Passed"

    def test_neither_id_raises(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(ToolError):
            _call(server, "get_receipt", {})

    def test_unknown_shard_raises(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(ToolError):
            _call(server, "get_receipt", {"shard_id": "shard-zzz"})

    def test_unknown_run_raises(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(ToolError):
            _call(server, "get_receipt", {"run_id": "2030-01-01T00:00:00Z"})

    def test_receipt_fields_present_and_bounded(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "get_receipt", {"shard_id": "shard-a"})
        expected_keys = {
            "shard_id", "run_id", "attempt_number", "created_at", "task_short",
            "task_full", "agent", "origin", "capture_depth", "model",
            "model_stages", "strategy", "risk", "sandbox", "files_changed",
            "files", "diff_added", "diff_removed", "checks", "status",
            "verification_status", "verification_reason",
            "verification_returncode", "verification_duration_seconds",
            "approval", "cost", "result", "repo", "branch", "git_state",
            "duration_seconds", "context_quality", "findings",
        }
        assert set(structured) == expected_keys


# ---------------------------------------------------------------------------
# search_history
# ---------------------------------------------------------------------------


class TestSearchHistory:
    def test_task_match(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "search_history", {"query": "flaky"})
        assert [h["shard_id"] for h in structured["result"]] == ["shard-b"]
        assert "task_short" in structured["result"][0]["matched_fields"]

    def test_empty_query_returns_empty(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "search_history", {"query": ""})
        assert structured["result"] == []

    def test_missing_required_query_argument_rejected(self, history: Path):
        server = build_server(repo_path=history)
        with pytest.raises(Exception):
            _call(server, "search_history", {})

    def test_no_match_returns_empty(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "search_history", {"query": "zzz-nope"})
        assert structured["result"] == []

    def test_repo_filter(self, history: Path):
        server = build_server(repo_path=history)
        _write(history, [
            _entry("add auth", T1, shard_id="s1", repo_name="alpha"),
            _entry("add auth", T2, shard_id="s2", repo_name="beta"),
        ])
        _, structured = _call(server, "search_history", {"query": "auth", "repo": "beta"})
        assert [h["shard_id"] for h in structured["result"]] == ["s2"]

    def test_limit(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "search_history", {"query": "a", "limit": 1})
        assert len(structured["result"]) == 1

    def test_multi_attempt_shard_hit_once(self, multi_attempt: Path):
        server = build_server(repo_path=multi_attempt)
        _, structured = _call(server, "search_history", {"query": "jwt"})
        assert [h["shard_id"] for h in structured["result"]] == ["shard-multi"]

    def test_hit_carries_status_and_score(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "search_history", {"query": "jwt"})
        hit = structured["result"][0]
        assert hit["status"] == "Passed"
        assert hit["score"] >= 1


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class TestJsonSerialization:
    def test_text_content_matches_structured_content_for_receipt(self, history: Path):
        server = build_server(repo_path=history)
        content, structured = _call(server, "get_receipt", {"shard_id": "shard-a"})
        assert len(content) == 1
        parsed = json.loads(content[0].text)
        assert parsed == structured

    def test_text_content_matches_structured_content_for_list(self, history: Path):
        """List-returning tools emit one TextContent block per item (FastMCP's
        default), so the *set* of parsed blocks must match structured["result"]."""
        server = build_server(repo_path=history)
        content, structured = _call(server, "recent_shards", {})
        parsed = [json.loads(block.text) for block in content]
        assert parsed == structured["result"]

    def test_receipt_round_trips_through_json_dumps(self, history: Path):
        server = build_server(repo_path=history)
        _, structured = _call(server, "get_receipt", {"shard_id": "shard-a"})
        # Every value must already be JSON-serializable (no dataclasses/objects).
        json.dumps(structured)


# ---------------------------------------------------------------------------
# Privacy: forbidden fields never leak through the MCP boundary
# ---------------------------------------------------------------------------

_SECRET_MARKERS = (
    "secret-note-token",
    "secret-prompt-token",
    "secret-transcript-token",
    "secret-stdout-token",
    "secret-stderr-token",
)


@pytest.fixture
def leaky_history(tmp_path: Path) -> Path:
    _write(tmp_path, [_entry(
        # "summary" (-> receipt.result) and "agent_notes" (-> Note-severity
        # findings) are deliberately NOT secret markers here: both are already
        # folded into canonical, bounded receipt fields (result/findings) that
        # the CLI itself displays at --full detail -- not raw prompt/
        # transcript/stdout/stderr, so they are expected to surface via MCP.
        "plain task", T1, shard_id="s1",
        summary="done",
        notes=["secret-note-token"],
        agent_notes=["agent note text"],
        raw_prompt="secret-prompt-token",
        transcript="secret-transcript-token",
        adapter_stdout_summary="secret-stdout-token",
        adapter_stderr_summary="secret-stderr-token",
        workspace_path="C:/Users/private/secret-folder",
    )])
    return tmp_path


def _blob(content) -> str:
    return "".join(block.text for block in content)


class TestPrivacyBoundary:
    def test_recent_shards_never_leaks_forbidden_fields(self, leaky_history: Path):
        server = build_server(repo_path=leaky_history)
        content, structured = _call(server, "recent_shards", {})
        blob = _blob(content) + json.dumps(structured)
        for marker in _SECRET_MARKERS:
            assert marker not in blob

    def test_get_receipt_never_leaks_forbidden_fields(self, leaky_history: Path):
        server = build_server(repo_path=leaky_history)
        content, structured = _call(server, "get_receipt", {"shard_id": "s1"})
        blob = _blob(content) + json.dumps(structured)
        for marker in _SECRET_MARKERS:
            assert marker not in blob
        assert "adapter_stdout_summary" not in structured
        assert "adapter_stderr_summary" not in structured
        assert "agent_notes" not in structured
        assert "run_timeline" not in structured
        # agent_notes content is intentionally NOT dropped outright: it already
        # flows into canonical findings (Note severity), matching what the CLI
        # receipt shows at --full -- confirm that path, not silent loss.
        assert any(f["message"] == "agent note text" for f in structured["findings"])

    def test_search_history_never_leaks_forbidden_fields(self, leaky_history: Path):
        server = build_server(repo_path=leaky_history)
        for term in _SECRET_MARKERS:
            content, structured = _call(server, "search_history", {"query": term})
            # None of these terms are searchable fields, so no hits either.
            assert structured["result"] == []

    def test_no_tool_response_contains_absolute_private_path(self, leaky_history: Path):
        server = build_server(repo_path=leaky_history)
        for name, args in (
            ("recent_shards", {}),
            ("get_shard", {"shard_id": "s1"}),
            ("get_receipt", {"shard_id": "s1"}),
            ("search_history", {"query": "plain"}),
        ):
            content, structured = _call(server, name, args)
            blob = _blob(content) + json.dumps(structured)
            assert "secret-folder" not in blob
            assert str(leaky_history) not in blob

    def test_error_message_never_leaks_local_filesystem_path(self, leaky_history: Path):
        server = build_server(repo_path=leaky_history)
        with pytest.raises(ToolError) as exc_info:
            _call(server, "get_shard", {"shard_id": "does-not-exist"})
        assert str(leaky_history) not in str(exc_info.value)
        assert "Traceback" not in str(exc_info.value)
