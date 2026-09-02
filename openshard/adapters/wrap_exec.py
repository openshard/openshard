"""Claude Code live subprocess wrapping for OpenShard.

Runs an external command (such as Claude Code) as a subprocess, captures
git state before and after the run, and creates a Shard receipt automatically.
Output: a coerced, content-hash-stamped entry written to .openshard/runs.jsonl.

Design constraints:
- Never raises in the public API.
- Never invents verification, cost, or approval.
- Never captures subprocess stdout/stderr — full passthrough only.
- Never stores raw file content or secrets.
- All free text passes through secret scrubbing before storage.
- Wrapped Shards are always clearly marked with import_source = "claude_code"
  and import_method = "openshard_wrap_v0".
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
from typing import Any

_MAX_FILES = 20
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


def _sanitize_task(task: str) -> str:
    """Sanitize a task description for safe storage.

    Scrubs secret-like values, strips control characters, and caps length.
    Returns a neutral placeholder if nothing safe remains.
    """
    from openshard.security.secret_scan import scrub_text_for_secrets

    if not isinstance(task, str) or not task.strip():
        return "Claude Code wrap session"
    scrubbed, _ = scrub_text_for_secrets(task[:_TASK_CAP], source_label="<task>")
    cleaned = " ".join(scrubbed.split())
    return cleaned[:_TASK_CAP] or "Claude Code wrap session"


def _sanitize_model(model: str | None) -> str:
    """Return a safe model string or ``"unknown"``."""
    from openshard.safety.sanitize import sanitize_text

    if not model:
        return "unknown"
    safe = sanitize_text(model, 100)
    return safe if safe else "unknown"


def capture_pre_run_state(repo_path: Path) -> dict[str, Any]:
    """Capture git state before running the wrapped command.

    Calls ``collect_git_info`` and returns a snapshot dict. Never raises.
    """
    from openshard.analysis.repo_map import collect_git_info

    git_info = collect_git_info(repo_path)
    return {
        "git_branch": git_info.branch,
        "git_head_commit_hash": git_info.head_commit,
        "git_dirty": git_info.dirty,
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def run_wrapped_command(cmd: list[str]) -> int:
    """Run *cmd* as a subprocess with full stdin/stdout/stderr passthrough.

    Returns the subprocess exit code. Never captures output. Never raises.
    """
    try:
        result = subprocess.run(cmd)
        return result.returncode
    except FileNotFoundError:
        return 127
    except Exception:
        return 1


def _build_wrap_events(
    entry: dict, changed_files: list[dict], files_source: str, exit_code: int,
) -> list[dict]:
    """Build this wrap run's canonical Events at observation time (Migration 5).

    Mirrors ``claude_code_import._build_import_events`` with one difference:
    the run-level event is keyed off ``exit_code`` -- a fact OpenShard
    directly observed by running the subprocess itself (``run_wrapped_command``),
    not one reported by Claude Code -- so that one event alone gets
    EVIDENCE_DIRECTLY_OBSERVED and EVENT_RUN_COMPLETED/EVENT_RUN_FAILED
    accordingly. Every file-level event still reflects only what git diff
    showed (EVIDENCE_GIT_OBSERVED/EVIDENCE_AGENT_REPORTED, same as import) --
    OpenShard observed that the wrapped process exited a certain way, not
    that any specific file changed for that reason. Never raises; returns
    [] on any internal failure.
    """
    try:
        from openshard.history.event import (
            EVENT_FILE_CHANGED,
            EVENT_RUN_COMPLETED,
            EVENT_RUN_FAILED,
            EVIDENCE_AGENT_REPORTED,
            EVIDENCE_DIRECTLY_OBSERVED,
            EVIDENCE_GIT_OBSERVED,
            SOURCE_CLAUDE_CODE_WRAP,
            STATUS_FAILED,
            STATUS_PASSED,
            STATUS_UNKNOWN,
            make_event,
        )

        file_evidence = EVIDENCE_GIT_OBSERVED if files_source == "git_diff_inferred" else EVIDENCE_AGENT_REPORTED
        run_event_type = EVENT_RUN_COMPLETED if exit_code == 0 else EVENT_RUN_FAILED
        run_status = STATUS_PASSED if exit_code == 0 else STATUS_FAILED

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
                event_type=run_event_type,
                source=SOURCE_CLAUDE_CODE_WRAP,
                action=str(summary),
                status=run_status,
                evidence=EVIDENCE_DIRECTLY_OBSERVED,
                **common,
            )
        ]
        # File-change status stays unknown: a changed file is an observed
        # fact, not a pass/fail outcome.
        for f in changed_files:
            events.append(
                make_event(
                    event_type=EVENT_FILE_CHANGED,
                    source=SOURCE_CLAUDE_CODE_WRAP,
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


def build_wrap_entry(
    task: str,
    *,
    model: str | None = None,
    pre_state: dict[str, Any],
    exit_code: int,
    repo_path: Path,
    shard_id: str | None = None,
    attempt_number: int = 1,
    run_index: int | None = None,
) -> dict:
    """Build a coerced Shard entry for a wrapped Claude Code subprocess run.

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

    Also embeds this run's own canonical Events under ``entry["events"]``
    (see ``_build_wrap_events``) — this entry is a Migration 5 producer
    record, so ``events_from_entry`` will use those directly instead of
    projecting one from the other fields at read time.
    """
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry

    safe_task = _sanitize_task(task)
    safe_model = _sanitize_model(model)

    changed_files, files_source = _parse_git_changed_files(repo_path)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    files_created = sum(1 for f in changed_files if f["change_type"] == "create")
    files_updated = sum(1 for f in changed_files if f["change_type"] == "update")
    files_deleted = sum(1 for f in changed_files if f["change_type"] == "delete")

    metadata: dict[str, Any] = {}
    if exit_code != 0:
        metadata["wrap_status"] = "subprocess_failed"

    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": now,
        "task": safe_task,
        "execution_model": safe_model,
        "executor": "claude_code_wrap",
        "import_source": "claude_code",
        "import_method": "openshard_wrap_v0",
        "import_note": (
            "Wrapped Claude Code subprocess. "
            "Files inferred from git diff. "
            "Model, cost, and verification not recorded by OpenShard."
        ),
        "files_source": files_source,
        "verification_attempted": False,
        "files_created": files_created,
        "files_updated": files_updated,
        "files_deleted": files_deleted,
        "files_detail": changed_files,
        "git_branch": pre_state.get("git_branch"),
        "git_head_commit_hash": pre_state.get("git_head_commit_hash"),
        "git_dirty": pre_state.get("git_dirty", False),
        "wrap_exit_code": exit_code,
        "summary": "",
    }

    if metadata:
        entry["metadata"] = metadata

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

    entry["events"] = _build_wrap_events(entry, changed_files, files_source, exit_code)

    return coerce_shard_entry(entry)


def write_wrap_entry(entry: dict, repo_path: Path) -> None:
    """Append *entry* to ``.openshard/runs.jsonl`` under *repo_path*.

    Creates the directory if it does not exist.  Never raises.
    """
    from openshard.history.jsonl_store import append_jsonl

    store_dir = repo_path / ".openshard"
    store_dir.mkdir(parents=True, exist_ok=True)
    append_jsonl(store_dir / "runs.jsonl", entry)
