"""Claude Code receipt import for OpenShard.

Turns a Claude Code session into a Shard-compatible run record without
live orchestration. Input: a task description + current repo git state.
Output: a coerced, content-hash-stamped entry written to .openshard/runs.jsonl.

Design constraints:
- Never raises in the public API.
- Never invents verification, cost, or approval.
- Never stores raw file content or secrets.
- All free text passes through secret scrubbing before storage.
- Imported Shards are always clearly marked with import_source = "claude_code".
- Builds its own canonical Events (see openshard.history.event) at
  observation time and embeds them as entry["events"] -- this adapter is a
  canonical Event producer (Migration 5), not just a later read-time
  projection. No separate Event store: events are one more field on the
  same runs.jsonl record.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

_MAX_FILES = 20
_MAX_NOTES_READ_CHARS = 4_000
_SUMMARY_CAP = 300
_TASK_CAP = 500
_PATH_CAP = 200

_STATUS_TO_CHANGE_TYPE: dict[str, str] = {
    "A": "create",
    "M": "update",
    "D": "delete",
    "R": "update",  # renamed: treat as update
    "C": "update",  # copied: treat as update
    "T": "update",  # type change
    "U": "update",  # unmerged
}


def _parse_git_changed_files(repo_path: Path) -> tuple[list[dict], str]:
    """Return changed files from ``git diff HEAD --name-status`` in *repo_path*.

    Returns ``(files, files_source)`` where ``files`` is a list of
    ``{path, change_type, summary}`` dicts and ``files_source`` is either
    ``"git_diff_inferred"`` or ``"not_available"``.  Never raises.

    File paths are stored relative to the repo root.  At most _MAX_FILES
    entries are returned.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--name-status"],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
        )
        if result.returncode != 0:
            return [], "not_available"
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return [], "git_diff_inferred"
    except Exception:
        return [], "not_available"

    from openshard.safety.sanitize import sanitize_text

    files: list[dict] = []
    for line in lines:
        if len(files) >= _MAX_FILES:
            break
        parts = line.split("\t", maxsplit=1)
        if len(parts) < 2:
            continue
        status_raw, path_raw = parts[0].strip(), parts[1].strip()
        status_code = status_raw[0] if status_raw else "M"
        change_type = _STATUS_TO_CHANGE_TYPE.get(status_code, "update")
        safe_path = sanitize_text(path_raw, _PATH_CAP)
        if not safe_path:
            continue
        files.append({
            "path": safe_path,
            "change_type": change_type,
            "summary": "inferred from git diff",
        })

    return files, "git_diff_inferred"


def _scrub_notes_file(path: Path, max_chars: int = _MAX_NOTES_READ_CHARS) -> str:
    """Read *path*, scrub for secrets, return safe text capped at *max_chars*.

    Returns an empty string on any error.  Never raises.  The raw file
    content is never stored — only the scrubbed, capped result.
    """
    from openshard.security.secret_scan import scrub_text_for_secrets

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    capped = raw[:max_chars]
    scrubbed, _ = scrub_text_for_secrets(capped, source_label="<notes-file>")
    return scrubbed[:_SUMMARY_CAP]


def _sanitize_task(task: str) -> str:
    """Sanitize a task description for safe storage.

    Scrubs secret-like values, replaces absolute paths with a neutral token,
    strips control characters, and caps length.  Returns a neutral placeholder
    if nothing safe remains.
    """
    from openshard.security.secret_scan import scrub_text_for_secrets

    if not isinstance(task, str) or not task.strip():
        return "Claude Code session import"
    scrubbed, _ = scrub_text_for_secrets(task[:_TASK_CAP], source_label="<task>")
    cleaned = " ".join(scrubbed.split())
    return cleaned[:_TASK_CAP] or "Claude Code session import"


def _sanitize_model(model: str | None) -> str:
    """Return a safe model string or ``"unknown"``."""
    from openshard.safety.sanitize import sanitize_text

    if not model:
        return "unknown"
    safe = sanitize_text(model, 100)
    return safe if safe else "unknown"


