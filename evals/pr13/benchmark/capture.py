"""Natural OpenShard burn-in: real Claude Code hooks feeding a private capture service.

The burn-in Claude session is captured exactly the way a user's session
is: ``install_claude_hooks`` (the production installer) writes the hook
configuration into the workspace's ``.claude/settings.local.json``, the
hooks POST to an OpenShard capture service, and the fold in
``adapters/claude_hooks.py`` writes the Shard to
``<workspace>/.openshard/runs.jsonl``. This module adds no capture logic
and writes no history of its own; it only

* points the session at a *benchmark-private* service (``OPENSHARD_HOME``
  and ``OPENSHARD_CAPTURE_PORT`` in the agent's environment) so the
  developer's own service and state file are never involved,
* waits for the session's Shard to land (the fold is asynchronous),
* reads the resulting history through OpenShard's public query API to
  report what evidence exists and whether the scenario's
  ``expected_evidence`` holds,
* and removes the hook configuration again afterwards.
"""

from __future__ import annotations

import json
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.config import ExpectedEvidence
from evals.pr13.benchmark.errors import BenchmarkError
from openshard.adapters.claude_capture_client import (
    ensure_service,
    health,
    request_shutdown,
    resolve_port,
)
from openshard.adapters.claude_hooks_install import install_claude_hooks, uninstall_claude_hooks
from openshard.history import query as history_query
from openshard.history.metrics import load_runs

HOOK_EXECUTOR = "claude_code_hooks"
_HISTORY_POLL_SECONDS = 0.25


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def capture_env_overrides(home: Path, port: int) -> dict[str, str]:
    """Environment that binds this session's hooks to a private capture service."""
    return {"OPENSHARD_HOME": str(home), "OPENSHARD_CAPTURE_PORT": str(port)}


def install_burn_in_capture(workspace: Path, port: int) -> dict[str, Any]:
    """Install OpenShard's Claude Code hooks in *workspace* via the production installer."""
    result = install_claude_hooks(repo_root=workspace, port=port)
    if result.status not in ("installed", "updated", "already_installed"):
        raise BenchmarkError(
            "hooks_install_failed", f"could not install Claude Code hooks: {result.message}",
            details={"status": result.status, "warnings": result.warnings},
        )
    return {
        "status": result.status, "settings_path": str(result.settings_path),
        "events": dict(result.events), "warnings": list(result.warnings), "port": port,
    }


def remove_burn_in_capture(workspace: Path) -> dict[str, Any]:
    result = uninstall_claude_hooks(repo_root=workspace)
    return {"status": result.status, "message": result.message}


def install_opencode_burn_in_capture(workspace: Path, port: int) -> dict[str, Any]:
    """Install OpenCode's production plugin capture in *workspace* (Cross-Agent Handoff scenario)."""
    from openshard.adapters.opencode_plugin_install import install_opencode_plugin

    result = install_opencode_plugin(repo_root=workspace, port=port)
    if result.status not in ("installed", "updated", "already_installed"):
        raise BenchmarkError(
            "hooks_install_failed", f"could not install the OpenCode plugin: {result.message}",
            details={"status": result.status, "warnings": result.warnings},
        )
    return {
        "status": result.status, "settings_path": str(result.settings_path),
        "events": dict(result.events), "warnings": list(result.warnings), "port": port,
    }


def remove_opencode_burn_in_capture(workspace: Path) -> dict[str, Any]:
    from openshard.adapters.opencode_plugin_install import uninstall_opencode_plugin

    result = uninstall_opencode_plugin(repo_root=workspace)
    return {"status": result.status, "message": result.message}


def start_capture_service(env: dict[str, str], *, wait_seconds: float = 8.0) -> dict[str, Any]:
    """Start (or find) the private capture service for *env*. Fails loudly when it cannot run."""
    port, state = ensure_service(env, wait_seconds=wait_seconds)
    if state == "disabled":
        raise BenchmarkError(
            "capture_disabled", "OPENSHARD_CAPTURE_DISABLE is set in the agent environment; HTTP hooks would record nothing",
        )
    if port is None or state == "unavailable":
        raise BenchmarkError(
            "capture_service_unavailable",
            "the OpenShard capture service could not be started for the burn-in "
            f"(see {env.get('OPENSHARD_HOME')}/claude-capture.log)",
        )
    doc = health(port) or {}
    return {"state": state, "port": port, "instance_id": doc.get("instance_id"), "pid": doc.get("pid")}


