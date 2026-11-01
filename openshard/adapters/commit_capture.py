"""A Receipt for one commit, rebuilt from the committing agent's own transcript.

Hooks are how Openshard normally watches an agent: they record every tool
call, prompt and token as the session runs. A session that started without
them -- Claude Code on the web opened outside the repository, a cloud
container nobody ran ``openshard setup`` in -- leaves Openshard nothing but
the commit GitHub sees, so its Receipt has no usage, tool calls or timings.

The agent still keeps its own transcript on the machine it runs on. At commit
time, ``openshard capture commit`` reads the part of that transcript since
the session's previous captured commit and writes one Receipt for this
commit: the agent, model, owner, prompts, tool calls (each failed one
counted), the tokens and their list-rate cost, when the work started and the
commit itself with its files and line counts, which git observed. Synced, it
is the Receipt bound to the commit, so the GitHub workflow's independent
checks attach to it instead of making a second, thinner Receipt.

Truth rules
-----------
* Reconstructed, and labelled so. Everything from the transcript is the
  transcript's; the session's live events were not observed. Git facts (the
  commit, its parent, its files and lines) are read from git, not the agent.
* Unknown stays unknown: no usage in the window means no tokens and no cost,
  never zero; a model without a list rate leaves cost unknown with a reason.
* Each transcript message belongs to one commit: the window starts where the
  session's previous captured commit ended (a state file per session under
  the Openshard home) and ends at this commit's time.
* Verification is not claimed here. The checks the agent ran are listed as
  its tool calls; the verdict comes from the independent CI run.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.adapters.claude_transcript_import import (
    REASON_NO_COST,
    REASON_NO_END,
    REASON_NO_GIT,
    TranscriptFacts,
    _parse_record,
    _Reader,
    build_transcript_import_entry,
)
from openshard.adapters.claude_transcript_usage import (
    _new_transcript_state,
    _read_transcript_increment,
    _summarise_transcript_state,
    _transcript_files,
)
from openshard.util.git import run_git

IMPORT_METHOD = "commit_transcript_capture"
ENDED_BY_COMMIT = "commit_captured"
_SHA_LEN = 40


class CaptureUnavailable(Exception):
    """No true Receipt can be built for this commit; the message says why."""


@dataclass
class CommitCaptureReport:
    status: str  # dry_run | written | already_recorded
    receipt_id: str
    commit: str
    session_id: str
    window: dict[str, str | None] = field(default_factory=dict)
    entry: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "receipt_id": self.receipt_id, "commit": self.commit,
                               "session_id": self.session_id, "window": dict(self.window)}
        if self.entry is not None:
            out["entry"] = self.entry
        return out


def receipt_id_for_commit(session_id: str, sha: str) -> str:
    """Deterministic: capturing the same commit of the same session twice yields the same id."""
    digest = hashlib.sha256(f"claude-commit:{session_id}:{sha.lower()}".encode()).hexdigest()
    return f"rcpt_{digest[:32]}"


def _git(repo: Path, *args: str) -> str | None:
    out = run_git(repo, list(args))
    return out.strip() if isinstance(out, str) else None


def _iso_utc(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CommitFacts:
    sha: str
    parent: str | None
    committed_at: str
    branch: str | None
    files: list[dict]
    added: int | None
    removed: int | None
    subject: str | None = None


def read_commit(repo: Path, rev: str = "HEAD") -> CommitFacts:
    sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    if not sha or len(sha) != _SHA_LEN:
        raise CaptureUnavailable(f"{rev} is not a commit in {repo}.")
    stamp = _git(repo, "log", "-1", "--format=%cI", sha)
    if not stamp:
        raise CaptureUnavailable(f"Could not read the commit time of {sha[:7]}.")
    parent = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^") or None
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = None if not branch or branch == "HEAD" else branch
    numstat = _git(repo, "show", "--format=", "--numstat", "--no-renames", sha) or ""
    status = _git(repo, "show", "--format=", "--name-status", "--no-renames", sha) or ""
    kinds = {}
    for line in status.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            kinds[parts[1]] = {"A": "created", "D": "deleted"}.get(parts[0][:1], "updated")
    files: list[dict] = []
    added = removed = 0
    exact = True
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        if a.isdigit() and d.isdigit():
            added += int(a)
            removed += int(d)
        else:
            exact = False  # binary file: no line count
        files.append({"path": path, "change_type": kinds.get(path, "updated"), "attribution": "git_observed",
                      "summary": "in the commit, read from git"})
    subject = _git(repo, "log", "-1", "--format=%s", sha) or None
    return CommitFacts(subject=subject, sha=sha.lower(), parent=parent.lower() if parent else None, committed_at=_iso_utc(stamp),
                       branch=branch, files=files[:200], added=added if exact else None,
                       removed=removed if exact else None)


def _openshard_home() -> Path:
    return Path(os.environ.get("OPENSHARD_HOME") or Path.home() / ".openshard")


def _state_path(session_id: str) -> Path:
    return _openshard_home() / "commit-capture" / f"{session_id}.json"


def _load_since(session_id: str) -> str | None:
    try:
        data = json.loads(_state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("until") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _save_until(session_id: str, until: str, sha: str) -> None:
    path = _state_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"until": until, "commit": sha}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _in_window(stamp: object, since: str | None, until: str) -> bool:
    """Exact instants: (since, until + 1s). The commit time has whole seconds, the transcript milliseconds."""
    if not isinstance(stamp, str):
        return False
    try:
        at = _instant(stamp)
    except ValueError:
        return False
    return (since is None or at > _instant(since)) and at < _instant(_bump(until))


def read_window(path: Path, *, repo_path: Path, since: str | None, until: str) -> TranscriptFacts | None:
    """The transcript's facts for records stamped in [*since*, *until*]; None when the window is empty."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    facts = TranscriptFacts(path=path, session_id="", sha256=hashlib.sha256(raw).hexdigest())
    reader = _Reader(facts, repo_path)
    for line in raw.splitlines():
        if not line.strip():
            continue
        record = _parse_record(line)
        if record is None:
            facts.unreadable_lines += 1
            continue
        if not facts.session_id and isinstance(record.get("sessionId"), str):
            facts.session_id = record["sessionId"]
        if _in_window(record.get("timestamp"), since, until):
            reader.note(record)
    reader.finish()
    if not facts.session_id or not facts.records:
        return None
    state = _new_transcript_state(_bump(since) if since else None, _bump(until))
    files, capped = _transcript_files(path)
    state["capped"] = capped
    try:
        facts.usage = _summarise_transcript_state(state) if _read_transcript_increment(state, files) else None
    except Exception:
        facts.usage = None
    return facts


