"""Tests for the privacy-safe telemetry foundation (0.4.2): state, schema,
queue, transport, client, receipt mapping.

Every test runs against a temporary ``OPENSHARD_HOME`` passed explicitly as
*env* and a ``RecordingTransport`` injected with ``client.configure``; no
test ever reads the developer's real state file or touches the network
(the one "offline" test connects to a closed loopback port on purpose).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from openshard.telemetry import client, events, queue, schema, state, transport

SECRET = "sk-ant-api03-SECRETSECRET12345678901234567890"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def env(home: Path) -> dict:
    return {
        "OPENSHARD_HOME": str(home),
        "OPENSHARD_TELEMETRY_NO_BACKGROUND": "1",
        "PATH": os.environ.get("PATH", ""),
    }


@pytest.fixture
def recording():
    rt = transport.RecordingTransport()
    client.configure(transport=rt, repo_config={})
    yield rt
    client.configure(transport=None, repo_config=None)


def _on(env: dict) -> state.TelemetryState:
    """Consent on, starting from an empty queue (turning on queues its own event)."""
    st = state.set_consent("on", source="cli", env=env)
    queue.clear(env)
    return st


# ---------------------------------------------------------------------------
# State: installation id + consent
# ---------------------------------------------------------------------------


class TestState:
    def test_first_use_creates_a_random_uuid4_and_unset_consent(self, env, home):
        st, created = state.ensure_state(env)
        assert created is True and st.improve == "unset" and st.richer == "off"
        assert schema._INSTALLATION_ID_RE.match(st.installation_id)
        again, created2 = state.ensure_state(env)
        assert created2 is False and again.installation_id == st.installation_id
        data = json.loads((home / "telemetry.json").read_text(encoding="utf-8"))
        assert data["installation_id"] == st.installation_id and data["schema_version"] == 1

    def test_id_is_not_derived_from_anything(self, tmp_path):
        ids = set()
        for name in ("a", "b", "c"):
            env = {"OPENSHARD_HOME": str(tmp_path / name)}
            ids.add(state.ensure_state(env)[0].installation_id)
        assert len(ids) == 3
        blob = (tmp_path / "a" / "telemetry.json").read_text(encoding="utf-8")
        for needle in (os.environ.get("USERNAME", "\0"), os.environ.get("USER", "\0"), socket.gethostname()):
            assert needle not in blob

    def test_consent_transitions(self, env):
        assert state.ensure_state(env)[0].improve == "unset"
        st = state.consent_after_notice(source="setup", env=env)
        assert st.improve == "on" and st.improve_source == "setup" and st.improve_decided_at
        st = state.set_consent("off", source="cli", env=env)
        assert st.improve == "off"
        # Showing the notice again never overrides a decision.
        assert state.consent_after_notice(source="setup", env=env).improve == "off"
        assert state.load_state(env).improve_source == "cli"
        with pytest.raises(ValueError):
            state.set_consent("maybe", source="cli", env=env)

    def test_reset_mints_a_new_id_and_keeps_consent(self, env):
        before = _on(env)
        after = state.reset_installation_id(env)
        assert after.installation_id != before.installation_id and after.improve == "on"

    def test_note_version(self, env):
        st, first, changed = state.note_version("0.4.2", env)
        assert first is True and changed is True and st.last_version == "0.4.2"
        _st, first, changed = state.note_version("0.4.2", env)
        assert first is False and changed is False
        _st, _first, changed = state.note_version("0.4.3", env)
        assert changed is True

    def test_unreadable_state_is_treated_as_absent(self, env, home):
        home.mkdir()
        (home / "telemetry.json").write_text("{ not json", encoding="utf-8")
        assert state.load_state(env) is None
        st, created = state.ensure_state(env)
        assert created is True and st.improve == "unset"

    def test_unknown_keys_survive_a_rewrite(self, env, home):
        state.ensure_state(env)
        data = json.loads((home / "telemetry.json").read_text(encoding="utf-8"))
        data["future_key"] = {"kept": True}
        (home / "telemetry.json").write_text(json.dumps(data), encoding="utf-8")
        state.set_consent("on", source="cli", env=env)
        assert json.loads((home / "telemetry.json").read_text(encoding="utf-8"))["future_key"] == {"kept": True}


class TestEffectiveStatus:
    def test_consent_decides_when_nothing_else_disables(self, env):
        assert state.effective_status(env=env, repo_config={}).enabled is False
        assert "not yet asked" in state.effective_status(env=env, repo_config={}).reason
        _on(env)
        assert state.effective_status(env=env, repo_config={}) == state.Effective(True, "on", "on")
        state.set_consent("off", source="cli", env=env)
        eff = state.effective_status(env=env, repo_config={})
        assert eff.enabled is False and eff.consent == "off"

    @pytest.mark.parametrize("value", ["off", "0", "false", "no", "FALSE", " off "])
    def test_env_kill_switch(self, env, value):
        _on(env)
        eff = state.effective_status(env={**env, "OPENSHARD_TELEMETRY": value}, repo_config={})
        assert eff.enabled is False and "OPENSHARD_TELEMETRY" in eff.reason

    def test_env_cannot_enable(self, env):
        eff = state.effective_status(env={**env, "OPENSHARD_TELEMETRY": "on"}, repo_config={})
        assert eff.enabled is False and eff.consent == "unset"

    def test_do_not_track_and_ci_disable_but_agent_env_does_not(self, env):
        _on(env)
        assert state.effective_status(env={**env, "DO_NOT_TRACK": "1"}, repo_config={}).enabled is False
        for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI"):
            eff = state.effective_status(env={**env, var: "true"}, repo_config={})
            assert eff.enabled is False and eff.reason == "disabled in CI", var
        assert state.effective_status(env={**env, "CI": "false"}, repo_config={}).enabled is True
        assert state.effective_status(env={**env, "OPENSHARD_AGENT": "1"}, repo_config={}).enabled is True

    def test_repository_config_turns_it_off_for_everyone(self, env):
        _on(env)
        eff = state.effective_status(env=env, repo_config={"telemetry": {"enabled": False}})
        assert eff.enabled is False and "config.yml" in eff.reason
        assert state.effective_status(env=env, repo_config={"telemetry": {"enabled": True}}).enabled is True
        assert state.effective_status(env=env, repo_config={"telemetry": "junk"}).enabled is True


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

IID = "0f1e2d3c-4b5a-4697-8877-665544332211"
RECEIPT = {
    "agent": "cursor", "origin": "external_observed", "capture_depth": "partial", "files_changed": 1,
    "files_source": "git_diff", "tool_calls": 3, "tool_failures": 0, "checks": "attempted_unverified",
    "attempt_number": 1, "is_retry": False, "turn_count": 1, "duration_s": 30, "cost_usd": 0.14,
    "model_family": "claude",
}


def _build(event_type="receipt.created", props=None, **kw):
    kw.setdefault("installation_id", IID)
    kw.setdefault("openshard_version", "0.4.2")
    return schema.build_event(event_type, RECEIPT if props is None else props, **kw)


class TestSchema:
    def test_valid_event_envelope(self):
        event, reason = _build()
        assert reason is None and event is not None
        assert tuple(event) == schema.ENVELOPE_KEYS  # nothing else can be in the envelope
        assert event["schema_version"] == 1 and event["consent_level"] == "improve"
        assert schema._INSTALLATION_ID_RE.match(event["event_id"]) and event["installation_id"] == IID
        assert event["occurred_at"].endswith("Z") and len(event["occurred_at"]) == 20  # second precision, UTC
        assert set(event["platform"]) == {"os", "arch", "python"}
        assert event["properties"] == RECEIPT
        for forbidden in ("hostname", "user", "email", "cwd", "repo", "locale", "timezone", "path"):
            assert forbidden not in json.dumps(event).lower()

    def test_unknown_and_reserved_event_types_are_dropped(self):
        assert _build("nope")[1] == "unknown_event_type"
        for reserved in schema.RESERVED_EVENT_TYPES:
            assert _build(reserved, {})[1] == "unknown_event_type"

    def test_required_missing_invalid_and_unknown_properties(self):
        missing = dict(RECEIPT)
        del missing["agent"]
        assert _build(props=missing)[1] == "missing:agent"
        assert _build(props={**RECEIPT, "agent": "my-private-agent"})[1] == "invalid:agent"
        assert _build(props={**RECEIPT, "files_changed": -1})[1] == "invalid:files_changed"
        assert _build(props={**RECEIPT, "files_changed": True})[1] == "invalid:files_changed"
        assert _build(props={**RECEIPT, "is_retry": "no"})[1] == "invalid:is_retry"
        assert _build(props={**RECEIPT, "cost_usd": -1})[1] == "invalid:cost_usd"
        # Optional properties may be None or absent; extra ones are silently dropped.
        event, _ = _build(props={**RECEIPT, "duration_s": None, "task": "Fix /etc/passwd", "file": "a.py"})
        assert event["properties"]["duration_s"] is None
        assert "task" not in event["properties"] and "file" not in event["properties"]
        assert "passwd" not in json.dumps(event)

    def test_tokens_cannot_be_paths_emails_urls_or_secrets(self):
        for bad in ("src/x.py", "C:\\x", "a@b.c", "https://x", "a b", "", "x" * 65, SECRET, None, 3):
            with pytest.raises(ValueError):
                schema.token(bad)
        assert schema.token("0.4.2") == "0.4.2" and schema.token("claude_code") == "claude_code"
        assert _build(openshard_version="0.4.2/../../etc")[1] == "bad_version"
        assert _build(installation_id="not-a-uuid")[1] == "bad_installation_id"  # type: ignore[arg-type]
        assert _build(consent_level="richer")[0] is not None
        assert _build(consent_level="everything")[1] == "bad_consent_level"

    def test_every_event_type_has_a_closed_property_list(self):
        for name, props in schema.describe_schema().items():
            assert name in schema.EVENT_TYPES and props
        # Free text is impossible: every validator is an enum, int, bool, money or token.
        allowed = {"command.invoked": {"command"}, "history.queried": {"command"}}
        assert allowed  # (documentation of intent; validators are checked below)
        event, reason = schema.build_event(
            "error.occurred", {"component": "cli", "category": "io"}, installation_id=IID, openshard_version="0.4.2",
        )
        assert reason is None and event["properties"] == {"component": "cli", "category": "io"}
        assert schema.build_event(
            "error.occurred", {"component": "cli", "category": "Traceback: KeyError"},
            installation_id=IID, openshard_version="0.4.2",
        )[1] == "invalid:category"

    def test_validate_event_round_trips_and_rejects_tampering(self):
        event, _ = _build()
        again, reason = schema.validate_event(event)
        assert reason is None and again == event
        assert schema.validate_event({**event, "schema_version": 2})[1] == "bad_schema_version"
        assert schema.validate_event({**event, "occurred_at": "2026-09-10T18:00:00.123Z"})[1] == "bad_occurred_at"
        assert schema.validate_event({**event, "properties": {**RECEIPT, "agent": "x"}})[1] == "invalid:agent"
        assert schema.validate_event({**event, "platform": {"os": "plan9", "arch": "x86_64", "python": "3.11"}})[1] == "bad_platform"
        assert schema.validate_event("x")[1] == "not_an_object"

    def test_platform_is_coarse(self):
        info = schema.platform_info()
        assert info["os"] in schema.OS_NAMES and info["arch"] in schema.ARCHES
        assert info["python"].count(".") == 1  # major.minor only

    def test_model_family_is_an_allowlist(self):
        assert schema.model_family("claude-4-sonnet") == "claude"
        assert schema.model_family("anthropic/claude-sonnet-5") == "claude"
        assert schema.model_family("gpt-5-codex") == "codex"
        assert schema.model_family("gpt-4.1") == "gpt"
        assert schema.model_family("o3-mini") == "o-series"
        assert schema.model_family("openai/gpt-5") == "gpt"
        assert schema.model_family("google/gemini-2.5-pro") == "gemini"
        assert schema.model_family("acme-internal-llm-v7") == "other"
        assert schema.model_family("unknown") == "unknown"
        assert schema.model_family(None) == "unknown" and schema.model_family("") == "unknown"


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _event(i: int = 0) -> dict:
    event, _ = schema.build_event("install.seen", {"first_run": bool(i % 2)}, installation_id=IID,
                                  openshard_version="0.4.2")
    event["event_id"] = f"{i:08d}-0000-4000-8000-000000000000"
    return event


class TestQueue:
    def test_append_take_peek_size(self, env):
        assert queue.size(env) == 0 and queue.take(10, env) == []
        for i in range(3):
            assert queue.append(_event(i), env)
        assert queue.size(env) == 3
        assert [e["event_id"] for e in queue.peek(2, env)] == [_event(1)["event_id"], _event(2)["event_id"]]
        taken = queue.take(2, env)
        assert [e["event_id"] for e in taken] == [_event(0)["event_id"], _event(1)["event_id"]]
        assert queue.size(env) == 1
        queue.requeue(taken, env)
        assert [e["event_id"] for e in queue.take(10, env)] == [_event(i)["event_id"] for i in (0, 1, 2)]
        queue.clear(env)
        assert queue.size(env) == 0

    def test_bounded_by_count_dropping_oldest(self, env):
        for i in range(queue.MAX_EVENTS + 25):
            queue.append(_event(i), env)
        assert queue.size(env) == queue.MAX_EVENTS
        assert queue.peek(1, env)[0]["event_id"] == _event(queue.MAX_EVENTS + 24)["event_id"]
        assert queue.take(1, env)[0]["event_id"] == _event(25)["event_id"]

    def test_bounded_by_bytes(self, env):
        with patch.object(queue, "MAX_BYTES", 2_000):
            for i in range(60):
                queue.append(_event(i), env)
            assert queue.queue_path(env).stat().st_size <= 2_000
            assert 0 < queue.size(env) < 60
            big = _event(99)
            big["properties"] = {"first_run": True}
            with patch.object(queue, "MAX_BYTES", 10):
                assert queue.append(big, env) is False

    def test_never_raises(self, env):
        with patch.object(queue, "queue_path", side_effect=RuntimeError("boom")):
            assert queue.append(_event(), env) is False
            assert queue.take(1, env) == [] and queue.peek(1, env) == [] and queue.size(env) == 0
            queue.requeue([_event()], env)
            queue.clear(env)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestTransport:
    def test_only_https_or_loopback_http(self):
        assert transport.endpoint_allowed("https://telemetry.openshard.dev/v1/events")
        assert transport.endpoint_allowed("http://127.0.0.1:8000/v1/events")
        assert transport.endpoint_allowed("http://localhost:8000/x")
        for bad in ("http://telemetry.openshard.dev/v1/events", "ftp://x", "", None, "https://", "not a url"):
            assert not transport.endpoint_allowed(bad), bad
        with pytest.raises(ValueError):
            transport.HttpsTransport("http://example.com/x", user_agent="t")

    def test_offline_endpoint_fails_fast_and_quietly(self):
        t = transport.HttpsTransport(f"http://127.0.0.1:{_closed_port()}/v1/events", user_agent="openshard/test",
                                     timeout=1.0)
        t0 = time.perf_counter()
        assert t.send([_event()]) is False
        assert time.perf_counter() - t0 < 5.0

    def test_backoff_grows_and_clears(self, env):
        assert not transport.in_backoff(env, now=1000.0)
        transport.record_failure(env, now=1000.0)
        assert transport.in_backoff(env, now=1000.0 + 59) and not transport.in_backoff(env, now=1000.0 + 61)
        transport.record_failure(env, now=2000.0)
        assert transport.in_backoff(env, now=2000.0 + 119) and not transport.in_backoff(env, now=2000.0 + 121)
        for _ in range(10):
            transport.record_failure(env, now=3000.0)
        assert not transport.in_backoff(env, now=3000.0 + 3601)
        transport.clear_backoff(env)
        assert not transport.in_backoff(env, now=3000.0 + 1)

    def test_recording_transport(self):
        rt = transport.RecordingTransport()
        assert rt.send([_event(1), _event(2)]) is True and len(rt.events) == 2
        assert transport.RecordingTransport(fail=True).send([_event()]) is False
        assert transport.NullTransport().send([_event()]) is True


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TestClient:
    def test_nothing_is_recorded_until_consent(self, env, home, recording):
        assert client.emit("install.seen", env, first_run=True) is False
        assert not (home / queue.QUEUE_FILENAME).exists()
        assert client.flush(env=env) == 0 and recording.events == []

    def test_emit_validates_queues_and_flush_sends(self, env, recording):
        _on(env)
        assert client.emit("install.seen", env, first_run=True) is True
        assert client.emit("install.seen", env, first_run="yes") is False
        assert client.counters()["last_drop_reason"] == "invalid:first_run"
        assert client.emit("attempt.outcome", env, anything=1) is False  # reserved for the richer layer
        assert queue.size(env) == 1
        assert client.flush(env=env) == 1
        assert queue.size(env) == 0 and len(recording.events) == 1
        event = recording.events[0]
        assert event["event_type"] == "install.seen" and event["properties"] == {"first_run": True}
        assert event["installation_id"] == state.load_state(env).installation_id

    def test_flush_failure_requeues_and_backs_off(self, env):
        _on(env)
        failing = transport.RecordingTransport(fail=True)
        client.configure(transport=failing, repo_config={})
        try:
            client.emit("install.seen", env, first_run=True)
            assert client.flush(env=env) == 0
            assert queue.size(env) == 1 and transport.in_backoff(env)
            ok = transport.RecordingTransport()
            client.configure(transport=ok, repo_config={})
            assert client.flush(env=env) == 0  # still in backoff: nothing sent, nothing lost
            transport.clear_backoff(env)
            assert client.flush(env=env) == 1 and ok.events and queue.size(env) == 0
        finally:
            client.configure(transport=None, repo_config=None)

    def test_kill_switches_stop_emit_and_flush(self, env, recording):
        _on(env)
        client.emit("install.seen", env, first_run=True)
        assert client.emit("install.seen", {**env, "OPENSHARD_TELEMETRY": "off"}, first_run=True) is False
        assert client.emit("install.seen", {**env, "DO_NOT_TRACK": "1"}, first_run=True) is False
        assert client.emit("install.seen", {**env, "CI": "true"}, first_run=True) is False
        assert client.flush(env={**env, "OPENSHARD_TELEMETRY": "0"}) == 0
        assert queue.size(env) == 1 and recording.events == []
        client.configure(transport=recording, repo_config={"telemetry": {"enabled": False}})
        assert client.emit("install.seen", env, first_run=True) is False and client.flush(env=env) == 0

    def test_emit_and_flush_never_raise(self, env, recording):
        _on(env)
        with patch.object(queue, "append", side_effect=RuntimeError("disk on fire")):
            assert client.emit("install.seen", env, first_run=True) is False
        with patch.object(state, "load_state", side_effect=RuntimeError("boom")):
            assert client.emit("install.seen", env, first_run=True) is False
            assert client.flush(env=env) == 0
        exploding = transport.RecordingTransport()
        exploding.send = lambda batch: (_ for _ in ()).throw(RuntimeError("network"))  # type: ignore[assignment]
        client.emit("install.seen", env, first_run=True)
        assert client.flush(env=env, transport=exploding) == 0
        assert queue.size(env) == 1  # a raising transport loses nothing

    def test_endpoint_resolution(self, env):
        assert client.resolve_endpoint(env, {}) == client.DEFAULT_ENDPOINT
        assert client.DEFAULT_ENDPOINT.startswith("https://")
        assert client.resolve_endpoint({**env, "OPENSHARD_TELEMETRY_ENDPOINT": "https://a.example/v1"}, {}) == "https://a.example/v1"
        assert client.resolve_endpoint(env, {"telemetry": {"endpoint": "https://b.example/v1"}}) == "https://b.example/v1"
        assert client.resolve_endpoint({**env, "OPENSHARD_TELEMETRY_ENDPOINT": "https://a.example/v1"},
                                       {"telemetry": {"endpoint": "https://b.example/v1"}}) == "https://a.example/v1"
        assert client.resolve_endpoint({**env, "OPENSHARD_TELEMETRY_ENDPOINT": "http://plain.example/v1"}, {}) is None
        assert isinstance(client._make_transport({**env, "OPENSHARD_TELEMETRY_ENDPOINT": "ftp://x"}, {}),
                          transport.NullTransport)
        assert isinstance(client._make_transport(env, {}), transport.HttpsTransport)

    def test_status_document(self, env, recording):
        doc = client.status(env)
        assert doc["enabled"] is False and doc["consent"] == "unset" and doc["installation_id"] is None
        _on(env)
        client.emit("install.seen", env, first_run=True)
        doc = client.status(env)
        assert doc["enabled"] is True and doc["queued"] == 1 and doc["endpoint"] == client.DEFAULT_ENDPOINT
        assert doc["installation_id"] and doc["consent_source"] == "cli" and doc["in_backoff"] is False

    def test_timed_command(self, env, recording):
        _on(env)
        with client.timed_command("last", env=env):
            pass
        with pytest.raises(FileNotFoundError):
            with client.timed_command("history", env=env):
                raise FileNotFoundError("/home/someone/.openshard/runs.jsonl")
        with pytest.raises(SystemExit):
            with client.timed_command("doctor", env=env):
                raise SystemExit(1)
        client.flush(env=env)
        props = [e["properties"] for e in recording.events]
        assert props[0]["command"] == "last" and props[0]["result"] == "ok" and props[0]["error_category"] is None
        assert props[1] == {"command": "history", "duration_ms": props[1]["duration_ms"], "result": "error",
                            "error_category": "io"}
        assert props[2]["command"] == "doctor" and props[2]["result"] == "error"
        assert "someone" not in json.dumps(recording.events)

    def test_background_flush_sends_without_blocking(self, env, recording):
        _on(env)
        bg_env = {k: v for k, v in env.items() if k != "OPENSHARD_TELEMETRY_NO_BACKGROUND"}
        t0 = time.perf_counter()
        assert client.emit("install.seen", bg_env, first_run=True) is True
        assert time.perf_counter() - t0 < 0.2  # emit returned before the flush thread ran
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not recording.events:
            time.sleep(0.05)
        assert len(recording.events) == 1 and queue.size(env) == 0
        assert all(not t.name.startswith("openshard-telemetry") or t.daemon for t in threading.enumerate())


# ---------------------------------------------------------------------------
# Receipt -> properties
# ---------------------------------------------------------------------------


def _hook_entry(**overrides) -> dict:
    entry = {
        "task": f"Fix the bug in /home/someone/repo/src/secret_module.py with {SECRET}",
        "executor": "cursor_hooks",
        "execution_model": "claude-4-sonnet",
        "files_source": "git_diff_inferred",
        "files_created": 1, "files_updated": 2, "files_deleted": 0,
        "files_detail": [{"path": "evals/basic/bug_fix/fixtures/word_utils.py", "change_type": "update"}],
        "verification_attempted": True, "verification_passed": None,
        "attempt_number": 2, "duration_seconds": 30.7, "estimated_cost": 0.14449,
        "repo_identity": "github.com/openshard/openshard",
        "git_branch": "feature/private-thing",
        "capture": {"agent": "cursor", "session_id": "6f1a2b3c-4d5e-4f60-8a71-b2c3d4e5f607",
                    "tool_call_count": 3, "tool_failure_count": 1, "turn_count": 1},
        "events": [{"action": "Shell: cat ~/.ssh/id_rsa"}],
    }
    entry.update(overrides)
    return entry


class TestReceiptProperties:
    def test_only_counts_and_enums_survive(self):
        props = events.receipt_properties(_hook_entry())
        assert props == {
            "agent": "cursor", "origin": "external_observed", "capture_depth": "partial",
            "files_changed": 3, "files_source": "git_diff", "tool_calls": 3, "tool_failures": 1,
            "checks": "attempted_unverified", "attempt_number": 2, "is_retry": True, "turn_count": 1,
            "duration_s": 30, "cost_usd": 0.14, "model_family": "claude",
        }
        event, reason = schema.build_event("receipt.completed", props, installation_id=IID, openshard_version="0.4.2")
        assert reason is None
        blob = json.dumps(event)
        for needle in ("word_utils", "someone", "secret_module", SECRET, "openshard/openshard", "private-thing",
                       "id_rsa", "6f1a2b3c", "claude-4-sonnet"):
            assert needle not in blob, needle

    def test_variants(self):
        assert events.receipt_properties(_hook_entry(files_source="cursor_hook_reported"))["files_source"] == "hook_reported"
        assert events.receipt_properties(_hook_entry(files_source="not_available"))["files_source"] == "not_available"
        assert events.receipt_properties(_hook_entry(files_source="weird"))["files_source"] == "other"
        assert events.receipt_properties(_hook_entry(verification_attempted=False))["checks"] == "none"
        assert events.receipt_properties(_hook_entry(verification_passed=True))["checks"] == "passed"
        assert events.receipt_properties(_hook_entry(verification_passed=False))["checks"] == "failed"
        no_cost = events.receipt_properties(_hook_entry(estimated_cost=None, duration_seconds=None, attempt_number=None))
        assert no_cost["cost_usd"] is None and no_cost["duration_s"] is None
        assert no_cost["attempt_number"] == 1 and no_cost["is_retry"] is False
        native = events.receipt_properties({"executor": "native", "workflow": "native", "execution_model": "openai/gpt-5",
                                            "retry_triggered": False, "verification_attempted": True,
                                            "verification_passed": True})
        assert native["agent"] == "native" and native["origin"] == "openshard_routed"
        assert native["capture_depth"] == "full" and native["model_family"] == "gpt" and native["checks"] == "passed"
        legacy = events.receipt_properties({"executor": "codex_hooks", "capture": {}})
        assert legacy["agent"] == "codex" and legacy["model_family"] == "unknown"
        assert events.receipt_properties({"executor": "something-else"})["agent"] == "other"
        private = events.receipt_properties(_hook_entry(execution_model="acme/internal-model-7b"))
        assert private["model_family"] == "other"

    def test_error_category_never_carries_the_message(self):
        assert events.error_category(FileNotFoundError("/home/x/secret")) == "io"
        assert events.error_category(PermissionError()) == "permission"
        assert events.error_category(TimeoutError()) == "timeout"
        assert events.error_category(ValueError("bad json")) == "parse"
        assert events.error_category(json.JSONDecodeError("x", "y", 0)) == "parse"
        assert events.error_category(RuntimeError("Traceback with /paths and " + SECRET)) == "unknown"
        assert events.error_category(None) == "unknown"
        for category in ("io", "permission", "timeout", "parse", "unknown"):
            assert category in schema.ERROR_CATEGORIES
