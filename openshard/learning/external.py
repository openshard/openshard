"""Opt-in, bounded advisory context for native Claude and Codex prompt hooks.

The response is a handoff through the documented hook protocol, never proof
the model consumed, followed or benefited from it. Capture hooks stay silent.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_INPUT = 256 * 1024
MAX_RECORD = 16 * 1024
MAX_CONTEXT = 8 * 1024
AGENTS = {"claude": "claude_code", "codex": "codex"}


def delivery_path(root: Path, agent: str, session: str) -> Path:
    key = hashlib.sha256(f"{agent}\0{session}".encode()).hexdigest()
    return root / ".openshard" / "learning-deliveries" / f"{key}.json"


def prepare_hook(data: Any, agent: str, *, env: Any = None, budget_ms: float = 25, hosted: bool = False) -> tuple[dict, Path | None, dict | None]:
    """Read one frozen snapshot, never live history or a transcript."""
    from openshard.adapters.claude_hooks import (
        extract_hook_payload,
        resolve_repo_root,
        sanitize_task_excerpt,
    )
    from openshard.learning.record import build_learning_record
    from openshard.learning.snapshot import lookup_snapshot
    from openshard.learning.worker import nudge

    if agent not in AGENTS or not isinstance(data, dict) or data.get("hook_event_name") != "UserPromptSubmit":
        return {}, None, None
    payload = extract_hook_payload(data)
    if payload is None or not payload.session_id or not payload.prompt:
        return {}, None, None
    payload.agent = AGENTS[agent]
    hook_env = dict(os.environ if env is None else env)
    if agent == "codex":
        hook_env.pop("CLAUDE_PROJECT_DIR", None)
    root = resolve_repo_root(payload, hook_env)
    if root is None:
        return {}, None, None
    task = sanitize_task_excerpt(payload.prompt)
    if not task:
        return {}, None, None
    if hosted:
        from openshard.learning.hosted import retrieve

        response, record = retrieve(root, task, env=hook_env)
        stored = {"version": 1, "emitted_at": datetime.now(UTC).isoformat(), "learning": record}
        return response, delivery_path(root, payload.agent, payload.session_id), stored
    snapshot = lookup_snapshot(root / ".openshard", budget_ms=budget_ms)
    ctx = snapshot.consult(task)
    nudge(root / ".openshard" / "runs.jsonl", snapshot)
    if ctx.prompt_text and len(ctx.prompt_text.encode()) > MAX_CONTEXT:
        from openshard.learning.retrieval import LearningContext

        ctx = LearningContext("unavailable", ctx.shape, signals_considered=None, error="context_oversized")
    record = build_learning_record(ctx, check=None, snapshot=snapshot.record())
    # Keep the original model-call meaning of context_supplied. Hook output
    # is a distinct, observable handoff, with no model-consumption claim.
    record["context_delivery"] = "hook_response_emitted" if ctx.used else "not_emitted"
    response = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx.prompt_text}} if ctx.used else {}
    stored = {
        "version": 1, "emitted_at": datetime.now(UTC).isoformat(),
        "learning": record,
    }
    return response, delivery_path(root, payload.agent, payload.session_id), stored


def emit_hook(stream: Any, output: Any, agent: str, *, env: Any = None, hosted: bool = False) -> None:
    """Fail open; write provenance only after a successful stdout handoff."""
    try:
        source = getattr(stream, "buffer", None) or stream
        raw = source.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("oversized")
        response, path, record = prepare_hook(json.loads(raw), agent, env=env, hosted=hosted)
    except Exception:
        response, path, record = {}, None, None
    try:
        output.write(json.dumps(response) + "\n")
        output.flush()
    except Exception:
        return
    if path is None or record is None:
        return
    temporary: str | None = None
    try:
        blob = json.dumps(record)
        if len(blob.encode()) > MAX_RECORD:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
            temporary = handle.name
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except Exception:
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def captured_learning(root: Path, agent: str, session: str, started_at: str) -> dict | None:
    """Last handoff in this capture segment, bounded and privacy-projected.

    An earlier resumed segment's output cannot attach to a new Receipt.
    This function is called only while folding live hook evidence, never to
    mutate an ended Receipt.
    """
    try:
        with delivery_path(root, agent, session).open("rb") as handle:
            raw = handle.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            return None
        stored = json.loads(raw)
        if stored.get("version") != 1:
            return None
        emitted = datetime.fromisoformat(stored["emitted_at"])
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if emitted < start or emitted > datetime.now(UTC):
            return None
        record = stored["learning"]
        from openshard.history.receipt_evidence import learning_block

        projected = learning_block({"learning": record})
        if projected is None:
            return None
        # Preserve the canonical nested shape consumed by learning_block.
        result = {
            "version": 1, "status": projected["status"], "used": projected["used"],
            "signals_considered": projected["signals_considered"],
            "signals_used": projected["signals_used"], "signal_ids": projected["signal_ids"],
            "context_supplied": False,
            "context_delivery": "hook_response_emitted" if record.get("context_delivery") == "hook_response_emitted" else "not_emitted",
            "routing": {"influenced": False, "reason": "external_agent_controls_routing"},
            "verification": {"influenced": False, "recommended_checks": [{"label": label} for label in projected["recommended_checks"]]},
            "snapshot": projected.get("snapshot"),
        }
        if "supporting_receipt_ids" in projected:
            result["supporting_receipt_ids"] = projected["supporting_receipt_ids"]
        return result
    except Exception:
        return None
