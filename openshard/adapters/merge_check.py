"""Compare a sealed Receipt's HEAD with the repository's default branch, with local git only.

Runs when a Receipt is finalised in the capture service (``claude_hooks._fold``),
never on a hook the agent is waiting on. Best effort under a strict budget
(:data:`BUDGET_SECONDS` for every git call together, each call bounded by what
is left): any failure leaves the fact it would have produced unknown, and
nothing here raises or blocks the agent.

* Base: the target of ``origin/HEAD``, else ``origin/main``, ``origin/master``,
  ``main``, ``master``, as this checkout already knows them. No ``git fetch``:
  ``base_sha`` and ``checked_at`` say exactly what was compared. No base, or the
  checkout is on the base branch itself: no check (the block is absent).
* ``behind_by`` / ``base_commits``: commits on the base not in HEAD (newest first).
* ``conflicts``: ``git merge-tree --write-tree`` (git >= 2.38) of HEAD and the
  base. Its objects go to a throw-away object directory, so the repository is
  left exactly as it was. Per conflicting path, the start lines of the HEAD-side
  changes that overlap a base-side change, from ``git diff -U0`` hunk headers
  against the merge base (only the ``@@`` numbers are read), and the author of
  the base-side commit that last touched the overlapping base lines
  (``git blame --porcelain``, only the commit header lines are read). No file
  content or diff text is kept. git too old or any merge-tree failure: ``None``.

Opt out with ``OPENSHARD_MERGE_CHECK=off``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history import merge_check as shape
from openshard.util.git import GIT_OBSERVE_FLAGS, NO_WINDOW_KW

MERGE_CHECK_ENV = "OPENSHARD_MERGE_CHECK"
BUDGET_SECONDS = 3.0

_CANDIDATES = ("refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main", "refs/heads/master")
_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BLAME_HEADER_RE = re.compile(rb"^([0-9a-f]{40}(?:[0-9a-f]{24})?) \d+ \d+(?: \d+)?$")
_OFF = frozenset({"0", "off", "false", "no"})


class _Git:
    """git calls in one checkout that share one deadline. Never raises."""

    def __init__(self, root: Path, budget: float) -> None:
        self.root = root
        self.deadline = time.monotonic() + budget

    def run(self, args: list[str], *, env: dict[str, str] | None = None) -> tuple[int, bytes] | None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            proc = subprocess.run(
                ["git", *GIT_OBSERVE_FLAGS, "--literal-pathspecs", *args], cwd=str(self.root), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=remaining,
                check=False, **NO_WINDOW_KW,
            )
        except Exception:
            return None
        return proc.returncode, proc.stdout

    def out(self, args: list[str]) -> str | None:
        result = self.run(args)
        return result[1].decode("utf-8", "replace") if result is not None and result[0] == 0 else None


def check(repo_root: Path, *, budget: float = BUDGET_SECONDS, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The ``merge_check`` block for *repo_root*'s HEAD now, or None when nothing could be compared. Never raises."""
    source = os.environ if env is None else env
    if str(source.get(MERGE_CHECK_ENV) or "").strip().lower() in _OFF:
        return None
    try:
        return _check(_Git(Path(repo_root), budget))
    except Exception:
        return None


def _check(git: _Git) -> dict[str, Any] | None:
    head = shape.sha_or_none((git.out(["rev-parse", "--verify", "-q", "HEAD^{commit}"]) or "").strip())
    found = _default_base(git) if head else None
    if head is None or found is None:
        return None
    base_ref, base_sha = found
    block: dict[str, Any] = {
        "base_ref": base_ref, "base_sha": base_sha, "head_sha": head,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "behind_by": None, "base_commits": None, "conflicts": None,
    }
    merge_base = shape.sha_or_none((git.out(["merge-base", head, base_sha]) or "").strip())
    if merge_base is not None:  # no merge base (unrelated or shallow history): what base gained is unknown
        count = (git.out(["rev-list", "--count", f"{head}..{base_sha}"]) or "").strip()
        block["behind_by"] = int(count) if count.isdigit() else None
        block["base_commits"] = _base_commits(git, head, base_sha)
        if block["behind_by"] == 0:
            block["conflicts"] = []  # the base is already in HEAD: merging it changes nothing
        else:
            block["conflicts"] = _conflicts(git, head, base_sha, merge_base)
    return shape.normalize(block)


