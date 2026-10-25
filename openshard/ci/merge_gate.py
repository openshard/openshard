"""Pure decision for the Openshard merge gate (the root ``action.yml``).

No I/O. Takes the Platform's gate verdict for a pull request's head commit
(``openshard.gate_verdict.v1``, read by ``ci/gate_platform.py``) or the
reason it could not be read, this runner's own trusted verdict (if any),
and the agent marks on the pull request's commits, and returns pass or
fail plus the markdown for the job summary. There is no third outcome.

Decision (docs/merge-gate.md):

* Platform ``verified`` -> pass; ``failed`` -> fail.
* ``pending`` never reaches here: the caller polls, and a timeout is an
  error.
* ``none`` (no Receipt for the commit):
  - this runner's trusted local evidence decides when it is conclusive;
  - agent marks on the pull request's commits -> the caller keeps asking
    within ``wait-minutes`` (the Receipt may still be on its way), then
    fail (an agent changed code and left no Receipt); the commits could
    not be read -> fail;
  - team policy ``agent_prs`` -> pass, labelled as a human change;
  - team policy ``all_prs`` -> fail.
* Any error (unreachable, credentials refused, fork, timeout, malformed
  answer, not a commit) -> fail with its reason. Never a silent pass.

PR findings (``verification/pr_findings.py``: size, junk files, edits to
the change's own grader, removed code, new dependencies) are shown under
"Change checks" for information. They do not change pass or fail.

Wording rules: unknown is shown as unknown, never zero, and a Receipt is
called tamper-evident only when the Platform says it witnessed it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from openshard.ci.gate_platform import PlatformError

STATUS_PASS = "pass"
STATUS_FAIL = "fail"

UNKNOWN = "unknown"
HUMAN_CHANGE = "Human change — no agent activity recorded."
_MAX_CELL = 160
_MAX_RECEIPTS = 10
_MAX_CHECKS = 50
_MAX_APPROVALS = 10
_MAX_MARKS = 5

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]#!|~$])")
_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%/+=&?:@-]*)?$")


@dataclass(frozen=True)
class GateResult:
    status: str
    exit_code: int
    verdict: str  # the Platform status, or the error kind
    headline: str
    summary_markdown: str = ""
    reasons: list[str] = field(default_factory=list)


def _cell(value: object) -> str:
    """*value* as one bounded table cell: no control characters, no HTML, markdown escaped."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return UNKNOWN
    text = " ".join(_CONTROL_RE.sub(" ", str(value)).split())
    if len(text) > _MAX_CELL:
        text = text[: _MAX_CELL - 1] + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MD_SPECIAL_RE.sub(r"\\\1", text)


def _short(sha: object) -> str:
    return sha[:12] if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,64}", sha) else UNKNOWN


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _words(value: object) -> str | None:
    """A recorded machine label in plain words (``official_rate_estimate`` -> ``official rate estimate``)."""
    return " ".join(value.replace("_", " ").split()) if isinstance(value, str) and value.strip() else None


def _cost(cost: object) -> str:
    """As the receipt card writes it: ``$0.4213`` under a dollar, ``$1.20`` above, ``~`` for an estimate."""
    if not isinstance(cost, dict):
        return UNKNOWN
    usd = cost.get("usd")
    if not isinstance(usd, int | float) or isinstance(usd, bool) or usd < 0:
        return UNKNOWN
    source = _words(cost.get("source"))
    estimate = "~" if source and "estimate" in source else ""
    amount = f"{estimate}${usd:.4f}" if usd < 1 else f"{estimate}${usd:.2f}"
    return f"{amount} · {source}" if source else f"{amount} · source unknown"


def _changes(receipt: dict) -> str:
    files = _count(receipt.get("files_changed"))
    added, removed = _count(receipt.get("lines_added")), _count(receipt.get("lines_removed"))
    if files is None and added is None and removed is None:
        return UNKNOWN
    parts = [f"{files} file{'' if files == 1 else 's'}" if files is not None else "files unknown"]
    if added is None and removed is None:
        parts.append("lines unknown")
    else:
        parts.append(f"{f'+{added}' if added is not None else '+unknown'} / "
                     f"{f'−{removed}' if removed is not None else '−unknown'}")
    return " · ".join(parts)


_STATUS_LABELS = {"verified": "Verified", "failed": "Verification failed", "pending": "Pending"}