def stop_capture_service(env: dict[str, str]) -> bool:
    try:
        return request_shutdown(env)
    except Exception:
        return False


def service_running(env: dict[str, str]) -> bool:
    return health(resolve_port(env)) is not None


def _read_entries(workspace: Path) -> list[dict]:
    """Raw run entries, tolerating the brief Windows lock during the fold's atomic replace."""
    for _ in range(20):
        try:
            return load_runs(workspace)
        except PermissionError:
            time.sleep(0.05)
    return load_runs(workspace)


def find_session_entry(workspace: Path, session_id: str, *, executor: str = HOOK_EXECUTOR) -> dict | None:
    """Find the hook/plugin-captured entry for *session_id*.

    *executor* selects which agent's capture path to look for
    (``"claude_code_hooks"`` default, or ``"opencode_plugin"`` -- both
    write the same ``capture.session_id``/``capture.session_end_observed``
    shape; see ``openshard.adapters.capture_agents``).
    """
    for entry in _read_entries(workspace):
        capture_block = entry.get("capture")
        if not isinstance(capture_block, dict):
            continue
        if entry.get("executor") == executor and capture_block.get("session_id") == session_id:
            return entry
    return None


def wait_for_captured_session(
    workspace: Path, session_id: str, *, timeout_seconds: float, executor: str = HOOK_EXECUTOR,
) -> dict:
    """Block until the burn-in session's Shard is folded into runs.jsonl with its session end observed."""
    deadline = time.monotonic() + timeout_seconds
    last: dict | None = None
    while time.monotonic() < deadline:
        entry = find_session_entry(workspace, session_id, executor=executor)
        if entry is not None:
            last = entry
            capture = entry.get("capture") or {}
            if capture.get("session_end_observed") or capture.get("task_status") == "turn_completed":
                return entry
        time.sleep(_HISTORY_POLL_SECONDS)
    if last is not None:
        # The session was captured but its end never folded; that is still
        # real, natural history -- report it rather than pretend otherwise.
        return last
    raise BenchmarkError(
        "burn_in_not_captured",
        f"no {executor}-captured Shard for session {session_id} appeared in {workspace}/.openshard/runs.jsonl "
        f"within {timeout_seconds}s; the burn-in produced no OpenShard history",
    )


def entry_count(workspace: Path) -> int:
    """Number of run entries currently persisted for *workspace*. Never raises."""
    return len(_read_entries(workspace))


def count_shard_attempts(workspace: Path, shard_id: str | None) -> int:
    """Number of persisted run entries carrying exactly *shard_id*. Never raises."""
    if not shard_id:
        return 0
    return sum(1 for e in _read_entries(workspace) if e.get("shard_id") == shard_id)


