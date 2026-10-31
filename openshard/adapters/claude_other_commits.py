"""Paths a Receipt's git diff shows only because another agent committed them.

A hook session's changed files are the git diff from its files base to the
working tree. When another agent (Codex, say) commits in the same checkout
during the session, that commit's paths land in the diff too. This module
names the paths that changed *only* in such commits so the classifier can
label them ``other_commit`` instead of counting them as this session's work.

Truth rule: a commit is another agent's only when its message positively
says so -- an ``Openshard-Agent`` trailer (or a ``Claude-Session`` trailer,
read as Claude Code) naming a different agent than this session's. A commit
with no such mark stays unknown and its paths stay ``git_observed``: without
the commit trailer hook, git cannot tell this session's commits from a
human's or another agent's, and a guess would hide this session's own work.
Anything git cannot answer (no base, too many commits, a merge, a failed
call) excludes nothing.
"""

from __future__ import annotations

from pathlib import Path

from openshard.adapters.claude_git_outcome import _MAX_SESSION_COMMITS, _sha_or_none
from openshard.util.git import run_git

_PATH_CAP = 200  # the same cap the git diff parser applies, so paths compare equal
_RECORD = "\x1e"
_FIELD = "\x1f"


def _commit_records(repo_root: Path, base: str) -> list[tuple[str, str, list[str]]] | None:
    """``[(sha, message, paths)]`` for ``base..HEAD``, or None when unknown (merge, too many, git error)."""
    out = run_git(repo_root, [
        "log", f"--max-count={_MAX_SESSION_COMMITS + 1}", "--no-renames", "--name-only",
        f"--format={_RECORD}%H{_FIELD}%P{_FIELD}%B{_FIELD}", f"{base}..HEAD",
    ])
    if out is None:
        return None
    records: list[tuple[str, str, list[str]]] = []
    for chunk in out.split(_RECORD)[1:]:
        parts = chunk.split(_FIELD, 3)
        sha = _sha_or_none(parts[0]) if len(parts) == 4 else None
        if sha is None or len(parts[1].split()) > 1:
            return None  # unparseable, or a merge whose paths --name-only does not list
        records.append((sha, parts[2], [ln.strip() for ln in parts[3].splitlines() if ln.strip()]))
    return None if len(records) > _MAX_SESSION_COMMITS else records


def _dirty_paths(repo_root: Path) -> set[str] | None:
    """Paths whose working tree differs from HEAD (tracked or untracked), or None when unknown."""
    tracked = run_git(repo_root, ["diff", "--no-renames", "--name-only", "HEAD"])
    untracked = run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    if tracked is None or untracked is None:
        return None
    return {ln.strip() for ln in (tracked + "\n" + untracked).splitlines() if ln.strip()}


def other_agent_commit_paths(buf: dict, repo_root: Path, agent_label: str) -> set[str]:
    """Paths changed since the session's files base only by commits another agent's trailer marks. Never raises.

    The base is the branch-switch base or the start HEAD (as
    ``_session_files_base`` chose it); *agent_label* is this session's agent
    (``Claude Code``, ``Codex``, ...). Commits git observed this session
    create (``git_outcome.commits``) are never another agent's. A path also
    touched by an unmarked or own commit, or changed again in the working
    tree, is not returned.
    """
    from openshard.ci.agent_marks import agent_name
    from openshard.safety.sanitize import sanitize_path

    try:
        outcome = buf.get("git_outcome")
        own_commits = outcome.get("commits") if isinstance(outcome, dict) else None
        start = _sha_or_none(buf.get("files_base_commit")) or _sha_or_none(buf.get("git_head_commit_hash"))
        records = _commit_records(repo_root, start) if start else None
        if not records:
            return set()
        own = {c for c in own_commits if isinstance(c, str)} if isinstance(own_commits, list) else set()
        mine = agent_label.strip().lower()
        foreign: set[str] = set()
        kept: set[str] = set()
        for sha, message, paths in records:
            name = agent_name(message)
            target = foreign if sha not in own and name and name.strip().lower() != mine else kept
            target.update(p for p in (sanitize_path(raw, _PATH_CAP) for raw in paths) if p)
        if not foreign:
            return set()
        dirty = _dirty_paths(repo_root)
        if dirty is None:
            return set()
        return foreign - kept - {p for p in (sanitize_path(raw, _PATH_CAP) for raw in dirty) if p}
    except Exception:
        return set()


__all__ = ["other_agent_commit_paths"]
