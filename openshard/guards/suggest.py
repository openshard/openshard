"""Find corrections in git history that could become guards.

A *correction* is a commit that is not marked as an agent's and that changes
or removes lines an agent commit wrote shortly before. That is the moment a
person fixed an agent's mistake, and the natural place for a guard: the
agent's commit is the ``--bad`` side and the correction the ``--good`` side
of ``openshard guard qualify``.

Which commits are an agent's comes from the commit itself, never a guess:

* ``Openshard-Agent: <agent>`` -- written by Openshard's commit hook when a
  captured agent session made the commit (observed);
* ``Claude-Session: <url>`` -- written by Claude Code on the web (declared);
* ``Co-Authored-By: ... <noreply@anthropic.com>`` and similar agent
  addresses -- written by the agent itself (declared).

Lines are attributed with ``git blame`` on the correction's parent. This is
a suggestion list for a person, not evidence: nothing is recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_SCAN = 150  # first-parent commits read
DEFAULT_WINDOW_DAYS = 14  # how long after an agent commit a change still counts as a correction
MAX_HUNKS_PER_COMMIT = 30
MAX_CANDIDATES = 20

_AGENT_EMAILS = re.compile(r"<(noreply@anthropic\.com|.*codex.*@openai\.com|cursoragent@cursor\.com|"
                           r"devin-ai-integration\[bot\]@users\.noreply\.github\.com)>", re.I)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_SEP_FIELD, _SEP_RECORD = "\x1f", "\x1e"


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    when: datetime
    subject: str
    agent: str | None  # agent label when the commit is marked as an agent's
    evidence: str | None  # observed | declared


@dataclass
class Correction:
    good: str  # the correcting commit
    bad: str  # the agent commit whose lines it changed (the most recent one)
    agent: str
    evidence: str
    subject: str
    files: list[str] = field(default_factory=list)
    lines_changed: int = 0
    days_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"good": self.good, "bad": self.bad, "agent": self.agent, "evidence": self.evidence,
                "subject": self.subject, "files": self.files, "lines_changed": self.lines_changed,
                "days_after": round(self.days_after, 1)}


def _agent_of(trailers: str) -> tuple[str | None, str | None]:
    for line in trailers.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "openshard-agent" and value:
            return value, "observed"
    for line in trailers.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "claude-session" and value:
            return "Claude Code (cloud)", "declared"
        if key == "co-authored-by" and _AGENT_EMAILS.search(value):
            return value.split("<", 1)[0].strip() or "agent", "declared"
    return None, None


def read_history(repo_root: Path, *, scan: int = DEFAULT_SCAN, rev: str = "HEAD") -> list[Commit]:
    """The last *scan* first-parent commits of *rev*, newest first. Empty on any git failure."""
    from openshard.util.git import run_git

    fmt = _SEP_FIELD.join(["%H", "%P", "%cI", "%s", "%(trailers:unfold,only)"]) + _SEP_RECORD
    out = run_git(Path(repo_root), ["log", "--first-parent", f"-n{scan}", f"--format={fmt}", rev])
    commits: list[Commit] = []
    for record in (out or "").split(_SEP_RECORD):
        parts = record.strip("\n").split(_SEP_FIELD)
        if len(parts) < 5 or not parts[0]:
            continue
        sha, parents, when, subject, trailers = parts[:5]
        try:
            stamp = datetime.fromisoformat(when)
        except ValueError:
            continue
        agent, evidence = _agent_of(trailers)
        commits.append(Commit(sha=sha, parents=tuple(parents.split()), when=stamp, subject=subject,
                              agent=agent, evidence=evidence))
    return commits


def _removed_ranges(repo_root: Path, parent: str, sha: str) -> dict[str, list[tuple[int, int]]]:
    """``{old path: [(start, count), ...]}``: lines of *parent* that *sha* changed or removed."""
    from openshard.util.git import run_git

    diff = run_git(Path(repo_root), ["diff", "-U0", "--no-color", "--no-renames", parent, sha])
    ranges: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    hunks = 0
    for line in (diff or "").splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("@@") and path:
            m = _HUNK.match(line)
            if not m:
                continue
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count > 0:
                ranges.setdefault(path, []).append((start, count))
                hunks += 1
                if hunks >= MAX_HUNKS_PER_COMMIT:
                    break
    return ranges


def _blamed(repo_root: Path, parent: str, path: str, start: int, count: int) -> dict[str, int]:
    """``{commit sha: lines}`` that last wrote lines ``start..start+count-1`` of *path* at *parent*."""
    from openshard.util.git import run_git

    out = run_git(Path(repo_root), ["blame", "--porcelain", "-L", f"{start},+{count}", parent, "--", path])
    owners: dict[str, int] = {}
    for line in (out or "").splitlines():
        head = line.split(" ", 1)[0]
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head) and len(line.split()) >= 3:
            owners[head] = owners.get(head, 0) + 1
    return owners


def find_corrections(
    repo_root: Path, *, scan: int = DEFAULT_SCAN, window_days: float = DEFAULT_WINDOW_DAYS, rev: str = "HEAD",
) -> list[Correction]:
    """Human commits that changed lines an agent commit wrote within *window_days*, newest first."""
    commits = read_history(repo_root, scan=scan, rev=rev)
    agent_commits = {c.sha: c for c in commits if c.agent}
    if not agent_commits:
        return []
    found: list[Correction] = []
    for c in commits:
        if c.agent or len(c.parents) != 1:
            continue  # agent commits and merges are never corrections
        hits: dict[str, int] = {}
        files: set[str] = set()
        for path, ranges in _removed_ranges(repo_root, c.parents[0], c.sha).items():
            for start, count in ranges:
                for owner, n in _blamed(repo_root, c.parents[0], path, start, count).items():
                    a = agent_commits.get(owner)
                    if a is None:
                        continue
                    age = (c.when - a.when).total_seconds() / 86400
                    if 0 <= age <= window_days:
                        hits[owner] = hits.get(owner, 0) + n
                        files.add(path)
        if not hits:
            continue
        bad = max(hits, key=lambda s: agent_commits[s].when)
        a = agent_commits[bad]
        found.append(Correction(good=c.sha, bad=bad, agent=a.agent or "agent", evidence=a.evidence or "declared",
                                subject=c.subject, files=sorted(files), lines_changed=sum(hits.values()),
                                days_after=(c.when - a.when).total_seconds() / 86400))
        if len(found) >= MAX_CANDIDATES:
            break
    return found


__all__ = ["Commit", "Correction", "find_corrections", "read_history"]
