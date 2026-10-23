"""``Openshard-Tokens-*`` commit trailers from the committing agent's own Claude Code transcript.

Cloud-agent commits get their Receipt from the GitHub workflow after the
push, and that workflow can only price what the commit message carries. This
module turns the usage of the agent that is committing, not yet claimed by
an earlier commit, into trailer lines.

Which agent: a Claude Code session keeps the main thread in
``<session>.jsonl`` and each subagent in ``<session>/subagents/agent-<id>.jsonl``,
and exposes no agent id to the commands it runs (``$CLAUDE_CODE_SESSION_ID``
is the session's, in subagents too). But each transcript records the Bash
call its agent is waiting on before the command runs, so the caller is the
one transcript whose open Bash call is this command (``usage trailers``, or
``git commit`` for a hook). Several such transcripts (parallel agents
committing at the same moment) are narrowed by the repository and message
file paths named in the command. Only that transcript is counted: other
agents' work is theirs to claim.

Usage is attributed once. A locked state file per session under the
OpenShard home (``usage-trailers/<session>.json``, shared by every agent,
worktree and repository) keeps the message ids already claimed. The ids of the latest call stay *pending* until ``HEAD`` moves in
the repository they were printed for: a commit that never landed releases
them, a landed one makes them permanent.

The window is the claim state, not ``HEAD``: an identified caller gets every
message of its own transcript that no commit has claimed (a subagent's
transcript starts when the agent does), so a rebase or merge that moves
``HEAD`` to a newer commit cannot shrink the count. The session window starts
at the session's latest landed claim (the whole session before the first).
``--since`` overrides either.

When the caller cannot be told apart, every transcript of the session is
counted instead and labelled ``Openshard-Usage-Scope: session-window`` (usage
since the session's last claimed commit, which may include parallel agents' work); an exact
caller is ``agent``. Claims are session-wide, so the two never count one
message twice. When no true number exists -- no new usage, malformed or
capped counters, several models, surcharged requests -- no trailer is
produced (never a zero) and :class:`UsageUnavailable` says why.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from openshard.adapters.claude_transcript_usage import (
    _new_transcript_state,
    _read_transcript_increment,
    _summarise_transcript_state,
    _transcript_roots,
)
from openshard.util.paths import is_absolute, path_key

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
_TAIL_BYTES = 512 * 1024  # the open Bash call is among the last records
_CALL_MARKERS = ("usage trailers", "git commit")
_WORD_RE = re.compile(r"[^\s\"';&|<>()]+")  # a command's words, unquoted
SCOPE_KEY = "Openshard-Usage-Scope"
SCOPE_AGENT = "agent"  # the committing agent's own transcript
SCOPE_SESSION = "session-window"  # every transcript of the session: may include parallel agents' work
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


def _now() -> datetime:
    return datetime.now(UTC)


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


def session_transcript(session_id: str | None, transcript: Path | None) -> tuple[str, Path, bool]:
    """The session id, a transcript, and whether it was named explicitly (``--transcript``).

    Without ``--transcript`` the transcript is the session's main file, found
    from ``$CLAUDE_CODE_SESSION_ID``; :func:`caller_transcript` then picks
    the committing agent's file among it and its subagents.
    """
    if transcript is not None:
        if not transcript.is_file():
            raise UsageUnavailable(f"Transcript {transcript} does not exist.")
        if not session_id:
            parent = transcript.resolve().parent
            session_id = parent.parent.name if parent.name == "subagents" else transcript.stem
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
    return session_id, mains[0], transcript is not None


def open_bash_commands(file: Path) -> list[str]:
    """Bash commands *file*'s agent has called and not yet had a result for (read from its tail)."""
    try:
        with file.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > _TAIL_BYTES:
        lines = lines[1:]  # the first line may be cut
    open_calls: dict[str, str] = {}
    for raw in lines:
        if b"tool_use" not in raw and b"tool_result" not in raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash" and isinstance(block.get("id"), str):
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str):
                    open_calls[block["id"]] = command
            elif block.get("type") == "tool_result":
                open_calls.pop(str(block.get("tool_use_id")), None)
    return list(open_calls.values())


def _session_files(main: Path) -> list[Path]:
    sub = main.with_suffix("") / "subagents"
    return [main, *sorted(p for p in sub.glob("*.jsonl") if p.is_file())] if sub.is_dir() else [main]


