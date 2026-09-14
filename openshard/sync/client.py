"""Receipt sync client: push local Shard receipts to an OpenShard Cloud endpoint.

Rules (mirroring the telemetry transport, the one existing egress path):

* Nothing is sent unless an endpoint is configured (``OPENSHARD_SYNC_ENDPOINT``
  or ``sync: {endpoint: ...}`` in ``.openshard/config.yml``).
* The bearer token comes from ``OPENSHARD_SYNC_TOKEN`` only. It is never read
  from, or written to, a config file, and never logged.
* HTTPS only, except plain HTTP to loopback for local development.
* Strict timeouts; one POST per receipt; failures are categorised, never
  reproduced verbatim.
* Idempotent: ``.openshard/sync_state.json`` remembers ``(run_id,
  content_hash, remote_id)`` so an unchanged receipt is not re-sent.
* Never on a hook path. Only ``openshard sync push`` (or an explicit caller)
  runs this.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from openshard.contracts.sync import SyncEnvelope, SyncResult, SyncTransport, build_sync_envelope
from openshard.history.jsonl_store import history_file_lock

ENDPOINT_ENV = "OPENSHARD_SYNC_ENDPOINT"
TOKEN_ENV = "OPENSHARD_SYNC_TOKEN"
STATE_FILENAME = "sync_state.json"
STATE_SCHEMA_VERSION = 1
CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
RECEIPTS_PATH = "/api/v1/sync/receipts"


def endpoint_allowed(endpoint: object) -> bool:
    """HTTPS anywhere, or plain HTTP to loopback only (local development)."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    try:
        parts = urlsplit(endpoint.strip())
    except ValueError:
        return False
    if parts.scheme == "https" and parts.netloc:
        return True
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class SyncConfig:
    endpoint: str | None
    token_present: bool
    source: str  # "env" | "config" | "none"
    reason: str

    @property
    def enabled(self) -> bool:
        return self.endpoint is not None and self.token_present


def resolve_sync_config(
    env: Mapping[str, str] | None = None, repo_config: Mapping[str, Any] | None = None,
) -> SyncConfig:
    """Where receipts would go, and whether they can. Never raises."""
    env = os.environ if env is None else env
    raw = env.get(ENDPOINT_ENV)
    source = "env"
    if not raw and isinstance(repo_config, Mapping):
        sync_cfg = repo_config.get("sync")
        if isinstance(sync_cfg, Mapping):
            raw = sync_cfg.get("endpoint")
            source = "config"
    token_present = bool(env.get(TOKEN_ENV))
    if not raw:
        return SyncConfig(None, token_present, "none", "no sync endpoint configured")
    if not endpoint_allowed(raw):
        return SyncConfig(None, token_present, source, "sync endpoint must be https:// (or http:// to loopback)")
    endpoint = str(raw).strip().rstrip("/")
    if not token_present:
        return SyncConfig(endpoint, False, source, f"{TOKEN_ENV} is not set")
    return SyncConfig(endpoint, True, source, "configured")


