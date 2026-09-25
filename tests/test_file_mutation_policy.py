from __future__ import annotations

import json

import pytest

from openshard.history.sandbox_apply_receipts import (
    SandboxApplyReceipt,
    _dict_to_receipt,
    _receipt_to_dict,
)
from openshard.native.sandbox_apply import apply_sandbox_changes
from openshard.policy.file_mutation import FileMutationGate, evaluate_file_write


@pytest.mark.parametrize("rel,expected", [
    ("src/app.py", "allow"),
    ("README.md", "allow"),
    (".env", "deny"),
    (".ENV", "deny"),
    (".env ", "deny"),
    ("id_rsa.", "deny"),
    (".env:stream", "deny"),
    ("sub /pyproject.toml.", "ask"),
    ("Pyproject.TOML", "ask"),
    ("sub\\.env", "deny"),
    ("config/.env.local", "deny"),
    ("certs/server.pem", "deny"),
    (".openshard/runs.jsonl", "deny"),
    (".git/config", "deny"),
    (".github/workflows/ci.yml", "ask"),
    ("pyproject.toml", "ask"),
    ("Dockerfile", "ask"),
])
def test_evaluate_file_write(rel, expected):
    assert evaluate_file_write(rel).decision == expected


def test_gate_fails_closed_without_approver():
    gate = FileMutationGate()
    assert gate.authorize("src/a.py") is True
    assert gate.authorize("pyproject.toml") is False
    assert gate.authorize(".env") is False
    s = gate.summary()
    assert s["denied"] == [".env"]
    assert s["approval_required"] == ["pyproject.toml"]
    assert s["approval_granted"] == []
    assert s["approval_denied"] == []  # no approver consulted -> no observed denial
    assert s["approval_unavailable"] == ["pyproject.toml"]
    assert s["approval_sources"] == []


def test_gate_approver_error_is_denial():
    def boom(rel, decision):
        raise RuntimeError("x")

    gate = FileMutationGate(approver=boom)
    assert gate.authorize("pyproject.toml") is False
    assert gate.summary()["approval_denied"] == ["pyproject.toml"]


def test_deny_is_never_overridden_by_approver():
    gate = FileMutationGate(approver=lambda r, d: (True, "flag_yes"))
    assert gate.authorize(".env") is False
    assert gate.summary()["approval_granted"] == []


def _sandbox(tmp_path, files):
    sb = tmp_path / "sb"
    for rel, text in files.items():
        p = sb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return sb


def test_apply_blocks_denied_and_ask_without_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sb = _sandbox(tmp_path, {"a.py": "1", ".env": "SECRET=1", "pyproject.toml": "x"})
    res = apply_sandbox_changes(repo, sb, include=None)
    assert res.files_applied == ["a.py"]
    assert not (repo / ".env").exists()
    assert not (repo / "pyproject.toml").exists()
    assert set(res.files_denied) == {".env", "pyproject.toml"}
    assert res.policy_summary["executed"] == ["a.py"]
    assert res.policy_summary["verification"] == "not_run"


def test_apply_ask_granted_writes_and_records_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sb = _sandbox(tmp_path, {"pyproject.toml": "x"})
    res = apply_sandbox_changes(repo, sb, approver=lambda r, d: (True, "interactive_prompt"))
    assert res.files_applied == ["pyproject.toml"]
    assert res.policy_summary["approval_granted"] == ["pyproject.toml"]
    assert res.policy_summary["approval_sources"] == ["interactive_prompt"]


def test_apply_ask_declined_does_not_write(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sb = _sandbox(tmp_path, {"pyproject.toml": "x"})
    res = apply_sandbox_changes(repo, sb, approver=lambda r, d: (False, "interactive_prompt"))
    assert res.files_applied == []
    assert not (repo / "pyproject.toml").exists()
    assert res.policy_summary["approval_denied"] == ["pyproject.toml"]
    assert res.applied is False


def test_receipt_policy_roundtrip_and_backcompat():
    r = SandboxApplyReceipt(policy={"denied": [".env"], "verification": "not_run"})
    d = _receipt_to_dict(r)
    assert json.loads(json.dumps(d))["policy"]["denied"] == [".env"]
    assert _dict_to_receipt(d).policy["verification"] == "not_run"
    old = {k: v for k, v in d.items() if k != "policy"}
    assert _dict_to_receipt(old).policy == {}
