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
  - agent marks on the pull request's commits -> fail (an agent changed
    code and left no Receipt); the commits could not be read -> fail;
  - team policy ``agent_prs`` -> pass, labelled as a human change;
  - team policy ``all_prs`` -> fail.
* Any error (unreachable, credentials refused, fork, timeout, malformed
  answer, not a commit) -> fail with its reason. Never a silent pass.

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


def _yes_no(value: object) -> str:
    return "yes" if value is True else "no" if value is False else UNKNOWN


def _cost(cost: object) -> str:
    if not isinstance(cost, dict):
        return UNKNOWN
    usd = cost.get("usd")
    if not isinstance(usd, int | float) or isinstance(usd, bool) or usd < 0:
        return UNKNOWN
    source = cost.get("source")
    amount = f"${usd:.2f}" if usd >= 0.01 else f"${usd:.4f}"
    return f"{amount} ({source})" if isinstance(source, str) and source else f"{amount} (source unknown)"


def _lines(receipt: dict) -> str:
    added, removed = _count(receipt.get("lines_added")), _count(receipt.get("lines_removed"))
    if added is None and removed is None:
        return UNKNOWN
    return f"+{added if added is not None else UNKNOWN} / -{removed if removed is not None else UNKNOWN}"


def _verification(block: object) -> str:
    if not isinstance(block, dict):
        return UNKNOWN
    raw = block.get("result")
    text = raw if isinstance(raw, str) and raw else UNKNOWN
    at = block.get("verified_at")
    bound = block.get("bound_commit")
    text += f", at {at}" if isinstance(at, str) and at else ", time unknown"
    text += f", bound to commit {_short(bound)}" if isinstance(bound, str) and bound else ", bound commit unknown"
    return text


def _approvals(value: object) -> str:
    if value is None:
        return UNKNOWN
    if not isinstance(value, list):
        return UNKNOWN
    if not value:
        return "none recorded"
    parts = []
    for item in value[:_MAX_APPROVALS]:
        if isinstance(item, dict):
            parts.append(", ".join(f"{k}: {v}" for k, v in item.items() if isinstance(v, str | int | float | bool)))
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
    """Markdown for one Receipt from the gate verdict: every field the Platform sent, unknown as unknown."""
    rid = receipt.get("receipt_id") if isinstance(receipt.get("receipt_id"), str) else None
    title = f"#### Receipt {_cell(rid)}"
    url = receipt.get("url")
    if isinstance(url, str) and _URL_RE.match(url):
        title += f" · [open in Openshard]({url})"
    rows = [
        ("Status", receipt.get("status")),
        ("Agent", receipt.get("agent")),
        ("Model", receipt.get("model")),
        ("Task", receipt.get("task")),
        ("Files changed", _count(receipt.get("files_changed"))),
        ("Lines", _lines(receipt)),
        ("Verification", _verification(receipt.get("verification"))),
        ("Cost", _cost(receipt.get("cost"))),
        ("Approvals", _approvals(receipt.get("approvals"))),
        ("Platform-witnessed", _yes_no(receipt.get("witnessed"))),
        ("Signature verified", _yes_no(receipt.get("signature_verified"))),
    ]
    merge = _merge_check(receipt.get("merge_check"))
    if merge:
        rows.append(("Merge check (informational)", merge))
    out = [title, "", "| | |", "| --- | --- |"]
    out += [f"| {name} | {_cell(value)} |" for name, value in rows]
    checks = receipt.get("checks")
    out.append("")
    if isinstance(checks, list) and checks:
        out += ["| Check | Outcome | Source |", "| --- | --- | --- |"]
        for check in checks[:_MAX_CHECKS]:
            c = check if isinstance(check, dict) else {}
            out.append(f"| {_cell(c.get('name'))} | {_cell(c.get('outcome'))} | {_cell(c.get('source'))} |")
        if len(checks) > _MAX_CHECKS:
            out.append(f"| … {len(checks) - _MAX_CHECKS} more | | |")
    elif isinstance(checks, list):
        out.append("Checks: none recorded.")
    else:
        out.append("Checks: unknown.")
    if receipt.get("witnessed") is True:
        out += ["", "The Platform witnessed this Receipt's capture, so a later rewrite of the local record is "
                    "detectable. Being witnessed is not being verified."]
    return out


def decide(
    sha: str,
    platform: dict[str, Any] | PlatformError,
    *,
    local_verdict: str | None = None,
    agent_marks: list[str] | None = None,
) -> GateResult:
    """The gate outcome for *sha*. *agent_marks* is None when the commits could not be read. Never raises."""
    receipts: list[dict] = []
    answer = platform.last if isinstance(platform, PlatformError) else platform
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
        elif agent_marks is None:
            status, headline = STATUS_FAIL, ("Failed: no Receipt for this commit, and the pull request's commits "
                                             "could not be read to tell whether an agent made them "
                                             "(check out with `fetch-depth: 0`).")
        elif agent_marks:
            names = ", ".join(agent_marks[:_MAX_MARKS])
            status, headline = STATUS_FAIL, f"Failed: an agent changed code but there is no Receipt (found {names})."
        elif policy == "agent_prs":
            status, headline = STATUS_PASS, HUMAN_CHANGE
        else:
            status, headline = STATUS_FAIL, ("Failed: no Receipt for this commit, and the team requires one for "
                                             "every pull request.")

    title = "passed" if status == STATUS_PASS else "failed"
    lines = [f"### Openshard gate: {title}", "", f"Commit `{_short(sha)}`. {_cell(headline)}"]
    if isinstance(answer, dict):
        receipts = [r for r in answer.get("receipts") or [] if isinstance(r, dict)]
        lines += ["", f"Platform answer: {_cell(answer.get('status'))}, checked {_cell(answer.get('checked_at'))}."]
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
    lines += ["", "Unknown means the Platform does not record it, never zero."]
    return GateResult(
        status=status,
        exit_code=0 if status == STATUS_PASS else 1,
        verdict=verdict,
        headline=headline,
        summary_markdown="\n".join(lines) + "\n",
        reasons=[] if status == STATUS_PASS else [headline],
    )


__all__ = ["HUMAN_CHANGE", "STATUS_FAIL", "STATUS_PASS", "GateResult", "decide", "render_receipt"]
