"""Read-only inventory of GitHub cloud-capture evidence for historical Receipt recovery.

For every Openshard Cloud Receipts workflow run it reconstructs which commits
the run inspected and what explicit Openshard metadata each carried (commit
trailers first, then the PR body, as the workflow reads them). It also derives
the deterministic hosted Receipt id and lists the independent recovery evidence
(Claude Code session records linked from the commit) that exists for it.

Inputs (all gathered read-only beforehand; see docs/receipt-recovery-2026-10-10.md):
  <dir>/<repo>-runs.tsv     workflow runs: id, event, branch, sha, conclusion, created_at, attempt
  <dir>/<repo>-prs.jsonl    pull requests: number, head, merge_sha, merged_at, created_at, body
  <dir>/sessions.json       Claude Code session records: id, created, updated, model, served, repos, usage

Usage:
  python -I scripts/receipt_recovery_github_evidence.py DIR CHECKOUTS_ROOT

Nothing is written except DIR/github_evidence.json. Stated limits: PR bodies
are read as they are now, and the commit set of a multi-commit push is
reconstructed from git ancestry rather than read from the push event.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# GitHub repository ids of the repositories the cloud workflow captures.
REPO_IDS = {"openshard": "1204966204", "platform": "1371280725"}
KEYS = {
    "agent": "Openshard-Agent", "model": "Openshard-Model", "provider": "Openshard-Provider",
    "surface": "Openshard-Surface", "cost": "Openshard-Cost-USD",
    "tokens_in": "Openshard-Tokens-Input", "tokens_out": "Openshard-Tokens-Output",
}
SESSION = re.compile(r"claude\.ai/code/(session_[A-Za-z0-9]+)")
_TAGGED_ONLY = 'select((.message // "") | test('


def receipt_id(repo_id: str, sha: str) -> str:
    """The hosted id the Platform derives for a GitHub cloud Receipt (``github-captures.ts``)."""
    return "rcpt_" + hashlib.sha256(f"github-cloud:\0{repo_id}\0{sha}".encode()).hexdigest()[:32]


def trailer(text: str | None, key: str) -> str:
    """The last ``Key: value`` line, case-insensitive, as the workflow's ``sed`` reads it."""
    found = re.findall(rf"^{re.escape(key)}:[ \t]*(.*)$", text or "", re.M | re.I)
    return found[-1].strip() if found else ""


def metadata(message: str, body: str, *, tagged_only: bool) -> dict[str, str]:
    """Declared metadata: commit trailers win, the PR body fills gaps unless the workflow read only commits."""
    return {k: trailer(message, key) or ("" if tagged_only else trailer(body, key)) for k, key in KEYS.items()}


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def session_evidence(session: dict[str, Any], repo: str, committed_at: str) -> dict[str, Any]:
    """How one linked Claude Code session relates to a commit. Never asserts more than the record shows."""
    return {
        "id": session["id"],
        "model": session["model"],
        "served": session["served"],
        "window": [session["created"], session["updated"]],
        "commit_in_window": _ts(session["created"]) <= _ts(committed_at) <= _ts(session["updated"]),
        "repo_in_scope": f"openshard/{repo}" in session["repos"],
        "usage_session_total": session["usage"],
    }


def consistent(evidence: dict[str, Any]) -> bool:
    """True when a session can stand for a commit's model: in window, in scope, no recorded fallback."""
    return bool(evidence["commit_in_window"] and evidence["repo_in_scope"] and evidence["served"] in (None, evidence["model"]))


