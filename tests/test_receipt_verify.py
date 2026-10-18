"""``openshard verify --signature``, bundles, ``openshard keys`` and the doctor's
signing block (docs/signing.md, Phase 1, part 2).

The check runs nothing and needs no network. Each verdict is pinned with its
wording, exit code and JSON shape; so are tampering, a broken seal chain, an
unknown key, an offline bundle round trip and the rule that no private key
material is ever printed or exported.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.cli.signing_cmd import doctor_checks
from openshard.history import receipt_keys as rk
from openshard.history import receipt_signing as rs
from openshard.history import receipt_verify as rv
from openshard.history.jsonl_store import append_jsonl
from openshard.history.receipt_identity import new_receipt_id
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.store import amend_latest_record, load_history


@pytest.fixture(autouse=True)
def _signing_on(monkeypatch):
    monkeypatch.delenv(rs.DISABLE_ENV, raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".openshard").mkdir()
    return tmp_path


@pytest.fixture
def offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("verify --signature must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


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


def _runs(repo: Path) -> Path:
    return repo / ".openshard" / "runs.jsonl"


def _sealed(repo: Path) -> dict:
    entry = _record()
    append_jsonl(_runs(repo), entry)
    return entry


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args))


def _rows(output: str) -> dict[str, str]:
    """Label -> text for the two-column verdict lines."""
    return {line[:11].strip(): line[11:] for line in output.splitlines() if line[:11].strip()}


def _private_material() -> list[str]:
    pem = (rk.keys_dir() / rk.PRIVATE_KEY_FILENAME).read_text()
    body = "".join(pem.strip().splitlines()[1:-1])
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    return [body, base64.b64encode(raw).decode(), raw.hex(), "PRIVATE KEY"]


def _forge_record(repo: Path, entry: dict) -> None:
    forged = {k: v for k, v in entry.items() if k != "content_hash"}
    forged["task"] = "something else"
    forged["content_hash"] = compute_shard_hash(forged)
    _runs(repo).write_text(json.dumps(forged) + "\n")


# ---------------------------------------------------------------------------
# The four verdicts
# ---------------------------------------------------------------------------


def test_verified(repo, offline):
    entry = _sealed(repo)
    result = _invoke("verify", "--signature")
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    fp = rk.load_or_create_key().fingerprint8
    assert rows["Receipt"] == f"{entry['receipt_id']} (this repository)"
    assert rows["Signature"].startswith(f"verified — sealed by ed25519:{fp}… (this install) at 20")
    assert rows["Record"] == "matches the sealed content (record digest checked)"
    assert rows["Projection"] == "matches the sealed copy (projection digest checked)"
    assert rows["Chain"] == "1 seal (created)"
    assert rows["Means"].startswith("the record can't be changed afterwards without this check failing")
    assert "self-asserted" in rows["Claimed"]
    assert rows["Limit"].startswith("a coding agent running as your OS user can read this key")
    assert rows["Not proven"] == "the agent's claims, the check results, or capture completeness"


def test_not_signed_reads_checksum_only_never_failed(repo, monkeypatch, offline):
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    _sealed(repo)
    result = _invoke("verify", "--signature")
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert rows["Signature"] == "Checksum only (not signed) — no seal for this receipt"
    assert rows["Checksum"] == "content_hash valid"
    assert "fail" not in result.output.lower() and "invalid" not in result.output.lower()
    assert _invoke("verify", "--signature", "--strict").exit_code == 2


def test_not_signed_after_signing_was_turned_off_says_it_was_sealed_before(repo, monkeypatch):
    _sealed(repo)
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "x"))
    result = _invoke("verify", "--signature")
    assert result.exit_code == 0
    assert _rows(result.output)["Signature"] == (
        "Checksum only (not signed) — earlier seals exist, but the latest change was not sealed (signing_disabled)")


def test_tampered_record_is_invalid(repo, offline):
    entry = _sealed(repo)
    _forge_record(repo, entry)
    result = _invoke("verify", "--signature")
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Signature"].startswith("invalid — record changed after it was sealed (sealed ")
    assert rows["Record"].startswith("differs from the sealed content")
    assert rows["Means"].startswith("do not rely on this receipt")


def test_broken_chain_is_invalid(repo):
    _sealed(repo)
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "one"))
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "two"))
    sidecar = rs.sidecar_path(repo / ".openshard")
    lines = sidecar.read_text().splitlines()
    assert len(lines) == 3
    sidecar.write_text("\n".join([lines[0], lines[2]]) + "\n")  # drop the middle seal
    result = _invoke("verify", "--signature", "--json")
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert (body["verdict"], body["signature"]["reason"]) == ("invalid", "chain_broken")
    assert body["signature"]["chain_problems"] == ["seal 1: does not link to the previous seal"]


def test_amendments_form_a_verified_chain(repo):
    _sealed(repo)
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "x"))
    result = _invoke("verify", "--signature")
    assert result.exit_code == 0
    assert _rows(result.output)["Chain"] == "2 seals, each linked to the one before (created, amended)"


def test_edited_earlier_seal_is_invalid(repo):
    _sealed(repo)
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "x"))
    sidecar = rs.sidecar_path(repo / ".openshard")
    first, second = (json.loads(ln) for ln in sidecar.read_text().splitlines())
    statement = json.loads(base64.b64decode(first["payload"]))
    statement["predicate"]["sealed_at"] = "2020-01-01T00:00:00+00:00"
    first["payload"] = base64.b64encode(json.dumps(statement).encode()).decode()
    sidecar.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n")
    stored = load_history(_runs(repo), coerce=False)
    result = rv.verify_local(_runs(repo), stored[-1], len(stored) - 1)
    assert (result.verdict, result.reason) == ("invalid", "earlier_seal_bad_signature")


# ---------------------------------------------------------------------------
# Unknown keys, trust, rotation and revocation
# ---------------------------------------------------------------------------


def _bundle_from_other_install(repo: Path, tmp_path: Path, monkeypatch, *flags: str) -> tuple[Path, Path]:
    """Seal and bundle a Receipt under another OPENSHARD_HOME; return (bundle, that home)."""
    mine = Path(rk.keys_dir()).parent
    other = tmp_path / "other-home"
    monkeypatch.setenv("OPENSHARD_HOME", str(other))
    _sealed(repo)
    bundle = tmp_path / "bundle.json"
    assert _invoke("verify", "--signature", "--bundle", str(bundle), *flags).exit_code == 0
    monkeypatch.setenv("OPENSHARD_HOME", str(mine))
    return bundle, other


def test_bundle_from_another_install_is_signed_unknown_key(repo, tmp_path, monkeypatch, offline):
    bundle, _other = _bundle_from_other_install(repo, tmp_path, monkeypatch)
    result = _invoke("verify", "--signature", "--file", str(bundle))
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert rows["Signature"].startswith("signed, key not trusted here — sealed by ed25519:")
    assert "key carried in the bundle, not trusted here" in rows["Signature"]
    assert "openshard keys trust FILE" in rows["Means"]
    assert _invoke("verify", "--signature", "--file", str(bundle), "--strict").exit_code == 2


def test_trusting_the_exported_key_makes_it_verified(repo, tmp_path, monkeypatch, offline):
    bundle, other = _bundle_from_other_install(repo, tmp_path, monkeypatch)
    monkeypatch.setenv("OPENSHARD_HOME", str(other))
    exported = tmp_path / "their.pub"
    assert _invoke("keys", "export", "--output", str(exported)).exit_code == 0
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "third-home"))
    assert _invoke("keys", "trust", str(exported)).exit_code == 0
    result = _invoke("verify", "--signature", "--file", str(bundle))
    assert result.exit_code == 0
    assert _rows(result.output)["Signature"].startswith("verified — sealed by ed25519:")
    assert "(pinned key)" in _rows(result.output)["Signature"]


def test_a_bundle_cannot_borrow_a_trusted_keyid(repo, tmp_path, monkeypatch):
    bundle, _other = _bundle_from_other_install(repo, tmp_path, monkeypatch)
    data = json.loads(bundle.read_text())
    mine = rk.load_or_create_key()  # trusted here
    for envelope in data["seals"]:
        envelope["signatures"][0]["keyid"] = mine.keyid
    data["public_keys"][0]["keyid"] = mine.keyid  # does not match its bytes: ignored
    result = rv.verify_bundle(data)
    assert (result.verdict, result.reason) == ("invalid", "bad_signature")


def test_seal_by_a_key_not_available_is_not_checked(repo, tmp_path, monkeypatch):
    bundle, _other = _bundle_from_other_install(repo, tmp_path, monkeypatch)
    data = json.loads(bundle.read_text())
    data["public_keys"] = []
    result = rv.verify_bundle(data)
    assert (result.verdict, result.reason, result.signature_checked) == (
        "signed_unknown_key", "public_key_not_available", False)
    assert rv.status_label(result).endswith("not checked (public key not available here)")
    path = tmp_path / "stripped.json"
    path.write_text(json.dumps(data))
    rows = _rows(_invoke("verify", "--signature", "--file", str(path)).output)
    assert rows["Signature"].startswith("signed, not checked — the seal names ed25519:")
    assert "Claimed" not in rows and rows["Means"] == "nothing about this seal could be checked"


def test_rotation_keeps_old_seals_verifying(repo):
    _sealed(repo)
    old = rk.load_or_create_key().keyid
    result = _invoke("keys", "rotate")
    assert result.exit_code == 0 and f"Retired {old}" in result.output
    assert rk.load_or_create_key().keyid != old
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "after rotation"))
    body = json.loads(_invoke("verify", "--signature", "--json").output)
    assert body["verdict"] == "verified"
    assert [s["key_source"] for s in body["signature"]["chain"]] == ["this_install_retired", "this_install"]


def test_revocation(repo):
    _sealed(repo)
    old = rk.load_or_create_key().keyid
    assert "rotate" in _invoke("keys", "revoke", old).output  # the current key is refused
    _invoke("keys", "rotate")
    assert _invoke("keys", "revoke", old).exit_code == 0
    stored = load_history(_runs(repo), coerce=False)
    result = rv.verify_local(_runs(repo), stored[-1], 0)
    assert result.verdict == "verified" and result.revoked_at, "sealed before the revocation"
    later = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).isoformat()
    revoked = (rk.keys_dir() / rk.REVOCATIONS_FILENAME)
    revoked.write_text(json.dumps({old: later}))
    result = rv.verify_local(_runs(repo), stored[-1], 0)
    assert (result.verdict, result.reason) == ("invalid", "key_revoked")


def test_trust_rejects_a_file_that_is_not_a_public_key(tmp_path):
    bad = tmp_path / "bad.pub"
    bad.write_text(json.dumps({"algorithm": "ed25519", "public_key": "AAAA"}))
    result = _invoke("keys", "trust", str(bad))
    assert result.exit_code == 1 and result.output.startswith("Not trusted:")


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


def test_bundle_round_trip_offline_excludes_the_record(repo, tmp_path, offline):
    entry = _sealed(repo)
    path = tmp_path / "b.json"
    result = _invoke("verify", "--signature", "--bundle", str(path))
    assert result.exit_code == 0
    assert "the full record is not included (add --include-record)" in result.output
    assert "which includes the task text, file names, repository and branch" in result.output
    data = json.loads(path.read_text())
    assert "record" not in data, "the full record is left out by default"
    assert data["projection"]["receipt_id"] == entry["receipt_id"]
    assert data["mediaType"] == rv.BUNDLE_MEDIA_TYPE and data["receipt_id"] == entry["receipt_id"]
    rows = _rows(_invoke("verify", "--signature", "--file", str(path)).output)
    assert rows["Receipt"] == f"{entry['receipt_id']} (bundle b.json)"
    assert rows["Signature"].startswith("verified")
    assert rows["Record"] == "full record not provided; record digest not checked"
    assert rows["Projection"] == "matches the sealed copy (projection digest checked)"
    assert rows["Means"].startswith("the receipt view can't be changed afterwards")


def test_bundle_with_record_checks_the_record_digest(repo, tmp_path, offline):
    _sealed(repo)
    path = tmp_path / "b.json"
    assert _invoke("verify", "--signature", "--bundle", str(path), "--include-record").exit_code == 0
    data = json.loads(path.read_text())
    assert data["record"]["task"] == "fix the parser in secret-project"
    assert _rows(_invoke("verify", "--signature", "--file", str(path)).output)["Record"] == (
        "matches the sealed content (record digest checked)")
    data["record"]["task"] = "edited"
    data["record"]["content_hash"] = compute_shard_hash({k: v for k, v in data["record"].items()
                                                         if k != "content_hash"})
    path.write_text(json.dumps(data))
    result = _invoke("verify", "--signature", "--file", str(path))
    assert result.exit_code == 1 and "record changed after it was sealed" in result.output


def test_edited_bundle_projection_or_receipt_id_is_invalid(repo, tmp_path):
    _sealed(repo)
    path = tmp_path / "b.json"
    _invoke("verify", "--signature", "--bundle", str(path))
    data = json.loads(path.read_text())
    data["projection"]["status"] = "verified-by-me"
    assert rv.verify_bundle(data).reason == "projection_changed"
    data = json.loads(path.read_text())
    data["receipt_id"] = new_receipt_id()
    assert rv.verify_bundle(data).verdict == "invalid"


def test_unreadable_bundle_is_an_error_not_a_verdict(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"hello": 1}))
    result = _invoke("verify", "--signature", "--file", str(path), "--json")
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["status"] == "error" and "not an OpenShard receipt bundle" in body["message"]


def test_no_bundle_for_an_unsigned_receipt(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    _sealed(repo)
    result = _invoke("verify", "--signature", "--bundle", str(tmp_path / "b.json"))
    assert result.exit_code == 1 and "has no seal" in result.output
    assert not (tmp_path / "b.json").exists()


# ---------------------------------------------------------------------------
# JSON shapes and the rest of the CLI
# ---------------------------------------------------------------------------


def test_verify_signature_json_shape(repo):
    entry = _sealed(repo)
    body = json.loads(_invoke("verify", "--signature", "--json").output)
    assert (body["command"], body["status"], body["verdict"], body["exit_code"]) == ("verify", "ok", "verified", 0)
    assert body["receipt"] == entry["receipt_id"] and body["signature_check"] is True
    sig = body["signature"]
    assert sig["verdict_rules"] == "1" and sig["source"] == "local" and sig["signature_checked"] is True
    assert (sig["record_check"], sig["projection_check"]) == ("matches", "matches")
    assert sig["bound_by_signature"] == ["record digest", "projection digest", "receipt id", "shard id",
                                         "record field commitments"]
    assert sig["seal_version"] == "2" and sig["sync_sealed"] is False
    assert "sealed_at (local clock)" in sig["claimed_not_proven"]
    assert sig["not_proven"] == ["agent_claims", "verification_outcome", "capture_completeness"]
    assert sig["keyid"].startswith("ed25519:") and len(sig["chain"]) == 1


def test_last_and_history_json_carry_signature_status(repo, monkeypatch):
    signed = _sealed(repo)
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    unsigned = _record(shard_id="shard_0002")
    append_jsonl(_runs(repo), unsigned)
    last = json.loads(_invoke("last", "--json").output)
    assert last["signature_status"] == {
        "verdict": "not_signed", "reason": "no_seal", "label": "Checksum only (not signed)", "keyid": None,
        "signature_checked": False, "verdict_rules": "1",
    }
    rows = {r["receipt_id"]: r["signature_status"] for r in json.loads(_invoke("history", "--json").output)["shards"]}
    assert rows[signed["receipt_id"]]["verdict"] == "verified"
    assert rows[signed["receipt_id"]]["label"].startswith("Signed · key ")
    assert rows[unsigned["receipt_id"]]["verdict"] == "not_signed"


def test_last_row_reads_invalid_for_a_broken_chain(repo):
    _sealed(repo)
    amend_latest_record(_runs(repo), "note", lambda r: r.__setitem__("note", "one"))
    sidecar = rs.sidecar_path(repo / ".openshard")
    sidecar.write_text(sidecar.read_text().splitlines()[1] + "\n")  # drop the first seal
    assert any(ln.split()[:2] == ["Signature", "Invalid"] and "seal chain broken" in ln
               for ln in _invoke("last").output.splitlines())


@pytest.mark.parametrize("args, message", [
    (["--file", "x.json"], "need --signature"),
    (["--signature", "--ci"], "cannot be combined with --ci"),
    (["--signature", "--dry-run"], "cannot be combined with --dry-run"),
    (["--signature", "--file", "x.json", "--receipt", "r"], "cannot be combined with --receipt"),
    (["--signature", "--include-record"], "--include-record needs --bundle"),
])
def test_usage_errors(args, message):
    result = _invoke("verify", *args)
    assert result.exit_code == 2 and message in result.output


def test_plain_verify_still_reruns_checks(repo):
    _sealed(repo)
    result = _invoke("verify", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["status"] == "dry_run" and "signature" not in body


def test_keys_show_and_export_print_only_public_material(repo):
    _sealed(repo)
    key = rk.load_or_create_key()
    outputs = [_invoke("keys", "show").output, _invoke("keys", "show", "--json").output,
               _invoke("keys", "export").output]
    assert f"{key.keyid} (this install)" in outputs[0]
    assert json.loads(outputs[1])["key"]["fingerprint"] == key.fingerprint8
    assert rk.parse_public_key(json.loads(outputs[2])) == (key.keyid, key.public_raw)
    for secret in _private_material():
        assert all(secret not in out for out in outputs)


def test_no_private_material_in_any_output_or_bundle(repo, tmp_path):
    _sealed(repo)
    path = tmp_path / "b.json"
    outputs = [
        _invoke("verify", "--signature").output,
        _invoke("verify", "--signature", "--json").output,
        _invoke("verify", "--signature", "--bundle", str(path), "--include-record").output,
        _invoke("last", "--json").output,
        _invoke("history", "--json").output,
        path.read_text(),
    ]
    for secret in _private_material():
        assert all(secret not in out for out in outputs), secret


def test_keys_show_without_a_key_creates_none(tmp_path):
    result = _invoke("keys", "show")
    assert result.output.startswith("No signing key yet")
    assert not (rk.keys_dir() / rk.PRIVATE_KEY_FILENAME).exists()


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _health(**overrides) -> dict:
    return {"enabled": True, "crypto_available": True, "key_present": True, "keyid": "ed25519:" + "ab" * 32,
            "problem": None, "owner_only": True, "platform": "posix", "home_failures_recorded": False,
            **overrides}


def test_doctor_warns_when_the_windows_acl_is_not_confirmed():
    rows = doctor_checks(_health(platform="windows", owner_only=False))
    assert ("Key owner-only", False,
            "the owner-only ACL could not be confirmed (icacls); other accounts on this machine "
            "may be able to read the key") in rows


def test_doctor_rows():
    assert [row[:2] for row in doctor_checks(_health())] == [
        ("Signing on", True), ("Signing key abababab", True), ("Key owner-only", True)]
    assert doctor_checks(_health(key_present=False))[-1] == ("Signing key: none yet (created at the first seal)",
                                                             True, "")
    assert doctor_checks(_health(problem="key_corrupt"))[-1][1] is False
    assert doctor_checks(_health(enabled=False))[0][1] is False
    assert doctor_checks(_health(home_failures_recorded=True))[-1][0] == "Signing failures"


def test_key_health_reads_the_windows_acl_outcome(monkeypatch):
    rk.load_or_create_key()
    monkeypatch.setattr(rk, "_is_windows", lambda: True)
    monkeypatch.setattr(rk, "_owner_only", lambda _path: False)
    health = rk.key_health()
    assert (health["platform"], health["owner_only"], health["problem"]) == ("windows", False, None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_key_health_reports_a_loose_mode():
    rk.load_or_create_key()
    path = rk.keys_dir() / rk.PRIVATE_KEY_FILENAME
    os.chmod(path, 0o644)
    assert rk.key_health()["owner_only"] is False


def test_doctor_cli_shows_the_signing_block(repo):
    _sealed(repo)
    text = _invoke("doctor").output
    assert "Receipt signing" in text and "✓ Signing on" in text
    assert "A coding agent running as your OS user can read the key" in text
    state = json.loads(_invoke("doctor", "--json").output)["receipt_signing"]
    assert state["enabled"] is True and state["keyid"] == rk.load_or_create_key().keyid
    for secret in _private_material():
        assert secret not in text


def test_hashless_record_without_receipt_id_reads_not_signed(repo):
    entry = {"timestamp": "2025-01-01T00:00:00Z", "task": "old run", "workflow": "import"}
    _runs(repo).write_text(json.dumps(entry) + "\n")
    result = rv.verify_local(_runs(repo), entry, 0)
    assert (result.verdict, result.reason) == ("not_signed", "content_hash_missing")
    assert rv.status_label(result) == "Not signed"
    assert _rows(_invoke("verify", "--signature").output)["Signature"] == "Not signed — not sealed (content_hash_missing)"
