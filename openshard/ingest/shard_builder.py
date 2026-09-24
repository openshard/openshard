"""Grouping policy and repository routing.

Grouping (v1 default, not an invariant): one native session -> one new Shard,
attempt 1. The rule is recorded on the receipt as a fact
(``import.grouping``) so a later regrouping can be attached rather than
rewritten.

Routing: a session's recorded ``cwd`` resolves to its git root today, and
that root's ``.openshard/`` is the destination. By default only sessions for
the target repository are imported; ``all_repos`` also routes into other
repositories that already have ``.openshard/``. A root that is the home
directory (or an ancestor of it) is refused unless ``allow_home_repo``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GROUPING_RULE = "one_session_default"

ROUTE_OK = "ok"
ROUTE_NO_CWD = "no_cwd_recorded"
ROUTE_CWD_MISSING = "cwd_no_longer_exists"
ROUTE_NOT_A_REPO = "not_in_a_git_repository"
ROUTE_HOME_REPO = "home_directory_repository_refused"
ROUTE_OTHER_REPO = "other_repository"
ROUTE_NOT_OPTED_IN = "repository_without_openshard"


@dataclass(frozen=True)
class Route:
    status: str
    repo_root: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == ROUTE_OK


def grouping_block() -> dict:
    from openshard.history.event import EVIDENCE_IMPORTED_TRANSCRIPT

    return {"rule": GROUPING_RULE, "evidence": EVIDENCE_IMPORTED_TRANSCRIPT}


def _is_home_root(root: Path) -> bool:
    from openshard.adapters.claude_hooks import _is_forbidden_capture_root

    return _is_forbidden_capture_root(root)


def resolve_session_repo(cwd: str | None) -> Route:
    """The git root a recorded ``cwd`` belongs to today (no routing policy)."""
    from openshard.adapters.claude_mcp_install import find_repo_root

    if not cwd:
        return Route(ROUTE_NO_CWD)
    p = Path(cwd)
    try:
        if not p.is_dir():
            return Route(ROUTE_CWD_MISSING)
    except OSError:
        return Route(ROUTE_CWD_MISSING)
    root = find_repo_root(p)
    if root is None:
        return Route(ROUTE_NOT_A_REPO)
    return Route(ROUTE_OK, root.resolve())


def route_session(
    cwd: str | None,
    target_root: Path,
    *,
    all_repos: bool = False,
    allow_home_repo: bool = False,
) -> Route:
    found = resolve_session_repo(cwd)
    if not found.ok or found.repo_root is None:
        return found
    root = found.repo_root
    if _is_home_root(root) and not allow_home_repo:
        return Route(ROUTE_HOME_REPO, root)
    if root == Path(target_root).resolve():
        return Route(ROUTE_OK, root)
    if not all_repos:
        return Route(ROUTE_OTHER_REPO, root)
    if not (root / ".openshard").is_dir():
        return Route(ROUTE_NOT_OPTED_IN, root)
    return Route(ROUTE_OK, root)