def wait_for_new_entry(
    workspace: Path, baseline_count: int, *, timeout_seconds: float, executor: str | None = None,
) -> dict:
    """Block until a new run entry (beyond *baseline_count*) appears, optionally filtered by *executor*.

    Used for agents (OpenCode) whose own CLI does not expose a session id
    the benchmark can match against ahead of time: the caller records how
    many entries existed before launching the agent and this waits for the
    count to grow, then returns the newest matching entry. Never guesses
    which entry is "the" new one beyond "appeared after the known count".
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        entries = _read_entries(workspace)
        if len(entries) > baseline_count:
            new_entries = entries[baseline_count:]
            if executor is not None:
                new_entries = [e for e in new_entries if e.get("executor") == executor]
            if new_entries:
                return new_entries[-1]
        time.sleep(_HISTORY_POLL_SECONDS)
    raise BenchmarkError(
        "burn_in_not_captured",
        f"no new run entry appeared in {workspace}/.openshard/runs.jsonl within {timeout_seconds}s "
        f"(had {baseline_count} before launch" + (f", executor={executor}" if executor else "") + ")",
    )


def _entry_files(entry: dict) -> list[str]:
    files: list[str] = []
    for f in entry.get("files_detail") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            files.append(f["path"])
    return files


def history_summary(workspace: Path) -> dict[str, Any]:
    """What OpenShard's own public query layer says about *workspace*'s history."""
    entries = _read_entries(workspace)
    shards: list[dict[str, Any]] = []
    for shard in history_query.list_shards(limit=50, repo_path=workspace):
        try:
            receipt = history_query.get_receipt(shard.shard_id, repo_path=workspace)
        except ValueError:
            continue
        shards.append({
            "shard_id": shard.shard_id,
            "created_at": shard.created_at,
            "task_short": shard.task_short,
            "agent": shard.agent,
            "origin": shard.origin,
            "capture_depth": shard.capture_depth,
            "status": receipt.status,
            "verification_status": receipt.verification_status or None,
            "files": list(receipt.files_touched),
            "task_completion": receipt.task_completion,
        })
    return {
        "present": bool(entries),
        "entries": len(entries),
        "executors": sorted({str(e.get("executor")) for e in entries}),
        "shards": shards,
    }


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower()


def evaluate_expected_evidence(spec: ExpectedEvidence, workspace: Path, evaluation_prompt: str) -> dict[str, Any]:
    """Check the scenario's ``expected_evidence`` against the real, preserved history.

    Also reports what ``relevant_context(<evaluation prompt>)`` -- the exact
    MCP call a treatment agent is nudged to make -- returns for this
    history, so the reader knows whether the evidence was *retrievable*,
    not merely present.
    """
    entries = _read_entries(workspace)
    if spec.executor:
        entries = [e for e in entries if e.get("executor") == spec.executor]
    checks: list[dict[str, Any]] = []
    checks.append({"check": "min_shards", "expected": spec.min_shards, "actual": len(entries),
                   "ok": len(entries) >= spec.min_shards})
    matching: list[dict] = []
    for entry in entries:
        task = str(entry.get("task") or "").lower()
        files = [_norm(f) for f in _entry_files(entry)]
        ok_task = (not spec.task_contains_any) or any(t.lower() in task for t in spec.task_contains_any)
        ok_incl = (not spec.files_include_any) or any(_norm(p) in files for p in spec.files_include_any)
        ok_excl = all(_norm(p) not in files for p in spec.files_exclude_all)
        if ok_task and ok_incl and ok_excl:
            matching.append(entry)
    checks.append({
        "check": "shard_matching_task_and_files",
        "task_contains_any": list(spec.task_contains_any),
        "files_include_any": list(spec.files_include_any),
        "files_exclude_all": list(spec.files_exclude_all),
        "matching_shard_ids": [str(e.get("shard_id")) for e in matching],
        "ok": bool(matching) if (spec.task_contains_any or spec.files_include_any or spec.files_exclude_all) else True,
    })
    ctx = history_query.relevant_context(evaluation_prompt, repo_path=workspace)
    retrievable = [m.shard.shard_id for m in ctx.matches]
    matching_ids = {str(e.get("shard_id")) for e in matching}
    checks.append({
        "check": "retrievable_via_relevant_context",
        "matches": retrievable,
        "burn_in_shard_in_matches": bool(matching_ids & set(retrievable)),
        "ok": bool(matching_ids & set(retrievable)) if matching_ids else False,
    })
    return {
        "description": spec.description,
        "present": all(c["ok"] for c in checks),
        "checks": checks,
        "relevant_context_text": ctx.context_text,
    }


def snapshot_history(workspace: Path, dest: Path) -> dict[str, Any]:
    """Copy ``<workspace>/.openshard`` into the results directory for the record."""
    src = workspace / ".openshard"
    if not src.is_dir():
        raise BenchmarkError("history_missing", f"no .openshard/ under {workspace}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    runs = dest / "runs.jsonl"
    return {"path": str(dest), "runs_lines": len(runs.read_text(encoding="utf-8").splitlines()) if runs.exists() else 0}


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