def _git(root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return result.returncode, result.stdout


def build(inventory: Path, checkouts: Path) -> dict[str, Any]:
    sessions = {s["id"]: s for s in json.loads((inventory / "sessions.json").read_text())}
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"repos": {}, "limits": [
        "PR bodies are current, not as of the run.",
        "Multi-commit push commit sets are reconstructed from git ancestry.",
        "Delivery outcomes (created / CI attached / rejected) need production read access or per-run logs.",
    ]}
    for repo in REPO_IDS:
        root = checkouts / repo
        fields = ["id", "event", "branch", "sha", "conclusion", "at", "attempt"]
        runs = sorted((dict(zip(fields, line.split("\t"), strict=False))
                       for line in (inventory / f"{repo}-runs.tsv").read_text().splitlines() if line.strip()),
                      key=lambda r: r["at"])
        prs = [json.loads(line) for line in (inventory / f"{repo}-prs.jsonl").read_text().splitlines() if line.strip()]
        by_merge = {p["merge_sha"]: p for p in prs if p.get("merged_at") and p.get("merge_sha")}
        by_head: dict[str, list[dict]] = collections.defaultdict(list)
        for pr in prs:
            by_head[pr["head"]].append(pr)
        last_head: dict[str, str] = {}
        seen: dict[str, dict[str, Any]] = {}
        for run in runs:
            head = run["sha"]
            yml = _git(root, "show", f"{head}:.github/workflows/openshard-cloud-receipts.yml")[1]
            tagged_only = _TAGGED_ONLY in yml
            if run["event"] == "workflow_dispatch":
                # The dispatch target is an input, which the runs API does not return.
                shas = [] if "commit_sha" in yml else [head]
            else:
                prev = last_head.get(run["branch"])
                if prev and _git(root, "merge-base", "--is-ancestor", prev, head)[0] == 0:
                    shas = _git(root, "rev-list", "--no-merges", f"{prev}..{head}")[1].split()
                else:
                    base = _git(root, "merge-base", head, "origin/main")[1].strip()
                    shas = (_git(root, "rev-list", "--no-merges", f"{base}..{head}")[1].split()[:20]
                            if base and base != head else []) or [head]
                last_head[run["branch"]] = head
            for sha in shas:
                message = _git(root, "show", "-s", "--format=%B", sha)[1]
                if tagged_only and not trailer(message, "Openshard-Agent"):
                    continue  # this workflow version never inspected untagged commits
                pr = by_merge.get(sha) or max(by_head.get(run["branch"], []), key=lambda p: p["created_at"], default=None)
                body = (pr or {}).get("body") or ""
                committed_at = _git(root, "show", "-s", "--format=%cI", sha)[1].strip()
                entry = seen.setdefault(sha, {
                    "repo": repo, "sha": sha, "branch": run["branch"], "pr": (pr or {}).get("number"),
                    "committed_at": committed_at, "receipt_id": receipt_id(REPO_IDS[repo], sha),
                    "meta": metadata(message, body, tagged_only=tagged_only),
                    "sessions": sorted(set(SESSION.findall(message)) | set(SESSION.findall(body))), "runs": [],
                })
                entry["runs"].append({"id": run["id"], "event": run["event"], "conclusion": run["conclusion"]})
        for entry in seen.values():
            entry["expected"] = "receipt_or_ci_attachment" if entry["meta"]["agent"] else "skipped_no_agent"
            entry["session_evidence"] = [session_evidence(sessions[s], repo, entry["committed_at"])
                                         for s in entry["sessions"] if s in sessions]
        mine = list(seen.values())
        rows += mine
        tagged = [e for e in mine if e["meta"]["agent"]]
        summary["repos"][repo] = {
            "workflow_runs": len(runs),
            "commits_inspected": len(mine),
            "expected_receipts_or_attachments": len(tagged),
            "skipped_no_agent": len(mine) - len(tagged),
            "of_expected": {
                "agent": collections.Counter(e["meta"]["agent"] for e in tagged).most_common(),
                "with_model": sum(1 for e in tagged if e["meta"]["model"]),
                "with_tokens": sum(1 for e in tagged if e["meta"]["tokens_in"] or e["meta"]["tokens_out"]),
                "with_cost": sum(1 for e in tagged if e["meta"]["cost"]),
                "with_claude_session_link": sum(1 for e in tagged if e["sessions"]),
            },
            "skipped_with_claude_session_link": sum(1 for e in mine if not e["meta"]["agent"] and e["sessions"]),
        }
    return {"summary": summary, "commits": rows}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    inventory, checkouts = Path(argv[1]), Path(argv[2])
    result = build(inventory, checkouts)
    (inventory / "github_evidence.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps(result["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