class HttpsSyncTransport:
    """One POST per envelope with a bearer token. stdlib only, lazy imports."""

    name = "https"

    def __init__(self, endpoint: str, token: str, *, timeout: float = TOTAL_TIMEOUT_SECONDS) -> None:
        if not endpoint_allowed(endpoint):
            raise ValueError("sync endpoint must be https:// (or http:// to loopback)")
        self._url = endpoint.rstrip("/") + RECEIPTS_PATH
        self._token = token
        self._timeout = timeout

    def push(self, envelope: SyncEnvelope) -> SyncResult:
        import urllib.error
        import urllib.request

        body = json.dumps(envelope.to_dict(), separators=(",", ":"), default=str).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": f"openshard-sync/{envelope.openshard_version}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - https enforced above
                status_code = resp.status
                raw = resp.read(65536)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return SyncResult(accepted=False, status="unauthorized", error_category="auth")
            if exc.code in (400, 409, 413, 422):
                return SyncResult(accepted=False, status="rejected", error_category=f"http_{exc.code}")
            return SyncResult(accepted=False, status="unreachable", error_category=f"http_{exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return SyncResult(accepted=False, status="unreachable", error_category="transport")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {}
        if status_code not in (200, 201):
            return SyncResult(accepted=False, status="rejected", error_category=f"http_{status_code}")
        remote_id = payload.get("id") if isinstance(payload, dict) else None
        server_status = payload.get("status") if isinstance(payload, dict) else None
        status = server_status if server_status in ("created", "updated", "unchanged") else (
            "created" if status_code == 201 else "updated"
        )
        return SyncResult(accepted=True, status=status, remote_id=str(remote_id) if remote_id else None)


# ---------------------------------------------------------------------------
# Local state
# ---------------------------------------------------------------------------


def state_path(repo_path: Path | None = None) -> Path:
    return (repo_path or Path.cwd()) / ".openshard" / STATE_FILENAME


def load_sync_state(repo_path: Path | None = None) -> dict:
    path = state_path(repo_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": STATE_SCHEMA_VERSION, "pushed": {}}
    if not isinstance(data, dict) or not isinstance(data.get("pushed"), dict):
        return {"schema_version": STATE_SCHEMA_VERSION, "pushed": {}}
    return data


def _save_sync_state(state: dict, repo_path: Path | None) -> None:
    path = state_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class SyncPushSummary:
    endpoint: str | None
    considered: int = 0
    sent: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    dry_run: bool = False
    results: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "considered": self.considered,
            "sent": self.sent,
            "skipped_unchanged": self.skipped_unchanged,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "results": self.results,
        }


def _run_key(entry: dict) -> str | None:
    rid = entry.get("run_id") or entry.get("timestamp")
    return rid if isinstance(rid, str) and rid else None


def push_entries(
    entries: list[dict],
    transport: SyncTransport | None,
    *,
    repo_path: Path | None = None,
    endpoint: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    outcomes: Mapping[str, dict] | None = None,
) -> SyncPushSummary:
    """Push each entry (newest last) as a sync envelope. Never raises.

    Entries already recorded in the sync state with the same ``content_hash``
    are skipped unless ``force``. ``dry_run`` builds envelopes and reports
    what would be sent without sending or recording anything.
    """
    summary = SyncPushSummary(endpoint=endpoint, dry_run=dry_run)
    state = load_sync_state(repo_path)
    pushed: dict = state.setdefault("pushed", {})
    by_shard: dict[str, list[dict]] = {}
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("shard_id"), str):
            by_shard.setdefault(e["shard_id"], []).append(e)

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        summary.considered += 1
        key = _run_key(entry)
        content_hash = entry.get("content_hash") if isinstance(entry.get("content_hash"), str) else None
        previous = pushed.get(key) if key else None
        if not force and previous and content_hash and previous.get("content_hash") == content_hash:
            summary.skipped_unchanged += 1
            continue
        sid = entry.get("shard_id")
        siblings = [s for s in by_shard.get(sid, []) if s is not entry] if isinstance(sid, str) else None
        try:
            from openshard.history.receipt_contract import build_receipt_contract
            outcome = (outcomes or {}).get(sid) if isinstance(sid, str) else None
            contract = build_receipt_contract(entry, index=index, siblings=siblings, outcome_record=outcome)
            envelope = build_sync_envelope(entry, siblings=siblings, index=index)
            envelope.receipt_contract = contract.to_dict()
        except Exception:
            summary.failed += 1
            summary.results.append({"run_id": key, "status": "envelope_failed"})
            continue
        if dry_run or transport is None:
            summary.results.append({"run_id": key, "shard_id": envelope.shard_id, "status": "would_send",
                                    "state": envelope.receipt_contract.get("state")})
            continue
        result = transport.push(envelope)
        summary.results.append({
            "run_id": key, "shard_id": envelope.shard_id, "status": result.status,
            "remote_id": result.remote_id, "error_category": result.error_category,
        })
        if result.accepted:
            summary.sent += 1
            if key:
                pushed[key] = {
                    "shard_id": envelope.shard_id, "content_hash": content_hash,
                    "remote_id": result.remote_id, "sent_at": envelope.sent_at, "endpoint": endpoint,
                }
        else:
            summary.failed += 1

    if not dry_run and transport is not None and summary.sent:
        try:
            with history_file_lock(state_path(repo_path)):
                # Merge with whatever another process wrote meanwhile.
                current = load_sync_state(repo_path)
                current.setdefault("pushed", {}).update(pushed)
                current["schema_version"] = STATE_SCHEMA_VERSION
                if endpoint:
                    current["endpoint"] = endpoint
                _save_sync_state(current, repo_path)
        except Exception:
            pass
    return summary
