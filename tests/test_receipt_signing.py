"""Local Ed25519 seals over Receipts (docs/signing.md, Phase 1, part 1).

A seal claims only that the record is unchanged since it was sealed with
this install's key. These tests pin the key handling, when seals are made,
the seal chain, how tampering shows up, that sealing never fails or blocks a
Receipt write, and that nothing about it reaches the Platform sync payload.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.history import receipt_keys as rk
from openshard.history import receipt_signing as rs
from openshard.history.jsonl_store import append_jsonl, upsert_jsonl
from openshard.history.receipt_identity import new_receipt_id
from openshard.history.receipt_render import render_compact_shard_receipt, with_signature_row
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.store import amend_latest_record, load_history

POSIX = os.name != "nt"


@pytest.fixture(autouse=True)
def _signing_on(monkeypatch):
    monkeypatch.delenv(rs.DISABLE_ENV, raising=False)


@pytest.fixture
def history(tmp_path) -> Path:
    directory = tmp_path / "repo" / ".openshard"
    directory.mkdir(parents=True)
    return directory


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _record(**extra) -> dict:
    entry = {
        "timestamp": "2026-10-18T10:00:00Z",
        "task": "fix the parser",
        "workflow": "import",
        "receipt_id": new_receipt_id(),
        "shard_id": "shard_0001",
        **extra,
    }
    entry["content_hash"] = compute_shard_hash(entry)
    return entry


def _envelopes(history: Path) -> list[dict]:
    return [line for line in rs.read_sidecar(history) if rs.is_envelope(line)]


def _statement(envelope: dict) -> dict:
    decoded = rs.envelope_payload(envelope)
    assert decoded is not None
    return decoded[1]


def _own_keys() -> dict[str, bytes]:
    own = rk.read_public_key()
    assert own is not None
    return {own[0]: own[1]}


def check_tamper(history: Path, receipt_id: str) -> rs.SignatureCheck:
    """Helper: the latest seal of *receipt_id* against the record as it is now."""
    stored = load_history(history / "runs.jsonl", coerce=False)
    index = next(i for i, e in enumerate(stored) if e.get("receipt_id") == receipt_id)
    return rs.signature_status(history / "runs.jsonl", stored[index], index)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_key_is_created_lazily_on_first_seal(history):
    key_file = rk.keys_dir() / rk.PRIVATE_KEY_FILENAME
    assert not key_file.exists()
    live = _record(capture={"session_end_observed": False, "last_activity_at": _now()})
    append_jsonl(history / "runs.jsonl", live)
    assert not key_file.exists(), "a record still changing is not sealed, so no key is made"
    append_jsonl(history / "runs.jsonl", _record())
    assert key_file.is_file()


@pytest.mark.skipif(not POSIX, reason="POSIX modes; Windows uses an owner-only ACL")
def test_key_file_is_owner_only():
    key = rk.load_or_create_key()
    directory = rk.keys_dir()
    assert stat.S_IMODE((directory / rk.PRIVATE_KEY_FILENAME).stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert key.owner_only is True


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_a_key_left_group_readable_is_tightened():
    rk.load_or_create_key()
    key_file = rk.keys_dir() / rk.PRIVATE_KEY_FILENAME
    os.chmod(key_file, 0o644)
    assert rk.load_or_create_key().owner_only is True
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_keyid_and_fingerprint():
    key = rk.load_or_create_key()
    assert key.keyid == "ed25519:" + hashlib.sha256(key.public_raw).hexdigest()
    assert len(key.public_raw) == 32
    assert key.fingerprint8 == key.keyid[len("ed25519:"):][:8]
    public = json.loads((rk.keys_dir() / rk.PUBLIC_KEY_FILENAME).read_text())
    assert public["keyid"] == key.keyid
    assert base64.b64decode(public["public_key"]) == key.public_raw
    assert rk.load_or_create_key().keyid == key.keyid, "the same key is reused"


def test_concurrent_first_use_creates_one_key():
    keyids: list[str] = []

    def worker() -> None:
        keyids.append(rk.load_or_create_key().keyid)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keyids)) == 1


def test_key_lives_in_openshard_home_with_awkward_path(tmp_path, monkeypatch):
    home = tmp_path / "Program Files" / "Ünïcode home"
    monkeypatch.setenv("OPENSHARD_HOME", str(home))
    key = rk.load_or_create_key()
    assert (home / "keys" / rk.PRIVATE_KEY_FILENAME).is_file()
    assert rk.read_public_key() == (key.keyid, key.public_raw)


def test_windows_acl_is_applied_once_and_failure_is_reported(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    class Done:
        returncode = 1

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(rk.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "dev")
    monkeypatch.setattr(rk.subprocess, "run", fake_run)
    monkeypatch.setattr(rk, "_WINDOWS_CONFIRMED", {})
    target = tmp_path / "k"
    target.write_text("x")
    assert rk._owner_only(target) is False
    assert rk._owner_only(target) is False
    assert len(calls) == 1
    assert calls[0][:2] == ["icacls", str(target)] and "/inheritance:r" in calls[0]


def test_an_unreadable_key_is_never_replaced(history):
    rk.load_or_create_key()
    key_file = rk.keys_dir() / rk.PRIVATE_KEY_FILENAME
    key_file.write_text("not a key")
    append_jsonl(history / "runs.jsonl", entry := _record())
    assert key_file.read_text() == "not a key"
    latest = rs.read_sidecar(history)[-1]
    assert latest["_type"] == rs.NOT_SIGNED_TYPE
    assert (latest["receipt_id"], latest["reason"]) == (entry["receipt_id"], "key_corrupt")


def test_private_key_never_reaches_the_sidecar(history):
    append_jsonl(history / "runs.jsonl", _record())
    pem = (rk.keys_dir() / rk.PRIVATE_KEY_FILENAME).read_text()
    body = "".join(pem.strip().splitlines()[1:-1])
    assert body and body not in (history / rs.SIDECAR_FILENAME).read_text()


# ---------------------------------------------------------------------------
# Sealing and the seal chain
# ---------------------------------------------------------------------------


def test_one_shot_record_is_sealed_when_written(history):
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    [envelope] = _envelopes(history)
    statement = _statement(envelope)
    predicate = statement["predicate"]
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert envelope["payloadType"] == "application/vnd.in-toto+json"
    assert statement["subject"][0] == {
        "name": f"record:{entry['receipt_id']}",
        "digest": {"sha256": entry["content_hash"].split(":", 1)[1]},
    }
    assert statement["subject"][1]["name"] == f"projection:{entry['receipt_id']}"
    # New seals are seal v2 (docs/receipt-seal-v2.md); v1 seals still verify.
    assert statement["subject"][2]["name"] == f"record_fields:{entry['receipt_id']}"
    assert predicate["seal_version"] == "2"
    assert predicate["seal_reason"] == "created"
    assert predicate["previous_record_digest"] is None
    assert predicate["covers"] == ["record_content", "record_fields", "synced_receipt"]
    assert predicate["does_not_cover"] == [
        "agent_claims", "verification_outcome", "capture_completeness", "sealed_at",
    ]
    check = rs.verify_envelope(envelope, public_keys=_own_keys(), trusted_keyids=set(_own_keys()),
                               receipt_id=entry["receipt_id"])
    assert check.verdict == rs.VERDICT_VERIFIED
    # runs.jsonl is untouched by sealing.
    assert load_history(history / "runs.jsonl", coerce=False) == [entry]


def test_dsse_signature_is_over_pae_of_the_statement_bytes(history):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    append_jsonl(history / "runs.jsonl", _record())
    [envelope] = _envelopes(history)
    body = base64.b64decode(envelope["payload"])
    pae = b"DSSEv1 28 application/vnd.in-toto+json %d " % len(body) + body
    [(keyid, raw)] = _own_keys().items()
    assert envelope["signatures"][0]["keyid"] == keyid
    Ed25519PublicKey.from_public_bytes(raw).verify(base64.b64decode(envelope["signatures"][0]["sig"]), pae)


def test_live_hook_record_is_sealed_at_session_end(history):
    runs = history / "runs.jsonl"
    rid = new_receipt_id()

    def hook_record(**capture) -> dict:
        entry = {"timestamp": "2026-10-18T10:00:00Z", "task": "t", "receipt_id": rid,
                 "capture": {"session_end_observed": False, "last_activity_at": _now(), **capture}}
        entry["content_hash"] = compute_shard_hash(entry)
        return entry

    def match(e: dict) -> bool:
        return e.get("receipt_id") == rid

    upsert_jsonl(runs, hook_record(), match)
    upsert_jsonl(runs, hook_record(turns=2), match)
    assert _envelopes(history) == []
    upsert_jsonl(runs, hook_record(session_end_observed=True, status="ended"), match)
    [envelope] = _envelopes(history)
    assert _statement(envelope)["predicate"]["seal_reason"] == "session_ended"


def test_real_hook_session_is_sealed_once_at_session_end(tmp_path, receipt_scope):
    from openshard.adapters.claude_hooks import handle_hook
    from tests.capture_fixtures import _make_repo

    root = _make_repo(tmp_path / "hookrepo")
    sid = "5ea15ea1-0000-4000-8000-000000000001"
    history = root / ".openshard"

    def fire(event: str) -> None:
        handle_hook({"session_id": sid, "cwd": str(root), "hook_event_name": event,
                     "prompt": "task", "reason": "other"})

    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        fire(event)
    if receipt_scope == "session":
        assert _envelopes(history) == [], "a live session is not sealed at Stop"
    else:  # a completed turn is final: sealed at its Stop, and the SessionEnd seals nothing more
        [at_stop] = _envelopes(history)
        assert _statement(at_stop)["predicate"]["seal_reason"] == "turn_completed"
    fire("SessionEnd")
    [envelope] = _envelopes(history)
    assert _statement(envelope)["predicate"]["seal_reason"] == (
        "session_ended" if receipt_scope == "session" else "turn_completed")
    [entry] = load_history(history / "runs.jsonl", coerce=False)
    assert check_tamper(history, entry["receipt_id"]).verdict == rs.VERDICT_VERIFIED


def test_idle_swept_record_is_sealed():
    stale = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat()
    entry = _record(capture={"session_end_observed": False, "last_activity_at": stale})
    assert rs.seal_point(entry) == rs.SEAL_IDLE_SWEPT


@pytest.mark.parametrize("capture", [
    None,
    {"session_end_observed": True},
    {"session_end_observed": False, "status": "ended"},
    {"session_end_observed": False, "last_activity_at": "2020-01-01T00:00:00Z"},
    {"session_end_observed": False, "last_activity_at": "now"},
    {"session_end_observed": False,
     "completeness": {"reasons": [{"kind": "session_end_not_observed"}]}},
])
def test_seal_point_matches_sync_eligibility(capture):
    from openshard.sync.envelope import eligibility

    if capture and capture.get("last_activity_at") == "now":
        capture = {**capture, "last_activity_at": _now()}
    entry = _record(**({"capture": capture} if capture is not None else {}))
    assert (rs.seal_point(entry) is not None) == eligibility(entry).eligible
    assert rs.seal_point({"task": "no id"}) is None


def test_note_and_verify_attestation_add_chained_seals(history):
    from openshard.verification.post_session import record_attestation

    runs = history / "runs.jsonl"
    entry = _record()
    append_jsonl(runs, entry)
    amend_latest_record(runs, "note", lambda r: r.__setitem__("note", "looked fine"))
    record_attestation(history.parent, {
        "kind": "post_session_verification", "receipt_id": entry["receipt_id"],
        "attestation_id": "att_1", "created_at": _now(), "commit": "a" * 40,
        "verification": {"status": "passed"},
    })
    envelopes = _envelopes(history)
    reasons = [_statement(e)["predicate"]["seal_reason"] for e in envelopes]
    # The note changed the record: a chained re-seal. The attestation lives in
    # its own file and is not part of the history --json projection today, so
    # the sealed content did not change and no seal is added (and none claims
    # to cover the attestation).
    assert reasons == ["created", "amended"]
    assert rs.chain_problems(envelopes) == []
    for earlier, later in zip(envelopes, envelopes[1:], strict=False):
        assert _statement(later)["predicate"]["previous_record_digest"] == rs.seal_digest(earlier)
    first, second = _statement(envelopes[0]), _statement(envelopes[1])
    assert first["subject"][0]["digest"] != second["subject"][0]["digest"], "the note changed the record"
    assert check_tamper(history, entry["receipt_id"]).verdict == rs.VERDICT_VERIFIED


def test_attestation_changing_the_projection_reseals(history, monkeypatch):
    runs = history / "runs.jsonl"
    entry = _record()
    append_jsonl(runs, entry)
    monkeypatch.setattr(rs, "projection_digest", lambda _p: "f" * 64)
    [outcome] = rs.reseal_after_attestation(history, {"receipt_id": entry["receipt_id"],
                                                      "kind": "post_session_verification"})
    assert (outcome.status, outcome.reason) == ("sealed", "amended")
    envelopes = _envelopes(history)
    assert _statement(envelopes[1])["subject"][1]["digest"]["sha256"] == "f" * 64
    assert rs.chain_problems(envelopes) == []


def test_amending_a_never_sealed_record_does_not_seal_it(history, monkeypatch):
    runs = history / "runs.jsonl"
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    append_jsonl(runs, _record())
    monkeypatch.delenv(rs.DISABLE_ENV)
    amend_latest_record(runs, "note", lambda r: r.__setitem__("note", "x"))
    assert _envelopes(history) == []


def test_rewriting_the_same_content_does_not_add_a_seal(history):
    runs = history / "runs.jsonl"
    entry = _record()
    append_jsonl(runs, entry)
    upsert_jsonl(runs, entry, lambda e: e.get("receipt_id") == entry["receipt_id"])
    assert len(_envelopes(history)) == 1


_CONCURRENT_WRITER = """
import sys
from openshard.history.jsonl_store import append_jsonl
from openshard.history.receipt_identity import new_receipt_id
from openshard.history.shard_hash import compute_shard_hash
for n in range(int(sys.argv[2])):
    entry = {"timestamp": "2026-10-18T10:00:00Z", "task": "t", "workflow": "import",
             "receipt_id": new_receipt_id(), "shard_id": f"shard_{n}"}
    entry["content_hash"] = compute_shard_hash(entry)
    append_jsonl(sys.argv[1], entry)
