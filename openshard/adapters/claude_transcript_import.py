"""Reconstruct a Receipt from a Claude Code session transcript.

Claude Code writes every session to ``~/.claude/projects/<project>/<session>.jsonl``.
When the capture hooks of a session were lost (the service refused or never
saw them), that transcript is the only evidence left: prompts, every tool call
with its timestamp and working directory, the model and its token usage.
This module turns one transcript into a Receipt for the repository the session
worked in, under these rules:

* **Labelled reconstructed, everywhere.** ``executor`` /
  ``import_method`` / ``capture.source`` name the transcript; every Event
  carries ``evidence = imported_transcript``; ``capture.completeness`` lists
  what a reconstruction cannot know.
* **Nothing is invented.** No git HEAD, diff, commits or verification (the
  working tree has moved on); no cost (the transcript has none); the branch
  the agent reported is kept as a report, never as an observation. Token
  usage is the provider's own counts from the transcript (de-duplicated per
  message, as the hook path counts them).
* **Nothing sensitive is stored.** The first prompt survives only as the
  hook path's scrubbed, bounded excerpt; commands go through the hook path's
  summariser; file paths are relativised to the repository and dropped when
  outside it; tool results and assistant text are never read into the record.
* **Idempotent, and never over a sealed Receipt.** The receipt id is derived
  from the session id, and nothing is written when the repository already
  holds any Receipt for that session (hook-captured or reconstructed).
  Timestamps are the transcript's own.

Writing is opt-in (``--write``); the default is a dry run that prints what
would be recorded. A written Receipt syncs like any other local Receipt.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openshard.history.verification import not_observable_verification

IMPORT_METHOD = "openshard_claude_transcript_v0"
CAPTURE_SOURCE = "claude_code_transcript"
EXECUTOR = "claude_code_transcript_import"
REASON_RECONSTRUCTED = "reconstructed_from_transcript"
REASON_NO_COST = "transcript_has_no_cost"
REASON_NO_USAGE = "transcript_has_no_usage"
REASON_NO_GIT = "git_state_not_observed"
REASON_NO_END = "session_end_not_observed"
_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_COMMAND_TOOLS = frozenset({"Bash"})
_MAX_LINE_BYTES = 4_000_000
_MAX_TOOL_USES = 5_000
_MAX_FILES = 200
_MAX_PATH_CAP = 200
_SUMMARY_CAP = 300


@dataclass
class ToolUse:
    at: str
    tool_use_id: str
    name: str
    cwd: str | None
    action: str  # scrubbed, bounded summary ("Bash: pytest -q", "Write parser.py")
    target: str | None = None  # repository-relative path, when a file tool named one inside the repository
    command_kind: str | None = None  # test | lint | other, for Bash
    failed: bool | None = None  # tool_result.is_error, when a result was seen
    sidechain: bool = False


@dataclass
class TranscriptFacts:
    path: Path
    session_id: str
    sha256: str
    claude_code_version: str | None = None
    started_at: str | None = None
    last_activity_at: str | None = None
    cwds: list[str] = field(default_factory=list)
    branches_reported: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    prompt_count: int = 0
    first_prompt: str | None = None  # raw, scrubbed later; never stored as-is
    turn_count: int = 0
    tool_uses: list[ToolUse] = field(default_factory=list)
    tool_uses_truncated: bool = False
    usage: dict | None = None
    records: int = 0
    unreadable_lines: int = 0


def receipt_id_for_session(session_id: str) -> str:
    """The deterministic receipt id of a reconstruction: the same transcript always yields the same id."""
    digest = hashlib.sha256(f"claude-transcript:{session_id}".encode()).hexdigest()
    return f"rcpt_{digest[:32]}"


def _add_unique(items: list[str], value: object, cap: int = 32) -> None:
    if isinstance(value, str) and value and value not in items and len(items) < cap:
        items.append(value)


def read_transcript(path: Path, *, repo_path: Path) -> TranscriptFacts | None:
    """Facts of one Claude Code transcript, reduced for storage. None when the file is not a transcript. Never raises."""
    from openshard.adapters.claude_hooks import (
        _to_repo_relative,
        read_transcript_usage,
        summarize_command,
    )

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    facts = TranscriptFacts(path=Path(path), session_id="", sha256=hashlib.sha256(raw).hexdigest())
    seen_tool_ids: set[str] = set()
    seen_turns: set[str] = set()
    results: dict[str, bool] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if len(line) > _MAX_LINE_BYTES:
            facts.unreadable_lines += 1
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            facts.unreadable_lines += 1
            continue
        if not isinstance(record, dict):
            facts.unreadable_lines += 1
            continue
        kind = record.get("type")
        if kind not in ("user", "assistant"):
            continue
        facts.records += 1
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid and not facts.session_id:
            facts.session_id = sid
        stamp = record.get("timestamp")
        if isinstance(stamp, str) and stamp:
            if facts.started_at is None or stamp < facts.started_at:
                facts.started_at = stamp
            if facts.last_activity_at is None or stamp > facts.last_activity_at:
                facts.last_activity_at = stamp
        _add_unique(facts.cwds, record.get("cwd"))
        _add_unique(facts.branches_reported, record.get("gitBranch"))
        if facts.claude_code_version is None and isinstance(record.get("version"), str):
            facts.claude_code_version = record["version"][:32]
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        sidechain = bool(record.get("isSidechain"))
        if kind == "user":
            if sidechain or record.get("isMeta"):
                continue
            if isinstance(content, str):
                facts.prompt_count += 1
                if facts.first_prompt is None:
                    facts.first_prompt = content
            elif isinstance(content, list):
                texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                for block in tool_results:
                    tool_use_id = block.get("tool_use_id")
                    if isinstance(tool_use_id, str):
                        results[tool_use_id] = bool(block.get("is_error"))
                if texts and not tool_results:
                    facts.prompt_count += 1
                    if facts.first_prompt is None:
                        facts.first_prompt = str(texts[0].get("text") or "")
            continue
        # assistant
        _add_unique(facts.models, message.get("model"))
        message_id = message.get("id")
        if message.get("stop_reason") == "end_turn" and isinstance(message_id, str) and message_id not in seen_turns:
            seen_turns.add(message_id)
            facts.turn_count += 1
        if not isinstance(content, list):
            continue
        cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else None
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_use_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_use_id, str) or not isinstance(name, str) or tool_use_id in seen_tool_ids:
                continue
            seen_tool_ids.add(tool_use_id)
            if len(facts.tool_uses) >= _MAX_TOOL_USES:
                facts.tool_uses_truncated = True
                continue
            tool_input: dict = block["input"] if isinstance(block.get("input"), dict) else {}
            action, target, command_kind = name[:64], None, None
            if name in _COMMAND_TOOLS:
                command = tool_input.get("command")
                action, _program, command_kind = summarize_command(command if isinstance(command, str) else None, name)
            elif name in _FILE_TOOLS:
                raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
                target = _to_repo_relative(raw_path if isinstance(raw_path, str) else None, repo_path)
                action = f"{name} {target}" if target else f"{name} (path outside the repository)"
            facts.tool_uses.append(ToolUse(
                at=stamp if isinstance(stamp, str) else (facts.last_activity_at or ""),
                tool_use_id=tool_use_id, name=name[:64], cwd=cwd, action=action[:_SUMMARY_CAP],
                target=target, command_kind=command_kind, sidechain=sidechain,
            ))
    for use in facts.tool_uses:
        if use.tool_use_id in results:
            use.failed = results[use.tool_use_id]
    if not facts.session_id or not facts.records:
        return None
    try:
        facts.usage = read_transcript_usage(Path(path))
    except Exception:
        facts.usage = None
    return facts


def _checkout_names(cwds: list[str]) -> list[str]:
    """Basenames of the directories the session worked in, never their paths."""
    names: list[str] = []
    for raw in cwds:
        name = Path(raw).name
        if name and name not in names:
            names.append(name[:_MAX_PATH_CAP])
    return names


def _files_detail(facts: TranscriptFacts) -> list[dict]:
    files: list[dict] = []
    seen: set[str] = set()
    for use in facts.tool_uses:
        if use.target and use.target not in seen:
            seen.add(use.target)
            files.append({
                "path": use.target,
                "change_type": "update",
                "summary": f"written by the agent ({use.name}); create vs update not distinguishable from the transcript",
                "attribution": "agent_reported",
            })
            if len(files) >= _MAX_FILES:
                break
    return files


def _events(entry: dict, facts: TranscriptFacts) -> list[dict]:
    from openshard.history.event import (
        EVENT_SESSION_ACTIVITY,
        EVENT_SESSION_STARTED,
        EVENT_TOOL_INVOKED,
        EVIDENCE_IMPORTED_TRANSCRIPT,
        STATUS_FAILED,
        STATUS_STARTED,
        STATUS_UNKNOWN,
        make_event,
    )

    common = {
        "run_id": entry.get("run_id"), "shard_id": entry.get("shard_id"),
        "attempt_number": entry.get("attempt_number"), "actor": "claude_code",
        "evidence": EVIDENCE_IMPORTED_TRANSCRIPT, "source": CAPTURE_SOURCE,
    }
    events = [make_event(
        event_type=EVENT_SESSION_STARTED, action="Claude Code session reconstructed from its transcript",
        status=STATUS_STARTED, occurred_at=facts.started_at, metadata={"transcript_sha256": facts.sha256}, **common,
    )]
    if facts.prompt_count:
        events.append(make_event(
            event_type=EVENT_SESSION_ACTIVITY, action=f"{facts.prompt_count} prompt(s) submitted",
            status=STATUS_UNKNOWN, occurred_at=facts.started_at, metadata={"prompt_count": facts.prompt_count}, **common,
        ))
    for use in facts.tool_uses:
        metadata: dict[str, Any] = {"tool": use.name, "tool_use_id": use.tool_use_id}
        if use.command_kind:
            metadata["command_kind"] = use.command_kind
        if use.sidechain:
            metadata["subagent"] = True
        if use.cwd:
            metadata["checkout"] = Path(use.cwd).name[:_MAX_PATH_CAP]
        events.append(make_event(
            event_type=EVENT_TOOL_INVOKED, action=use.action, target=use.target, target_is_path=use.target is not None,
            status=STATUS_FAILED if use.failed else STATUS_UNKNOWN, occurred_at=use.at or None, metadata=metadata, **common,
        ))
    return [e.to_dict() for e in events]


def build_transcript_import_entry(facts: TranscriptFacts, *, repo_path: Path, run_index: int | None = None) -> dict:
    """A coerced, content-hash-stamped Receipt entry reconstructed from *facts*. Never reads the transcript again."""
    from openshard.adapters.claude_code_import import _sanitize_task
    from openshard.history.receipt_identity import RECEIPT_ID_FIELD
    from openshard.history.shard_contract import _make_shard_id
    from openshard.history.shard_schema import SHARD_SCHEMA_VERSION, coerce_shard_entry
    from openshard.history.task_title import derive_task_title

    task = _sanitize_task(facts.first_prompt or "", placeholder="Claude Code session (transcript reconstruction)")
    started = facts.started_at or ""
    files = _files_detail(facts)
    failures = sum(1 for use in facts.tool_uses if use.failed)
    reasons = [REASON_RECONSTRUCTED, REASON_NO_GIT, REASON_NO_END, REASON_NO_COST]
    if facts.usage is None:
        reasons.append(REASON_NO_USAGE)
    if facts.tool_uses_truncated:
        reasons.append("tool_calls_truncated")
    entry: dict = {
        "schema_version": SHARD_SCHEMA_VERSION,
        "timestamp": started,
        "task": task,
        "task_title": derive_task_title(task),
        "execution_model": facts.models[0] if facts.models else "unknown",
        "executor": EXECUTOR,
        "import_source": "claude_code",
        "import_method": IMPORT_METHOD,
        "import_note": (
            "Reconstructed from the Claude Code transcript after its hook events were lost. "
            "Tool calls, prompts and token usage are the transcript's; git state, cost and "
            "verification were not observed."
        ),
        "files_source": "transcript_reported",
        "verification_attempted": False,
        "verification_passed": None,
        "verification": not_observable_verification(),
        "files_created": 0,
        "files_updated": len(files),
        "files_deleted": 0,
        "files_detail": files,
        "git_branch": None,
        "git_head_commit_hash": None,
        "summary": (
            f"Claude Code session reconstructed from transcript: {len(facts.tool_uses)} tool call(s), "
            f"{facts.prompt_count} prompt(s), {facts.turn_count} completed turn(s)."
        )[:_SUMMARY_CAP],
        "run_id": f"{started}-{facts.session_id[:8]}",
        "shard_id": _make_shard_id(started, run_index),
        "attempt_number": 1,
        RECEIPT_ID_FIELD: receipt_id_for_session(facts.session_id),
        "capture": {
            "source": CAPTURE_SOURCE,
            "agent": "claude_code",
            "agent_vendor": "Anthropic",
            "session_id": facts.session_id,
            "status": "reconstructed",
            "session_end_observed": False,
            "session_end_reason": None,
            "start_source": "transcript",
            "started_at": facts.started_at,
            "last_activity_at": facts.last_activity_at,
            "prompt_count": facts.prompt_count,
            "turn_count": facts.turn_count,
            "tool_call_count": len(facts.tool_uses),
            "tool_failure_count": failures,
            "models_seen": list(facts.models),
            "model_source": "transcript" if facts.models else None,
            "completeness": {"status": "partial", "reasons": reasons},
            "cost_not_recorded_reason": REASON_NO_COST,
            "reconstructed": {
                "from": CAPTURE_SOURCE,
                "transcript_sha256": facts.sha256,
                "claude_code_version": facts.claude_code_version,
                "checkouts": _checkout_names(facts.cwds),
                "branches_reported": list(facts.branches_reported),
                "records": facts.records,
                "unreadable_lines": facts.unreadable_lines,
            },
        },
    }
    usage = facts.usage
    totals = (usage or {}).get("totals") if isinstance(usage, dict) else None
    if isinstance(totals, dict) and sum(int(v) for v in totals.values()) > 0:
        entry["prompt_tokens"] = int(totals.get("input", 0))
        entry["completion_tokens"] = int(totals.get("output", 0))
        entry["cache_creation_tokens"] = int(totals.get("cache_creation", 0))
        entry["cache_read_tokens"] = int(totals.get("cache_read", 0))
        entry["total_tokens"] = entry["prompt_tokens"] + entry["completion_tokens"]
        entry["tokens_provenance"] = "provider_reported"
        if usage is not None and not usage.get("complete", True):
            entry["capture"]["completeness"]["reasons"].append("transcript_usage_incomplete")
    else:
        entry["capture"]["tokens_not_recorded_reason"] = REASON_NO_USAGE
    try:
        from openshard.history.repo_identity import REPO_IDENTITY_FIELD, capture_repo_identity

        identity = capture_repo_identity(repo_path)
        if identity:
            entry[REPO_IDENTITY_FIELD] = identity
    except Exception:
        pass
    entry["events"] = _events(entry, facts)
    try:
        from openshard.config.settings import stamp_owner

        stamp_owner(entry, repo_path)
    except Exception:
        pass
    return coerce_shard_entry(entry)


def existing_receipts_for_session(repo_path: Path, session_id: str) -> list[str]:
    """Receipt ids already recorded in *repo_path* for *session_id* (hook-captured or reconstructed). Never raises."""
    found: list[str] = []
    path = Path(repo_path) / ".openshard" / "runs.jsonl"
    try:
        if not path.exists():
            return found
        wanted = receipt_id_for_session(session_id)
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if session_id not in line and wanted not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                capture: dict = entry["capture"] if isinstance(entry.get("capture"), dict) else {}
                rid = entry.get("receipt_id")
                if capture.get("session_id") == session_id or rid == wanted:
                    found.append(str(rid or "<no receipt id>"))
    except OSError:
        return found
    return found


@dataclass
class ImportReport:
    status: str  # dry_run | written | already_recorded | not_a_transcript
    receipt_id: str | None = None
    session_id: str | None = None
    existing: list[str] = field(default_factory=list)
    entry: dict | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"status": self.status, "receipt_id": self.receipt_id, "session_id": self.session_id,
                               "existing": list(self.existing)}
        if self.entry is not None:
            out["entry"] = self.entry
        return out


def import_transcript(path: Path, *, repo_path: Path, write: bool = False) -> ImportReport:
    """Reconstruct the Receipt of *path* for *repo_path*; write it only with *write*. Never raises."""
    facts = read_transcript(Path(path), repo_path=Path(repo_path))
    if facts is None:
        return ImportReport(status="not_a_transcript")
    existing = existing_receipts_for_session(Path(repo_path), facts.session_id)
    receipt_id = receipt_id_for_session(facts.session_id)
    if existing:
        return ImportReport(status="already_recorded", receipt_id=receipt_id, session_id=facts.session_id, existing=existing)
    runs_path = Path(repo_path) / ".openshard" / "runs.jsonl"
    try:
        run_index = sum(1 for _ in runs_path.open(encoding="utf-8")) if runs_path.exists() else 0
    except OSError:
        run_index = None
    entry = build_transcript_import_entry(facts, repo_path=Path(repo_path), run_index=run_index)
    if not write:
        return ImportReport(status="dry_run", receipt_id=receipt_id, session_id=facts.session_id, entry=entry)
    from openshard.adapters.claude_code_import import write_import_entry

    write_import_entry(entry, Path(repo_path))
    return ImportReport(status="written", receipt_id=receipt_id, session_id=facts.session_id, entry=entry)