def caller_transcript(main: Path, hints: tuple[str, ...] = ()) -> Path | None:
    """The transcript (main thread or one subagent) of the agent running this command, or None.

    The candidate is a transcript whose open Bash call runs this command or
    ``git commit``. Several are narrowed to those whose call names every
    *hint* (the repository and message-file paths). Anything but exactly one
    is None: the caller cannot be told apart, never guessed.
    """
    found = [(f, cmds) for f in _session_files(main)
             if (cmds := [c for c in open_bash_commands(f) if any(m in c for m in _CALL_MARKERS)])]
    if len(found) > 1 and hints:
        found = [(f, cmds) for f, cmds in found if any(all(_names(c, h) for h in hints) for c in cmds)]
    return found[0][0] if len(found) == 1 else None


def _names(command: str, path: str) -> bool:
    """Whether *command* names *path*, however it is spelled.

    Git prints ``C:/...`` where the agent's shell wrote ``C:\\...`` (or a short
    name, or another case), so a word of the command counts when it is the
    same location (``path_key``), not only the same text. Only absolute words
    count: a relative one would be resolved against this process's directory,
    not the other agent's.
    """
    if path in command:
        return True
    want = path_key(path)
    return any(is_absolute(word) and path_key(word) == want for word in _WORD_RE.findall(command))


def read_usage(files: list[Path], since: str | None, exclude: set[str]) -> tuple[dict | None, list[str]]:
    """Summed usage of messages at or after *since* not in *exclude*, and the message ids counted.

    Claimed ids seed the reader's de-duplication memory, so a claimed
    message is skipped; a message in several files counts once.
    """
    state = _new_transcript_state(since)
    state["closed_ids"] = list(exclude)
    if not _read_transcript_increment(state, files):
        raise UsageUnavailable(f"Transcript {files[0].name} could not be read.")
    if state.get("invalid_usage") or state.get("capped"):
        raise UsageUnavailable("The transcript's usage is incomplete (malformed or capped counters).")
    ids = [i for i in state["closed_ids"] if i not in exclude]
    ids += [i for slot in state["files"].values() for i in (slot.get("recent") or {}) if i not in exclude]
    return _summarise_transcript_state(state), ids


def trailer_lines(summary: dict | None, scope: str = SCOPE_AGENT) -> list[str]:
    """``Openshard-Model``, ``Openshard-Usage-Scope`` and ``Openshard-Tokens-*`` lines, or :class:`UsageUnavailable`.

    Refused only when no number would be true: no usage, incomplete
    counters, or usage the workflow would price wrongly (several models, an
    unnamed model, requests billed above list rates).
    """
    if not summary:
        raise UsageUnavailable("No new usage since the previous commit.")
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
    lines = [f"Openshard-Model: {model}", f"{SCOPE_KEY}: {scope}"]
    for field, key in TRAILER_KEYS:
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise UsageUnavailable(f"The {field} counter is not a whole number.")
        if field != "cache_creation_1h" or value:
            lines.append(f"{key}: {value}")
    return lines


def _short(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}k" if n >= 1000 else str(n)


def trailer_summary(lines: list[str]) -> str:
    """One line for the terminal: scope, total tokens and the list-rate estimate when one exists."""
    from openshard.models.pricing import estimate_usage_cost

    values = dict(line.split(": ", 1) for line in lines)
    counts = {field: int(values.get(key, "0")) for field, key in TRAILER_KEYS}
    total = counts["input"] + counts["output"] + counts["cache_read"] + counts["cache_creation"]
    cost = estimate_usage_cost(values.get("Openshard-Model"), input_tokens=counts["input"],
                               output_tokens=counts["output"], cache_read_tokens=counts["cache_read"],
                               cache_write_tokens=counts["cache_creation"] - counts["cache_creation_1h"],
                               cache_write_1h_tokens=counts["cache_creation_1h"])
    price = f"~${cost.usd:.{2 if cost.usd >= 0.01 else 4}f} list-rate est." if cost is not None else "cost unknown"
    return f"scope {values.get(SCOPE_KEY)}, {_short(total)} tokens, {price}"


def _state_path(session_id: str) -> Path:
    from openshard.util.home import openshard_home

    return Path(openshard_home()) / "usage-trailers" / f"{session_id}.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise parallel agents of one session on its state file (no lock where ``fcntl`` is missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as fh:
        try:
            import fcntl
        except ImportError:  # Windows: cloud sessions run on Linux
            yield
            return
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UsageUnavailable(f"Attribution state {path} is unreadable; usage could be counted twice.") from exc
    if not (isinstance(data, dict) and isinstance(data.get("attributed", []), list)
            and isinstance(data.get("pending", {}), dict) and isinstance(data.get("since", {}), dict)
            and isinstance(data.get("session_since", ""), str)):
        raise UsageUnavailable(f"Attribution state {path} is malformed; usage could be counted twice.")
    return data


