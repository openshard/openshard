"""v0.4.4 Phase 1/2 -- global Receipt identity.

``shard_id`` is ``shard-YYYYMMDD-NNNN`` from the runs.jsonl line count and
cannot be a global identity. ``receipt_id`` is minted at record creation,
independently of history position, and never collides under concurrent
session creation across threads or repositories. ``shard_id`` keeps its
historic meaning and old records without a ``receipt_id`` keep rendering.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.receipt_identity import RECEIPT_ID_FIELD, is_receipt_id, new_receipt_id
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)
from openshard.history.views import receipt_to_dict
from tests.capture_fixtures import _make_repo


def _entries(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _session(repo: Path, sid: str) -> None:
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    base = {"session_id": sid, "cwd": str(repo)}
    handle_claude_hook({**base, "hook_event_name": "SessionStart", "source": "startup"}, env=env)
    handle_claude_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": f"task {sid}"}, env=env)
    handle_claude_hook({**base, "hook_event_name": "Stop"}, env=env)


class TestReceiptIdFormat:
    def test_new_ids_are_well_formed_and_distinct(self):
        ids = {new_receipt_id() for _ in range(10_000)}
        assert len(ids) == 10_000
        assert all(is_receipt_id(i) for i in ids)

    def test_validator_rejects_garbage(self):
        for bad in ("", "shard-20260914-0001", "rcpt_", "rcpt_XYZ", None, 42):
            assert not is_receipt_id(bad)


class TestConcurrentSessions:
    def test_concurrent_sessions_in_one_repo_never_share_a_receipt_id(self, tmp_path, monkeypatch):
        # 24 sessions contend for one runs.jsonl lock; on a loaded CI box a
        # 3 s lock budget can expire (the hook then reports "error", which is
        # a lock-latency fact, not an identity one). Give the lock the time
        # it needs so this test measures identity only.
        from openshard.adapters import claude_hooks as ch

        monkeypatch.setattr(ch, "_LOCK_TIMEOUT_SECONDS", 60.0)
        repo = _make_repo(tmp_path / "repo")
        sids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(24)]
        errors: list[BaseException] = []

        def worker(sid: str) -> None:
            try:
                _session(repo, sid)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert not errors
        entries = _entries(repo)
        assert len(entries) == len(sids)
        receipt_ids = [e[RECEIPT_ID_FIELD] for e in entries]
        assert all(is_receipt_id(r) for r in receipt_ids)
        assert len(set(receipt_ids)) == len(sids), "receipt_id collided under concurrent session creation"

    def test_same_history_position_in_two_repos_gives_distinct_receipt_ids(self, tmp_path):
        repo_a = _make_repo(tmp_path / "a")
        repo_b = _make_repo(tmp_path / "b")
        sid = "11111111-2222-4333-8444-555555555555"
        _session(repo_a, sid)
        _session(repo_b, sid)
        a, b = _entries(repo_a)[0], _entries(repo_b)[0]
        # Historic shard_id is position-based and *does* collide across repos:
        # that is exactly why it is not a global identity.
        assert a["shard_id"] == b["shard_id"]
        assert a[RECEIPT_ID_FIELD] != b[RECEIPT_ID_FIELD]

    def test_receipt_id_is_stable_across_folds_of_one_session(self, tmp_path):
        repo = _make_repo(tmp_path / "repo")
        sid = "11111111-2222-4333-8444-555555555555"
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        base = {"session_id": sid, "cwd": str(repo)}
        handle_claude_hook({**base, "hook_event_name": "SessionStart", "source": "startup"}, env=env)
        handle_claude_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": "t"}, env=env)
        first = _entries(repo)[0][RECEIPT_ID_FIELD]
        handle_claude_hook({**base, "hook_event_name": "Stop"}, env=env)
        handle_claude_hook({**base, "hook_event_name": "SessionEnd", "reason": "other"}, env=env)
        entries = _entries(repo)
        assert len(entries) == 1
        assert entries[0][RECEIPT_ID_FIELD] == first
        # Resumed after SessionEnd: the rebuilt buffer keeps the same receipt_id.
        handle_claude_hook({**base, "hook_event_name": "Stop"}, env=env)
        assert _entries(repo)[0][RECEIPT_ID_FIELD] == first


class TestRenderingAndCompatibility:
    def test_receipt_exposes_receipt_id_alongside_shard_id(self, tmp_path):
        repo = _make_repo(tmp_path / "repo")
        _session(repo, "11111111-2222-4333-8444-555555555555")
        entry = _entries(repo)[0]
        receipt = build_shard_receipt(entry, 0)
        assert receipt.receipt_id == entry[RECEIPT_ID_FIELD]
        assert receipt.shard_id == entry["shard_id"]
        compact = render_compact_shard_receipt(receipt)
        full = render_full_shard_receipt(receipt)
        assert receipt.receipt_id in compact and receipt.receipt_id in full
        assert receipt.shard_id in compact and receipt.shard_id in full
        assert receipt_to_dict(receipt)["receipt_id"] == receipt.receipt_id

    @pytest.mark.parametrize("entry", [
        {"task": "legacy", "timestamp": "2026-04-13T06:24:08Z", "workflow": "native", "executor": "native"},
        {"task": "legacy hooks", "timestamp": "2026-04-13T06:24:08Z", "executor": "claude_code_hooks",
         "shard_id": "shard-20260413-0007", "capture": {"session_id": "x"}},
        {"task": "bare"},
    ])
    def test_old_records_without_receipt_id_render_and_never_get_one_at_display_time(self, entry):
        receipt = build_shard_receipt(dict(entry), 6)
        assert receipt.receipt_id is None
        compact = render_compact_shard_receipt(receipt)
        assert "Receipt ID" not in compact
        assert receipt.shard_id.startswith("shard-")
        assert receipt_to_dict(receipt)["receipt_id"] is None


class TestRepoIdentityProjection:
    """``repo_identity`` (canonical host/owner/repo) rides the extended export
    beside the folder-name ``repo``; the MCP default key set is unchanged."""

    def test_extended_export_carries_stored_repo_identity(self):
        entry = {
            "schema_version": "1.2", "receipt_id": new_receipt_id(), "timestamp": "2026-09-16T09:12:03Z",
            "task": "t", "agent": "codex", "repo_name": "openshard",
            "repo_identity": "github.com/openshard/openshard",
        }
        receipt = build_shard_receipt(entry, index=0)
        assert receipt.repo == "openshard"
        assert receipt.repo_identity == "github.com/openshard/openshard"
        extended = receipt_to_dict(receipt, extended=True)
        assert extended["repo"] == "openshard"
        assert extended["repo_identity"] == "github.com/openshard/openshard"
        assert "repo_identity" not in receipt_to_dict(receipt)

    def test_missing_or_malformed_identity_is_none_never_derived(self, tmp_path):
        for value in (None, "", 42, {"host": "github.com"}):
            entry = {"receipt_id": new_receipt_id(), "timestamp": "2026-09-16T09:12:03Z", "task": "t",
                     "agent": "codex", "repo_name": "openshard"}
            if value is not None:
                entry["repo_identity"] = value
            receipt = build_shard_receipt(entry, index=0)
            assert receipt.repo_identity is None
            assert receipt_to_dict(receipt, extended=True)["repo_identity"] is None
