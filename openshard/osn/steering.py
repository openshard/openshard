"""Mid-run steering: operator notes and a stop request, read at turn boundaries.

    openshard osn steer <run id> "note"      -> shown to the model on its next turn
    openshard osn steer <run id> --stop      -> the run stops before its next turn

Both append one line to ``.openshard/osn-runs/<run id>/steering.jsonl``. The
executor reads the file at the start of every turn (one small read; nothing
is read while a model call is in flight), shows new notes on that turn as
operator notes, and on a stop request ends the attempt before any further
model call so the run checkpoints as interrupted and ``osn resume`` can pick
it up. A note is advisory context from the person running OpenShard: it
never changes policy, the verify command or the file-mutation gate, and the
Receipt records that a note was *shown* (kind, attempt, turn, size, hash),
never that it was followed and never its text.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.osn.checkpoint import checkpoint_dir

STEERING_FILE = "steering.jsonl"
KIND_NOTE = "note"
KIND_STOP = "stop"
KINDS = (KIND_NOTE, KIND_STOP)
MAX_NOTE_CHARS = 2000
MAX_NOTES_PER_TURN = 5
MAX_NOTES_PER_RUN = 20
MAX_FILE_BYTES = 256 * 1024


def steering_path(repo_root: Path, run_id: str) -> Path:
    return checkpoint_dir(repo_root, run_id) / STEERING_FILE


def _clean(text: str) -> str:
    cleaned = "".join(
        ch if ch == "\n" or (unicodedata.category(ch) != "Cc") else " " for ch in text
    ).strip()
    return cleaned[:MAX_NOTE_CHARS]


def write_steering(repo_root: Path, run_id: str, kind: str, text: str = "") -> dict[str, Any]:
    """Append one steering event for a checkpointed run. Raises ``ValueError`` when the run is unknown."""
    if kind not in KINDS:
        raise ValueError(f"unknown steering kind '{kind}'")
    directory = checkpoint_dir(repo_root, run_id)
    if not (directory / "checkpoint.json").is_file():
        raise ValueError(f"no checkpointed OSN run '{run_id}' under .openshard/osn-runs/")
    note = _clean(text) if kind == KIND_NOTE else ""
    if kind == KIND_NOTE and not note:
        raise ValueError("a note needs some text")
    record = {"at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind, "text": note}
    with (directory / STEERING_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


@dataclass
class SteeringReader:
    """Reads new steering events for one run; remembers what the model was shown."""

    repo_root: Path
    run_id: str
    _offset: int = 0
    notes_shown: int = 0
    stop_requested: bool = False
    shown: list[dict[str, Any]] = field(default_factory=list)

    def poll(self, attempt: int, turn: int, role: str = "executor") -> tuple[list[str], bool]:
        """New notes (bounded) and whether a stop was requested, recording what is handed over. Never raises."""
        path = steering_path(self.repo_root, self.run_id)
        notes: list[str] = []
        stop = False
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return [], False
            with path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return [], False
        if not chunk.endswith(b"\n"):
            chunk = chunk[: chunk.rfind(b"\n") + 1]  # a line still being written waits for the next turn
        self._offset += len(chunk)
        for raw in chunk.decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("kind")
            if kind == KIND_STOP:
                stop = True
            elif kind == KIND_NOTE and isinstance(record.get("text"), str):
                text = _clean(record["text"])
                if text and len(notes) < MAX_NOTES_PER_TURN and self.notes_shown + len(notes) < MAX_NOTES_PER_RUN:
                    notes.append(text)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for text in notes:
            self.shown.append({
                "kind": KIND_NOTE, "attempt": attempt, "turn": turn, "role": role, "chars": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "at": now,
            })
        self.notes_shown += len(notes)
        if stop and not self.stop_requested:
            self.stop_requested = True
            self.shown.append({"kind": KIND_STOP, "attempt": attempt, "turn": turn, "role": role, "at": now})
        return notes, stop

    def to_record(self) -> dict[str, Any] | None:
        """What steering happened (never note text), or None when nothing did."""
        if not self.shown:
            return None
        return {
            "source": "operator_file",
            "notes_shown": self.notes_shown,
            "stop_requested": self.stop_requested,
            "events": list(self.shown),
            "evidence": "notes were shown to the model on the recorded turn; whether it followed them is not observed",
        }


__all__ = [
    "KIND_NOTE", "KIND_STOP", "MAX_NOTE_CHARS", "STEERING_FILE", "SteeringReader", "steering_path", "write_steering",
]