def _bump(until: str) -> str:
    """The usage window's end is exclusive; the commit's own second is included."""
    dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    return datetime.fromtimestamp(dt.timestamp() + 1, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_commit_entry(facts: TranscriptFacts, commit: CommitFacts, *, repo_path: Path, since: str | None) -> dict:
    from openshard.history.capture_completeness import REASON_SESSION_END_NOT_OBSERVED, make_reason
    from openshard.history.shard_schema import coerce_shard_entry

    entry = build_transcript_import_entry(facts, repo_path=repo_path)
    entry.pop("content_hash", None)
    capture = entry["capture"]
    reasons = capture["completeness"]["reasons"]
    if REASON_NO_GIT in reasons:
        reasons.remove(REASON_NO_GIT)
    if REASON_NO_END in reasons:
        reasons[reasons.index(REASON_NO_END)] = make_reason(
            REASON_SESSION_END_NOT_OBSERVED, detail="closed at the commit it produced; live session events were not observed",
        )
    if entry.get("estimated_cost") is not None and REASON_NO_COST in reasons:
        reasons.remove(REASON_NO_COST)
    if commit.subject:
        # The task is what the commit says it did; the window's prompts stay counted in capture.
        from openshard.adapters.claude_code_import import _sanitize_task
        from openshard.history.task_title import derive_task_title

        entry["task"] = _sanitize_task(commit.subject, placeholder=entry["task"])
        entry["task_title"] = derive_task_title(entry["task"])
    entry["receipt_id"] = receipt_id_for_commit(facts.session_id, commit.sha)
    entry["import_method"] = IMPORT_METHOD
    entry["import_note"] = (
        "Rebuilt at commit time from the committing agent's own transcript, because the session ran without "
        "Openshard's hooks. Prompts, tool calls and token usage are the transcript's for the work since the "
        "previous captured commit; cost is an estimate at official list rates; the commit, its files and line "
        "counts were read from git. Live session events were not observed."
    )
    entry["git_head_commit_hash"] = commit.parent
    entry["git_branch"] = commit.branch
    entry["git_end_head"] = commit.sha
    entry["session_commits"] = {"source": "git_observed", "shas": [commit.sha], "truncated": False, "result": commit.sha}
    entry["files_source"] = "git_observed"
    entry["files_detail"] = commit.files
    entry["files_created"] = sum(1 for f in commit.files if f["change_type"] == "created")
    entry["files_deleted"] = sum(1 for f in commit.files if f["change_type"] == "deleted")
    entry["files_updated"] = len(commit.files) - entry["files_created"] - entry["files_deleted"]
    if commit.added is not None and commit.removed is not None:
        entry["lines_added"], entry["lines_removed"] = commit.added, commit.removed
    capture["reconstructed"]["ended_by"] = ENDED_BY_COMMIT
    capture["reconstructed"]["window"] = {"since": since, "until": commit.committed_at, "commit": commit.sha}
    capture["ended_at"] = commit.committed_at
    return coerce_shard_entry(entry)


def _already_recorded(repo_path: Path, receipt_id: str) -> bool:
    path = repo_path / ".openshard" / "runs.jsonl"
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return any(receipt_id in line for line in fh)
    except OSError:
        return False


def capture_commit(repo_path: Path, *, rev: str = "HEAD", session_id: str | None = None,
                   transcript: Path | None = None, since: str | None = None, write: bool = False) -> CommitCaptureReport:
    """Build (and with *write*, record) the Receipt of commit *rev* from the agent's transcript."""
    from openshard.adapters.commit_usage_trailers import (
        UsageUnavailable,
        resolve_since,
        session_transcript,
    )

    repo_path = Path(repo_path)
    try:
        sid, main, _ = session_transcript(session_id, transcript)
        window_since = resolve_since(repo_path, since) if since else _load_since(sid)
    except UsageUnavailable as exc:
        raise CaptureUnavailable(str(exc)) from exc
    commit = read_commit(repo_path, rev)
    rid = receipt_id_for_commit(sid, commit.sha)
    window = {"since": window_since, "until": commit.committed_at}
    if _already_recorded(repo_path, rid):
        return CommitCaptureReport("already_recorded", rid, commit.sha, sid, window)
    if window_since is not None and window_since > commit.committed_at:
        raise CaptureUnavailable(f"Commit {commit.sha[:7]} is older than the session's last captured commit.")
    facts = read_window(main, repo_path=repo_path, since=window_since, until=commit.committed_at)
    if facts is None:
        raise CaptureUnavailable("The transcript has no activity between the previous captured commit and this one.")
    entry = build_commit_entry(facts, commit, repo_path=repo_path, since=window_since)
    if not write:
        return CommitCaptureReport("dry_run", rid, commit.sha, sid, window, entry)
    from openshard.adapters.claude_code_import import write_import_entry

    write_import_entry(entry, repo_path)
    _save_until(sid, commit.committed_at, commit.sha)
    return CommitCaptureReport("written", rid, commit.sha, sid, window, entry)