def _result(receipt: dict) -> str:
    """The Receipt's status, then what its verification block says, once."""
    raw = receipt.get("status")
    status = _STATUS_LABELS.get(raw, raw) if isinstance(raw, str) and raw else UNKNOWN
    block = receipt.get("verification")
    if not isinstance(block, dict):
        return f"{status} · verification unknown"
    parts = [status]
    result = block.get("result")
    if not isinstance(result, str) or not result:
        parts.append("verification unknown")
    elif result != raw:
        parts.append(f"verification {_words(result)}")
    bound = block.get("bound_commit")
    parts.append(f"bound to commit {_short(bound)}" if isinstance(bound, str) and bound else "bound commit unknown")
    at = block.get("verified_at")
    parts.append(f"at {at}" if isinstance(at, str) and at else "time unknown")
    return " · ".join(parts)


def _checks(checks: object) -> str:
    """``2/2 passed``, or each outcome named: ``1 passed · 1 failed``. Never a bare count."""
    if not isinstance(checks, list):
        return UNKNOWN
    if not checks:
        return "none recorded"
    counts: dict[str, int] = {}
    for check in checks:
        outcome = check.get("outcome") if isinstance(check, dict) else None
        key = _words(outcome) or UNKNOWN
        counts[key] = counts.get(key, 0) + 1
    if list(counts) == ["passed"]:
        return f"{len(checks)}/{len(checks)} passed"
    failed = [c["name"] for c in checks
              if isinstance(c, dict) and c.get("outcome") == "failed" and isinstance(c.get("name"), str)]
    named = f" ({', '.join(failed[:5])})" if failed else ""
    return " · ".join(f"{n} {name}{named if name == 'failed' else ''}" for name, n in counts.items())


def _witnessed(receipt: dict) -> str:
    """Whether the Platform witnessed the capture, and whether its signature verified."""
    witnessed = receipt.get("witnessed")
    head = "Yes" if witnessed is True else "No" if witnessed is False else UNKNOWN
    signed = receipt.get("signature_verified")
    seal = ("signature verified" if signed is True else "signature not verified" if signed is False
            else "signature unknown")
    return f"{head} · {seal}"


def _approvals(value: object) -> str:
    if not isinstance(value, list):
        return UNKNOWN
    if not value:
        return "none recorded"
    parts = []
    for item in value[:_MAX_APPROVALS]:
        if isinstance(item, dict):
            parts.append(", ".join(
                f"{_words(k)}: {'yes' if v is True else 'no' if v is False else _words(v) if isinstance(v, str) else v}"
                for k, v in item.items() if isinstance(v, str | int | float | bool)))
        else:
            parts.append(str(item))
    more = f" (+{len(value) - _MAX_APPROVALS} more)" if len(value) > _MAX_APPROVALS else ""
    return "; ".join(parts) + more


def _merge_check(block: object) -> str | None:
    """One informational line from a ``merge_check`` block; never a gate criterion."""
    from openshard.history.merge_check import normalize

    norm = normalize(block)
    if norm is None:
        return None
    conflicts = norm.get("conflicts")
    base = norm["base_ref"]
    if conflicts is None:
        text = f"conflicts with {base}: unknown"
    elif conflicts:
        text = f"{len(conflicts)} conflicting path(s) with {base}"
    else:
        text = f"no conflicts with {base}"
    if norm.get("behind_by") is not None:
        text += f", {norm['behind_by']} commit(s) behind"
    return f"{text} (checked {norm['checked_at']})"


