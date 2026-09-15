"""v0.4.4 Phase 1/4 -- corrupt or lost evidence is never silently forgotten.

A malformed line in a durable capture queue used to be skipped and the file
treated as fully replayed. Now: valid neighbours are still applied, the
damaged material is quarantined for diagnosis, the service counts it, and
the affected record says its capture is incomplete and why. A transient
I/O failure keeps the existing retry path and is never treated as
corruption.
"""

# ruff: noqa: F811 -- pytest fixtures are re-exported by import from the service test module
from __future__ import annotations

import json
from pathlib import Path

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.history.capture_completeness import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_INCOMPLETE,
    COMPLETENESS_UNKNOWN,
    derive_capture_completeness,
)
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from tests.test_claude_capture_service import (  # noqa: F401 - fixtures re-exported for pytest
    SID,
    _lines,
    _queue_line,
    _session_dir,
    _wait_for,
    capture_env,
    repo,
    service,
)


def _quarantine_dir(repo: Path) -> Path:
    return _session_dir(repo) / svc.QUARANTINE_DIRNAME


def _write_queue(repo: Path, text: str, key: str = SID) -> Path:
    directory = _session_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}{svc.QUEUE_SUFFIX}"
    path.write_text(text, encoding="utf-8")
    return path


class TestCorruptQueueLines:
    def test_corrupt_line_is_quarantined_counted_and_marks_capture_incomplete(self, service, repo):
        _write_queue(
            repo,
            _queue_line("ok-1", "UserPromptSubmit", task_excerpt="fine") + "\n"
            + "garbage that is not json\n"
            + _queue_line("ok-2", "Stop") + "\n",
        )
        service.server.recorder.recover(repo)
        assert _wait_for(lambda: len(_lines(repo)) == 1 and _lines(repo)[0]["capture"].get("completeness"))
        assert service.server.recorder.wait_idle(20)
        entry = _lines(repo)[0]
        # Valid neighbours were recovered.
        assert entry["task"] == "fine"
        assert entry["capture"]["turn_count"] == 1
        # The loss is explicit on the record.
        completeness = entry["capture"]["completeness"]
        assert completeness["status"] == COMPLETENESS_INCOMPLETE
        kinds = {r["kind"]: r for r in completeness["reasons"]}
        assert kinds["corrupt_queued_event"]["count"] == 1
        # ... and counted by the service.
        stats = client.health(service.port)["stats"]
        assert stats["corrupt_lines"] == 1
        assert stats["replay_errors"] == 0  # corruption is not a transient error
        # The damaged material is preserved for diagnosis, never left in the live queue.
        quarantined = list(_quarantine_dir(repo).glob("*"))
        assert len(quarantined) == 1
        text = quarantined[0].read_text(encoding="utf-8")
        assert "garbage that is not json" in text
        assert not list(_session_dir(repo).glob("*.queue*.jsonl"))
        # The receipt says so: depth stays partial, the gap is named.
        rendered = render_compact_shard_receipt(build_shard_receipt(entry, 0))
        gaps = next(ln for ln in rendered.splitlines() if ln.strip().startswith("Gaps"))
        assert "could not be decoded" in gaps
        assert "partial" in rendered and "did not execute or verify" in rendered

    def test_structurally_invalid_lines_count_as_corrupt(self, service, repo):
        _write_queue(
            repo,
            _queue_line("ok-1", "UserPromptSubmit", task_excerpt="fine") + "\n"
            + json.dumps({"id": 1}) + "\n"                                        # no data dict
            + json.dumps([1, 2]) + "\n"                                            # not an object
            + json.dumps({"id": "bad", "kind": "hook", "at": "x", "data": {"event": "Nope", "session_id": SID}}) + "\n",
        )
        service.server.recorder.recover(repo)
        assert _wait_for(lambda: len(_lines(repo)) == 1 and _lines(repo)[0]["capture"].get("completeness"))
        assert service.server.recorder.wait_idle(20)
        entry = _lines(repo)[0]
        assert entry["capture"]["completeness"]["status"] == COMPLETENESS_INCOMPLETE
        reason = next(r for r in entry["capture"]["completeness"]["reasons"] if r["kind"] == "corrupt_queued_event")
        assert reason["count"] == 3
        assert client.health(service.port)["stats"]["corrupt_lines"] == 3
        assert len(list(_quarantine_dir(repo).glob("*"))) == 1

    def test_entirely_corrupt_queue_is_quarantined_not_deleted(self, service, repo):
        path = _write_queue(repo, "\x00\x01 binary junk\n{not json\n")
        service.server.recorder.recover(repo)
        assert service.server.recorder.wait_idle(20)
        assert not path.exists()
        assert len(list(_quarantine_dir(repo).glob("*"))) == 1
        assert client.health(service.port)["stats"]["corrupt_lines"] == 2

    def test_quarantine_is_bounded_and_carries_no_absolute_paths(self, service, repo):
        huge = "x" * 200_000
        _write_queue(repo, _queue_line("ok-1", "UserPromptSubmit", task_excerpt="fine") + "\n" + huge + "\n")
        service.server.recorder.recover(repo)
        assert service.server.recorder.wait_idle(20)
        quarantined = list(_quarantine_dir(repo).glob("*"))
        assert len(quarantined) == 1
        text = quarantined[0].read_text(encoding="utf-8")
        assert len(text) < 20_000
        assert str(repo) not in text

    def test_recovered_record_without_prior_activity_still_records_the_loss_when_work_arrives(self, service, repo):
        # Only the corrupt line exists at recovery time: nothing is known about
        # the session yet, so no record is fabricated -- but the loss is not
        # forgotten: as soon as the session shows work, its record carries it.
        _write_queue(repo, "not json at all\n")
        service.server.recorder.recover(repo)
        assert service.server.recorder.wait_idle(20)
        assert _lines(repo) == []
        assert client.health(service.port)["stats"]["corrupt_lines"] == 1
        from tests.test_claude_capture_service import _payload, _post

        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="later work"), project_dir=str(repo))
        assert _wait_for(lambda: len(_lines(repo)) == 1)
        assert service.server.recorder.wait_idle(20)
        assert _lines(repo)[0]["capture"]["completeness"]["status"] == COMPLETENESS_INCOMPLETE


