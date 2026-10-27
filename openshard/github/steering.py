"""Steering derived from a pull request: what a person told a PR-driven agent on GitHub.

Where OpenShard has no runtime presence (Codex cloud started from ChatGPT, a
Copilot or Cursor background agent, ``@codex`` / ``@claude`` on a pull request),
the only steering that leaves a trace is what reaches GitHub. This module reads
it, read-only, through the GitHub CLI and turns it into the same steering block
hook capture writes (``capture.steering`` version 1), with ``source: github`` on
every event and ``environment: web``:

* a review that requests changes   -> ``follow_up``, intent ``correction`` (the review state decides)
* a review that approves           -> ``follow_up``, intent ``approval``
* a review that comments           -> ``follow_up``, intent from ``rules-v1`` on its text
* a PR comment addressed to the agent (``@codex fix ...``) -> ``follow_up``, intent from ``rules-v1``

plus counts: inline review comments, review re-requests, comments by bots
(ignored: a bot is not a person), and whether the commit followed a review.

Which commit. The cloud-receipts workflow creates one Receipt per commit, so
each commit carries the steering it answered: the person's input after the
previous commit of the pull request and up to this one. Times are the
commits' committer dates (GitHub records when a commit was made, not when it
was pushed). Input after the last commit is on no Receipt until a commit
follows it.

Privacy. Comment and review text is classified here, in the CI job, and dropped
at once: :func:`reduce_activity` returns only kinds, intents, rule ids, lengths
and times, and nothing else in this module ever holds the text. Logins are not
kept either.

Not derivable from GitHub, and listed under ``not_available`` with the reason:
the first prompt, mid-turn messages, interrupts, permission answers, and a
person's own commits (a cloud agent often pushes as the person who started it).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.adapters.steering_intent import METHOD, classify_intent

SOURCE = "github"
ENVIRONMENT = "web"
MAX_EVENTS = 100
_MAX_ITEMS = 1000  # per GitHub list read; a longer review history is cut, and the block says so
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
# Agents people address on a pull request. A mention is a word-boundary ``@name``, not an email or a path.
AGENT_HANDLES: tuple[str, ...] = (
    "codex", "claude", "copilot", "cursor", "devin", "jules", "grok", "openhands", "gemini", "amp",
)

NOT_AVAILABLE: dict[str, str] = {
    "initial_prompt": "the task reaches a PR-driven agent through its own app, or an issue or chat OpenShard "
                      "does not read; GitHub shows only what follows on the pull request",
    "interjection": "GitHub does not show whether the agent was still working when a comment was posted",
    "interrupt": "a person stops a cloud agent in the agent's own app; nothing reaches GitHub",
    "permission_decision": "permission prompts are answered in the agent's own app, not on GitHub",
    "manual_edit": "GitHub names the account behind a commit, and cloud agents often push as the person who "
                   "started them, so a person's commit cannot be told from the agent's",
}

Runner = Callable[..., Any]


@dataclass(frozen=True)
class Input:
    """One person's input on the pull request, with its text already dropped."""

    at: datetime
    via: str  # review_changes_requested | review_approved | review_commented | agent_mention
    intent: str
    confidence: str
    rule: str
    chars: int | None


@dataclass
class Activity:
    """What :func:`reduce_activity` keeps of a pull request: no text, no logins."""

    inputs: list[Input]
    review_comment_times: list[datetime]
    rerequest_times: list[datetime]
    bot_comment_times: list[datetime]
    truncated: bool = False


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else None


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_bot(user: Mapping[str, Any]) -> bool:
    login = user.get("login")
    return user.get("type") == "Bot" or (isinstance(login, str) and login.endswith("[bot]"))


def _mention_re(handles: Iterable[str]) -> re.Pattern[str]:
    names = "|".join(re.escape(h) for h in sorted(set(handles), key=len, reverse=True))
    return re.compile(rf"(?<![\w@./-])@({names})(?![\w-])", re.IGNORECASE)


def _classified(text: str, via: str, at: datetime) -> Input:
    result = classify_intent(text)
    return Input(at=at, via=via, intent=result.intent, confidence=result.confidence, rule=result.rule,
                 chars=len(text))


def _authored(items: Iterable[object], time_key: str) -> Iterator[tuple[Mapping[str, Any], datetime, bool]]:
    """``(item, at, by_bot)`` for each item with a time and an author; no author (a deleted account) is skipped:
    not provably a person, not a bot either."""
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get("user"), Mapping):
            at = _parse(item.get(time_key))
            if at is not None:
                yield item, at, _is_bot(item["user"])


