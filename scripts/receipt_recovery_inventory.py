"""Read-only completeness inventory and dry-run recovery plan for hosted Receipts.

Usage:
  OPENSHARD_API_KEY=osk_... OPENSHARD_ORG_ID=<org> \
    python -I scripts/receipt_recovery_inventory.py OUT_DIR [--evidence github_evidence.json]
  python -I scripts/receipt_recovery_inventory.py OUT_DIR --from-file receipts.json [--evidence ...]

The evidence file comes from scripts/receipt_recovery_github_evidence.py.

Only GET requests are issued. Nothing is written to production. The plan lists
what *could* be attached as later evidence, with the evidence and the reason
it is trustworthy; it never proposes changing a sealed Receipt.

Field rules (the dashboard's own rules):
- "unknown", "not recorded", "none", "" and the model placeholders "auto" and
  "default" are not values.
- A missing number is unknown, never zero. A recorded 0 counts as recorded.
- Later usage (`usage_current`) wins over the Receipt's own fields when its
  status is observed/reconciled/estimated.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

NOT_RECORDED = {"", "not recorded", "unknown", "none"}
MODEL_PLACEHOLDERS = NOT_RECORDED | {"auto", "default"}
KNOWN_USAGE = {"observed", "reconciled", "estimated"}
UNATTRIBUTED = {("github observed cloud work", "github_observed"), ("unknown", "github_observed")}


def recorded(value, placeholders=NOT_RECORDED):
    return isinstance(value, str) and value.strip().lower() not in placeholders


def fetch_all(base, org, key):
    receipts, cursor = [], None
    while True:
        query = {"limit": "200", **({"cursor": cursor} if cursor else {})}
        url = f"{base}/v1/orgs/{org}/receipts?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            page = json.load(response)
        receipts += page["receipts"]
        cursor = page.get("next_cursor")
        if not cursor:
            return receipts


def classify(view):
    r = view["receipt"]
    usage = (view.get("usage_current") or {}).get("usage") or {}
    later_tokens = (usage.get("tokens") or {}).get("status") in KNOWN_USAGE
    later_cost = (usage.get("cost") or {}).get("status") in KNOWN_USAGE
    later_model = recorded((usage.get("model") or {}).get("id"), MODEL_PLACEHOLDERS)

    agent_label = (r.get("agent") or "").strip()
    unattributed = (agent_label.lower(), r.get("origin")) in UNATTRIBUTED or (
        agent_label.lower() == "openshard" and r.get("origin") != "openshard_routed")
    tokens_own = any(r.get(k) is not None for k in ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_creation"))
    cost_own = r.get("cost_usd") is not None

    if later_cost:
        cost_kind = (usage["cost"].get("kind") or usage["cost"].get("source") or "later_evidence")
    elif cost_own:
        cost_kind = "estimate" if r.get("cost_is_estimate") else (r.get("cost_provenance") or "unlabelled")
    else:
        cost_kind = "unknown"

    current = (view.get("verification_current") or {}).get("state") or {}
    block = r.get("verification") or {}
    if current.get("state") in ("verified_passed", "verified_failed"):
        verification = "independently_verified"
    elif block.get("status") in ("passed", "failed") and block.get("complete") is True:
        verification = "complete_session"
    elif block.get("status") in ("passed", "failed", "partial") or r.get("verification_status") in ("passed", "failed"):
        verification = "partial_or_claimed"
    else:
        verification = "none"

    return {
        "receipt_id": r["receipt_id"],
        "created_at": r.get("created_at"),
        "source_product": (view.get("source") or {}).get("product"),
        "origin": r.get("origin"),
        "agent": None if unattributed or not recorded(agent_label) else agent_label,
        "repo_identity": r.get("repo_identity"),
        "commit": r.get("commit"),
        "run_id": r.get("run_id"),
        "has_model": later_model or recorded(r.get("model"), MODEL_PLACEHOLDERS),
        "has_tokens": later_tokens or tokens_own,
        "has_cost": later_cost or cost_own,
        "cost_kind": cost_kind,
        "verification": verification,
        "has_files": r.get("files_changed") is not None or bool(r.get("files")),
        "capture_completeness": (r.get("capture_completeness") or {}).get("status"),
    }


def plan(rows, evidence):
    """Dry-run actions from independent evidence. Never proposes rewriting a Receipt."""
    by_commit = {}
    if evidence:
        for e in evidence["commits"]:
            by_commit[(f"github.com/openshard/{e['repo']}", e["sha"])] = e
    actions, unrecoverable = [], collections.Counter()
    for row in rows:
        e = by_commit.get((row["repo_identity"], row["commit"]))
        sessions = [s for s in (e or {}).get("session_evidence", [])
                    if s["commit_in_window"] and s["repo_in_scope"] and s["served"] in (None, s["model"])]
        if not row["has_model"]:
            if len(sessions) == 1:
                actions.append({"receipt_id": row["receipt_id"], "field": "model", "value": sessions[0]["model"],
                                "evidence": f"Claude Code session {sessions[0]['id']}", "action": "attach_usage_evidence(model only)",
                                "why": "commit links this session; commit time inside session window; repo in session scope; no model fallback recorded"})
            else:
                unrecoverable["model: no consistent independent evidence"] += 1
        if not row["has_tokens"]:
            unrecoverable["tokens: session totals are not attributable per Receipt" if sessions else "tokens: no provider record"] += 1
        if not row["has_cost"]:
            unrecoverable["cost: no billed/runtime record; estimate needs tokens + exact model"] += 1
        if row["agent"] is None:
            if len(sessions) == 1:
                actions.append({"receipt_id": row["receipt_id"], "field": "agent", "value": "Claude Code",
                                "evidence": f"Claude Code session {sessions[0]['id']}", "action": "report_only",
                                "why": "agent is a sealed field; record as declared correlation evidence, never rewrite"})
            else:
                unrecoverable["agent: no session or metadata evidence"] += 1
    return actions, unrecoverable


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[1])
    args = argv[2:]
    evidence = json.loads(Path(args[args.index("--evidence") + 1]).read_text()) if "--evidence" in args else None
    out.mkdir(parents=True, exist_ok=True)
    if "--from-file" in args:
        views = json.loads(Path(args[args.index("--from-file") + 1]).read_text())
    else:
        views = fetch_all(os.environ.get("OPENSHARD_API_BASE", "https://api.openshard.dev"),
                          os.environ["OPENSHARD_ORG_ID"], os.environ["OPENSHARD_API_KEY"])
        (out / "receipts.json").write_text(json.dumps(views))
    rows = [classify(v) for v in views]
    n = len(rows)
    stats = {
        "total": n,
        "with_model": sum(r["has_model"] for r in rows),
        "with_tokens": sum(r["has_tokens"] for r in rows),
        "with_cost": sum(r["has_cost"] for r in rows),
        "cost_kind": collections.Counter(r["cost_kind"] for r in rows).most_common(),
        "verification": collections.Counter(r["verification"] for r in rows).most_common(),
        "missing_attribution": sum(r["agent"] is None for r in rows),
        "with_files": sum(r["has_files"] for r in rows),
        "by_source": collections.Counter(r["source_product"] for r in rows).most_common(),
        "by_agent": collections.Counter(r["agent"] or "(not identified)" for r in rows).most_common(),
        "fully_complete": sum(r["has_model"] and r["has_tokens"] and r["has_cost"] and r["agent"] is not None
                              and r["verification"] in ("independently_verified", "complete_session") for r in rows),
    }
    actions, unrecoverable = plan(rows, evidence)
    (out / "inventory.json").write_text(json.dumps({"stats": stats, "rows": rows}, indent=1))
    (out / "dry_run_plan.json").write_text(json.dumps({"actions": actions, "unrecoverable": unrecoverable.most_common()}, indent=1))
    print(json.dumps({"stats": stats, "actions": len(actions), "unrecoverable": unrecoverable.most_common()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
