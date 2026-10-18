"""``Openshard-Tokens-*`` commit trailers from a Claude Code session's own transcripts.

Cloud-agent commits get their Receipt from the GitHub workflow after the
push, and that workflow can only price what the commit message carries. This
module reads the committing session's transcripts (the main ``<session>.jsonl``
plus its ``subagents/*.jsonl``, through ``claude_transcript_usage``) and turns
the usage not yet claimed by an earlier commit into trailer lines.

Usage is attributed once. A per-session state file under the OpenShard home
(``usage-trailers/<session>.json``, so every worktree and repository the
session commits from shares it) keeps the message ids already claimed. The
ids of the latest call stay *pending* until ``HEAD`` moves in the repository
they were printed for: a commit that never landed releases them, a landed one
makes them permanent. Without a state the window starts at ``HEAD``'s commit
time (usage before the last commit belongs to it), or at ``--since``.

When usage cannot be stated exactly -- no transcript, no new usage, malformed
or capped counters, more than one model, surcharged requests -- no trailer is
produced (never a zero) and :class:`UsageUnavailable` says why.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from openshard.adapters.claude_transcript_usage import (
    _new_transcript_state,
    _read_transcript_increment,
    _summarise_transcript_state,
    _transcript_roots,
)

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
MAX_SUBAGENT_FILES = 500
TRAILER_KEYS = (
    ("input", "Openshard-Tokens-Input"),
    ("output", "Openshard-Tokens-Output"),
    ("cache_read", "Openshard-Tokens-Cache-Read"),
    ("cache_creation", "Openshard-Tokens-Cache-Creation"),
    ("cache_creation_1h", "Openshard-Tokens-Cache-Creation-1h"),
)
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_TOKEN_FIELDS = ("input", "output", "cache_read", "cache_creation")


class UsageUnavailable(Exception):
    """Usage for this commit cannot be stated exactly; nothing must be printed."""


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_since(repo: Path, value: str) -> str:
    """``--since``: an ISO time, or a commit whose committer time starts the window."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(dt if dt.tzinfo else dt.replace(tzinfo=UTC))
    except ValueError:
        pass
    stamp = _git(repo, "log", "-1", "--format=%cI", value, "--")
    if not stamp:
        raise UsageUnavailable(f"--since {value!r} is neither an ISO time nor a commit in this repository.")
    return _utc(datetime.fromisoformat(stamp))


def session_transcript(session_id: str | None, transcript: Path | None) -> tuple[str, Path]:
    """The session id and its main transcript, from ``--transcript`` or ``$CLAUDE_CODE_SESSION_ID``."""
    if transcript is not None:
        if not transcript.is_file():
            raise UsageUnavailable(f"Transcript {transcript} does not exist.")
        session_id = session_id or transcript.stem
        mains = [transcript]
    else:
        session_id = session_id or os.environ.get(SESSION_ENV, "").strip() or None
        if not session_id:
            raise UsageUnavailable(f"No Claude Code session: ${SESSION_ENV} is not set and no --transcript was given.")
        if not _SESSION_RE.match(session_id):
            raise UsageUnavailable("The Claude Code session id is malformed.")
        mains = sorted({p.resolve() for root in _transcript_roots() for p in root.glob(f"*/{session_id}.jsonl") if p.is_file()})
        if not mains:
            raise UsageUnavailable(f"No transcript for Claude Code session {session_id} under ~/.claude/projects.")
        if len(mains) > 1:
            raise UsageUnavailable(f"Session {session_id} has {len(mains)} transcripts; pass the right one with --transcript.")
    if not _SESSION_RE.match(session_id):
        raise UsageUnavailable("The Claude Code session id is malformed.")
    return session_id, mains[0]


def _files(main: Path, since: str | None) -> list[Path]:
    """*main* plus every subagent transcript written to at or after *since* (older ones cannot hold its usage)."""
    floor = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() if since else None
    sub = main.with_suffix("") / "subagents"
    children = sorted(p for p in sub.glob("*.jsonl") if p.is_file()) if sub.is_dir() else []
    children = [p for p in children if floor is None or p.stat().st_mtime >= floor - 1]
    if len(children) > MAX_SUBAGENT_FILES:
        raise UsageUnavailable(f"More than {MAX_SUBAGENT_FILES} subagent transcripts in the window; usage is not complete.")
    return [main, *children]