def _review_input(review: Mapping[str, Any], at: datetime, first_inline: Mapping[int, str]) -> Input | None:
    """A person's submitted review as an input: its state decides, else its text (or its first inline comment's)."""
    body = review.get("body")
    text = body if isinstance(body, str) and body.strip() else None
    state = review.get("state")
    if state == "CHANGES_REQUESTED":
        return Input(at, "review_changes_requested", "correction", "high", "github.review_changes_requested",
                     len(text) if text else None)
    if state == "APPROVED":
        return Input(at, "review_approved", "approval", "high", "github.review_approved", len(text) if text else None)
    review_id = review.get("id")
    text = text or (first_inline.get(review_id) if isinstance(review_id, int) else None)
    return _classified(text, "review_commented", at) if text is not None else None


def _rerequests(timeline: Iterable[object], reviewed: Mapping[str, list[datetime]]) -> list[datetime]:
    """When a review was requested again from someone who had already reviewed."""
    out: list[datetime] = []
    for item in timeline:
        if not isinstance(item, Mapping) or item.get("event") != "review_requested":
            continue
        at = _parse(item.get("created_at"))
        reviewer = item.get("requested_reviewer")
        login = reviewer.get("login") if isinstance(reviewer, Mapping) else None
        if at is not None and isinstance(login, str) and any(t < at for t in reviewed.get(login.lower(), [])):
            out.append(at)
    return out


def reduce_activity(
    *,
    reviews: Iterable[object],
    review_comments: Iterable[object],
    issue_comments: Iterable[object],
    timeline: Iterable[object] = (),
    handles: Iterable[str] = AGENT_HANDLES,
    truncated: bool = False,
) -> Activity:
    """Classify each person's review and agent-addressed comment, then drop the text. Never raises on bad items."""
    mention = _mention_re(handles)
    inputs: list[Input] = []
    bots: list[datetime] = []
    inline: list[datetime] = []
    first_inline: dict[int, str] = {}  # review id -> its first inline comment's text, for an empty review body
    for comment, at, by_bot in _authored(review_comments, "created_at"):
        (bots if by_bot else inline).append(at)
        review_id, body = comment.get("pull_request_review_id"), comment.get("body")
        if not by_bot and isinstance(review_id, int) and isinstance(body, str) and body.strip():
            first_inline.setdefault(review_id, body)
    reviewed: dict[str, list[datetime]] = {}  # reviewer -> when they reviewed (for re-requests; not kept)
    for review, at, by_bot in _authored(reviews, "submitted_at"):
        if review.get("state") not in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED"):
            continue  # pending (unsubmitted) or dismissed: not an instruction the agent received as such
        if by_bot:
            bots.append(at)
            continue
        login = review["user"].get("login")
        if isinstance(login, str):
            reviewed.setdefault(login.lower(), []).append(at)
        item = _review_input(review, at, first_inline)
        if item is not None:
            inputs.append(item)
    for comment, at, by_bot in _authored(issue_comments, "created_at"):
        body = comment.get("body")
        if by_bot:
            bots.append(at)
        elif isinstance(body, str) and mention.search(body):
            inputs.append(_classified(mention.sub(" ", body).strip() or body, "agent_mention", at))
    inputs.sort(key=lambda i: i.at)
    return Activity(inputs=inputs, review_comment_times=inline, rerequest_times=_rerequests(timeline, reviewed),
                    bot_comment_times=bots, truncated=truncated)


def commit_window(commits: Iterable[object], sha: str) -> tuple[datetime | None, datetime] | None:
    """``(after, up_to)`` for *sha* among the pull request's commits; None when it is not one of them.

    ``after`` is the previous commit's committer date (None for the first
    commit: everything before it). Committer dates, because GitHub records
    when a commit was made, not when it was pushed.
    """
    previous: datetime | None = None
    for item in commits:
        if not isinstance(item, Mapping):
            continue
        commit = item.get("commit")
        committer = commit.get("committer") if isinstance(commit, Mapping) else None
        at = _parse(committer.get("date")) if isinstance(committer, Mapping) else None
        if item.get("sha") == sha:
            return (previous, at) if at is not None else None
        if at is not None:
            previous = at
    return None


def build_block(activity: Activity, window: tuple[datetime | None, datetime]) -> dict[str, Any]:
    """The ``capture.steering`` (version 1) block for one commit's window. No text, no logins."""
    after, up_to = window

    def inside(at: datetime) -> bool:
        return (after is None or at > after) and at <= up_to

    chosen = [i for i in activity.inputs if inside(i.at)]
    events: list[dict[str, Any]] = []
    for item in chosen[:MAX_EVENTS]:
        event: dict[str, Any] = {"at": _stamp(item.at), "kind": "follow_up", "after": "unknown", "source": SOURCE,
                                 "via": item.via, "intent": item.intent, "intent_confidence": item.confidence,
                                 "intent_rule": item.rule}
        if item.chars is not None:
            event["prompt_chars"] = item.chars
        events.append(event)
    github: dict[str, Any] = {
        "review_comments": sum(1 for t in activity.review_comment_times if inside(t)),
        "review_rerequests": sum(1 for t in activity.rerequest_times if inside(t)),
        "bot_comments_ignored": sum(1 for t in activity.bot_comment_times if inside(t)),
        "commit_after_review": any(i.via.startswith("review_") for i in chosen),
        "window_to": _stamp(up_to),
    }
    if after is not None:
        github["window_from"] = _stamp(after)
    block: dict[str, Any] = {
        "version": 1,
        "intent_method": METHOD,
        "events": events,
        "events_dropped": max(0, len(chosen) - MAX_EVENTS),
        "turn_ends": [],
        "not_available": dict(NOT_AVAILABLE),
        "environment": ENVIRONMENT,
        "environment_source": SOURCE,
        "github": github,
    }
    if activity.truncated:
        block["log_not_read_reason"] = "github_history_truncated"
    return block


