"""A session's outcome in git: the commits it created and the HEAD it ended on.

Pure helpers for ``claude_hooks``: reading which commits a checkout's reflog
says were *created* (``_reflog_created``), the session's own result commit
when a later agent committed on top (``_own_result_commit``), and carrying a
computed outcome across finalisations (``_merge_git_outcome``,
``_stamp_git_outcome``, ``_stored_git_outcome``). The outcome itself is
computed in ``claude_hooks._session_git_outcome``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openshard.util.git import run_git

# The same value as ``claude_hooks.ATTR_GIT_OBSERVED`` (kept here to avoid an import cycle).
ATTR_GIT_OBSERVED = "git_observed"


_MAX_SESSION_COMMITS = 50


_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _sha_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _SHA_RE.match(value) else None


# Reflog subjects of entries that *create* a new commit in this checkout:
# commit / amend / merge commit, cherry-pick, revert, ``git am``, and a true
# (non-fast-forward) merge or pull ("... Merge made by the 'ort' strategy.").
# A fast-forward ("pull: Fast-forward", "merge x: Fast-forward"), checkout
# or reset only moves HEAD onto existing commits and never qualifies.
# Rebase picks ("rebase (pick)", "pull --rebase (pick)") are excluded too:
# they rewrite commits that may have been made before the session, so they
# are under-counted rather than claimed.
_REFLOG_CREATED_RE = re.compile(
    r"^(?:commit(?: \((?:amend|merge|initial)\))?|cherry-pick|revert|am):"
    r"|^(?:merge|pull)\b[^:]*: Merge made by "
)


def _reflog_created(repo_root: Path) -> dict[str, int] | None:
    """``{sha: reflog unix time}`` for HEAD reflog entries that created a commit, or None. Never raises."""
    out = run_git(repo_root, ["reflog", "show", "--date=unix", "--format=%H%x09%gd%x09%gs", "-n", "500", "HEAD"])
    if not out or not out.strip():
        # No reflog (core.logAllRefUpdates=false, a fresh clone, an error):
        # which commits the session created is unknown, never "none".
        return None
    created: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha = _sha_or_none(parts[0])
        stamp = re.search(r"@\{(\d+)\}$", parts[1].strip())
        if sha and stamp and _REFLOG_CREATED_RE.match(parts[2]):
            created.setdefault(sha, int(stamp.group(1)))  # newest entry first
    return created


def _own_result_commit(repo_root: Path, commits: list[str], end: str) -> str | None:
    """The session's last own commit when another session committed on top of it, else None.

    When the end HEAD is one of the session's own commits it is the result
    (``commit_value``). When it is not -- a delegated agent committed after
    this session in the same checkout -- the result is the session's newest
    own commit (``commits`` is topologically ordered, newest first), but only
    when git confirms it is an ancestor of the end HEAD. Unknown stays None.
    """
    if not commits or end in commits:
        return None
    last = commits[0]
    if run_git(repo_root, ["merge-base", "--is-ancestor", last, end]) is None:
        return None
    return last


def _merge_git_outcome(fresh: dict | None, stored: object) -> dict | None:
    """*fresh* with what an earlier outcome for this session already established.

    A failed git read (*fresh* None -- e.g. Claude Code tearing the session
    down at SessionEnd) keeps the stored outcome instead of dropping it; an
    unknown commit set at the same end HEAD keeps the stored one; a pull
    request found earlier for a commit still in the session's set is kept
    when this lookup found none (``gh`` failing, offline). Never invents a
    commit or a PR the earlier, attributed outcome did not hold.
    """
    prior = stored if isinstance(stored, dict) and _sha_or_none(stored.get("end_head")) else None
    if fresh is None:
        return dict(prior) if prior is not None else None
    if prior is None:
        return fresh
    merged = dict(fresh)
    if merged.get("commits") is None and isinstance(prior.get("commits"), list) and (
        prior.get("end_head") == merged.get("end_head")
    ):
        merged["commits"] = list(prior["commits"])
        merged["truncated"] = bool(prior.get("truncated"))
        if prior.get("result") in merged["commits"]:
            merged["result"] = prior["result"]
    prior_pr = prior.get("pull_request")
    if (
        "pull_request" not in merged and isinstance(prior_pr, dict) and isinstance(prior_pr.get("url"), str)
        and prior_pr.get("head") in (merged.get("commits") or [])
    ):
        merged["pull_request"] = dict(prior_pr)
    return merged


def _stamp_git_outcome(entry: dict, buf: dict) -> None:
    """Persist the finalisation-time git outcome (``_session_git_outcome``) on *entry*."""
    outcome = buf.get("git_outcome")
    if not isinstance(outcome, dict) or _sha_or_none(outcome.get("end_head")) is None:
        return
    entry["git_end_head"] = outcome["end_head"]
    commits = outcome.get("commits")
    if isinstance(commits, list):
        entry["session_commits"] = {
            "source": ATTR_GIT_OBSERVED,
            "shas": [c for c in commits if _sha_or_none(c)][:_MAX_SESSION_COMMITS],
            "truncated": bool(outcome.get("truncated")),
        }
        if outcome.get("result") in entry["session_commits"]["shas"]:
            # The session's own last commit, an ancestor of a later agent's end HEAD.
            entry["session_commits"]["result"] = outcome["result"]
    pr = outcome.get("pull_request")
    if isinstance(pr, dict) and isinstance(pr.get("url"), str):
        entry["pull_request"] = {k: pr.get(k) for k in ("url", "number", "head", "source")}


def _stored_git_outcome(entry: dict) -> dict | None:
    """The git outcome a persisted record carries (rebuilt buffers keep it until re-finalised)."""
    end = _sha_or_none(entry.get("git_end_head"))
    if end is None:
        return None
    outcome: dict[str, Any] = {"source": ATTR_GIT_OBSERVED, "end_head": end, "commits": None, "truncated": False}
    block = entry.get("session_commits")
    if isinstance(block, dict) and isinstance(block.get("shas"), list):
        outcome["commits"] = [c for c in block["shas"] if _sha_or_none(c)]
        outcome["truncated"] = bool(block.get("truncated"))
        if block.get("result") in outcome["commits"]:
            outcome["result"] = block["result"]
    pr = entry.get("pull_request")
    if isinstance(pr, dict) and isinstance(pr.get("url"), str):
        outcome["pull_request"] = dict(pr)
    return outcome
