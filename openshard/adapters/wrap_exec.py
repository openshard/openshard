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

    entry["run_id"] = entry["timestamp"]
    if shard_id:
        entry["shard_id"] = shard_id
    else:
        from openshard.history.shard_contract import _make_shard_id
        entry["shard_id"] = _make_shard_id(entry["timestamp"], run_index)
    entry["attempt_number"] = attempt_number

    return coerce_shard_entry(entry)


def write_wrap_entry(entry: dict, repo_path: Path) -> None:
    """Append *entry* to ``.openshard/runs.jsonl`` under *repo_path*.

    Creates the directory if it does not exist.  Never raises.
    """
    from openshard.history.jsonl_store import append_jsonl

    store_dir = repo_path / ".openshard"
    store_dir.mkdir(parents=True, exist_ok=True)
    append_jsonl(store_dir / "runs.jsonl", entry)
