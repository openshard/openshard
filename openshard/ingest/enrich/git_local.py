"""Historical git evidence from the local repository (§6).

Commit-graph queries only (``util/git.py``): ``rev-parse``, ``merge-base
--is-ancestor``, ``diff-tree``, ``log``/``rev-list`` with a time window. The
working tree and index are never read -- today's checkout says nothing about
a past session.

Evidence rules (conservative about causality):

* ``git_verified`` -- a SHA that git itself printed in the session's tool
  output (``git commit``), that exists, is reachable from the session's
  branch, and whose diff overlaps the files the session edited. Files in
  such a commit's diff are upgraded to ``git_verified``.
* ``git_observed`` -- found in git but not strongly linked: a commit on the
  branch within ``[start, end + 2h]`` touching the edited files; a printed or
  claimed SHA that exists but fails a verification condition; the head at
  session start inferred from the branch and start time.
* ``agent_reported`` -- a SHA the agent claimed that is not in this repo.
* Nothing is inferred when the branch, the window or the edited files are
  unknown: those facts stay ``unknown``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from openshard.history.event import (
    EVIDENCE_AGENT_REPORTED,
    EVIDENCE_GIT_OBSERVED,
    EVIDENCE_GIT_VERIFIED,
)
from openshard.ingest.model import Fact, HistoricalCommit, HistoricalSession, unknown_fact

ENRICHER_ID = "enricher.git_local@1"
CANDIDATE_GRACE = timedelta(hours=2)
MAX_CANDIDATES = 20

REASON_TOOL_OUTPUT_VERIFIED = "sha_in_tool_output_exists_reachable_overlaps_edits"
REASON_TOOL_OUTPUT_UNLINKED = "sha_in_tool_output_exists_but_not_linked"
REASON_CLAIM_EXISTS = "claimed_sha_exists_not_verified"
REASON_CLAIM_MISSING = "claimed_sha_not_in_repository"
REASON_WINDOW_CANDIDATE = "commit_in_session_window_touching_edited_files"


def _shift(stamp: str, delta: timedelta) -> str | None:
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt + delta).isoformat()


def enrich_git(session: HistoricalSession, repo_root: Path) -> HistoricalSession:
    """Add git facts/commits to *session* in place (pre-seal). Never raises."""
    try:
        _enrich(session, repo_root)
    except Exception:
        session.dropped["git_enrichment_failed"] = session.dropped.get("git_enrichment_failed", 0) + 1
    return session


def _enrich(s: HistoricalSession, root: Path) -> None:
    from openshard.history.repo_identity import capture_repo_identity
    from openshard.util.git import (
        commit_exists,
        commit_files,
        commits_in_window,
        is_ancestor,
        ref_exists,
        rev_before,
    )

    if s.fact("repo").value is None:
        identity = capture_repo_identity(root)
        # The origin remote as configured *now*: a repository-level fact, not a
        # claim about the session's working tree.
        s.facts["repo"] = Fact(identity, EVIDENCE_GIT_OBSERVED, "git:remote.origin.url") if identity else unknown_fact()

    branch = s.value("branch")
    window = s.value("window") or {}
    start, end = window.get("start"), window.get("end") or window.get("start")
    branch_known = isinstance(branch, str) and ref_exists(root, branch)

    if s.value("head_at_start") is None:
        head = rev_before(root, branch, start) if branch_known and start else None
        s.facts["head_at_start"] = Fact(head, EVIDENCE_GIT_OBSERVED, "git:rev-list --before") if head else unknown_fact()

    edited = {e.path for e in s.file_edits}
    commits: dict[str, HistoricalCommit] = {}
    verified_files: set[str] = set()

    for sha in s.tool_output_shas:
        full = commit_exists(root, sha)
        if full is None:
            continue
        files = set(commit_files(root, full) or [])
        reachable = is_ancestor(root, full, branch) if branch_known else None
        if reachable is True and edited and files & edited:
            commits[full] = HistoricalCommit(full, EVIDENCE_GIT_VERIFIED, "git:cat-file+merge-base+diff-tree",
                                             REASON_TOOL_OUTPUT_VERIFIED)
            verified_files |= files & edited
        else:
            commits[full] = HistoricalCommit(full, EVIDENCE_GIT_OBSERVED, "git:cat-file", REASON_TOOL_OUTPUT_UNLINKED)

    for sha in s.claimed_shas:
        full = commit_exists(root, sha)
        if full is None:
            commits.setdefault(sha, HistoricalCommit(sha, EVIDENCE_AGENT_REPORTED, "transcript", REASON_CLAIM_MISSING))
        else:
            commits.setdefault(full, HistoricalCommit(full, EVIDENCE_GIT_OBSERVED, "git:cat-file", REASON_CLAIM_EXISTS))

    if branch_known and start and end and edited:
        until = _shift(end, CANDIDATE_GRACE)
        since = _shift(start, timedelta(0))
        found = commits_in_window(root, branch, since, until, sorted(edited), limit=MAX_CANDIDATES) if until and since else None
        for full in found or []:
            commits.setdefault(full, HistoricalCommit(full, EVIDENCE_GIT_OBSERVED, "git:log --since --until",
                                                      REASON_WINDOW_CANDIDATE))

    s.commits = list(commits.values())
    for edit in s.file_edits:
        if edit.path in verified_files:
            edit.evidence = EVIDENCE_GIT_VERIFIED
