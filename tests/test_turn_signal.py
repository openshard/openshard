"""The open-turn signal (docs/per-turn-receipts.md, "In progress").

With per-turn Receipts, the prompt Event that opens a turn names the turn's
Receipt (``turn_opened``, ``receipt_id``, ``receipt_scope``, ``turn_index``)
and the Stop that seals it adds a ``receipt.sealed`` Event. Both are ordinary
Events, so a linked session streams them through remote capture while the
turn is open. Nothing reaches the network: the spool is read directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openshard.adapters import claude_turn_receipts as turns
from openshard.history.store import load_history
from openshard.remote import config as rconfig
from openshard.remote import spool
from tests.capture_fixtures import _make_repo
from tests.test_remote_capture import CAPTURE_ID, ORG, RUNS, TOKEN, URL, _hook

_REMOTE_METADATA_KEYS = 24  # Platform remote-capture contract: metadata keys per Event


@pytest.fixture
def attached(tmp_path: Path, monkeypatch) -> rconfig.Attachment:
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "vm-home"))
    monkeypatch.setenv("OPENSHARD_REMOTE_NO_SPAWN", "1")
    for name in (rconfig.URL_ENV, rconfig.TOKEN_ENV, rconfig.DISABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    return rconfig.save_attachment(capture_url=URL, token=TOKEN, organisation_id=ORG, agent="claude-code")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "workspace")


def _turn(repo: Path, prompt: str, name: str, *, stop: bool = True) -> None:
    _hook(repo, "UserPromptSubmit", prompt=prompt)
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _hook(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / name)},
          tool_response={"filePath": str(repo / name)})
    if stop:
        _hook(repo, "Stop")


def _spooled() -> list[dict]:
    events, _ = spool.pending(None, CAPTURE_ID, limit=500)
    return events


def test_a_linked_session_streams_which_turn_is_open_and_when_it_seals(attached, repo, monkeypatch):
    monkeypatch.setenv(turns.TURN_RECEIPTS_ENV, "on")
    _hook(repo, "SessionStart", source="startup")
    _turn(repo, "add a", "a.py")
    _turn(repo, "add b", "b.py", stop=False)  # turn 2 is open: no Stop yet
    first, second = load_history(repo / RUNS, coerce=False)
    events = _spooled()
    opened = [e["metadata"] for e in events if e["metadata"].get("turn_opened") is True]
    assert [(m["receipt_scope"], m["turn_index"], m["receipt_id"]) for m in opened] == [
        ("turn", 1, first["receipt_id"]), ("turn", 2, second["receipt_id"]),
    ]
    sealed = [e for e in events if e["event_type"] == "receipt.sealed"]
    assert [e["metadata"]["receipt_id"] for e in sealed] == [first["receipt_id"]]  # the open turn is not sealed
    assert sealed[0]["metadata"]["turn_status"] == "turn_completed" and sealed[0]["status"] == "passed"
    # The open turn's Receipt is on disk, unsealed: the signal names a Receipt that exists.
    assert "turn_sealed_at" not in second["capture"] and second["capture"]["receipt_scope"] == "turn"
    # The sealed Receipt keeps the Events it streamed.
    assert any(e["event_type"] == "receipt.sealed" for e in first["events"])
    for event in events:  # the remote-capture contract's metadata shape
        metadata = event["metadata"]
        assert len(metadata) <= _REMOTE_METADATA_KEYS
        assert all(isinstance(v, (str, int, bool)) or v is None for v in metadata.values())


def test_session_receipts_stream_no_turn_signal(attached, repo, monkeypatch):
    monkeypatch.setenv(turns.TURN_RECEIPTS_ENV, "off")
    _hook(repo, "SessionStart", source="startup")
    _turn(repo, "add a", "a.py")
    _turn(repo, "add b", "b.py")
    events = _spooled()
    assert events  # the session still streams
    assert not any("turn_opened" in e["metadata"] for e in events)
    assert not any(e["event_type"] == "receipt.sealed" for e in events)