def _default_base(git: _Git) -> tuple[str, str] | None:
    """``(short ref, sha)`` of the default branch this checkout knows, else None (also when HEAD is on it)."""
    target = (git.out(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"]) or "").strip()
    candidates = ([target] if target.startswith("refs/remotes/") else []) + list(_CANDIDATES)
    listed = git.out(["for-each-ref", "--format=%(refname)%00%(objectname)", *dict.fromkeys(candidates)])
    if listed is None:
        return None
    refs = dict(line.split("\0", 1) for line in listed.splitlines() if "\0" in line)
    current = (git.out(["symbolic-ref", "-q", "HEAD"]) or "").strip()
    for ref in candidates:
        sha = shape.sha_or_none(refs.get(ref, "").strip())
        if sha is None:
            continue
        if ref == current:
            return None  # the checkout is on the base branch: there is nothing to merge
        return ref.removeprefix("refs/remotes/").removeprefix("refs/heads/"), sha
    return None


def _base_commits(git: _Git, head: str, base: str) -> list[dict[str, Any]] | None:
    raw = git.out(["log", "--no-show-signature", f"--max-count={shape.MAX_BASE_COMMITS}",
                   "--format=%H%x1f%an%x1f%ct%x1f%s%x1e", f"{head}..{base}"])
    if raw is None:
        return None
    commits = []
    for record in raw.split("\x1e"):
        fields = record.strip("\r\n").split("\x1f")
        if len(fields) != 4 or shape.sha_or_none(fields[0]) is None:
            continue
        stamp = datetime.fromtimestamp(int(fields[2]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if fields[2].isdigit() else None
        commits.append({"sha": fields[0], "author": fields[1], "subject": fields[3], "committed_at": stamp})
    return commits


def _conflicts(git: _Git, head: str, base: str, merge_base: str) -> list[dict[str, Any]] | None:
    paths = _conflicting_paths(git, head, base)
    if paths is None:
        return None
    out = []
    for path in paths[: shape.MAX_CONFLICT_PATHS]:
        lines, base_ranges = _overlaps(git, merge_base, head, base, path)
        base_author, base_sha = _last_base_author(git, base, path, base_ranges)
        out.append({"path": path, "lines": lines, "base_author": base_author, "base_sha": base_sha})
    return out


def _conflicting_paths(git: _Git, head: str, base: str) -> list[str] | None:
    """Paths ``git merge-tree`` reports as conflicting ([] when clean), or None when it cannot say."""
    objects = (git.out(["rev-parse", "--path-format=absolute", "--git-path", "objects"]) or "").strip()
    if not objects:
        return None
    with tempfile.TemporaryDirectory(prefix="openshard-merge-check-") as scratch:
        env = dict(os.environ)
        alternates = [objects, *filter(None, [env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")])]
        env.update({"GIT_OBJECT_DIRECTORY": scratch, "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(alternates)})
        result = git.run(["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", head, base], env=env)
    if result is None or result[0] not in (0, 1):
        return None  # timed out, git older than 2.38, or the merge itself failed
    fields = result[1].split(b"\0")
    if result[0] == 0 or not shape.sha_or_none(fields[0].decode("ascii", "replace")):
        return [] if result[0] == 0 else None
    paths: list[str] = []
    for raw in fields[1:]:
        if not raw:
            break
        path = shape.safe_path(raw.decode("utf-8", "replace"))
        if path is not None and path not in paths:
            paths.append(path)
    return paths or None  # git reported conflicts but none could be named: unknown, never clean


def _hunks(git: _Git, old: str, new: str, path: str) -> list[tuple[int, int, int, int]] | None:
    """``(old_index, old_count, new_start, new_count)`` per ``-U0`` hunk; only ``@@`` header numbers are read."""
    result = git.run(["diff", "--no-ext-diff", "--no-color", "--no-renames", "--histogram", "-U0", old, new, "--", path])
    if result is None or result[0] != 0:
        return None
    hunks = []
    for line in result[1].splitlines():
        match = _HUNK_RE.match(line)
        if match:
            a, b = int(match[1]), int(match[2] or b"1")
            c, d = int(match[3]), int(match[4] or b"1")
            hunks.append((a - 1 if b else a, b, c, d))  # xdiff's 0-based index into the merge base
    return hunks


def _overlaps(git: _Git, merge_base: str, head: str, base: str, path: str) -> tuple[list[int], list[tuple[int, int]]]:
    """HEAD-side start lines of the changes that overlap or touch a base-side change, and those base-side line ranges.

    The rule git's three-way merge uses: two changes conflict unless one ends
    before the other starts (touching counts). Unknown (a failed or timed-out
    diff, a binary file): no lines.
    """
    ours, theirs = _hunks(git, merge_base, head, path), _hunks(git, merge_base, base, path)
    if not ours or not theirs:
        return [], []
    lines: list[int] = []
    ranges: list[tuple[int, int]] = []
    for i1, n1, start, count in ours:
        hits = [(c, d) for i2, n2, c, d in theirs if not (i1 + n1 < i2 or i2 + n2 < i1)]
        if not hits:
            continue
        lines.append(max(1, start if count else start + 1))
        ranges.extend((c, c + d - 1) for c, d in hits if d)
    return sorted(set(lines))[: shape.MAX_LINES_PER_PATH], sorted(set(ranges))[: shape.MAX_LINES_PER_PATH]


def _last_base_author(git: _Git, base: str, path: str, ranges: list[tuple[int, int]]) -> tuple[str | None, str | None]:
    """Author and sha of the base-side commit that last touched *ranges* of *path* (by committer time)."""
    if not ranges:
        return None, None
    args = ["blame", "--porcelain"]
    for lo, hi in ranges:
        args += ["-L", f"{lo},{hi}"]
    result = git.run([*args, base, "--", path])
    if result is None or result[0] != 0:
        return None, None
    authors: dict[str, str] = {}
    times: dict[str, int] = {}
    current = None
    for line in result[1].splitlines():
        header = _BLAME_HEADER_RE.match(line)
        if header:
            current = header[1].decode("ascii")
        elif current and line.startswith(b"author "):
            authors[current] = line[7:].decode("utf-8", "replace")
        elif current and line.startswith(b"committer-time ") and line[15:].strip().isdigit():
            times[current] = int(line[15:].strip())
    if not times:
        return None, None
    sha = max(times, key=lambda s: times[s])
    return authors.get(sha), sha


def stamp(entry: dict, buf: dict) -> None:
    """Put the finalisation-time ``merge_check`` (``buf``) on *entry*, normalised. Absent when not checked."""
    block = shape.normalize(buf.get("merge_check"))
    if block is not None:
        entry["merge_check"] = block


__all__ = ["BUDGET_SECONDS", "MERGE_CHECK_ENV", "check", "stamp"]
