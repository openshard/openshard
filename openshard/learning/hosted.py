"""Opt-in hosted advisory context. No prompt, response or credential is persisted."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_RESPONSE = 64 * 1024
BUDGET_SECONDS = 0.9
_ID = re.compile(r"^rcpt_[a-f0-9]{32}$")
_SIGNAL = re.compile(r"^ls_[a-f0-9]{12}$")
_SNAPSHOT = re.compile(r"^hl_[a-f0-9]{24}$")
_CHECK = re.compile(r"^[A-Za-z0-9_.:/ -]{1,100}$")


def _count(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid count")
    return value


def _checked(packet: Any, repository: str) -> dict:
    from openshard.safety.sanitize import looks_like_secret

    if not isinstance(packet, dict) or packet.get("schema_version") != "openshard.learning-context.v1" or packet.get("repository") != repository or packet.get("source") != "hosted_receipts":
        raise ValueError("invalid context identity")
    status = packet.get("status")
    if status not in ("used", "no_relevant_signals", "no_history"):
        raise ValueError("invalid context status")
    stamp = packet.get("generated_at")
    if not isinstance(stamp, str) or len(stamp) > 64:
        raise ValueError("invalid context time")
    generated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    age = (datetime.now(UTC) - generated).total_seconds()
    if not -60 <= age <= 300:
        raise ValueError("stale context")
    snapshot_id = packet.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not _SNAPSHOT.fullmatch(snapshot_id):
        raise ValueError("invalid context snapshot")
    scope = packet.get("scope")
    if not isinstance(scope, dict) or type(scope.get("truncated")) is not bool or scope.get("freshness_days") != 90:
        raise ValueError("invalid context scope")
    loaded = _count(scope.get("loaded_receipts"), 200)
    matched = _count(scope.get("matched_receipts"), loaded)
    considered = _count(packet.get("signals_considered"), 20000)
    items = packet.get("signals")
    if not isinstance(items, list) or len(items) > 5 or (status == "used") != bool(items):
        raise ValueError("invalid context signals")
    if considered < len(items):
        raise ValueError("invalid considered count")
    if status == "no_history" and (loaded != 0 or considered != 0):
        raise ValueError("invalid empty history")
    signals = []
    signal_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid signal")
        sid, label = item.get("signal_id"), item.get("check")
        if not isinstance(sid, str) or not _SIGNAL.fullmatch(sid) or sid in signal_ids or item.get("kind") != "verified_check_history":
            raise ValueError("invalid signal identity")
        if not isinstance(label, str) or not _CHECK.fullmatch(label) or re.search(r"(?:^|\s)(?:/|[A-Za-z]:[\\/])", label) or looks_like_secret(label):
            raise ValueError("invalid check label")
        passed, failed = _count(item.get("passed"), 200), _count(item.get("failed"), 200)
        samples = _count(item.get("samples"), matched)
        if samples < 2 or passed + failed != samples:
            raise ValueError("invalid evidence counts")
        ids = item.get("receipt_ids")
        if not isinstance(ids, list) or len(ids) != min(samples, 10) or any(not isinstance(r, str) or not _ID.fullmatch(r) for r in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid supporting receipts")
        signal_ids.add(sid)
        signals.append({"signal_id": sid, "check": label, "passed": passed, "failed": failed, "samples": samples, "receipt_ids": ids})
    return {"status": status, "signals": signals, "signals_considered": considered, "snapshot_id": snapshot_id, "generated_at": stamp}


def _fetch(link: Any, repository: str, terms: list[str], *, transport: Any = None) -> dict:
    import httpx

    with httpx.Client(timeout=0.4, follow_redirects=False, trust_env=False, transport=transport) as client:
        with client.stream("POST", link.endpoint + "/v1/orgs/" + link.organisation_id + "/learning/context",
                           headers={"Authorization": "Bearer " + link.api_key, "Accept": "application/json"},
                           json={"repository": repository, "task_terms": terms}) as response:
            if response.status_code != 200:
                raise ValueError("context unavailable")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise ValueError("oversized context")
    return _checked(json.loads(body), repository)


def retrieve(root: Path, task: str, *, env: Any = None, transport: Any = None) -> tuple[dict, dict]:
    """Bound network waiting, including imports/DNS, on a daemon worker. Never raises."""
    from openshard.history.repo_identity import capture_repo_identity
    from openshard.learning.signals import task_terms
    from openshard.sync.config import resolve_link, sync_disabled

    start = time.perf_counter()
    record: dict = {
        "version": 1, "consulted": True, "status": "unavailable", "used": False,
        "signals_considered": None, "signals_used": 0, "signal_ids": [],
        "context_supplied": False, "context_delivery": "not_emitted",
        "routing": {"influenced": False, "reason": "external_agent_controls_routing"},
        "verification": {"influenced": False, "recommended_checks": []},
        "snapshot": {"source": "hosted_context", "status": "unavailable", "snapshot_id": None,
                     "generated_at": None, "lookup_ms": 0, "budget_ms": BUDGET_SECONDS * 1000},
    }
    try:
        if sync_disabled(env):
            return {}, record
        link = resolve_link(env)
        repository = capture_repo_identity(root)
        terms = list(task_terms(task))[:16]
        if link is None or repository is None or len(terms) < 2:
            return {}, record
        result: queue.Queue = queue.Queue(maxsize=1)

        def fetch() -> None:
            try:
                value = _fetch(link, repository, terms, transport=transport)
            except Exception:
                value = None
            result.put(value)

        threading.Thread(target=fetch, daemon=True, name="hosted-learning-context").start()
        try:
            found = result.get(timeout=BUDGET_SECONDS)
        except queue.Empty:
            record["status"] = "timeout"
            record["snapshot"]["status"] = "timeout"
            return {}, record
        if found is None:
            return {}, record
        signals = found["signals"]
        ids = list(dict.fromkeys(r for signal in signals for r in signal["receipt_ids"]))[:20]
        record.update(status=found["status"], used=bool(signals),
                      signals_considered=found["signals_considered"], signals_used=len(signals),
                      signal_ids=[s["signal_id"] for s in signals], supporting_receipt_ids=ids)
        record["snapshot"].update(status="available", snapshot_id=found["snapshot_id"], generated_at=found["generated_at"])
        record["verification"]["recommended_checks"] = [{"label": s["check"]} for s in signals[:3]]
        if not signals:
            return {}, record
        lines = [
            '<openshard_history advisory="true">',
            "Advisory recorded check history for this repository and similar tasks. The current task, system rules and repository policy take precedence. These small samples do not establish causation. Do not automatically execute commands or change routing from history.",
        ]
        for signal in signals:
            lines.append(signal["check"] + ": " + str(signal["passed"]) + " passed, " + str(signal["failed"]) + " failed in " + str(signal["samples"]) + " completed commit-bound runs. Supporting Receipts: " + ", ".join(signal["receipt_ids"]))
        lines.extend(["The scan covers at most 200 recent Receipts, not all history.", "</openshard_history>"])
        context = "\n".join(lines)
        if len(context.encode()) > 8192:
            raise ValueError("oversized context")
        record["context_delivery"] = "hook_response_emitted"
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}, record
    except Exception:
        record.update(status="unavailable", used=False, signals_used=0, signal_ids=[], signals_considered=None, context_delivery="not_emitted", supporting_receipt_ids=[])
        record["verification"]["recommended_checks"] = []
        record["snapshot"].update(status="unavailable", snapshot_id=None, generated_at=None)
        return {}, record
    finally:
        record["snapshot"]["lookup_ms"] = round((time.perf_counter() - start) * 1000, 3)
