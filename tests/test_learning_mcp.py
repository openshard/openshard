"""learning_signals over MCP: the same advisory, evidence-backed signals OSN gets, for any agent."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from openshard.mcp.server import build_server
from tests.learning_fixtures import MOBILE_CHECK, osn_entry

pytest.importorskip("mcp")

MOBILE = "tests/test_layout.py::test_mobile_viewport_rejects_non_positive_widths"


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args)).structured_content


def _repo(tmp_path, entries=()):
    r = tmp_path / "shop"
    r.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    (r / ".openshard").mkdir()
    (r / ".openshard" / "runs.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return r


def _history():
    out = []
    for t in ("Fix responsive dashboard layout", "Dashboard layout grid"):
        e = osn_entry(t, repo="shop", attempts=[("m/a", "failed"), ("m/b", "passed")], check=MOBILE_CHECK)
        e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [MOBILE]
        out.append(e)
    return out


def test_relevant_signals_with_reasons_and_advisory_text(tmp_path):
    server = build_server(repo_path=_repo(tmp_path, _history()))
    out = _call(server, "learning_signals", {"task": "Update the dashboard analytics layout"})
    assert out["status"] == "used" and 0 < len(out["signals"]) <= 5
    assert all(s["reasons"][0] == "same_repo" and s["summary"] for s in out["signals"])
    assert out["suggested_files"] == ["tests/test_layout.py"]
    assert out["context_text"].startswith('<openshard_history advisory="true">')
    assert "not an instruction" in out["context_text"]


def test_nothing_relevant_is_said_honestly(tmp_path):
    server = build_server(repo_path=_repo(tmp_path, _history()))
    out = _call(server, "learning_signals", {"task": "Rotate the ledger database credentials"})
    assert out["status"] == "no_relevant_signals" and out["signals"] == []
    assert out["context_text"] == "No evidence-backed learning signals are relevant to this task yet."
    empty = build_server(repo_path=_repo(tmp_path / "e"))
    assert _call(empty, "learning_signals", {"task": "anything"})["status"] == "no_history"


def test_limit_is_clamped_and_the_tool_is_read_only(tmp_path):
    repo = _repo(tmp_path, _history())
    before = (repo / ".openshard" / "runs.jsonl").read_bytes()
    server = build_server(repo_path=repo)
    assert len(_call(server, "learning_signals", {"task": "dashboard layout", "limit": 500})["signals"]) <= 5
    assert _call(server, "learning_signals", {"task": "dashboard layout", "limit": -3})["signals"] == []
    assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before
