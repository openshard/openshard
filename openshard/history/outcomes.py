"""Local outcome records: what happened to a Shard's work after the run.

Outcomes (merged, deployed, rolled back, ...) are appended to
``.openshard/outcomes.jsonl`` beside ``runs.jsonl`` and joined at read time.
The run record itself is never edited, so its ``content_hash`` stays valid
and a later outcome can never be mistaken for tampering.

Read-time consumers pass the latest outcome for a Shard into
``build_receipt_contract(..., outcome_record=...)``; it takes precedence over
an ``outcome`` block the producer wrote at run time, because it is newer.
"""

from __future__ import annotations

import json
from pathlib import Path

from openshard.contracts.outcomes import OUTCOME_STATUSES, OutcomeReport
from openshard.history.jsonl_store import append_jsonl

OUTCOMES_FILENAME = "outcomes.jsonl"
SCHEMA_VERSION = 1


def outcomes_path(repo_path: Path | None = None) -> Path:
    return (repo_path or Path.cwd()) / ".openshard" / OUTCOMES_FILENAME


def record_outcome(report: OutcomeReport, *, repo_path: Path | None = None) -> Path:
    """Append one outcome record. Raises ``ValueError`` on an unknown status."""
    if report.status not in OUTCOME_STATUSES:
        raise ValueError(f"unknown outcome status {report.status!r}")
    path = outcomes_path(repo_path)
    record = {
        "schema_version": SCHEMA_VERSION,
        "shard_id": report.shard_id,
        "status": report.status,
        "source": report.source[:80],
        "reference": (report.reference or "")[:120] or None,
        "recorded_by": report.recorded_by,
        "human_intervention": report.human_intervention,
        "note": (report.note or "")[:300] or None,
        "recorded_at": report.recorded_at,
    }
    append_jsonl(path, record)
    return path


def load_outcomes(repo_path: Path | None = None) -> dict[str, dict]:
    """Latest outcome record per ``shard_id``. Never raises; skips bad lines."""
    path = outcomes_path(repo_path)
    latest: dict[str, dict] = {}
    if not path.exists():
        return latest
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
                if not isinstance(rec, dict):
                    continue
                sid = rec.get("shard_id")
                if isinstance(sid, str) and sid and rec.get("status") in OUTCOME_STATUSES:
                    latest[sid] = rec  # file order == time order
    except OSError:
        return latest
    return latest


def outcome_for_shard(shard_id: str, repo_path: Path | None = None) -> dict | None:
    return load_outcomes(repo_path).get(shard_id)
