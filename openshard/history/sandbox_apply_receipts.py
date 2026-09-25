from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from openshard.history.jsonl_store import append_jsonl

_APPLY_RECEIPTS_PATH = Path(".openshard") / "sandbox_apply_receipts.jsonl"


@dataclass
class SandboxApplyReceipt:
    schema_version: int = 1
    receipt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.datetime.now(
            datetime.UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    source_run_id: str = ""
    sandbox_path: str = ""
    applied: bool = False
    files_applied: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    applied_count: int = 0
    skipped_count: int = 0
    dry_run: bool = False
    reason: str = ""
    raw_content_stored: bool = False
    policy: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.raw_content_stored = False
        self.applied_count = len(self.files_applied)
        self.skipped_count = len(self.files_skipped)


def _receipt_to_dict(receipt: SandboxApplyReceipt) -> dict:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "timestamp": receipt.timestamp,
        "source_run_id": receipt.source_run_id,
        "sandbox_path": receipt.sandbox_path,
        "applied": receipt.applied,
        "files_applied": list(receipt.files_applied),
        "files_skipped": list(receipt.files_skipped),
        "applied_count": receipt.applied_count,
        "skipped_count": receipt.skipped_count,
        "dry_run": receipt.dry_run,
        "reason": receipt.reason,
        "policy": dict(receipt.policy),
        "raw_content_stored": False,
    }


def _dict_to_receipt(d: dict) -> SandboxApplyReceipt:
    return SandboxApplyReceipt(
        schema_version=d.get("schema_version", 1),
        receipt_id=d.get("receipt_id", ""),
        timestamp=d.get("timestamp", ""),
        source_run_id=d.get("source_run_id", ""),
        sandbox_path=d.get("sandbox_path", ""),
        applied=bool(d.get("applied", False)),
        files_applied=list(d.get("files_applied") or []),
        files_skipped=list(d.get("files_skipped") or []),
        dry_run=bool(d.get("dry_run", False)),
        reason=d.get("reason", ""),
        policy=dict(d.get("policy") or {}),
        raw_content_stored=False,
    )


def log_sandbox_apply_receipt(receipt: SandboxApplyReceipt) -> None:
    path = Path.cwd() / _APPLY_RECEIPTS_PATH
    d = _receipt_to_dict(receipt)
    d["raw_content_stored"] = False
    append_jsonl(path, d)


def load_sandbox_apply_receipts() -> list[SandboxApplyReceipt]:
    path = Path.cwd() / _APPLY_RECEIPTS_PATH
    if not path.exists():
        return []
    receipts: list[SandboxApplyReceipt] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            receipts.append(_dict_to_receipt(json.loads(line)))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return receipts


def recent_sandbox_apply_receipts(limit: int = 10) -> list[SandboxApplyReceipt]:
    receipts = load_sandbox_apply_receipts()
    return receipts[-limit:] if len(receipts) > limit else receipts