def _settle_pending(state: dict, own: str) -> None:
    """A claim whose ``HEAD`` moved is permanent. One that did not is released when it is this caller's
    (it is asking again) and kept otherwise: another agent may be about to commit it."""
    for key, claim in list(state.get("pending", {}).items()):
        repo = claim.get("repo") if isinstance(claim, dict) else None
        head_now = _git(Path(repo), "rev-parse", "HEAD") if isinstance(repo, str) and Path(repo).is_dir() else None
        if head_now is not None and head_now == claim.get("head"):
            if key == own:
                del state["pending"][key]
            continue
        state["attributed"] = [*state.get("attributed", []), *((claim or {}).get("ids") or [])]
        at = claim.get("at") if isinstance(claim, dict) else None
        if isinstance(at, str) and at > state.get("session_since", ""):
            state["session_since"] = at  # the session window restarts after its latest landed claim
        del state["pending"][key]


def usage_trailers(
    repo: Path,
    *,
    session_id: str | None = None,
    transcript: Path | None = None,
    since: str | None = None,
    record: bool = True,
    hints: tuple[str, ...] = (),
    deliver: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """Trailer lines for the usage this commit has not had claimed by an earlier one.

    The committing agent's own transcript when it can be identified
    (``Openshard-Usage-Scope: agent``: all of its unclaimed messages);
    otherwise every transcript of the session (``session-window``: unclaimed
    usage since the session's last landed claim, which may include parallel
    agents' work). *deliver* (when given) receives the lines
    before they are claimed; if it raises, nothing is claimed.
    """
    session_id, main, explicit = session_transcript(session_id, transcript)
    toplevel = _git(repo, "rev-parse", "--show-toplevel")
    if toplevel is None:
        raise UsageUnavailable(f"{repo} is not inside a Git repository.")
    caller = main if explicit else caller_transcript(main, (toplevel, *hints))
    scope, files, key = ((SCOPE_AGENT, [caller], caller.stem) if caller is not None
                         else (SCOPE_SESSION, _session_files(main), "session"))
    head = _git(repo, "rev-parse", "HEAD")
    path = _state_path(session_id)
    with _locked(path):
        state = _load_state(path)
        _settle_pending(state, key)
        # Not HEAD's time: after a rebase or merge HEAD is someone else's newer commit.
        floor = (resolve_since(repo, since) if since
                 else (state.get("session_since") or None) if scope == SCOPE_SESSION else None)
        claimed = {*state.get("attributed", []),
                   *(i for c in state.get("pending", {}).values() for i in (c.get("ids") or []))}
        summary, ids = read_usage(files, floor, claimed)
        lines = trailer_lines(summary, scope)
        if deliver is not None:
            deliver(lines)
        if record:
            state.setdefault("pending", {})[key] = {"repo": toplevel, "head": head, "ids": ids,
                                                    "at": _utc(_now())}
            state.update(version=2, session=session_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, path)
    return lines


def _drop_own_trailers(msg_file: Path, keys: set[str]) -> None:
    """Remove trailer lines whose key is exactly one of *keys* from the message's last paragraph.

    ``git interpret-trailers --if-exists replace`` matches keys by prefix, so
    writing ``Openshard-Tokens-Cache-Creation-1h`` would also drop
    ``Openshard-Tokens-Cache-Creation``. Exact keys are removed here and the
    new lines are then added.
    """
    text = msg_file.read_text(encoding="utf-8")
    body, sep, block = text.rstrip("\n").rpartition("\n\n")
    if not sep:
        return
    kept = [ln for ln in block.split("\n") if ln.split(":", 1)[0].strip().lower() not in keys]
    msg_file.write_text(body + ("\n\n" + "\n".join(kept) if kept else "") + "\n", encoding="utf-8")


def append_trailers(msg_file: Path, lines: list[str]) -> None:
    """Add *lines* to a commit message file's trailer block (``git interpret-trailers``),
    replacing any trailer with exactly the same key."""
    try:
        _drop_own_trailers(msg_file, {line.split(":", 1)[0].strip().lower() for line in lines})
    except OSError as exc:
        raise UsageUnavailable(f"Could not add trailers to {msg_file}: {exc}") from exc
    args = ["git", "interpret-trailers", "--in-place", "--if-exists", "add"]
    for line in lines:
        args += ["--trailer", line]
    try:
        out = subprocess.run([*args, str(msg_file)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageUnavailable(f"Could not add trailers to {msg_file}: {exc}") from exc
    if out.returncode != 0:
        raise UsageUnavailable(f"Could not add trailers to {msg_file}: {out.stderr.strip()}")