def read_usage(main: Path, since: str | None, exclude: set[str]) -> tuple[dict | None, list[str]]:
    """Summed usage of messages at or after *since* not in *exclude*, and the message ids counted.

    Claimed ids seed the reader's de-duplication memory, so a claimed message
    is skipped in the main transcript and in every subagent transcript alike.
    """
    state = _new_transcript_state(since)
    state["closed_ids"] = list(exclude)
    if not _read_transcript_increment(state, _files(main, since)):
        raise UsageUnavailable(f"Transcript {main.name} could not be read.")
    if state.get("invalid_usage") or state.get("capped"):
        raise UsageUnavailable("The transcript's usage is incomplete (malformed or capped counters).")
    ids = [i for i in state["closed_ids"] if i not in exclude]
    ids += [i for slot in state["files"].values() for i in (slot.get("recent") or {}) if i not in exclude]
    return _summarise_transcript_state(state), ids


def trailer_lines(summary: dict | None) -> list[str]:
    """``Openshard-Model`` and ``Openshard-Tokens-*`` lines for one summary, or :class:`UsageUnavailable`."""
    if not summary:
        raise UsageUnavailable("No new usage in this session since the previous commit.")
    if not summary.get("complete"):
        raise UsageUnavailable("The transcript's usage is incomplete (malformed or capped counters).")
    if summary.get("surcharged"):
        raise UsageUnavailable("Some requests were billed above list rates (fast mode, US-only or server tools).")
    models = {m: u for m, u in (summary.get("by_model") or {}).items()
              if any(u.get(f) for f in _TOKEN_FIELDS)}  # zero-usage synthetic records are not a model
    if len(models) != 1:
        raise UsageUnavailable(f"Usage spans {len(models)} models; the trailers can only be priced for one.")
    model, usage = next(iter(models.items()))
    if model == "unknown":
        raise UsageUnavailable("The transcript does not name the model that produced this usage.")
    lines = [f"Openshard-Model: {model}"]
    for field, key in TRAILER_KEYS:
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise UsageUnavailable(f"The {field} counter is not a whole number.")
        if field != "cache_creation_1h" or value:
            lines.append(f"{key}: {value}")
    return lines


def _state_path(session_id: str) -> Path:
    from openshard.util.home import openshard_home

    return Path(openshard_home()) / "usage-trailers" / f"{session_id}.json"


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UsageUnavailable(f"Attribution state {path} is unreadable; usage could be counted twice.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("attributed", []), list):
        raise UsageUnavailable(f"Attribution state {path} is malformed; usage could be counted twice.")
    return data


def _settle_pending(state: dict) -> None:
    """Pending ids become attributed once ``HEAD`` moved in their repository; otherwise they are released."""
    pending = state.pop("pending", None)
    if not isinstance(pending, dict):
        return
    repo = pending.get("repo")
    head_now = _git(Path(repo), "rev-parse", "HEAD") if isinstance(repo, str) and Path(repo).is_dir() else None
    if head_now is not None and head_now == pending.get("head"):
        return  # that commit never landed: its usage is still unclaimed
    state["attributed"] = [*state.get("attributed", []), *(pending.get("ids") or [])]


def usage_trailers(
    repo: Path,
    *,
    session_id: str | None = None,
    transcript: Path | None = None,
    since: str | None = None,
    record: bool = True,
    deliver: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """Trailer lines for the usage this commit has not had claimed by an earlier one.

    *deliver* (when given) receives the lines before they are claimed; if it
    raises, nothing is claimed.
    """
    session_id, main = session_transcript(session_id, transcript)
    toplevel = _git(repo, "rev-parse", "--show-toplevel")
    if toplevel is None:
        raise UsageUnavailable(f"{repo} is not inside a Git repository.")
    head = _git(repo, "rev-parse", "HEAD")
    path = _state_path(session_id)
    state = _load_state(path)
    _settle_pending(state)
    floor = resolve_since(repo, since) if since else state.get("since")
    if floor is None and head is not None:
        floor = resolve_since(repo, head)
    summary, ids = read_usage(main, floor, set(state.get("attributed", [])))
    lines = trailer_lines(summary)
    if deliver is not None:
        deliver(lines)
    if record:
        state.update(version=1, session=session_id, since=state.get("since") or floor,
                     pending={"repo": toplevel, "head": head, "ids": ids})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    return lines


def append_trailers(msg_file: Path, lines: list[str]) -> None:
    """Add *lines* to a commit message file's trailer block (``git interpret-trailers``)."""
    args = ["git", "interpret-trailers", "--in-place", "--if-exists", "replace"]
    for line in lines:
        args += ["--trailer", line]
    try:
        out = subprocess.run([*args, str(msg_file)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageUnavailable(f"Could not add trailers to {msg_file}: {exc}") from exc
    if out.returncode != 0:
        raise UsageUnavailable(f"Could not add trailers to {msg_file}: {out.stderr.strip()}")
