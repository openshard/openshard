"""``Openshard-Model`` / ``Openshard-Tokens-*`` commit trailers from the committing Codex thread.

A Codex commit nobody captured locally gets its Receipt from the GitHub
workflow, which can only price what the commit message carries. Codex gives
the commands it runs its thread id (``$CODEX_THREAD_ID``) and keeps a
cumulative ``token_count`` in that thread's rollout
(``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl``), read by
:func:`~openshard.adapters.codex_transcript.read_codex_transcript_usage`.

Usage is attributed once: a locked state file per thread under the Openshard
home (``usage-trailers/codex-<thread>.json``) keeps the cumulative total the
thread's landed commits have claimed. A commit gets the difference. The
latest claim stays *pending* until ``HEAD`` moves in its repository, so a
commit that never landed releases it.

Truth rules: one thread is one agent (``Openshard-Usage-Scope: agent``);
Codex's counter has no cache-write field, so no ``Cache-Creation`` trailer is
written (not reported, never zero). No rollout, a rollout that does not prove
the thread id, malformed counters, a thread served by several models, or no
new usage: no trailer, and :class:`UsageUnavailable` says why.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from openshard.adapters.codex_transcript import _codex_home, read_codex_transcript_usage
from openshard.adapters.commit_usage_trailers import (
    SCOPE_AGENT,
    SCOPE_KEY,
    UsageUnavailable,
    _git,
    _locked,
    _now,
    _utc,
)

THREAD_ENV = "CODEX_THREAD_ID"
_THREAD_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# (counter field, trailer key). No cache-write: Codex does not report one.
TRAILER_KEYS = (
    ("input", "Openshard-Tokens-Input"),
    ("output", "Openshard-Tokens-Output"),
    ("cache_read", "Openshard-Tokens-Cache-Read"),
)
_FIELDS = tuple(field for field, _ in TRAILER_KEYS)


def thread_rollout(thread_id: str, env: Mapping[str, str] | None = None) -> Path:
    """The one rollout file of *thread_id* under ``$CODEX_HOME/sessions``, or :class:`UsageUnavailable`."""
    if not _THREAD_RE.match(thread_id):
        raise UsageUnavailable("The Codex thread id is malformed.")
    root = _codex_home(env) / "sessions"
    found = sorted({p.resolve() for p in root.glob(f"*/*/*/rollout-*-{thread_id}.jsonl") if p.is_file()})
    if not found:
        raise UsageUnavailable(f"No rollout for Codex thread {thread_id} under {root}.")
    if len(found) > 1:
        raise UsageUnavailable(f"Codex thread {thread_id} has {len(found)} rollouts; usage could be counted twice.")
    return found[0]


def _totals(path: Path, thread_id: str) -> tuple[str, dict[str, int]]:
    """The thread's single model and its cumulative counters, or :class:`UsageUnavailable`."""
    usage = read_codex_transcript_usage(path, thread_id)
    if usage is None:
        raise UsageUnavailable(f"The rollout {path.name} has no readable token counter for this thread.")
    if not usage.get("complete"):
        raise UsageUnavailable("The rollout's token counter is malformed.")
    (model, row), = usage["by_model"].items()
    if model == "unknown":
        raise UsageUnavailable("The thread does not name one model; the trailers can only be priced for one.")
    return model, {field: int(row[field]) for field in _FIELDS}


def _state_path(thread_id: str) -> Path:
    from openshard.util.home import openshard_home

    return Path(openshard_home()) / "usage-trailers" / f"codex-{thread_id}.json"


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UsageUnavailable(f"Attribution state {path} is unreadable; usage could be counted twice.") from exc
    if not (isinstance(data, dict) and isinstance(data.get("landed", {}), dict)
            and isinstance(data.get("pending") or {}, dict)):
        raise UsageUnavailable(f"Attribution state {path} is malformed; usage could be counted twice.")
    return data


def _settle_pending(state: dict) -> None:
    """A pending claim whose ``HEAD`` moved landed; one whose ``HEAD`` did not is released (asked again)."""
    claim = state.pop("pending", None)
    if not isinstance(claim, dict):
        return
    repo = claim.get("repo")
    head_now = _git(Path(repo), "rev-parse", "HEAD") if isinstance(repo, str) and Path(repo).is_dir() else None
    if head_now is not None and head_now == claim.get("head"):
        return
    if isinstance(claim.get("totals"), dict):
        state["landed"] = claim["totals"]


def codex_usage_trailers(
    repo: Path,
    thread_id: str,
    *,
    env: Mapping[str, str] | None = None,
    deliver: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """Trailer lines for the thread's usage no earlier commit claimed, or :class:`UsageUnavailable`.

    *deliver* (when given) receives the lines before they are claimed; if it
    raises, nothing is claimed.
    """
    toplevel = _git(repo, "rev-parse", "--show-toplevel")
    if toplevel is None:
        raise UsageUnavailable(f"{repo} is not inside a Git repository.")
    rollout = thread_rollout(thread_id, env)
    path = _state_path(thread_id)
    with _locked(path):
        state = _load_state(path)
        _settle_pending(state)
        model, totals = _totals(rollout, thread_id)
        landed = state.get("landed") or {}
        delta = {}
        for field in _FIELDS:
            before = landed.get(field, 0)
            if not isinstance(before, int) or isinstance(before, bool) or before > totals[field]:
                raise UsageUnavailable("The thread's counter went backwards; usage could be counted twice.")
            delta[field] = totals[field] - before
        if not any(delta.values()):
            raise UsageUnavailable("No new usage since the previous commit.")
        lines = [f"Openshard-Model: {model}", f"{SCOPE_KEY}: {SCOPE_AGENT}",
                 *(f"{key}: {delta[field]}" for field, key in TRAILER_KEYS)]
        if deliver is not None:
            deliver(lines)
        state["pending"] = {"repo": toplevel, "head": _git(repo, "rev-parse", "HEAD"), "totals": totals,
                            "at": _utc(_now())}
        state.update(version=1, thread=thread_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    return lines