def payload_block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    """The block as a Receipt carries it in sync: validated, with derived autonomy, without local-only keys."""
    from openshard.history import steering as view

    stored = view.stored_block({"steering": block})
    if stored is None:
        return None
    return view.sync_block({**stored, "autonomy": view.autonomy(stored)})


# ---------------------------------------------------------------------------
# GitHub CLI (read-only)
# ---------------------------------------------------------------------------


def _gh_list(repo_root: Path, path: str, runner: Runner) -> tuple[list[dict] | None, bool]:
    """Every item of a GitHub list endpoint (one JSON object per line), capped; None when unreadable."""
    try:
        proc = runner(
            ["gh", "api", "--paginate", "-H", "Accept: application/vnd.github+json", "--jq", ".[]",
             f"repos/{{owner}}/{{repo}}/{path}"],
            cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    if getattr(proc, "returncode", 1) != 0:
        return None, False
    items: list[dict] = []
    for line in str(getattr(proc, "stdout", "") or "").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items[:_MAX_ITEMS], len(items) > _MAX_ITEMS


def _pr_for_commit(repo_root: Path, sha: str, runner: Runner) -> int | None:
    prs, _ = _gh_list(repo_root, f"commits/{sha}/pulls", runner)
    if not prs:
        return None
    merged = [p for p in prs if p.get("merged_at")]
    number = (merged or prs)[0].get("number")  # the same choice the cloud-receipts workflow makes
    return number if isinstance(number, int) and number > 0 else None


def steering_for_commit(
    repo_root: Path, sha: str, *, pr: int | None = None, runner: Runner | None = None,
    handles: Iterable[str] = AGENT_HANDLES,
) -> tuple[dict[str, Any] | None, str]:
    """``(block, "")`` for commit *sha* of its pull request, or ``(None, reason)``. Never raises."""
    try:
        if not _SHA_RE.match(sha):
            return None, "not a commit id"
        if runner is None:
            if shutil.which("gh") is None:
                return None, "the GitHub CLI (gh) is not installed"
            runner = subprocess.run
        number = pr if isinstance(pr, int) and pr > 0 else _pr_for_commit(repo_root, sha, runner)
        if number is None:
            return None, "no pull request contains this commit"
        reads = {name: _gh_list(repo_root, f"{path}?per_page=100", runner) for name, path in (
            ("commits", f"pulls/{number}/commits"), ("reviews", f"pulls/{number}/reviews"),
            ("review_comments", f"pulls/{number}/comments"), ("issue_comments", f"issues/{number}/comments"),
            ("timeline", f"issues/{number}/timeline"),
        )}
        if any(items is None for name, (items, _) in reads.items() if name != "timeline"):
            return None, "GitHub did not return the pull request's reviews and comments"
        window = commit_window(reads["commits"][0] or [], sha)
        if window is None:
            return None, "the commit is not one of the pull request's commits"
        activity = reduce_activity(
            reviews=reads["reviews"][0] or [], review_comments=reads["review_comments"][0] or [],
            issue_comments=reads["issue_comments"][0] or [], timeline=reads["timeline"][0] or [],
            handles=handles, truncated=any(cut for _, cut in reads.values()),
        )
        del reads  # the only place comment text was held
        return build_block(activity, window), ""
    except Exception as exc:  # observational: never fail the caller
        return None, f"steering could not be derived ({type(exc).__name__})"


def main(argv: list[str] | None = None) -> int:
    """``python -m openshard.github.steering --commit SHA [--pr N]``: the payload block as JSON, or ``null``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m openshard.github.steering")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--pr", type=int, default=None)
    args = parser.parse_args(argv)
    block, reason = steering_for_commit(Path.cwd(), args.commit, pr=args.pr)
    if block is None:
        print(f"openshard: no GitHub steering for {args.commit[:12]}: {reason}", file=sys.stderr)
        print("null")
        return 0
    print(json.dumps(payload_block(block), separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the cloud-receipts workflow
    raise SystemExit(main())


__all__ = [
    "AGENT_HANDLES", "ENVIRONMENT", "NOT_AVAILABLE", "SOURCE", "Activity", "Input", "build_block", "commit_window",
    "main", "payload_block", "reduce_activity", "steering_for_commit",
]