def render_receipt(receipt: dict) -> list[str]:
    """Markdown for one Receipt, laid out like the receipt card: key fields, then the evidence folded.

    Every field the Platform sent appears once; anything it did not record reads ``unknown``.
    """
    rid = receipt.get("receipt_id") if isinstance(receipt.get("receipt_id"), str) else None
    raw = receipt.get("status")
    label = _STATUS_LABELS.get(raw, raw) if isinstance(raw, str) and raw else UNKNOWN
    rows: list[tuple[str, object]] = [
        ("Result", _result(receipt)),
        ("Agent", receipt.get("agent")),
        ("Model", receipt.get("model")),
    ]
    if isinstance(receipt.get("authorship"), str) and receipt["authorship"].strip():
        rows.append(("Authorship", _words(receipt["authorship"])))
    rows += [
        ("Task", receipt.get("task")),
        ("Changes", _changes(receipt)),
        ("Checks", _checks(receipt.get("checks"))),
        ("Cost", _cost(receipt.get("cost"))),
        ("Witnessed", _witnessed(receipt)),
    ]
    out = [f"#### Receipt {_cell(rid)} · {_cell(label)}", "", "| | |", "| --- | --- |"]
    out += [f"| {name} | {_cell(value)} |" for name, value in rows]
    url = receipt.get("url")
    out.append(f"| Receipt | [Open in Openshard]({url}) |" if isinstance(url, str) and _URL_RE.match(url)
               else "| Receipt | no link |")
    if receipt.get("witnessed") is True:
        out += ["", "The Platform witnessed this Receipt's capture, so a later rewrite of the local record is "
                    "detectable. Being witnessed is not being verified."]

    checks = receipt.get("checks")
    n = len(checks) if isinstance(checks, list) else None
    out += ["", f"<details><summary>Checks ({n if n is not None else UNKNOWN}) and approvals</summary>", ""]
    if isinstance(checks, list) and checks:
        out += ["| Check | Outcome | Source |", "| --- | --- | --- |"]
        for check in checks[:_MAX_CHECKS]:
            c = check if isinstance(check, dict) else {}
            out.append(f"| {_cell(c.get('name'))} | {_cell(_words(c.get('outcome')))} | "
                       f"{_cell(_words(c.get('source')))} |")
        if len(checks) > _MAX_CHECKS:
            out.append(f"| … {len(checks) - _MAX_CHECKS} more | | |")
    else:
        out.append(f"Checks: {'none recorded' if isinstance(checks, list) else UNKNOWN}.")
    out += ["", "| | |", "| --- | --- |", f"| Approvals | {_cell(_approvals(receipt.get('approvals')))} |"]
    merge = _merge_check(receipt.get("merge_check"))
    if merge:
        out.append(f"| Merge check (informational) | {_cell(merge)} |")
    out += ["", "</details>"]
    return out


_NOT_GIVEN: Any = object()
_MAX_FINDINGS = 10


def render_findings(findings: list[dict] | None) -> list[str]:
    """Markdown for the PR findings: what was seen, for information only."""
    head = ["#### Change checks", "", "Information only: these do not change this result."]
    if findings is None:
        return head + ["", "Not checked: the base commit was unknown or could not be read."]
    if not findings:
        return head + ["", "Nothing found."]
    out = head + ["", "| Check | What was seen | Where |", "| --- | --- | --- |"]
    for f in findings[:_MAX_FINDINGS]:
        paths, names = f.get("paths"), f.get("names")
        listed: list = paths if isinstance(paths, list) and paths else names if isinstance(names, list) else []
        where = ", ".join(str(p) for p in listed[:5]) or "-"
        out.append(f"| {_cell(_words(f.get('kind')))} | {_cell(f.get('detail'))} | {_cell(where)} |")
    return out


