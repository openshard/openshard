"""Remote capture (``openshard.remote``): evidence leaves an ephemeral runtime while the agent works.

Every test runs against a temporary ``OPENSHARD_HOME`` with a scripted
client or a throw-away loopback HTTP server; nothing reaches the network and
no detached flusher is ever started (``OPENSHARD_REMOTE_NO_SPAWN``).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook, handle_hook
from openshard.history.store import load_history
from openshard.remote import collector, spool
from openshard.remote import config as rconfig
from openshard.remote.transport import RemoteCaptureClient
from openshard.sync.transport import (
    KIND_CONFLICT,
    KIND_CREATED,
    KIND_DUPLICATE,
    KIND_REJECTED,
    KIND_UNAUTHORIZED,
    KIND_UNAVAILABLE,
    SendResult,
)
from openshard.verification import post_session as ps
from tests.capture_fixtures import _git, _make_repo

CAPTURE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_ID = "11111111-2222-4333-8444-555555555555"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
ENDPOINT = "https://platform.example.test"
URL = f"{ENDPOINT}/v1/remote-captures/{CAPTURE_ID}"
TOKEN = "osr_AbCd1234_" + "t" * 43
SID = "12121212-3434-4565-8787-909090909090"
RUNS = Path(".openshard") / "runs.jsonl"

# The closed key set of one Event in the Platform's remote-capture contract v1
# (openshard/platform: packages/contracts/src/remote-capture.ts).
WIRE_EVENT_KEYS = {
    "seq", "event_id", "schema_version", "event_type", "occurred_at", "run_id", "shard_id", "attempt_number",
    "actor", "source", "action", "target", "status", "evidence", "metadata", "raw_content_stored",
}
BATCH_KEYS = {"contract", "contract_version", "source", "collector_id", "events", "links"}


class FakeClient:
    """A scripted Platform: answers from a list (the last one repeats) and remembers what it was sent."""

    def __init__(self, results: list[SendResult] | None = None) -> None:
        self.results = list(results or [SendResult(KIND_DUPLICATE, 200)])
        self.batches: list[dict] = []
        self.receipts: list[dict] = []
        self.evidence: list[tuple[str, dict]] = []
        self.receipt_results: list[SendResult] = [SendResult(KIND_CREATED, 201)]

    def _next(self) -> SendResult:
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def send_events(self, batch: dict) -> SendResult:
        self.batches.append(json.loads(json.dumps(batch)))
        return self._next()

    def send(self, envelope: dict) -> SendResult:
        self.receipts.append(json.loads(json.dumps(envelope)))
        return self.receipt_results[min(len(self.receipts) - 1, len(self.receipt_results) - 1)]

    def send_evidence(self, receipt_id: str, envelope: dict) -> SendResult:
        self.evidence.append((receipt_id, json.loads(json.dumps(envelope))))
        return SendResult(KIND_CREATED, 201)

    @property
    def stored_ids(self) -> list[str]:
        """Event ids the Platform would hold: first arrival wins, as it does there."""
        seen: list[str] = []
        for batch in self.batches:
            for event in batch["events"]:
                if event["event_id"] not in seen:
                    seen.append(event["event_id"])
        return seen


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "vm-home"
    monkeypatch.setenv("OPENSHARD_HOME", str(path))
    monkeypatch.setenv("OPENSHARD_REMOTE_NO_SPAWN", "1")
    for name in (rconfig.URL_ENV, rconfig.TOKEN_ENV, rconfig.DISABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def attached(home: Path) -> rconfig.Attachment:
    return rconfig.save_attachment(capture_url=URL, token=TOKEN, organisation_id=ORG, agent="claude-code")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "workspace")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


def _hook(repo: Path, event: str, **extra) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **extra}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _work(repo: Path, tool_calls: int = 3) -> None:
    """The start of an agent session: prompt, then *tool_calls* shell commands. No end."""
    _hook(repo, "SessionStart", source="startup")
    _hook(repo, "UserPromptSubmit", prompt="fix the failing test")
    for i in range(tool_calls):
        _hook(repo, "PostToolUse", **_bash(f"python -m pytest tests/test_{i}.py -q"))


def _event(i: int) -> dict:
    return {
        "schema_version": 1, "event_id": f"evt-{i:04d}", "event_type": "tool.invoked",
        "occurred_at": f"2026-09-30T18:00:{i % 60:02d}Z", "run_id": "run-1", "shard_id": "shard-20260930-0001",
        "attempt_number": 1, "actor": "claude_code_hooks", "source": "claude_code_hooks",
        "action": f"Bash: step {i}", "target": "python", "status": "passed", "evidence": "agent_reported",
        "metadata": {"hook": "PostToolUse", "tool": "Bash"}, "raw_content_stored": False,
    }


# ---------------------------------------------------------------------------
# attachment
# ---------------------------------------------------------------------------


class TestAttachment:
    def test_not_attached_by_default_and_the_hook_path_does_nothing(self, home, repo):
        assert rconfig.resolve_attachment() is None and rconfig.attached_hint() is False
        _work(repo)
        assert spool.read_state() is None and not spool.spool_dir().exists()
        assert collector.flush().stopped == "not_attached"

    def test_save_load_round_trip_is_private(self, home):
        saved = rconfig.save_attachment(capture_url=URL + "/", token=TOKEN, organisation_id=ORG, agent="codex")
        loaded = rconfig.resolve_attachment()
        assert loaded is not None and loaded.capture_url == URL and loaded.token == TOKEN
        assert (loaded.capture_id, loaded.endpoint, loaded.organisation_id) == (CAPTURE_ID, ENDPOINT, ORG)
        assert loaded.token_prefix == "osr_AbCd1234" and TOKEN not in json.dumps(saved.to_public_dict())
        assert rconfig.clear_attachment() is True and rconfig.resolve_attachment() is None

    def test_environment_attaches_when_no_file_exists(self, home, monkeypatch):
        monkeypatch.setenv(rconfig.URL_ENV, URL)
        monkeypatch.setenv(rconfig.TOKEN_ENV, TOKEN)
        att = rconfig.resolve_attachment()
        assert att is not None and att.source == "env" and att.capture_id == CAPTURE_ID
        monkeypatch.setenv(rconfig.DISABLE_ENV, "off")
        assert rconfig.resolve_attachment() is None and rconfig.attached_hint() is False

    @pytest.mark.parametrize("url", [
        f"http://platform.example.test/v1/remote-captures/{CAPTURE_ID}",  # plaintext to a remote host
        f"{ENDPOINT}/v1/orgs/{ORG}/receipts",
        f"{ENDPOINT}/v1/remote-captures/not-a-uuid",
        "",
    ])
    def test_unusable_urls_and_tokens_are_refused(self, home, url):
        with pytest.raises(ValueError):
            rconfig.save_attachment(capture_url=url, token=TOKEN)
        with pytest.raises(ValueError) as err:
            rconfig.save_attachment(capture_url=URL, token="osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz")
        assert "osk_" not in str(err.value)  # an error never echoes a secret

    def test_loopback_http_is_allowed_for_local_development(self, home):
        url = f"http://127.0.0.1:8787/v1/remote-captures/{CAPTURE_ID}"
        assert rconfig.save_attachment(capture_url=url, token=TOKEN).endpoint == "http://127.0.0.1:8787"


# ---------------------------------------------------------------------------
# what may leave
# ---------------------------------------------------------------------------


class TestWireEvent:
    def test_a_core_event_becomes_a_contract_exact_wire_event(self):
        wire = spool.wire_event(_event(1))
        assert wire is not None and set(wire) | {"seq"} == WIRE_EVENT_KEYS
        assert wire["raw_content_stored"] is False and wire["metadata"] == {"hook": "PostToolUse", "tool": "Bash"}

    def test_unsafe_fields_are_withheld_not_sent(self):
        dirty = _event(2) | {
            "target": "/home/agent/.ssh/id_rsa",
            "action": f"Bash: curl -H 'Authorization: Bearer {TOKEN}'",
            "metadata": {
                "tool": "Bash", "stdout": "2 passed", "prompt": "do it", "env": "A=1", "nested": {"a": 1},
                "list": [1, 2], "path": "C:\\Users\\dev\\secret.txt", "note": "token=abc123", "exit_code": 1,
                "ok": True, "missing": None,
            },
        }
        wire = spool.wire_event(dirty)
        assert wire is not None
        assert wire["target"] is None and wire["action"] == "[withheld]"
        assert wire["metadata"] == {"tool": "Bash", "exit_code": 1, "ok": True, "missing": None}
        assert TOKEN not in json.dumps(wire)

    def test_repo_relative_paths_survive(self):
        wire = spool.wire_event(_event(3) | {
            "event_type": "file.changed", "target": "openshard/adapters/claude_capture_service.py",
            "action": "modified openshard/adapters/claude_capture_service.py",
        })
        assert wire is not None and wire["target"] == "openshard/adapters/claude_capture_service.py"

    def test_an_event_without_a_usable_id_is_not_spooled(self):
        assert spool.wire_event({"event_type": "tool.invoked"}) is None
        assert spool.wire_event(_event(4) | {"event_id": "has spaces"}) is None
        assert spool.wire_event("nope") is None


# ---------------------------------------------------------------------------
# the spool
# ---------------------------------------------------------------------------


class TestSpool:
    def test_append_orders_events_and_acknowledge_drains(self, attached):
        assert spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 6)]) == 5
        events, state = spool.pending(None, CAPTURE_ID)
        assert [e["seq"] for e in events] == [1, 2, 3, 4, 5] and state["next_seq"] == 6
        assert state["collector_id"].startswith("col_") and len(state["collector_id"]) == 20
        spool.acknowledge(None, CAPTURE_ID, 3, sent=3)
        assert [e["seq"] for e in spool.pending(None, CAPTURE_ID)[0]] == [4, 5] and spool.pending_count() == 2
        spool.acknowledge(None, CAPTURE_ID, 5, sent=2)
        assert spool.pending_count() == 0
        assert (spool.spool_dir() / spool.SPOOL_FILENAME).read_text(encoding="utf-8") == ""  # compacted
        # Sequence numbers never restart within a spool.
        spool.append(None, CAPTURE_ID, [_event(6)])
        assert spool.pending(None, CAPTURE_ID)[0][0]["seq"] == 6

    def test_survives_a_restart_and_a_torn_final_line(self, attached):
        spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 4)])
        spool.acknowledge(None, CAPTURE_ID, 1, sent=1)
        collector_id = spool.read_state()["collector_id"]
        # The process dies mid-write: a partial line is left at the end of the file.
        with (spool.spool_dir() / spool.SPOOL_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 4, "event": {"event_id": "evt-tor')
        # A new process reads the same spool.
        events, state = spool.pending(None, CAPTURE_ID)
        assert [e["seq"] for e in events] == [2, 3] and state["collector_id"] == collector_id

    def test_file_events_are_spooled_once_however_often_a_fold_repeats_them(self, attached):
        file_event = _event(50) | {"event_type": "file.changed", "target": "a.py"}
        assert spool.append(None, CAPTURE_ID, [], file_events=[file_event]) == 1
        assert spool.append(None, CAPTURE_ID, [], file_events=[file_event]) == 0
        assert spool.append(None, CAPTURE_ID, [_event(51)], file_events=[file_event]) == 1

    def test_a_different_capture_starts_a_fresh_spool(self, attached):
        spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 4)])
        first = spool.read_state()["collector_id"]
        events, state = spool.pending(None, OTHER_ID)
        assert events == [] and state["capture_id"] == OTHER_ID and state["collector_id"] != first

    def test_stops_spooling_at_the_journal_limit_and_counts_what_it_dropped(self, attached, monkeypatch):
        monkeypatch.setattr(spool, "MAX_SPOOLED_EVENTS", 4)
        assert spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 4)]) == 3
        assert spool.append(None, CAPTURE_ID, [_event(i) for i in range(4, 8)]) == 1
        assert spool.read_state()["dropped"] == 3

    def test_nothing_secret_is_ever_written_to_the_spool(self, attached, repo):
        _work(repo)
        for path in spool.spool_dir().iterdir():
            if path.is_file():
                assert TOKEN not in path.read_text(encoding="utf-8", errors="replace"), path.name


# ---------------------------------------------------------------------------
# the hook path
# ---------------------------------------------------------------------------


class TestHookPath:
    def test_hooks_spool_their_events_once_and_touch_no_network(self, attached, repo, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("the hook path must never open a connection")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        _work(repo, tool_calls=3)
        events, state = spool.pending(None, CAPTURE_ID)
        assert [e["event_type"] for e in events] == [
            "session.started", "session.activity", "tool.invoked", "tool.invoked", "tool.invoked",
        ]
        assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
        assert len({e["event_id"] for e in events}) == 5
        # The Receipt the session is building is known from the first prompt on.
        entry = load_history(repo / RUNS, coerce=False)[-1]
        assert [link["receipt_id"] for link in state["links"]] == [entry["receipt_id"]]
        assert state["repos"] == [str(repo)]
        assert state.get("deliver") is not True  # nothing is finalised yet

    def test_session_end_is_one_more_event_and_asks_for_the_receipt_to_be_delivered(self, attached, repo):
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        _hook(repo, "SessionEnd", reason="exit")
        events, state = spool.pending(None, CAPTURE_ID)
        assert events[-1]["event_type"] == "run.completed" and events[-1]["metadata"]["reason"] == "exit"
        assert state["deliver"] is True

    def test_changed_files_are_streamed_as_git_observed_events(self, attached, repo):
        _hook(repo, "UserPromptSubmit", prompt="edit the readme")
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        _hook(repo, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(repo / "README.md")})
        _hook(repo, "Stop")
        _hook(repo, "Stop")  # a second fold re-derives the same file Event
        events, _ = spool.pending(None, CAPTURE_ID)
        changed = [e for e in events if e["event_type"] == "file.changed"]
        assert [e["target"] for e in changed] == ["README.md"]
        assert str(repo) not in json.dumps(events)  # never an absolute path

    def test_other_agents_share_the_same_tap(self, attached, repo):
        for event, fields in (
            ("UserPromptSubmit", {"prompt": "test it"}),
            ("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
            ("Stop", {}),
        ):
            handle_hook({"session_id": SID, "hook_event_name": event, "cwd": str(repo), "model": "gpt-5-codex",
                         **fields}, env={}, agent="codex")
        events, _ = spool.pending(None, CAPTURE_ID)
        assert {e["source"] for e in events} == {"codex_hooks"} and len(events) >= 3

    def test_a_broken_spool_never_breaks_capture(self, attached, repo, monkeypatch):
        monkeypatch.setattr(spool, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        assert load_history(repo / RUNS, coerce=False)[-1]["capture"]["tool_call_count"] == 1


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


class TestFlush:
    def test_sends_batches_and_acknowledges_only_what_was_accepted(self, attached):
        spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 251)])
        client = FakeClient()
        report = collector.flush(client=client)
        assert (report.batches, report.events_sent, report.pending, report.stopped) == (3, 250, 0, None)
        assert [len(b["events"]) for b in client.batches] == [100, 100, 50]
        first = client.batches[0]
        assert set(first) == BATCH_KEYS and set(first["events"][0]) == WIRE_EVENT_KEYS
        assert first["contract"] == "openshard.remote-capture" and first["contract_version"] == "1"
        assert first["collector_id"] == spool.read_state()["collector_id"]
        assert TOKEN not in json.dumps(client.batches)
        assert collector.flush(client=client).batches == 0  # nothing left: no request

    def test_stream_early_events_leave_before_the_session_ends(self, attached, repo):
        client = FakeClient()
        _work(repo, tool_calls=2)
        collector.flush(client=client)
        assert len(client.stored_ids) == 4  # hosted while the agent is still working
        _hook(repo, "PostToolUse", **_bash("ruff check ."))
        collector.flush(client=client)
        assert len(client.stored_ids) == 5
        assert all(batch["links"] for batch in client.batches)  # every batch names the Receipt being built

    def test_abrupt_destruction_keeps_exactly_what_left(self, attached, repo):
        """20 Events happen, 15 reach the Platform, the VM dies without an end hook."""
        client = FakeClient()
        _work(repo, tool_calls=13)  # 15 Events
        assert collector.flush(client=client).events_sent == 15
        for i in range(5):  # 5 more, never flushed
            _hook(repo, "PostToolUse", **_bash(f"python -m pytest tests/late_{i}.py -q"))
        assert spool.pending_count() == 5
        # The VM is destroyed here: no SessionEnd, no final flush, the spool is gone with it.
        assert len(client.stored_ids) == 15
        sent_types = [e["event_type"] for b in client.batches for e in b["events"]]
        assert "run.completed" not in sent_types  # no end was fabricated
        assert client.receipts == []  # and no Receipt was manufactured
        # The capture still knows which Receipt the session was building.
        assert client.batches[-1]["links"][0]["receipt_id"].startswith("rcpt_")

    def test_a_lost_acknowledgement_means_a_resend_not_a_loss_or_a_duplicate(self, attached):
        spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 11)])
        client = FakeClient([SendResult(KIND_UNAVAILABLE, None), SendResult(KIND_DUPLICATE, 200)])
        first = collector.flush(client=client, now=1000.0)  # the Platform stored it but the answer was lost
        assert (first.stopped, first.pending) == ("unavailable", 10)
        assert collector.flush(client=client, now=1001.0).stopped == "backoff"  # not hammering
        spool.append(None, CAPTURE_ID, [_event(11)])  # work continues meanwhile
        second = collector.flush(client=client, now=1010.0)
        assert (second.events_sent, second.pending, second.stopped) == (11, 0, None)
        assert len(client.batches[0]["events"]) == 10 and len(client.batches[1]["events"]) == 11  # overlapping
        assert len(client.stored_ids) == 11  # de-duplicated on event_id, as the Platform does

    @pytest.mark.parametrize("failure", [SendResult(KIND_UNAVAILABLE, 429), SendResult(KIND_UNAVAILABLE, 500),
                                         SendResult(KIND_UNAVAILABLE, None)])
    def test_429_500_and_no_network_back_off_and_recover(self, attached, failure):
        spool.append(None, CAPTURE_ID, [_event(1)])
        client = FakeClient([failure, failure, SendResult(KIND_DUPLICATE, 200)])
        assert collector.flush(client=client, now=100.0).stopped == "unavailable"
        assert collector.flush(client=client, now=104.0).stopped == "backoff"  # 5 s
        assert collector.flush(client=client, now=106.0).stopped == "unavailable"
        assert collector.flush(client=client, now=115.0).stopped == "backoff"  # 10 s now
        report = collector.flush(client=client, now=117.0)
        assert (report.events_sent, report.stopped) == (1, None)
        assert spool.read_state()["failures"] == 0 and spool.read_state()["backoff_until"] is None

    def test_the_spool_survives_five_minutes_without_the_platform(self, attached, repo):
        down = FakeClient([SendResult(KIND_UNAVAILABLE, None)])
        now = 10_000.0
        for minute in range(5):
            _hook(repo, "PostToolUse", **_bash(f"python -m pytest tests/m{minute}.py -q"))
            collector.flush(client=down, now=now + minute * 60)
        assert spool.pending_count() == 6  # session start + 5 tool calls, all still here
        up = FakeClient()
        assert collector.flush(client=up, now=now + 600).events_sent == 6

    def test_a_refused_batch_loses_only_the_refused_event(self, attached):
        spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 5)])

        class Picky(FakeClient):
            def send_events(self, batch: dict) -> SendResult:
                self.batches.append(batch)
                bad = any(e["event_id"] == "evt-0003" for e in batch["events"])
                return SendResult(KIND_REJECTED, 422, "privacy_violation") if bad else SendResult(KIND_DUPLICATE, 200)

        report = collector.flush(client=Picky())
        assert (report.events_sent, report.events_rejected, report.pending, report.stopped) == (3, 1, 0, None)
        assert spool.read_state()["rejected"] == 1

    def test_a_dead_token_stops_for_good_and_keeps_the_spool(self, attached):
        spool.append(None, CAPTURE_ID, [_event(1), _event(2)])
        client = FakeClient([SendResult(KIND_UNAUTHORIZED, 401)])
        assert collector.flush(client=client).stopped == "unauthorized"
        assert collector.flush(client=client).stopped == "unauthorized"
        assert len(client.batches) == 1 and spool.pending_count() == 2  # asked once, lost nothing locally

    def test_a_full_journal_stops_events_but_still_delivers_the_receipt(self, attached, repo):
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        _hook(repo, "SessionEnd", reason="exit")
        client = FakeClient([SendResult(KIND_CONFLICT, 409, "remote_capture_full")])
        report = collector.flush(client=client)
        assert report.stopped == "journal_full" and len(client.receipts) == 1

    def test_two_flushers_never_send_the_same_batch_at_once(self, attached):
        spool.append(None, CAPTURE_ID, [_event(i) for i in range(1, 4)])
        inner: list[str | None] = []

        class Reentrant(FakeClient):
            def send_events(self, batch: dict) -> SendResult:
                inner.append(collector.flush(client=FakeClient()).stopped)  # a second flusher, mid-send
                return super().send_events(batch)

        report = collector.flush(client=Reentrant())
        assert inner == ["busy"] and report.events_sent == 3

    def test_heartbeat_is_an_empty_batch_and_only_when_asked(self, attached):
        spool.append(None, CAPTURE_ID, [])
        client = FakeClient()
        assert collector.flush(client=client).batches == 0 and client.batches == []
        report = collector.flush(client=client, heartbeat=True)
        assert report.heartbeat is True and client.batches[0]["events"] == []


# ---------------------------------------------------------------------------
# the canonical Receipt, delivered through the capture
# ---------------------------------------------------------------------------


class TestReceiptDelivery:
    def _finished(self, repo: Path) -> dict:
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        _hook(repo, "SessionEnd", reason="exit")
        return load_history(repo / RUNS, coerce=False)[-1]

    def test_a_finished_session_delivers_its_receipt_once_with_the_capture_token_only(self, attached, repo):
        entry = self._finished(repo)
        client = FakeClient()
        report = collector.flush(client=client)
        assert report.receipts is not None and report.receipts["created"] == 1
        envelope = client.receipts[0]
        assert envelope["contract"] == "openshard.receipt-sync"
        assert envelope["receipt"]["receipt_id"] == entry["receipt_id"]
        assert client.batches[-1]["links"][0]["receipt_id"] == entry["receipt_id"]  # the same identity
        # Repeated flushes deliver nothing again.
        collector.flush(client=client)
        collector.flush(client=client, deliver=True)
        assert len(client.receipts) == 1
        assert spool.read_state()["deliver"] is False

    def test_an_unfinished_session_delivers_no_receipt(self, attached, repo):
        _work(repo, tool_calls=2)
        client = FakeClient()
        report = collector.flush(client=client, deliver=True)
        assert client.receipts == [] and report.receipts["in_progress"] == 1

    def test_later_verification_leaves_through_the_capture_too(self, attached, repo):
        entry = self._finished(repo)
        client = FakeClient()
        collector.flush(client=client)
        check = ps.PlannedCheck(name="pytest", argv=["pytest"], kind="test", origin="contract", safety="safe", reason="")
        tree = ps.TreeState(head="a" * 40, dirty=False, tracked_dirty=False)
        ps.record_attestation(repo, ps.build_attestation(
            entry, [ps.CheckRun(check, "passed", exit_code=0)], before=tree, after=tree,
            started_at="2026-09-30T18:05:00Z", completed_at="2026-09-30T18:05:00Z",
        ))
        assert collector.request_delivery(repo_root=repo) is True
        collector.flush(client=client)
        assert [rid for rid, _ in client.evidence] == [entry["receipt_id"]]
        assert client.evidence[0][1]["state"]["basis"] == "post_session"
        assert len(client.receipts) == 1  # the Receipt itself was not resent
        collector.flush(client=client, deliver=True)
        assert len(client.evidence) == 1

    def test_verify_asks_for_delivery_when_attached(self, attached, repo, monkeypatch):
        import sys

        import yaml

        self._finished(repo)
        (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        (repo / ".openshard" / "config.yml").write_text(yaml.safe_dump({"verification_commands": [
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_ok.py"],
        ]}), encoding="utf-8")
        monkeypatch.chdir(repo)
        from openshard.cli.main import cli

        spool.update_state(None, CAPTURE_ID, deliver=False)
        assert CliRunner().invoke(cli, ["verify", "--json"]).exit_code == 0
        assert spool.read_state()["deliver"] is True


# ---------------------------------------------------------------------------
# asking for a flush
# ---------------------------------------------------------------------------


class TestNotify:
    def test_spawns_at_most_one_detached_flusher_per_interval(self, attached, monkeypatch):
        monkeypatch.delenv("OPENSHARD_REMOTE_NO_SPAWN")
        spawned: list[int] = []
        monkeypatch.setattr(collector, "_spawn_detached_flusher", lambda env: spawned.append(1) or True)
        for _ in range(20):  # a burst of hooks
            collector.notify()
        assert len(spawned) == 1
        spool.update_state(None, CAPTURE_ID, last_spawn_at=time.time() - collector.FLUSH_INTERVAL_SECONDS - 1)
        collector.notify()
        assert len(spawned) == 2

    def test_not_attached_or_disabled_spawns_nothing(self, home, monkeypatch):
        monkeypatch.delenv("OPENSHARD_REMOTE_NO_SPAWN")
        monkeypatch.setattr(collector, "_spawn_detached_flusher", lambda env: (_ for _ in ()).throw(AssertionError()))
        collector.notify()

    def test_the_service_thread_flushes_shortly_after_an_event_and_on_stop(self, attached, monkeypatch):
        monkeypatch.setattr(collector, "FLUSH_INTERVAL_SECONDS", 0.05)
        flushed: list[int] = []
        real_flush = collector.flush
        client = FakeClient()
        monkeypatch.setattr(collector, "flush", lambda env=None, **k: flushed.append(1) or real_flush(env, client=client, **k))
        stop = threading.Event()
        thread = threading.Thread(target=collector.flush_periodically, args=(stop,), daemon=True)
        thread.start()
        try:
            spool.append(None, CAPTURE_ID, [_event(1)])
            collector.notify()  # wakes the thread instead of spawning a process
            deadline = time.time() + 5
            while not client.stored_ids and time.time() < deadline:
                time.sleep(0.02)
            assert client.stored_ids == ["evt-0001"]
        finally:
            stop.set()
            collector._wake.set()
            thread.join(timeout=5)
        assert not thread.is_alive() and collector._background_active is False


# ---------------------------------------------------------------------------
# transport + CLI against a loopback Platform
# ---------------------------------------------------------------------------


class _Platform(BaseHTTPRequestHandler):
    """Just enough of the Platform's capture routes to exercise the real HTTP client."""

    token = TOKEN
    events: list[dict] = []
    requests: list[dict] = []
    status_override: int | None = None

    def _answer(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method: str) -> None:
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        cls.requests.append({"method": method, "path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        base = f"/v1/remote-captures/{CAPTURE_ID}"
        if self.headers.get("Authorization") != f"Bearer {cls.token}" or not self.path.startswith(base):
            return self._answer(401, {"error": {"code": "unauthenticated", "message": "no"}})
        if cls.status_override is not None:
            return self._answer(cls.status_override, {"error": {"code": "internal_error", "message": "x"}})
        if method == "GET" and self.path == base:
            return self._answer(200, {"id": CAPTURE_ID, "organisation_id": ORG, "agent": "claude-code", "state": "live",
                                      "event_count": len(cls.events), "expires_at": "2026-10-01T02:00:00.000Z"})
        if method == "POST" and self.path == base + "/events":
            held = {e["event_id"] for e in cls.events}
            fresh = [e for e in body["events"] if e["event_id"] not in held]
            cls.events.extend(fresh)
            return self._answer(200, {"accepted": len(fresh), "duplicates": len(body["events"]) - len(fresh),
                                      "event_count": len(cls.events), "state": "live", "expires_at": "x"})
        if method == "POST" and self.path == base + "/receipts":
            return self._answer(201, {"outcome": "created", "receipt_id": body["receipt"]["receipt_id"]})
        return self._answer(404, {"error": {"code": "not_found", "message": "Route not found."}})

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def log_message(self, *_args):
        return


@pytest.fixture
def platform():
    _Platform.events, _Platform.requests, _Platform.status_override = [], [], None
    httpd = HTTPServer(("127.0.0.1", 0), _Platform)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1/remote-captures/{CAPTURE_ID}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestHttp:
    def test_client_talks_only_to_its_capture_with_its_token(self, home, platform):
        att = rconfig.save_attachment(capture_url=platform, token=TOKEN)
        client = RemoteCaptureClient(att, user_agent="openshard/test", timeout=5.0)
        result, status = client.status()
        assert result.accepted and status["state"] == "live"
        assert client.send_events({"events": [{"event_id": "e1"}]}).accepted
        assert client.send({"receipt": {"receipt_id": "rcpt_" + "a" * 32}}).kind == KIND_CREATED
        assert {r["auth"] for r in _Platform.requests} == {f"Bearer {TOKEN}"}
        assert all(r["path"].startswith(f"/v1/remote-captures/{CAPTURE_ID}") for r in _Platform.requests)
        wrong = RemoteCaptureClient(rconfig.Attachment(capture_url=platform, token=TOKEN[:-1] + "x", source="env"),
                                    user_agent="t", timeout=5.0)
        assert wrong.status()[0].kind == KIND_UNAUTHORIZED
        _Platform.status_override = 503
        assert client.send_events({"events": []}).kind == KIND_UNAVAILABLE

    def test_an_unreachable_platform_is_unavailable_quickly(self, home):
        att = rconfig.Attachment(capture_url=f"http://127.0.0.1:9/v1/remote-captures/{CAPTURE_ID}", token=TOKEN, source="env")
        assert RemoteCaptureClient(att, user_agent="t", timeout=1.0).send_events({"events": []}).kind == KIND_UNAVAILABLE


class TestCli:
    def _run(self, args, monkeypatch, cwd: Path):
        from openshard.cli.main import cli

        monkeypatch.chdir(cwd)
        return CliRunner().invoke(cli, args, catch_exceptions=False)

    def test_attach_status_flush_detach(self, home, repo, platform, monkeypatch):
        out = self._run(["remote", "status"], monkeypatch, repo)
        assert "not attached" in out.output
        monkeypatch.setenv(rconfig.URL_ENV, platform)
        monkeypatch.setenv(rconfig.TOKEN_ENV, TOKEN)
        out = self._run(["remote", "attach"], monkeypatch, repo)
        assert out.exit_code == 0 and "Attached to remote capture" in out.output and TOKEN not in out.output
        monkeypatch.delenv(rconfig.TOKEN_ENV)  # the secret was only there during setup
        assert rconfig.resolve_attachment().organisation_id == ORG

        _work(repo, tool_calls=2)
        out = self._run(["remote", "status", "--json"], monkeypatch, repo)
        doc = json.loads(out.output)
        assert doc["status"] == "attached" and doc["local"]["pending"] == 4 and TOKEN not in out.output

        out = self._run(["remote", "flush"], monkeypatch, repo)
        assert "Sent 4 event(s) in 1 batch(es); 0 still queued." in out.output
        assert len(_Platform.events) == 4
        out = self._run(["remote", "flush", "--json"], monkeypatch, repo)
        assert json.loads(out.output)["events_sent"] == 0

        out = self._run(["remote", "status"], monkeypatch, repo)
        assert "4 event(s) preserved" in out.output and "4 sent, 0 queued" in out.output
        assert "Detached." in self._run(["remote", "detach"], monkeypatch, repo).output
        assert "Not attached" in self._run(["remote", "flush"], monkeypatch, repo).output

    def test_attach_refuses_a_bad_token_without_echoing_it(self, home, repo, platform, monkeypatch):
        from openshard.cli.main import cli

        monkeypatch.chdir(repo)
        bad = TOKEN[:-1] + "x"
        out = CliRunner().invoke(cli, ["remote", "attach", "--url", platform, "--token", bad])
        assert out.exit_code != 0 and "not valid for this capture" in out.output and bad not in out.output
        assert rconfig.resolve_attachment() is None
        out = CliRunner().invoke(cli, ["remote", "attach", "--url", platform, "--token", "osk_not_a_capture_token_000000000"])
        assert out.exit_code != 0 and "osr_" in out.output

    def test_help_lists_remote_under_integrations(self, repo, monkeypatch):
        out = self._run(["--help"], monkeypatch, repo)
        assert "remote" in out.output
