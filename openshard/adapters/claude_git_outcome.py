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

from openshard.adapters.claude_transcript_usage import _parse_utc
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


_REFLOG_SWITCH_RE = re.compile(r"^checkout: moving from (\S+) to (\S+)$")

# Why a Receipt's changed files are not taken from git (``capture.git_files_unknown_reason``).
FILES_BRANCH_SWITCH_UNKNOWN = "branch_switch_base_unknown"


def _files_base_after_switch(
    repo_root: Path, start: str | None, start_branch: str | None, window: tuple[int, int] | None,
) -> tuple[str | None, bool]:
    """``(base, switched)``: the commit the session's file diff starts from. Never raises.

    The start HEAD is the base unless the session ended on a branch it
    switched to. Then the base is HEAD when the reflog first records the
    move onto that branch inside the session *window* (unix seconds), so
    files that differ only between the two branches are never counted as
    this session's work. A switch git cannot place (no reflog, a detached
    HEAD, no move onto the final branch in the window) gives ``(None, True)``:
    the git-observed file set is unknown, never a guess.
    """
    current = (run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]) or "").strip() or None
    out = None
    if window is not None:
        out = run_git(repo_root, ["reflog", "show", "--date=unix", "--format=%H%x09%gd%x09%gs", "-n", "500", "HEAD"])
    if not out or not out.strip():
        # Reflog unknown: only a branch name that visibly changed is a switch.
        if current and start_branch and current != start_branch:
            return None, True
        return start, False
    lo, hi = window if window is not None else (0, 0)
    moves: list[tuple[str, str, str]] = []  # (from, to, sha after the move), oldest first
    for line in reversed(out.splitlines()):
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha = _sha_or_none(parts[0])
        stamp = re.search(r"@\{(\d+)\}$", parts[1].strip())
        match = _REFLOG_SWITCH_RE.match(parts[2].strip())
        if sha and stamp and match and lo <= int(stamp.group(1)) <= hi and match.group(1) != match.group(2):
            moves.append((match.group(1), match.group(2), sha))
    if not moves:
        return start, False
    if current is not None and current != "HEAD" and current == moves[0][0]:
        return start, False  # back on the branch the session started on
    for _from, to, sha in moves:
        if current is not None and current != "HEAD" and to == current:
            return sha, True
    return None, True


def _session_files_base(buf: dict, repo_root: Path, grace: int) -> tuple[str | None, bool]:
    """``(base, known)`` for the session's file diff; records a switch on *buf*. Never raises.

    ``known`` False: the agent changed branch and the reflog cannot say where
    the session's work on the final branch began, so the observed file set
    is unknown (``git_files_unknown_reason``), never a guess.
    """
    base = buf.get("git_head_commit_hash")
    started = _parse_utc(buf.get("started_at"))
    last = _parse_utc(buf.get("last_activity_at")) or started
    window = None
    if started is not None and last is not None:
        window = (int(started.timestamp()), int(last.timestamp()) + grace)
    start_branch = buf.get("git_branch") if isinstance(buf.get("git_branch"), str) else None
    switched_base, switched = _files_base_after_switch(repo_root, _sha_or_none(base), start_branch, window)
    buf.pop("files_base_commit", None)
    buf.pop("git_files_unknown_reason", None)
    if not switched:
        return (base if isinstance(base, str) else None), True
    if switched_base is None:
        buf["git_files_unknown_reason"] = FILES_BRANCH_SWITCH_UNKNOWN
        return None, False
    buf["files_base_commit"] = switched_base
    return switched_base, True


def _stamp_files_base(entry: dict, buf: dict) -> str | None:
    """Record the branch-switch file base (or why it is unknown) on *entry*; return the base."""
    files_base = _sha_or_none(buf.get("files_base_commit"))
    if files_base is not None:
        # Files and lines count from where the session moved onto its final branch.
        entry["capture"]["files_base_commit"] = files_base
    if buf.get("git_files_unknown_reason") == FILES_BRANCH_SWITCH_UNKNOWN:
        entry["capture"]["git_files_unknown_reason"] = FILES_BRANCH_SWITCH_UNKNOWN
    return files_base


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


# Why a Receipt carries no line counts (``capture.lines_not_recorded_reason``).
LINES_NO_BASE_COMMIT = "no_base_commit"
LINES_NO_COMMIT = "no_session_commit"
LINES_BINARY_FILE = "binary_file_changed"
LINES_FILES_TRUNCATED = "changed_files_truncated"
LINES_GIT_UNAVAILABLE = "git_diff_unavailable"
LINES_UNCOMMITTED = "uncommitted_changes_present"


def _session_line_counts(
    repo_root: Path, base: str | None, commit: str | None, paths: list[str], *, truncated: bool = False,
) -> tuple[int | None, int | None, str | None]:
    """``(added, removed, None)`` from ``git diff --numstat base commit -- paths``, else ``(None, None, reason)``.

    *base* is HEAD when the session started, *commit* the session's own result
    commit (``receipt_evidence.commit_value``) and *paths* the changes counted
    as this session's (the same attribution as ``files_detail``). Unknown
    stays unknown: no base, no commit, a binary file (numstat ``-``), a
    truncated file list, uncommitted changes on those paths or a failed git
    read give no numbers, never 0. Never raises.
    """
    if _sha_or_none(base) is None:
        return None, None, LINES_NO_BASE_COMMIT
    if _sha_or_none(commit) is None:
        return None, None, LINES_NO_COMMIT
    if truncated:
        return None, None, LINES_FILES_TRUNCATED
    if not paths:
        return 0, 0, None  # git observed the range; nothing in it is this session's
    pathspec = ["--", *(f":(literal){p}" for p in paths)]
    # This session's paths still differing from *commit* (tracked edits or
    # untracked files) are work the commit range cannot count: unknown, not
    # an undercount presented as the total.
    pending = run_git(repo_root, ["diff", "--name-only", "-z", str(commit), *pathspec])
    untracked = run_git(repo_root, ["ls-files", "--others", "--exclude-standard", "-z", *pathspec])
    if pending is None or untracked is None:
        return None, None, LINES_GIT_UNAVAILABLE
    if pending.strip("\0").strip() or untracked.strip("\0").strip():
        return None, None, LINES_UNCOMMITTED
    out = run_git(repo_root, ["diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", str(base), str(commit), *pathspec])
    if out is None:
        return None, None, LINES_GIT_UNAVAILABLE
    added = removed = 0
    for row in out.split("\0"):
        if not row.strip():
            continue
        cols = row.split("\t", 2)
        if len(cols) < 2:
            continue
        if cols[0] == "-" or cols[1] == "-":
            return None, None, LINES_BINARY_FILE
        if not (cols[0].isdigit() and cols[1].isdigit()):
            return None, None, LINES_GIT_UNAVAILABLE
        added += int(cols[0])
        removed += int(cols[1])
    return added, removed, None


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
