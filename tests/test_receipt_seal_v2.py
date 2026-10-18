"""Seal v2 (docs/receipt-seal-v2.md): JCS, field commitments, reveals and the sync seal.

v2 adds subjects to the v1 Statement and never changes what a v1 seal
means: v1 seals keep verifying, a chain may mix both, and nothing here makes
a weaker claim read stronger (an unsealed record is not sealed by sync).
``tests/fixtures/seal-v2/vectors.json`` is shared with the Platform.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history import receipt_keys as rk
from openshard.history import receipt_seal_v2 as v2
from openshard.history import receipt_signing as rs
from openshard.history import receipt_verify as rv
from openshard.history.jsonl_store import append_jsonl
from openshard.history.receipt_identity import new_receipt_id
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.store import amend_latest_record, load_history
from openshard.util import jcs

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "seal-v2" / "vectors.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _signing_on(monkeypatch):
    monkeypatch.delenv(rs.DISABLE_ENV, raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".openshard").mkdir()
    return tmp_path


def _history(repo: Path) -> Path:
    return repo / ".openshard"


def _record(**extra) -> dict:
    entry = {
        "timestamp": "2026-10-18T10:00:00Z",
        "task": "fix the parser in secret-project",
        "workflow": "import",
        "receipt_id": new_receipt_id(),
        "shard_id": "shard_0001",
        **extra,
    }
    entry["content_hash"] = compute_shard_hash(entry)
    return entry


def _sealed(repo: Path, **extra) -> dict:
    entry = _record(**extra)
    append_jsonl(_history(repo) / "runs.jsonl", entry)
    return entry


def _envelopes(repo: Path) -> list[dict]:
    return [ln for ln in rs.read_sidecar(_history(repo)) if rs.is_envelope(ln)]


def _statement(envelope: dict) -> tuple[bytes, dict]:
    decoded = rs.envelope_payload(envelope)
    assert decoded is not None
    return decoded


def _subject(statement: dict, name: str) -> str | None:
    return rs._subject_digest(statement, name)


def _own() -> dict[str, bytes]:
    own = rk.read_public_key()
    assert own is not None
    return {own[0]: own[1]}


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args))


# ---------------------------------------------------------------------------
# JCS (RFC 8785), shared vectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS["jcs"], ids=lambda v: v["name"])
def test_jcs_vectors(vector):
    canonical = jcs.canonicalize(json.loads(vector["input_json"]))
    assert canonical.decode("utf-8") == vector["canonical"]
    assert hashlib.sha256(canonical).hexdigest() == vector["sha256"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "\ud800", {1: "x"}, b"bytes", {1.5}])
def test_jcs_refuses_what_json_cannot_say(value):
    with pytest.raises(ValueError):
        jcs.canonicalize(value)


def test_hosted_seal_vector_verifies_and_breaks_when_changed():
    hosted = VECTORS["hosted_seal"]
    envelope, key = hosted["seal"]["envelope"], hosted["seal"]["public_key"]
    keyid, raw = rk.parse_public_key(key) or (None, b"")
    assert keyid == key["keyid"]
    assert v2.sync_digest(hosted["receipt"]) == hosted["sync_sha256"]
    check = rs.verify_envelope(envelope, public_keys={keyid: raw}, trusted_keyids={keyid},
                               sync_sha256=hosted["sync_sha256"])
    assert check.verdict == hosted["expected"]["registered_key"] == rs.VERDICT_VERIFIED
    untrusted = rs.verify_envelope(envelope, public_keys={keyid: raw}, sync_sha256=hosted["sync_sha256"])
    assert untrusted.verdict == hosted["expected"]["unregistered_key"] == rs.VERDICT_UNKNOWN_KEY
    changed = dict(hosted["receipt"], task_short="something else")
    assert rs.verify_envelope(envelope, public_keys={keyid: raw}, trusted_keyids={keyid},
                              sync_sha256=v2.sync_digest(changed)).reason == "synced_receipt_changed"
    forged = json.loads(json.dumps(envelope))
    forged["signatures"][0]["sig"] = base64.b64encode(b"\0" * 64).decode()
    assert rs.verify_envelope(forged, public_keys={keyid: raw}, trusted_keyids={keyid}).reason == "bad_signature"
    body = base64.b64decode(envelope["payload"])
    assert v2.check_reveal(hosted["reveal"], statement=json.loads(body), statement_bytes=body) == "matches"


# ---------------------------------------------------------------------------
# New seals are v2; v1 still verifies
# ---------------------------------------------------------------------------


def test_new_seal_commits_every_field_with_local_salts(repo):
    entry = _sealed(repo)
    [envelope] = _envelopes(repo)
    body, statement = _statement(envelope)
    rid = entry["receipt_id"]
    assert statement["predicateType"] == v2.PREDICATE_TYPE
    assert statement["predicate"]["canonicalization"] == "https://www.rfc-editor.org/rfc/rfc8785"
    assert _subject(statement, v2.sync_subject(rid)) is None, "only sync makes a sync: subject"
    line = v2.find_salts(_history(repo), hashlib.sha256(body).hexdigest())
    assert line is not None and set(line["salts"]) == set(entry) - {"content_hash"}
    for name, salt in line["salts"].items():
        assert line["commitments"][name] == v2.commitment(rid, name, entry[name], salt)
    assert _subject(statement, v2.fields_subject(rid)) == v2.fields_digest(line["commitments"])
    # Salts and values never reach the sidecar or the Statement.
    sidecar = (_history(repo) / rs.SIDECAR_FILENAME).read_text(encoding="utf-8")
    assert all(salt not in sidecar for salt in line["salts"].values())
    assert b"secret-project" not in body and "secret-project" not in sidecar


def test_fresh_salts_per_seal_hide_whether_a_field_changed(repo):
    entry = _sealed(repo)
    amend_latest_record(_history(repo) / "runs.jsonl", "note", lambda r: r.__setitem__("note", "later"))
    first, second = (_statement(e)[0] for e in _envelopes(repo))
    a = v2.find_salts(_history(repo), hashlib.sha256(first).hexdigest())
    b = v2.find_salts(_history(repo), hashlib.sha256(second).hexdigest())
    assert a and b and a["commitments"]["task"] != b["commitments"]["task"]
    assert a["salts"]["task"] != b["salts"]["task"] and entry["task"]
    assert len(base64.urlsafe_b64decode(a["salts"]["task"] + "=")) == 32


def test_a_v1_seal_still_verifies_and_chains_into_v2(repo, monkeypatch):
    entry = _record()
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    append_jsonl(_history(repo) / "runs.jsonl", entry)
    monkeypatch.delenv(rs.DISABLE_ENV)
    stored = load_history(_history(repo) / "runs.jsonl", coerce=False)
    projection = rs.receipt_projection(stored[0], 0, [])
    statement = rs.build_statement(
        receipt_id=entry["receipt_id"], shard_id="shard_0001", record_sha256=rs.record_digest(stored[0]),
        projection_sha256=rs.projection_digest(projection), seal_reason="created",
        previous_seal_digest=None, core_version="0.4.9")
    v1 = rs.sign_statement(statement, rk.load_or_create_key())
    (_history(repo) / rs.SIDECAR_FILENAME).write_text(json.dumps(v1) + "\n", encoding="utf-8")
    result = rv.verify_local(_history(repo) / "runs.jsonl", stored[0], 0)
    assert (result.verdict, result.seal_version) == ("verified", "1")
    assert "record field commitments" not in result.to_dict()["bound_by_signature"]
    amend_latest_record(_history(repo) / "runs.jsonl", "note", lambda r: r.__setitem__("note", "later"))
    stored = load_history(_history(repo) / "runs.jsonl", coerce=False)
    result = rv.verify_local(_history(repo) / "runs.jsonl", stored[-1], len(stored) - 1)
    assert (result.verdict, result.seal_version, len(result.chain)) == ("verified", "2", 2)


def test_v2_statement_without_field_commitments_is_invalid(repo):
    entry = _sealed(repo)
    [envelope] = _envelopes(repo)
    _body, statement = _statement(envelope)
    statement["subject"] = statement["subject"][:2]
    forged = rs.sign_statement(statement, rk.load_or_create_key())
    check = rs.verify_envelope(forged, public_keys=_own(), trusted_keyids=set(_own()), receipt_id=entry["receipt_id"])
    assert (check.verdict, check.reason) == ("invalid", "malformed_statement")


def test_a_field_without_a_json_form_is_named_not_dropped(repo):
    entry = _sealed(repo, ratio=float("nan"))
    [envelope] = _envelopes(repo)
    _body, statement = _statement(envelope)
    assert statement["predicate"]["uncommitted_fields"] == ["ratio"]
    assert rv.verify_local(_history(repo) / "runs.jsonl", entry, 0).verdict == "verified"


# ---------------------------------------------------------------------------
# Reveals
# ---------------------------------------------------------------------------


def test_reveal_one_field_in_a_bundle_and_check_it_offline(repo, tmp_path):
    entry = _sealed(repo, files=["src/secret.py"])
    out = tmp_path / "bundle.json"
    result = _invoke("verify", "--signature", "--bundle", str(out), "--reveal", "files")
    assert result.exit_code == 0, result.output
    assert "revealed with their salts: files" in result.output
    bundle = json.loads(out.read_text(encoding="utf-8"))
    [reveal] = bundle["reveals"]
    assert (reveal["field"], reveal["value"]) == ("files", ["src/secret.py"])
    assert "record" not in bundle and set(reveal) == {
        "_type", "receipt_id", "statement_sha256", "field", "value", "salt", "commitments"}
    assert entry["task"] not in json.dumps(reveal), "only the revealed field's value is disclosed"
    checked = _invoke("verify", "--signature", "--file", str(out))
    assert checked.exit_code == 0 and "Revealed   files: matches its sealed commitment" in checked.output

    reveal["value"] = ["src/other.py"]
    out.write_text(json.dumps(bundle), encoding="utf-8")
    lied = rv.verify_bundle(json.loads(out.read_text(encoding="utf-8")))
    assert (lied.verdict, lied.reason, lied.reveals) == (
        "invalid", "reveal_mismatch", [{"field": "files", "check": "value_does_not_match"}])


def test_reveal_needs_a_bundle_and_a_committed_field(repo, tmp_path):
    _sealed(repo)
    assert "--reveal needs --bundle" in _invoke("verify", "--signature", "--reveal", "task").output
    result = _invoke("verify", "--signature", "--bundle", str(tmp_path / "b.json"), "--reveal", "nope")
    assert result.exit_code == 1 and "no commitment for field 'nope'" in result.output
    assert not (tmp_path / "b.json").exists()


# ---------------------------------------------------------------------------
# The sync seal
# ---------------------------------------------------------------------------


def _payload(repo: Path) -> tuple[dict, str]:
    from openshard.sync.envelope import receipt_payload

    entries = load_history(_history(repo) / "runs.jsonl", coerce=True)
    return receipt_payload(entries[-1], len(entries) - 1), entries[-1]["content_hash"]


def test_sync_seal_names_the_exact_hosted_receipt(repo):
    entry = _sealed(repo)
    receipt, record_hash = _payload(repo)
    digest = v2.sync_digest(receipt)
    outcome = rs.seal_for_sync(_history(repo), entry["receipt_id"], record_hash=record_hash, sync_sha256=digest)
    assert (outcome.status, outcome.reason) == ("sealed", "synced") and outcome.envelope
    first, synced = _envelopes(repo)
    _body, statement = _statement(synced)
    assert statement["predicate"]["previous_record_digest"] == rs.seal_digest(first)
    assert _subject(statement, v2.sync_subject(entry["receipt_id"])) == digest
    check = rs.verify_envelope(synced, public_keys=_own(), trusted_keyids=set(_own()), sync_sha256=digest)
    assert check.verdict == "verified"
    # A retry of the same send reuses the seal instead of growing the chain.
    again = rs.seal_for_sync(_history(repo), entry["receipt_id"], record_hash=record_hash, sync_sha256=digest)
    assert (again.reason, again.envelope) == ("already_sealed", synced) and len(_envelopes(repo)) == 2
    # Locally the chain still verifies, and says it names the hosted copy.
    result = rv.verify_local(_history(repo) / "runs.jsonl", entry, 0)
    assert (result.verdict, result.sync_sealed) == ("verified", True)
    assert "hosted receipt digest" in result.to_dict()["bound_by_signature"]


def test_sync_never_seals_after_the_fact(repo, monkeypatch):
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    entry = _sealed(repo)
    receipt, record_hash = _payload(repo)
    digest = v2.sync_digest(receipt)
    assert rs.seal_for_sync(_history(repo), entry["receipt_id"], record_hash=record_hash,
                            sync_sha256=digest).reason == "signing_disabled"
    monkeypatch.delenv(rs.DISABLE_ENV)
    never = rs.seal_for_sync(_history(repo), entry["receipt_id"], record_hash=record_hash, sync_sha256=digest)
    assert (never.reason, never.envelope) == ("never_sealed", None) and not _envelopes(repo)


def test_sync_seal_refuses_a_record_that_changed_since_sync_read_it(repo):
    entry = _sealed(repo)
    receipt, _record_hash = _payload(repo)
    outcome = rs.seal_for_sync(_history(repo), entry["receipt_id"], record_hash="sha256:" + "0" * 64,
                               sync_sha256=v2.sync_digest(receipt))
    assert (outcome.reason, outcome.envelope) == ("record_changed_since_discovery", None)
    assert len(_envelopes(repo)) == 1


def test_salts_file_is_never_read_by_sync_code():
    sync_dir = Path(rs.__file__).resolve().parents[1] / "sync"
    for module in sync_dir.rglob("*.py"):
        assert v2.SALTS_FILENAME not in module.read_text(encoding="utf-8"), module
