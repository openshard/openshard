"""Storage protocols and their local (JSONL/filesystem) implementations.

The pipeline talks only to ``HistoryStore`` and ``JobStore``; no connector or
parser imports this module. A hosted implementation plugs in behind the same
two protocols.

Local layout under a repository's ``.openshard/``:

* ``runs.jsonl`` -- historical receipts are *appended* (never upserted).
* ``attachments.jsonl`` -- append-only later evidence pinned to a receipt's
  ``content_hash`` (§7). Kept separate from Verification v2's
  ``verifications.jsonl``; both are sidecars that never rewrite a receipt.
* ``imports/index.jsonl`` -- dedupe cache, rebuildable from the ``import``
  blocks in ``runs.jsonl`` and the attachments.
* ``imports/<job_id>/{job.json, items.jsonl, cursor.json, cancel.request}``
  -- job state. No content: object keys, hashes, states and error classes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from openshard.history.jsonl_store import LockTimeoutError, append_jsonl, history_file_lock

RUNS_FILE = "runs.jsonl"
ATTACHMENTS_FILE = "attachments.jsonl"
IMPORTS_DIR = "imports"
INDEX_FILE = "index.jsonl"


@dataclass
class ImportRecord:
    """What is already stored for one ``import_key``."""

    import_key: str
    source_sha256: str
    receipt_id: str | None = None  # a historical receipt
    attachment_id: str | None = None  # or an attachment on a live receipt


@dataclass
class LiveRecord:
    receipt_id: str
    content_hash: str | None


@dataclass
class StoreSnapshot:
    imports: dict[str, ImportRecord] = field(default_factory=dict)
    live: dict[tuple[str, str], LiveRecord] = field(default_factory=dict)  # (executor, session_id)
    record_count: int = 0


class HistoryStore(Protocol):
    def snapshot(self) -> StoreSnapshot: ...

    def append_receipt(self, entry: dict) -> None: ...

    def append_attachment(self, attachment: dict) -> None: ...

    def record_count(self) -> int: ...


class ActiveJobError(RuntimeError):
    """Another import job holds this repository's import lock."""


def _read_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except FileNotFoundError:
        return


class LocalHistoryStore:
    """``.openshard/runs.jsonl`` + ``attachments.jsonl`` + the import index cache."""

    def __init__(self, repo_root: Path) -> None:
        self.root = Path(repo_root)
        self.base = self.root / ".openshard"

    @property
    def runs_path(self) -> Path:
        return self.base / RUNS_FILE

    @property
    def attachments_path(self) -> Path:
        return self.base / ATTACHMENTS_FILE

    @property
    def index_path(self) -> Path:
        return self.base / IMPORTS_DIR / INDEX_FILE

    def snapshot(self) -> StoreSnapshot:
        """Authoritative dedupe state, rebuilt from runs.jsonl + attachments.jsonl.

        The index file is only a cache: this never trusts it over the records,
        so a crash between a receipt append and its index line cannot cause a
        duplicate on resume.
        """
        snap = StoreSnapshot()
        for rec in _read_jsonl(self.runs_path):
            snap.record_count += 1
            imp = rec.get("import")
            if isinstance(imp, dict) and isinstance(imp.get("import_key"), str):
                snap.imports[imp["import_key"]] = ImportRecord(
                    imp["import_key"], str(imp.get("source_sha256") or ""), receipt_id=rec.get("receipt_id")
                )
            capture = rec.get("capture")
            if isinstance(capture, dict) and isinstance(capture.get("session_id"), str):
                executor = rec.get("executor")
                if isinstance(executor, str) and isinstance(rec.get("receipt_id"), str):
                    snap.live[(executor, capture["session_id"])] = LiveRecord(
                        rec["receipt_id"], rec.get("content_hash")
                    )
        for att in _read_jsonl(self.attachments_path):
            key = att.get("import_key")
            if not isinstance(key, str):
                continue
            existing = snap.imports.get(key)
            if existing is None or existing.receipt_id is None:
                snap.imports[key] = ImportRecord(
                    key, str(att.get("source_sha256") or ""), attachment_id=att.get("attachment_id")
                )
        return snap

    def record_count(self) -> int:
        return sum(1 for _ in _read_jsonl(self.runs_path))

    def append_receipt(self, entry: dict) -> None:
        append_jsonl(self.runs_path, entry)
        self._index(entry.get("import"), receipt_id=entry.get("receipt_id"))

    def append_attachment(self, attachment: dict) -> None:
        append_jsonl(self.attachments_path, attachment)
        self._index(attachment, attachment_id=attachment.get("attachment_id"))

    def _index(self, block: object, **ids: object) -> None:
        if not isinstance(block, dict):
            return
        try:
            append_jsonl(self.index_path, {
                "import_key": block.get("import_key"),
                "source_sha256": block.get("source_sha256"),
                **{k: v for k, v in ids.items() if v},
            })
        except OSError:
            pass  # a cache; snapshot() never depends on it

    def rebuild_index(self) -> int:
        from openshard.history.jsonl_store import write_jsonl

        snap = self.snapshot()
        rows = [
            {"import_key": r.import_key, "source_sha256": r.source_sha256,
             **({"receipt_id": r.receipt_id} if r.receipt_id else {}),
             **({"attachment_id": r.attachment_id} if r.attachment_id else {})}
            for r in snap.imports.values()
        ]
        write_jsonl(self.index_path, rows)
        return len(rows)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class LocalJobStore:
    def __init__(self, repo_root: Path) -> None:
        self.base = Path(repo_root) / ".openshard" / IMPORTS_DIR

    def job_dir(self, job_id: str) -> Path:
        return self.base / job_id

    def write_job(self, job_id: str, job: dict) -> None:
        path = self.job_dir(job_id) / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("job.json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(job, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def read_job(self, job_id: str) -> dict | None:
        try:
            data = json.loads((self.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def append_item(self, job_id: str, item: dict) -> None:
        append_jsonl(self.job_dir(job_id) / "items.jsonl", item)

    def items(self, job_id: str) -> list[dict]:
        return list(_read_jsonl(self.job_dir(job_id) / "items.jsonl"))

    def write_cursor(self, job_id: str, cursor: dict) -> None:
        path = self.job_dir(job_id) / "cursor.json"
        tmp = path.with_name("cursor.json.tmp")
        tmp.write_text(json.dumps(cursor), encoding="utf-8")
        os.replace(tmp, path)

    def read_cursor(self, job_id: str) -> dict | None:
        try:
            data = json.loads((self.job_dir(job_id) / "cursor.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def request_cancel(self, job_id: str) -> bool:
        d = self.job_dir(job_id)
        if not (d / "job.json").is_file():
            return False
        (d / "cancel.request").write_text("cancel\n", encoding="utf-8")
        return True

    def cancel_requested(self, job_id: str) -> bool:
        return (self.job_dir(job_id) / "cancel.request").is_file()

    def list_jobs(self) -> list[dict]:
        if not self.base.is_dir():
            return []
        jobs = []
        for d in sorted(self.base.iterdir()):
            if d.is_dir() and d.name.startswith("ijob_"):
                job = self.read_job(d.name)
                if job is not None:
                    jobs.append(job)
        return sorted(jobs, key=lambda j: str(j.get("created_at") or ""))

    @contextmanager
    def active_lock(self) -> Iterator[None]:
        """One active import job per repository (``imports/active.lock``).

        An OS-level lock: a crashed job releases it with its process, so a
        crash never wedges the repository.
        """
        try:
            with history_file_lock(self.base / "active", timeout=0.2):
                yield
        except LockTimeoutError:
            raise ActiveJobError("another import job is running in this repository") from None