class TestTransientFailuresStayTransient:
    def test_unreadable_queue_file_is_retried_not_quarantined(self, service, repo, monkeypatch):
        _write_queue(repo, _queue_line("ok-1", "UserPromptSubmit", task_excerpt="fine") + "\n")
        real_read_text = Path.read_text
        failures = {"n": 0}

        def flaky(self, *args, **kwargs):
            if self.name.endswith(".jsonl") and ".queue." in self.name and failures["n"] < 2:
                failures["n"] += 1
                raise PermissionError("antivirus holds the file")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(svc, "_RETRY_BACKOFF_SECONDS", 0.05)
        monkeypatch.setattr(Path, "read_text", flaky)
        service.server.recorder.recover(repo)
        assert _wait_for(lambda: len(_lines(repo)) == 1 and _lines(repo)[0]["task"] == "fine")
        assert failures["n"] == 2
        assert not _quarantine_dir(repo).exists()
        stats = client.health(service.port)["stats"]
        assert stats["corrupt_lines"] == 0
        assert _lines(repo)[0]["capture"]["completeness"]["status"] == COMPLETENESS_COMPLETE

    def test_replay_error_outcome_keeps_the_file_for_retry(self, service, repo, monkeypatch):
        _write_queue(repo, _queue_line("ok-1", "UserPromptSubmit", task_excerpt="fine") + "\n")
        from openshard.adapters import claude_hooks as ch

        real = ch.apply_reduced_hook
        calls = {"n": 0}

        def failing_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return ch.HookOutcome(event="UserPromptSubmit", action="error", session_id=SID, detail="LockTimeoutError")
            return real(*args, **kwargs)

        monkeypatch.setattr(svc, "_RETRY_BACKOFF_SECONDS", 0.05)
        monkeypatch.setattr(svc, "apply_reduced_hook", failing_once)
        service.server.recorder.recover(repo)
        assert _wait_for(lambda: len(_lines(repo)) == 1 and _lines(repo)[0]["task"] == "fine")
        assert calls["n"] == 2
        assert not _quarantine_dir(repo).exists()
        stats = client.health(service.port)["stats"]
        assert stats["replay_errors"] >= 1 and stats["corrupt_lines"] == 0


