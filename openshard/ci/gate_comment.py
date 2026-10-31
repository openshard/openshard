"""Post the merge gate's summary as one pull request comment, updated in place on every run.

The job summary lives on the Actions run, which reviewers rarely open. With
``openshard ci gate --github-pr-comment`` the same markdown is also written
to the pull request as a single comment, found again by a hidden marker and
edited rather than added to, so the thread never fills with copies.

The comment is information only. Posting it never changes the gate's pass
or fail, never raises, and a token that may not comment (no
``pull-requests: write``, a fork's read-only token) is reported as a note.
Only the summary markdown, which ``merge_gate`` has already escaped, is sent.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

MARKER = "<!-- openshard-gate -->"
DEFAULT_API = "https://api.github.com"
_MAX_BODY = 65_000  # GitHub's limit is 65,536 characters
_MAX_PAGES = 10
_TIMEOUT = 15.0
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")  # owner/name, never "." or ".."
_API_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~/-]*)?$")

Opener = Callable[..., Any]


def pr_number(event: dict) -> int | None:
    """The pull request number of a ``pull_request`` / ``pull_request_target`` event, else None."""
    pr = event.get("pull_request") if isinstance(event, dict) else None
    number = pr.get("number") if isinstance(pr, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None


def body_for(markdown: str) -> str:
    text = markdown.strip()
    if len(text) > _MAX_BODY:
        text = text[:_MAX_BODY] + "\n\n… (shortened; the run's job summary has the full text)"
    return f"{MARKER}\n{text}\n"


def _request(opener: Opener, method: str, url: str, token: str, payload: dict | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "openshard-gate",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    with opener(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _existing(opener: Opener, base: str, token: str) -> int | None:
    """The id of an earlier gate comment on the pull request (the first carrying the marker)."""
    for page in range(1, _MAX_PAGES + 1):
        items = _request(opener, "GET", f"{base}/comments?per_page=100&page={page}", token)
        if not isinstance(items, list):
            return None
        for item in items:
            body = item.get("body") if isinstance(item, dict) else None
            if isinstance(body, str) and body.startswith(MARKER) and isinstance(item.get("id"), int):
                return item["id"]
        if len(items) < 100:
            return None
    return None


def upsert(markdown: str, *, repo: str, number: int, token: str, api: str = DEFAULT_API,
           opener: Opener | None = None) -> tuple[bool, str]:
    """Create or update the gate comment. ``(posted, message)``; never raises."""
    opener = opener or urllib.request.urlopen
    if not token:
        return False, "no GitHub token, so no pull request comment was posted."
    if not _REPO_RE.match(repo or ""):
        return False, "the repository name was not readable, so no pull request comment was posted."
    api = (api or DEFAULT_API).rstrip("/")
    if not _API_RE.match(api):
        return False, "the GitHub API address is not an https:// URL, so no pull request comment was posted."
    base = f"{api}/repos/{repo}/issues/{number}"
    try:
        existing = _existing(opener, base, token)
        if existing is None:
            _request(opener, "POST", f"{base}/comments", token, {"body": body_for(markdown)})
            return True, f"posted the gate summary on pull request #{number}."
        _request(opener, "PATCH", f"{api}/repos/{repo}/issues/comments/{existing}", token,
                 {"body": body_for(markdown)})
        return True, f"updated the gate summary on pull request #{number}."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            return False, ("the token may not comment on this pull request (the job needs "
                           "`pull-requests: write`; a fork's token is read-only), so no comment was posted.")
        return False, f"GitHub answered {exc.code}, so no pull request comment was posted."
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"GitHub could not be reached ({type(exc).__name__}), so no pull request comment was posted."


__all__ = ["MARKER", "body_for", "pr_number", "upsert"]