def decide(
    sha: str,
    platform: dict[str, Any] | PlatformError,
    *,
    local_verdict: str | None = None,
    agent_marks: list[str] | None = None,
    waited_minutes: float | None = None,
    change_findings: list[dict] | None = _NOT_GIVEN,
    carried: dict[str, Any] | None = None,
) -> GateResult:
    """The gate outcome for *sha*. Never raises.

    *agent_marks* is None when the commits could not be read; *waited_minutes*
    is how long the caller waited for an agent change's Receipt.
    *change_findings* are the PR findings (None: not checked); shown, never decisive.
    *carried* is set when *sha* only merges the base branch into an agent's work
    (``ci/base_merges.py``): ``agent_commit``, ``merges`` and the Platform's
    ``platform`` answer for the agent's last commit. It is used only when the
    Platform has no Receipt for *sha* itself.
    """
    receipts: list[dict] = []
    answer = platform.last if isinstance(platform, PlatformError) else platform
    carried_answer = carried.get("platform") if isinstance(carried, dict) else None
    carried_status = carried_answer.get("status") if isinstance(carried_answer, dict) else None
    agent_sha = carried.get("agent_commit") if isinstance(carried, dict) else None
    later = len(carried.get("merges") or []) if isinstance(carried, dict) else 0
    if isinstance(platform, PlatformError):
        verdict, status = platform.kind, STATUS_FAIL
        headline = f"Failed: {platform.message}"
    else:
        verdict = str(platform.get("status"))
        receipts = [r for r in platform.get("receipts") or [] if isinstance(r, dict)]
        policy = (platform.get("policy") or {}).get("require_receipt")
        if verdict == "verified":
            status, headline = STATUS_PASS, "Verified: the Platform's evidence for this commit says its checks passed."
        elif verdict == "failed":
            status, headline = STATUS_FAIL, "Failed: verification failed for this commit."
        elif verdict == "pending":
            status, headline = STATUS_FAIL, "Failed: verification had not finished when the gate stopped waiting."
        elif local_verdict == "verified":
            status, headline = STATUS_PASS, ("Verified by evidence on this runner (the Platform has no Receipt "
                                             "for this commit).")
        elif local_verdict == "failed":
            status, headline = STATUS_FAIL, "Failed: evidence on this runner says verification failed."
        elif carried_status in ("verified", "failed", "pending"):
            # The head only merges the base branch in, cleanly and with no agent marks: the agent's
            # change is judged on its own last commit, whose Receipt the Platform holds.
            answer, verdict = carried_answer, str(carried_status)
            merged = f"{later} later commit{'' if later == 1 else 's'} only merge{'s' if later == 1 else ''} the base branch in"
            if carried_status == "verified":
                status, headline = STATUS_PASS, (f"Verified: the agent's last commit {_short(agent_sha)} is verified, "
                                                 f"and the {merged}, cleanly and with no agent marks.")
            elif carried_status == "failed":
                status, headline = STATUS_FAIL, f"Failed: verification failed for the agent's last commit {_short(agent_sha)}."
            else:
                status, headline = STATUS_FAIL, (f"Failed: verification of the agent's last commit {_short(agent_sha)} "
                                                 "had not finished when the gate stopped waiting.")
        elif agent_marks is None:
            status, headline = STATUS_FAIL, ("Failed: no Receipt for this commit, and the pull request's commits "
                                             "could not be read to tell whether an agent made them "
                                             "(check out with `fetch-depth: 0`).")
        elif agent_marks:
            names = ", ".join(agent_marks[:_MAX_MARKS])
            waited = f" The gate waited {waited_minutes:g} min for one." if waited_minutes else ""
            status, headline = STATUS_FAIL, (f"Failed: an agent changed code but there is no Receipt "
                                             f"(found {names}).{waited}")
        elif policy == "agent_prs":
            status, headline = STATUS_PASS, HUMAN_CHANGE
        else:
            status, headline = STATUS_FAIL, ("Failed: no Receipt for this commit, and the team requires one for "
                                             "every pull request.")

    title = "Passed" if status == STATUS_PASS else "Failed"
    # The heading already says Failed; the reason is not prefixed with it again.
    reason = headline[len("Failed: "):] if headline.startswith("Failed: ") else headline
    lines = [f"### Openshard gate · {title}", "", _cell(reason[:1].upper() + reason[1:])]
    meta = [f"Commit `{_short(sha)}`"]
    if answer is carried_answer and carried_answer is not None:
        meta = [f"Commit `{_short(sha)}` merges the base branch in",
                f"evidence for the agent's last commit `{_short(agent_sha)}`"]
    if isinstance(answer, dict):
        meta += [f"Platform answer {_cell(answer.get('status'))}", f"checked {_cell(answer.get('checked_at'))}"]
    lines += ["", " · ".join(meta)]
    if isinstance(answer, dict):
        receipts = [r for r in answer.get("receipts") or [] if isinstance(r, dict)]
        group = answer.get("merge_group")
        if isinstance(group, dict):
            lines += ["", f"Merge queue: PR \\#{_cell(group.get('pull_request'))} head `{_short(group.get('head_sha'))}` "
                          f"(queue commit `{_short(group.get('requested_sha'))}`)."]
        if answer.get("status") == "none" and isinstance(answer.get("reason"), str):
            lines += ["", f"Platform reason: {_cell(answer['reason'])}"]
        for receipt in receipts[:_MAX_RECEIPTS]:
            lines += [""] + render_receipt(receipt)
        if len(receipts) > _MAX_RECEIPTS:
            lines += ["", f"{len(receipts) - _MAX_RECEIPTS} more Receipt(s) not shown."]
    if change_findings is not _NOT_GIVEN:
        lines += [""] + render_findings(change_findings)
    lines += ["", "<sub>Unknown means the Platform does not record it, never zero.</sub>"]
    return GateResult(
        status=status,
        exit_code=0 if status == STATUS_PASS else 1,
        verdict=verdict,
        headline=headline,
        summary_markdown="\n".join(lines) + "\n",
        reasons=[] if status == STATUS_PASS else [headline],
    )


__all__ = ["HUMAN_CHANGE", "STATUS_FAIL", "STATUS_PASS", "GateResult", "decide", "render_findings", "render_receipt"]
