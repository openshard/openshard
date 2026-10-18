"""Sync sends the install seal beside the receipt (seal v2, docs/receipt-seal-v2.md).

The seal must name the exact receipt that went on the wire, validate against
the vendored strict Platform schema, verify with the key it carries, and
never cost a Receipt its sync: an unsealed record goes unsigned, and a
Platform that refuses the seal still gets the receipt.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from openshard.history import receipt_keys as rk
from openshard.history import receipt_seal_v2 as v2
from openshard.history import receipt_signing as rs
from openshard.sync import client, config, envelope, outbox, transport
from openshard.sync.transport import SendResult
from openshard.util import jcs
from tests.capture_fixtures import _git, _make_repo
from tests.test_platform_sync import (
    ENDPOINT,
    KEY,
    ORG,
    RECEIPT_SCHEMA,
    _entries,
    _session,
    _sid,
    _valid,
)


@pytest.fixture
def env(tmp_path: Path) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "widget")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


@pytest.fixture
def link(env: dict) -> config.PlatformLink:
    return config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)


@pytest.fixture
def recording():
    rt = transport.RecordingPlatformTransport()
    client.configure(transport=rt, repo_config={})
    yield rt
    client.configure(transport=None, repo_config=None)


@pytest.fixture(autouse=True)
def _signing_on(monkeypatch):
    monkeypatch.delenv(rs.DISABLE_ENV, raising=False)


def _wire(doc: dict) -> dict:
    """What HttpsPlatformTransport puts on the wire, parsed back."""
    return json.loads(json.dumps(doc, separators=(",", ":"), ensure_ascii=True, default=str))


def test_sealed_receipt_goes_with_its_seal_and_key(repo, env, link, recording):
    _session(repo, _sid(1))
    report = client.flush(repo, env=env)
    assert (report.created, report.sealed, report.signing_key) == (1, 1, "registered")
    [sent] = recording.envelopes
    _valid(RECEIPT_SCHEMA, sent)
    seal = sent["seal"]
    assert set(seal) == {"envelope", "public_key"}
    keyid, raw = rk.parse_public_key(seal["public_key"]) or (None, b"")
    assert keyid == seal["envelope"]["signatures"][0]["keyid"] == rk.read_public_key()[0]  # type: ignore[index]
    assert recording.registered_keys == [seal["public_key"]]
    digest = jcs.sha256_hex(_wire(sent)["receipt"])
    check = rs.verify_envelope(seal["envelope"], public_keys={keyid: raw}, trusted_keyids={keyid},
                               receipt_id=sent["receipt"]["receipt_id"], sync_sha256=digest)
    assert check.verdict == rs.VERDICT_VERIFIED
    statement = json.loads(base64.b64decode(seal["envelope"]["payload"]))
    assert statement["predicate"]["seal_reason"] == v2.SEAL_SYNCED
    # Digests and ids only: none of the receipt's text travels in the seal.
    assert sent["receipt"]["task_short"] and sent["receipt"]["task_short"] not in json.dumps(statement)
    assert "salt" not in json.dumps(seal)


def test_receipt_is_identical_with_or_without_a_seal(repo, env, link, recording, monkeypatch):
    _session(repo, _sid(1))
    [entry] = _entries(repo)
    client.flush(repo, env=env)
    [sent] = recording.envelopes
    built = envelope.build_envelope(_entries_coerced(repo)[0], 0, core_version=sent["source"]["version"])
    assert sent["receipt"] == _wire(built)["receipt"] and set(sent) - set(built) == {"seal"}
    assert entry == _entries(repo)[0], "sealing for sync never touches runs.jsonl"


def _entries_coerced(repo: Path) -> list[dict]:
    from openshard.history.store import load_history

    return load_history(repo / ".openshard" / "runs.jsonl", coerce=True)


def test_signing_off_sends_the_receipt_unsigned_and_registers_nothing(repo, env, link, recording, monkeypatch):
    monkeypatch.setenv(rs.DISABLE_ENV, "off")
    _session(repo, _sid(1))
    report = client.flush(repo, env=env)
    assert (report.created, report.sealed, report.signing_key) == (1, 0, None)
    assert "seal" not in recording.envelopes[0] and recording.registered_keys == []


def test_a_platform_that_refuses_the_seal_still_gets_the_receipt(repo, env, link):
    old_platform = SendResult(transport.KIND_REJECTED, 400, "invalid_payload", [{"path": "seal", "message": "unknown key"}])
    rt = transport.RecordingPlatformTransport([old_platform, SendResult(transport.KIND_CREATED, 201)])
    _session(repo, _sid(1))
    report = client.flush(repo, env=env, transport=rt)
    assert (report.created, report.rejected, report.sealed) == (1, 0, 0)
    first, second = rt.envelopes
    assert "seal" in first and "seal" not in second and first["receipt"] == second["receipt"]
    assert {r["state"] for r in outbox.load_outbox(repo).values()} == {"synced"}


def test_a_rejection_of_the_receipt_itself_is_not_resent(repo, env, link):
    privacy = SendResult(transport.KIND_REJECTED, 422, "privacy_violation", {"violations": []})
    other = SendResult(transport.KIND_REJECTED, 400, "invalid_payload", [{"path": "receipt.files", "message": "x"}])
    for result in (privacy, other):
        rt = transport.RecordingPlatformTransport([result])
        _session(repo, _sid(len(_entries(repo)) + 1))
        report = client.flush(repo, env=env, transport=rt)
        assert report.rejected == 1 and len(rt.envelopes) == 1 and "seal" in rt.envelopes[0]


def test_key_registration_without_a_route_never_blocks_sync(repo, env, link):
    rt = transport.RecordingPlatformTransport()
    rt.key_result = SendResult(transport.KIND_UNSUPPORTED, 404)
    _session(repo, _sid(1))
    report = client.flush(repo, env=env, transport=rt)
    assert (report.created, report.sealed, report.signing_key) == (1, 1, "unsupported")


def test_https_transport_registers_the_key_beside_the_receipts_route(link, monkeypatch):
    sent: list[tuple[str, dict]] = []
    https = transport.HttpsPlatformTransport(link, user_agent="openshard/test")
    monkeypatch.setattr(https, "_post", lambda url, body, classify: sent.append((url, body)) or SendResult("created", 201))
    record = {"algorithm": "ed25519", "keyid": "ed25519:" + "a" * 64, "public_key": "A" * 43 + "="}
    https.register_signing_key(record)
    assert sent == [(f"{ENDPOINT}/v1/orgs/{ORG}/signing-keys", record)]
    assert KEY not in json.dumps(sent)
