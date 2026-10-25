"""Head commits that only bring the base branch into an agent's pull request.

GitHub's "Update branch" button (or ``git merge main``) adds a merge commit
on top of an agent's work. That commit has no agent marks, so no Receipt is
made for it, while the agent's own last commit has one. Without this, the
merge gate waits for a Receipt that can never arrive and fails.

``base_merges_after_agent`` walks back from the head along first parents and
returns the agent's last commit when every commit above it is such a merge:

* it has exactly two parents and no agent marks of its own;
* its second parent is already on the base branch (it brings in nothing that
  is not on the base);
* its tree is exactly what git's own merge of the two parents gives, so it
  adds no edits of its own (no conflict resolution, no extra change).

Anything else, including a non-merge commit without marks, returns None and
the gate decides on the head commit as before. Read only; never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from openshard.ci.agent_marks import agent_marks
from openshard.util.git import run_git

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_MERGES = 20


@dataclass(frozen=True)
class BaseMerges:
    agent_commit: str
    merges: tuple[str, ...]


def _parents(root: Path, sha: str) -> list[str] | None:
    out = run_git(root, ["rev-list", "--parents", "-n", "1", sha])
    if out is None:
        return None
    parts = out.split()
    if not parts or parts[0] != sha or not all(_SHA_RE.match(p) for p in parts):
        return None
    return parts[1:]


def _message(root: Path, sha: str) -> str | None:
    return run_git(root, ["log", "-1", "--format=%B", sha])


def _tree(root: Path, sha: str) -> str | None:
    out = run_git(root, ["rev-parse", f"{sha}^{{tree}}"])
    out = out.strip() if out else ""
    return out if _SHA_RE.match(out) else None


def _clean_merge_tree(root: Path, first: str, second: str) -> str | None:
    """The tree git's own merge of the two commits gives, or None on a conflict or an error."""
    out = run_git(root, ["merge-tree", "--write-tree", first, second])
    line = out.splitlines()[0].strip() if out else ""
    return line if _SHA_RE.match(line) else None


def _on_base(root: Path, sha: str, base: str) -> bool:
    return run_git(root, ["merge-base", "--is-ancestor", sha, base]) is not None


def base_merges_after_agent(root: Path, head: str, base: str | None) -> BaseMerges | None:
    """The agent's last commit under *head* when everything above it only merges *base* in, else None."""
    if not base or not _SHA_RE.match(head or "") or not _SHA_RE.match(base):
        return None
    merges: list[str] = []
    current = head
    for _ in range(_MAX_MERGES + 1):
        message = _message(root, current)
        if message is None:
            return None
        if agent_marks(message):
            return BaseMerges(agent_commit=current, merges=tuple(merges)) if merges else None
        if len(merges) >= _MAX_MERGES:
            return None
        parents = _parents(root, current)
        if parents is None or len(parents) != 2:
            return None
        first, second = parents
        if not _on_base(root, second, base) or _on_base(root, first, base):
            return None
        tree, merged = _tree(root, current), _clean_merge_tree(root, first, second)
        if tree is None or merged is None or tree != merged:
            return None
        merges.append(current)
        current = first
    return None


__all__ = ["BaseMerges", "base_merges_after_agent"]