class TestCompletenessModel:
    """Depth (how much could be seen) and completeness (is evidence known
    lost) are separate answers; a healthy partial capture is *complete*."""

    def test_new_hooks_record_without_loss_is_partial_depth_and_complete(self):
        entry = {"executor": "claude_code_hooks",
                 "capture": {"hook_events_dropped": 0, "completeness": {"status": "complete", "reasons": []}}}
        block = derive_capture_completeness(entry)
        assert block["depth"] == "partial"
        assert block["status"] == COMPLETENESS_COMPLETE
        assert block["reasons"] == [] and block["derived"] is False

    def test_legacy_hooks_record_without_loss_tracking_is_unknown_not_complete(self):
        entry = {"executor": "claude_code_hooks", "capture": {"hook_events_dropped": 0}}
        block = derive_capture_completeness(entry)
        assert block["depth"] == "partial"
        assert block["status"] == COMPLETENESS_UNKNOWN
        assert block["derived"] is True

    def test_old_record_with_dropped_events_is_derived_incomplete_and_labelled(self):
        entry = {"executor": "claude_code_hooks", "capture": {"hook_events_dropped": 3}}
        block = derive_capture_completeness(entry)
        assert block["status"] == COMPLETENESS_INCOMPLETE
        assert block["derived"] is True
        assert block["reasons"][0]["kind"] == "dropped_hook_events"
        assert block["reasons"][0]["count"] == 3

    def test_stored_block_wins_over_derivation(self):
        stored = {"status": COMPLETENESS_INCOMPLETE,
                  "reasons": [{"kind": "corrupt_queued_event", "count": 1, "detail": "1 queued event could not be decoded"}]}
        entry = {"executor": "claude_code_hooks", "capture": {"completeness": stored, "hook_events_dropped": 0}}
        assert derive_capture_completeness(entry) == {**stored, "depth": "partial", "derived": False}

    def test_pre_release_stored_partial_status_reads_as_complete(self):
        entry = {"executor": "claude_code_hooks", "capture": {"completeness": {"status": "partial", "reasons": []}}}
        assert derive_capture_completeness(entry)["status"] == COMPLETENESS_COMPLETE

    def test_native_record_is_full_and_complete_and_unknown_origin_is_unknown(self):
        native = derive_capture_completeness({"workflow": "native", "executor": "native"})
        assert (native["depth"], native["status"]) == ("full", COMPLETENESS_COMPLETE)
        unknown = derive_capture_completeness({"task": "x"})
        assert (unknown["depth"], unknown["status"]) == ("unknown", COMPLETENESS_UNKNOWN)

    def test_receipt_shows_partial_capture_and_no_known_gaps(self):
        entry = {"task": "t", "timestamp": "2026-09-01T10:00:00Z", "executor": "claude_code_hooks",
                 "capture": {"session_id": SID, "task_status": "turn_completed", "hook_events_dropped": 0,
                             "completeness": {"status": "complete", "reasons": []}}}
        rendered = render_compact_shard_receipt(build_shard_receipt(entry, 0))
        assert "Incomplete" not in rendered
        assert "did not execute or verify" in rendered
        gaps = next(ln for ln in rendered.splitlines() if ln.strip().startswith("Gaps"))
        assert "None known" in gaps
