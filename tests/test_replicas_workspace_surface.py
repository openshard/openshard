"""A Replicas workspace (Claude Code started in ~/workspaces, above its checkouts) is routed like a hosted workspace."""

from __future__ import annotations

import os

from openshard.adapters import claude_turn_receipts as turns
from openshard.adapters import claude_workspace as ws
from openshard.connected import config as cconfig
from tests.capture_fixtures import _Service, _wait_for
from tests.test_claude_workspace_capture import _Claude, _edit, _receipts, _sealed, workspace  # noqa: F401


def test_replicas_is_a_workspace_surface_that_seals_turns_at_stop():
    assert ws.workspace_enabled({cconfig.SURFACE_ENV: "replicas"})
    assert ("claude_code", "replicas") in turns._CLOUD_TURN_SURFACES
    assert not ws.workspace_enabled({})


def test_a_replicas_session_records_into_the_checkout_it_touched(workspace, monkeypatch):  # noqa: F811
    monkeypatch.setenv(cconfig.SURFACE_ENV, "replicas")
    for name in (cconfig.ENDPOINT_ENV, cconfig.ORG_ENV, cconfig.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)  # no connected-capture credential: sync carries Receipts
    service = _Service({**os.environ})
    try:
        ws.install_workspace(workspace, port=service.port)
        claude = _Claude(workspace)
        core = (workspace / "core").resolve()
        claude.send("SessionStart", source="startup")
        claude.send("UserPromptSubmit", prompt="Fix the discount")
        claude.reply(input_tokens=120, output_tokens=12)
        _edit(claude, core / "a.py", "A = 2\n")
        claude.send("Stop")  # a Replicas VM may sleep without SessionEnd: the turn is sealed here
        assert _wait_for(lambda: _sealed(core, 1))
    finally:
        service.stop()

    assert not (workspace / ".openshard" / "runs.jsonl").exists()  # never the parent directory
    (receipt,) = _receipts(core)
    assert receipt["task"] == "Fix the discount"
    assert receipt["executor"] == "claude_code_hooks"
    assert receipt["prompt_tokens"] == 120 and receipt["completion_tokens"] == 12
    assert receipt["capture"]["session_end_observed"] is False
    assert {f["path"] for f in receipt["files_detail"] if f.get("attribution") != "pre_existing"} == {"a.py"}