"""


def test_concurrent_writers_each_get_one_whole_seal(history):
    """Separate processes (capture service and CLI) seal into one sidecar
    without torn lines, lost seals or a second key."""
    import subprocess

    runs = history / "runs.jsonl"
    procs = [
        subprocess.Popen([sys.executable, "-c", _CONCURRENT_WRITER, str(runs), "5"], env=dict(os.environ))
        for _ in range(4)
    ]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    raw_lines = (history / rs.SIDECAR_FILENAME).read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line) for line in raw_lines), "every sidecar line is whole JSON"
    stored = load_history(runs, coerce=False)
    assert len(stored) == 20
    envelopes = _envelopes(history)
    assert len(envelopes) == 20 and len({e["signatures"][0]["keyid"] for e in envelopes}) == 1
    for entry in stored:
        assert check_tamper(history, entry["receipt_id"]).verdict == rs.VERDICT_VERIFIED


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_edit_with_recomputed_hash_is_caught(history):
    runs = history / "runs.jsonl"
    entry = _record()
    append_jsonl(runs, entry)
    assert check_tamper(history, entry["receipt_id"]).verdict == rs.VERDICT_VERIFIED
    forged = {k: v for k, v in entry.items() if k != "content_hash"}
    forged["task"] = "something else"
    forged["content_hash"] = compute_shard_hash(forged)
    runs.write_text(json.dumps(forged) + "\n")
    check = check_tamper(history, entry["receipt_id"])
    assert (check.verdict, check.reason) == (rs.VERDICT_INVALID, "record_changed")
    assert rs.signature_label(check) == "Invalid (record changed after it was sealed)"


def test_edited_envelope_is_caught(history):
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    [envelope] = _envelopes(history)
    statement = _statement(envelope)
    statement["predicate"]["seal_reason"] = "session_ended"
    envelope["payload"] = base64.b64encode(json.dumps(statement).encode()).decode()
    check = rs.verify_envelope(envelope, public_keys=_own_keys(), receipt_id=entry["receipt_id"])
    assert (check.verdict, check.reason) == (rs.VERDICT_INVALID, "bad_signature")


def test_seal_by_another_key_is_never_shown_as_verified(history):
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    [envelope] = _envelopes(history)
    check = rs.verify_envelope(envelope, public_keys=_own_keys(), trusted_keyids=set(),
                               receipt_id=entry["receipt_id"])
    assert check.verdict == rs.VERDICT_UNKNOWN_KEY
    missing = rs.verify_envelope(envelope, public_keys={}, receipt_id=entry["receipt_id"])
    assert missing.verdict == rs.VERDICT_KEY_UNAVAILABLE
    assert "Signed" not in rs.signature_label(missing)


def test_broken_chain_is_reported(history):
    runs = history / "runs.jsonl"
    append_jsonl(runs, _record())
    amend_latest_record(runs, "note", lambda r: r.__setitem__("note", "x"))
    envelopes = _envelopes(history)
    assert rs.chain_problems(envelopes[1:]) == ["seal 0: does not link to the previous seal"]


# ---------------------------------------------------------------------------
# Failure modes: a Receipt is always written
# ---------------------------------------------------------------------------


def test_no_crypto_records_not_signed(history, monkeypatch):
    def unavailable(env=None):
        raise rk.KeyUnavailableError("crypto_unavailable")

    monkeypatch.setattr(rs, "load_or_create_key", unavailable)
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    assert load_history(history / "runs.jsonl", coerce=False) == [entry]
    [line] = rs.read_sidecar(history)
    assert (line["_type"], line["reason"]) == (rs.NOT_SIGNED_TYPE, "crypto_unavailable")
    assert rs.signature_label(check_tamper(history, entry["receipt_id"])) == "Checksum only (not signed)"


def test_unexpected_error_never_fails_the_write(history, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rs, "sign_statement", boom)
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    assert load_history(history / "runs.jsonl", coerce=False) == [entry]
    assert rs.read_sidecar(history)[-1]["reason"] == "seal_failed:RuntimeError"


def test_unwritable_sidecar_is_recorded_in_home(history):
    (history / rs.SIDECAR_FILENAME).mkdir()
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    assert load_history(history / "runs.jsonl", coerce=False) == [entry]
    failures = rk.keys_dir().parent / rs.FAILURES_FILENAME
    line = json.loads(failures.read_text().splitlines()[-1])
    assert line["receipt_id"] == entry["receipt_id"]
    assert line["reason"] in {"sidecar_unwritable", "seal_failed:IsADirectoryError",
                              "seal_failed:PermissionError"}


def test_content_hash_mismatch_is_never_sealed(history):
    entry = _record()
    entry["task"] = "edited after hashing"
    append_jsonl(history / "runs.jsonl", entry)
    assert _envelopes(history) == []
    assert rs.read_sidecar(history)[-1]["reason"] == "content_hash_mismatch"


def test_record_without_content_hash_is_skipped(history):
    entry = _record()
    del entry["content_hash"]
    append_jsonl(history / "runs.jsonl", entry)
    assert rs.read_sidecar(history) == []
    # No checksum, so the row never says "Checksum only".
    assert rs.signature_label(check_tamper(history, entry["receipt_id"])) == "Not signed"


@pytest.mark.parametrize("value", ["off", "0", "false", "No", "disabled"])
def test_env_turns_signing_off(history, monkeypatch, value):
    monkeypatch.setenv(rs.DISABLE_ENV, value)
    append_jsonl(history / "runs.jsonl", _record())
    assert not (history / rs.SIDECAR_FILENAME).exists()
    assert not (rk.keys_dir() / rk.PRIVATE_KEY_FILENAME).exists()


def test_repo_config_turns_signing_off(history):
    (history / "config.yml").write_text("receipt_signing: false\n")
    append_jsonl(history / "runs.jsonl", _record())
    assert not (history / rs.SIDECAR_FILENAME).exists()


def test_global_config_turns_signing_off(history):
    home = rk.keys_dir().parent
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yml").write_text("receipt_signing: false\n")
    assert rs.signing_enabled(history) is False


def test_unreadable_config_keeps_signing_on(history):
    (history / "config.yml").write_text("receipt_signing: [unclosed\n")
    assert rs.signing_enabled(history) is True


def test_amending_after_signing_was_turned_off_reads_unsigned_not_invalid(history, monkeypatch):
    runs = history / "runs.jsonl"
    entry = _record()
    append_jsonl(runs, entry)
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    amend_latest_record(runs, "note", lambda r: r.__setitem__("note", "x"))
    check = check_tamper(history, entry["receipt_id"])
    assert (check.verdict, check.reason) == (rs.VERDICT_NOT_SIGNED, "signing_disabled")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_receipt_shows_signed_with_fingerprint(history):
    entry = _record()
    append_jsonl(history / "runs.jsonl", entry)
    label = rs.signature_label(check_tamper(history, entry["receipt_id"]))
    assert label == f"Signed · key {rk.load_or_create_key().fingerprint8}"
    text = with_signature_row(render_compact_shard_receipt(build_shard_receipt(entry, 0)), label)
    lines = text.splitlines()
    integrity = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Integrity"))
    assert lines[integrity + 1].split() == ["Signature", "Signed", "·", "key", label.split()[-1]]


def test_old_receipt_shows_checksum_only():
    entry = _record()
    assert rs.signature_label(rs.SignatureCheck(rs.VERDICT_NOT_SIGNED, "no_seal")) == "Checksum only (not signed)"
    plain = render_compact_shard_receipt(build_shard_receipt(entry, 0))
    assert with_signature_row(plain, None) == plain


def test_last_command_prints_the_signature_row(tmp_path, monkeypatch):
    from openshard.cli.main import cli

    monkeypatch.chdir(tmp_path)
    runs = Path(".openshard") / "runs.jsonl"
    append_jsonl(runs, _record())
    result = CliRunner().invoke(cli, ["last"])
    assert result.exit_code == 0, result.output
    assert any(ln.split()[:3] == ["Signature", "Signed", "·"] for ln in result.output.splitlines())


# ---------------------------------------------------------------------------
# Platform sync is unchanged
# ---------------------------------------------------------------------------


def test_sync_payload_is_identical_with_and_without_a_seal(history, monkeypatch):
    from openshard.sync.envelope import build_envelope

    entry = _record()
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    append_jsonl(history / "runs.jsonl", entry)
    unsigned = build_envelope(load_history(history / "runs.jsonl")[0], 0, core_version="0.5.0")
    monkeypatch.delenv(rs.DISABLE_ENV)
    rs.seal_after_write(history / "runs.jsonl", entry)
    assert _envelopes(history), "the record is now sealed"
    signed = build_envelope(load_history(history / "runs.jsonl")[0], 0, core_version="0.5.0")
    assert signed == unsigned
    blob = json.dumps(signed)
    assert "signature" not in blob and "ed25519" not in blob and "dsse" not in blob.lower()


def test_sync_code_never_reads_the_sidecar():
    """Sync asks for one seal through ``seal_for_sync`` (seal v2, sync/seal.py) and nothing else."""
    sync_dir = Path(rs.__file__).resolve().parents[1] / "sync"
    for module in sync_dir.rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert rs.SIDECAR_FILENAME not in text and "read_sidecar" not in text, module
        if module.name != "seal.py":
            assert "receipt_signing" not in text, module
    seal_module = (sync_dir / "seal.py").read_text(encoding="utf-8")
    assert "from openshard.history.receipt_signing import seal_for_sync\n" in seal_module
    assert seal_module.count("receipt_signing") == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path separator check")
def test_sidecar_sits_beside_runs_jsonl(history):
    assert rs.sidecar_path(history) == history / "receipt-signatures.jsonl"
