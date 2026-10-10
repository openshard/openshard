#!/usr/bin/env python3
"""Read-only audit of an organisation's hosted Receipts and a recovery dry run.

Three steps, each writing a JSON file the next one reads:

  snapshot  GET every Receipt from the Platform (no other HTTP method is ever
            sent). Credentials come from ``OPENSHARD_API_KEY`` /
            ``OPENSHARD_API_BASE`` / ``OPENSHARD_ORG_ID`` or the linked
            ``~/.openshard/platform.json``; the key is never printed.
  evidence  Read commit trailers from local Git clones (``git log --all``).
  report    Completeness counts and a recovery dry run, as Markdown and JSON.

Evidence rules (OpenShard truth rules):

* Missing is not zero: a value is "present" only when recorded, never inferred.
* A Receipt is immutable. Recovery candidates name the original evidence and
  the later-evidence mechanism that could carry them; nothing is applied here.
* Only per-commit evidence can recover per-commit values. Session-level token
  or cost totals are never divided across commits.
* A ``Claude-Session`` trailer is a claim about the agent, not attribution.

Stdlib only, so it runs from a bare checkout: ``python scripts/receipt_audit.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

NOT_RECORDED = frozenset({"", "not recorded", "unknown", "none"})
# Labels that name an observation channel rather than an agent.
GENERIC_AGENT_LABELS = frozenset({"github observed cloud work"})
VERIFIED = frozenset({"passed", "failed"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.+?)[ \t]*$")
CLAUDE_SESSION_RE = re.compile(r"^https://claude\.ai/code/(session_[A-Za-z0-9]{8,128})$")
# Claude Code writes its model's display name into the co-author trailer
# (e.g. "Claude Fable 5.1"); a bare "Claude" names no model.
CLAUDE_COAUTHOR_MODEL_RE = re.compile(r"^Claude ((?:Fable|Opus|Sonnet|Haiku) \d+(?:\.\d+)?) <noreply@anthropic\.com>$")
OPENSHARD_KEYS = (
    "Openshard-Agent", "Openshard-Model", "Openshard-Provider", "Openshard-Surface", "Openshard-Owner",
    "Openshard-Cost-USD", "Openshard-Tokens-Input", "Openshard-Tokens-Output",
    "Openshard-Tokens-Cache-Read", "Openshard-Tokens-Cache-Creation",
)


# --------------------------------------------------------------------------- snapshot


class ReadOnlyClient:
    """A GET-only Platform client. There is deliberately no way to send a body."""

    def __init__(self, base: str, org: str, key: str, opener: Callable[..., Any] = urllib.request.urlopen):
        if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
            raise ValueError("The Platform endpoint must use https.")
        self._base = base.rstrip("/")
        self._org = org
        self._key = key
        self._open = opener

    def get(self, path: str, query: Mapping[str, str] | None = None) -> Any:
        url = f"{self._base}/v1/orgs/{urllib.parse.quote(self._org)}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self._key}", "Accept": "application/json",
        })
        with self._open(request, timeout=60) as response:
            return json.load(response)

    def receipts(self, page_size: int = 200) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            query = {"limit": str(page_size)}
            if cursor:
                query["cursor"] = cursor
            page = self.get("/receipts", query)
            out.extend(page.get("receipts") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return out


def credentials(env: Mapping[str, str] = os.environ) -> tuple[str, str, str]:
    """(endpoint, organisation id, key) from the environment or the linked platform.json."""
    if env.get("OPENSHARD_API_KEY"):
        base = env.get("OPENSHARD_API_BASE") or "https://api.openshard.dev"
        org = env.get("OPENSHARD_ORG_ID") or ""
        if not org:
            raise SystemExit("OPENSHARD_ORG_ID is required with OPENSHARD_API_KEY.")
        return base, org, env["OPENSHARD_API_KEY"]
    home = Path(env.get("OPENSHARD_HOME") or Path.home() / ".openshard")
    try:
        linked = json.loads((home / "platform.json").read_text(encoding="utf-8"))
        return linked["endpoint"], linked["organisation_id"], linked["api_key"]
    except (OSError, ValueError, KeyError):
        raise SystemExit("No Platform credential: set OPENSHARD_API_KEY/OPENSHARD_ORG_ID or link with `openshard login`.")


def snapshot(client: ReadOnlyClient) -> dict:
    receipts = client.receipts()
    facets = client.get("/receipts/facets")
    ids = [r.get("receipt_id") for r in receipts]
    if len(set(ids)) != len(ids):
        raise SystemExit("The receipt listing returned duplicate ids; refusing to report on it.")
    if isinstance(facets.get("receipt_count"), int) and facets["receipt_count"] != len(receipts):
        raise SystemExit(f"Listed {len(receipts)} Receipts but facets count {facets['receipt_count']}.")
    return {"receipt_count": len(receipts), "receipts": receipts}


# --------------------------------------------------------------------------- evidence


def parse_trailers(message: str) -> list[tuple[str, str]]:
    """Trailer lines of the last paragraph block(s) that look like ``Key: value``.

    Git trailers live in the final paragraph; Claude Code separates its
    session trailer from the co-author trailer with a blank line, so every
    trailing paragraph made only of trailer lines counts.
    """
    paragraphs = [p.strip("\n") for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    out: list[tuple[str, str]] = []
    for paragraph in reversed(paragraphs[1:]):
        lines = paragraph.splitlines()
        parsed = [TRAILER_RE.match(line) for line in lines]
        if not all(parsed):
            break
        out[:0] = [(m.group(1), m.group(2)) for m in parsed if m]
    return out


def commit_evidence(sha: str, message: str) -> dict:
    trailers = parse_trailers(message)
    lower = {}
    for key, value in trailers:
        lower.setdefault(key.lower(), []).append(value)
    declared = {key: lower[key.lower()][-1] for key in OPENSHARD_KEYS if key.lower() in lower}
    session = None
    for value in lower.get("claude-session", []):
        match = CLAUDE_SESSION_RE.match(value)
        if match:
            session = match.group(1)
    coauthor_models = sorted({
        m.group(1) for value in lower.get("co-authored-by", []) if (m := CLAUDE_COAUTHOR_MODEL_RE.match(value))
    })
    claude_coauthor = any(v.startswith("Claude") and v.endswith("<noreply@anthropic.com>") for v in lower.get("co-authored-by", []))
    pr = re.search(r"\(#(\d+)\)\s*$", message.splitlines()[0] if message else "")
    return {
        "sha": sha,
        "subject": message.splitlines()[0] if message else "",
        "pr_number": int(pr.group(1)) if pr else None,
        "declared": declared,
        "claude_session": session,
        "claude_coauthor": claude_coauthor,
        "claude_coauthor_models": coauthor_models,
    }


def scan_repository(path: Path, run: Callable[..., str] | None = None) -> list[dict]:
    """Evidence for every commit reachable from any ref in the local clone at *path*."""
    def _git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout

    output = (run or _git)("log", "--all", "--format=%H%x00%B%x00%x1e")
    out = []
    for record in output.split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, _, rest = record.partition("\x00")
        if SHA_RE.match(sha):
            out.append(commit_evidence(sha, rest.rstrip("\x00\n")))
    return out


# --------------------------------------------------------------------------- sessions

# A transcript may only recover a Receipt's usage when it ends inside that
# Receipt's own window; later activity would be attributed to the wrong record.
WINDOW_TOLERANCE_SECONDS = 120


def _usage_bounds(path: Path, kind: str) -> tuple[str | None, str | None]:
    """Timestamps of the first and last usage-bearing lines (Claude assistant usage / Codex token_count)."""
    first = last = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"usage"' not in line and '"token_count"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                usage_line = (
                    (kind == "claude" and isinstance((row.get("message") or {}).get("usage"), dict))
                    or (kind == "codex" and (row.get("payload") or {}).get("type") == "token_count")
                )
                if usage_line and isinstance(row.get("timestamp"), str):
                    first = first or row["timestamp"]
                    last = row["timestamp"]
    except OSError:
        return None, None
    return first, last


def _seconds(a: str, b: str) -> float | None:
    from datetime import datetime

    try:
        return (datetime.fromisoformat(a.replace("Z", "+00:00")) - datetime.fromisoformat(b.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


def session_candidates(receipts: Iterable[Mapping[str, Any]], histories: Iterable[Path],
                       claude_root: Path, codex_root: Path) -> list[dict]:
    """Usage for session Receipts missing tokens, read from their own local transcripts.

    A session Receipt *is* the session, so its transcript's usage belongs to
    it. Only single-segment, ended sessions whose transcript ends within the
    Receipt's last activity qualify; everything else is listed for review.
    Uses Core's own transcript readers (run from a Core checkout).
    """
    from openshard.adapters.claude_hooks import read_transcript_usage
    from openshard.adapters.codex_transcript import read_codex_transcript_usage

    records: dict[str, dict] = {}
    segments: Counter = Counter()
    for history in histories:
        try:
            lines = history.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("receipt_id"), str):
                if record["receipt_id"] not in records:
                    records[record["receipt_id"]] = record
                    segments[(record.get("capture") or {}).get("session_id")] += 1
    out = []
    for row in receipts:
        fact = facts(row)
        record = records.get(str(fact["receipt_id"]))
        if fact["has_tokens"] or record is None:
            continue
        capture = record.get("capture") or {}
        session = capture.get("session_id")
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", session):
            continue
        claude = sorted(claude_root.glob(f"*/{session}.jsonl"))
        codex = sorted(codex_root.glob(f"*/*/*/*{session}.jsonl"))
        if len(claude) + len(codex) != 1:
            continue
        kind, path = ("claude", claude[0]) if claude else ("codex", codex[0])
        usage = (read_transcript_usage(path, since=capture.get("started_at")) if kind == "claude"
                 else read_codex_transcript_usage(path, session))
        totals = (usage or {}).get("totals") or {}
        models = [m for m in ((usage or {}).get("by_model") or {}) if m != "unknown"]
        first, last = _usage_bounds(path, kind)
        ended = capture.get("session_end_observed") is True
        gap = _seconds(last, capture["last_activity_at"]) if last and capture.get("last_activity_at") else None
        reasons = []
        if segments[session] != 1:
            reasons.append("session has several Receipt segments")
        if not ended:
            reasons.append("session end was not observed")
        if gap is None or gap > WINDOW_TOLERANCE_SECONDS:
            reasons.append("transcript continues after the Receipt's last activity")
        lead = _seconds(capture["started_at"], first) if first and capture.get("started_at") else None
        if kind == "codex" and (lead is None or lead > WINDOW_TOLERANCE_SECONDS):
            # Codex counters are cumulative: usage before capture began would be counted.
            reasons.append("transcript usage starts before the Receipt's session start")
        if not usage or not usage.get("complete") or not (totals.get("input") or totals.get("output")):
            reasons.append("transcript has no complete usage")
        out.append({
            "receipt_id": fact["receipt_id"], "repo_identity": fact["repo_identity"], "commit": fact["commit"],
            "agent": fact["agent"], "field": "tokens",
            "value": {k: totals.get(k) for k in ("input", "output", "cache_read", "cache_creation")},
            "models": models,
            "confidence": "high" if not reasons else "review",
            "evidence": f"Local {kind} transcript of session {session[:8]}… (single segment, ended)" if not reasons
            else f"Local {kind} transcript: {'; '.join(reasons)}",
        })
    return out


# --------------------------------------------------------------------------- report


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return None if value.strip().lower() in NOT_RECORDED else value.strip()


def _usage(row: Mapping[str, Any]) -> Mapping[str, Any]:
    current = row.get("usage_current")
    usage = current.get("usage") if isinstance(current, Mapping) else None
    return usage if isinstance(usage, Mapping) else {}


def _known(dimension: Mapping[str, Any] | None, field: str) -> bool:
    if not isinstance(dimension, Mapping) or dimension.get("status") in (None, "pending", "unknown"):
        return False
    value = dimension.get(field)
    return isinstance(value, int | float) and value >= 0


def facts(row: Mapping[str, Any]) -> dict:
    """What one Receipt (with its later evidence) actually records."""
    receipt = row.get("receipt") if isinstance(row.get("receipt"), Mapping) else {}
    usage = _usage(row)
    model = _text((usage.get("model") or {}).get("id") if isinstance(usage.get("model"), Mapping) else None) or _text(receipt.get("model"))
    tokens = usage.get("tokens") if isinstance(usage.get("tokens"), Mapping) else None
    has_tokens = (
        isinstance(receipt.get("tokens_input"), int) or isinstance(receipt.get("tokens_output"), int)
        or _known(tokens, "input") or _known(tokens, "output")
    )
    cost = receipt.get("cost_usd")
    has_cost = (isinstance(cost, int | float) and cost >= 0) or _known(usage.get("cost") if isinstance(usage.get("cost"), Mapping) else None, "usd")
    agent = _text(row.get("agent") or receipt.get("agent"))
    # "OpenShard" outside OpenShard-routed runs is Core's label for a run whose
    # executor it could not establish (origin unknown), not an agent identity.
    agent_known = agent is not None and agent.lower() not in GENERIC_AGENT_LABELS and not (
        agent.lower() == "openshard" and row.get("origin") != "openshard_routed")
    state = (row.get("verification_current") or {}).get("state") if isinstance(row.get("verification_current"), Mapping) else None
    verification = (state or {}).get("effective_status") if isinstance(state, Mapping) else None
    verification = verification or row.get("verification_status") or receipt.get("verification_status")
    commit = receipt.get("commit") if isinstance(receipt.get("commit"), str) and SHA_RE.match(receipt["commit"]) else None
    return {
        "receipt_id": row.get("receipt_id"),
        "repo_identity": row.get("repo_identity"),
        "source_product": (row.get("source") or {}).get("product") if isinstance(row.get("source"), Mapping) else None,
        "origin": row.get("origin"),
        "agent": agent,
        "agent_known": agent_known,
        "model": model,
        "has_tokens": bool(has_tokens),
        "has_cost": bool(has_cost),
        "verification": verification,
        "verification_complete": verification in VERIFIED,
        "capture": row.get("capture_completeness_status"),
        "commit": commit,
    }


def _model_from_coauthor(name: str) -> str:
    return "claude-" + name.lower().replace(" ", "-").replace(".", "-")


def recovery_candidates(fact: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> list[dict]:
    """Original per-commit evidence that could fill a missing value. Never applied here."""
    if not evidence:
        return []
    out = []
    declared = evidence.get("declared") or {}
    claude_receipt = (fact.get("agent") or "").lower().startswith("claude code")
    if fact["model"] is None:
        if declared.get("Openshard-Model"):
            out.append({"field": "model", "value": declared["Openshard-Model"], "confidence": "high",
                        "evidence": "Openshard-Model trailer on the commit"})
        elif claude_receipt and len(evidence.get("claude_coauthor_models") or []) == 1:
            name = evidence["claude_coauthor_models"][0]
            out.append({"field": "model", "value": _model_from_coauthor(name), "confidence": "high",
                        "evidence": f"Claude Code co-author trailer names {name} on a Claude Code Receipt"})
        elif len(evidence.get("claude_coauthor_models") or []) == 1:
            name = evidence["claude_coauthor_models"][0]
            out.append({"field": "model", "value": _model_from_coauthor(name), "confidence": "review",
                        "evidence": f"Claude co-author trailer names {name}, but the Receipt does not name Claude Code"})
    if not fact["has_tokens"] and (declared.get("Openshard-Tokens-Input") or declared.get("Openshard-Tokens-Output")):
        out.append({"field": "tokens", "value": {k: declared[k] for k in declared if k.startswith("Openshard-Tokens")},
                    "confidence": "high", "evidence": "Per-commit token trailers"})
    if not fact["has_cost"] and declared.get("Openshard-Cost-USD"):
        out.append({"field": "cost", "value": declared["Openshard-Cost-USD"], "confidence": "high",
                    "evidence": "Per-commit Openshard-Cost-USD trailer"})
    if not fact["agent_known"]:
        if declared.get("Openshard-Agent"):
            out.append({"field": "agent", "value": declared["Openshard-Agent"], "confidence": "high",
                        "evidence": "Openshard-Agent trailer", "in_place": False})
        elif evidence.get("claude_session"):
            out.append({"field": "agent", "value": "Claude Code", "confidence": "claim",
                        "evidence": f"Claude-Session {evidence['claude_session']} needs a captured session to corroborate it",
                        "in_place": False})
    return out


def build_report(receipts: Iterable[Mapping[str, Any]], evidence: Mapping[str, Mapping[str, Any]],
                 sessions: Iterable[Mapping[str, Any]] = ()) -> dict:
    rows = [facts(r) for r in receipts]
    from_sessions: dict[str, list[dict]] = {}
    for candidate in sessions:
        from_sessions.setdefault(str(candidate["receipt_id"]), []).append(
            {k: v for k, v in candidate.items() if k not in ("receipt_id", "repo_identity", "commit", "agent")})
    totals = Counter()
    candidates: list[dict] = []
    for fact in rows:
        totals["total"] += 1
        totals["with_model"] += fact["model"] is not None
        totals["with_tokens"] += fact["has_tokens"]
        totals["with_cost"] += fact["has_cost"]
        totals["missing_agent"] += not fact["agent_known"]
        totals["incomplete_verification"] += not fact["verification_complete"]
        totals["incomplete_capture"] += fact["capture"] != "complete"
        missing = [name for name, ok in (
            ("model", fact["model"] is not None), ("tokens", fact["has_tokens"]), ("cost", fact["has_cost"]),
            ("agent", fact["agent_known"]),
        ) if not ok]
        found = recovery_candidates(fact, evidence.get(fact["commit"]) if fact["commit"] else None)
        found += from_sessions.get(str(fact["receipt_id"]), [])
        if missing:
            totals["with_missing_data"] += 1
        recoverable = [c for c in found if c["confidence"] == "high" and c.get("in_place", True)]
        if recoverable:
            totals["eligible_for_recovery"] += 1
        if found and not recoverable:
            totals["review_or_claim_only"] += 1
        if missing and not found:
            totals["unrecoverable"] += 1
        if fact["commit"] and fact["commit"] in evidence:
            totals["with_commit_evidence"] += 1
        for candidate in found:
            candidates.append({"receipt_id": fact["receipt_id"], "repo_identity": fact["repo_identity"],
                               "commit": fact["commit"], "agent": fact["agent"], **candidate})
    breakdown: dict[str, Counter] = {"origin": Counter(), "agent": Counter(), "verification": Counter(), "repo": Counter()}
    for fact in rows:
        breakdown["origin"][str(fact["origin"])] += 1
        breakdown["agent"][str(fact["agent"])] += 1
        breakdown["verification"][str(fact["verification"])] += 1
        breakdown["repo"][str(fact["repo_identity"])] += 1
    return {
        "totals": dict(totals),
        "breakdown": {k: dict(v.most_common()) for k, v in breakdown.items()},
        "candidates": candidates,
        "missing_by_origin": _missing_by(rows, "origin"),
    }


def _missing_by(rows: list[dict], key: str) -> dict:
    out: dict[str, Counter] = {}
    for fact in rows:
        bucket = out.setdefault(str(fact[key]), Counter())
        bucket["total"] += 1
        bucket["no_model"] += fact["model"] is None
        bucket["no_tokens"] += not fact["has_tokens"]
        bucket["no_cost"] += not fact["has_cost"]
        bucket["no_agent"] += not fact["agent_known"]
        bucket["verification_incomplete"] += not fact["verification_complete"]
    return {k: dict(v) for k, v in out.items()}


def render_markdown(report: Mapping[str, Any]) -> str:
    t = Counter(report["totals"])
    lines = [
        "# Receipt completeness audit and recovery dry run", "",
        "Read-only. Counts include later usage/verification evidence; missing values are never counted as zero.", "",
        "| Measure | Receipts |", "| --- | ---: |",
    ]
    for label, key in (
        ("Total Receipts", "total"), ("With model", "with_model"), ("With input/output tokens", "with_tokens"),
        ("With cost", "with_cost"), ("Missing agent attribution", "missing_agent"),
        ("Incomplete verification (not passed/failed)", "incomplete_verification"),
        ("Incomplete capture", "incomplete_capture"), ("With any missing model/tokens/cost/agent", "with_missing_data"),
        ("Bound to a commit with Git evidence", "with_commit_evidence"),
        ("Eligible for recovery (high-confidence per-commit evidence)", "eligible_for_recovery"),
        ("Evidence needs review or corroboration only", "review_or_claim_only"),
        ("Missing data with no recoverable evidence", "unrecoverable"),
    ):
        lines.append(f"| {label} | {t[key]} |")
    lines += ["", "## Missing data by origin", "", "| Origin | Total | No model | No tokens | No cost | No agent | Verification incomplete |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for origin, c in sorted(report["missing_by_origin"].items(), key=lambda kv: -kv[1]["total"]):
        lines.append(f"| {origin} | {c['total']} | {c.get('no_model', 0)} | {c.get('no_tokens', 0)} | {c.get('no_cost', 0)} | {c.get('no_agent', 0)} | {c.get('verification_incomplete', 0)} |")
    lines += ["", "## Recovery dry run", "", "Nothing below has been applied. Receipts are immutable; values could only be added as later evidence.", "",
              "| Receipt | Commit | Field | Value | Confidence | Evidence |", "| --- | --- | --- | --- | --- | --- |"]
    for c in report["candidates"]:
        value = c["value"] if isinstance(c["value"], str) else json.dumps(c["value"], sort_keys=True)
        if c.get("models"):
            value += f" ({', '.join(c['models'])})"
        lines.append(f"| {c['receipt_id']} | {(c['commit'] or '')[:12]} | {c['field']} | {value} | {c['confidence']} | {c['evidence']} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="GET every hosted Receipt (read-only)")
    snap.add_argument("--out", type=Path, required=True)
    ev = sub.add_parser("evidence", help="Commit trailer evidence from local Git clones")
    ev.add_argument("--repo", action="append", required=True, metavar="PATH", type=Path)
    ev.add_argument("--out", type=Path, required=True)
    ses = sub.add_parser("sessions", help="Usage from local transcripts of session Receipts missing tokens")
    ses.add_argument("--receipts", type=Path, required=True)
    ses.add_argument("--history", action="append", required=True, type=Path, metavar="RUNS_JSONL")
    ses.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    ses.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    ses.add_argument("--out", type=Path, required=True)
    rep = sub.add_parser("report", help="Completeness counts and recovery dry run")
    rep.add_argument("--receipts", type=Path, required=True)
    rep.add_argument("--evidence", type=Path, required=True)
    rep.add_argument("--sessions", type=Path, help="Output of the sessions step")
    rep.add_argument("--out-json", type=Path, required=True)
    rep.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        base, org, key = credentials()
        data = snapshot(ReadOnlyClient(base, org, key))
        args.out.write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"Snapshot: {data['receipt_count']} Receipts -> {args.out}")
    elif args.command == "evidence":
        commits: dict[str, dict] = {}
        for path in args.repo:
            for item in scan_repository(path):
                commits.setdefault(item["sha"], item)
        args.out.write_text(json.dumps({"commit_count": len(commits), "commits": commits}, indent=1), encoding="utf-8")
        print(f"Evidence: {len(commits)} commits -> {args.out}")
    elif args.command == "sessions":
        receipts = json.loads(args.receipts.read_text(encoding="utf-8"))["receipts"]
        found = session_candidates(receipts, args.history, args.claude_root, args.codex_root)
        args.out.write_text(json.dumps({"candidates": found}, indent=1), encoding="utf-8")
        print(f"Sessions: {len(found)} transcript candidates ({sum(c['confidence'] == 'high' for c in found)} high) -> {args.out}")
    else:
        receipts = json.loads(args.receipts.read_text(encoding="utf-8"))["receipts"]
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))["commits"]
        sessions = json.loads(args.sessions.read_text(encoding="utf-8"))["candidates"] if args.sessions else []
        report = build_report(receipts, evidence, sessions)
        args.out_json.write_text(json.dumps(report, indent=1), encoding="utf-8")
        args.out_md.write_text(render_markdown(report), encoding="utf-8")
        print(json.dumps(report["totals"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