def _build_import_events(entry: dict, changed_files: list[dict], files_source: str) -> list[dict]:
    """Build this import's canonical Events at observation time (Migration 5).

    Called once, right before the entry is coerced and written, while
    ``changed_files``/``files_source`` and the entry's own run_id/shard_id/
    attempt_number are still local values here -- not reconstructed later
    from stored fields the way ``events_from_entry``'s legacy projection
    path has to. Serialized via ``Event.to_dict()`` into a plain list so it
    round-trips through ``runs.jsonl`` like any other field. Never raises;
    returns [] on any internal failure so a broken Event can never block an
    import.

    Evidence mirrors ``event.build_event_from_adapter_entry``'s import
    logic unchanged: git-diff-sourced facts are EVIDENCE_GIT_OBSERVED,
    everything else EVIDENCE_AGENT_REPORTED -- OpenShard did not execute
    this run, so no fact here is ever EVIDENCE_DIRECTLY_OBSERVED.
    """
    try:
        from openshard.history.event import (
            EVENT_FILE_CHANGED,
            EVENT_RUN_COMPLETED,
            EVIDENCE_AGENT_REPORTED,
            EVIDENCE_GIT_OBSERVED,
            SOURCE_CLAUDE_CODE_IMPORT,
            STATUS_FAILED,
            STATUS_PASSED,
            STATUS_UNKNOWN,
            make_event,
        )

        file_evidence = EVIDENCE_GIT_OBSERVED if files_source == "git_diff_inferred" else EVIDENCE_AGENT_REPORTED

        verification_attempted = bool(entry.get("verification_attempted"))
        verification_passed = entry.get("verification_passed")
        if verification_attempted and verification_passed is True:
            run_status = STATUS_PASSED
        elif verification_attempted and verification_passed is False:
            run_status = STATUS_FAILED
        else:
            run_status = STATUS_UNKNOWN

        actor = entry.get("import_source") if isinstance(entry.get("import_source"), str) else None
        summary = entry.get("summary") or entry.get("import_note") or "external run observed"
        common = {
            "run_id": entry.get("run_id"),
            "shard_id": entry.get("shard_id"),
            "attempt_number": entry.get("attempt_number"),
            "actor": actor,
            "occurred_at": entry.get("timestamp"),
        }

        events = [
            make_event(
                event_type=EVENT_RUN_COMPLETED,
                source=SOURCE_CLAUDE_CODE_IMPORT,
                action=str(summary),
                status=run_status,
                evidence=file_evidence,
                **common,
            )
        ]
        # File-change status stays unknown: a changed file is an observed
        # fact, not a pass/fail outcome.
        for f in changed_files:
            events.append(
                make_event(
                    event_type=EVENT_FILE_CHANGED,
                    source=SOURCE_CLAUDE_CODE_IMPORT,
                    action=f"file {f.get('change_type', 'update')}",
                    target=f.get("path"),
                    status=STATUS_UNKNOWN,
                    evidence=file_evidence,
                    **common,
                )
            )
        return [e.to_dict() for e in events]
    except Exception:
        return []


def build_claude_code_import_entry(
    task: str,
    *,
    model: str | None = None,
    notes_file: Path | None = None,
    repo_path: Path,
    shard_id: str | None = None,
    attempt_number: int = 1,
    run_index: int | None = None,
) -> dict:
    """Build a coerced Shard entry for a Claude Code session import.

    Never raises.  Never invents verification, cost, model identity, or
    approval.  All free text is scrubbed before use.  The returned dict
    has already passed through ``coerce_shard_entry`` (blocked fields
    stripped, content_hash stamped).

    ``shard_id``, when given, must already be a validated, existing Shard
    id (see ``openshard.history.run_attempt.resolve_shard_for_attempt`` —
    validation is the caller's responsibility so this function keeps its
    never-raises contract); the entry is then stamped as another attempt
    on that Shard. When omitted, a fresh shard_id is minted from this
    entry's own timestamp and ``run_index`` — a new Shard, attempt 1.

    Also embeds this import's own canonical Events under ``entry["events"]``
    (see ``_build_import_events``) — this entry is a Migration 5 producer
    record, so ``events_from_entry`` will use those directly instead of
    projecting one from the other fields at read time.
    """
    from openshard.analysis.repo_map import collect_git_info
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry

    safe_task = _sanitize_task(task)
    safe_model = _sanitize_model(model)
    git_info = collect_git_info(repo_path)
    changed_files, files_source = _parse_git_changed_files(repo_path)

    summary = ""
    if notes_file is not None:
        summary = _scrub_notes_file(notes_file)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    files_created = sum(1 for f in changed_files if f["change_type"] == "create")
    files_updated = sum(1 for f in changed_files if f["change_type"] == "update")
    files_deleted = sum(1 for f in changed_files if f["change_type"] == "delete")

    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": now,
        "task": safe_task,
        "execution_model": safe_model,
        "executor": "claude_code_import",
        "import_source": "claude_code",
        "import_method": "openshard_import_v0",
        "import_note": (
            "Imported from Claude Code. "
            "Files inferred from git diff. "
            "Model, cost, and verification not recorded by OpenShard."
        ),
        "files_source": files_source,
        "verification_attempted": False,
        "verification_passed": None,
        "files_created": files_created,
        "files_updated": files_updated,
        "files_deleted": files_deleted,
        "files_detail": changed_files,
        "git_branch": git_info.branch,
        "git_head_commit_hash": git_info.head_commit,
        "git_dirty": git_info.dirty,
        "summary": summary,
    }
    # Additive stable repository identity from the origin remote (no network,
    # credentials stripped, never raises). Omitted when there is no remote.
    try:
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD, capture_repo_identity
        _repo_identity = capture_repo_identity(repo_path)
        if _repo_identity:
            entry[REPO_IDENTITY_FIELD] = _repo_identity
    except Exception:
        pass
    entry["run_id"] = entry["timestamp"]
    if shard_id:
        entry["shard_id"] = shard_id
    else:
        from openshard.history.shard_contract import _make_shard_id
        entry["shard_id"] = _make_shard_id(entry["timestamp"], run_index)
    entry["attempt_number"] = attempt_number

    entry["events"] = _build_import_events(entry, changed_files, files_source)

    return coerce_shard_entry(entry)


def write_import_entry(entry: dict, repo_path: Path) -> None:
    """Append *entry* to ``.openshard/runs.jsonl`` under *repo_path*.

    Creates the directory if it does not exist.  Never raises.
    """
    from openshard.history.jsonl_store import append_jsonl

    store_dir = repo_path / ".openshard"
    store_dir.mkdir(parents=True, exist_ok=True)
    append_jsonl(store_dir / "runs.jsonl", entry)
